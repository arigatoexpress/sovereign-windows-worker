@echo off
rem sovereign windows worker — restart-forever wrapper with log rotation.
cd /d C:\Users\aribs\agent-worker
set SOV_WORKER_MODEL=ollama/qwen3-coder:30b
set SOV_WORKER_WEAK_MODEL=ollama/gemma3:4b
set SOV_WORKER_RELEASE=canonical-worker-v4
set SOV_WORKER_THO_ENABLED=0

rem Rotate worker.log if it exceeds ~100 MB.
for %%F in (worker.log) do (
    if %%~zF GTR 104857600 (
        move /Y worker.log worker.log.1 >nul 2>&1
    )
)

:loop
echo [%date% %time%] worker starting model=%SOV_WORKER_MODEL% >> worker.log
C:\Users\aribs\AppData\Local\Programs\Python\Python313\python.exe worker.py >> worker.log 2>&1
echo [%date% %time%] worker exited rc=%errorlevel% — restarting in 30s >> worker.log
timeout /t 30 /nobreak > nul
goto loop
