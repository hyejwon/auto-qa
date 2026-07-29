# planner_node.py
from google.genai import types
from google import genai
from llm_client import build_genai_client
from typing import Any, Dict, List, Optional
from pathlib import Path
import json
import re
import yaml
import logging
from pydantic import BaseModel, ValidationError
from langfuse_disabled import get_client
from defense_dsl import (
    DefenseAction,
    DefenseIntentPlan,
    DefenseIntentStep,
    DefenseState,
    StageEntryRoute,
    known_defense_state_after,
)
from defense_compiler import DefenseGameProfile

langfuse = get_client()

logger = logging.getLogger(__name__)

_STAGE_AFTER_LABEL = re.compile(
    r"(?:스테이지|stage)\s*(\d{1,3})\s*[-–—]\s*(\d{1,3})",
    re.IGNORECASE,
)
_STAGE_BEFORE_LABEL = re.compile(
    r"(?<!\d)(\d{1,3})\s*[-–—]\s*(\d{1,3})"
    r"\s*(?:스테이지|stage|에서|에(?=\s|$|진입|입장))",
    re.IGNORECASE,
)
_UI_ENTRY_FOCUS_TERMS = (
    "에너지 소비",
    "정식 입장",
    "입장 과정",
    "정식 보상",
    "보상 지급",
)


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


