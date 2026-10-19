param([switch]$SkipOpen,[ValidateRange(1,65535)][int]$PostgresPort=55432)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw '请先创建.venv并安装requirements.lock.txt，详见README。' }
$taskNode = (Get-Command node -ErrorAction Stop).Source
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'frontend\node_modules\vite\bin\vite.js'))) { throw '请先在frontend执行npm ci。' }
docker info --format '{{.ServerVersion}}' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Docker未就绪，请启动Docker Desktop后重试。' }
$taskOldPgPort=[Environment]::GetEnvironmentVariable('QUALITY_POSTGRES_PORT','Process')
try {
    $env:QUALITY_POSTGRES_PORT=[string]$PostgresPort
    docker compose up -d postgres
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL容器启动失败' }
    for ($taskAttempt=0; $taskAttempt -lt 30; $taskAttempt++) {
        docker compose exec -T postgres pg_isready -U quality -d quality | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 1
    }
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL未就绪' }
} finally {
    [Environment]::SetEnvironmentVariable('QUALITY_POSTGRES_PORT',$taskOldPgPort,'Process')
}
foreach ($taskPort in @(8050,5175)) { if (Get-NetTCPConnection -LocalPort $taskPort -State Listen -ErrorAction SilentlyContinue) { throw "端口$taskPort已使用，请先运行stop-demo.ps1；不会结束未知服务。" } }
& $taskPython (Join-Path $taskRoot 'scripts\prepare-demo.py') --postgres-port $PostgresPort
if ($LASTEXITCODE -ne 0) { throw '专用演示库准备失败' }
$taskRuntime = Join-Path $taskRoot 'data\runtime'
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
$taskFixture = Join-Path $taskRoot 'scripts\run-protocol-fixture.py'
$taskVite = Join-Path $taskRoot 'frontend\node_modules\vite\bin\vite.js'
$taskQuotedFixture=([char]34)+$taskFixture+([char]34)
$taskQuotedVite=([char]34)+$taskVite+([char]34)
. (Join-Path $PSScriptRoot 'demo-process.ps1')
$taskStatePath=Join-Path $taskRuntime 'demo-processes.json'
$taskState=@{workspace_root=$taskRoot;mode='fake_only';backend=$null;frontend=$null}
function Save-DemoState { $taskState | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $taskStatePath -Encoding utf8 }
function Wait-DemoPort($Port,$Url,$Kind) {
    for($taskAttempt=0;$taskAttempt -lt 30;$taskAttempt++) {
        try { Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 2 | Out-Null; $taskListener=Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop; return (Get-DemoProcessIdentity $taskListener.OwningProcess) }
        catch { Start-Sleep -Seconds 1 }
    }
    throw ('演示'+$Kind+'未就绪，检查data/runtime日志')
}
$taskOldApi=[Environment]::GetEnvironmentVariable('QUALITY_API_TARGET','Process')
$taskOldFlag=[Environment]::GetEnvironmentVariable('VITE_PROTOCOL_DEMO','Process')
try {
    $taskBackend=Start-Process -FilePath $taskPython -ArgumentList @($taskQuotedFixture,'--database','quality_demo','--port','8050','--postgres-port',[string]$PostgresPort) -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRuntime 'demo-backend.out.log') -RedirectStandardError (Join-Path $taskRuntime 'demo-backend.err.log')
    $taskState.backend=Get-DemoProcessIdentity $taskBackend.Id
    Save-DemoState
    $taskState.backend=Wait-DemoPort 8050 'http://127.0.0.1:8050/openapi.json' 'backend'
    if (-not (Assert-DemoProcessIdentity $taskState.backend $taskRoot 'backend')) { throw '演示后端身份无法核验' }
    Save-DemoState
    $env:QUALITY_API_TARGET='http://127.0.0.1:8050'
    $env:VITE_PROTOCOL_DEMO='1'
    $taskFrontend=Start-Process -FilePath $taskNode -ArgumentList @($taskQuotedVite,'--host','127.0.0.1','--port','5175','--strictPort','--configLoader','native') -WorkingDirectory (Join-Path $taskRoot 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRuntime 'demo-frontend.out.log') -RedirectStandardError (Join-Path $taskRuntime 'demo-frontend.err.log')
    $taskState.frontend=Get-DemoProcessIdentity $taskFrontend.Id
    Save-DemoState
    $taskState.frontend=Wait-DemoPort 5175 'http://127.0.0.1:5175' 'frontend'
    if (-not (Assert-DemoProcessIdentity $taskState.frontend $taskRoot 'frontend')) { throw '演示前端身份无法核验' }
    Save-DemoState
    $taskLogin=Invoke-RestMethod 'http://127.0.0.1:8050/api/auth/login' -Method Post -ContentType 'application/json' -Body '{"username":"admin","password":"demo-only"}'
    Invoke-RestMethod 'http://127.0.0.1:8050/api/admin/import-demo' -Method Post -Headers @{Authorization="Bearer $($taskLogin.access_token)"} | Out-Null
} catch {
    $taskFailure=$_
    if (Test-Path -LiteralPath $taskStatePath) { & (Join-Path $PSScriptRoot 'stop-demo.ps1') }
    throw $taskFailure
} finally {
    [Environment]::SetEnvironmentVariable('QUALITY_API_TARGET',$taskOldApi,'Process')
    [Environment]::SetEnvironmentVariable('VITE_PROTOCOL_DEMO',$taskOldFlag,'Process')
}
Write-Output '协议演示已启动：http://127.0.0.1:5175。所有模型Fake、外部模型请求0；停止用scripts/stop-demo.ps1。'
if (-not $SkipOpen) { Start-Process 'http://127.0.0.1:5175' -WindowStyle Hidden }
