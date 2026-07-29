# Auto QA 사용 및 개발 가이드

Auto QA는 Android 게임을 ADB로 제어하고 Gemini Vision으로 화면을 판단하는 모바일 QA 자동화 도구입니다. 웹 UI에서 게임과 템플릿을 선택하고, 스텝을 편집·조합해 실행한 뒤 판정 스크린샷과 CSV 리포트를 확인할 수 있습니다.

## 1. 현재 구현 범위

### 디바이스와 앱 관리

- USB 및 무선 ADB 디바이스 목록/선택
- IP로 무선 ADB 연결·해제하고 등록된 주소 자동 재연결
- 같은 디바이스에서 동시 테스트를 막는 실행 락
- App Tester 프로젝트·빌드 조회, 선택 버전 설치, 설치 상태 확인
- 선택한 게임 앱 삭제
- `apks/` 폴더의 APK 목록 조회·설치
- 테스트 시작 전 화면 깨우기 및 잠금 상태 확인
- `.env` 설정 시 숫자 PIN 자동 해제 (`ADB_UNLOCK_PIN`)

### 템플릿과 스텝 편집

- YAML 템플릿 목록 조회·불러오기·저장·삭제
- 여러 템플릿을 하나의 테스트로 조합
- 템플릿 블록과 개별 스텝을 드래그해 순서 변경
- 템플릿 블록 전체 삭제 및 개별 스텝 삭제
- action, target, description, timeout, retry, params 편집
- `wait`, `scroll`, timeout, retry 필드의 용도와 단위를 UI에 표시
- `{{name}}` 파라미터 치환과 label·description·example 표시
- 앱 제어 스텝의 `{{package}}`는 앞에서 선택한 게임 패키지로 자동 적용
- 자연어 시나리오를 기존 템플릿 기반 스텝으로 생성
- 디펜스 인게임은 제한된 의미 DSL과 게임별 Profile로 기존 스텝을 결정론적으로 생성

### 실행과 리포트

- WebSocket 실시간 로그와 라이브 화면 미리보기
- 실행 중단
- 스텝별 PASS/FAIL/SKIPPED, 실패 이유, 통과 근거, Vision 신뢰도 표시
- 조합한 템플릿 구간별 스텝 결과 그룹화
- `find_and_tap`, `read_text`, `dismiss_popups` 판정 스크린샷 저장
- 클릭 전, 후조건 판정, 팝업 감지, 최종 화면 판정 시점 구분
- 스텝·템플릿·판정 근거를 포함한 UTF-8 BOM CSV 리포트 저장
- 실행 결과 JSON과 선택적 ADB 화면 녹화

> Adaptive QA 자동 보정 루프는 현재 API와 UI에서 비활성화된 상태입니다.

## 2. 아키텍처

```text
React + TypeScript UI (frontend/src)
  -> REST / WebSocket
FastAPI (api_server.py:8000)
  -> QAOrchestrator       : 스텝 실행, 후조건 검증, 증거 저장
  -> ADBController        : 앱/화면/입력/APK/녹화 제어
  -> GeminiVisionAgent    : UI 요소 탐지, 화면 검증, OCR
  -> PlannerNode          : 자연어 -> 기존 스텝 또는 Defense 의미 Plan
  -> DefensePlanCompiler  : 의미 Plan + Game Profile -> 기존 스텝
  -> UnityAPIClient       : Unity 치트/프로퍼티 API (`skip_tutorial`, `call_cheat`, `set_property`, `check_property`)
  -> SRDebuggerController : 숨겨진 제스처로 SR Debugger 진입
  -> CSVReporter          : 실행/판정 근거 CSV
  -> Android device via ADB
```

프론트엔드 수정 시 `npm run build`로 `frontend/dist` 정적 파일을 갱신해야 `api_server.py`가 최신 UI를 제공합니다.

## 3. 실행 준비

### 필수 환경

| 항목 | 내용 |
|:---|:---|
| Python | 3.12 권장 |
| ADB | Android Platform Tools, PATH 등록 또는 `ADB_PATH` 설정 |
| Android | USB 또는 무선 디버깅 활성화 |
| LLM | 사내 LLM Gateway URL과 토큰 |
| Node.js | 프론트엔드를 수정·빌드할 때만 필요 |

### `.env`

`.env.example`을 기준으로 프로젝트 루트에 `.env`를 생성합니다.

```env
LLM_GATEWAY_URL=https://llm-gateway.111percent.net/llm/google
LLM_GATEWAY_TOKEN=

# 로컬 단일 디바이스 모드에서만 권장
ADB_DEVICE=
ADB_PATH=

# 숫자 PIN만 자동 해제 가능
ADB_UNLOCK_PIN=

# 비워두면 디바이스별 ADB 로컬 포트를 자동 할당
UNITY_API_URL=

GOOGLE_TEST_ID=
GOOGLE_TEST_PASSWORD=

# 역할별 모델 라우팅 (생략 시 config.py 기본값)
GEMINI_INTENT_MODEL=
GEMINI_PLANNER_MODEL=
GEMINI_VISION_MODEL=
GEMINI_VISION_LITE_MODEL=

# legacy Planner에 넣을 관련 템플릿 상한 (기본 5)
PLANNER_TEMPLATE_LIMIT=5
```

