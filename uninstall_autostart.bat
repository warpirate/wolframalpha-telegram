@echo off
REM Removes the login-autostart launcher. Does not stop a bot that is already
REM running - use stop_bot.bat for that.

set LAUNCHER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\wolfram-telegram-bot.vbs

if exist "%LAUNCHER%" (
  del "%LAUNCHER%"
  echo Removed autostart launcher.
) else (
  echo Autostart was not installed.
)
