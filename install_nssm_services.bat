@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0"
set "LOG_DIR=%ROOT%logs"
set "BACKEND_SERVICE=qa-auto-backend"
set "FRONTEND_SERVICE=qa-auto-frontend-dev"
set "ADB_TASK=qa-auto-adb-startup"
set "INSTALL_FRONTEND=0"

if /i "%~1"=="--frontend-dev" set "INSTALL_FRONTEND=1"
if /i "%~1"=="/frontend-dev" set "INSTALL_FRONTEND=1"

net session >nul 2>&1
if errorlevel 1 (
  echo [ERROR] 관리자 권한 CMD/PowerShell에서 실행하세요.
  exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

if defined NSSM_EXE (
  if exist "%NSSM_EXE%" goto :nssm_found
)

for /f "delims=" %%I in ('where nssm 2^>nul') do (
  set "NSSM_EXE=%%I"
  goto :nssm_found
)

echo [ERROR] nssm.exe를 찾지 못했습니다.
echo         nssm.exe 폴더를 PATH에 추가하거나, 아래처럼 지정 후 다시 실행하세요.
echo         set NSSM_EXE=C:\path\to\nssm.exe
exit /b 1

:nssm_found
if not exist "%ROOT%run_server.bat" (
  echo [ERROR] run_server.bat을 찾지 못했습니다: "%ROOT%run_server.bat"
  exit /b 1
)

if not exist "%ROOT%adb_start.bat" (
  echo [ERROR] adb_start.bat을 찾지 못했습니다: "%ROOT%adb_start.bat"
  exit /b 1
)

echo [*] backend service install/update: %BACKEND_SERVICE%
sc query %BACKEND_SERVICE% >nul 2>&1
if errorlevel 1 (
  "%NSSM_EXE%" install %BACKEND_SERVICE% "%ComSpec%" /c "%ROOT%run_server.bat"
) else (
  echo     existing service found; updating NSSM settings
  "%NSSM_EXE%" set %BACKEND_SERVICE% Application "%ComSpec%"
  "%NSSM_EXE%" set %BACKEND_SERVICE% AppParameters /c "%ROOT%run_server.bat"
)
"%NSSM_EXE%" set %BACKEND_SERVICE% AppDirectory "%ROOT%"
"%NSSM_EXE%" set %BACKEND_SERVICE% Start SERVICE_AUTO_START
"%NSSM_EXE%" set %BACKEND_SERVICE% AppStdout "%LOG_DIR%\backend.out.log"
"%NSSM_EXE%" set %BACKEND_SERVICE% AppStderr "%LOG_DIR%\backend.err.log"
"%NSSM_EXE%" set %BACKEND_SERVICE% AppRotateFiles 1
"%NSSM_EXE%" set %BACKEND_SERVICE% AppRotateOnline 1
"%NSSM_EXE%" set %BACKEND_SERVICE% AppRotateBytes 10485760
"%NSSM_EXE%" set %BACKEND_SERVICE% AppExit Default Restart

if "%INSTALL_FRONTEND%"=="1" call :install_frontend || exit /b 1

echo [*] adb startup scheduled task install/update: %ADB_TASK%
schtasks /Create /TN "%ADB_TASK%" /SC ONLOGON /DELAY 0001:00 /TR "\"%ROOT%adb_start.bat\"" /RL HIGHEST /F
if errorlevel 1 (
  echo [ERROR] 작업 스케줄러 등록 실패: %ADB_TASK%
  exit /b 1
)

echo [*] backend service start
"%NSSM_EXE%" start %BACKEND_SERVICE%

if "%INSTALL_FRONTEND%"=="1" (
  echo [*] frontend dev service start
  "%NSSM_EXE%" start %FRONTEND_SERVICE%
)

echo.
echo [OK] setup complete
echo      Backend: http://localhost:8000
if "%INSTALL_FRONTEND%"=="1" echo      Frontend dev: http://localhost:3001
echo      ADB log: "%LOG_DIR%\adb_start.log"
exit /b 0

:install_frontend
where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm을 찾지 못했습니다. Node.js 설치와 PATH를 확인하세요.
  exit /b 1
)

if not exist "%ROOT%frontend\package.json" (
  echo [ERROR] frontend\package.json을 찾지 못했습니다.
  exit /b 1
)

for /f "delims=" %%I in ('where npm 2^>nul') do (
  set "NPM_EXE=%%I"
  goto :npm_found
)

:npm_found
echo [*] frontend dev service install/update: %FRONTEND_SERVICE%
sc query %FRONTEND_SERVICE% >nul 2>&1
if errorlevel 1 (
  "%NSSM_EXE%" install %FRONTEND_SERVICE% "%NPM_EXE%" run dev
) else (
  echo     existing service found; updating NSSM settings
  "%NSSM_EXE%" set %FRONTEND_SERVICE% Application "%NPM_EXE%"
  "%NSSM_EXE%" set %FRONTEND_SERVICE% AppParameters run dev
)
"%NSSM_EXE%" set %FRONTEND_SERVICE% AppDirectory "%ROOT%frontend"
"%NSSM_EXE%" set %FRONTEND_SERVICE% Start SERVICE_AUTO_START
"%NSSM_EXE%" set %FRONTEND_SERVICE% AppStdout "%LOG_DIR%\frontend.out.log"
"%NSSM_EXE%" set %FRONTEND_SERVICE% AppStderr "%LOG_DIR%\frontend.err.log"
"%NSSM_EXE%" set %FRONTEND_SERVICE% AppRotateFiles 1
"%NSSM_EXE%" set %FRONTEND_SERVICE% AppRotateOnline 1
"%NSSM_EXE%" set %FRONTEND_SERVICE% AppRotateBytes 10485760
"%NSSM_EXE%" set %FRONTEND_SERVICE% AppExit Default Restart
exit /b 0
