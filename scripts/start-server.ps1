param(
    [ValidateSet("start", "stop", "restart", "console", "logs", "status", "install-task", "uninstall-task")]
    [string] $Action = "start"
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pidFile = Join-Path $projectRoot ".server.pid"
$watchdogPidFile = Join-Path $projectRoot ".watchdog.pid"
$watchdogTaskName = "menhir-watchdog"
$logDir = Join-Path $projectRoot "logs"
$logFile = Join-Path $logDir "server.log"
$errorLogFile = Join-Path $logDir "server.err.log"
$accessLogFile = Join-Path $logDir "server.access.log"
$launcherLogFile = Join-Path $logDir "launcher.log"
$envFile = Join-Path $projectRoot ".env"
$srcRoot = Join-Path $projectRoot "src"
$apiHost = "127.0.0.1"
$apiPort = 8090
# Neo4j runs on the remote OVM host (parsed from NEO4J_URI in .env below), not locally.
$neo4jHost = ""
$neo4jPort = 7687

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

function Write-LauncherLog {
param(
    [string] $Message
)

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
Add-Content -Path $launcherLogFile -Value "$timestamp $Message" -Encoding utf8
}

function Test-Neo4jReady {
    # Neo4j runs on the remote OVM host (NEO4J_URI in .env), NOT a local Docker
    # container -- so probe the remote bolt port over TCP instead of `docker inspect`.
    # The desktop no longer runs Docker for menhir at all.
    if (-not $neo4jHost) { return $false }
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($neo4jHost, $neo4jPort, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(2000, $false)
        if ($ok -and $client.Connected) { $client.Close(); return $true }
        $client.Close(); return $false
    }
    catch {
        return $false
    }
}

if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*MENHIR_API_HOST=(.+?)\s*$') {
            $apiHost = $matches[1].Trim('"')
        }
        elseif ($line -match '^\s*MENHIR_API_PORT=(.+?)\s*$') {
            $parsedPort = 0
            if ([int]::TryParse($matches[1].Trim('"'), [ref] $parsedPort)) {
                $apiPort = $parsedPort
            }
        }
        elseif ($line -match '^\s*NEO4J_URI=.*://([^:/]+):(\d+)') {
            $neo4jHost = $matches[1]
            $neo4jPort = [int]$matches[2]
        }
    }
}

$apiProbeHost = switch ($apiHost) {
    "0.0.0.0" { "127.0.0.1" }
    "::" { "[::1]" }
    default { $apiHost }
}

function Test-ServerReady {
    param(
        [int] $TimeoutSeconds = 2
    )

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://$apiProbeHost`:$apiPort/api/ready" -TimeoutSec $TimeoutSeconds
        Write-LauncherLog "readiness ok status=$($response.StatusCode) timeout_s=$TimeoutSeconds"
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    }
    catch {
        Write-LauncherLog "readiness failed timeout_s=$TimeoutSeconds error=$($_.Exception.Message)"
        return $false
    }
}

function Wait-ForServerReady {
    param(
        [int] $TimeoutSeconds = 30,
        [int] $ProbeTimeoutSeconds = 5
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Test-ServerReady -TimeoutSeconds $ProbeTimeoutSeconds) {
            Write-LauncherLog "wait ready timeout_s=$TimeoutSeconds probe_timeout_s=$ProbeTimeoutSeconds"
            return $true
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    Write-LauncherLog "wait timed_out timeout_s=$TimeoutSeconds probe_timeout_s=$ProbeTimeoutSeconds"
    return $false
}

function Get-ListeningProcessId {
    try {
        $connection = Get-NetTCPConnection -LocalAddress $apiHost -LocalPort $apiPort -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($null -ne $connection -and $null -ne $connection.OwningProcess) {
            Write-LauncherLog "listener found pid=$($connection.OwningProcess) host=$apiHost port=$apiPort"
            return [int] $connection.OwningProcess
        }
    }
    catch {
        Write-LauncherLog "listener lookup failed error=$($_.Exception.Message)"
    }
    return $null
}

function Get-RunningProcessId {
    if (-not (Test-Path $pidFile)) {
        return Get-ListeningProcessId
    }

    $rawPidLine = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $rawPidLine) {
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
        return $null
    }

    $rawPid = $rawPidLine.Trim()
    if ([string]::IsNullOrWhiteSpace($rawPid)) {
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
        return $null
    }

    $process = Get-Process -Id $rawPid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Write-LauncherLog "pid file stale pid=$rawPid"
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
        return Get-ListeningProcessId
    }

    Write-LauncherLog "pid file process alive pid=$rawPid"
    return [int] $rawPid
}

