@echo off
REM Makes the bot start automatically every time you log into Windows.
REM Drops a tiny launcher in your Startup folder that runs run_bot.bat with no
REM visible console window. No admin rights needed.
REM Undo any time with uninstall_autostart.bat.

setlocal
set HERE=%~dp0
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set LAUNCHER=%STARTUP%\wolfram-telegram-bot.vbs

> "%LAUNCHER%" echo Set sh = CreateObject("WScript.Shell")
>> "%LAUNCHER%" echo sh.CurrentDirectory = "%HERE:~0,-1%"
>> "%LAUNCHER%" echo sh.Run """%HERE%run_bot.bat""", 0, False

if exist "%LAUNCHER%" (
  echo Installed: %LAUNCHER%
  echo The bot will start on every login.
  echo Starting it now...
  wscript "%LAUNCHER%"
  echo.
  echo Running in the background. Watch it with:  type bot.log
  echo Stop it with:  stop_bot.bat
) else (
  echo FAILED to write %LAUNCHER%
  exit /b 1
)
endlocal
