# qa-auto 배포 및 실행 가이드

## 아키텍처 개요

```
┌─────────────────────────────────────────────────────────┐
│                    사내 인프라                           │
│                                                         │
│  ┌─────────────────────────┐                            │
│  │  Linux 서버 (Docker)    │                            │
│  │  orchestrator_server    │ ← GitHub Actions 자동 배포  │
│  │  포트: 8000             │                            │
│  │  - React 웹 UI 서빙     │                            │
│  │  - 에이전트 목록 관리   │                            │
│  │  - 템플릿/파이프라인    │                            │
│  │  - LLM 플랜 생성        │                            │
│  └────────────┬────────────┘                            │
│               │ HTTP (에이전트 등록/프록시)              │
│    ┌──────────┼──────────────────────┐                  │
│    ▼          ▼                      ▼                  │
│  ┌──────┐  ┌──────┐            ┌──────┐                 │
│  │테스터│  │테스터│            │테스터│                  │
│  │A PC  │  │B PC  │  ...       │N PC  │                 │
│  │      │  │      │            │      │                 │
│  │ exe  │  │ exe  │            │ exe  │ ← GitHub Releases│
│  │:8000 │  │:8000 │            │:8000 │                 │
│  └──┬───┘  └──┬───┘            └──┬───┘                 │
│     │ ADB     │ ADB               │ ADB                 │
│  ┌──▼───┐  ┌──▼───┐            ┌──▼───┐                 │
│  │LDPlayer│ │LDPlayer│         │LDPlayer│               │
│  └──────┘  └──────┘            └──────┘                 │
│                                                         │
│               ┌────────────────┐                        │
│               │ LLM Gateway    │                        │
│               │ (사내 AI API)  │ ← 에이전트에서 직접 호출│
│               └────────────────┘                        │
└─────────────────────────────────────────────────────────┘
```

**흐름 요약**
- 테스터는 브라우저로 `http://사내서버IP:8000` 접속
- 오케스트레이터가 각 에이전트(exe)에 명령 프록시
- 에이전트가 자기 PC의 LDPlayer를 ADB로 제어
- 에이전트가 LLM Gateway에 직접 호출하여 AI 판단 수행
- WebSocket 로그는 브라우저 → 에이전트 직접 연결

---

## 1단계: 리눅스 서버 초기 설정 (최초 1회)

### 필수 패키지 설치

```bash
# Docker 설치
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# 디렉토리 생성
sudo mkdir -p /opt/qa-auto
sudo chown $USER:$USER /opt/qa-auto
cd /opt/qa-auto
```

### 환경변수 파일 생성

```bash
cat > /opt/qa-auto/.env << 'EOF'
ORCHESTRATOR_PORT=8000
LLM_GATEWAY_URL=https://llm-gateway.111percent.net/llm/google
LLM_GATEWAY_TOKEN=발급받은_토큰값
EOF
```

### docker-compose 파일 복사

GitHub 저장소에서 `docker-compose.yaml`을 서버에 복사:

```bash
scp docker-compose.yaml user@서버IP:/opt/qa-auto/
```

또는 직접 생성:

```bash
cat > /opt/qa-auto/docker-compose.yaml << 'EOF'
version: '3.8'
services:
  orchestrator:
    image: qa-auto-orchestrator:latest
    container_name: qa-orchestrator
    ports:
      - "8000:8000"
    volumes:
      - ./templates:/app/templates
      - ./pipelines:/app/pipelines
      - ./test_results:/app/test_results
      - ./recordings:/app/recordings
    env_file:
      - .env
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
EOF
```

---

## 2단계: GitHub Actions 시크릿 설정 (최초 1회)

GitHub 저장소 → **Settings → Secrets and variables → Actions** 에서 추가:

| 시크릿 이름 | 값 |
|------------|-----|
| `SERVER_HOST` | 사내 리눅스 서버 IP |
| `SERVER_USER` | SSH 접속 유저명 (예: `ubuntu`) |
| `SERVER_SSH_KEY` | SSH 개인키 내용 (`~/.ssh/id_rsa` 전체) |

SSH 키 생성 (없는 경우):

```bash
ssh-keygen -t ed25519 -C "github-actions"
# 생성된 공개키를 서버에 등록
cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys
# 생성된 개인키를 GitHub 시크릿에 등록
cat ~/.ssh/id_ed25519
```

---

## 3단계: 서버 자동 배포 (GitHub Actions)

### 브랜치 전략 — dev/prod 분리

| 브랜치 | 환경 | 서버 디렉토리 | 이미지 태그 | 포트 | 컨테이너 |
|--------|------|--------------|------------|------|----------|
| `main` | prod | `/opt/qa-auto` | `latest` | 8000 | `qa-orchestrator` |
| `dev` | dev | `/opt/qa-auto-dev` | `dev` | 8001 | `qa-orchestrator-dev` |

- 작업 흐름: 기능 브랜치 → `dev` 머지(dev 서버 자동 배포, `http://서버IP:8001`에서 검증) → `main` 머지(prod 자동 배포)
- 두 스택은 같은 서버에 나란히 뜨며 templates/pipelines/test_results/recordings 데이터가 서로 분리됨
- 에이전트 EXE 릴리즈는 `main` 푸시에서만 빌드됨 (dev는 서버 스택만 배포)

**dev 스택 최초 1회 설정** (prod와 동일하되 디렉토리만 다름):

```bash
sudo mkdir -p /opt/qa-auto-dev
sudo chown $USER:$USER /opt/qa-auto-dev
# .env 생성 — ORCHESTRATOR_PORT는 컨테이너 내부 포트이므로 8000 유지 (외부 8001 매핑은 워크플로우가 처리)
cp /opt/qa-auto/.env /opt/qa-auto-dev/.env
```

