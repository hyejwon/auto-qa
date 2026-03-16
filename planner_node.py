# planner_node.py
from google.genai import types
from google import genai
from typing import Dict, List, Optional
from pathlib import Path
import json
import yaml
import logging
from pydantic import BaseModel

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

class PlannerNode:
    """자연어 → YAML 테스트케이스 변환 플래너"""
    
    def __init__(self, project: str, location: str = "global", 
                 model: str = "gemini-2.0-flash-exp"):
        self.client = genai.Client(
            vertexai=True,
            project=project,
            location=location
        )
        self.model = model
        logger.info(f"Initialized Planner Node: {model}")
    
    def create_test_plan(
        self, 
        natural_language_scenario: str,
        package_name: str = ""
    ) -> TestPlan:
        """
        자연어 시나리오를 구조화된 테스트 플랜으로 변환
        
        Args:
            natural_language_scenario: 자연어로 작성된 테스트 시나리오
            package_name: 앱 패키지명 (옵션)
        
        Returns:
            TestPlan: 구조화된 테스트 플랜
        """
        prompt = self._build_planner_prompt(natural_language_scenario, package_name)
        
        try:
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
            
            logger.info(f"Test plan created: {test_plan.title}")
            logger.info(f"Total steps: {len(test_plan.steps)}")
            
            return test_plan
            
        except Exception as e:
            logger.error(f"Failed to create test plan: {e}")
            raise
    
    def _build_planner_prompt(self, scenario: str, package_name: str) -> str:
        """플래너 프롬프트 생성"""
        return f"""
당신은 모바일 QA 테스트 전문가입니다. 자연어로 작성된 테스트 시나리오를 구조화된 테스트 스텝으로 변환하세요.

**입력 시나리오:**
{scenario}

**앱 패키지명:** {package_name or '(자동 추출)'}

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
       "expect_hidden": "탭 후 사라져야 하는 요소 (선택)"
     }}
   - **`expect_visible`은 필수이다.** 탭 후 어떤 요소/화면이 보여야 하는지 반드시 명시하라.
   - 예: 버튼 탭 → 팝업이 뜨면 expect_visible="팝업 제목", 화면 전환이면 expect_visible="다음 화면 특징 요소"

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
       "expect_changed": true/false   // true: 값이 달라야 PASS / false: 값이 같아야 PASS
     }}
   - PID 변경 여부 확인 예시:
     1) 연동 전: action=read_text, target="PID 값", params={{save_as: "pid_before"}}
     2) 연동 후: action=read_text, target="PID 값", params={{compare_with: "pid_before", expect_changed: true}}

10. `skip_tutorial` - 튜토리얼/훈련소 클리어 치트 실행 (Unity SR API 호출)
    - params: 없음


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
12. 튜토리얼 스킵 후 로비 진입 시 뜨는 팝업(이벤트, 공지, 보상 등)은 개별 `find_and_tap` 스텝으로 처리하세요.
13. `launch_app` 후 별도 `wait` 스텝은 불필요하다 (실행 후 5초 대기가 자동 적용됨).

**출력 형식 (JSON):**
{{
  "title": "테스트 제목 (간결하게)",
  "description": "테스트 설명",
  "package": "com.example.app",
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
