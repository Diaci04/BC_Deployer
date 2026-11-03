@echo off
rem Prima installazione (e riparazione): ambiente Python, librerie, password e chiave in .env.
rem Si puo' rilanciare quando vuoi, per esempio per cambiare la password: non perde nulla.
setlocal
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\bcdeployer-venv"
set "PY=%VENV%\Scripts\python.exe"

where py >nul 2>&1 || (echo Python non trovato: serve il comando "py", versione 3.12 o piu' recente. & goto :errore)

rem Crea l'ambiente se manca o se non funziona piu' (per esempio dopo un aggiornamento di Python)
set "OK="
if exist "%PY%" "%PY%" -c "pass" >nul 2>&1 && set "OK=1"
if not defined OK (
    if exist "%VENV%" rd /s /q "%VENV%"
    echo Creo l'ambiente Python in %VENV%
    py -3 -m venv "%VENV%" || goto :errore
)
"%PY%" -c "import sys; sys.exit(sys.version_info < (3, 12))" || (echo Serve Python 3.12 o piu' recente: py -0 mostra le versioni installate. & goto :errore)

echo Installo le librerie, puo' richiedere qualche minuto...
"%PY%" -m pip install -q --disable-pip-version-check -r requirements.txt || goto :errore

rem Se Windows blocca il componente compilato di SQLAlchemy, si usa la versione solo Python
"%PY%" -c "import sqlalchemy.engine" >nul 2>&1 || (
    echo SQLAlchemy bloccato da Windows: installo la versione senza componenti compilati...
    set DISABLE_SQLALCHEMY_CEXT=1
    "%PY%" -m pip install -q --disable-pip-version-check --force-reinstall --no-deps --no-binary sqlalchemy sqlalchemy || goto :errore
)
"%PY%" -c "import sqlalchemy.engine, cryptography.fernet, argon2, pydantic" || (echo Windows blocca alcune librerie: su questo PC l'app non puo' girare in locale. & goto :errore)

echo.
"%PY%" -m app.cli setup || goto :errore
echo.
echo Fatto. Per avviare l'app: doppio clic su Avvia.bat
pause
exit /b 0

:errore
echo.
echo Installazione non completata: leggi il messaggio qui sopra.
pause
exit /b 1
