@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo LogScope - open http://127.0.0.1:8765 after startup
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 app.py
) else (
  python app.py
)
pause