다중 디바이스 테스트 서버에서는 `ADB_DEVICE`와 `UNITY_API_URL`을 비워 두고 UI에서 디바이스를 선택하는 구성을 권장합니다.

## 4. 실행 방법

### Windows

```bat
run_app.bat
```

### macOS / Linux

```bash
chmod +x run_app.sh
./run_app.sh
```

두 스크립트는 `.venv`를 생성하고 `requirements-app.txt`를 설치한 뒤 `api_server.py`를 실행합니다. 기본 접속 주소는 `http://localhost:8000`입니다.

수동 실행:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements-app.txt
python api_server.py
```

프론트엔드 개발:

```bash
cd frontend
npm install
npm run dev
```

Vite 개발 서버는 `http://localhost:3001`이며 `/api`, `/ws`, `/debug`를 `http://localhost:8000`으로 프록시합니다.

프론트엔드 프로덕션 빌드:

```bash
cd frontend
npm run build
```

## 5. UI 사용 흐름

### 5.1 디바이스 선택

1. 상단 `디바이스 선택`에서 온라인 디바이스를 선택합니다.
2. 무선 디바이스는 `IP로 연결`에 `IP:PORT`를 입력합니다.
3. 온라인 디바이스가 하나면 자동 선택됩니다.

### 5.2 게임과 빌드 선택

1. App Tester 프로젝트 목록에서 게임을 선택합니다.
2. `설치됨` / `미설치` 상태와 패키지명을 확인합니다.
3. 필요하면 버전 목록에서 빌드를 골라 설치합니다.
4. 삭제는 선택한 프로젝트 패키지에만 적용됩니다.
5. 테스트할 패키지가 선택되어야 다음 단계로 진행할 수 있습니다.

### 5.3 테스트 구성

`기존 선택` 모드:

1. 템플릿을 선택하고 `불러오기`를 누릅니다.
2. 다른 템플릿을 추가로 불러오면 뒤에 추가됩니다.
3. 템플릿 블록 또는 개별 스텝을 드래그해 필요한 위치로 옮깁니다.
4. 템플릿 블록의 `X`는 소속 스텝까지 전체 삭제합니다.
5. 개별 스텝의 휴지통 버튼은 해당 스텝만 삭제합니다.

`새로 만들기` 모드:

1. 자연어로 시나리오를 작성합니다.
2. Planner 방식에서 `기존 범용` 또는 `디펜스 인게임`을 선택합니다.
3. `스텝 생성`을 누르면 기존 방식은 관련 템플릿을 참조하고, 디펜스 방식은 의미 Plan을 Game Profile로 확장합니다.
4. 생성된 스텝을 실행 전에 반드시 검토합니다.

`디펜스 인게임`은 현재 이지스 디펜스 Profile이 있는 패키지에만 사용합니다.
지원하지 않는 요청이나 패키지는 임의의 기존 스텝으로 우회하지 않고 오류를 표시합니다.
자세한 구조와 확장 방법은 `docs/defense-planner.md`를 참고합니다.

공통:

- 스텝별 action·target·description·timeout·retry를 수정할 수 있습니다.
- action별 주요 params는 기본 필드로 표시되고, 전체 params는 `params JSON 편집`에서 수정합니다.
- `실행`은 현재 구성을 즉시 실행합니다.
- `저장`은 현재 구성을 `templates/` YAML로 저장합니다.

### 5.4 결과 확인

- 요약: 전체 PASS/FAIL, 통과/실행 스텝 수, 오류 메시지
- 스텝 결과: 템플릿 구간별 PASS/FAIL/SKIPPED와 근거
- 판정 스크린샷: step, 판정 시점, 시간, 타겟, PASS/FAIL
- CSV 저장: 테스트·템플릿·스텝·판정 근거를 한 파일로 내보냄
- `다시 실행`: 현재 구성으로 복귀
- `처음으로`: 게임 선택 단계로 복귀

## 6. 템플릿 YAML

### 기본 구조

```yaml
title: 앱 종료 후 재실행
description: 실행 중인 앱을 종료한 뒤 다시 실행한다.
package: ''
parameters:
  - name: package
    label: 앱 패키지명
    description: 종료한 뒤 다시 실행할 Android 앱의 패키지명
    example: com.percent.aos.cooptd
    default: ''
steps:
  - action: close_app
    target: '{{package}}'
    description: 앱을 완전히 종료
    timeout: 10
    retry: 2
  - action: launch_app
    target: '{{package}}'
    description: 종료한 앱을 다시 실행
    timeout: 20
    retry: 2
expected_results:
  - 지정한 앱이 종료된 뒤 정상적으로 다시 실행되어야 한다.
```

### 템플릿 필드

| 필드 | 필수 | 설명 |
|:---|:---:|:---|
| `title` | O | UI에 표시할 템플릿명 |
| `description` | - | 템플릿 설명 |
| `package` | - | 기본 패키지명. UI에서 선택한 게임을 우선 사용 |
| `parameters` | - | `{{name}}` 입력값의 표시 정보와 기본값 |
| `steps` | O | 순서대로 실행할 스텝 |
| `preconditions` | - | `google_account:<email>` 형식의 사전 조건 |
| `expected_results` | - | 기대 결과 문서화 |
| `source_scenario` | - | 자연어 원본 |

