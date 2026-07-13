# qa-auto 배포 및 실행 가이드

## 아키텍처 — 단일 서버 + 무선 디바이스

테스터 PC에 아무것도 설치하지 않는 구조. 서버가 폰을 Wi-Fi ADB로 직접 제어한다.

```
[테스터 브라우저] ──HTTP/WS──> [중앙 서버 (api_server + adb)]
                                     │ adb over Wi-Fi (adb connect 폰IP:5555)
                                     ▼
                              [개인 디바이스 N대 (무선 디버깅 ON)]
```

**테스터 사용 절차** (상세: `USER_SETUP.md` 상단 "테스터용" 섹션)
1. 폰: 무선 디버깅 ON (USB로 최초 1회 `adb tcpip 5555`)
2. 브라우저: `http://서버IP:8000` 접속
3. 헤더의 **"IP로 연결"** → 폰 IP 입력 → 디바이스 선택 → 테스트 실행

**동작 특성**
- 등록한 디바이스는 `state/devices.json`에 영속화되고, 연결이 끊기면 서버가 30초마다 자동 재연결 시도 (`DEVICE_RECONNECT_INTERVAL`로 조정)
- 테스트 실행 중인 디바이스는 잠금 처리 — 다른 세션이 같은 폰에 실행 요청 시 409 거부, 드롭다운에 "실행 중" 표시
- 폰 재부팅 시 무선 디버깅이 꺼지는 기종은 다시 켜줘야 함 (USB로 `adb tcpip 5555`)
- QA 디바이스는 공유기에서 MAC 고정 IP 할당 권장 (IP 바뀌면 재등록 필요)

**네트워크 전제**: 서버 → 폰(5555) 아웃바운드가 가능해야 함. 폰은 게스트 Wi-Fi 불가. 사전 확인: 서버에서 `adb connect 폰IP:5555`

---

## 서버 세팅 A — 사내 Windows PC (기본)

전용 서버 없이 상시 켜두는 사내 Windows PC 1대로 운영한다.

### 사전 준비물

| 항목 | 설명 |
|------|------|
| Python 3.12 | 설치 시 "Add python.exe to PATH" 체크 |
| Git | 코드 받기/업데이트용 |
| ADB (platform-tools) | `adb version` 확인. 없으면 platform-tools의 adb 경로를 PATH에 추가 |
| 고정 IP | IT에 요청 (DHCP면 테스터 접속 주소가 바뀜) |

### 최초 1회 세팅

```bat
git clone <레포주소> qa-auto
cd qa-auto
setup_server.bat   ← 우클릭 → "관리자 권한으로 실행"
```

`setup_server.bat`이 자동으로 처리하는 것:
1. Python/adb 확인 → 가상환경 생성 → 의존성 설치
2. `.env` 생성 (메모장이 열리면 `LLM_GATEWAY_TOKEN`만 채우면 됨 — `ADB_DEVICE`, `UNITY_API_URL`은 **비워둠**)
3. 방화벽 인바운드 8000 허용
4. 절전/최대 절전 해제 (서버가 잠들면 팀 전체 QA 중단)
5. (선택) 로그온 시 자동 시작 등록 (작업 스케줄러 `qa-auto-server`)
6. 서버 IP 안내 후 서버 시작

### 평상시 실행

```bat
run_server.bat
```

- 브라우저 자동 오픈 없이 서버만 상시 실행, 프로세스가 죽으면 5초 후 자동 재시작
- 자동 시작을 등록했다면 PC 로그온 시 알아서 뜸

### 업데이트

```bat
git pull
run_server.bat 재시작 (창 닫고 다시 실행)
```

> 프론트엔드를 수정한 경우에만 `cd frontend && npm run build` 후 재시작 (빌드 결과물이 리포에 포함되어 있어 평소엔 불필요)

### 운영 주의사항

- **끄지 말 것**: 이 PC가 꺼지면 팀 전체 QA가 중단됨. 개인 업무용 PC 말고 공용 PC 사용 권장
- Windows 업데이트 자동 재시작 시간대를 업무 외 시간으로 설정
- `screenshots/`, `recordings/`, `test_results/`가 계속 쌓이므로 주기적 정리

---

## 서버 세팅 B — Linux + Docker (선택)

전용 리눅스 서버가 생기면 Docker로 이전할 수 있다. 이미지에 adb가 포함되어 있다.

```bash
# 서버 초기 설정
sudo mkdir -p /opt/qa-auto && sudo chown $USER:$USER /opt/qa-auto
cd /opt/qa-auto
# .env 생성 (LLM_GATEWAY_URL/TOKEN)
# docker-compose.yaml 복사 후:
docker compose up -d
```

- 브랜치별 스택: `main` → `/opt/qa-auto`(8000, latest) / `dev` → `/opt/qa-auto-dev`(8001, dev 태그)
- GitHub Actions 배포는 현재 **수동 실행 전용** (Actions 탭 → "Deploy Orchestrator (Linux Server)" → Run workflow, 브랜치 선택)
- 자동 배포를 켜려면 `.github/workflows/deploy-server.yml` 상단의 push 트리거 주석 해제
- 대상 디렉토리에 `.env`가 없으면 배포가 의도적으로 실패함

---

## 트러블슈팅

### 서버에서 폰이 안 잡힘 (`adb connect` 실패)
- 폰이 게스트 Wi-Fi에 붙어 있지 않은지 확인 (클라이언트 격리)
- 폰 재부팅 후 무선 디버깅이 꺼졌는지 확인 → USB로 `adb tcpip 5555` 재실행
- 서버↔폰이 다른 VLAN이면 방화벽에서 5555 허용 필요 (IT 협의)

### 테스터 브라우저에서 서버 접속 불가
- 서버 PC 방화벽 8000 인바운드 확인 (`setup_server.bat`이 등록함)
- 서버 IP가 바뀌지 않았는지 확인 (`ipconfig`)

### 디바이스가 "실행 중"으로 계속 표시됨
- 해당 세션 테스트가 실제로 돌고 있는 것. 강제로 풀려면 서버 재시작 (`run_server.bat` 창 닫고 재실행)

### 테스트 중 화면이 이상하게 눌림
- 테스트 실행 중에는 폰 화면을 만지지 말 것 (수동 터치와 자동 입력이 섞임)

### 녹화 파일이 없음
- 실행 시 "녹화 ON" 활성화 여부 확인, `recordings/` 폴더 확인
