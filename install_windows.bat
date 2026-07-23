@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title FC26 AI Character Studio - Installation automatique

set "PYTHON_EXE="
call :find_python
if defined PYTHON_EXE goto :python_ready

echo.
echo Python 3.11 n'est pas encore installe.
echo Installation automatique en cours...
echo.

where winget >nul 2>nul
if not errorlevel 1 (
  echo [1/2] Installation de Python 3.11 avec winget...
  winget install --id Python.Python.3.11 -e --scope user --accept-package-agreements --accept-source-agreements --silent
  call :find_python
  if defined PYTHON_EXE goto :python_ready
)

echo Winget indisponible ou installation non detectee.
echo [1/2] Telechargement de l'installateur officiel Python 3.11.9...
set "PY_INSTALLER=%TEMP%\python-3.11.9-amd64.exe"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile '%PY_INSTALLER%'"
if errorlevel 1 (
  echo.
  echo Impossible de telecharger Python automatiquement.
  echo Verifie ta connexion Internet puis relance install_windows.bat.
  pause
  exit /b 1
)

 echo Installation de Python pour ce compte Windows...
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1 Include_test=0 Shortcuts=0 SimpleInstall=1
if errorlevel 1 (
  echo.
  echo L'installation automatique de Python a echoue.
  echo Relance ce fichier avec clic droit ^> Executer en tant qu'administrateur.
  pause
  exit /b 1
)

del /q "%PY_INSTALLER%" >nul 2>nul
call :find_python
if not defined PYTHON_EXE (
  echo.
  echo Python a ete installe mais n'est pas encore detecte.
  echo Ferme cette fenetre puis relance install_windows.bat.
  pause
  exit /b 1
)

:python_ready
echo [2/2] Python detecte : "%PYTHON_EXE%"

if exist ".venv\Scripts\python.exe" (
  echo Environnement Studio deja present. Mise a jour...
) else (
  echo Creation de l'environnement du Studio...
  "%PYTHON_EXE%" -m venv .venv
  if errorlevel 1 (
    echo.
    echo La creation de l'environnement Python a echoue.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :pip_error
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :pip_error

echo.
echo ============================================================
echo  INSTALLATION TERMINEE
echo ============================================================
echo.
echo Lance maintenant setup_windows.bat pour choisir ton code prive.
pause
exit /b 0

:pip_error
echo.
echo L'installation des composants du Studio a echoue.
echo Verifie ta connexion Internet puis relance install_windows.bat.
pause
exit /b 1

:find_python
set "PYTHON_EXE="

if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
  set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"
  goto :eof
)

if exist "%ProgramFiles%\Python311\python.exe" (
  set "PYTHON_EXE=%ProgramFiles%\Python311\python.exe"
  goto :eof
)

if defined ProgramFiles(x86) if exist "%ProgramFiles(x86)%\Python311\python.exe" (
  set "PYTHON_EXE=%ProgramFiles(x86)%\Python311\python.exe"
  goto :eof
)

for /f "delims=" %%P in ('py -3.11 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%P"
if defined PYTHON_EXE goto :eof

for /f "delims=" %%P in ('python -c "import sys; assert sys.version_info[:2] == (3, 11); print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%P"
goto :eof