`parameters` 항목은 `name`, `label`, `description`, `placeholder`, `example`, `default`를 지원합니다. 템플릿을 UI에서 다시 저장해도 이 정보가 유지됩니다.

### 공통 스텝 필드

| 필드 | 기본값 | 설명 |
|:---|:---:|:---|
| `action` | - | 실행할 공식 step 이름 |
| `target` | `null` | UI 요소, 패키지, APK 파일명 등 action의 주 대상 |
| `description` | `''` | 실행 로그와 리포트에 표시할 설명 |
| `params` | `{}` | action별 추가 설정 |
| `timeout` | `30` | 스텝 제한 시간(초). 내부 루프가 있는 action에서 주로 사용 |
| `retry` | `3` | 총 시도 횟수. `back`, `dismiss_popups`는 내부 재시도 규칙을 사용 |

## 7. 공식 Step 레퍼런스

UI와 신규 템플릿에서 사용하는 공식 step 이름은 아래 20개입니다.

| Step | target | 주요 params | 기능 |
|:---|:---|:---|:---|
| `find_and_tap` | 필수 | 후조건, 스크롤 탐색, 선택 스텝 | Vision으로 요소를 찾고 클릭 후 검증 |
| `verify` | 필수 | - | 현재 화면의 요소 노출 검증 |
| `read_text` | 필수 | `save_as`, 비교 조건 | OCR 값 저장·비교 |
| `scroll` | - | `direction`, `times` | 선언적 스크롤 |
| `wait` | - | `seconds`, `until_*` | 지정 시간 또는 앱 상태 조건 대기 |
| `back` | 선택 | `expect_visible`, `expect_hidden` | Android 뒤로가기와 복귀 검증 |
| `dismiss_popups` | 선택 | `stop_when_visible`, `rules` | 개수가 변하는 허용 팝업 반복 처리 |
| `home` | - | - | Android HOME 키 |
| `launch_app` | 패키지 | `package` | 앱 실행 후 화면 안정화 대기 |
| `close_app` | 패키지 | `package` | `am force-stop`으로 앱 종료 |
| `install_app` | APK 파일명 | `apk` | `apks/` APK 설치 |
| `uninstall_app` | 패키지 | `package` | 앱 삭제 |
| `skip_tutorial` | 패키지 | `package` | 패키지별 Unity 치트로 튜토리얼 스킵 |
| `call_cheat` | 치트 id | `id`, `args`, `wait_seconds`, `not_found_ok` | v2 치트 API 실행 |
| `set_property` | 프로퍼티 id | `id`, `value` | v2 프로퍼티 쓰기 |
| `repeat_until` | - | `steps`, `until_visible`, `until_scene`, `max_iterations` | 조건 만족까지 스텝 묶음 반복 |
| `check_property` | 프로퍼티 id | `id`, `expect_value`, `save_as`, `compare_with`, `expect_*` | v2 프로퍼티 읽기/검증 |
| `enter_sr_debugger` | 패키지 | `strategies` 등 | 숨겨진 제스처로 SR Debugger 진입 |
| `swipe` | - | `x1`, `y1`, `x2`, `y2` | 좌표 기반 스와이프 |
| `input_text` | - | `text` | ADB로 문자열 입력 |

`tutorial_pass`는 기존 YAML 하위 호환을 위해 백엔드에만 남아 있는 레거시 alias입니다. UI, 플래너, 신규 템플릿에서는 `skip_tutorial`만 사용합니다.

### 7.1 `find_and_tap`

```yaml
- action: find_and_tap
  target: 정말로 건너뛰시겠습니까 팝업의 예 버튼
  description: 튀토리얼 건너뛰기 확인
  timeout: 15
  retry: 2
  params:
    expect_hidden: 정말로 건너뛰시겠습니까 팝업
    wait_seconds: 1
```

주요 params:

| params | 설명 |
|:---|:---|
| `expect_visible` | 탭 후 보여야 하는 target 문자열 또는 목록 |
| `expect_hidden` | 탭 후 사라져야 하는 target 문자열 또는 목록 |
| `wait_seconds` | 탭 후 후조건 검증 전 대기 초 |
| `optional` | `true`면 대상이 없을 때 실패가 아닌 SKIPPED |
| `no_recovery` | `true`면 대상 미발견 시 이벤트 팝업 자동 복구를 시도하지 않음 |
| `scroll_search` | `true`면 대상을 찾을 때까지 페이지 스크롤 |
| `max_scrolls` | 스크롤 탐색 최대 횟수, 기본 5 |
| `scroll_direction` | `down` 또는 `up` |
| `then_tap` | 첫 탭 후 이어서 탭할 두 번째 target |
| `tap_point` | `center`면 target 노출만 확인하고 실제 탭은 화면 중앙에 실행 |
| `unity_name` | Profile에서 확인한 Unity component 이름. exact unique match일 때만 사용 |
| `cache_scope` | 게임 상태별 안정적인 좌표 캐시 범위 |
| `cache_safe` | 후조건과 `cache_scope`가 있을 때만 캐시 읽기 허용 |

