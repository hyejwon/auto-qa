# QA 자동화 테스트 도구 가이드

AI 기반 모바일 QA 자동화 도구입니다. 자연어 시나리오를 입력하면 Gemini가 테스트 플랜을 생성하고, ADB로 연결된 Android 디바이스에서 자동으로 테스트를 실행합니다.

---

## 목차

1. [아키텍처](#1-아키텍처)
2. [환경 설정](#2-환경-설정)
3. [로컬 실행](#3-로컬-실행)
4. [Docker 실행](#4-docker-실행)
5. [UI 사용법](#5-ui-사용법)
6. [템플릿 작성 가이드](#6-템플릿-작성-가이드)
7. [액션 타입 레퍼런스](#7-액션-타입-레퍼런스)
8. [API 레퍼런스](#8-api-레퍼런스)
9. [모듈 구성](#9-모듈-구성)
10. [트러블슈팅](#10-트러블슈팅)
11. [Windows 실행파일 빌드](#11-windows-실행파일-빌드)

---

## 1. 아키텍처

```
┌──────────────────────────────────────────────────────┐
│           React SPA (frontend/src)                   │
│   TemplateCreateTab │ TemplateRunTab │ RecordingsTab  │
│              Vite + TypeScript + Tailwind             │
└───────────────────────┬──────────────────────────────┘
                        │ REST API + WebSocket
                        ▼
┌──────────────────────────────────────────────────────┐
│           FastAPI Backend (api_server.py :8000)       │
│  /api/*  REST Endpoints   /ws/logs/{id}  WebSocket   │
├──────────────────────────────────────────────────────┤
│  PlannerNode            │  QAOrchestrator             │
│  (Gemini LLM)           │  (테스트 실행 엔진)          │
│  자연어 → YAML 변환      │  스텝 순차 실행             │
├────────────┬────────────┼────────────┬────────────────┤
│ VisionAgent│ElementCache│ADBController│UnityAPIClient │
│ (Gemini    │ (SQLite)   │ (ADB 명령)  │ (Unity SR     │
│  Vision)   │            │             │  치트 API)    │
└────────────┴────────────┴────────────┴────────────────┘
                                   │
                           ┌───────┴───────┐
                           │ Android Device │
                           │   (via ADB)    │
                           └───────────────┘
```

### 실행 흐름

1. **시나리오 입력** → React UI에서 자연어 테스트 시나리오 작성
2. **플랜 생성** → `POST /api/plan/generate` → `PlannerNode`가 Gemini API로 YAML 생성
3. **템플릿 저장** → `POST /api/templates` → `templates/` 디렉토리에 YAML 저장
4. **템플릿 로드 & 편집** → `GET /api/templates/{name}` → UI에서 스텝 편집
5. **테스트 실행** → `POST /api/test/run` + `WS /ws/logs/{session_id}` → 실시간 로그 스트리밍
6. **결과 저장** → `test_results/`에 JSON, 화면 녹화는 `recordings/`에 MP4

---

## 2. 환경 설정

### 사전 요구사항

| 항목 | 요구 버전 |
|:---|:---|
| Python | 3.12+ |
| Node.js | 18+ |
| ADB | Android Platform Tools |
| GCP 프로젝트 | Vertex AI API 활성화 |

### .env 파일

프로젝트 루트에 `.env` 파일을 생성합니다:

```env
# GCP / Vertex AI
GOOGLE_APPLICATION_CREDENTIALS=./credentials.json

# (선택) 특정 디바이스 지정
ADB_DEVICE=emulator-5554

# Langfuse (선택)
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=
```

### GCP 인증

`credentials.json` 파일을 프로젝트 루트에 배치합니다. `.env`에 경로를 지정하지 않아도 `api_server.py`가 실행 파일 옆의 `credentials.json`을 자동으로 탐지합니다.

### Python 의존성 설치

```bash
pip install -r requirements.txt
```

주요 의존성:
- `fastapi`, `uvicorn` — 백엔드 API 서버
- `google-genai` — Gemini API
- `langfuse` — LLM 호출 추적 (옵션)
- `pydantic` — 데이터 모델
- `pillow` — 이미지 처리 (스크린샷 비교)
- `pyyaml` — 템플릿 파싱
- `python-dotenv` — 환경 변수 로드

---

## 3. 로컬 실행

### ADB 디바이스 연결 확인

```bash
adb devices
# List of devices attached
# emulator-5554   device
```

### 백엔드 서버 실행

```bash
python api_server.py
```

### 프론트엔드 개발 서버 실행 (개발 시)

```bash
cd frontend
npm install
npm run dev
```

프론트엔드 개발 서버(`http://localhost:5173`)는 API 요청을 `http://localhost:8000`으로 프록시합니다.

> **프로덕션 모드**: `npm run build` 후 `python api_server.py`만 실행하면 `http://localhost:8000`에서 React UI와 API가 함께 서빙됩니다.

### 프론트엔드 빌드

```bash
cd frontend
npm install
npm run build   # frontend/dist/ 생성
```

빌드 후 `api_server.py`가 `frontend/dist`를 정적 파일로 서빙합니다.

---

## 4. Docker 실행

### 빌드 & 실행

```bash
docker compose up --build -d
```

### 로그 확인

```bash
docker logs qa-app --tail 50 -f
```

### Docker에서 ADB 사용

Docker 컨테이너는 호스트의 ADB 서버에 연결합니다. **호스트에서 ADB 서버가 실행 중이어야** 합니다:

```bash
# 호스트에서 ADB 서버 시작
adb start-server

# 디바이스 연결 확인
adb devices
```

`.env`에 아래 설정을 추가합니다:

```env
ANDROID_ADB_SERVER_ADDRESS=host.docker.internal
ANDROID_ADB_SERVER_PORT=5037
```

### 주요 볼륨 마운트

| 호스트 경로 | 컨테이너 경로 | 설명 |
|:---|:---|:---|
| `./templates` | `/app/templates` | 테스트 템플릿 YAML |
| `./test_results` | `/app/test_results` | 실행 결과 JSON |
| `./screenshots` | `/app/screenshots` | 스텝별 스크린샷 |
| `./recordings` | `/app/recordings` | 화면 녹화 영상 |
| `./apks` | `/app/apks` | 설치용 APK 파일 |

---

## 5. UI 사용법

브라우저에서 `http://localhost:8000` 접속. 화면 상단의 디바이스 상태 표시줄과 3개 탭으로 구성됩니다.

### 디바이스 상태 (상단 고정)

- 현재 연결된 ADB 디바이스 ID와 모델명을 상시 표시
- WiFi ADB 연결: IP 주소 입력 후 연결/해제

### 5.1 템플릿 생성 탭

1. **샘플 시나리오** 버튼으로 빠르게 시나리오 불러오기
2. **패키지명** 선택 (드롭다운)
3. **테스트 시나리오** 입력: 자연어로 테스트 시나리오 작성
4. **플랜 생성** 클릭 → Gemini가 YAML 테스트 플랜 생성
5. 생성된 YAML을 직접 편집 가능
6. **템플릿으로 저장** 클릭 → `templates/` 디렉토리에 저장

#### 시나리오 작성 팁

```
앱을 실행한다.
→ 햄버거 메뉴를 클릭한다.
→ 설정 메뉴로 진입한다.
→ 진동 ON 버튼을 클릭한다.
→ 앱을 재실행한다.
→ 진동 OFF 표시가 보이는지 확인한다.
→ 앱을 종료한다.
```

- `→`로 스텝을 구분하면 가독성이 좋습니다
- "클릭한다", "확인한다", "대기한다" 등 명확한 동사 사용
- 검증 항목은 "~이 보이는지 확인한다" 형태로 작성

### 5.2 템플릿 실행 탭

1. 드롭다운에서 **템플릿 선택** → **로드** 클릭
2. 로드된 스텝을 UI에서 편집:
   - **액션** 변경 (드롭다운)
   - **target** 수정 (텍스트 입력 또는 패키지/APK 선택)
   - **설명** 수정
   - **expect_visible** / **expect_hidden** 입력
   - `⠿` 드래그 핸들로 순서 이동
   - **휴지통** 아이콘으로 스텝 삭제
   - **스텝 추가** 버튼으로 새 스텝 추가
3. **녹화** 체크박스 활성화 시 테스트 중 화면 자동 녹화
4. **실행** 클릭 → WebSocket으로 실시간 로그 출력
5. 실행 완료 후 결과 요약 표시 (PASS/FAIL, 스텝별 결과)
6. **중단** 버튼으로 실행 중지 가능
7. **저장** 버튼으로 현재 스텝 상태를 템플릿으로 덮어쓰기

### 5.3 녹화 영상 탭

- 테스트 실행 시 자동으로 화면 녹화 (실행 탭에서 녹화 체크박스 활성화 필요)
- 녹화 파일 선택 → 미리보기 및 다운로드

---

## 6. 템플릿 작성 가이드

템플릿은 `templates/` 디렉토리에 YAML 파일로 저장됩니다.

### 기본 구조

```yaml
title: 테스트 제목
description: 테스트 설명
package: com.example.app
steps:
  - action: launch_app
    target: null
    params:
      package: com.example.app
    description: 앱을 실행한다.
    timeout: 30
    retry: 3

  - action: find_and_tap
    target: 로그인 버튼
    params:
      expect_visible: 메인 화면
      wait_seconds: 2
    description: 로그인 버튼을 클릭한다.
    timeout: 10
    retry: 2

  - action: verify
    target: 메인 화면 타이틀
    params: {}
    description: 메인 화면이 정상 출력되는지 확인한다.
    timeout: 10
    retry: 2

  - action: close_app
    target: null
    params:
      package: com.example.app
    description: 앱을 종료한다.
    timeout: 10
    retry: 1

expected_results:
  - 로그인 후 메인 화면이 정상 출력되어야 한다.
preconditions: []
source_scenario: 원본 자연어 시나리오 텍스트
```

### 스텝 필드 설명

| 필드 | 타입 | 설명 |
|:---|:---|:---|
| `action` | string | 액션 타입 (아래 레퍼런스 참고) |
| `target` | string \| null | 대상 UI 요소 (자연어 설명) |
| `params` | object | 액션별 추가 파라미터 |
| `description` | string | 스텝 설명 (로그에 표시) |
| `timeout` | int | 타임아웃 (초) |
| `retry` | int | 재시도 횟수 |

---

## 7. 액션 타입 레퍼런스

### `launch_app` — 앱 실행

```yaml
- action: launch_app
  target: com.example.app
```

앱을 실행하고 화면 안정화까지 자동 대기합니다. 별도 `wait` 스텝 불필요.

### `close_app` — 앱 종료

```yaml
- action: close_app
  target: com.example.app
```

### `install_app` — APK 설치

```yaml
- action: install_app
  target: app-release.apk   # apks/ 디렉토리 기준 파일명
```

### `uninstall_app` — 앱 삭제

```yaml
- action: uninstall_app
  target: com.example.app
```

### `find_and_tap` — UI 요소 찾기 & 탭

```yaml
- action: find_and_tap
  target: 구글 로그인 버튼
  params:
    expect_visible: 이용약관 화면        # (필수) 탭 후 보여야 하는 요소
    expect_hidden: 로그인 버튼           # (선택) 탭 후 사라져야 하는 요소
    wait_seconds: 2                      # (선택) 탭 후 대기 시간
    verify_timeout_sec: 2.0              # (선택) 검증 타임아웃
```

실행 순서:
1. 화면 안정화 대기
2. 고정 좌표 매칭 시도 (`FIXED_TAP_TARGETS`)
3. 실패 시 Gemini Vision으로 요소 위치 탐지
4. 탭 실행
5. `expect_visible` / `expect_hidden` 검증

> **중요**: `expect_visible`은 필수 파라미터입니다. 탭 후 어떤 화면/요소가 나타나야 하는지 반드시 명시하세요.

### `verify` — 화면 검증

```yaml
- action: verify
  target: 설정 팝업
```

Gemini Vision으로 현재 화면에 target이 존재하는지 확인합니다.

### `read_text` — 텍스트 읽기 & 비교

```yaml
# 값 읽어서 저장
- action: read_text
  target: 현재 다이아 보유량 텍스트
  params:
    save_as: diamond_before

# 이전 값과 비교
- action: read_text
  target: 현재 다이아 보유량 텍스트
  params:
    compare_with: diamond_before
    expect_changed: true          # true: 값이 달라야 PASS / false: 같아야 PASS
```

재화 변동, PID 변경 등을 검증할 때 사용합니다.

### `input_text` — 텍스트 입력

```yaml
- action: input_text
  target: 입력할 텍스트 내용
```

### `wait` — 대기

```yaml
- action: wait
  params:
    seconds: 5
```

### `back` — 뒤로가기

```yaml
# 단순 뒤로가기
- action: back

# 뒤로가기 + 검증 (atomic)
- action: back
  params:
    expect_visible: 이전 화면 요소
    expect_hidden: 현재 팝업
    wait_seconds: 1.0
```

### `home` — 홈 버튼

```yaml
- action: home
```

### `swipe` — 스와이프

```yaml
- action: swipe
  params:
    x1: 360
    y1: 800
    x2: 360
    y2: 400
    duration: 300
```

### `skip_tutorial` — 튜토리얼 스킵

```yaml
- action: skip_tutorial
  target: com.example.app
```

Unity SR 치트 API를 호출하여 튜토리얼을 건너뜁니다.

---

## 8. API 레퍼런스

백엔드는 `http://localhost:8000`에서 실행됩니다. 전체 OpenAPI 문서는 `http://localhost:8000/docs`에서 확인할 수 있습니다.

### 디바이스

| 메서드 | 경로 | 설명 |
|:---|:---|:---|
| GET | `/api/device` | 기본 연결 디바이스 상태 |
| GET | `/api/devices` | 전체 연결 디바이스 목록 |
| POST | `/api/devices/connect` | ADB WiFi 연결 `{address: "192.168.x.x:5555"}` |
| POST | `/api/devices/disconnect` | ADB WiFi 연결 해제 `{address: "..."}` |

### APK / 패키지

| 메서드 | 경로 | 설명 |
|:---|:---|:---|
| GET | `/api/apks` | `apks/` 디렉토리 APK 파일 목록 |
| POST | `/api/apk/install` | APK 설치 `{filename: "app.apk"}` |
| GET | `/api/packages` | 지원 패키지 목록 |
| POST | `/api/app/uninstall` | 앱 삭제 `{package: "com.example.app"}` |

### 템플릿

| 메서드 | 경로 | 설명 |
|:---|:---|:---|
| GET | `/api/templates` | 템플릿 목록 |
| GET | `/api/templates/{name}` | 템플릿 YAML 조회 |
| POST | `/api/templates` | 템플릿 저장 `{name, content, scenario}` |
| DELETE | `/api/templates/{name}` | 템플릿 삭제 |

### 플랜 생성

| 메서드 | 경로 | 설명 |
|:---|:---|:---|
| POST | `/api/plan/generate` | Gemini로 YAML 플랜 생성 `{scenario, package}` |

### 테스트 실행

| 메서드 | 경로 | 설명 |
|:---|:---|:---|
| POST | `/api/test/run` | 테스트 실행 시작 `{title, package, steps, session_id, record}` |
| POST | `/api/test/stop` | 테스트 중단 `{session_id}` |

### WebSocket — 실시간 로그

```
WS /ws/logs/{session_id}
```

`POST /api/test/run` 직후 WebSocket을 연결하면 실행 로그를 실시간으로 수신합니다.

메시지 타입:

| type | 설명 |
|:---|:---|
| `log` | 실행 로그 문자열 |
| `result` | 테스트 결과 객체 (PASS/FAIL, 스텝 결과) |
| `error` | 오류 메시지 |
| `done` | 실행 종료 신호 |
| `ping` | 연결 유지 신호 (30초 타임아웃 방지) |

### 녹화 영상

| 메서드 | 경로 | 설명 |
|:---|:---|:---|
| GET | `/api/recordings` | 녹화 MP4 파일 목록 |
| GET | `/recordings/{filename}` | MP4 파일 직접 접근 (StaticFiles) |

---

## 9. 모듈 구성

### 백엔드

| 파일 | 역할 |
|:---|:---|
| `api_server.py` | FastAPI 서버 진입점 — REST API + WebSocket + SPA 서빙 |
| `config.py` | 경로(exe/bundle/dev 분기) 및 Gemini API 설정 |
| `qa_orchestrator.py` | 테스트 실행 엔진 (스텝 순차 실행, 화면 안정화, 검증) |
| `planner_node.py` | 자연어 → YAML 테스트 플랜 변환 (Gemini API) |
| `vision_agent.py` | 스크린샷 기반 UI 요소 탐지 (Gemini Vision) |
| `adb_controller.py` | ADB 명령 래퍼 (탭, 스와이프, 스크린샷, 녹화) |
| `unity_api_client.py` | Unity 인게임 치트 API 클라이언트 |
| `element_cache.py` | UI 요소 좌표 캐시 (SQLite) |
| `test_manager.py` | 테스트케이스/결과 Pydantic 모델 및 YAML 로드/저장 |

### 프론트엔드 (`frontend/src/`)

| 파일 | 역할 |
|:---|:---|
| `App.tsx` | 루트 컴포넌트 — 탭 네비게이션 (템플릿 생성 / 실행 / 녹화) |
| `components/DeviceStatus.tsx` | 상단 디바이스 상태 표시 및 WiFi ADB 연결 UI |
| `components/tabs/TemplateCreateTab.tsx` | 자연어 시나리오 → Gemini 플랜 생성 → 템플릿 저장 |
| `components/tabs/TemplateRunTab.tsx` | 템플릿 로드 → 스텝 편집(DnD) → 실행 → WebSocket 로그 |
| `components/tabs/RecordingsTab.tsx` | 녹화 영상 목록 및 미리보기 |
| `api/client.ts` | axios 기반 API 클라이언트 |
| `types/index.ts` | TypeScript 타입 정의 및 액션 목록 상수 |

### 디렉토리 구조

```
auto-qa/
├── api_server.py            # 백엔드 진입점
├── config.py
├── qa_orchestrator.py
├── planner_node.py
├── vision_agent.py
├── adb_controller.py
├── unity_api_client.py
├── element_cache.py
├── test_manager.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yaml
├── .env                     # 환경 변수
├── credentials.json         # GCP 인증
├── frontend/
│   ├── src/                 # React 소스
│   ├── dist/                # 빌드 결과 (npm run build 후 생성)
│   └── package.json
├── templates/               # 테스트 템플릿 YAML
├── test_results/            # 실행 결과 JSON
├── screenshots/             # 스텝별 스크린샷
├── screenshots_debug/       # Vision 디버그 이미지
├── recordings/              # 화면 녹화 영상 MP4
└── apks/                    # 설치용 APK 파일
```

---

## 10. 트러블슈팅

### ADB 연결 실패

```
ConnectionError: No device connected
```

- `adb devices`로 디바이스 연결 확인
- Docker 환경: 호스트에서 `adb start-server` 실행 확인
- `.env`에 `ANDROID_ADB_SERVER_ADDRESS=host.docker.internal` 설정 확인

### Gemini API 인증 오류

- `credentials.json` 파일이 실행파일(또는 `api_server.py`) 옆에 있는지 확인
- `GOOGLE_APPLICATION_CREDENTIALS` 환경 변수 확인
- GCP 프로젝트에서 Vertex AI API 활성화 확인

### UI가 표시되지 않는 경우 (프로덕션 모드)

`frontend/dist`가 없으면 React UI가 서빙되지 않습니다.

```bash
cd frontend && npm install && npm run build
```

빌드 후 `python api_server.py`를 재시작합니다.

### Vision으로 요소를 찾지 못하는 경우

- `target` 설명을 더 구체적으로 작성 (예: "버튼" → "좌측 상단 햄버거 메뉴 버튼")
- `screenshots_debug/` 폴더에서 디버그 이미지 확인
- `FIXED_TAP_TARGETS`에 고정 좌표 추가 고려

### 화면 안정화 타임아웃

```
Screen did not stabilize within 10.0s
```

- 게임 애니메이션이 계속되는 화면에서 발생 가능
- `wait` 스텝으로 충분한 대기 시간 확보
- `QAOrchestrator.STABILITY_THRESHOLD` 값 조정 (기본 0.01)

---

## 11. Windows 실행파일 빌드

PyInstaller를 사용하여 Windows 단독 실행파일(`.exe`)을 생성합니다.

> **중요**: Windows `.exe`는 **반드시 Windows 환경에서** 빌드해야 합니다. macOS/Linux에서 크로스 컴파일은 지원되지 않습니다.

### 사전 준비

#### 1. PyInstaller 설치

```bash
pip install pyinstaller
```

#### 2. 프론트엔드 빌드

빌드 전에 `frontend/dist`가 최신 상태인지 확인합니다:

```bash
cd frontend
npm install
npm run build
cd ..
```

#### 3. 필수 파일 확인

빌드 전 아래 파일/디렉토리가 존재해야 합니다:

```
auto-qa/
├── api_server.py          # 빌드 진입점
├── frontend/
│   └── dist/             # 프론트엔드 빌드 결과물 (반드시 존재)
└── templates/            # 테스트 템플릿 디렉토리 (비어있어도 됨)
```

### 빌드 명령

프로젝트 루트 디렉토리에서 실행합니다 (cmd):

```cmd
pyinstaller --onefile --name auto-qa ^
  --add-data "frontend/dist;frontend/dist" ^
  --add-data "templates;templates" ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.lifespan.on ^
  --collect-all google-genai ^
  --copy-metadata google-genai ^
  --hidden-import langfuse ^
  api_server.py
```

PowerShell에서는 `^` 대신 `` ` ``(백틱) 사용:

```powershell
pyinstaller --onefile --name auto-qa `
  --add-data "frontend/dist;frontend/dist" `
  --add-data "templates;templates" `
  --hidden-import uvicorn.logging `
  --hidden-import uvicorn.loops.auto `
  --hidden-import uvicorn.protocols.http.auto `
  --hidden-import uvicorn.protocols.websockets.auto `
  --hidden-import uvicorn.lifespan.on `
  --collect-all google-genai `
  --copy-metadata google-genai `
  --hidden-import langfuse `
  api_server.py
```

### 옵션 설명

| 옵션 | 설명 |
|:---|:---|
| `--onefile` | 단일 `.exe` 파일로 패키징 |
| `--name auto-qa` | 출력 파일명 (`auto-qa.exe`) |
| `--add-data "frontend/dist;frontend/dist"` | React 빌드 파일 포함 (Windows 구분자 `;`) |
| `--add-data "templates;templates"` | 기본 템플릿 디렉토리 포함 |
| `--hidden-import uvicorn.*` | uvicorn 내부 모듈 명시적 포함 |
| `--collect-all google-genai` | google-genai 패키지 전체 포함 |
| `--copy-metadata google-genai` | google-genai 메타데이터 복사 (버전 체크에 필요) |
| `--hidden-import langfuse` | langfuse 모듈 포함 |

### 빌드 결과

```
auto-qa/
├── dist/
│   └── auto-qa.exe       # 배포용 단독 실행파일
├── build/                # 빌드 임시 파일 (삭제 가능)
└── auto-qa.spec          # PyInstaller 스펙 파일
```

### 실행파일 배포

`auto-qa.exe`와 함께 아래 파일을 같은 폴더에 배치합니다:

```
배포 폴더/
├── auto-qa.exe
├── .env                  # 환경 변수 설정
├── credentials.json      # GCP 인증 파일
├──apks/                  # 실행 할 game apk 목록
│   └── a.apk
│   └── b.apk
```

실행 후 브라우저에서 `http://localhost:8000` 접속:

```cmd
auto-qa.exe
```

### 빌드 트러블슈팅

#### `ModuleNotFoundError` 발생 시

누락된 모듈을 `--hidden-import`로 추가합니다:

```bash
pyinstaller ... --hidden-import 모듈명 api_server.py
```

#### UI가 표시되지 않는 경우

`frontend/dist`가 빌드에 포함되지 않았을 수 있습니다. `npm run build` 후 재빌드하세요.

#### 실행파일 크기를 줄이고 싶은 경우

`--onedir` 옵션으로 폴더 형태로 패키징하면 압축 오버헤드 없이 실행 속도도 빠릅니다:

```bash
pyinstaller --onedir --name auto-qa ...
# dist/auto-qa/ 폴더 생성 → 폴더 전체를 압축하여 배포
```
