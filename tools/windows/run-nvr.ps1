# Runs the desktop NVR (TSS-DT) from this checkout, the way the "Axiom NVR" scheduled task starts it
# (tools/windows/install-nvr-task.ps1). Before this the server ran inside a Claude session and stopped with it.
# A server that exits is started again after 10 s (Task Scheduler's own retry doesn't fire on an exit code); ending
# the task (tools/windows/restart-nvr.ps1) ends this loop. Output goes to E:\NVR\logs\nvr-<date>.log, one file per
# day; logs older than 14 days are removed.
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$logs = "E:\NVR\logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$env:OLLAMA_MODELS = "E:\Ollama\models"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location (Join-Path $repo "backend")
$python = Join-Path $repo ".venv\Scripts\python.exe"

while ($true) {
    Get-ChildItem $logs -Filter "nvr-*.log" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    $log = Join-Path $logs ("nvr-{0:yyyy-MM-dd}.log" -f (Get-Date))
    "==== start $(Get-Date -Format s) ====" | Out-File -Append -Encoding utf8 $log
    # through cmd: Windows PowerShell's own >> writes UTF-16, which grep and editors read as binary
    cmd /c "`"$python`" -m nvr >> `"$log`" 2>&1"
    "==== exit $LASTEXITCODE $(Get-Date -Format s); starting again in 10 s ====" | Out-File -Append -Encoding utf8 $log
    Start-Sleep 10
}