탭 후 판정이 필요한 핵심 스텝은 `expect_visible` 또는 `expect_hidden`을 정의합니다. 판매·결제·계정 삭제처럼 후속 화면이 느린 흐름은 `wait_seconds`와 `timeout`을 함께 조정합니다.
정확 Unity 또는 캐시 좌표로 이미 탭했다면 후조건 실패 뒤 Vision으로 다시 누르지 않고 검증만 재시도합니다.

### 7.2 `verify`

```yaml
- action: verify
  target: 설정 팝업
  timeout: 10
  retry: 2
```

실행 시점의 새 스크린샷에서 target을 Vision으로 찾습니다.

**`params.scene` — Vision 없이 씬 판정**

```yaml
- action: verify
  target: 아웃게임 로비 화면        # 사람이 읽는 설명으로만 쓰임
  params:
    scene: outgame                 # "ingame|outgame" 처럼 |로 여러 개 허용
```

`params.scene`이 있으면 스크린샷을 판독하지 않고 앱 내부 v2 치트 목록의 id 접두사로 현재 씬을 판정합니다. 치트/프로퍼티는 씬 단위로 등록되므로 목록에 잡히는 접두사가 곧 현재 씬입니다(이지스 디펜스: 전투 화면 `ingame`, 로비 `outgame`).

- Vision 호출 0회 — 빠르고 비용이 없으며 화면 해석 오차가 없습니다
- 씬이 아직 안 올라왔으면(스플래시·로딩) 접두사가 비어 실패하므로 로딩 완료 확인도 겸합니다
- 접두사 이름은 게임마다 다르므로 템플릿이 지정합니다. 카탈로그(`GET /api/unity/catalog`)에서 확인할 수 있습니다
- 팝업이 떠 있는지까지는 알 수 없습니다 — 팝업 정리는 별도 스텝으로 처리하세요

### 7.3 `read_text`

```yaml
- action: read_text
  target: 현재 다이아 수량
  params:
    save_as: diamond_before

- action: read_text
  target: 현재 다이아 수량
  params:
    compare_with: diamond_before
    expect_decrease: true
```

| params | 설명 |
|:---|:---|
| `save_as` | OCR 결과를 테스트 컨텍스트에 저장할 이름 |
| `compare_with` | 이전에 `save_as`로 저장한 이름 |
| `expect_changed` | `true`: 변경, `false`: 유지를 기대 |
| `expect_increase` | 숫자 값 증가를 기대 |
| `expect_decrease` | 숫자 값 감소를 기대 |
| `expect_delta` | 정확한 증감량을 기대 (예: `-5`) |
| `expect_delta_from` | 앞서 `save_as`로 저장한 값을 그대로 기대 증감량으로 사용 |
| `delta_sign` | `expect_delta_from`의 부호. 기본 `1`(증가), 소비 검증이면 `-1` |
| `delta_tolerance` | 증감량 비교 허용 오차 (기본 0) |

`expect_delta_from`은 기대값을 템플릿에 하드코딩할 수 없는 재화 검증용입니다. 화면에서 읽은 값(보상 수량, 표기된 소모 비용 등)을 그대로 기대 증감량으로 써서, 매 실행마다 값이 달라져도 정확한 증감을 자동 판정합니다.

```yaml
# 결과 화면의 보상 수치를 읽어두고
- action: read_text
  target: 결과 팝업의 보상 골드 수량
  params:
    save_as: reward_gold

# 아웃게임 골드가 "정확히 그만큼" 늘었는지 검증
- action: read_text
  target: 상단 골드 수량
  params:
    compare_with: gold_before
    expect_delta_from: reward_gold

# 소비 검증은 delta_sign: -1
- action: read_text
  target: 크레딧 수량
  params:
    compare_with: credit_before
    expect_delta_from: aegis_cost
    delta_sign: -1
```

`read_screen`의 각 item과 `check_property`에서도 동일하게 동작합니다.

### 7.4 `scroll`

```yaml
- action: scroll
  params:
    direction: down
    times: 2
```

- `direction`: `down`, `up`, `top`, `bottom`
- `times`: `down`/`up` 반복 횟수
- `top`/`bottom`은 화면 변화가 없을 때까지 최대 10회 스크롤합니다.

### 7.5 `wait`

```yaml
- action: wait
  params:
    seconds: 2.5
```

`seconds`는 대기 시간(초)입니다. UI에서 0.5초 단위로 편집할 수 있습니다.

고정 시간 대신 앱 상태를 기다릴 수도 있습니다.

```yaml
- action: wait
  timeout: 120
  params:
    until_scene: ingame
    poll_interval_seconds: 1
```

| params | 설명 |
|:---|:---|
| `until_scene` | Unity 치트 id 접두사 기반 씬. `ingame\|outgame`처럼 복수 허용 |
| `until_property` | `{id: 프로퍼티ID, equals: 기대값}` |
| `until_unity_button` | 정확히 하나 존재해야 하는 Unity component 이름 또는 이름 목록 |
| `until_unity_button_hidden` | 사라져야 하는 Unity 이름. API 정상 확인용 `until_unity_button`과 함께 사용 |
| `until_visible` | Vision으로 보여야 하는 target 또는 목록 |
| `until_hidden` | Vision으로 사라져야 하는 target 또는 목록 |
| `poll_interval_seconds` | 조건 재확인 간격, 최소 0.1초 |
| `consecutive_matches` | 연속 충족 횟수, 기본 1·상한 5. 씬 전환의 순간값 방지용 |

