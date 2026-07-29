@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "LOG_DIR=%~dp0logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG=%LOG_DIR%\adb_start.log"

echo.>> "%LOG%"
echo [%date% %time%] adb startup begin>> "%LOG%"

where adb >nul 2>nul
if errorlevel 1 (
  echo [%date% %time%] ERROR: adb not found in PATH>> "%LOG%"
  exit /b 1
)

adb kill-server >> "%LOG%" 2>&1
timeout /t 2 /nobreak >nul
adb start-server >> "%LOG%" 2>&1
timeout /t 2 /nobreak >nul
adb devices >> "%LOG%" 2>&1

echo [%date% %time%] adb startup end>> "%LOG%"
exit /b 0
