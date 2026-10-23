# Restarts the desktop NVR run by the "Axiom NVR" scheduled task: stops the task, makes sure the server and its
# MediaMTX / ffmpeg children are gone (ending the task doesn't always end its child processes), then starts it again.
# -StopOnly leaves it stopped.
param([switch]$StopOnly)
$task = "Axiom NVR"
Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match "-m nvr(\s|$)" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
for ($i = 0; $i -lt 20; $i++) {
    if (@(Get-Process mediamtx, ffmpeg -ErrorAction SilentlyContinue).Count -eq 0) { break }
    Start-Sleep 1
}
Get-Process mediamtx, ffmpeg -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
if (-not $StopOnly) { Start-ScheduledTask -TaskName $task; "started $task" } else { "stopped $task" }
