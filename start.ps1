$ErrorActionPreference = "Stop"
$runtime = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $runtime)) {
    Write-Host "请先创建 Python 3.11 虚拟环境并安装 requirements.txt，参阅使用说明.md。"
    exit 1
}
Write-Host "ChatDB: http://127.0.0.1:8000"
& $runtime (Join-Path $PSScriptRoot "run.py")
