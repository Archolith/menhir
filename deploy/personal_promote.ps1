# Verify the exact staging evidence and owner approval before invoking Menhir's
# existing production deployment transaction.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet("Maintenance", "AppOnly")][string]$Mode,
    [Parameter(Mandatory = $true)][string]$BundlePath,
    [Parameter(Mandatory = $true)][string]$ExpectedBundleSha256,
    [Parameter(Mandatory = $true)][string]$Release,
    [Parameter(Mandatory = $true)][string]$ExpectedReleaseSha256,
    [Parameter(Mandatory = $true)][string]$StagingReceipt,
    [Parameter(Mandatory = $true)][string]$ExpectedStagingReceiptSha256,
    [Parameter(Mandatory = $true)][string]$Approval,
    [Parameter(Mandatory = $true)][string]$ExpectedApprovalSha256,
    [Parameter(Mandatory = $true)][string]$SourceRepository
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

function Read-JsonEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "$Label must be a regular non-reparse-point file."
    }
    if ((Get-FileSha256 -Path $item.FullName) -ne $ExpectedSha256) {
        throw "$Label digest differs from the approved personal deployment state."
    }
    return Get-Content -LiteralPath $item.FullName -Raw | ConvertFrom-Json
}

function Assert-ExactProperties {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    if ((Compare-Object ($Expected | Sort-Object) $actual).Count -ne 0) {
        throw "$Label schema has missing or unknown fields."
    }
}

function Assert-UtcTimestamp {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$Recent
    )
    $parsed = [DateTimeOffset]::MinValue
    if ($Value -is [DateTime]) {
        if ($Value.Kind -ne [DateTimeKind]::Utc) {
            throw "$Label must be an ISO-8601 UTC timestamp."
        }
        $parsed = [DateTimeOffset]::new($Value)
    }
    elseif ($Value -is [DateTimeOffset]) {
        $parsed = $Value
    }
    else {
        $text = [string]$Value
        $parsedOk = [DateTimeOffset]::TryParse(
            $text,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind,
            [ref]$parsed
        )
        if (-not $text.EndsWith("Z", [StringComparison]::Ordinal) -or -not $parsedOk) {
            throw "$Label must be an ISO-8601 UTC timestamp."
        }
    }
    if ($parsed.Offset -ne [TimeSpan]::Zero) {
        throw "$Label must be an ISO-8601 UTC timestamp."
    }
    $age = [DateTimeOffset]::UtcNow - $parsed
    if ($age.TotalMinutes -lt -1) {
        throw "$Label is in the future."
    }
    if ($Recent -and $age.TotalHours -gt 24) {
        throw "$Label is older than 24 hours; restage before promotion."
    }
    return $parsed
}

$digests = @(
    $ExpectedBundleSha256,
    $ExpectedReleaseSha256,
    $ExpectedStagingReceiptSha256,
    $ExpectedApprovalSha256
)
if ($digests.Where({ $_ -notmatch '^[0-9a-f]{64}$' }).Count -ne 0 -or
    $Release -notmatch '^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$') {
    throw "Personal promotion identity is invalid."
}

$bundle = (Resolve-Path -LiteralPath $BundlePath).Path
if ((Get-BundleTreeSha256 -Root $bundle) -ne $ExpectedBundleSha256) {
    throw "Install bundle digest differs from the staged release."
}
$releasePath = Join-Path $bundle "rootfs\srv\menhir\production\release\release.json"
if ((Get-FileSha256 -Path $releasePath) -ne $ExpectedReleaseSha256) {
    throw "Bundled release authority digest differs from the staged release."
}
$releaseAuthority = Get-Content -LiteralPath $releasePath -Raw | ConvertFrom-Json
if ([string]$releaseAuthority.release_id -ne $Release) {
    throw "Bundled release authority identity differs from the approved release."
}
$authorityClass = [string]$releaseAuthority.deployment_class
if ($authorityClass -notin @("app-only", "security-config", "maintenance")) {
    throw "Bundled release authority deployment class is invalid."
}
$authorityMode = if ($authorityClass -eq "app-only") { "AppOnly" } else { "Maintenance" }
if ($Mode -ne $authorityMode) {
    throw "Promotion mode differs from the immutable release authority."
}

$staging = Read-JsonEvidence -Path $StagingReceipt `
    -ExpectedSha256 $ExpectedStagingReceiptSha256 -Label "Staging receipt"
$approvalValue = Read-JsonEvidence -Path $Approval `
    -ExpectedSha256 $ExpectedApprovalSha256 -Label "Promotion approval"

Assert-ExactProperties -Value $staging -Label "Staging receipt" -Expected @(
    "schema", "kind", "result", "release_id", "release_sha256", "bundle_sha256",
    "deployment_class", "images", "runner_sha256", "started_utc", "completed_utc",
    "test_identities", "checks", "production_preflight"
)
Assert-ExactProperties -Value $approvalValue -Label "Promotion approval" -Expected @(
    "schema", "kind", "release_id", "release_sha256", "bundle_sha256",
    "staging_receipt_sha256", "approved_by", "approved_utc"
)

