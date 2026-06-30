@echo off
REM Cai Task Scheduler — tu chay khi Windows khoi dong
REM Chay file nay bang quyen Administrator

cd /d "%~dp0.."
set APP_DIR=%CD%
set TASK_NAME=ClenderForumBot
set BAT_PATH=%APP_DIR%\deploy\start_windows.bat

echo App folder: %APP_DIR%
echo Task name:  %TASK_NAME%

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python khong tim thay trong PATH
    pause
    exit /b 1
)

schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1

schtasks /Create /TN "%TASK_NAME%" /TR "\"%BAT_PATH%\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 (
    echo Thu tao task voi user hien tai...
    schtasks /Create /TN "%TASK_NAME%" /TR "\"%BAT_PATH%\"" /SC ONSTART /RL HIGHEST /F
)

if errorlevel 1 (
    echo ERROR: Khong tao duoc scheduled task. Chay CMD as Administrator.
    pause
    exit /b 1
)

echo.
echo OK! Task "%TASK_NAME%" da duoc cai.
echo   - Tu chay khi Windows boot
echo   - Tu restart neu process crash
echo.
echo Lenh quan ly:
echo   schtasks /Run /TN "%TASK_NAME%"
echo   schtasks /End  /TN "%TASK_NAME%"
echo   schtasks /Query /TN "%TASK_NAME%" /V /FO LIST
echo.
pause