여러 조건을 같이 쓰면 모두 만족해야 하며 전체 제한 시간은 `timeout`입니다.
조건 대기와 고정 대기는 실행 중단 요청에 즉시 반응합니다.

### 7.6 `back`

```yaml
- action: back
  description: 이전 화면으로 복귀
  params:
    expect_visible: 로비 화면
    expect_hidden: VIP 상점 팝업
    wait_seconds: 1
```

후조건이 있으면 뒤로가기와 검증을 하나의 원자적 스텝으로 처리하고, 첫 검증 실패 시 뒤로가기를 한 번 더 시도합니다.

### 7.7 `dismiss_popups`

```yaml
- action: dismiss_popups
  description: 허용된 팝업을 닫고 로비 진입
  timeout: 120
  retry: 1
  params:
    stop_when_visible: 우측 상단 햄버거 메뉴 버튼
    max_count: 4
    timeout_seconds: 120
    initial_wait_seconds: 1
    quiet_seconds: 0
    poll_interval_seconds: 0.5
    rules:
      - target: VIP 상점 좌측 하단 뒤로가기 버튼
        action: tap
      - target: 새로운 컨텐츠가 해금되었어요 팝업
        action: tap_center
```

- `stop_when_visible`: 팝업 처리가 끝난 최종 화면의 고유 요소. 문자열 또는 목록
- `rules`: 허용할 팝업과 처리 action 목록
- rule action: `back`, `tap`, `tap_center`
- `max_count`: 0~20, 기본 4
- `timeout_seconds`: 반복 전체 제한 시간
- `quiet_seconds`: 최종 화면을 연속으로 확인할 시간, 기본 1.5
- `initial_wait_seconds`: 첫 팝업 감지 전 대기
- `poll_interval_seconds`: 재탐색 간격, 최소 0.1

허용 목록에 없는 팝업은 임의로 누르지 않습니다. 계정 삭제 확인처럼 단일·필수 확인은 `find_and_tap + expect_visible/hidden`으로 작성하고, 개수나 순서가 바뀐 이벤트·보상·공지 팝업에 `dismiss_popups`을 사용합니다.

### 7.8 `home`

```yaml
- action: home
```

Android HOME 키를 누릅니다.

### 7.9 `launch_app`, `close_app`

```yaml
- action: close_app
  target: com.percent.aos.cooptd
- action: launch_app
  target: com.percent.aos.cooptd
```

UI에서 템플릿을 불러올 때 비어 있거나 `{{package}}`인 target은 앞 단계에서 선택한 게임 패키지로 채워집니다. API는 target을 `params.package`로 정규화합니다. `launch_app`은 앱 실행 후 화면 안정화를 대기하므로 고정 `wait`를 즉시 뒤에 추가할 필요는 없습니다.

### 7.10 `install_app`, `uninstall_app`

```yaml
- action: install_app
  target: game-release.apk
- action: uninstall_app
  target: com.example.game
```

`install_app`의 target 또는 `params.apk`는 `apks/` 기준 파일명입니다. `uninstall_app`은 target, `params.package`, 현재 테스트 패키지 순서로 패키지를 결정합니다.

### 7.11 `skip_tutorial`

```yaml
- action: skip_tutorial
  target: com.percent.aos.cooptd
  timeout: 20
  retry: 2
```

치트는 `unity_api_client.py`의 `GAME_CHEAT_MAP`에 패키지별로 등록합니다. 빌드에 따라 두 가지 형태를 지원합니다.

| 빌드 | 매핑 | 호출 엔드포인트 |
| --- | --- | --- |
| v2 치트 빌드 | `{"id": "<치트 id>", "args": {...}}` | `POST /api/v2/cheats/execute` |
| 레거시 SR 빌드 | `{"category": "...", "name": "..."}` | `POST/GET /api/sr/call` |

레거시 호출이 실패하거나 매핑이 없으면 `GET /api/v2/cheats` 목록에서 튜토리얼 스킵 치트를 자동으로 찾아 실행합니다(`find_cheat_id_v2`). 명시적 `UNITY_API_URL`이 없으면 ADB가 디바이스의 37772 포트를 고유한 로컬 포트로 포워딩합니다.

예시 — 이지스 디펜스(`com.supermagic.aos.aegisdefense`)는 `/api/sr/call`이 404라서 v2 치트 `ingame.tutorial.skip`("인게임/스테이지 > 인게임 튜토리얼 스킵")을 사용합니다.

```yaml
- action: skip_tutorial
  target: com.supermagic.aos.aegisdefense
  timeout: 20
  retry: 2
```

v2 치트/프로퍼티 API 상세는 `sr_api.md`를 참고합니다. `/api/sr/options`는 Android 앱 내부 SR 서버 엔드포인트이며 Auto QA FastAPI 엔드포인트가 아닙니다. Windows PowerShell에서 수동 확인할 때는 `curl` alias 대신 `curl.exe`를 사용합니다.

### 7.12 `call_cheat` / `set_property` / `check_property`

