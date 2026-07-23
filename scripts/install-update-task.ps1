param([switch]$AsSystem)
$ErrorActionPreference = 'Stop'
$TaskName = 'XianyuAutoReply-Zhinianboke-Portable-Update'
$Root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Update = Join-Path $Root 'UPDATE.cmd'
if (-not (Test-Path -LiteralPath $Update)) { throw "UPDATE.cmd 不存在: $Update" }
$Action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" -Argument "/d /c `"`"$Update`" --scheduled`"" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At '03:30'
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)
if ($AsSystem) {
  $Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
} else {
  $User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
  $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
}
$Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description '每日 03:30 检查、构建、验证并切换 zhinianboke/xianyu-auto-reply 绿色版源码；失败自动回滚。'
Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State,@{n='WorkingDirectory';e={$_.Actions.WorkingDirectory}},@{n='Arguments';e={$_.Actions.Arguments}}
