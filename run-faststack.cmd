@echo off
setlocal

set "REPOROOT=%~dp0"
set "PYTHONEXE=%REPOROOT%.venv\Scripts\python.exe"

if not exist "%PYTHONEXE%" (
    echo Could not find venv Python at: %PYTHONEXE%
    exit /b 1
)

cd /d "%REPOROOT%"
"%PYTHONEXE%" -m faststack.app %*
exit /b %ERRORLEVEL%
