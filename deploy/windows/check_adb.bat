@echo off
echo ==========================================
echo ADB 상태 확인
echo ==========================================

:: ADB 버전
echo.
echo [1/3] ADB 버전:
adb version

:: 연결된 디바이스
echo.
echo [2/3] 연결된 디바이스:
adb devices

:: Port Forwarding 목록
echo.
echo [3/3] Port Forwarding 목록:
adb forward --list

echo.
echo ==========================================
echo.

:: API 테스트
echo MCP Server API 테스트 (선택사항)
echo API 엔드포인트를 테스트할까요? (Y/N)
set /p TEST_API=

if /i "%TEST_API%"=="Y" (
    echo.
    echo 테스트 중: http://localhost:37772/api/findAllButtons
    curl -s http://localhost:37772/api/findAllButtons
    echo.
)

pause