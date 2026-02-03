@echo off
echo ==========================================
echo ADB 연결 및 Port Forwarding 설정
echo ==========================================

:: BlueStacks 확인
echo.
echo [1/4] BlueStacks 확인...
tasklist /FI "IMAGENAME eq HD-Player.exe" 2>NUL | find /I /N "HD-Player.exe">NUL
if "%ERRORLEVEL%"=="1" (
    echo [X] BlueStacks를 먼저 실행해주세요.
    pause
    exit /b 1
)
echo [O] BlueStacks 실행 중

:: ADB 연결
echo.
echo [2/4] ADB 연결...
adb connect 127.0.0.1:5555
timeout /t 2 /nobreak >nul

:: 디바이스 확인
echo.
echo [3/4] 연결된 디바이스:
adb devices
echo.

:: Port Forwarding 설정
echo.
echo [4/4] Port Forwarding 설정...
echo.
echo 기존 포트 정리...
adb forward --remove-all

echo.
echo 새 포트 설정...
adb forward tcp:37772 tcp:37772

if %ERRORLEVEL% EQU 0 (
    echo [O] Port Forwarding 설정 완료
) else (
    echo [X] Port Forwarding 설정 실패
    pause
    exit /b 1
)

:: 설정 확인
echo.
echo ==========================================
echo 현재 Port Forwarding 목록:
echo ==========================================
adb forward --list
echo ==========================================
echo.
echo [SUCCESS] ADB 설정 완료!
echo.
echo MCP Server API 접근 가능:
echo http://localhost:37772/api/findAllButtons
echo.
pause