$expectedClass = $authorityClass
if ($staging.schema -ne 1 -or $staging.kind -ne "menhir-personal-staging" -or
    $staging.result -ne "passed" -or $staging.release_id -ne $Release -or
    $staging.release_sha256 -ne $ExpectedReleaseSha256 -or
    $staging.bundle_sha256 -ne $ExpectedBundleSha256 -or
    $staging.deployment_class -ne $expectedClass) {
    throw "Staging receipt is not bound to this production promotion."
}
if ($staging.images.menhir -ne $releaseAuthority.images.menhir -or
    $staging.images.neo4j -ne $releaseAuthority.images.neo4j) {
    throw "Staging receipt image identity differs from the release authority."
}
if ($staging.runner_sha256 -notmatch '^[0-9a-f]{64}$' -or
    $staging.test_identities.oauth_client_id -ne "menhir-staging-probe" -or
    $staging.test_identities.subject -ne "menhir-admin" -or
    $staging.test_identities.namespace -ne "menhir-staging") {
    throw "Staging receipt runner or test identity is invalid."
}
$preflight = $staging.production_preflight
Assert-ExactProperties -Value $preflight -Label "Production readiness preflight" -Expected @(
    "schema", "kind", "result", "observed_utc", "deployment_class",
    "candidate_release_id", "checks", "canonical_sha256"
)
Assert-ExactProperties -Value $preflight.checks -Label "Production readiness preflight checks" -Expected @(
    "live_services", "network_roles", "release_journal", "headroom", "maintenance_route"
)
Assert-ExactProperties -Value $preflight.checks.headroom -Label "Production readiness headroom" -Expected @(
    "disk_free_bytes", "disk_required_bytes", "memory_available_bytes", "memory_required_bytes"
)
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($null -eq $pythonCommand) {
    throw "Python is required to verify the production readiness preflight seal."
}
$sealProgram = @'
import hashlib, json, sys
def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value
with open(sys.argv[1], encoding="utf-8") as handle:
    receipt = json.load(handle, object_pairs_hook=unique)
preflight = receipt["production_preflight"]
actual = preflight.pop("canonical_sha256")
encoded = json.dumps(preflight, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
raise SystemExit(0 if actual == hashlib.sha256(encoded).hexdigest() else 1)
'@
$sealProgram | & $pythonCommand.Source - $StagingReceipt
if ($LASTEXITCODE -ne 0) {
    throw "Production readiness preflight seal is invalid."
}
$routeRequired = $expectedClass -ne "app-only"
if ($preflight.schema -ne 1 -or
    $preflight.kind -ne "menhir-production-readiness-preflight" -or
    $preflight.result -ne "passed" -or
    $preflight.deployment_class -ne $expectedClass -or
    $preflight.candidate_release_id -ne $Release -or
    [string]$preflight.canonical_sha256 -notmatch '^[0-9a-f]{64}$' -or
    [bool]$preflight.checks.maintenance_route.applicable -ne $routeRequired -or
    [long]$preflight.checks.headroom.disk_free_bytes -lt [long]$preflight.checks.headroom.disk_required_bytes -or
    [long]$preflight.checks.headroom.memory_available_bytes -lt [long]$preflight.checks.headroom.memory_required_bytes) {
    throw "Production readiness preflight is not bound to this promotion."
}
$requiredChecks = @(
    "artifact_identity", "production_memory_limits", "production_network_shape",
    "oauth_policy_shape", "ingress_request_handling", "isolated_disposable_data",
    "non_production_credentials", "production_authority_absent", "oauth_discovery",
    "oauth_authorization_code_pkce", "mcp_initialize", "mcp_tools_list", "mcp_recall",
    "synthetic_write_allowed", "denied_operation_refused", "restart_persistence",
    "automatic_rollback"
)
$actualChecks = @($staging.checks.PSObject.Properties.Name | Sort-Object)
if ((Compare-Object ($requiredChecks | Sort-Object) $actualChecks).Count -ne 0) {
    throw "Staging receipt has an incomplete or unknown check set."
}
foreach ($check in $requiredChecks) {
    if ($staging.checks.$check -ne $true) {
        throw "Staging receipt did not pass required check: $check"
    }
}
$startedAt = Assert-UtcTimestamp -Value $staging.started_utc -Label "Staging start"
$preflightAt = Assert-UtcTimestamp -Value $preflight.observed_utc -Label "Production preflight observation"
$completedAt = Assert-UtcTimestamp -Value $staging.completed_utc -Label "Staging completion" -Recent
if ($preflightAt -gt $startedAt -or $completedAt -lt $startedAt) {
    throw "Staging completion precedes staging start."
}

if ($approvalValue.schema -ne 1 -or
    $approvalValue.kind -ne "menhir-personal-promotion-approval" -or
    $approvalValue.release_id -ne $Release -or
    $approvalValue.release_sha256 -ne $ExpectedReleaseSha256 -or
    $approvalValue.bundle_sha256 -ne $ExpectedBundleSha256 -or
    $approvalValue.staging_receipt_sha256 -ne $ExpectedStagingReceiptSha256 -or
    [string]$approvalValue.approved_by -notmatch '^[A-Za-z0-9._@+-]{1,128}$') {
    throw "Owner approval is not bound to this staged release."
}
$approvedAt = Assert-UtcTimestamp -Value $approvalValue.approved_utc -Label "Approval time"
if ($approvedAt -lt $completedAt) {
    throw "Owner approval predates the completed staging receipt."
}

$operatorWrapper = if ($env:MENHIR_OPERATOR_DEPLOY_WRAPPER) {
    $env:MENHIR_OPERATOR_DEPLOY_WRAPPER
}
else {
    Join-Path $env:USERPROFILE "IdeaProjects\scripts\deploy-menhir.ps1"
}
if (-not (Test-Path -LiteralPath $operatorWrapper -PathType Leaf)) {
    throw "Existing Menhir production transaction wrapper was not found: $operatorWrapper"
}

$global:LASTEXITCODE = 0
& $operatorWrapper -Mode $Mode -BundlePath $bundle `
    -ExpectedBundleSha256 $ExpectedBundleSha256 -Release $Release `
    -SourceRepository $SourceRepository
$powerShellSucceeded = $?
if (-not $powerShellSucceeded -or $LASTEXITCODE -ne 0) {
    throw "Menhir production transaction failed."
}
