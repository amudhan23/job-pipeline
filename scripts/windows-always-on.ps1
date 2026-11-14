# Keep jobpipe running on this Windows laptop (WSL):
#   - starts `jobpipe serve` (crawler + dashboard + auto-apply) when you log in,
#     and restarts it if it stops
#   - stops the laptop sleeping while it's plugged in (including with the lid closed)
#
# Run in PowerShell (no admin needed):
#   powershell -ExecutionPolicy Bypass -File scripts\windows-always-on.ps1
# Undo:
#   Unregister-ScheduledTask -TaskName jobpipe -Confirm:$false
#   powercfg /change standby-timeout-ac 30
param(
  [string]$Distro = "Ubuntu",
  [string]$RepoPath = "~/jobs/job-pipeline"
)

$cmd = "cd $RepoPath && source .venv/bin/activate && exec jobpipe serve"
$action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $Distro -- bash -lc `"$cmd`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "jobpipe" -Action $action -Trigger $trigger -Settings $settings `
  -Description "jobpipe: job crawler, dashboard and auto-apply" -Force | Out-Null

# Plugged in: never sleep or hibernate, and closing the lid does nothing
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT

Start-ScheduledTask -TaskName "jobpipe"
Write-Host "jobpipe is running and starts at every login. Dashboard: http://localhost:8050"
Write-Host "Keep the laptop plugged in; on battery Windows may still sleep."
