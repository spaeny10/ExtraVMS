# Installs the "Axiom NVR" scheduled task for the desktop NVR: starts at logon of this user, restarts after a crash
# (every minute, up to 999 times), no time limit, runs on battery. It runs tools/windows/run-nvr.ps1 hidden.
# Restart it with tools/windows/restart-nvr.ps1. Remove with: Unregister-ScheduledTask -TaskName "Axiom NVR"
$task = "Axiom NVR"
$script = Join-Path $PSScriptRoot "run-nvr.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Axiom Vision desktop NVR (TSS-DT) from $((Resolve-Path (Join-Path $PSScriptRoot '..\..')))" -Force | Out-Null
"installed $task"