`skip_tutorial`처럼 패키지별로 미리 등록된 치트만이 아니라, v2 치트/프로퍼티를 스텝에서 직접 호출합니다. UI로는 만들 수 없는 사전 상태(목표 웨이브, 몬스터 소환, 재화 지급, 무적 등)를 세팅하거나 화면에 안 보이는 내부 값으로 판정할 때 씁니다.

```yaml
- action: call_cheat
  target: ingame.stage.go_to_wave      # params.id로 써도 됩니다
  params:
    args:
      targetWave: 8
    wait_seconds: 3

- action: set_property
  target: ingame.player.invincible_state
  params:
    value: true

- action: check_property
  target: ingame.player.invincible_state
  params:
    label: 플레이어 무적          # 리포트 표시 이름 (생략 시 프로퍼티 id)
    expect_value: true
```

- `call_cheat`의 `not_found_ok: true`는 현재 씬에 그 치트가 없을 때(`not_found`)도 통과 처리합니다.
- `check_property`는 `read_text`와 동일하게 `save_as` / `compare_with` / `expect_delta` / `expect_increase` / `expect_changed`를 지원해 리포트 재화 비교 표에 기록됩니다. `choiceable-float`처럼 `Value`가 객체로 오는 타입(`debug.time_scale`)은 안쪽 `value`로 판정합니다.
- 쓰기 불가 프로퍼티에 `set_property`를 하면 `read_only`로 실패합니다.

⚠️ **치트와 프로퍼티는 씬 단위로 등록됩니다.** `ingame.*`는 전투 화면, `outgame.*`는 로비에서만 목록에 잡히므로 해당 씬에 진입한 뒤 호출해야 합니다(2026-07-27 이지스 디펜스 실기기 확인). 예: 로비에서 `ingame.wave.skip`을 부르면 `not_found`입니다.

⚠️ 실패해야 정상인 스텝(잘못된 인자 거부 등)은 표현할 수 없습니다 — `optional: true`는 "건너뜀"으로 처리되어 서버 다운과 구분되지 않습니다.

### 7.13 `repeat_until`

조건이 만족될 때까지 하위 스텝 묶음을 반복합니다. 웨이브 디펜스처럼 "준비 → 배치 → 진행"을 N번 되풀이해야 하는 흐름을 스텝 수십 개로 펼쳐 쓰지 않기 위한 액션이며, 웨이브 수가 다른 스테이지에도 그대로 재사용됩니다.

```yaml
- action: repeat_until
  description: 준비 단계마다 이지스를 소환하고 웨이브를 진행 — 보스 웨이브까지 반복
  timeout: 900
  params:
    until_visible: 하단 버튼이 빨간색이고 초대형 비행체 발견 문구가 보이는 보스 웨이브 준비 화면
    max_iterations: 25
    timeout_seconds: 780
    steps:
      - action: find_and_tap
        target: 화면 하단 가운데 이지스 소환 버튼
        params: { wait_seconds: 1 }
      - action: find_and_tap
        target: 화면 최하단 웨이브 시작 버튼
        params: { wait_seconds: 9 }
```

| params | 설명 |
|:---|:---|
| `steps` | 반복할 스텝 목록 (일반 스텝과 같은 스키마) |
| `until_visible` | 이 대상이 보이면 종료 (Vision) |
| `until_hidden` | 이 대상이 사라지면 종료 (Vision) |
| `until_scene` | 이 씬 접두사가 되면 종료 (치트 목록 기반, Vision 호출 없음) |
| `max_iterations` | 최대 반복 횟수 (기본 20, 상한 200) |
| `timeout_seconds` | 전체 제한 시간 (기본 `step.timeout`) |
| `check_every` | N회마다 조건 확인 (기본 1). Vision 조건일 때 호출 절약용 |
| `strict` | `true`면 하위 스텝 실패 즉시 중단 (기본 `false`) |

`strict` 기본값이 `false`인 이유는 반복 루프에서 하위 스텝 실패가 정상인 경우가 많기 때문입니다 — 크레딧이 모자라 소환이 안 되거나, 전투 중이라 시작 버튼이 없는 회차가 그렇습니다. 하위 스텝 결과는 리포트에 개별 행으로 남지 않고 `repeat_until` 한 줄로 요약됩니다.

`until_*` 조건 중 하나는 반드시 있어야 하며, 조건은 **매 반복 시작 전에** 먼저 확인합니다(이미 도달한 상태면 0회 반복으로 통과).

### 7.14 `enter_sr_debugger`

```yaml
- action: enter_sr_debugger
  target: com.percent.aos.cooptd
  params:
    verify_target: SRDebugger
    max_attempts: 2
    strategies:
      - name: fixed_30_30_fast_double_tap
        coordinate_space: unity_pixels
        x: 30
        y: 30
        count: 2
        interval_sec: 0.03
        post_wait_sec: 1
```

`strategies`를 비우면 기본 숨겨진 제스처 후보를 순서대로 시도합니다. `coordinate_space` 값은 `unity_pixels`, `unity_ratio`, 화면 ADB 좌표계를 지원합니다.

### 7.15 `swipe`

```yaml
- action: swipe
  params:
    x1: 360
    y1: 900
    x2: 360
    y2: 300
```

