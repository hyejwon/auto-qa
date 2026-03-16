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
8. [모듈 구성](#8-모듈-구성)
9. [트러블슈팅](#9-트러블슈팅)

---

## 1. 아키텍처

```
┌─────────────────────────────────────────────────┐
│                  NiceGUI (Web UI)                │
│               main_nicegui.py :7860              │
├─────────────────────────────────────────────────┤
│  PlannerNode          │  QAOrchestrator          │
│  (Gemini LLM)         │  (테스트 실행 엔진)       │
│  자연어 → YAML 변환    │  스텝 순차 실행           │
├───────────┬───────────┼──────────┬───────────────┤
│ VisionAgent│ElementCache│ADBController│UnityAPIClient│
│ (Gemini    │ (SQLite)   │ (ADB 명령)  │ (Unity SR    │
│  Vision)   │            │             │  치트 API)   │
└───────────┴───────────┴──────────┴───────────────┘
                                  │
                          ┌───────┴───────┐
                          │ Android Device │
                          │   (via ADB)    │
                          └───────────────┘
```

### 실행 흐름

1. **시나리오 입력** → 자연어 테스트 시나리오를 UI에 입력
2. **플랜 생성** → `PlannerNode`가 Gemini API로 YAML 테스트 플랜 생성
3. **템플릿 저장** → 생성된 플랜을 `templates/` 디렉토리에 YAML 파일로 저장
4. **템플릿 로드 & 편집** → UI에서 스텝 추가/삭제/순서변경/내용수정
5. **테스트 실행** → `QAOrchestrator`가 각 스텝을 순차적으로 실행
6. **결과 저장** → `test_results/`에 JSON 형태로 결과 저장, 화면 녹화 파일 자동 생성

---

## 2. 환경 설정

### 사전 요구사항

| 항목 | 요구 버전 |
|:---|:---|
| Python | 3.12+ |
| ADB | Android Platform Tools |
| GCP 프로젝트 | Vertex AI API 활성화 |

### .env 파일

프로젝트 루트에 `.env` 파일을 생성합니다:

```env
# GCP / Vertex AI
GOOGLE_APPLICATION_CREDENTIALS=./credentials.json

# (Docker 환경에서 ADB 호스트 연결 시)
ANDROID_ADB_SERVER_ADDRESS=host.docker.internal
ANDROID_ADB_SERVER_PORT=5037

# (선택) 특정 디바이스 지정
ADB_DEVICE=emulator-5554
```

### GCP 인증

`credentials.json` 파일을 프로젝트 루트에 배치하거나, `gcloud auth application-default login`으로 인증합니다.

### Python 의존성 설치

```bash
pip install -r requirements.txt
```

주요 의존성:
- `nicegui` — Web UI 프레임워크
- `google-genai`, `langchain-google-vertexai` — Gemini API
- `langgraph` — LLM 오케스트레이션
- `pydantic` — 데이터 모델
- `pillow` — 이미지 처리 (스크린샷 비교)
- `pyyaml` — 템플릿 파싱

---

## 3. 로컬 실행

### ADB 디바이스 연결 확인

```bash
adb devices
# List of devices attached
# emulator-5554   device
```

### 앱 실행

```bash
python main_nicegui.py
```

브라우저에서 `http://localhost:7860` 접속.

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

`docker-compose.yaml`에서 `host.docker.internal`을 통해 호스트 ADB에 접근합니다.

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

웹 UI는 4개 탭으로 구성됩니다.

### 5.1 APK 관리

- **APK 설치**: `apks/` 폴더에 APK 파일을 넣고, 드롭다운에서 선택 후 설치
- **앱 삭제**: 등록된 패키지명을 선택하여 삭제

### 5.2 템플릿 생성

1. **패키지명** 선택 (드롭다운)
2. **시나리오 입력**: 자연어로 테스트 시나리오 작성 (또는 샘플 시나리오 버튼 클릭)
3. **플랜 생성** 클릭 → Gemini가 YAML 테스트 플랜 생성
4. 생성된 YAML을 직접 편집 가능
5. **템플릿으로 저장** 클릭 → `templates/` 디렉토리에 저장

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

### 5.3 템플릿 실행

1. 드롭다운에서 **템플릿 선택** → **로드** 클릭
2. 로드된 스텝을 UI에서 편집:
   - **액션** 변경 (드롭다운)
   - **target** 수정 (텍스트 입력)
   - **설명** 수정 (텍스트 입력)
   - **⬆⬇** 순서 이동 또는 ≡ 드래그 앤 드롭
   - **✕** 스텝 삭제
   - **➕** 새 스텝 추가
3. **바로 실행** 클릭 → 실시간 로그 출력
4. 실행 완료 후 결과 요약 표시 (PASS/FAIL, 스텝별 결과)
5. **중단** 버튼으로 실행 중지 가능

### 5.4 녹화 영상

- 테스트 실행 시 자동으로 화면 녹화
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
  params:
    package: com.example.app
```

앱을 실행하고 화면 안정화까지 자동 대기합니다. 별도 `wait` 스텝 불필요.

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

재화 변동, PID 변경 등을 검증할 때 사용합니다. `save_as`로 저장한 후 `compare_with`로 비교하는 쌍으로 구성하세요.

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

`expect_visible` / `expect_hidden` 지정 시 뒤로가기 후 자동 검증합니다. 별도 `verify` 스텝 불필요.

### `home` — 홈 버튼

```yaml
- action: home
```

### `close_app` — 앱 종료

```yaml
- action: close_app
  params:
    package: com.example.app
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
```

Unity SR 치트 API를 호출하여 튜토리얼을 건너뜁니다. 패키지별로 `GAME_CHEAT_MAP`에 치트 정보가 등록되어 있어야 합니다.

---

## 8. 모듈 구성

| 파일 | 역할 |
|:---|:---|
| `main_nicegui.py` | NiceGUI 웹 UI (진입점) |
| `config.py` | 경로 및 Gemini API 설정 |
| `qa_orchestrator.py` | 테스트 실행 엔진 (스텝 순차 실행, 화면 안정화, 검증) |
| `planner_node.py` | 자연어 → YAML 테스트 플랜 변환 (Gemini API) |
| `vision_agent.py` | 스크린샷 기반 UI 요소 탐지 (Gemini Vision) |
| `adb_controller.py` | ADB 명령 래퍼 (탭, 스와이프, 스크린샷, 녹화) |
| `unity_api_client.py` | Unity 인게임 치트 API 클라이언트 |
| `element_cache.py` | UI 요소 좌표 캐시 (SQLite) |
| `test_manager.py` | 테스트케이스/결과 모델 및 YAML 로드/저장 |

### 디렉토리 구조

```
auto-qa/
├── main_nicegui.py          # 진입점
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
├── templates/               # 테스트 템플릿 YAML
├── testcases/               # 자동 생성된 테스트케이스
├── test_results/            # 실행 결과 JSON
├── screenshots/             # 스텝별 스크린샷
├── screenshots_debug/       # Vision 디버그 이미지
├── recordings/              # 화면 녹화 영상
└── apks/                    # 설치용 APK 파일
```

---

## 9. 트러블슈팅

### ADB 연결 실패

```
ConnectionError: No device connected
```

- `adb devices`로 디바이스 연결 확인
- Docker 환경: 호스트에서 `adb start-server` 실행 확인
- `.env`에 `ANDROID_ADB_SERVER_ADDRESS=host.docker.internal` 설정 확인

### Gemini API 인증 오류

- `credentials.json` 파일 존재 여부 확인
- `GOOGLE_APPLICATION_CREDENTIALS` 환경 변수 확인
- GCP 프로젝트에서 Vertex AI API 활성화 확인

### 플랜 생성 시 Connection Error 팝업

Gemini API 호출이 동기 블로킹으로 실행되면 NiceGUI의 이벤트 루프가 차단되어 WebSocket 연결이 끊길 수 있습니다. `await run.io_bound()`로 감싸서 별도 스레드에서 실행해야 합니다.

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