function Get-WatchdogTask {
    return Get-ScheduledTask -TaskName $watchdogTaskName -ErrorAction SilentlyContinue
}

function Enable-WatchdogTask {
    $task = Get-WatchdogTask
    if ($null -eq $task) {
        Write-LauncherLog "watchdog task missing name=$watchdogTaskName"
        return
    }

    if ($task.State -eq "Disabled") {
        Enable-ScheduledTask -TaskName $watchdogTaskName -ErrorAction Stop | Out-Null
        Write-LauncherLog "watchdog task enabled name=$watchdogTaskName"
        Write-Host "Enabled watchdog task '$watchdogTaskName'."
    }
}

function Disable-WatchdogTask {
    $task = Get-WatchdogTask
    if ($null -eq $task) {
        Write-LauncherLog "watchdog task missing name=$watchdogTaskName"
        return
    }

    if ($task.State -ne "Disabled") {
        # Disable first so the repeating trigger cannot race process shutdown.
        Disable-ScheduledTask -TaskName $watchdogTaskName -ErrorAction Stop | Out-Null
        Write-LauncherLog "watchdog task disabled name=$watchdogTaskName"
        Write-Host "Disabled watchdog task '$watchdogTaskName'."
    }
    Stop-ScheduledTask -TaskName $watchdogTaskName -ErrorAction SilentlyContinue | Out-Null
}

function Stop-Server {
    # Kill the watchdog first so it cannot restart the server after we kill the child.
    if (Test-Path $watchdogPidFile) {
        $rawWatchdogPid = Get-Content $watchdogPidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not [string]::IsNullOrWhiteSpace($rawWatchdogPid)) {
            $wProcess = Get-Process -Id $rawWatchdogPid -ErrorAction SilentlyContinue
            if ($null -ne $wProcess) {
                Write-LauncherLog "stop watchdog pid=$rawWatchdogPid"
                Stop-Process -Id $rawWatchdogPid -Force
            }
        }
        Remove-Item $watchdogPidFile -Force -ErrorAction SilentlyContinue
    }

    $runningPid = Get-RunningProcessId
    if ($null -eq $runningPid) {
        if (Test-ServerReady) {
            Write-LauncherLog "stop skipped reachable_untracked host=$apiHost port=$apiPort"
            Write-Host "menhir server is reachable on $apiHost`:$apiPort but is not tracked by $pidFile."
            return
        }
        Write-LauncherLog "stop skipped not_running"
        Write-Host "menhir server is not running."
        return
    }

    Write-LauncherLog "stop pid=$runningPid"
    Stop-Process -Id $runningPid -Force
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped menhir server (PID $runningPid)"
}

function Wait-ForServerStopped {
    param(
        [int] $TimeoutSeconds = 10
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (-not (Test-ServerReady -TimeoutSeconds 1) -and ($null -eq (Get-ListeningProcessId))) {
            Write-LauncherLog "stopped confirmed host=$apiHost port=$apiPort"
            return $true
        }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)

    Write-LauncherLog "stop wait_timeout host=$apiHost port=$apiPort timeout_s=$TimeoutSeconds"
    return $false
}

if (-not (Test-Path $venvPython)) {
    Write-LauncherLog "venv missing path=$venvPython"
    throw "Python virtualenv not found at $venvPython"
}

