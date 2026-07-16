@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM qa-auto 서버 워치독 — 작업 스케줄러로 1분마다 실행 (setup_server.bat이 등록)
REM 동작: /health 무응답 → 행 걸린 api_server 프로세스 종료 (run_server 루프가 5초 후 재시작)
REM       → 그래도 죽어 있으면 (루프 자체가 없음) qa-auto-server 작업 기동
REM 로그: watchdog.log (1MB 초과 시 자동 리셋)

set URL=http://localhost:8000/health
set LOG=%~dp0watchdog.log

REM ── 1차 체크
curl -s -m 5 %URL% | find "ok" >nul
if not errorlevel 1 exit /b 0

REM ── 재시작 직후/일시 부하 오탐 방지: 10초 후 2차 체크
timeout /t 10 /nobreak >nul
curl -s -m 5 %URL% | find "ok" >nul
if not errorlevel 1 exit /b 0

REM ── 로그 로테이션 (1MB 초과 시 리셋)
if exist "%LOG%" for %%A in ("%LOG%") do if %%~zA gtr 1048576 del "%LOG%"

echo [%date% %time%] health check FAILED — api_server 재시작 시도 >> "%LOG%"

REM ── 행 걸린 api_server 프로세스 종료 → run_server.bat 루프가 5초 후 자동 재시작
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*api_server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

REM ── 루프가 재시작할 시간을 주고 3차 체크
timeout /t 20 /nobreak >nul
curl -s -m 5 %URL% | find "ok" >nul
if not errorlevel 1 (
  echo [%date% %time%] 프로세스 종료 후 자동 복구 확인 >> "%LOG%"
  exit /b 0
)

REM ── run_server 루프 자체가 없음 → 스케줄러 작업으로 기동
echo [%date% %time%] 루프 부재 — qa-auto-server 작업 기동 >> "%LOG%"
schtasks /Run /TN "qa-auto-server" >nul 2>&1
if errorlevel 1 echo [%date% %time%] qa-auto-server 작업 기동 실패 — 작업 미등록? setup_server.bat 재실행 필요 >> "%LOG%"
exit /b 0
