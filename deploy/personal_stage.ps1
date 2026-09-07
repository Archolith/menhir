# Upload and run one immutable Menhir bundle in isolated VPS staging.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Bundle,
    [Parameter(Mandatory = $true)][string]$ExpectedBundleSha256,
    [Parameter(Mandatory = $true)][string]$ExpectedReleaseId,
    [Parameter(Mandatory = $true)][string]$ExpectedReleaseSha256,
    [Parameter(Mandatory = $true)]
    [ValidateSet("app-only", "security-config", "maintenance")]
    [string]$DeploymentClass,
    [Parameter(Mandatory = $true)][string]$Receipt,
    [Parameter(Mandatory = $true)][string]$Workspace
)

$ErrorActionPreference = "Stop"

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Get-BundleTreeSha256 {
    param([Parameter(Mandatory = $true)][string]$Root)
    $rootItem = Get-Item -LiteralPath $Root -Force
    if (-not $rootItem.PSIsContainer -or
        ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Install bundle must be a non-reparse-point directory."
    }
    $rootPrefix = $rootItem.FullName.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $files = @{}
    foreach ($entry in Get-ChildItem -LiteralPath $rootItem.FullName -Force -Recurse) {
        $relative = $entry.FullName.Substring($rootPrefix.Length).Replace('\', '/')
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Install bundle contains a reparse point: $relative"
        }
        if (-not $entry.PSIsContainer) {
            if ($entry -isnot [IO.FileInfo]) {
                throw "Install bundle contains a special entry: $relative"
            }
            $files[$relative] = $entry.FullName
        }
    }
    $names = [Collections.Generic.List[string]]::new()
    $names.AddRange([string[]]$files.Keys)
    $names.Sort([StringComparer]::Ordinal)
    $tree = [Security.Cryptography.IncrementalHash]::CreateHash(
        [Security.Cryptography.HashAlgorithmName]::SHA256
    )
    $utf8 = [Text.UTF8Encoding]::new($false, $true)
    try {
        foreach ($relative in $names) {
            $fileHash = Get-FileSha256 -Path $files[$relative]
            $tree.AppendData($utf8.GetBytes("$relative`0$fileHash`n"))
        }
        return ([BitConverter]::ToString($tree.GetHashAndReset())).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $tree.Dispose()
    }
}

function Resolve-OperatorScripts {
    $candidates = @()
    if ($env:MENHIR_OPERATOR_SCRIPTS) {
        $candidates += $env:MENHIR_OPERATOR_SCRIPTS
    }
    $candidates += (Join-Path $env:USERPROFILE "IdeaProjects\scripts")
    $candidates += (Join-Path $PSScriptRoot "..\..\..\scripts")
    foreach ($candidate in $candidates) {
        $ssh = Join-Path $candidate "vps-ssh.ps1"
        $scp = Join-Path $candidate "vps-scp.ps1"
        if ((Test-Path -LiteralPath $ssh -PathType Leaf) -and
            (Test-Path -LiteralPath $scp -PathType Leaf)) {
            return @{ Ssh = $ssh; Scp = $scp }
        }
    }
    throw "Could not find vps-ssh.ps1 and vps-scp.ps1. Set MENHIR_OPERATOR_SCRIPTS."
}