switch ($Action) {
    "stop" {
        Disable-WatchdogTask
        Stop-Server
        exit 0
    }

    "restart" {
        Disable-WatchdogTask
        Stop-Server
        if (-not (Wait-ForServerStopped)) {
            Write-Host "Warning: previous server still releasing $apiHost`:$apiPort; starting anyway (watchdog will retry if the port is busy)."
        }
    }

    "console" {
        # Live operator dashboard (rich): ensure the server is up (start it in the
        # background if needed), then attach the dashboard. Quitting the dashboard
        # (q / Ctrl+C) leaves the server running -- it is a monitor, not the
        # server's lifetime. Press 'p' in the dashboard to toggle privacy redaction.
        $thisScript = $MyInvocation.MyCommand.Path
        $startedByUs = $false

        if (($null -eq (Get-RunningProcessId)) -and (-not (Test-ServerReady))) {
            Write-Host "menhir server not running — starting it in the background first..."
            & $thisScript start
            $startedByUs = $true
            Write-Host ""
        }

        $env:PYTHONPATH = $srcRoot
        $env:ENV_FILE = $envFile
        $env:MENHIR_LOG_DIR = $logDir
        $env:PYTHONIOENCODING = "utf-8"

        Write-LauncherLog "console dashboard host=$apiHost port=$apiPort python=$venvPython"
        & $venvPython -m menhir.cli console

        if ($startedByUs) {
            Write-Host ""
            Write-Host "Dashboard closed. menhir server is still running in the background."
            Write-Host "  Status:  .\scripts\start-server.ps1 status"
            Write-Host "  Stop it: .\scripts\start-server.ps1 stop"
        }
        exit 0
    }

    "logs" {
        if (-not (Test-Path $logFile)) {
            Write-Host "No server log yet at $logFile. Start the server first (start / console)."
            exit 1
        }
        Write-Host "Tailing $logFile (Ctrl+C to stop)..."
        Write-Host ""
        Get-Content -Path $logFile -Tail 40 -Wait
        exit 0
    }

    "status" {
        $runningPid = Get-RunningProcessId
        $neo4jState = if (Test-Neo4jReady) { "up" } else { "down" }
        $httpState = if (Test-ServerReady) { "ready" } else { "unreachable" }

        if ($null -ne $runningPid) {
            $serverField = "PID $runningPid"
        }
        elseif ($httpState -eq "ready") {
            $serverField = "untracked"
        }
        else {
            $serverField = "stopped"
        }

        $watchdogField = "none"
        if (Test-Path $watchdogPidFile) {
            $rawWatchdogPid = Get-Content $watchdogPidFile -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not [string]::IsNullOrWhiteSpace($rawWatchdogPid)) {
                $wProcess = Get-Process -Id $rawWatchdogPid -ErrorAction SilentlyContinue
                $watchdogField = if ($null -ne $wProcess) { "PID $rawWatchdogPid" } else { "PID $rawWatchdogPid (stale)" }
            }
        }

        $watchdogTask = Get-WatchdogTask
        $watchdogTaskField = if ($null -eq $watchdogTask) {
            "missing"
        }
        elseif ($watchdogTask.State -eq "Disabled") {
            "disabled"
        }
        else {
            "enabled/$($watchdogTask.State)"
        }

        Write-LauncherLog "status server=$serverField watchdog=$watchdogField watchdog_task=$watchdogTaskField neo4j=$neo4jState http=$httpState"
        Write-Host "menhir  server=$serverField  watchdog=$watchdogField  watchdog_task=$watchdogTaskField  bind=$apiHost`:$apiPort  neo4j=$neo4jState  http=$httpState"
        if ($null -ne $runningPid -or $httpState -eq "ready") {
            Write-Host "  logs: $logFile   (tail live: .\scripts\start-server.ps1 logs)"
        }
        exit 0
    }

    "install-task" {
        $thisScript = $MyInvocation.MyCommand.Path

        # wscript.exe has no console host and WScript.Shell.Run(..., 0) means SW_HIDE,
        # so neither wscript nor the pwsh child ever show a window.
        $vbsPath = Join-Path $scriptDir "run-hidden.vbs"
        $vbsContent = "Set oShell = CreateObject(""WScript.Shell"")" + [Environment]::NewLine +
                      "oShell.Run ""pwsh.exe -NonInteractive -File """"" + $thisScript + """""  -Action start"", 0, False"
        Set-Content -Path $vbsPath -Value $vbsContent -Encoding ASCII
        Write-LauncherLog "install-task vbs=$vbsPath"

        # Trigger 1: fire at every user logon so the server comes up on login
        $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

        # Trigger 2: poll every minute — restarts the server if it was killed between logon cycles
        $repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Minutes 1)

        $taskAction = New-ScheduledTaskAction `
            -Execute "wscript.exe" `
            -Argument "`"$vbsPath`""

        $taskSettings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -MultipleInstances IgnoreNew `
            -StartWhenAvailable

        $taskPrincipal = New-ScheduledTaskPrincipal `
            -UserId $env:USERNAME `
            -LogonType Interactive `
            -RunLevel Limited

        Register-ScheduledTask `
            -TaskName $watchdogTaskName `
            -Action $taskAction `
            -Trigger @($logonTrigger, $repeatTrigger) `
            -Settings $taskSettings `
            -Principal $taskPrincipal `
            -Force | Out-Null

        Write-LauncherLog "install-task name=$watchdogTaskName script=$thisScript"
        Write-Host "Watchdog task '$watchdogTaskName' installed."
        Write-Host "  Triggers: at logon + every minute"
        Write-Host "  Action:   wscript.exe -> run-hidden.vbs -> start-server.ps1 start (no window)"
        exit 0
    }

    "uninstall-task" {
        Unregister-ScheduledTask -TaskName $watchdogTaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-LauncherLog "uninstall-task name=$watchdogTaskName"
        Write-Host "Watchdog task '$watchdogTaskName' removed."
        exit 0
    }
}

Enable-WatchdogTask

$runningPid = Get-RunningProcessId
if ($null -ne $runningPid) {
    $runningPid | Set-Content -Path $pidFile
    Write-LauncherLog "start skipped already_running pid=$runningPid"
    Write-Host "menhir server already running (PID $runningPid, log: $logFile)"
    exit 0
}
if (Test-ServerReady) {
    Write-LauncherLog "start skipped reachable_untracked host=$apiHost port=$apiPort"
    Write-Host "menhir server already reachable on $apiHost`:$apiPort (PID file missing: $pidFile)"
    exit 0
}

