# Stage and execute one bounded Menhir security-config transaction.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet("SecurityConfig")][string]$Mode,
    [Parameter(Mandatory = $true)][string]$BundlePath,
    [Parameter(Mandatory = $true)][string]$ExpectedBundleSha256,
    [Parameter(Mandatory = $true)][string]$Release,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedReleaseSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedIngressContainerId,
    [Parameter(Mandatory = $true)][string]$SourceRepository,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedRootRunnerSha256,
    [Parameter(Mandatory = $true)][string]$TransactionReceipt
)

$ErrorActionPreference = "Stop"

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Get-BundleTreeSha256 {
    param([Parameter(Mandatory = $true)][string]$Root)
    $item = Get-Item -LiteralPath $Root -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Install bundle must be a non-reparse-point directory."
    }
    $prefix = $item.FullName.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $files = @{}
    foreach ($entry in Get-ChildItem -LiteralPath $item.FullName -Force -Recurse) {
        $relative = $entry.FullName.Substring($prefix.Length).Replace('\', '/')
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Install bundle contains a reparse point: $relative"
        }
        if (-not $entry.PSIsContainer) { $files[$relative] = $entry.FullName }
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
            $tree.AppendData($utf8.GetBytes("$relative`0$(Get-FileSha256 $files[$relative])`n"))
        }
        return ([BitConverter]::ToString($tree.GetHashAndReset())).Replace("-", "").ToLowerInvariant()
    }
    finally { $tree.Dispose() }
}

function Get-DockerCredential {
    param([Parameter(Mandatory = $true)][string]$Helper)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Helper
    $start.Arguments = "get"
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.CreateNoWindow = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw "Could not start Docker credential helper." }
        $process.StandardInput.WriteLine("ghcr.io")
        $process.StandardInput.Close()
        $output = $process.StandardOutput.ReadToEnd()
        $errorOutput = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "Docker credential helper failed: $errorOutput" }
        return $output | ConvertFrom-Json
    }
    finally { $process.Dispose() }
}

if (Test-Path -LiteralPath $TransactionReceipt) {
    throw "Root transaction receipt path must not already exist."
}
$scripts = Join-Path $env:USERPROFILE "IdeaProjects\scripts"
$sshScript = Join-Path $scripts "vps-ssh.ps1"
$scpScript = Join-Path $scripts "vps-scp.ps1"
foreach ($helper in @($sshScript, $scpScript)) {
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
        throw "Required VPS helper not found: $helper"
    }
}
$bundle = (Resolve-Path -LiteralPath $BundlePath).Path
if ($ExpectedBundleSha256 -notmatch '^[0-9a-f]{64}$' -or
    (Get-BundleTreeSha256 $bundle) -ne $ExpectedBundleSha256) {
    throw "Install bundle digest differs from the approved release."
}

