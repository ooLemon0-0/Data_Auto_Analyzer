param(
    [Parameter(Mandatory=$true)][string]$SourcePath,
    [Parameter(Mandatory=$true)][string]$TargetPath,
    [Parameter(Mandatory=$true)][int]$AppProcessId,
    [Parameter(Mandatory=$true)][string]$ExpectedHash
)
$ErrorActionPreference = 'Stop'
$sourceFile = [IO.Path]::GetFullPath($SourcePath)
$targetFile = [IO.Path]::GetFullPath($TargetPath)
$logFile = Join-Path ([IO.Path]::GetDirectoryName($sourceFile)) 'install.log'
$replacementFile = $targetFile + '.' + [Guid]::NewGuid().ToString('N') + '.new'
$backupFile = $targetFile + '.' + [Guid]::NewGuid().ToString('N') + '.previous'
$installed = $false
function Start-ReviewApp {
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $targetFile
    $startInfo.WorkingDirectory = [IO.Path]::GetDirectoryName($targetFile)
    $startInfo.UseShellExecute = $true
    [Diagnostics.Process]::Start($startInfo) | Out-Null
}
try {
    if ($sourceFile -eq $targetFile -or [IO.Path]::GetExtension($targetFile) -ne '.exe') { throw 'Invalid update path' }
    if ((Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash -ne $ExpectedHash) { throw 'Update checksum mismatch' }
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while (Get-Process -Id $AppProcessId -ErrorAction SilentlyContinue) {
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Application did not exit' }
        Start-Sleep -Milliseconds 200
    }
    [IO.File]::Copy($sourceFile, $replacementFile)
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            [IO.File]::Replace($replacementFile, $targetFile, $backupFile)
            $installed = $true
            break
        } catch {
            if ($attempt -eq 59) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
    Start-ReviewApp
    'Update installed and restarted' | Set-Content -LiteralPath $logFile -Encoding UTF8
    Remove-Item -LiteralPath $backupFile -ErrorAction SilentlyContinue
} catch {
    $_.Exception.Message | Set-Content -LiteralPath $logFile -Encoding UTF8
    if (-not $installed -and (Test-Path -LiteralPath $targetFile)) {
        Start-ReviewApp
    }
} finally {
    Remove-Item -LiteralPath $replacementFile -ErrorAction SilentlyContinue
}
