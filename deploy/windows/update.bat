@echo off
setlocal enabledelayedexpansion

echo ==========================================
echo Auto QA - 업데이트
echo ==========================================

:: 실행 중인 컨테이너 중지
echo.
echo [1/4] 실행 중인 컨테이너 정리...
docker-compose down 2>nul
echo [O] 정리 완료

:: 기존 이미지 삭제
echo.
echo [2/4] 기존 이미지 삭제...
docker rmi auto-qa:latest 2>nul
echo [O] 삭제 완료

:: 새 이미지 파일 확인
echo.
echo [3/4] 새 이미지 파일 확인...
set FOUND=0
for %%f in (images\auto-qa-*.tar.gz images\auto-qa-*.tar) do (
    set FOUND=1
    set IMAGE_FILE=%%f
)

if !FOUND!==0 (
    echo [X] 새 이미지 파일이 없습니다.
    echo Google Drive에서 최신 파일을 다운로드하세요.
    pause
    exit /b 1
)

:: 압축 해제 (필요시)
echo !IMAGE_FILE! | findstr /C:".gz" >nul
if %ERRORLEVEL! EQU 0 (
    echo 압축 해제 중...
    "C:\Program Files\7-Zip\7z.exe" x "!IMAGE_FILE!" -o"images\" -y
    set IMAGE_FILE=!IMAGE_FILE:.gz=!
)

:: 새 이미지 로드
echo.
echo [4/4] 새 이미지 로드 중... (3-5분 소요)
docker load -i "!IMAGE_FILE!"

if %ERRORLEVEL! EQU 0 (
    echo.
    echo ==========================================
    echo [SUCCESS] 업데이트 완료!
    echo ==========================================
    docker images | findstr auto-qa
    echo.
) else (
    echo [X] 업데이트 실패
)

pause