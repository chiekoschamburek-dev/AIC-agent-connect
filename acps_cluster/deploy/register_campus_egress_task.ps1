# 注册"仅手动触发"的校园网出口任务：没有开机/登录触发器，不会自启。
# 启动：schtasks /run /tn Yanshu-CampusEgress-Manual
# 停止：powershell -File stop_campus_egress.ps1
$ErrorActionPreference = 'Stop'
$name = 'Yanshu-CampusEgress-Manual'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

$cmd = "`"$dir\run_campus_egress.cmd`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument "-NoProfile -WindowStyle Hidden -Command `"cmd /c `"$cmd`"`""

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit ([TimeSpan]::Zero)   # 默认 3 天会杀掉常驻，这里设为不限时

$existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($existing) {
  Set-ScheduledTask -TaskName $name -Action $action -Settings $settings | Out-Null
  Write-Host "已更新任务 $name"
} else {
  # 不传 -Trigger：任务只能手动 schtasks /run 启动
  Register-ScheduledTask -TaskName $name -Action $action -Settings $settings `
    -Description '研枢校园网出口：白名单代理 + SSH 反向隧道（仅手动触发，不开机自启）' | Out-Null
  Write-Host "已注册任务 $name"
}

$t = Get-ScheduledTask -TaskName $name
Write-Host ("触发器数量 = {0} （0 = 不会开机/登录自启）" -f $t.Triggers.Count)
Write-Host ("执行时限  = {0}" -f $t.Settings.ExecutionTimeLimit)
Write-Host ("电池策略  = 允许电池运行/$($t.Settings.DisallowStartIfOnBatteries)  休眠不停止/$($t.Settings.StopIfGoingOnBatteries)")
Write-Host ("动作      = $($t.Actions[0].Execute) $($t.Actions[0].Arguments)")
