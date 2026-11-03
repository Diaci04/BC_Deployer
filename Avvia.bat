@echo off
rem Avvia BC Deployer e apre il browser. Con /nobrowser non apre il browser (usato dall'avvio automatico).
setlocal
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\bcdeployer-venv\Scripts\python.exe"
set "URL=http://localhost:8000"
if not exist "%PY%" goto :manca
if not exist ".env" goto :manca

rem Gia' in esecuzione? Allora basta aprire il browser
curl -s -o nul -m 2 %URL%/health && goto :apri

title BC Deployer - non chiudere questa finestra
if /i not "%~1"=="/nobrowser" start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 4; Start-Process '%URL%'"
echo BC Deployer in esecuzione su %URL%
echo Chiudi questa finestra per fermarlo.
echo.
"%PY%" -m uvicorn app.asgi:app --host 127.0.0.1 --port 8000
echo.
echo BC Deployer si e' fermato.
pause
exit /b

:apri
if /i not "%~1"=="/nobrowser" start "" %URL%
exit /b 0

:manca
echo Prima esegui Installa.bat
pause
exit /b 1
