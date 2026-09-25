# Start the NVR (backend, MediaMTX and the dedicated Ollama are all launched by the backend).
$ErrorActionPreference = 'Stop'
Set-Location "$PSScriptRoot\backend"
& "$PSScriptRoot\.venv\Scripts\python.exe" -m nvr