> `.env`가 없으면 배포 워크플로우가 실패하도록 되어 있음 (실수로 빈 설정 배포 방지)

`main` 또는 `dev` 브랜치에 코드 푸시 시 자동 실행:

```
orchestrator_server.py, config.py, frontend/** 등 변경
        ↓ git push origin main
GitHub Actions (ubuntu-latest)
  1. React 빌드 (npm run build)
  2. Docker 이미지 빌드
  3. 이미지 tar.gz → 서버 SCP 전송
  4. SSH 접속 → docker compose up
        ↓
서버 자동 재시작 완료
```

### 수동 배포 (필요시)

```bash
# 로컬에서 이미지 빌드 후 서버 전송
cd frontend && npm run build && cd ..
docker build -t qa-auto-orchestrator:latest .
docker save qa-auto-orchestrator:latest | gzip > qa-auto-orchestrator.tar.gz
scp qa-auto-orchestrator.tar.gz user@서버IP:/opt/qa-auto/

# 서버에서
ssh user@서버IP
cd /opt/qa-auto
docker load < qa-auto-orchestrator.tar.gz
docker compose up -d --no-build
```

### 서버 상태 확인

```bash
docker ps                          # 컨테이너 실행 여부
docker logs qa-orchestrator -f     # 실시간 로그
curl http://localhost:8000/health  # 헬스체크
```

---

## 4단계: 에이전트 EXE 빌드 (GitHub Actions)

`main` 브랜치에 에이전트 관련 파일 변경 시 자동 빌드:

```
agent_server.py, vision_agent.py 등 변경
        ↓ git push origin main
GitHub Actions (windows-latest)
  1. React 빌드
  2. PyInstaller → qa-auto.exe
  3. .env.example 포함하여 zip 압축
  4. GitHub Releases 업로드
        ↓
Releases 페이지에서 다운로드 가능
```

**다운로드 위치**: GitHub 저장소 → **Releases** → 최신 `qa-auto-agent-*.zip`

---

## 5단계: 테스터 PC 설정

### 준비물
- Windows 10/11
- LDPlayer (또는 BlueStacks) 설치 및 실행 중
- 사내 네트워크 연결

### 설치 절차

**1. EXE 다운로드**

GitHub Releases에서 `qa-auto-agent-*.zip` 다운로드 후 압축 해제

```
qa-auto-agent/
├── qa-auto.exe
└── .env.example
```

**2. 환경변수 파일 설정**

`.env.example`을 `.env`로 복사 후 편집:

```env
# 오케스트레이터 서버 주소
ORCHESTRATOR_URL=http://사내서버IP:8000

# 이 PC의 식별 이름 (팀원끼리 중복 금지)
AGENT_NAME=홍길동-PC

# 에이전트 포트 (기본값 사용)
AGENT_PORT=8000

# LDPlayer ADB 주소 (기본값 사용)
ADB_DEVICE=127.0.0.1:5555

# LLM Gateway
LLM_GATEWAY_URL=https://llm-gateway.111percent.net/llm/google
LLM_GATEWAY_TOKEN=발급받은_토큰값
```

**3. LDPlayer ADB 포트 확인**

LDPlayer가 여러 개 실행 중인 경우 포트가 다를 수 있음:
- LDPlayer 1번: `127.0.0.1:5555`
- LDPlayer 2번: `127.0.0.1:5557`

확인 방법: LDPlayer → 설정 → 기타 → ADB 포트 확인

**4. EXE 실행**

`qa-auto.exe` 더블클릭 (또는 터미널에서 실행)

```
[INFO] qa-auto Agent 시작 중...
[INFO] ADB 디바이스 연결: 127.0.0.1:5555
[INFO] 오케스트레이터 등록 완료: http://사내서버IP:8000
[INFO] Uvicorn running on http://0.0.0.0:8000
```

> **방화벽 알림이 뜨면 "허용" 클릭** (오케스트레이터와 통신 필요)

---

## 6단계: 사용

**테스터 각자**의 접속 방법:

```
브라우저 → http://사내서버IP:8000
```

1. 상단 에이전트 드롭다운에서 자신의 PC 선택
2. 사전 점검(Preflight) 자동 실행
3. 파이프라인 탭에서 테스트 실행

---

## 업데이트 절차

### 서버 업데이트 (개발자)
```
코드 수정 → git push origin main → GitHub Actions 자동 배포
```
별도 작업 불필요. 약 3-5분 후 서버 자동 재시작.

### 에이전트 업데이트 (테스터)
```
GitHub Releases → 최신 zip 다운로드 → 기존 exe 교체 → .env는 그대로 유지
```

---

## 트러블슈팅

### 에이전트가 오케스트레이터에 등록되지 않음
- `.env`의 `ORCHESTRATOR_URL` 확인 (IP, 포트 정확히)
- 서버 방화벽에서 8000 포트 허용 여부 확인
- `curl http://사내서버IP:8000/health` 로 서버 응답 확인

### ADB 연결 실패
- LDPlayer 실행 중인지 확인
- `.env`의 `ADB_DEVICE` 포트 확인 (LDPlayer 설정에서 확인)
- `adb devices` 명령으로 수동 확인

### 브라우저에서 에이전트가 보이지 않음
- exe 실행 후 콘솔에 "오케스트레이터 등록 완료" 메시지 확인
- 브라우저 새로고침 (15초마다 자동 갱신)
- 에이전트 오프라인 상태: exe를 재실행

### 미러링이 안 보임
- 에이전트 선택 후 미러링 버튼 클릭
- ADB 연결 상태 확인 (사전 점검 탭)

### 녹화 파일이 없음
- 테스트 실행 시 "녹화 ON" 버튼 활성화 확인 (빨간색)
- 녹화 탭에서 에이전트 선택 후 새로고침
