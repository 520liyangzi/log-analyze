param([switch]$SkipInstall)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not $SkipInstall) {
  python -m pip install --disable-pip-version-check -r requirements.txt
  python -m pip install --disable-pip-version-check pyinstaller==6.16.0
}

$webAssets = "$PSScriptRoot\dist;dist"
$promptAssets = "$PSScriptRoot\prompts;prompts"
$skillAssets = "$PSScriptRoot\skills\logscope;skills/logscope"
python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --console `
  --name LogScope `
  --distpath release `
  --workpath build/pyinstaller `
  --specpath build/pyinstaller `
  --add-data $webAssets `
  --add-data $promptAssets `
  --add-data $skillAssets `
  --hidden-import urllib.error `
  --hidden-import urllib.request `
  --collect-all winpty `
  app.py

if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller 构建失败，退出码：$LASTEXITCODE"
}

Write-Host "构建完成：$PSScriptRoot\release\LogScope.exe"
