# 디펜스 인게임 Planner 설계

## 목표

자연어 모델은 사용자의 의도만 구조화하고, 게임별 좌표·치트·검증 방식은 코드와
Game Profile이 결정한다. 실행기는 기존 `TestStep`만 받으므로 현재 템플릿과
`QAOrchestrator`를 그대로 사용할 수 있다.

```text
자연어
  -> Intent Compiler (PlannerNode)
  -> DefenseIntentPlan
  -> DefensePlanCompiler + Game Profile
  -> 기존 TestStep[]
  -> QAOrchestrator
```

Planner가 Intent Compiler 역할을 수행하는 것은 맞다. 다만 책임을 다음처럼 나눈다.

- Planner: `스테이지 진입`, `3회 소환`, `웨이브 시작`처럼 무엇을 할지 선택
- Planner 경계 보정: 모델이 `lobby → summon/start_wave` 사이의 `enter_stage`만
  빠뜨린 경우 Profile의 `options.default_stage`로 해당 전이를 결정론적으로 보충.
  반복 웨이브 사이에는 다음 `prep` 상태 대기만 보충
- Game Profile: 해당 게임에서 어떤 치트·버튼·후조건을 사용할지 선언
- Compiler: 의미 액션을 저수준 Step으로 결정론적으로 확장하고 검증
- Runner: 확장된 Step을 순서대로 실행하고 증거를 저장

모델이 좌표, 임의 치트 ID, 재시도 정책을 직접 만들지 않게 하는 것이 핵심이다.

## 사용 모드

`POST /api/plan/generate` 요청의 `planner_mode`로 경로를 명시한다.

```json
{
  "scenario": "앱을 초기화하고 1-1에 들어가 이지스를 2회 소환한 뒤 전투를 시작해",
  "package": "com.supermagic.aos.aegisdefense",
  "planner_mode": "defense"
}
```

- `legacy`: 기존 범용 저수준 Step 생성. 기본값이며 기존 요청과 호환된다.
- `defense`: 제한된 Defense DSL 생성 후 서버에서 기존 Step으로 확장한다.

지원 프로필이 없거나 의미 액션으로 표현할 수 없는 요청은 `legacy`로 조용히
우회하지 않고 422 오류를 반환한다. 사용자가 다른 동작을 실행하는 것을 막기 위해서다.

응답의 `plan.steps`와 YAML에는 실행 가능한 기존 Step만 들어간다.
`semantic_plan`은 의도 해석을 확인하기 위한 메타데이터다.

## MVP 의미 액션

| 액션 | 의미 |
|:---|:---|
| `bootstrap_to_lobby` | 앱을 초기 상태에서 로비까지 진입 |
| `enter_stage` | `cheat` 또는 `ui` 경로로 스테이지 진입 |
| `summon` | 지정 횟수만큼 유닛 소환 |
| `start_wave` | 준비된 웨이브 시작 |
| `set_speed` | 지원되는 배속 동작 수행 |
| `pause` / `resume` | 전투 일시정지·재개 |
| `wait_for_state` | 상태가 될 때까지 제한 시간 내 대기 |
| `verify_state` | 현재 상태 확인 |
| `claim_result` | 결과 확인 후 로비 복귀 |

MVP는 임의 좌표, 전략 생성, 자동 승리, 드래그 합성, 결제, 계정 흐름을 다루지 않는다.
필요한 기능은 실제 반복 사례가 생긴 뒤 DSL과 Profile에 함께 추가한다.

## 이지스 Profile

`game_profiles/aegis.yaml`은 다음을 담는다.

- 패키지와 지원 의미 액션
- `lobby`, `prep`, `wave_active`, `paused`, `victory`, `defeat`, `result` 상태 판정
- 각 의미 액션을 기존 Step으로 확장하는 recipe
- 실제 조작 뒤의 화면·씬·프로퍼티 후조건
- 안전하게 재사용할 수 있는 `cache_scope`

의미 액션이 게임 상태를 바꾸는데 recipe에 후조건이 하나도 없으면 Compiler가
실행 전에 실패시킨다. Profile placeholder가 해석되지 않거나 기존 `TestStep`
스키마에 맞지 않아도 동일하게 실패한다.

