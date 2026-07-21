"""
Langfuse Prompt Management 초기 등록 스크립트.
한 번만 실행하면 됩니다. 이후 프롬프트 수정은 Langfuse UI에서 직접 가능.

사용법:
    python setup_prompts.py
"""
from langfuse import Langfuse
from dotenv import load_dotenv

load_dotenv()
langfuse = Langfuse()

# ── 1. find_element ──────────────────────────────────────────────────────────
langfuse.create_prompt(
    name="find_element",
    prompt="""이 게임 화면에서 '{{target_description}}'을(를) 찾아주세요.
box_2d는 [ymin, xmin, ymax, xmax] 형식으로, 0~1000 범위로 정규화해서 반환하세요.

**규칙:**
1. 요소가 여러 개면 가장 중앙/명확한 것 선택
2. 찾을 수 없으면 box_2d를 null로 반환

**반환 형식 (JSON만):**
{
  "found": true/false,
  "box_2d": [ymin, xmin, ymax, xmax] or null,
  "description": "찾은 요소 설명",
  "confidence": 0.0~1.0
}""",
    labels=["production"],
    tags=["vision"],
    config={"model": "gemini-2.0-flash-exp", "response_mime_type": "application/json"},
)
print("✓ find_element prompt created")

# ── 2. analyze_screen_state ──────────────────────────────────────────────────
langfuse.create_prompt(
    name="analyze_screen_state",
    prompt="""이 게임 화면을 분석해줘.

screen_type은 반드시 아래 값 중 하나만 사용해:
- "title"    : 타이틀/스플래시/로그인 화면 (게스트·Google·Apple 로그인 버튼 등)
- "lobby"    : 메인 로비/홈 화면 (햄버거 메뉴, 전투 시작 등 주요 HUD 포함)
- "settings" : 설정 팝업 또는 설정 화면 (진동·효과음·이용약관·계정연동 등)
- "account"  : 계정 연동 팝업 (Google/Apple 연동·로그아웃·계정삭제 버튼 등)
- "shop"     : 상점/구매 화면
- "battle"   : 전투/게임플레이 화면
- "ranking"  : 랭킹 화면
- "unknown"  : 위 항목에 해당하지 않는 경우

JSON 형식으로 반환:
{
  "screen_type": "위 목록 중 하나",
  "ui_elements": ["요소1", "요소2", ...],
  "popups": ["팝업1", ...] or [],
  "suggested_actions": ["액션1", "액션2", ...]
}""",
    labels=["production"],
    tags=["vision"],
    config={"model": "gemini-2.0-flash-exp", "response_mime_type": "application/json"},
)
print("✓ analyze_screen_state prompt created")

# ── 3. read_text ─────────────────────────────────────────────────────────────
langfuse.create_prompt(
    name="read_text",
    prompt="""이 게임 화면에서 '{{region_description}}'에 해당하는 텍스트 값을 읽어줘.

**규칙:**
1. 해당 영역의 텍스트만 정확히 반환한다.
2. 찾을 수 없으면 value를 null로 반환한다.

**반환 형식 (JSON만):**
{
  "found": true/false,
  "value": "읽은 텍스트" or null
}""",
    labels=["production"],
    tags=["vision"],
    config={"model": "gemini-2.0-flash-exp", "response_mime_type": "application/json"},
)
print("✓ read_text prompt created")

# ── 4. judge_flow_completion ─────────────────────────────────────────────────
langfuse.create_prompt(
    name="judge_flow_completion",
    prompt="""당신은 모바일 게임 QA 전문가입니다.
아래는 테스트 시나리오 실행 결과입니다.

시나리오 제목: {{title}}
전체 상태: {{status}}
실행 스텝 (vision_confidence는 UI 요소 탐지 신뢰도):
{{steps_summary}}

채점 기준:
- 1.0 : 모든 스텝 완료, vision_confidence 전반적으로 높음
- 0.7~0.9 : 핵심 플로우 완료, 일부 스텝 실패 또는 confidence 낮음
- 0.4~0.6 : 핵심 플로우 중 중요 스텝 실패
- 0.0~0.3 : 시나리오 목적 달성 불가 수준

반드시 아래 JSON 형식으로만 응답하세요:
{
  "score": 0.0~1.0,
  "reason": "판단 근거 1~2문장",
  "failed_steps": [실패한 스텝 번호 리스트],
  "severity": "CRITICAL|WARNING|OK"
}""",
    labels=["production"],
    tags=["eval"],
    config={"model": "gemini-2.5-flash"},
)
print("✓ judge_flow_completion prompt created")