class PlannerResponseFormatError(ValueError):
    """LLM이 테스트 계획 객체가 아닌 형식을 반환했을 때 발생."""


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
        device_catalog: str = "",
    ) -> TestPlan:
        """
        자연어 시나리오를 구조화된 테스트 플랜으로 변환

        Args:
            natural_language_scenario: 자연어로 작성된 테스트 시나리오
            package_name: 앱 패키지명 (옵션)
            template_library: 검증된 기존 템플릿 YAML 모음 — 플래너가 스텝을
                재사용/조합할 소스. 비면 백지 생성.
            device_catalog: 이 패키지에서 실제로 수집된 v2 치트/프로퍼티 카탈로그 텍스트
                (unity_catalog.to_planner_text). 있으면 플래너가 실존하는 치트 id로
                사전 상태를 만드는 스텝을 생성한다.

        Returns:
            TestPlan: 구조화된 테스트 플랜
        """
        prompt = self._build_planner_prompt(
            natural_language_scenario, package_name or "(자동 추출)", template_library,
            device_catalog)

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
                        temperature=0.1,
                    )
                )

                parsed = getattr(response, "parsed", None)
                if parsed is None:
                    try:
                        parsed = json.loads(response.text)
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise PlannerResponseFormatError(
                            "AI가 올바른 JSON 테스트 계획을 반환하지 않았습니다. 다시 생성해 주세요."
                        ) from exc

                try:
                    plan_data = self._normalize_plan_data(
                        parsed, natural_language_scenario, package_name
                    )
                    test_plan = TestPlan.model_validate(plan_data)
                except PlannerResponseFormatError:
                    raise
                except (TypeError, ValueError, ValidationError) as exc:
                    raise PlannerResponseFormatError(
                        "AI 테스트 계획에 필수 항목이 누락되었습니다. 다시 생성해 주세요."
                    ) from exc

                span.update(output=test_plan.model_dump())

            logger.info(f"Test plan created: {test_plan.title}")
            logger.info(f"Total steps: {len(test_plan.steps)}")

            return test_plan

        except Exception as e:
            logger.error(f"Failed to create test plan: {e}")
            raise

    def create_defense_plan(
        self,
        natural_language_scenario: str,
        package_name: str,
        profile: DefenseGameProfile,
    ) -> DefenseIntentPlan:
        """Compile natural language into the bounded defense-game intent DSL.

        Unlike ``create_test_plan``, this method never generates tap targets,
        coordinates, retries, or low-level runtime actions.  The deterministic
        profile compiler expands the returned plan afterwards.
        """

        if not package_name.strip():
            raise ValueError("디펜스 인게임 플래너에는 package가 필요합니다.")
        if package_name.strip() != profile.package:
            raise ValueError(
                f"선택한 package와 프로필이 다릅니다: "
                f"{package_name.strip()} != {profile.package}"
            )

        prompt = self._build_defense_intent_prompt(
            natural_language_scenario,
            profile,
        )
        try:
            with langfuse.start_as_current_observation(
                as_type="span",
                name="create_defense_intent_plan",
                input={
                    "scenario": natural_language_scenario,
                    "package": package_name,
                    "profile": profile.game,
                    "prompt": prompt,
                },
            ) as span:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=[prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        # 사내 Gateway는 Pydantic JSON Schema의
                        # additionalProperties를 받지 않으므로 provider용
                        # 최소 Schema를 따로 쓰고, 응답은 아래에서 다시
                        # DefenseIntentPlan으로 엄격 검증한다.
                        response_schema=self._defense_response_schema(),
                    ),
                )

                parsed = getattr(response, "parsed", None)
                if isinstance(parsed, BaseModel):
                    plan_data = parsed.model_dump(mode="json")
                elif isinstance(parsed, dict):
                    plan_data = dict(parsed)
                else:
                    try:
                        plan_data = json.loads(response.text)
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise PlannerResponseFormatError(
                            "AI가 올바른 디펜스 의미 Plan을 반환하지 않았습니다."
                        ) from exc

                if not isinstance(plan_data, dict):
                    raise PlannerResponseFormatError(
                        "AI 디펜스 의미 Plan이 JSON 객체가 아닙니다."
                    )

                # Request package is the source of truth.  A model must never
                # redirect execution to another installed application.
                plan_data["package"] = package_name.strip()
                try:
                    plan = DefenseIntentPlan.model_validate(plan_data)
                except ValidationError as exc:
                    raise PlannerResponseFormatError(
                        "AI 디펜스 의미 Plan이 허용된 DSL 스키마를 벗어났습니다."
                    ) from exc
                plan = self._normalize_defense_intent_plan(
                    plan,
                    natural_language_scenario,
                    profile,
                )

                span.update(output=plan.model_dump(mode="json"))

            logger.info(
                "Defense intent plan created: %s (%d semantic steps: %s)",
                plan.title,
                len(plan.steps),
                ", ".join(step.action.value for step in plan.steps),
            )
            return plan
        except Exception as exc:
            logger.error("Failed to create defense intent plan: %s", exc)
            raise

    @staticmethod
    def _normalize_defense_intent_plan(
        plan: DefenseIntentPlan,
        scenario: str,
        profile: DefenseGameProfile,
    ) -> DefenseIntentPlan:
        """Repair only deterministic transitions into the known prep state.

        The deterministic Compiler remains strict.  This boundary normalizer
        handles one common small-model omission: ``bootstrap_to_lobby`` followed
        directly by ``summon`` or ``start_wave``.  It also waits for ``prep``
        between repeated waves.  It never invents a stage unless the profile
        explicitly declares ``options.default_stage``.
        """

        if not plan.supported:
            return plan

        default_entry: DefenseIntentStep | None = None
        default_stage = profile.options.get("default_stage")
        if (
            DefenseAction.ENTER_STAGE in profile.supported_actions
            and isinstance(default_stage, dict)
            and not any(term in scenario for term in _UI_ENTRY_FOCUS_TERMS)
        ):
            entry_data = dict(default_stage)
            stage_match = (
                _STAGE_AFTER_LABEL.search(scenario)
                or _STAGE_BEFORE_LABEL.search(scenario)
            )
            if stage_match:
                entry_data["chapter"] = int(stage_match.group(1))
                entry_data["stage"] = int(stage_match.group(2))

            try:
                default_entry = DefenseIntentStep.model_validate(
                    {
                        "action": DefenseAction.ENTER_STAGE,
                        **entry_data,
                    }
                )
            except ValidationError:
                # Profile loading validates this configuration.  Leave lobby
                # transitions strict if a test double bypasses validation.
                default_entry = None

        current_state: DefenseState | None = None
        normalized_steps: list[DefenseIntentStep] = []
        inserted_entries = 0
        inserted_prep_waits = 0
        bootstrap_seen = False
        prep_actions = {
            DefenseAction.SUMMON,
            DefenseAction.START_WAVE,
        }

        for step in plan.steps:
            if (
                current_state == DefenseState.LOBBY
                and step.action in prep_actions
                and default_entry is not None
                and bootstrap_seen
                and inserted_entries == 0
            ):
                normalized_steps.append(default_entry.model_copy(deep=True))
                current_state = DefenseState.PREP
                inserted_entries += 1
            elif (
                current_state == DefenseState.WAVE_ACTIVE
                and step.action in prep_actions
                and DefenseState.PREP in profile.states
            ):
                normalized_steps.append(
                    DefenseIntentStep(
                        action=DefenseAction.WAIT_FOR_STATE,
                        state=DefenseState.PREP,
                    )
                )
                current_state = DefenseState.PREP
                inserted_prep_waits += 1

            normalized_steps.append(step)
            current_state = known_defense_state_after(step, current_state)
            if step.action == DefenseAction.BOOTSTRAP_TO_LOBBY:
                bootstrap_seen = True

        if not inserted_entries and not inserted_prep_waits:
            return plan

        logger.warning(
            "Normalized defense intent plan: inserted enter_stage=%d "
            "(%s-%s), wait_for_prep=%d",
            inserted_entries,
            default_entry.chapter if default_entry else "-",
            default_entry.stage if default_entry else "-",
            inserted_prep_waits,
        )
        if len(normalized_steps) > 50:
            logger.warning(
                "Skipped defense intent normalization because it would exceed "
                "the 50-step DSL limit"
            )
            return plan
        return DefenseIntentPlan.model_validate(
            {
                **plan.model_dump(mode="json"),
                "steps": [
                    step.model_dump(mode="json")
                    for step in normalized_steps
                ],
            }
        )

    @staticmethod
    def _defense_response_schema() -> types.Schema:
        """Provider-compatible schema; strict rules remain in Pydantic."""

        nullable_integer = lambda: types.Schema(
            type=types.Type.INTEGER,
            nullable=True,
        )
        nullable_enum = lambda values: types.Schema(
            type=types.Type.STRING,
            enum=values,
            nullable=True,
        )
        step_schema = types.Schema(
            type=types.Type.OBJECT,
            properties={
                "action": types.Schema(
                    type=types.Type.STRING,
                    enum=[action.value for action in DefenseAction],
                ),
                "chapter": nullable_integer(),
                "stage": nullable_integer(),
                "route": nullable_enum(
                    [route.value for route in StageEntryRoute]
                ),
                "count": nullable_integer(),
                "speed": types.Schema(
                    type=types.Type.STRING,
                    nullable=True,
                ),
                "state": nullable_enum(
                    [state.value for state in DefenseState]
                ),
                "timeout_seconds": nullable_integer(),
            },
            required=["action"],
            property_ordering=[
                "action",
                "chapter",
                "stage",
                "route",
                "count",
                "speed",
                "state",
                "timeout_seconds",
            ],
        )
        return types.Schema(
            type=types.Type.OBJECT,
            properties={
                "title": types.Schema(type=types.Type.STRING),
                "description": types.Schema(type=types.Type.STRING),
                "package": types.Schema(type=types.Type.STRING),
                "supported": types.Schema(type=types.Type.BOOLEAN),
                "unsupported_reason": types.Schema(type=types.Type.STRING),
                "steps": types.Schema(
                    type=types.Type.ARRAY,
                    items=step_schema,
                ),
                "expected_results": types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(type=types.Type.STRING),
                ),
            },
            required=["title", "supported", "steps"],
            property_ordering=[
                "title",
                "description",
                "package",
                "supported",
                "unsupported_reason",
                "steps",
                "expected_results",
            ],
        )

    @classmethod
    def _normalize_plan_data(
        cls,
        plan_data: Any,
        scenario: str,
        package_name: str,
    ) -> Dict[str, Any]:
        """모델 응답을 단일 TestPlan 객체 형태로 정규화한다."""
        if isinstance(plan_data, BaseModel):
            plan_data = plan_data.model_dump()

        if isinstance(plan_data, dict):
            if "steps" in plan_data:
                return plan_data

            for key in ("plan", "test_plan", "result"):
                nested = plan_data.get(key)
                if nested is not None:
                    return cls._normalize_plan_data(
                        nested, scenario, package_name
                    )

            for key in ("plans", "test_plans", "test_cases"):
                nested = plan_data.get(key)
                if nested is not None:
                    return cls._normalize_plan_data(
                        nested, scenario, package_name
                    )

        if isinstance(plan_data, list):
            if plan_data and all(
                isinstance(step, dict) and "action" in step
                for step in plan_data
            ):
                title = next(
                    (line.strip() for line in scenario.splitlines() if line.strip()),
                    "생성된 테스트",
                )[:80]
                return {
                    "title": title,
                    "description": scenario.strip(),
                    "package": package_name,
                    "steps": plan_data,
                    "expected_results": [],
                    "required_tab": None,
                }

            if len(plan_data) == 1:
                return cls._normalize_plan_data(
                    plan_data[0], scenario, package_name
                )

        response_type = type(plan_data).__name__
        logger.warning("Unexpected planner response shape: %s", response_type)
        raise PlannerResponseFormatError(
            "AI가 단일 테스트 계획이 아닌 형식을 반환했습니다. 다시 생성해 주세요."
        )
    
    def _build_planner_prompt(self, scenario: str, package_name: str,
                              template_library: str = "",
                              device_catalog: str = "") -> str:
        """플래너 프롬프트 생성"""
        catalog_section = ""
        if device_catalog.strip():
            catalog_section = f"""
**이 게임에서 실제로 사용 가능한 치트/프로퍼티 (앱 내부 v2 API에서 수집됨):**
아래 목록은 이 패키지의 빌드에서 실기기로 수집한 것이다. 여기 있는 id만 사용하고,
없는 id는 절대 지어내지 마라. 인자는 표기된 타입·범위·enum 값을 지켜라.

{device_catalog}

**치트 우선 설계 규칙 (중요):**
- 화면을 여러 번 눌러야 만들 수 있는 **사전 상태**(특정 웨이브/스테이지 진입, 재화 지급,
  몬스터 소환, 무적, 튜토리얼 스킵 등)는 UI 탭 대신 `call_cheat` / `set_property`로 만들어라.
  UI 조작은 느리고 계정 진행도에 따라 흐름이 달라져 깨지기 쉽다.
- 다만 **검증(판정) 자체는 화면을 보는 `verify` / `read_text` / `read_screen`으로 하라.**
  치트로 상태를 만들고 치트로 확인하면 실제 UI가 맞게 동작하는지 증명되지 않는다.
- 재화 지급/소비처럼 "무엇이 실제로 반영되는가"가 검증 대상이면, 그 판정 경로는 반드시
  UI를 정식으로 거쳐야 한다. 치트로 시작한 판이 정식 진행으로 인정되지 않는 게임도 있다.
- 상태를 바꾸는 `set_property`를 썼으면 시나리오 끝에 원래 값으로 되돌리는 스텝을 넣어라.
- ⚠️ **치트/프로퍼티는 씬 단위로 등록된다.** id 접두사/카테고리가 씬을 뜻한다
  (예: `ingame.*`=전투 화면, `outgame.*`=로비, `common.*`/`debug.*`=공용).
  전투 화면 치트는 전투에 진입한 뒤에 호출해야 하고, 로비에서 부르면 `not_found`로 실패한다.
  따라서 씬 전용 치트를 쓰기 전에 그 씬으로 들어가는 스텝을 먼저 배치하라.
"""

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
{catalog_section}{library_section}

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
   - params.scene: "ingame" / "outgame" 등을 주면 Vision 대신 앱 내부 v2 치트 목록의
     id 접두사로 씬을 판정한다(Vision 호출 0회, 화면 해석 오차 없음).
     "지금 로비인가 전투인가"를 확인하는 스텝은 반드시 이 방식을 쓰라.
     씬 접두사는 위 치트 카탈로그 id의 앞부분을 그대로 쓴다. "a|b"로 여러 씬 허용.
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
       "expect_delta_from": "변수명", // 앞서 save_as로 저장한 값을 그대로 기대 증감량으로 사용.
                                      // 기대값을 미리 알 수 없는 재화 검증에 쓴다 —
                                      // 예: 결과 화면 보상 수치를 저장해두고 보유 재화가
                                      // 정확히 그만큼 늘었는지 판정
       "delta_sign": -1,              // expect_delta_from의 부호. 기본 1(증가), 소비 검증이면 -1
       "delta_tolerance": 0           // expect_delta/expect_delta_from 허용 오차 (기본 0 = 정확히 일치)
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

