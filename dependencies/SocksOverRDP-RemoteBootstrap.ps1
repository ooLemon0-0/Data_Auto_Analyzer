[CmdletBinding()]
param(
    [string]$SettingsPath = (Join-Path $PSScriptRoot "SocksOverRDP-RemoteSettings.json"),
    [switch]$EnsureOnly
)

$ErrorActionPreference = "Stop"

function Write-BootstrapLog {
    param([string]$Message)

    $logDirectory = Join-Path $env:LOCALAPPDATA "Cisdi_Data_Auto_Analyzer\logs"
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $logPath = Join-Path $logDirectory "socks_over_rdp_remote.log"
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Value "$timestamp $Message" -Encoding UTF8
}

try {
    if (-not (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) {
        throw "Remote settings file is missing: $SettingsPath"
    }

    $settings = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
    $clientSourceDirectory = [string]$settings.client_source_directory
    $installDirectory = [string]$settings.remote_install_directory
    $serverExecutable = [string]$settings.server_executable
    $waitSeconds = [int]$settings.wait_seconds
    if (-not $clientSourceDirectory -or -not $installDirectory -or -not $serverExecutable) {
        throw "Remote settings are incomplete."
    }
    if ($waitSeconds -lt 1) {
        $waitSeconds = 90
    }

    $sourceServer = Join-Path $clientSourceDirectory $serverExecutable
    $targetServer = Join-Path $installDirectory $serverExecutable
    # The redirected client drive is needed only for first install or updates.
    # Once the server is installed locally, never delay service startup waiting
    # for \\tsclient after an RDP reconnect.
    if (-not (Test-Path -LiteralPath $targetServer -PathType Leaf)) {
        $deadline = (Get-Date).AddSeconds($waitSeconds)
        while (-not (Test-Path -LiteralPath $sourceServer -PathType Leaf)) {
            if ((Get-Date) -ge $deadline) {
                break
            }
            Start-Sleep -Seconds 2
        }
    }

    New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
    $currentSessionId = (Get-Process -Id $PID).SessionId
    $serverProcessName = [System.IO.Path]::GetFileNameWithoutExtension($serverExecutable)
    $sessionProcesses = @(
        Get-Process -Name $serverProcessName -ErrorAction SilentlyContinue |
            Where-Object { $_.SessionId -eq $currentSessionId }
    )

    if (Test-Path -LiteralPath $sourceServer -PathType Leaf) {
        $sourceHash = (Get-FileHash -LiteralPath $sourceServer -Algorithm SHA256).Hash
        $targetHash = $null
        if (Test-Path -LiteralPath $targetServer -PathType Leaf) {
            $targetHash = (Get-FileHash -LiteralPath $targetServer -Algorithm SHA256).Hash
        }
        if ($sourceHash -ne $targetHash) {
            $sessionProcesses | Stop-Process -Force -ErrorAction SilentlyContinue
            $sessionProcesses = @()
            Copy-Item -LiteralPath $sourceServer -Destination $targetServer -Force
            $copiedHash = (Get-FileHash -LiteralPath $targetServer -Algorithm SHA256).Hash
            if ($copiedHash -ne $sourceHash) {
                throw "Server executable hash verification failed after copy."
            }
            Write-BootstrapLog "Updated $targetServer from the redirected client drive."
        }
    }
    elseif (-not (Test-Path -LiteralPath $targetServer -PathType Leaf)) {
        throw "The redirected source and installed server executable are both unavailable."
    }
    else {
        Write-BootstrapLog "Redirected source was unavailable; using the installed server executable."
    }

    # A logon/reconnect task must restart the server for the new virtual channel.
    # The watchdog uses -EnsureOnly and leaves a healthy process untouched.
    $sessionProcesses = @(
        Get-Process -Name $serverProcessName -ErrorAction SilentlyContinue |
            Where-Object { $_.SessionId -eq $currentSessionId }
    )
    if ($EnsureOnly -and $sessionProcesses.Count -gt 0) {
        Write-BootstrapLog "Watchdog confirmed $targetServer is running in RDP session $currentSessionId (PID $($sessionProcesses[0].Id))."
        return
    }
    $sessionProcesses | Stop-Process -Force -ErrorAction SilentlyContinue
    if ($sessionProcesses.Count -gt 0) {
        Start-Sleep -Milliseconds 500
    }

    Start-Process -FilePath $targetServer -WorkingDirectory $installDirectory -WindowStyle Hidden
    Start-Sleep -Seconds 2
    $started = Get-Process -Name $serverProcessName -ErrorAction SilentlyContinue |
        Where-Object { $_.SessionId -eq $currentSessionId } |
        Select-Object -First 1
    if (-not $started) {
        throw "SocksOverRDP server did not remain running in RDP session $currentSessionId."
    }
    $mode = if ($EnsureOnly) { "watchdog recovery" } else { "logon/reconnect" }
    Write-BootstrapLog "Started $targetServer in RDP session $currentSessionId (PID $($started.Id), mode=$mode)."
}
catch {
    Write-BootstrapLog "FAILED: $($_.Exception.Message)"
    throw
}
