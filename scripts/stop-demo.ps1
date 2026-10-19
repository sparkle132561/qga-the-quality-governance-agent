$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
$taskStatePath=Join-Path $taskRoot 'data\runtime\demo-processes.json'
if (-not (Test-Path -LiteralPath $taskStatePath)) { Write-Output '没有本工作区登记的演示进程。'; exit 0 }
$taskState=Get-Content -LiteralPath $taskStatePath -Raw | ConvertFrom-Json
if ($taskState.workspace_root -ne $taskRoot -or $taskState.mode -ne 'fake_only') { throw '进程登记不属于本工作区' }
. (Join-Path $PSScriptRoot 'demo-process.ps1')
$taskValidated=@()
foreach($taskKind in @('backend','frontend')) {
    $taskIdentity=$taskState.$taskKind
    if (-not $taskIdentity) { continue }
    if (Assert-DemoProcessIdentity $taskIdentity $taskRoot $taskKind) { $taskValidated+= $taskIdentity }
}
foreach($taskIdentity in $taskValidated) {
    taskkill.exe /PID $taskIdentity.pid /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '停止已核验的演示进程失败' }
}
Remove-Item -LiteralPath $taskStatePath
Write-Output '已停止本工作区演示服务；数据库容器、卷和上传文件保留。'
