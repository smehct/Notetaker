@echo off
setlocal
cd /d "%~dp0"

set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    echo Python wurde nicht gefunden. Bitte Python 3.12 installieren:
    echo     winget install Python.Python.3.12
    echo Danach dieses Fenster schliessen und install.bat erneut starten.
    pause
    exit /b 1
)

echo Lege virtuelle Umgebung an ...
%PY% -m venv .venv || goto :error
echo Installiere Pakete (das kann einige Minuten dauern) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :error
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error

echo.
choice /c JN /m "Verknuepfung auf dem Desktop anlegen"
if errorlevel 2 goto :done
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Notetaker.lnk'); $s.TargetPath='%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments='app.py'; $s.WorkingDirectory='%~dp0'; $s.Save()"

:done
echo.
echo Fertig. Starten mit start.bat.
pause
exit /b 0

:error
echo.
echo Die Installation ist fehlgeschlagen. Bitte die Meldungen oben pruefen.
pause
exit /b 1