# ── 5. create_test_plan (planner) ────────────────────────────────────────────
langfuse.create_prompt(
    name="create_test_plan",
    prompt="""당신은 모바일 QA 테스트 전문가입니다. 자연어로 작성된 테스트 시나리오를 구조화된 테스트 스텝으로 변환하세요.

**입력 시나리오:**
{{scenario}}

**앱 패키지명:** {{package_name}}

**사용 가능한 액션 타입:**
1. `launch_app` - 앱 실행
   - params: {package: "앱 패키지명"}

2. `find_and_tap` - UI 요소 찾아서 클릭
   - target: "찾을 UI 요소 설명" (예: "구글 로그인 버튼", "확인 버튼")
   - params:
     {
       "expect_visible": "탭 후 보여야 하는 요소 (필수)",
       "wait_seconds": 3,
       "verify_timeout_sec": 2.0,
       "expect_hidden": "탭 후 사라져야 하는 요소 (선택)"
     }
   - **`expect_visible`은 필수이다.** 탭 후 어떤 요소/화면이 보여야 하는지 반드시 명시하라.
   - 예: 버튼 탭 → 팝업이 뜨면 expect_visible="팝업 제목", 화면 전환이면 expect_visible="다음 화면 특징 요소"

3. `verify` - 화면에 특정 요소가 보이는지 검증
   - target: "검증할 UI 요소"

4. `wait` - 대기
   - params: {seconds: 대기시간}

5. `back` - 뒤로가기 버튼
   - params(선택):
     {
       "wait_seconds": 1.0,
       "expect_visible": "뒤로가기 후 보여야 하는 요소",
       "expect_hidden": "뒤로가기 후 사라져야 하는 요소"
     }
   - back 후 복귀 검증이 필요하면 separate wait/verify 대신 이 params를 우선 사용

6. `home` - 홈 버튼

7. `close_app` - 앱 종료
   - params: {package: "앱 패키지명"}

8. `swipe` - 스와이프
   - params: {x1, y1, x2, y2, duration}

9. `read_text` - 화면에서 텍스트 값을 읽어 저장하고, 이전 값과 비교 검증
   - target: "읽을 텍스트 영역 설명" (예: "PID 값", "유저 ID 숫자")
   - params(선택):
     {
       "save_as": "변수명",
       "compare_with": "변수명",
       "expect_changed": true/false
     }
   - PID 변경 여부 확인 예시:
     1) 연동 전: action=read_text, target="PID 값", params={save_as: "pid_before"}
     2) 연동 후: action=read_text, target="PID 값", params={compare_with: "pid_before", expect_changed: true}

10. `skip_tutorial` - Unity QA helper API로 튜토리얼/훈련소 클리어 처리
    - target 또는 params.package: 앱 패키지명
    - SR Debugger 화면/이미지 조작 없이 Unity API만 호출한다.

11. `dismiss_popups` - 허용된 팝업을 반복해서 닫고 최종 화면까지 도달
    - params:
      {
        "stop_when_visible": "최종 화면의 고유 요소",
        "max_count": 4,
        "timeout_seconds": 30,
        "quiet_seconds": 1.5,
        "rules": [
          {"target": "팝업 설명", "action": "back|tap|tap_center"}
        ]
      }
    - 팝업 개수나 반복 횟수가 달라질 수 있을 때 사용한다.
    - rules에 명시되지 않은 팝업은 임의로 닫지 않는다.

**변환 규칙:**
1. 시나리오를 논리적 순서대로 스텝으로 분해
2. 각 스텝은 하나의 명확한 액션만 수행
3. UI 요소는 사용자가 이해하기 쉬운 자연어로 표현
4. 암묵적인 대기 시간을 명시적인 wait 스텝으로 추가
5. 검증 스텝을 적절히 삽입
6. 패키지명이 시나리오에 없으면 일반적인 패턴 추론
7. 뒤로가기 후 특정 화면으로 복귀해야 하는 경우 `back` 스텝의 params에 `expect_visible`, `expect_hidden`, `wait_seconds`를 넣어 atomic 하게 검증
8. 단, back 이후 완전히 다른 사용��� 액션이 이어지고 복귀 확인 기준이 없으면 일반 `back`만 사용
9. 모든 `find_and_tap`에는 반드시 `expect_visible`을 넣어야 한다. 탭 후 어떤 요소/화면이 나타나야 하는지 명시하라.
10. 다음 스텝의 target과 동일한 요소라도 `expect_visible`은 생략하지 말라.
11. `skip_tutorial`은 필요 시 `wait`를 넣어 치트 적용 시간을 보장
12. 개수나 순서가 달라지는 이벤트/공지/보상 팝업은 `dismiss_popups`로 처리한다.
13. `launch_app` 후 별도 `wait` 스텝은 불필요하다 (실행 후 5초 대기가 자동 적용됨).

**출력 형식 (JSON):**
{
  "title": "테스트 제목 (간결하게)",
  "description": "테스트 설명",
  "package": "com.example.app",
  "steps": [
    {
      "action": "액션타입",
      "target": "대상 요소 (옵션)",
      "params": {},
      "description": "이 스텝이 하는 일",
      "timeout": 30,
      "retry": 3
    }
  ],
  "expected_results": [
    "기대 결과 1",
    "기대 결과 2"
  ]
}

**중요:**
- JSON 형식만 출력하고 다른 텍스트는 포함하지 마세요
- 모든 필드를 빠짐없이 채우세요
- steps 배열은 최소 1개 이상의 스텝을 포함해야 합니다
- 가능하면 `back + wait + verify`를 따로 나누지 말고, `back.params.expect_visible/expect_hidden`로 표현하세요
- 가능하면 `find_and_tap + wait + verify`도 `find_and_tap.params.expect_visible/expect_hidden`로 합치세요
- PID / 유저 ID / 계정 ID 등 숫자/문자 값의 변경·유지 여부를 확인해야 하는 경우 `read_text`를 사용하세요
- `read_text`로 값을 비교할 때는 반드시 확인 전(save_as)과 후(compare_with)를 쌍으로 구성하세요""",
    labels=["production"],
    tags=["planner"],
    config={"model": "gemini-2.0-flash-exp", "temperature": 0.1, "response_mime_type": "application/json"},
)
print("✓ create_test_plan prompt created")

langfuse.flush()
print("\n모든 프롬프트 등록 완료. Langfuse UI에서 확인/수정 가능합니다.")
