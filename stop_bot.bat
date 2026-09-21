@echo off
REM Stops the background bot: kills the restart loop first, then python itself.

taskkill /f /im wscript.exe /fi "WINDOWTITLE eq *wolfram*" >nul 2>&1

for /f "tokens=2 delims=," %%p in ('
  wmic process where "name='python.exe' and commandline like '%%main.py%%'" get processid^,name /format:csv 2^>nul ^| findstr /r "[0-9]"
') do echo killing python pid %%p & taskkill /f /pid %%p >nul 2>&1

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run_bot.bat*' -or ($_.Name -eq 'python.exe' -and $_.CommandLine -like '*main.py*') } | ForEach-Object { Write-Host ('stopping ' + $_.Name + ' pid ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"

echo Done.
