@echo off
REM Chay Bot + Web 24/7 tren Windows VPS
REM Double-click hoac: deploy\start_windows.bat

cd /d "%~dp0.."

echo [%date% %time%] Starting Forum Bot...
echo Folder: %CD%

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python khong tim thay trong PATH
    pause
    exit /b 1
)

if not exist run.py (
    echo ERROR: Khong thay run.py trong %CD%
    pause
    exit /b 1
)

:loop
echo.
echo [%date% %time%] python run.py
python run.py
echo.
echo [%date% %time%] Process dung — tu dong khoi dong lai sau 10 giay...
timeout /t 10 /nobreak
goto loop