현재 실행 엔진은 `x1`, `y1`, `x2`, `y2`를 사용합니다. 목록 스크롤은 좌표 대신 `scroll`을 권장합니다.

### 7.16 `input_text`

```yaml
- action: input_text
  params:
    text: hello world
```

`params.text`를 ADB `input text`로 입력합니다. 입력 전에 필드 포커스가 없으면 `find_and_tap`으로 입력창을 먼저 누릅니다.

## 8. 현재 기본 템플릿

`templates/`에 등록된 템플릿:

- `google_login.yaml`: Google 로그인과 선택적 약관 팝업
- `guest_login.yaml`: Guest 로그인 확인 흐름
- `setting.yaml`: 햄버거 메뉴에서 설정 팝업 진입
- `terms_privacy_popup.yaml`: 약관/개인정보 팝업 처리
- `계정_삭제.yaml`: 계정 삭제 확인 후 로그인 화면 복귀 검증
- `앱_종료_재실행.yaml`: 선택한 게임을 종료한 뒤 재실행
- `협타디_튜토리얼_스킵_및_컨텐츠_해금_흐름_테스트.yaml`: 튜토리얼 치트, 재실행, 건너뛰기, 변동 팝업 처리
- `뉴비패키지_구매_검증.yaml`: 뉴비 패키지 구매 전후 재화 검증
- `다이아_4000_구매_검증.yaml`: 다이아 4000 상품 구매 검증
- `다이아_구매_구매인증_항상요구.yaml`: Google Play 구매 인증 `항상 요구` 흐름
- `다이아_구매_구매인증_요구안함.yaml`: Google Play 구매 인증 `요구 안함` 흐름
- `결제테스트_게임내재화_테스트.yaml`: 게임 내 재화 구매 검증

템플릿 파일명은 UI 선택 값이고, YAML의 `title`은 실행 및 리포트 표시명입니다.

## 9. 실행·판정 규칙

1. 테스트 시작 전 화면을 깨우고 잠금 상태를 확인합니다.
2. 잠금 화면을 해제하지 못하면 테스트 화면으로 오판하지 않고 환경 오류로 중단합니다.
3. OCR이 필요한 `read_text`, `read_items`, `read_screen`만 스텝 시작 스크린샷을 저장합니다.
4. `find_and_tap`은 exact Unity, 명시적 안전 캐시, 고정 shortcut, Vision 순서로 target을 찾습니다.
5. 탭 후 `expect_visible`/`expect_hidden`이 있으면 실행 화면을 새로 캡처해 최대 3회 검증합니다.
6. 탭은 성공했지만 후조건이 실패한 경우 같은 target을 다시 누르지 않고 검증만 재시도합니다.
7. `optional: true`인 스텝의 target이 없으면 SKIPPED로 계속 진행합니다.
8. 필수 스텝이 실패하면 해당 스텝의 실패 이유를 저장하고 실행을 종료합니다.

## 10. 결과 파일

| 경로 | 내용 |
|:---|:---|
| `test_results/` | 실행 결과 JSON |
| `screenshots/` | 스텝 실행·라이브 스크린샷 |
| `screenshots_debug/taps/` | 클릭·OCR·팝업 판정 증거 이미지 |
| `screenshots_debug/find_and_tap_debug.jsonl` | 탭·OCR 판정 메타데이터 |
| `screenshots_debug/interrupt_debug.jsonl` | 팝업 감지·최종 화면 판정 메타데이터 |
| `reports/` | CSV 리포트 |
| `recordings/` | `record: true`로 실행한 ADB 화면 녹화 |
| `templates/` | YAML 템플릿 |
| `pipelines/` | 저장된 파이프라인 JSON |

CSV는 실행 식별자·제목·상태·시간·오류, 템플릿명, 스텝 번호·action·description·target·상태·근거·신뢰도, 판정 순번·시점·시간·타겟·결과·근거·이미지를 저장합니다.

## 11. 주요 API

전체 스키마는 서버 실행 후 `http://localhost:8000/docs`에서 확인합니다.

| 영역 | 주요 API |
|:---|:---|
| 디바이스 | `GET /api/devices`, `POST /api/devices/connect`, `POST /api/devices/disconnect`, `GET /api/preflight` |
| App Tester | `GET /api/apptester/apps`, `GET /api/apptester/builds`, `POST /api/apptester/install` |
| APK/앱 | `GET /api/apks`, `POST /api/apk/install`, `GET /api/packages`, `POST /api/app/uninstall` |
| 템플릿 | `GET/POST /api/templates`, `GET/DELETE /api/templates/{name}` |
| 자연어 생성 | `POST /api/plan/generate` |
| 테스트 | `POST /api/test/run`, `POST /api/test/stop`, `WS /ws/logs/{session_id}` |
| 판정 근거 | `GET /api/debug/taps`, `GET /api/screen/latest` |
| CSV | `POST /api/reports/csv` |
| 공유 리포트 | `POST /api/reports/share`, `GET /api/reports/share/{report_id}`, `/report/{report_id}` |
| SR | `POST /api/unity/tutorial-pass`, `POST /api/sr-debugger/enter` |
| 파이프라인 | `GET/POST /api/pipelines`, `POST /api/pipeline/run` |
| 평가 | `/api/eval/cases`, `/api/eval/runs`, `/api/eval/runs/{run_id}/report` |
| 녹화 | `GET /api/recordings` |
| 상태 | `GET /health` |

