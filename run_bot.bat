@echo off
REM Runs the bot and restarts it if it ever exits (crash, network drop, reboot of the
REM Nebius endpoint, etc). Logs everything to bot.log next to this file.
REM Close the window or run stop_bot.bat to stop it.

cd /d "%~dp0"

set PYTHON=C:\Users\suhai\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PYTHON%" set PYTHON=python

:loop
echo. >> bot.log
echo ================ started %date% %time% ================ >> bot.log
"%PYTHON%" main.py >> bot.log 2>&1
echo ---------------- exited  %date% %time% (restarting in 10s) ---------------- >> bot.log
timeout /t 10 /nobreak > nul
goto loop
