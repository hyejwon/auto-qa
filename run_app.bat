@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   qa-auto  (PC에서 바로 실행)
echo ============================================

REM 1) Python 확인
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python을 찾을 수 없습니다. Python 3.12 설치 후 PATH에 추가하세요.
  echo         https://www.python.org/downloads/release/python-3129/
  pause & exit /b 1
)

REM 2) 가상환경 (최초 1회 생성)
if not exist ".venv\Scripts\python.exe" (
  echo [*] 가상환경 생성 중... ^(최초 1회, 잠시 걸립니다^)
  python -m venv .venv
)
call ".venv\Scripts\activate.bat"

REM 3) 의존성 설치/확인
echo [*] 의존성 설치/확인 중...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements-app.txt
if errorlevel 1 ( echo [ERROR] 의존성 설치 실패 & pause & exit /b 1 )

REM 4) .env 확인
if not exist ".env" (
  echo [!] .env 가 없어 .env.example 을 복사합니다.
  copy ".env.example" ".env" >nul
  echo [!] 메모장이 열리면 LLM_GATEWAY_TOKEN 과 ADB_DEVICE 를 채우고
  echo     저장 후 이 창에서 다시 run_app.bat 을 실행하세요.
  notepad ".env"
  pause & exit /b 1
)

REM 5) adb 확인 (경고만)
where adb >nul 2>nul
if errorlevel 1 (
  echo [!] 경고: adb 를 PATH 에서 못 찾았습니다. LDPlayer/platform-tools 의 adb 경로를 PATH 에 추가하세요.
)

REM 6) 4초 뒤 브라우저 자동 오픈 + 서버 실행
echo [*] 잠시 후 브라우저가 http://localhost:8000 으로 열립니다.
echo     (창을 닫으면 종료됩니다)
start "" /b cmd /c "timeout /t 4 >nul & start http://localhost:8000"
python api_server.py
pause