리포트 화면의 **리포트 공유** 버튼은 실행 당시 결과와 판정 근거를
`reports/shared/`에 스냅샷으로 저장하고 `/report/{report_id}` 링크를 복사합니다.
공유 페이지는 기존 리포트 화면을 읽기 전용으로 렌더링합니다.

## 12. 트러블슈팅

### ADB 디바이스가 없음

```bash
adb devices -l
```

- USB 디버깅 권한과 인증 팝업을 확인합니다.
- 무선 연결은 `IP:PORT`를 확인합니다.
- 다중 디바이스에서는 실행한 디바이스 ID가 UI 선택값과 같은지 확인합니다.

### 화면은 열려 있는데 잠금으로 판정됨

```powershell
$env:ADB_DEVICE = "디바이스_ID"
python -c "from adb_controller import ADBController; a=ADBController(); print({'device': a.device_id, 'screen_on': a.is_screen_on(), 'locked': a.is_locked()})"
```

정상은 `screen_on=True`, `locked=False`입니다. 서버의 `dev` 커밋과 백엔드 재시작 여부를 먼저 확인합니다. `stayon`은 화면 꺼짐을 막지만 재부팅 후 최초 PIN 요구를 없애지는 못합니다.

### Windows에서 SR options curl 연결이 닫힘

PowerShell의 `curl`은 Windows PowerShell 5.1에서 `Invoke-WebRequest` alias입니다. 실제 curl과 ADB 포워딩을 사용합니다.

```powershell
$serial = "디바이스_ID"
$port = (adb -s $serial forward tcp:0 tcp:37772).Trim()
curl.exe --noproxy "*" "http://127.0.0.1:$port/api/sr/options"
```

### Vision이 target을 찾지 못함

- `target`에 위치, 형태, 텍스트, 주변 컨텍스트를 함께 작성합니다.
- 예: `버튼` 대신 `정말로 삭제하시겠습니까 팝업의 예 버튼`
- 결과 화면의 판정 스크린샷과 `screenshots_debug/`를 확인합니다.

### 탭은 됐지만 후조건이 실패함

- `wait_seconds`를 늘립니다.
- `expect_visible`를 팝업 자체가 아닌 다음 상태의 고유 요소로 지정합니다.
- 클릭 후 실제 판정 시점은 리포트의 `후조건 판정` 스크린샷으로 확인합니다.

### 팝업 개수가 매번 다름

- 확정적인 팝업 한 개는 `find_and_tap`으로 처리합니다.
- 1~3개처럼 수가 달라지는 팝업은 `dismiss_popups`에 허용 rule과 `stop_when_visible`을 정의합니다.
- 파괴적 확인이나 결제 버튼을 포괄적 팝업 rule로 등록하지 않습니다.

### UI 수정이 반영되지 않음

```bash
cd frontend
npm run build
```

`frontend/dist/index.html`의 자산 해시가 갱신됐는지 확인하고 `api_server.py`를 재시작합니다.

## 13. 모듈과 디렉터리

| 파일 | 역할 |
|:---|:---|
| `api_server.py` | 단일 노드 FastAPI, REST/WebSocket, React 정적 파일 제공 |
| `agent_server.py` | 중앙 오케스트레이터와 연동하는 PC Agent 서버 |
| `orchestrator_server.py` | 중앙 오케스트레이터 서버 |
| `qa_orchestrator.py` | step 순차 실행과 판정 엔진 |
| `adb_controller.py` | ADB 디바이스 제어, APK, 스크린샷, 잠금, 녹화 |
| `vision_agent.py` | Gemini Vision UI 탐지·OCR·화면 분석 |
| `planner_node.py` | 자연어 시나리오에서 step 생성 |
| `defense_dsl.py` | 디펜스 게임 공통 의미 액션과 구조화 출력 스키마 |
| `defense_compiler.py` | Game Profile 로드와 의미 Plan의 기존 Step 확장 |
| `planner_context.py` | legacy Planner에 넣을 관련 템플릿 선택 |
| `unity_api_client.py` | Unity SR API, 패키지별 `GAME_CHEAT_MAP` |
| `sr_debugger.py` | SR Debugger 제스처 진입 |
| `csv_reporter.py` | 감사 가능한 스텝·증거 CSV 생성 |
| `test_manager.py` | Pydantic action/test/result 모델과 결과 저장 |
| `frontend/src/components/wizard/` | 게임 선택, 실행/편집, 리포트 UI |
| `frontend/src/lib/template.ts` | 편집한 step의 YAML 직렬화 |
| `templates/` | 재사용 YAML 템플릿 |
| `game_profiles/` | 패키지별 디펜스 상태·recipe·안전 캐시 범위 |

## 14. 커밋하지 않는 로컬 산출물

- `.env`, `credentials.json`
- `apks/`
- `screenshots/`, `screenshots_debug/`
- `test_results/`, `reports/`, `recordings/`
- `*.db`, `__pycache__/`, `tmp/`

프론트엔드를 수정했다면 `frontend/dist/`는 소스와 함께 커밋합니다.
