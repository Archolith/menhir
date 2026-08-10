param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "check", "unit", "test", "online", "smoke", "integration", "clean")]
    [string]
    $Task = "check",

    [string] $Python = ""
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$srcRoot = Join-Path $projectRoot "src"
$tmpRoot = Join-Path $projectRoot ".agent\test_tmp"
$pytestBaseTmp = Join-Path $tmpRoot "pytest"

$env:PYTHONPATH = $srcRoot

if (-not (Test-Path $tmpRoot)) {
    New-Item -ItemType Directory -Path $tmpRoot -Force | Out-Null
}
if (-not (Test-Path $pytestBaseTmp)) {
    New-Item -ItemType Directory -Path $pytestBaseTmp -Force | Out-Null
}

# Force project-local temp paths so constrained/locked system temp dirs do not break tooling.
$env:TEMP = $tmpRoot
$env:TMP = $tmpRoot
$env:TMPDIR = $tmpRoot

# Ensure pytest temp artifacts stay in workspace temp.
$baseOpt = "--basetemp=$pytestBaseTmp"
if ([string]::IsNullOrWhiteSpace($env:PYTEST_ADDOPTS)) {
    $env:PYTEST_ADDOPTS = $baseOpt
}
elseif (-not ($env:PYTEST_ADDOPTS -match "--basetemp=")) {
    $env:PYTEST_ADDOPTS = "$($env:PYTEST_ADDOPTS) $baseOpt"
}

if (-not (Test-Path $venvPython)) {
    throw "Python virtualenv not found. Create/activate a venv at $projectRoot\.venv first."
}

if (-not $Python) {
    $Python = $venvPython
}

switch ($Task) {
    "install" {
        & $Python -m pip install -e .
        break
    }

    "check" {
        & $Python -m menhir.main
        break
    }

    "unit" {
        & $Python -m pytest -m unit
        break
    }

    "test" {
        & $Python -m pytest
        break
    }

    "online" {
        & $Python -m pytest --run-online -m online
        break
    }

    "smoke" {
        & $Python smoke_test.py
        break
    }

    "integration" {
        & $Python integration_test.py
        break
    }

    "clean" {
        Get-ChildItem -Path $projectRoot -Recurse -Include "__pycache__","*.pyc" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "Cleanup complete."
        break
    }
}
