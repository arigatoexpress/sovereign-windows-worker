@echo off
rem sovereign windows worker — restart-forever wrapper (mirrors forecast-server pattern)
cd /d C:\Users\aribs\agent-worker
:loop
echo [%date% %time%] worker starting >> worker.log
C:\Users\aribs\AppData\Local\Programs\Python\Python313\python.exe worker.py >> worker.log 2>&1
echo [%date% %time%] worker exited rc=%errorlevel% — restarting in 30s >> worker.log
timeout /t 30 /nobreak > nul
goto loop
