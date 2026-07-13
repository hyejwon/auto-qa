@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   qa-auto 중앙 서버 세팅 (Windows, 최초 1회)
echo ============================================
echo.

REM ── 관리자 권한 확인 (방화벽/절전/자동시작 등록에 필요)
net session >nul 2>&1
if errorlevel 1 (
  echo [!] 관리자 권한이 아닙니다. 방화벽/절전/자동시작 설정은 건너뜁니다.
  echo     전체 세팅을 하려면 이 파일을 우클릭 - "관리자 권한으로 실행" 하세요.
  set IS_ADMIN=0
) else (
  set IS_ADMIN=1
)

REM ── 1) Python 확인
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python을 찾을 수 없습니다. Python 3.12 설치 후 PATH에 추가하세요.
  echo         https://www.python.org/downloads/release/python-3129/
  pause & exit /b 1
)

REM ── 2) adb 확인 (서버는 폰을 무선으로 제어하므로 필수)
where adb >nul 2>nul
if errorlevel 1 (
  echo [ERROR] adb 를 PATH 에서 못 찾았습니다.
  echo         platform-tools 를 받아 adb.exe 경로를 PATH 에 추가하세요:
  echo         https://developer.android.com/tools/releases/platform-tools
  pause & exit /b 1
)

REM ── 3) 가상환경 + 의존성
if not exist ".venv\Scripts\python.exe" (
  echo [*] 가상환경 생성 중... ^(최초 1회, 잠시 걸립니다^)
  python -m venv .venv
)
call ".venv\Scripts\activate.bat"
echo [*] 의존성 설치/확인 중...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements-app.txt
if errorlevel 1 ( echo [ERROR] 의존성 설치 실패 & pause & exit /b 1 )

REM ── 4) .env 확인
if not exist ".env" (
  echo [!] .env 가 없어 .env.example 을 복사합니다.
  copy ".env.example" ".env" >nul
  echo [!] 메모장이 열리면 LLM_GATEWAY_TOKEN 을 채우세요.
  echo     ^(중앙 서버는 ADB_DEVICE 를 비워둡니다 — 웹 UI에서 디바이스를 선택합니다^)
  notepad ".env"
)

if "%IS_ADMIN%"=="0" goto :skip_admin

REM ── 5) 방화벽: 8000 포트 인바운드 허용 (테스터 브라우저 접속용)
netsh advfirewall firewall show rule name="qa-auto-server-8000" >nul 2>&1
if errorlevel 1 (
  netsh advfirewall firewall add rule name="qa-auto-server-8000" dir=in action=allow protocol=TCP localport=8000 >nul
  echo [*] 방화벽 인바운드 8000 허용 등록 완료
) else (
  echo [*] 방화벽 규칙 이미 존재
)

REM ── 6) 절전 해제 (서버가 잠들면 팀 전체 QA 중단)
powercfg /change standby-timeout-ac 0 >nul
powercfg /change hibernate-timeout-ac 0 >nul
echo [*] 절전/최대 절전 해제 완료 ^(AC 전원 기준^)

REM ── 7) 로그온 시 자동 시작 등록 (선택)
set /p AUTOSTART="[?] PC 로그온 시 서버 자동 시작을 등록할까요? (y/N): "
if /i "%AUTOSTART%"=="y" (
  schtasks /Create /TN "qa-auto-server" /TR "\"%~dp0run_server.bat\"" /SC ONLOGON /RL HIGHEST /F >nul
  echo [*] 자동 시작 등록 완료 ^(작업 스케줄러: qa-auto-server^)
)

:skip_admin

REM ── 8) 서버 IP 안내 + 실행
echo.
echo ============================================
echo   세팅 완료. 이 PC의 IP:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do for %%b in (%%a) do echo   http://%%b:8000
echo   테스터에게 위 주소를 공유하세요.
echo ============================================
echo.
set /p RUNNOW="[?] 지금 서버를 시작할까요? (Y/n): "
if /i not "%RUNNOW%"=="n" call run_server.bat
pause
