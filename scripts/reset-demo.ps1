param([switch]$ConfirmDemoDataReset,[ValidateRange(1,65535)][int]$PostgresPort=55432)
$ErrorActionPreference='Stop'
if (-not $ConfirmDemoDataReset) { throw '此操作清空专用quality_demo的案件/审批/记忆/工单/checkpoint；需传-ConfirmDemoDataReset。quality/quality_test/quality_ui_test及上传文件保留。' }
$taskRoot=Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot 'stop-demo.ps1')
& (Join-Path $taskRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'prepare-demo.py') --reset --confirm-demo-reset --postgres-port $PostgresPort
if ($LASTEXITCODE -ne 0) { throw '专用库重置失败，未自动重启服务' }
Write-Output '专用演示库已清空；再次运行start-demo.ps1会初始化演示目录。'
