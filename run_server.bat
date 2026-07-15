@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM qa-auto 중앙 서버 상시 실행 (브라우저 자동 오픈 없음, 죽으면 5초 후 자동 재시작)
REM 최초 세팅은 setup_server.bat 을 먼저 실행하세요.

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] 가상환경이 없습니다. setup_server.bat 을 먼저 실행하세요.
  pause & exit /b 1
)
if not exist ".env" (
  echo [ERROR] .env 가 없습니다. setup_server.bat 을 먼저 실행하세요.
  pause & exit /b 1
)
call ".venv\Scripts\activate.bat"

:loop
echo [%date% %time%] qa-auto 서버 시작 (http://0.0.0.0:8000)
python api_server.py
echo [%date% %time%] 서버 종료됨 — 5초 후 재시작 (중단하려면 Ctrl+C 또는 창 닫기)
timeout /t 5 >nul
goto loop
