# Backend API Test Guide

이 문서는 백엔드 API만으로 파이프라인을 실행하고, 실패 시 Gemini 평가 결과까지 확인하는 방법을 정리한다.

## Server

```bash
uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload
```

## Pipeline Run API

파이프라인 실행은 비동기다. `POST /api/pipeline/run`은 시작 여부만 반환하고, 최종 결과는 WebSocket으로 온다.

### 1. session_id 정하기

```bash
SESSION_ID=api_test_001
```

### 2. WebSocket 연결

다른 터미널에서 실행한다. WebSocket을 먼저 연결하면 서버가 해당 `session_id`의 시작을 기다린다.

```bash
python - <<'PY'
import asyncio
import json
import websockets

session_id = "api_test_001"

async def main():
    async with websockets.connect(f"ws://localhost:8000/ws/logs/{session_id}") as ws:
        async for message in ws:
            data = json.loads(message)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if data.get("type") == "done":
                break

asyncio.run(main())
PY
```

### 3. Pipeline 실행

```bash
curl -X POST http://localhost:8000/api/pipeline/run \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "api_test_001",
    "record": false,
    "steps": [
      {
        "action": "launch_app",
        "target": "com.percent.aos.cooptd",
        "description": "앱 실행"
      },
      {
        "action": "verify",
        "target": "일부러_없는_텍스트",
        "description": "실패 유도"
      }
    ]
  }'
```

최종 WebSocket 메시지는 아래 형태다.

```json
{
  "type": "result",
  "data": {
    "status": "FAIL",
    "title": "파이프라인 실행",
    "steps_passed": 1,
    "steps_executed": 2,
    "error_message": "Step 2 실패: 실패 유도 ...",
    "step_results": [],
    "eval_output": {
      "final_score": 0.42,
      "needs_alert": true,
      "flow": {
        "score": 0.4,
        "reason": "핵심 검증 스텝이 실패했습니다.",
        "failed_steps": [2],
        "severity": "CRITICAL"
      },
      "vision": {
        "score": 0.45,
        "low_confidence_steps": []
      }
    }
  }
}
```

## Result File

실행 결과는 `test_results/*.json`에 저장된다. 이제 Gemini 평가 이후 저장하므로 `eval_output`도 함께 들어간다.

```bash
ls -t test_results | head
```

## Single Test API

단일 테스트 API도 같은 방식이다.

WebSocket:

```text
ws://localhost:8000/ws/logs/{session_id}
```

REST:

```bash
curl -X POST http://localhost:8000/api/test/run \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "api_test_single_001",
    "title": "백엔드 단일 테스트",
    "package": "com.percent.aos.cooptd",
    "record": false,
    "steps": [
      {"action": "launch_app", "target": "com.percent.aos.cooptd"},
      {"action": "verify", "target": "일부러_없는_텍스트"}
    ]
  }'
```

## Guest Login: cooptd

`com.percent.aos.cooptd`의 게스트 로그인은 `게스트 로그인 데이터 유실 안내 팝업`이 아니라 약관 확인 알림이 뜬다. 따라서 실패 분석 시 아래처럼 기대값을 잡는다.

실패했던 기대값:

```json
{"expect_visible": "게스트 로그인 데이터 유실 안내 팝업"}
```

실제 앱 흐름:

```text
게스트 로그인
→ 약관 알림 팝업
→ 동의합니다
→ 로그인 중입니다...
→ 튜토리얼/인트로 화면의 탭하여 넘어가기
```

API payload:

