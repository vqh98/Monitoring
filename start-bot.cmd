@echo off
setlocal
cd /d "%~dp0"
rem Resolve Python on the current machine instead of hard-coding the
rem account-specific Codex runtime path from the development machine.
set "PYTHON="
where python >nul 2>&1 && set "PYTHON=python"
if not defined PYTHON (
  where py >nul 2>&1 && set "PYTHON=py -3"
)
if not defined PYTHON (
  echo Python runtime not found on PATH.
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