Compiler가 앞 단계에서 상태를 알 수 있는 경우 `lobby → pause`,
`wave_active → summon` 같은 불가능한 순서도 실행 전에 거부한다. 별도 자율 Agent가
게임을 추측하며 복구하지 않는다. 단, Profile에 `options.default_stage`가 명시된
게임은 Planner 경계에서 누락된 `lobby → enter_stage → prep` 전이와 반복 웨이브의
`wave_active → wait_for_state(prep)` 전이만 보충한다.

## 런타임 탐색 순서

`find_and_tap`은 다음 순서를 사용한다.

1. 이메일 주소는 Android XML exact match
2. Profile의 `unity_name`은 Unity metadata exact unique match
3. `cache_safe: true`, `cache_scope`, 후조건이 모두 있을 때만 좌표 캐시
4. 기존 고정 탭 shortcut
5. Vision

Unity exact나 캐시 좌표로 이미 탭한 뒤 후조건이 실패하면 Vision으로 다시 누르지
않는다. 검증만 재시도하고, 캐시 항목은 무효화한다. 소환·구매처럼 중복 탭이 게임
상태를 두 번 바꾸는 사고를 막기 위한 규칙이다.

Vision으로 찾은 좌표도 후조건이 통과한 경우에만 명시된 `cache_scope`에 저장한다.

## 조건 대기

기존 `wait` action은 고정 시간과 조건 대기를 모두 지원한다.

```yaml
- action: wait
  timeout: 120
  params:
    until_scene: ingame
    poll_interval_seconds: 1
```

지원 조건:

- `until_scene: ingame|outgame`
- `until_property: {id: debug.time_scale, equals: 2}`
- `until_unity_button: BtnSummonAegis`
- `until_unity_button_hidden: BtnPausePopup` (`until_unity_button`과 함께 사용)
- `until_visible` / `until_hidden`

여러 조건을 같이 쓰면 모두 만족해야 통과한다. API 오류나 빈 응답은
`until_hidden` 성공으로 간주하지 않는다. 조건이 없으면 기존 `seconds` 대기이며,
두 방식 모두 사용자 중단 요청에 즉시 반응한다. 전환 순간의 오래된 응답을 피해야
하면 `consecutive_matches: 2`처럼 연속 충족 횟수를 지정한다.

## 새 디펜스 게임 추가 순서

1. 실기기에서 로비·준비·전투·일시정지·결과 상태의 안정적인 판정 신호를 기록한다.
2. UI 이름, Unity component 이름, 사용 가능한 치트·프로퍼티를 확인한다.
3. `game_profiles/<game>.yaml`에 기존 의미 액션의 recipe만 작성한다.
4. Profile을 로드하고 동일 의미 Plan을 두 번 컴파일해 결과가 같은지 확인한다.
5. 각 상태 변경 recipe에 후조건이 있는지 확인한다.
6. 짧은 시나리오부터 실기기 smoke test를 수행한다.
7. 기존 DSL로 표현할 수 없는 반복 요구가 확인될 때만 새 의미 액션을 추가한다.

게임별 차이는 Profile에 두고, Runner 조건문이나 Planner prompt에 패키지별 로직을
추가하지 않는 것을 기본 원칙으로 한다.

## 모델 라우팅

모델은 역할별 환경 변수로 독립 변경할 수 있다.

```env
GEMINI_INTENT_MODEL=gemini-3.1-flash-lite
GEMINI_PLANNER_MODEL=gemini-3.1-pro-preview
GEMINI_VISION_MODEL=gemini-3.1-flash-lite
GEMINI_VISION_LITE_MODEL=gemini-2.5-flash
PLANNER_TEMPLATE_LIMIT=5
```

- Defense Intent Compiler는 작은 enum 구조만 생성하므로 `GEMINI_INTENT_MODEL`
- 기존 범용 Planner는 `GEMINI_PLANNER_MODEL`
- 좌표 탐지·OCR은 `GEMINI_VISION_MODEL`
- 후조건·팝업 배치 판독은 `GEMINI_VISION_LITE_MODEL`

사내 Gateway가 지원하는 모델 이름을 우선 사용하고, 모델 교체는 환경 변수로
측정하면서 진행한다.