```bash
curl -X POST http://localhost:8000/api/test/run \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "guest_login_cooptd",
    "title": "cooptd 게스트 로그인",
    "package": "com.percent.aos.cooptd",
    "record": false,
    "steps": [
      {
        "action": "launch_app",
        "target": "com.percent.aos.cooptd",
        "description": "앱 실행",
        "timeout": 20,
        "retry": 1
      },
      {
        "action": "find_and_tap",
        "target": "게스트 로그인",
        "description": "게스트 로그인 버튼 클릭",
        "params": {
          "expect_visible": "동의합니다"
        },
        "timeout": 20,
        "retry": 2
      },
      {
        "action": "find_and_tap",
        "target": "동의합니다",
        "description": "약관 알림 동의",
        "params": {
          "expect_visible": "탭하여 넘어가기"
        },
        "timeout": 45,
        "retry": 2
      }
    ]
  }'
```

## Adaptive QA Agent API

`POST /api/adaptive/test/run`은 정적인 스텝을 한 번 실행하고 끝내지 않는다. 실패하면 Gemini 평가 결과, step failure, 최신 스크린샷을 근거로 테스트 스텝을 수정한 뒤 다시 실행한다.

루프:

```text
초기 steps 실행
→ 실패 결과 + eval_output 수집
→ 실패 원인 분석
→ step patch 생성
→ patch 적용
→ 재실행
→ PASS 또는 max_iterations 도달
```

`cooptd` 게스트 로그인처럼 잘못된 기대값을 가진 테스트를 넣으면, 실제 앱 흐름에 맞춰 아래 수정이 자동 적용된다.

```text
기존: Guest login 버튼 → 게스트 로그인 데이터 유실 안내 팝업
수정: 게스트 로그인 → 동의합니다 → 탭하여 넘어가기
```

예시:

```bash
curl -X POST http://localhost:8000/api/adaptive/test/run \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "adaptive_guest_login_cooptd",
    "title": "adaptive cooptd 게스트 로그인",
    "package": "com.percent.aos.cooptd",
    "goal": "게스트 로그인으로 게임에 진입한다.",
    "max_iterations": 3,
    "reset_app_each_iteration": true,
    "steps": [
      {
        "action": "find_and_tap",
        "target": "Guest login 버튼",
        "description": "Guest login 버튼 클릭",
        "params": {
          "expect_visible": "게스트 로그인 데이터 유실 안내 팝업"
        },
        "timeout": 15,
        "retry": 2
      },
      {
        "action": "find_and_tap",
        "target": "아니오 버튼",
        "description": "데이터 유실 안내 팝업에서 아니오 클릭",
        "params": {
          "expect_hidden": "게스트 로그인 데이터 유실 안내 팝업"
        },
        "timeout": 10,
        "retry": 2
      }
    ]
  }'
```

응답에는 각 iteration의 실행 결과, 실패 분석, 적용한 patches, 최종 steps가 포함된다.

```json
{
  "run": {
    "status": "PASS",
    "iterations": [
      {
        "iteration": 1,
        "status": "FAIL",
        "analysis": {
          "failure_reason": "...",
          "patch_rationale": "cooptd 실제 흐름에 맞춰 ..."
        },
        "patches": [
          {
            "op": "update_step",
            "index": 2,
            "updates": {
              "target": "게스트 로그인",
              "params": {"expect_visible": "동의합니다"}
            }
          },
          {
            "op": "insert_after",
            "index": 2,
            "step": {
              "action": "find_and_tap",
              "target": "동의합니다",
              "params": {"expect_visible": "탭하여 넘어가기"}
            }
          }
        ]
      }
    ],
    "final_steps": []
  }
}
```

이미 로그인된 상태에서는 로그인 화면이 나오지 않으므로 아래 검증으로 성공 상태만 확인한다.

```bash
curl -X POST http://localhost:8000/api/test/run \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "guest_login_cooptd_success_verify",
    "title": "cooptd 게스트 로그인 성공 상태 검증",
    "package": "com.percent.aos.cooptd",
    "record": false,
    "steps": [
      {
        "action": "verify",
        "target": "탭하여 넘어가기",
        "description": "게스트 로그인 후 튜토리얼 화면 진입 확인",
        "timeout": 10,
        "retry": 1
      }
    ]
  }'
```