# Neo4j is remote (OVM host) and managed there -- the desktop no longer starts Docker
# Desktop or a local Neo4j container. If the remote graph is unreachable, the server's
# own startup preflight reports it clearly.
if (-not (Test-Neo4jReady)) {
    Write-LauncherLog "neo4j remote unreachable host=$neo4jHost port=$neo4jPort"
    Write-Host "Warning: remote Neo4j at $neo4jHost`:$neo4jPort is not reachable; starting anyway (server will report the connectivity error)."
}

Set-Content -Path $launcherLogFile -Value "" -Encoding utf8
Set-Content -Path $errorLogFile -Value "" -Encoding utf8

Write-Host "Starting menhir server..."
Write-LauncherLog "start launching host=$apiHost port=$apiPort python=$venvPython"
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $venvPython
$startInfo.Arguments = "-m menhir.cli serve-watch"
$startInfo.WorkingDirectory = $projectRoot
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.EnvironmentVariables["PYTHONPATH"] = $srcRoot
$startInfo.EnvironmentVariables["ENV_FILE"] = $envFile
$startInfo.EnvironmentVariables["MENHIR_LOG_DIR"] = $logDir

$process = [System.Diagnostics.Process]::Start($startInfo)

if ($null -eq $process) {
    Write-LauncherLog "start failed process_not_created"
    throw "Failed to start menhir server process"
}

$process.Id | Set-Content -Path $pidFile
Write-LauncherLog "start launched pid=$($process.Id)"
Start-Sleep -Milliseconds 500
$launchedProcess = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
if ($null -eq $launchedProcess) {
    Write-LauncherLog "start parent_exited pid=$($process.Id)"
}
else {
    Write-LauncherLog "start parent_alive pid=$($process.Id)"
}
if (Wait-ForServerReady -TimeoutSeconds 30 -ProbeTimeoutSeconds 5) {
    $resolvedPid = Get-ListeningProcessId
    if ($null -ne $resolvedPid) {
        $resolvedPid | Set-Content -Path $pidFile
        Write-LauncherLog "start ready pid=$resolvedPid"
        Write-Host "menhir server ready (PID $resolvedPid, log: $logFile, err: $errorLogFile, access: $accessLogFile)"
    }
    else {
        Write-LauncherLog "start ready pid_unresolved parent_pid=$($process.Id)"
        Write-Host "menhir server ready (PID pending resolution, log: $logFile, err: $errorLogFile, access: $accessLogFile)"
    }
}
else {
    Write-LauncherLog "start not_ready pid=$($process.Id)"
    Write-Host "menhir server launched but did not report ready within timeout (PID $($process.Id), log: $logFile, err: $errorLogFile, access: $accessLogFile)"
}