13. `call_cheat` - 앱 내부 v2 치트 API로 UI 조작 없이 게임 상태를 바꾼다
    - target 또는 params.id: 치트 id (예: "ingame.stage.go_to_wave")
    - params.args: 치트 인자 객체 (예: {{"targetWave": 8}}) — 인자가 없으면 생략
    - params.wait_seconds: 치트 적용/연출 대기 시간
    - params.not_found_ok: true면 현재 씬에 그 치트가 없을 때(not_found)도 통과 처리
    - ⚠️ 치트는 씬 단위로 등록된다. 인게임 치트(`ingame.*`)는 전투 화면에서만, 아웃게임
      치트(`outgame.*`)는 로비에서만 목록에 잡힌다 — 해당 씬에 들어간 뒤 호출해야 한다.
    - UI로 재현하기 어려운 사전 상태(목표 웨이브, 몬스터 소환, 재화 지급 등)를 만드는 용도다.
      검증 자체는 화면을 보는 verify/read_text로 하라.

14. `set_property` - v2 프로퍼티에 값을 쓴다 (쓰기 가능한 항목만)
    - target 또는 params.id: 프로퍼티 id (예: "ingame.player.invincible_state")
    - params.value: 타입에 맞는 값 (bool은 true/false, 숫자는 숫자, enum은 이름 문자열)
    - 쓰기 불가 항목은 실패한다.

