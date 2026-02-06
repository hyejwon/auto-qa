#!/bin/bash
set -e

echo "=========================================="
echo "Auto QA - 컨테이너 시작"
echo "=========================================="

# 환경 변수
ADB_DEVICE=${ADB_DEVICE:-127.0.0.1:5555}
APK_PATH=${APK_PATH:-/app/apks/cooptd.apk}
MCP_PORT=${MCP_PORT:-37772}
ADB_SERVER_SOCKET=${ADB_SERVER_SOCKET:-}
MAX_RETRIES=10
RETRY_DELAY=3

echo
echo "[1/5] 환경 변수 확인"
echo "  ADB_DEVICE: $ADB_DEVICE"
echo "  APK_PATH: $APK_PATH"
echo "  MCP_PORT: $MCP_PORT"
if [ -n "$ADB_SERVER_SOCKET" ]; then
    echo "  ADB_SERVER_SOCKET: $ADB_SERVER_SOCKET (호스트 ADB 서버 사용)"
fi

# ADB 연결 (호스트 ADB 서버 모드 vs 직접 연결 모드)
echo
echo "[2/5] ADB 연결 중..."

if [ -n "$ADB_SERVER_SOCKET" ]; then
    # 호스트 ADB 서버를 통한 연결 (Docker 환경)
    echo "  호스트 ADB 서버에 연결 중..."
    for i in $(seq 1 $MAX_RETRIES); do
        echo "  시도 $i/$MAX_RETRIES..."

        # 호스트 ADB 서버에서 디바이스 목록 확인
        if adb devices 2>/dev/null | grep -q "$ADB_DEVICE"; then
            echo "  [OK] 호스트 ADB 서버를 통해 디바이스 발견"
            break
        else
            if [ $i -eq $MAX_RETRIES ]; then
                echo "  [ERROR] 호스트 ADB 서버에서 디바이스를 찾을 수 없습니다"
                echo "  호스트에서 다음 명령어를 실행했는지 확인하세요:"
                echo "    adb kill-server && adb -a -P 5037 nodaemon server"
                echo "  그리고 BlueStacks가 실행 중인지 확인하세요."
                exit 1
            fi
            echo "  디바이스 대기 중, ${RETRY_DELAY}초 후 재시도..."
            sleep $RETRY_DELAY
        fi
    done
else
    # 직접 연결 모드 (로컬 환경)
    for i in $(seq 1 $MAX_RETRIES); do
        echo "  시도 $i/$MAX_RETRIES..."

        if adb connect $ADB_DEVICE 2>/dev/null | grep -q "connected"; then
            echo "  [OK] ADB 연결 성공"
            break
        else
            if [ $i -eq $MAX_RETRIES ]; then
                echo "  [ERROR] ADB 연결 실패"
                exit 1
            fi
            echo "  연결 실패, ${RETRY_DELAY}초 후 재시도..."
            sleep $RETRY_DELAY
        fi
    done
fi

# 연결된 디바이스 확인
echo
echo "  연결된 디바이스:"
adb devices

# Port Forwarding 설정
echo
echo "[3/5] Port Forwarding 설정"
echo "  포트: $MCP_PORT"
adb -s $ADB_DEVICE forward tcp:$MCP_PORT tcp:$MCP_PORT

if [ $? -eq 0 ]; then
    echo "  [OK] Port Forwarding 설정 완료"
    echo "  현재 Port Forwarding 목록:"
    adb forward --list | grep $MCP_PORT
else
    echo "  [WARNING] Port Forwarding 설정 실패 (계속 진행)"
fi

# APK 파일 확인
echo
echo "[4/5] APK 파일 확인"
if [ ! -f "$APK_PATH" ]; then
    echo "  [ERROR] APK 파일이 없습니다: $APK_PATH"
    exit 1
fi
echo "  [OK] APK 파일 존재: $APK_PATH"

# APK 설치
echo
echo "[5/5] APK 설치 중..."
echo "  파일: $APK_PATH"

if adb -s $ADB_DEVICE install -r "$APK_PATH"; then
    echo "  [OK] APK 설치 완료"
else
    echo "  [ERROR] APK 설치 실패"
    exit 1
fi

echo
echo "=========================================="
echo "[SUCCESS] 준비 완료"
echo "=========================================="
echo

# 전달된 명령 실행
exec "$@"