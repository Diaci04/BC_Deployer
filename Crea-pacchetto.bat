@echo off
rem Crea sul Desktop bc-deployer.zip da passare a un collega: senza password, chiave, database e file temporanei.
setlocal
cd /d "%~dp0"
set "TMPDIR=%TEMP%\bc-deployer"
if exist "%TMPDIR%" rd /s /q "%TMPDIR%"
robocopy "%~dp0." "%TMPDIR%" /E /XD .venv __pycache__ .pytest_cache .git data tests /XF .env "pc aziendale.txt" *.pyc requirements-dev.txt >nul
if errorlevel 8 goto :errore
powershell -NoProfile -Command "$z = Join-Path ([Environment]::GetFolderPath('Desktop')) 'bc-deployer.zip'; Compress-Archive -Path '%TMPDIR%' -DestinationPath $z -Force; Write-Host ('Creato ' + $z)" || goto :errore
rd /s /q "%TMPDIR%"
pause
exit /b 0

:errore
echo Pacchetto non creato: leggi il messaggio qui sopra.
pause
exit /b 1
