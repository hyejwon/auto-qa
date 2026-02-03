@echo off
echo ==========================================
echo Auto QA 테스트 실행
echo ==========================================

:: Docker 확인
echo.
echo [1/3] Docker Desktop 확인...
docker info >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [X] Docker Desktop을 실행해주세요.
    pause
    exit /b 1
)
echo [O] Docker Desktop 실행 중

:: BlueStacks 확인
echo.
echo [2/3] BlueStacks 확인...
tasklist /FI "IMAGENAME eq HD-Player.exe" 2>NUL | find /I /N "HD-Player.exe">NUL
if "%ERRORLEVEL%"=="1" (
    echo [X] BlueStacks를 먼저 실행해주세요.
    pause
    exit /b 1
)
echo [O] BlueStacks 실행 중

:: APK 파일 확인
echo.
echo [3/3] APK 파일 확인...
if not exist "apks\cooptd.apk" (
    echo [X] apks\cooptd.apk 파일이 없습니다.
    pause
    exit /b 1
)
echo [O] APK 파일 존재

:: QA 테스트 실행
echo.
echo ==========================================
echo QA 테스트 시작
echo (APK 설치는 자동으로 진행됩니다)
echo ==========================================
docker-compose up

echo.
echo ==========================================
echo QA 테스트 완료!
echo ==========================================
pause