15. `check_property` - v2 프로퍼티 현재값을 읽어 검증한다 (화면에 안 보이는 내부 값 판정용)
    - target 또는 params.id: 프로퍼티 id
    - params.expect_value: 기대값 (bool/숫자/문자열)
    - read_text와 동일하게 params.save_as / compare_with / expect_delta / expect_changed 사용 가능
    - params.label: 리포트에 표시할 이름 (생략 시 프로퍼티 id)

16. `repeat_until` - 조건이 만족될 때까지 하위 스텝 묶음을 반복
    - params.steps: 반복할 스텝 목록 (일반 스텝과 같은 스키마)
    - params.until_visible / until_hidden: Vision으로 볼 종료 조건
    - params.until_scene: 씬 접두사로 볼 종료 조건 (Vision 호출 없음)
    - params.max_iterations(기본 20) / timeout_seconds / check_every / strict(기본 false)
    - "N웨이브를 반복해서 진행", "목표 화면이 나올 때까지 같은 동작 반복"처럼 횟수가
      가변이거나 많은 흐름은 스텝을 수십 개로 펼치지 말고 이 액션 하나로 표현하라.
    - 하위 스텝 실패는 기본적으로 무시된다(재화 부족·버튼 미노출 등 정상 상황).

17. `dismiss_popups` - 허용된 팝업을 반복해서 닫고 최종 화면까지 도달
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
15. 플랫폼 계정 연동에서 `기존 데이터 / 현재 데이터` 선택 팝업은 해당 플랫폼 계정에
    기존 게임 데이터가 있을 때만 나타난다. 시나리오가 "기존 데이터가 있는 계정"을
    명시한 경우에만 이 팝업과 선택 스텝을 필수로 생성하라. "기존 데이터가 없는 신규 계정"이면
    계정 선택 후 선택 팝업 없이 바로 연동 완료 화면으로 전환되는 후조건을 사용하라.

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

    @staticmethod
    def _build_defense_intent_prompt(
        scenario: str,
        profile: DefenseGameProfile,
    ) -> str:
        """Build a short prompt that selects semantic actions only."""

        allowed = ", ".join(action.value for action in profile.supported_actions)
        return f"""
당신은 모바일 디펜스 게임 QA의 Intent Compiler입니다.
자연어 시나리오를 좌표나 UI 구현이 아닌, 아래 허용된 의미 액션으로만 변환하세요.

입력 시나리오:
{scenario}

게임 프로필:
{profile.planner_context()}

허용 액션:
{allowed}

규칙:
1. find_and_tap, verify, wait, call_cheat 같은 저수준 실행 액션은 절대 출력하지 마세요.
2. 앱을 초기 상태에서 시작해야 하면 bootstrap_to_lobby를 첫 단계로 선택하세요.
3. 스테이지 진입 자체가 검증 대상이 아니면 enter_stage.route="cheat"를 사용하세요.
4. 에너지 소비, 정식 입장, 정식 보상 지급이 검증 대상이면 enter_stage.route="ui"를 사용하세요.
5. summon.count는 실제 요청 횟수만 넣고 1~20 범위를 지키세요.
6. 각 의미 액션의 클릭 후 검증은 게임 프로필 Compiler가 자동으로 추가하므로 중복 생성하지 마세요.
7. 허용 액션만으로 시나리오를 정확히 표현할 수 없으면 supported=false,
   steps=[]로 두고 unsupported_reason에 부족한 기능을 구체적으로 적으세요.
8. package는 반드시 "{profile.package}"로 유지하세요.
9. 추측한 게임 규칙이나 존재하지 않는 기능을 만들지 마세요.
10. 상태 순서를 지키세요: bootstrap_to_lobby 뒤 상태는 lobby이고,
    summon과 start_wave는 prep 상태에서만 실행할 수 있으므로 그 전에
    enter_stage를 반드시 넣으세요. start_wave 뒤 상태는 wave_active입니다.
11. 시나리오에 스테이지가 없으면 게임 프로필의 options.default_stage를 사용하세요.
12. 여러 웨이브를 진행할 때는 각 start_wave 사이에
    wait_for_state(state="prep")를 넣으세요. wave_active 대기는 전투 시작 확인일 뿐,
    다음 웨이브를 시작할 수 있는 prep 상태 전환이 아닙니다.

JSON Schema에 맞는 객체만 반환하세요.
""".strip()
    
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
