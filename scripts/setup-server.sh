#!/bin/bash
# qa-auto 오케스트레이터 서버 초기 세팅 스크립트
# 사용법: bash setup-server.sh

set -e

echo "=== qa-auto 서버 초기 세팅 ==="

# 1. Docker 설치 확인
if ! command -v docker &> /dev/null; then
    echo "[1/4] Docker 설치 중..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    echo "Docker 설치 완료. 재로그인 후 다시 실행하세요."
    exit 0
else
    echo "[1/4] Docker 이미 설치됨"
fi

# 2. 디렉토리 생성
echo "[2/4] 디렉토리 생성..."
sudo mkdir -p /opt/qa-auto/{templates,pipelines,test_results,recordings}
sudo chown -R $USER:$USER /opt/qa-auto
echo "완료: /opt/qa-auto"

# 3. .env 파일 생성
if [ ! -f /opt/qa-auto/.env ]; then
    echo "[3/4] .env 파일 생성..."
    cat > /opt/qa-auto/.env << 'EOF'
# LLM Gateway
LLM_GATEWAY_URL=https://llm-gateway.111percent.net/llm/google
LLM_GATEWAY_TOKEN=여기에_토큰_입력

# 서버 포트
ORCHESTRATOR_PORT=8000

# Langfuse (선택)
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=
EOF
    echo "완료: /opt/qa-auto/.env (토큰 값을 직접 수정해주세요)"
else
    echo "[3/4] .env 파일 이미 존재 — 건너뜀"
fi

# 4. 방화벽 포트 오픈 (ufw 사용 시)
if command -v ufw &> /dev/null; then
    echo "[4/4] 방화벽 포트 8000 오픈..."
    sudo ufw allow 8000/tcp
    echo "완료"
else
    echo "[4/4] ufw 없음 — 방화벽 설정 필요 시 수동으로 포트 8000 오픈"
fi

echo ""
echo "=== 세팅 완료 ==="
echo "다음 단계:"
echo "  1. /opt/qa-auto/.env 파일에서 LLM_GATEWAY_TOKEN 입력"
echo "  2. GitHub Actions Secrets 등록:"
echo "     SERVER_HOST = $(hostname -I | awk '{print $1}')"
echo "     SERVER_USER = $USER"
echo "     SERVER_SSH_KEY = (cat ~/.ssh/id_rsa)"
echo "  3. SSH 공개키 등록: cat ~/.ssh/id_rsa.pub >> ~/.ssh/authorized_keys"
echo ""
echo "  수동 실행 (GitHub Actions 전 테스트용):"
echo "    cd /opt/qa-auto"
echo "    docker compose -f docker-compose.orchestrator.yml up -d"
