@echo off
setlocal enabledelayedexpansion

set PORT=8765

echo Cerco processi in ascolto sulla porta %PORT%...
echo.

set FOUND=0

for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    set FOUND=1
    set PID=%%P

    echo Trovato processo con PID !PID! sulla porta %PORT%:
    tasklist /FI "PID eq !PID!"
    echo.

    taskkill /PID !PID! /F
    if !ERRORLEVEL! EQU 0 (
        echo -^> Processo !PID! terminato con successo.
    ) else (
        echo -^> ERRORE nel terminare il processo !PID! ^(prova a lanciare come amministratore^).
    )
    echo.
)

if "%FOUND%"=="0" (
    echo Nessun processo trovato sulla porta %PORT%. Il bridge non e' bloccato.
) else (
    echo Fatto. La porta %PORT% dovrebbe ora essere libera.
)

echo.
pause