$sourceManifestPath = Join-Path $bundle "bundle-manifest.json"
$sourceManifest = Get-Content -LiteralPath $sourceManifestPath -Raw | ConvertFrom-Json
$selected = [ordered]@{
    "release.json" = "/srv/menhir/production/release/release.json"
    "production.env" = "/srv/menhir/production/release/production.env"
    "client-policy.json" = "/srv/menhir/production/policy/client-policy.json"
    "operations-policy.json" = "/etc/yawn-vps/menhir-oauth-policy.json"
    "oauth-public.pem" = "/etc/yawn-vps/menhir-oauth-public.pem"
}
$uploadId = [Guid]::NewGuid().ToString("N")
$temporary = Join-Path ([IO.Path]::GetTempPath()) "menhir-security-config-$uploadId"
$remoteRoot = "/home/thron/.menhir-security-config-upload"
$remoteBundle = "$remoteRoot/security-$uploadId"
$remoteHost = if ($env:YAWN_VPS_HOST) { $env:YAWN_VPS_HOST } else { "thron@147.93.132.141" }
$remoteRunner = "/srv/menhir/scaffold/bin/menhir_security_config.py"
[IO.Directory]::CreateDirectory($temporary) | Out-Null
try {
    foreach ($name in $selected.Keys) {
        $destination = $selected[$name]
        $source = Join-Path $bundle ("rootfs" + $destination.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Security-config source bundle is missing $destination"
        }
        if ($sourceManifest.files.$destination.sha256 -ne (Get-FileSha256 $source)) {
            throw "Source bundle does not bind $destination"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $temporary $name)
    }
    $releaseValue = Get-Content -LiteralPath (Join-Path $temporary "release.json") -Raw | ConvertFrom-Json
    if ((Get-FileSha256 (Join-Path $temporary "release.json")) -ne $ExpectedReleaseSha256 -or
        $releaseValue.release_id -ne $Release -or
        $releaseValue.deployment_class -ne "security-config" -or
        $releaseValue.ingress_mode -ne "cloudflared") {
        throw "Release is not the approved Cloudflared security-config authority."
    }
    $existingReceiptJson = (& $sshScript "sudo -n $remoteRunner receipt" 2>$null | Out-String).Trim()
    $existingReceiptExit = $LASTEXITCODE
    if ($existingReceiptExit -eq 0 -and -not [string]::IsNullOrWhiteSpace($existingReceiptJson)) {
        $existingReceipt = $existingReceiptJson | ConvertFrom-Json
        if ($existingReceipt.kind -eq "menhir-security-config-transaction" -and
            $existingReceipt.result -eq "passed" -and $existingReceipt.stage -eq "complete" -and
            $existingReceipt.candidate_release_id -eq $Release -and
            $existingReceipt.candidate_release_sha256 -eq $ExpectedReleaseSha256 -and
            $existingReceipt.ingress_container_id_after -eq $ExpectedIngressContainerId -and
            $existingReceipt.runner_sha256 -eq $ExpectedRootRunnerSha256) {
            [IO.File]::WriteAllText(
                $TransactionReceipt,
                ($existingReceipt | ConvertTo-Json -Depth 8),
                [Text.UTF8Encoding]::new($false)
            )
            exit 0
        }
    }
    Copy-Item -LiteralPath $sourceManifestPath -Destination (Join-Path $temporary "source-manifest.json")

    $credentialHelper = Get-Command docker-credential-desktop -ErrorAction Stop
    $credential = Get-DockerCredential -Helper $credentialHelper.Source
    if ([string]::IsNullOrWhiteSpace($credential.Username) -or
        [string]::IsNullOrWhiteSpace($credential.Secret)) {
        throw "Docker Desktop did not provide the existing GHCR credential."
    }
    $auth = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes("$($credential.Username):$($credential.Secret)")
    )
    [IO.File]::WriteAllText(
        (Join-Path $temporary "docker-config.json"),
        (@{ auths = @{ "ghcr.io" = @{ auth = $auth } } } | ConvertTo-Json -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    $credential = $null
    $auth = $null

    $files = [ordered]@{}
    foreach ($file in Get-ChildItem -LiteralPath $temporary -File) {
        $files[$file.Name] = Get-FileSha256 $file.FullName
    }
    $manifest = [ordered]@{
        schema = 1
        kind = "menhir-security-config-bundle"
        source_bundle_sha256 = $files["source-manifest.json"]
        files = $files
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content `
        -LiteralPath (Join-Path $temporary "security-config-manifest.json") -Encoding ascii

    & $sshScript "install -d -m 0700 '$remoteRoot'"
    if ($LASTEXITCODE -ne 0) { throw "Could not create remote security-config upload root." }
    & $scpScript -Source $temporary -Destination "${remoteHost}:$remoteBundle" -Recurse
    if ($LASTEXITCODE -ne 0) { throw "Could not upload the security-config bundle." }
    & $sshScript "chmod 0700 '$remoteBundle' && chmod 0600 '$remoteBundle'/*"
    if ($LASTEXITCODE -ne 0) { throw "Could not restrict the security-config upload." }
    $receiptJson = (& $sshScript "sudo -n $remoteRunner deploy $uploadId $ExpectedRootRunnerSha256 $ExpectedReleaseSha256 $ExpectedIngressContainerId" | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($receiptJson)) {
        throw "Security-config root transaction failed."
    }
    $receipt = $receiptJson | ConvertFrom-Json
    if ($receipt.kind -ne "menhir-security-config-transaction" -or
        $receipt.result -ne "passed" -or $receipt.stage -ne "complete" -or
        $receipt.candidate_release_id -ne $Release -or
        $receipt.candidate_release_sha256 -ne $ExpectedReleaseSha256 -or
        $receipt.runner_sha256 -ne $ExpectedRootRunnerSha256) {
        throw "Security-config root transaction returned an invalid receipt."
    }
    [IO.File]::WriteAllText(
        $TransactionReceipt,
        ($receipt | ConvertTo-Json -Depth 8),
        [Text.UTF8Encoding]::new($false)
    )
}
finally {
    if ($remoteBundle -match '^/home/thron/\.menhir-security-config-upload/security-[a-f0-9]{32}$') {
        & $sshScript "rm -rf -- '$remoteBundle'" | Out-Null
    }
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force
    }
}
