# planner_node.py
from google.genai import types
from google import genai
from llm_client import build_genai_client
from typing import Dict, List, Optional
from pathlib import Path
import json
import yaml
import logging
from pydantic import BaseModel
from langfuse_disabled import get_client

langfuse = get_client()

logger = logging.getLogger(__name__)

class PlannerStep(BaseModel):
    """플래너가 생성한 스텝"""
    action: str
    target: Optional[str] = None
    params: Dict = {}
    description: str = ""
    timeout: int = 30
    retry: int = 3

class TestPlan(BaseModel):
    """플래너가 생성한 테스트 플랜"""
    title: str
    description: str
    package: str
    steps: List[PlannerStep]
    expected_results: List[str] = []
    required_tab: Optional[str] = None  # 실행 전 자동으로 이동해야 하는 하단 네비게이션 탭 (예: "전투")

class PlannerNode:
    """자연어 → YAML 테스트케이스 변환 플래너"""
    
    def __init__(self, project: str = "", location: str = "global",
                 model: str = "gemini-2.0-flash-exp"):
        self.client = build_genai_client()
        self.model = model
        logger.info(f"Initialized Planner Node: {model}")
    
    def create_test_plan(
        self,
        natural_language_scenario: str,
        package_name: str = "",
        template_library: str = "",
    ) -> TestPlan:
        """
        자연어 시나리오를 구조화된 테스트 플랜으로 변환

        Args:
            natural_language_scenario: 자연어로 작성된 테스트 시나리오
            package_name: 앱 패키지명 (옵션)
            template_library: 검증된 기존 템플릿 YAML 모음 — 플래너가 스텝을
                재사용/조합할 소스. 비면 백지 생성.

        Returns:
            TestPlan: 구조화된 테스트 플랜
        """
        prompt = self._build_planner_prompt(
            natural_language_scenario, package_name or "(자동 추출)", template_library)

        try:
            with langfuse.start_as_current_observation(
                as_type="span",
                name="create_test_plan",
                input={"scenario": natural_language_scenario, "package": package_name, "prompt": prompt},
            ) as span:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=[prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1
                    )
                )

                plan_data = json.loads(response.text)
                test_plan = TestPlan(**plan_data)
                span.update(output=plan_data)

            logger.info(f"Test plan created: {test_plan.title}")
            logger.info(f"Total steps: {len(test_plan.steps)}")

            return test_plan

        except Exception as e:
            logger.error(f"Failed to create test plan: {e}")
            raise
    
    def _build_planner_prompt(self, scenario: str, package_name: str,
                              template_library: str = "") -> str:
        """플래너 프롬프트 생성"""
        library_section = ""
        if template_library.strip():
            library_section = f"""
**검증된 템플릿 라이브러리 (스텝 재사용 최우선):**
아래는 실기기에서 검증이 끝난 이 프로젝트의 템플릿들이다. 시나리오의 일부가
라이브러리 템플릿의 흐름과 겹치면 그 스텝들을 **target 문구와 params까지 그대로
복사**해서 사용하라. target 표현을 임의로 바꾸거나 params(optional, expect_visible,
scroll_search 등)를 빼먹으면 안 된다 — 그 문구/설정들은 실기기 시행착오로 다듬어진
것이다. 라이브러리에 없는 동작만 새로 작성하라.

{template_library}
"""
        return f"""
당신은 모바일 QA 테스트 전문가입니다. 자연어로 작성된 테스트 시나리오를 구조화된 테스트 스텝으로 변환하세요.

**입력 시나리오:**
{scenario}

**앱 패키지명:** {package_name or '(자동 추출)'}
{library_section}

**사용 가능한 액션 타입:**
1. `launch_app` - 앱 실행
   - params: {{package: "앱 패키지명"}}

2. `find_and_tap` - UI 요소 찾아서 클릭
   - target: "찾을 UI 요소 설명" (예: "구글 로그인 버튼", "확인 버튼")
   - params:
     {{
       "expect_visible": "탭 후 보여야 하는 요소 (필수)",
       "wait_seconds": 3,
       "verify_timeout_sec": 2.0,
       "expect_hidden": "탭 후 사라져야 하는 요소 (선택)",
       "optional": true,              // 조건부 팝업 등 안 나올 수도 있는 대상 — 미발견 시 실패 대신 건너뜀
       "scroll_search": true,         // 스크롤해야 나오는 대상 (상점 하단 상품 등)
       "max_scrolls": 8,              // scroll_search 시 최대 스크롤 횟수
       "then_tap": "연속 탭 대상",     // 첫 탭 직후 이어서 탭할 대상 (옵션 선택→확인 등)
       "tap_point": "center"          // 대상 확인만 하고 화면 정중앙 탭 (아무 곳이나 눌러 닫는 획득 팝업용)
     }}
   - **`expect_visible`은 필수이다.** 탭 후 어떤 요소/화면이 보여야 하는지 반드시 명시하라.
   - 예: 버튼 탭 → 팝업이 뜨면 expect_visible="팝업 제목", 화면 전환이면 expect_visible="다음 화면 특징 요소"
   - 최초 실행에만 나오는 약관/동의/권한 팝업 스텝에는 `optional: true`를 넣어라 (재실행 시 안 나옴)
   - 같은 화면에 수량·가격이 같은 유사 상품이 여럿이면 target에 섹션 헤더를 명시하라
     (예: "마신석 섹션 헤더 아래 50 상품의 5000 다이아 버튼")

3. `verify` - 화면에 특정 요소가 보이는지 검증
   - target: "검증할 UI 요소"

4. `wait` - 대기
   - params: {{seconds: 대기시간}}

5. `back` - 뒤로가기 버튼
   - params(선택):
     {{
       "wait_seconds": 1.0,
       "expect_visible": "뒤로가기 후 보여야 하는 요소",
       "expect_hidden": "뒤로가기 후 사라져야 하는 요소"
     }}
   - back 후 복귀 검증이 필요하면 separate wait/verify 대신 이 params를 우선 사용

6. `home` - 홈 버튼

7. `close_app` - 앱 종료
   - params: {{package: "앱 패키지명"}}

8. `swipe` - 스와이프
   - params: {{x1, y1, x2, y2, duration}}

9. `read_text` - 화면에서 텍스트 값을 읽어 저장하고, 이전 값과 비교 검증
   - target: "읽을 텍스트 영역 설명" (예: "PID 값", "유저 ID 숫자")
   - params(선택):
     {{
       "save_as": "변수명",           // 읽은 값을 저장할 변수명 (나중에 compare_with로 참조)
       "compare_with": "변수명",      // 이전에 save_as로 저장한 변수명과 비교
       "expect_changed": true/false,  // true: 값이 달라야 PASS / false: 값이 같아야 PASS
       "expect_increase": true,       // 숫자가 증가해야 PASS (재화 지급 검증, 방향만)
       "expect_decrease": true,       // 숫자가 감소해야 PASS (재화 차감 검증, 방향만)
       "expect_delta": 4000,          // 정확히 이만큼 변해야 PASS (예: +4000, -1500 — 정밀 검증)
       "delta_tolerance": 0           // expect_delta 허용 오차 (기본 0 = 정확히 일치)
     }}
   - PID 변경 여부 확인 예시:
     1) 연동 전: action=read_text, target="PID 값", params={{save_as: "pid_before"}}
     2) 연동 후: action=read_text, target="PID 값", params={{compare_with: "pid_before", expect_changed: true}}
   - 구매/보상으로 정확히 얼마나 늘거나 줄어야 하는지 알 때는 expect_increase/decrease 대신
     expect_delta를 써서 정밀하게 검증하라 (예: 4000 다이아 상품 구매 시 expect_delta: 4000)

10. `read_items` - 화면의 아이템 목록과 각 항목의 보유 여부를 함께 스캔해 저장/비교
    - target: "스캔할 목록과 보유 판단 기준 설명"
      (예: "마물 탭 아이템 목록 (컬러 아이콘=보유, 회색/자물쇠 아이콘=미보유)")
    - params(선택):
      {{
        "save_as": "변수명",                 // 스캔한 [{{name, owned, info}}] 목록을 저장
        "compare_with": "변수명",            // 이전 save_as 스냅샷과 비교해 보유 상태 변화 계산
        "expect_new_owned_count": 2,         // 이번에 새로 보유로 바뀐 항목 수가 정확히 이만큼이어야 PASS
        "expect_new_owned": ["마물A", "유물B"], // 새로 보유로 바뀐 항목에 이 이름들이 반드시 포함돼야 PASS
        "expect_no_change": true             // true면 보유 상태가 전혀 변하지 않아야 PASS
      }}
    - 여러 재화/아이템을 한 번에 지급하는 상품(예: 뉴비패키지) 검증 예시:
      1) 구매 전: 탭마다 read_items, params={{save_as: "monsters_before"}} / {{save_as: "relics_before"}}
      2) 구매 진행
      3) 구매 후: 같은 탭에서 read_items, params={{compare_with: "monsters_before", expect_new_owned_count: 1}}

11. `read_screen` - 한 화면에 같이 보이는 여러 항목을 vision 호출 1번으로 모아서 확인
    - target: 생략 가능 (사람이 읽을 라벨 용도)
    - params.items(필수): 항목 리스트
      {{
        "items": [
          {{
            "name": "diamond",                 // 내부 식별자 (save_as/compare_with 키로도 씀)
            "description": "다이아 수량",       // 값을 읽을 대상이면 구체적인 영역 설명,
                                                // 존재만 확인할 조건이면 판단 기준까지 명시
                                                // (예: "검귀 카드 — 다이아/자물쇠 아이콘 없음")
            "save_as": "diamond_before",        // (값 읽기 항목) read_text와 동일
            "compare_with": "diamond_before",   // (값 읽기 항목) read_text와 동일
            "expect_increase": true,            // (값 읽기 항목) read_text와 동일한 expect_* 전부 지원
            "optional": true                    // 화면에 없어도 실패 대신 건너뜀
          }}
        ]
      }}
    - "값을 읽는 항목"(재화 수량 등)은 read_text와 동일하게 save_as/compare_with/expect_changed/
      expect_increase/expect_decrease/expect_delta/delta_tolerance를 그대로 쓸 수 있다.
    - "존재만 확인하는 항목"(카드가 보이는지 등, 값이 없음)은 found 여부만 판정한다 —
      save_as를 주면 true가 저장된다.
    - 언제 쓰나: 같은 화면(탭 이동 없이)에 여러 정보가 동시에 보일 때 — 예를 들어 상단바에
      다이아·실버가 항상 같이 보이면 read_text 두 번 대신 read_screen 하나로 묶어라. 마물 탭처럼
      숫자(마신석)와 카드 존재 확인(검귀)이 같은 화면에 있어도 items 하나에 같이 넣으면 된다.
      화면이 바뀌어야 보이는 정보(다른 탭)까지 억지로 묶지는 마라 — vision이 그 화면을
      실제로 보고 있을 때만 정확하다.
    - params.scroll_search: true / max_scrolls / scroll_fraction — items 중 일부가 스크롤해야
      보이는 카드처럼 즉시 안 보여도, 재화 표시줄 같은 상단 고정 영역은 스크롤해도 그대로
      보이는 화면이 많다. 이럴 때 scroll_search를 켜면 안 보이는 항목만 스크롤하며 배치
      호출을 재시도한다 — 상단 고정 값과 스크롤 필요한 카드를 같은 read_screen에 넣어도 된다.

12. `skip_tutorial` - Unity QA helper API로 튜토리얼/훈련소 클리어 처리
    - target 또는 params.package: 앱 패키지명
    - SR Debugger 화면/이미지 조작 없이 Unity API만 호출한다.
    - ⚠️ 치트 호출 후 앱을 재시작해 "건너뛰기 확인" 팝업을 직접 눌러 마무리하는 흐름을
      만들 때는, 그 팝업을 여는/닫는 find_and_tap 스텝에 반드시 `optional: true`를 넣어라.
      이미 튜토리얼이 스킵된 계정으로 재실행하면 그 팝업 자체가 안 뜨기 때문에, optional이
      없으면 스텝이 그냥 실패한다 (2026-07-23 실기기에서 확인된 문제).
    - ⚠️ 로그인 직후(로그인이 완전히 끝나기 전) 이 API를 호출하면 앱이 그대로 재부팅되는
      문제가 있다 (2026-07-23 확인). skip_tutorial 스텝 바로 앞에, 로그인 완료 후 나타나는
      화면(예: 전투/로비 탭 또는 튜토리얼 건너뛰기 버튼 등 — 계정 상태에 따라 둘 중 하나)이
      안정적으로 보이는지 확인하는 `verify` 스텝을 반드시 넣어라.
    - ⚠️ 계정이 이미 튜토리얼을 끝낸 상태라면 이 API/앱 재시작 자체가 불필요하다 — 로그인 후
      화면이 이미 전투/로비 탭이면 `params.skip_if_visible`(로비 탭 등)을 넣어서
      skip_tutorial/close_app/launch_app/이후 정리용 dismiss_popups까지 전부 건너뛰게 하라.

13. `dismiss_popups` - 허용된 팝업을 반복해서 닫고 최종 화면까지 도달
    - params:
      {{
        "stop_when_visible": "최종 화면의 고유 요소",
        "max_count": 4,
        "timeout_seconds": 30,
        "quiet_seconds": 1.5,
        "rules": [
          {{"target": "팝업 설명", "action": "back|tap|tap_center"}}
        ]
      }}
    - 팝업 개수나 반복 횟수가 달라질 수 있을 때 사용한다.
    - rules에 명시되지 않은 팝업은 임의로 닫지 않는다.


**변환 규칙:**
1. 시나리오를 논리적 순서대로 스텝으로 분해
2. 각 스텝은 하나의 명확한 액션만 수행
3. UI 요소는 사용자가 이해하기 쉬운 자연어로 표현
4. 암묵적인 대기 시간을 명시적인 wait 스텝으로 추가
5. 검증 스텝을 적절히 삽입
6. 패키지명이 시나리오에 없으면 일반적인 패턴 추론
7. 뒤로가기 후 특정 화면으로 복귀해야 하는 경우 `back` 스텝의 params에
   `expect_visible`, `expect_hidden`, `wait_seconds`를 넣어 atomic 하게 검증
8. 단, back 이후 완전히 다른 사용자 액션이 이어지고 복귀 확인 기준이 없으면 일반 `back`만 사용
9. 모든 `find_and_tap`에는 반드시 `expect_visible`을 넣어야 한다. 탭 후 어떤 요소/화면이 나타나야 하는지 명시하라.
10. 다음 스텝의 target과 동일한 요소라도 `expect_visible`은 생략하지 말라.
11. `skip_tutorial`은 필요 시 `wait`를 넣어 치트 적용 시간을 보장
12. 개수나 순서가 달라지는 이벤트/공지/보상 팝업은 `dismiss_popups`로 처리한다.
13. `launch_app` 후 별도 `wait` 스텝은 불필요하다 (실행 후 5초 대기가 자동 적용됨).
14. 시나리오가 하단 네비게이션의 특정 탭(상점/마물/전투/유물/뽑기)이 이미 떠 있다고
    가정하고 시작하면(예: 계정 설정/로그아웃/삭제처럼 우측 상단 햄버거 메뉴가 필요한
    작업 — 이 메뉴는 '전투' 탭에서만 보인다), 최상위 `required_tab`에 그 탭 이름을
    넣어라. 실행 직전 그 탭으로 자동 이동하는 스텝이 서버에서 맨 앞에 끼워지므로,
    스텝 목록 자체에 탭 이동 스텝을 직접 넣지 않아도 된다 — `required_tab`만 채우면 된다.
    해당 없으면 생략(null).

**출력 형식 (JSON):**
{{
  "title": "테스트 제목 (간결하게)",
  "description": "테스트 설명",
  "package": "com.example.app",
  "required_tab": "전투 등 실행 전 필요한 하단 탭 이름 (해당 없으면 null)",
  "steps": [
    {{
      "action": "액션타입",
      "target": "대상 요소 (옵션)",
      "params": {{}},
      "description": "이 스텝이 하는 일",
      "timeout": 30,
      "retry": 3
    }}
  ],
  "expected_results": [
    "기대 결과 1",
    "기대 결과 2"
  ]
}}

**중요:**
- JSON 형식만 출력하고 다른 텍스트는 포함하지 마세요
- 모든 필드를 빠짐없이 채우세요
- steps 배열은 최소 1개 이상의 스텝을 포함해야 합니다
- 가능하면 `back + wait + verify`를 따로 나누지 말고, `back.params.expect_visible/expect_hidden`로 표현하세요
- 가능하면 `find_and_tap + wait + verify`도 `find_and_tap.params.expect_visible/expect_hidden`로 합치세요
- PID / 유저 ID / 계정 ID 등 숫자/문자 값의 변경·유지 여부를 확인해야 하는 경우 `read_text`를 사용하세요
- `read_text`로 값을 비교할 때는 반드시 확인 전(save_as)과 후(compare_with)를 쌍으로 구성하세요
- 실행할 때마다 달라질 수 있는 값(계정 이메일, 상품명 등)은 target에 `{{{{변수명}}}}` 플레이스홀더로
  쓰세요 (예: `{{{{account_email}}}}`) — 실행 시 UI에서 값을 입력받아 치환됩니다.
  단, 라이브러리 템플릿을 재사용할 때는 그 템플릿의 표기를 그대로 따르세요
- 여러 재화/아이템을 동시에 지급하는 상품(뉴비패키지 등)을 검증할 때는, 지급 동작 전에
  재화별 `read_text`(save_as)와 아이템 탭별 `read_items`(save_as)로 상태를 스냅샷 떠두고,
  지급 후 동일 대상을 `compare_with`로 다시 읽어 `expect_delta`(재화)/`expect_new_owned_count`
  또는 `expect_new_owned`(아이템)로 정확한 수치까지 검증하세요
"""
    
    def save_as_yaml(
        self, 
        test_plan: TestPlan, 
        output_dir: Path,
        test_id: Optional[str] = None
    ) -> Path:
        """
        테스트 플랜을 YAML 파일로 저장
        
        Args:
            test_plan: 저장할 테스트 플랜
            output_dir: 저장 디렉토리
            test_id: 테스트 ID (없으면 자동 생성)
        
        Returns:
            Path: 저장된 YAML 파일 경로
        """
        if not test_id:
            # 자동 ID 생성: TC_AUTO_001, TC_AUTO_002, ...
            existing_files = list(output_dir.glob("TC_AUTO_*.yaml"))
            next_num = len(existing_files) + 1
            test_id = f"TC_AUTO_{next_num:03d}"
        
        yaml_data = {
            "id": test_id,
            "title": test_plan.title,
            "description": test_plan.description,
            "package": test_plan.package,
            "steps": [step.model_dump() for step in test_plan.steps],
            "expected_results": test_plan.expected_results
        }
        
        output_path = output_dir / f"{test_id}.yaml"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            yaml.dump(yaml_data, f, allow_unicode=True, sort_keys=False)
        
        logger.info(f"Test plan saved: {output_path}")
        return output_path
    
    def generate_and_save(
        self,
        scenario: str,
        output_dir: Path,
        package_name: str = "",
        test_id: Optional[str] = None
    ) -> tuple[TestPlan, Path]:
        """
        자연어 시나리오 → 테스트 플랜 생성 → YAML 저장 (원스톱)
        
        Returns:
            (TestPlan, yaml_path) 튜플
        """
        # 1. 테스트 플랜 생성
        test_plan = self.create_test_plan(scenario, package_name)
        
        # 2. YAML 저장
        yaml_path = self.save_as_yaml(test_plan, output_dir, test_id)
        
        return test_plan, yaml_path
