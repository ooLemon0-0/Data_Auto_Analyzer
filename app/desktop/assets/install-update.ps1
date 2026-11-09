param(
    [Parameter(Mandatory=$true)][string]$SourcePath,
    [Parameter(Mandatory=$true)][string]$TargetPath,
    [Parameter(Mandatory=$true)][int]$AppProcessId,
    [Parameter(Mandatory=$true)][string]$ExpectedHash
)
$ErrorActionPreference = 'Stop'
$sourceFile = [IO.Path]::GetFullPath($SourcePath)
$targetFile = [IO.Path]::GetFullPath($TargetPath)
$logFile = [IO.Path]::Combine([IO.Path]::GetDirectoryName($sourceFile), 'install.log')
$replacementFile = $targetFile + '.' + [Guid]::NewGuid().ToString('N') + '.new'
$backupFile = $targetFile + '.' + [Guid]::NewGuid().ToString('N') + '.previous'
$installed = $false
function Start-ReviewApp {
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $targetFile
    $startInfo.WorkingDirectory = [IO.Path]::GetDirectoryName($targetFile)
    $startInfo.UseShellExecute = $true
    [Diagnostics.Process]::Start($startInfo) | Out-Null
}
try {
    if ($sourceFile -eq $targetFile -or [IO.Path]::GetExtension($targetFile) -ne '.exe') { throw 'Invalid update path' }
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($sourceFile)
    try { $actualHash = [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
    if ($actualHash -ne $ExpectedHash.ToLowerInvariant()) { throw 'Update checksum mismatch' }
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while ($true) {
        try { $appProcess = [Diagnostics.Process]::GetProcessById($AppProcessId) }
        catch [ArgumentException] { break }
        try { $running = -not $appProcess.HasExited } finally { $appProcess.Dispose() }
        if (-not $running) { break }
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Application did not exit' }
        [Threading.Thread]::Sleep(200)
    }
    [IO.File]::Copy($sourceFile, $replacementFile)
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            [IO.File]::Replace($replacementFile, $targetFile, $backupFile)
            $installed = $true
            break
        } catch {
            if ($attempt -eq 59) { throw }
            [Threading.Thread]::Sleep(500)
        }
    }
    Start-ReviewApp
    [IO.File]::WriteAllText($logFile, 'Update installed and restarted', [Text.Encoding]::UTF8)
    if ([IO.File]::Exists($backupFile)) { [IO.File]::Delete($backupFile) }
} catch {
    [IO.File]::WriteAllText($logFile, $_.Exception.Message, [Text.Encoding]::UTF8)
    if (-not $installed -and [IO.File]::Exists($targetFile)) { Start-ReviewApp }
} finally {
    if ([IO.File]::Exists($replacementFile)) { [IO.File]::Delete($replacementFile) }
}
