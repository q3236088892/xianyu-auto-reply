$ErrorActionPreference = 'Stop'
$TaskName = 'XianyuAutoReply-Zhinianboke-Portable-Update'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; Write-Host "已删除计划任务: $TaskName" }
else { Write-Host "计划任务不存在: $TaskName" }
