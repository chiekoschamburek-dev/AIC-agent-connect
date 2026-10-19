# 手动停掉校园网出口。
# 精确匹配特征串（不用宽泛的 campus/18888），并保护"调用方自己的祖先进程链"，
# 避免把承载本脚本的 shell 一起杀掉（上一版就是因为命令行里含关键词而自杀）。
$ErrorActionPreference = 'SilentlyContinue'
$patterns = @('start_campus_egress\.sh', 'campus_llm_proxy\.py', 'run_campus_egress\.cmd')

# 本进程 + 全部祖先 = 不可杀
$protected = New-Object 'System.Collections.Generic.HashSet[int]'
$cur = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$PID"
while ($cur) {
  [void]$protected.Add([int]$cur.ProcessId)
  if (-not $cur.ParentProcessId) { break }
  $cur = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$($cur.ParentProcessId)"
}

$t = Get-ScheduledTask -TaskName 'Yanshu-CampusEgress-Manual'
if ($t) { $t.Stop() | Out-Null; Write-Host '已请求停止计划任务实例' }

$targets = Get-CimInstance Win32_Process | Where-Object {
  $proc = $_
  $hit = $false
  foreach ($pat in $patterns) { if ($proc.CommandLine -match $pat) { $hit = $true } }
  $proc.CommandLine -and (-not $protected.Contains([int]$proc.ProcessId)) -and $hit
}
foreach ($p in $targets) {
  $brief = $p.CommandLine
  if ($brief.Length -gt 80) { $brief = $brief.Substring(0, 80) }
  Write-Host ("终止 pid=" + $p.ProcessId + " " + $p.Name + " :: " + $brief)
  Stop-Process -Id $p.ProcessId -Force
}
if (-not $targets) { Write-Host '没有需要终止的出口进程' }

Start-Sleep -Seconds 2
$l = Get-NetTCPConnection -LocalPort 18888 -State Listen
if ($l) { $l | ForEach-Object { Write-Host ("警告：18888 仍被 pid=" + $_.OwningProcess + " 占用") } }
else { Write-Host '127.0.0.1:18888 已释放（服务器侧反向监听会在一分钟内消失）' }
