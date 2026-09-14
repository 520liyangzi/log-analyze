param([switch]$SkipInstall)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not $SkipInstall) {
  python -m pip install --disable-pip-version-check -r requirements.txt
  python -m pip install --disable-pip-version-check pyinstaller==6.16.0
}
python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --console `
  --name LogScope `
  --distpath release `
  --workpath build/pyinstaller `
  --specpath build/pyinstaller `
  --add-data "dist;dist" `
  --add-data "prompts;prompts" `
  --add-data "skills/logscope;skills/logscope" `
  --hidden-import urllib.error `
  --hidden-import urllib.request `
  --collect-all winpty `
  app.py

Write-Host "构建完成：$PSScriptRoot\release\LogScope.exe"
