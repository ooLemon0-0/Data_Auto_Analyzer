[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this installer from an elevated PowerShell or Command Prompt."
}

$sourceSettingsPath = Join-Path $PSScriptRoot "SocksOverRDP-RemoteSettings.json"
if (-not (Test-Path -LiteralPath $sourceSettingsPath -PathType Leaf)) {
    throw "Remote settings file is missing: $sourceSettingsPath"
}
$settings = Get-Content -LiteralPath $sourceSettingsPath -Raw | ConvertFrom-Json
$installDirectory = [string]$settings.remote_install_directory
$bootstrapScript = [string]$settings.bootstrap_script
$taskName = [string]$settings.task_name
if (-not $installDirectory -or -not $bootstrapScript -or -not $taskName) {
    throw "Remote settings are incomplete."
}

$sourceBootstrapPath = Join-Path $PSScriptRoot $bootstrapScript
$targetBootstrapPath = Join-Path $installDirectory $bootstrapScript
$targetSettingsPath = Join-Path $installDirectory "SocksOverRDP-RemoteSettings.json"
if (-not (Test-Path -LiteralPath $sourceBootstrapPath -PathType Leaf)) {
    throw "Remote bootstrap script is missing: $sourceBootstrapPath"
}

New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
Copy-Item -LiteralPath $sourceBootstrapPath -Destination $targetBootstrapPath -Force
Copy-Item -LiteralPath $sourceSettingsPath -Destination $targetSettingsPath -Force

$powerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$taskArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $targetBootstrapPath
$watchdogTaskName = "$taskName Watchdog"
$watchdogArguments = "$taskArguments -EnsureOnly"
$watchdogStartBoundary = (Get-Date).AddMinutes(1).ToString("yyyy-MM-ddTHH:mm:ss")
$eventChannel = "Microsoft-Windows-TerminalServices-LocalSessionManager/Operational"
$eventQuery = '<QueryList><Query Id="0" Path="{0}"><Select Path="{0}">*[System[(EventID=25)]]</Select></Query></QueryList>' -f $eventChannel
$escape = [System.Security.SecurityElement]
$escapedPowerShellPath = $escape::Escape($powerShellPath)
$escapedTaskArguments = $escape::Escape($taskArguments)
$escapedWatchdogArguments = $escape::Escape($watchdogArguments)
$escapedWatchdogStartBoundary = $escape::Escape($watchdogStartBoundary)
$escapedUserSid = $escape::Escape($identity.User.Value)
$escapedEventQuery = $escape::Escape($eventQuery)
$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Synchronize and start SocksOverRDP in the interactive RDP session.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT10S</Delay>
      <UserId>$escapedUserSid</UserId>
    </LogonTrigger>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>$escapedEventQuery</Subscription>
      <Delay>PT5S</Delay>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$escapedUserSid</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>StopExisting</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT3M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$escapedPowerShellPath</Command>
      <Arguments>$escapedTaskArguments</Arguments>
    </Exec>
  </Actions>
</Task>
"@

$watchdogTaskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Keep SocksOverRDP running in the interactive RDP session.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>$escapedWatchdogStartBoundary</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$escapedUserSid</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT2M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$escapedPowerShellPath</Command>
      <Arguments>$escapedWatchdogArguments</Arguments>
    </Exec>
  </Actions>
</Task>
"@

try {
    Register-ScheduledTask -TaskName $taskName -Xml $taskXml -Force | Out-Null
    Register-ScheduledTask -TaskName $watchdogTaskName -Xml $watchdogTaskXml -Force | Out-Null
}
catch {
    throw "Failed to register the persistent SocksOverRDP tasks: $($_.Exception.Message)"
}

$legacyReconnectTask = "$taskName Reconnect"
if (Get-ScheduledTask -TaskName $legacyReconnectTask -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $legacyReconnectTask -Confirm:$false
}

$registeredTask = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
$registeredWatchdogTask = Get-ScheduledTask -TaskName $watchdogTaskName -ErrorAction Stop
$triggerTypes = @(
    $registeredTask.Triggers | ForEach-Object { $_.CimClass.CimClassName }
)
if (
    "MSFT_TaskLogonTrigger" -notin $triggerTypes -or
    "MSFT_TaskEventTrigger" -notin $triggerTypes
) {
    throw "The scheduled task was registered without both required triggers."
}
if (-not $registeredWatchdogTask.Settings.Enabled) {
    throw "The SocksOverRDP watchdog task was registered but is disabled."
}

try {
    & $targetBootstrapPath -SettingsPath $targetSettingsPath
}
catch {
    throw "The initial SocksOverRDP server start failed: $($_.Exception.Message)"
}

Write-Host "SocksOverRDP remote bootstrap is installed and running."
Write-Host "Task: $taskName (logon + RDP reconnect triggers)"
Write-Host "Watchdog: $watchdogTaskName (checks every minute)"
Write-Host "Log: $env:LOCALAPPDATA\Cisdi_Data_Auto_Analyzer\logs\socks_over_rdp_remote.log"
