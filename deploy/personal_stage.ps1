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
if ($menhirImage -notmatch '^ghcr\.io/[a-z0-9._/-]+(?::[a-zA-Z0-9._-]+)?@sha256:[0-9a-f]{64}$') {
    throw "Bundled MENHIR_IMAGE is not an immutable GHCR reference."
}
$runner = Join-Path $PSScriptRoot "personal_stage_vps.py"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "VPS staging runner is missing: $runner"
}
$runnerSha = Get-FileSha256 -Path $runner
$helpers = Resolve-OperatorScripts
$remoteHost = if ($env:YAWN_VPS_HOST) { $env:YAWN_VPS_HOST } else { "thron@147.93.132.141" }
$uploadId = [Guid]::NewGuid().ToString("N")
$remoteRoot = "/home/thron/.menhir-stage-upload/$uploadId"
$remoteBundle = "$remoteRoot/bundle"
$remoteReceipt = "$remoteRoot/staging-receipt.json"
$localTemp = "$receiptPath.$uploadId.tmp"
$localImageTar = "$receiptPath.$uploadId.image.tar"
$localImageIdentity = "$receiptPath.$uploadId.image-identity.json"
$remoteImageTar = "$remoteRoot/menhir-image.tar"
$remoteImageIdentity = "$remoteRoot/menhir-image-identity.json"
$installedRunner = "/srv/menhir/scaffold/bin/menhir_stage_vps.py"
$temporaryImageTag = "menhir-stage-export:$uploadId"
$temporaryImageTagCreated = $false

function Invoke-Vps {
    param([Parameter(Mandatory = $true)][string]$Command)
    & $helpers.Ssh $Command
    if ($LASTEXITCODE -ne 0) {
        throw "VPS command failed with exit code $LASTEXITCODE."
    }
}

try {
    $menhirImageInspectOutput = @(& docker image inspect $menhirImage)
    if ($LASTEXITCODE -ne 0) {
        & docker pull $menhirImage
        if ($LASTEXITCODE -ne 0) {
            throw "Could not obtain the selected Menhir image locally."
        }
        $menhirImageInspectOutput = @(& docker image inspect $menhirImage)
        if ($LASTEXITCODE -ne 0) {
            throw "Could not inspect the selected Menhir image after pulling it."
        }
    }
    $menhirImageInspect = @(($menhirImageInspectOutput -join [Environment]::NewLine) | ConvertFrom-Json)
    if ($menhirImageInspect.Count -ne 1) {
        throw "Selected Menhir image inspection was ambiguous."
    }
    $menhirImageValue = $menhirImageInspect[0]
    $menhirImageId = [string]$menhirImageValue.Id
    if ($menhirImageId -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Selected Menhir image has an invalid local image ID."
    }
    $selectedDigest = $menhirImage.Split("@", 2)[1]
    $matchingRepoDigests = @($menhirImageValue.RepoDigests | Where-Object {
        $_ -is [string] -and $_.EndsWith("@$selectedDigest", [StringComparison]::Ordinal)
    })
    if ($matchingRepoDigests.Count -lt 1) {
        throw "Selected Menhir image inspection is not bound to the requested registry digest."
    }
    if ($null -eq $menhirImageValue.Config -or
        $null -eq $menhirImageValue.RootFS -or
        $menhirImageValue.RootFS.Type -ne "layers" -or
        @($menhirImageValue.RootFS.Layers).Count -lt 1 -or
        @($menhirImageValue.RootFS.Layers | Where-Object {
            $_ -isnot [string] -or $_ -notmatch '^sha256:[0-9a-f]{64}$'
        }).Count -ne 0) {
        throw "Selected Menhir image has invalid configuration or layer identity metadata."
    }
    $identity = [ordered]@{
        schema = 1
        image_ref = $menhirImage
        image_id = $menhirImageId
        export_tag = $temporaryImageTag
        config = $menhirImageValue.Config
        rootfs = $menhirImageValue.RootFS
    }
    $utf8NoBom = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText(
        $localImageIdentity,
        (($identity | ConvertTo-Json -Depth 100) + "`n"),
        $utf8NoBom
    )
    $menhirImageIdentitySha256 = Get-FileSha256 -Path $localImageIdentity

    & docker image tag $menhirImageId $temporaryImageTag
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the temporary Menhir image export tag."
    }
    $temporaryImageTagCreated = $true
    $taggedImageId = @(& docker image inspect --format '{{.Id}}' $temporaryImageTag)
    if ($LASTEXITCODE -ne 0 -or $taggedImageId.Count -ne 1 -or
        $taggedImageId[0].Trim() -ne $menhirImageId) {
        throw "Temporary Menhir image export tag does not resolve to the selected image ID."
    }
    & docker save --output $localImageTar $temporaryImageTag
    if ($LASTEXITCODE -ne 0) {
        throw "Could not export the selected Menhir image."
    }
    $menhirImageArchiveSha256 = Get-FileSha256 -Path $localImageTar
    Invoke-Vps "install -d -m 0700 '/home/thron/.menhir-stage-upload' '$remoteRoot'"
    & $helpers.Scp -Source $bundlePath -Destination "${remoteHost}:$remoteBundle" -Recurse
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload the selected install bundle."
    }
    & $helpers.Scp -Source $localImageTar -Destination "${remoteHost}:$remoteImageTar"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload the selected Menhir image."
    }
    & $helpers.Scp -Source $localImageIdentity -Destination "${remoteHost}:$remoteImageIdentity"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload the selected Menhir image identity."
    }
    Invoke-Vps "sudo -n /usr/bin/python3 '$installedRunner' --upload-root '$remoteRoot' --expected-bundle-sha256 '$ExpectedBundleSha256' --expected-release-id '$ExpectedReleaseId' --expected-release-sha256 '$ExpectedReleaseSha256' --expected-menhir-image-archive-sha256 '$menhirImageArchiveSha256' --expected-menhir-image-identity-sha256 '$menhirImageIdentitySha256' --deployment-class '$DeploymentClass' --expected-runner-sha256 '$runnerSha'"
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
    if (Test-Path -LiteralPath $localImageIdentity) {
        Remove-Item -LiteralPath $localImageIdentity -Force
    }
    if ($temporaryImageTagCreated) {
        & docker image rm $temporaryImageTag *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Could not remove the temporary Menhir image export tag: $temporaryImageTag"
        }
    }
    try {
        Invoke-Vps "rm -rf -- '$remoteRoot'"
    }
    catch {
        Write-Warning "Could not remove the exact temporary VPS staging upload: $remoteRoot"
    }
}
