@echo off
rem Attiva o disattiva l'avvio di BC Deployer all'accesso a Windows (doppio clic per cambiare).
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\BC Deployer.bat"
if exist "%LNK%" (
    del "%LNK%"
    echo Avvio automatico DISATTIVATO.
) else (
    > "%LNK%" echo @start "BC Deployer" /min "%~dp0Avvia.bat" /nobrowser
    echo Avvio automatico ATTIVATO.
    echo A ogni accesso a Windows BC Deployer parte in una finestra ridotta a icona.
    echo Aprilo dal browser su http://localhost:8000 oppure con Avvia.bat
)
pause
