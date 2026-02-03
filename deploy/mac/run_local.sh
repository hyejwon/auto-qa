#!/bin/bash
set -e

echo "=========================================="
echo "Auto QA - 로컬 실행 (Mac)"
echo "=========================================="

# 프로젝트 루트로 이동
cd "$(dirname "$0")/../.."

# BlueStacks 확인
echo
echo "[1/3] BlueStacks 확인..."
if ! pgrep -x "BlueStacks" > /dev/null; then
    echo "[X] BlueStacks가 실행되지 않았습니다."
    exit 1
fi
echo "[O] BlueStacks 실행 중"

# ADB 설정
echo
echo "[2/3] ADB 설정..."
./deploy/mac/setup_adb.sh

# QA 실행
echo
echo "[3/3] QA 테스트 시작..."
echo "=========================================="

# Docker 사용 여부 선택
echo
echo "실행 방법을 선택하세요:"
echo "1) Docker 컨테이너에서 실행"
echo "2) 로컬 Python에서 실행"
read -p "선택 (1 or 2): " choice

case $choice in
    1)
        echo "Docker 컨테이너에서 실행..."
        docker-compose run --rm auto-qa python main.py
        ;;
    2)
        echo "로컬 Python에서 실행..."
        python main.py
        ;;
    *)
        echo "잘못된 선택입니다."
        exit 1
        ;;
esac

echo
echo "=========================================="
echo "완료!"
echo "=========================================="