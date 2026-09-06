@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=C:\Users\r_parastar\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PYTHON%" (
  echo Python runtime not found: %PYTHON%
  exit /b 1
)
:restart
echo [%date% %time%] Starting news monitor...
"%PYTHON%" -u run.py >> data\bot.log 2>> data\bot.err.log
echo [%date% %time%] Bot stopped with exit code %errorlevel%. Restarting in 10 seconds...
if not defined FIRST_RESTART (
  set "FIRST_RESTART=1"
  timeout /t 10 /nobreak >nul
) else (
  echo Subsequent retry scheduled in 5 minutes...
  timeout /t 300 /nobreak >nul
)
goto restart