if ($ExpectedBundleSha256 -notmatch '^[0-9a-f]{64}$' -or
    $ExpectedReleaseSha256 -notmatch '^[0-9a-f]{64}$' -or
    $ExpectedReleaseId -notmatch '^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$') {
    throw "Staging release identity is invalid."
}
$bundlePath = (Resolve-Path -LiteralPath $Bundle).Path
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$receiptPath = [IO.Path]::GetFullPath($Receipt)
if ([IO.Path]::GetDirectoryName($receiptPath) -ne $workspacePath) {
    throw "Staging receipt must be written directly inside the personal deployment workspace."
}
if (Test-Path -LiteralPath $receiptPath) {
    throw "Staging receipt path already exists."
}
if ((Get-BundleTreeSha256 -Root $bundlePath) -ne $ExpectedBundleSha256) {
    throw "Install bundle digest differs from the selected product release."
}
$productionEnv = Join-Path $bundlePath "rootfs\srv\menhir\production\release\production.env"
if (-not (Test-Path -LiteralPath $productionEnv -PathType Leaf)) {
    throw "Bundled production environment is missing."
}
$menhirImageRows = @(Get-Content -LiteralPath $productionEnv | Where-Object {
    $_ -match '^MENHIR_IMAGE='
})
if ($menhirImageRows.Count -ne 1) {
    throw "Bundled production environment must contain exactly one MENHIR_IMAGE."
}
$menhirImage = $menhirImageRows[0].Substring("MENHIR_IMAGE=".Length).Trim("'`"")
if ($menhirImage -notmatch '^ghcr\.io/[a-z0-9._/-]+:[a-zA-Z0-9._-]+@sha256:[0-9a-f]{64}$') {
    throw "Bundled MENHIR_IMAGE is not an immutable GHCR reference."
}
$menhirImageTag = $menhirImage.Split("@", 2)[0]
$runner = Join-Path $PSScriptRoot "personal_stage_vps.py"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "VPS staging runner is missing: $runner"
}
$wrapperSha = Get-FileSha256 -Path $PSCommandPath
$vpsRunnerSha = Get-FileSha256 -Path $runner
$runnerIdentity = [Security.Cryptography.IncrementalHash]::CreateHash(
    [Security.Cryptography.HashAlgorithmName]::SHA256
)
$identityUtf8 = [Text.UTF8Encoding]::new($false, $true)
try {
    $runnerIdentity.AppendData($identityUtf8.GetBytes("personal_stage.ps1`0$wrapperSha`n"))
    $runnerIdentity.AppendData($identityUtf8.GetBytes("personal_stage_vps.py`0$vpsRunnerSha`n"))
    $runnerSha = ([BitConverter]::ToString($runnerIdentity.GetHashAndReset())).Replace("-", "").ToLowerInvariant()
}
finally {
    $runnerIdentity.Dispose()
}
$helpers = Resolve-OperatorScripts
$remoteHost = if ($env:YAWN_VPS_HOST) { $env:YAWN_VPS_HOST } else { "thron@147.93.132.141" }
$uploadId = [Guid]::NewGuid().ToString("N")
$remoteRoot = "/home/thron/.menhir-stage-upload/$uploadId"
$remoteBundle = "$remoteRoot/bundle"
$remoteRunner = "$remoteRoot/personal_stage_vps.py"
$remoteReceipt = "$remoteRoot/staging-receipt.json"
$localTemp = "$receiptPath.$uploadId.tmp"
$localImageTar = "$receiptPath.$uploadId.image.tar"
$remoteImageTar = "$remoteRoot/menhir-image.tar"

function Invoke-Vps {
    param([Parameter(Mandatory = $true)][string]$Command)
    & $helpers.Ssh $Command
    if ($LASTEXITCODE -ne 0) {
        throw "VPS command failed with exit code $LASTEXITCODE."
    }
}

try {
    & docker image inspect $menhirImage *> $null
    if ($LASTEXITCODE -ne 0) {
        & docker pull $menhirImage
        if ($LASTEXITCODE -ne 0) {
            throw "Could not obtain the selected Menhir image locally."
        }
    }
    $menhirImageId = (& docker image inspect --format '{{.Id}}' $menhirImage).Trim()
    if ($LASTEXITCODE -ne 0 -or $menhirImageId -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Could not resolve the selected Menhir image ID."
    }
    & docker save --output $localImageTar $menhirImageTag
    if ($LASTEXITCODE -ne 0) {
        throw "Could not export the selected Menhir image."
    }
    Invoke-Vps "install -d -m 0700 '/home/thron/.menhir-stage-upload' '$remoteRoot'"
    & $helpers.Scp -Source $bundlePath -Destination "${remoteHost}:$remoteBundle" -Recurse
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload the selected install bundle."
    }
    & $helpers.Scp -Source $runner -Destination "${remoteHost}:$remoteRunner"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload the staging runner."
    }
    & $helpers.Scp -Source $localImageTar -Destination "${remoteHost}:$remoteImageTar"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload the selected Menhir image."
    }
    Invoke-Vps "sudo -n docker load --input '$remoteImageTar'"
    Invoke-Vps "chmod 0600 '$remoteRunner' && sudo -n python3 '$remoteRunner' --bundle '$remoteBundle' --expected-bundle-sha256 '$ExpectedBundleSha256' --expected-release-id '$ExpectedReleaseId' --expected-release-sha256 '$ExpectedReleaseSha256' --expected-menhir-image-id '$menhirImageId' --deployment-class '$DeploymentClass' --runner-sha256 '$runnerSha' --receipt '$remoteReceipt'"
    Invoke-Vps "sudo -n chown thron:thron '$remoteReceipt' && chmod 0600 '$remoteReceipt'"
    & $helpers.Scp -Source "${remoteHost}:$remoteReceipt" -Destination $localTemp
    if ($LASTEXITCODE -ne 0) {
        throw "Could not retrieve the staging receipt."
    }
    Move-Item -LiteralPath $localTemp -Destination $receiptPath
}
finally {
    if (Test-Path -LiteralPath $localTemp) {
        Remove-Item -LiteralPath $localTemp -Force
    }
    if (Test-Path -LiteralPath $localImageTar) {
        Remove-Item -LiteralPath $localImageTar -Force
    }
    try {
        Invoke-Vps "rm -rf -- '$remoteRoot'"
    }
    catch {
        Write-Warning "Could not remove the exact temporary VPS staging upload: $remoteRoot"
    }
}
