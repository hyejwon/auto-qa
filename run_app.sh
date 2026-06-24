#!/usr/bin/env bash
# qa-auto 단일 노드 실행 — Mac/Linux (Python만 필요, Docker/Node 불필요)
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "  qa-auto  (PC에서 바로 실행)"
echo "============================================"

# 1) Python 확인
if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 를 찾을 수 없습니다. Python 3.12 를 설치하세요."
  exit 1
fi

# 2) 가상환경 (최초 1회)
if [ ! -d ".venv" ]; then
  echo "[*] 가상환경 생성 중... (최초 1회)"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 3) 의존성
echo "[*] 의존성 설치/확인 중..."
pip install --upgrade pip >/dev/null
pip install -r requirements-app.txt

# 4) .env
if [ ! -f ".env" ]; then
  echo "[!] .env 가 없어 .env.example 을 복사합니다."
  cp .env.example .env
  echo "[!] .env 를 열어 LLM_GATEWAY_TOKEN 과 ADB_DEVICE 를 채운 뒤 다시 실행하세요."
  exit 1
fi

# 5) adb 확인 (경고만)
command -v adb >/dev/null 2>&1 || echo "[!] 경고: adb 를 PATH 에서 못 찾았습니다. platform-tools 를 PATH 에 추가하세요."

# 6) 4초 뒤 브라우저 자동 오픈 + 서버 실행
echo "[*] 잠시 후 브라우저가 http://localhost:8000 으로 열립니다. (Ctrl+C 로 종료)"
( sleep 4; (open http://localhost:8000 2>/dev/null || xdg-open http://localhost:8000 2>/dev/null) ) &
python api_server.py
