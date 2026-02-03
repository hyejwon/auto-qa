@echo off
echo ==========================================
echo Auto QA - Gradio 시각화 앱 실행
echo ==========================================

:: Docker 확인
echo.
echo [1/2] Docker Desktop 확인...
docker info >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [X] Docker Desktop을 실행해주세요.
    pause
    exit /b 1
)
echo [O] Docker Desktop 실행 중

:: Gradio 앱 실행
echo.
echo [2/2] Gradio 앱 시작...
echo ==========================================
echo.
echo 브라우저에서 다음 주소로 접속하세요:
echo http://localhost:7860
echo.
echo 종료하려면 Ctrl+C를 누르세요.
echo ==========================================

docker-compose run --rm -p 7860:7860 auto-qa python gradio_app.py

pause