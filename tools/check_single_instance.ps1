param(
    [string]$Exe = "$env:LOCALAPPDATA/Programs/Colab-yoga/Colab-yoga.exe",
    [int]$Launches = 3,
    [int]$LaunchIntervalMs = 200,
    [string]$Shortcut,
    [switch]$CloseWindow,
    [switch]$VerifyTray,
    [switch]$ObserveOnly
)

$ErrorActionPreference = 'Stop'
$exePath = (Resolve-Path -LiteralPath $Exe).Path
$exeName = [IO.Path]::GetFileName($exePath)

function Get-ConsoleProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='$exeName'")
}

$before = @(Get-ConsoleProcesses)
[pscustomobject]@{
    phase = 'before'
    count = $before.Count
    pids = @($before | ForEach-Object { $_.ProcessId })
    paths = @($before | ForEach-Object { $_.ExecutablePath })
} | ConvertTo-Json -Compress

$shortcutPaths = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Colab-yoga.lnk'),
    (Join-Path $env:USERPROFILE 'DeskBox/AI/Colab-yoga.lnk'),
    (Join-Path $env:APPDATA 'Microsoft/Windows/Start Menu/Programs/Colab-yoga.lnk')
)
$shell = New-Object -ComObject WScript.Shell
foreach ($path in $shortcutPaths) {
    if (Test-Path -LiteralPath $path) {
        $link = $shell.CreateShortcut($path)
        [pscustomobject]@{
            phase = 'shortcut'
            path = $path
            target = $link.TargetPath
            arguments = $link.Arguments
            workingDirectory = $link.WorkingDirectory
        } | ConvertTo-Json -Compress
    }
}
if ($ObserveOnly) { exit 0 }

if ($CloseWindow) {
    if ($before.Count -ne 1) { throw 'CloseWindow requires exactly one running console.' }
    $process = Get-Process -Id $before[0].ProcessId
    if (-not $process.CloseMainWindow()) { throw 'Could not close the console window.' }
    Start-Sleep -Milliseconds 500
    $process.Refresh()
    if ($process.HasExited -or $process.MainWindowHandle -ne 0) {
        throw 'Closing the window must leave the process running in the tray.'
    }
}
$entry = if ($Shortcut) { (Resolve-Path -LiteralPath $Shortcut).Path } else { $exePath }
for ($i = 0; $i -lt $Launches; $i++) {
    Start-Process -FilePath $entry -WorkingDirectory ([IO.Path]::GetDirectoryName($exePath))
    if ($LaunchIntervalMs -gt 0) { Start-Sleep -Milliseconds $LaunchIntervalMs }
}
Start-Sleep -Seconds 3
$after = @(Get-ConsoleProcesses)
[pscustomobject]@{
    phase = 'after'
    entry = $entry
    launches = $Launches
    count = $after.Count
    pids = @($after | ForEach-Object { $_.ProcessId })
    paths = @($after | ForEach-Object { $_.ExecutablePath })
} | ConvertTo-Json -Compress
if ($after.Count -ne 1) {
    throw "Expected one Colab-yoga process after repeated launches; found $($after.Count)."
}
if ($CloseWindow) {
    $process = Get-Process -Id $after[0].ProcessId
    if ($process.Id -ne $before[0].ProcessId -or $process.MainWindowHandle -eq 0) {
        throw 'Launching the shortcut must reopen the existing console window.'
    }
}
if ($VerifyTray) {
    $snapshot = & agent-desktop snapshot --surface taskbar -i --compact | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or -not $snapshot.ok) { throw 'Could not read the taskbar.' }
    function Get-TrayIcons($node) {
        if ($node.native_id.value -eq 'NotifyItemIcon' -and $node.name -like '*Colab-yoga*') {
            $node.name
        }
        foreach ($child in $node.children) { Get-TrayIcons $child }
    }
    $icons = @(Get-TrayIcons $snapshot.data.tree)
    [pscustomobject]@{ phase = 'tray'; count = $icons.Count; names = $icons } | ConvertTo-Json -Compress
    if ($icons.Count -ne 1) { throw "Expected one visible tray icon; found $($icons.Count)." }
}
Write-Output 'PASS: repeated launches keep one console process.'
