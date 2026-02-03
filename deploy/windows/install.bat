@echo off
setlocal enabledelayedexpansion

echo ==========================================
echo Auto QA - 초기 설치
echo ==========================================

:: Docker Desktop 확인
echo.
echo [1/6] Docker Desktop 확인...
docker info >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [X] Docker Desktop이 실행되지 않았습니다.
    echo.
    echo 해결 방법:
    echo 1. Docker Desktop 설치: https://www.docker.com/products/docker-desktop
    echo 2. Docker Desktop 실행
    echo 3. install.bat 다시 실행
    pause
    exit /b 1
)
echo [O] Docker Desktop 실행 중

:: 이미지 파일 확인
echo.
echo [2/6] 이미지 파일 확인...
set FOUND=0
for %%f in (images\auto-qa-*.tar.gz images\auto-qa-*.tar) do (
    set FOUND=1
    set IMAGE_FILE=%%f
)

if !FOUND!==0 (
    echo [X] 이미지 파일이 없습니다.
    echo.
    echo 다음 단계를 수행하세요:
    echo 1. Google Drive에서 auto-qa-latest.tar.gz 다운로드
    echo 2. 이 폴더의 images\ 폴더에 저장
    echo 3. install.bat 다시 실행
    echo.
    echo Google Drive 링크를 브라우저에서 열까요? (Y/N)
    set /p OPEN_BROWSER=
    if /i "!OPEN_BROWSER!"=="Y" (
        start https://drive.google.com/drive/folders/YOUR_FOLDER_ID
    )
    pause
    exit /b 1
)
echo [O] 이미지 파일 발견: !IMAGE_FILE!

:: 압축 해제 (필요시)
echo.
echo [3/6] 파일 형식 확인...
echo !IMAGE_FILE! | findstr /C:".gz" >nul
if %ERRORLEVEL% EQU 0 (
    echo [!] gzip 압축 파일 감지
    echo 압축 해제 중...
    
    "C:\Program Files\7-Zip\7z.exe" x "!IMAGE_FILE!" -o"images\" -y
    
    if %ERRORLEVEL% NEQ 0 (
        echo [X] 압축 해제 실패
        echo 7-Zip 설치 필요: https://www.7-zip.org/
        pause
        exit /b 1
    )
    
    set IMAGE_FILE=!IMAGE_FILE:.gz=!
    echo [O] 압축 해제 완료
)

:: Docker 이미지 로드
echo.
echo [4/6] Docker 이미지 로드 중...
echo 시간이 걸릴 수 있습니다 (3-5분)...
docker load -i "!IMAGE_FILE!"

if %ERRORLEVEL! NEQ 0 (
    echo [X] 이미지 로드 실패
    pause
    exit /b 1
)
echo [O] 이미지 로드 완료!

:: 디렉토리 생성
echo.
echo [5/6] 작업 디렉토리 생성...
if not exist "apks" mkdir apks
if not exist "report" mkdir report
if not exist "screenshots" mkdir screenshots
if not exist "logs" mkdir logs
echo [O] 디렉토리 생성 완료

:: 환경 설정
echo.
echo [6/6] 환경 설정...
if not exist ".env" (
    echo [!] .env 파일을 생성해야 합니다.
    echo.
    
    if exist ".env.example" (
        copy /Y .env.example .env >nul
        echo [!] .env.example을 복사했습니다.
        echo [!] .env 파일을 열어서 API 키를 입력하세요.
        notepad .env
    ) else (
        echo [X] .env.example 파일이 없습니다.
        echo 직접 .env 파일을 생성해주세요.
    )
) else (
    echo [O] .env 파일이 이미 존재합니다
)

:: 설치 완료
echo.
echo ==========================================
echo [SUCCESS] 설치 완료!
echo ==========================================
echo.
echo 설치된 이미지:
docker images | findstr auto-qa
echo.
echo ==========================================
echo 다음 단계:
echo 1. .env 파일에 API 키 입력
echo 2. apks 폴더에 cooptd.apk 복사
echo 3. BlueStacks 실행
echo 4. run_qa.bat 실행
echo ==========================================
echo.
pause