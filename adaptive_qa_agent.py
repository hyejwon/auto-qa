"""Adaptive QA agent loop.

Runs a test case, evaluates failures, patches brittle test steps, and retries.
This is the agentic layer above QAOrchestrator; QAOrchestrator remains the
deterministic executor.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from google.genai import types
from pydantic import BaseModel, Field

from config import Config
from llm_client import build_genai_client
from qa_orchestrator import QAOrchestrator
from test_manager import ActionType, TestCase

logger = logging.getLogger(__name__)

# LLM이 만든 패치 스텝이 실제 실행 가능한 액션인지 검증하기 위한 집합
VALID_ACTIONS = {a.value for a in ActionType}


class AdaptiveRunRequest(BaseModel):
    session_id: str
    title: str
    package: str
    steps: list[dict[str, Any]]
    max_iterations: int = 3
    goal: str = ""
    reset_app_each_iteration: bool = True


class StepPatch(BaseModel):
    op: str
    index: int
    step: Optional[dict[str, Any]] = None
    updates: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class AdaptiveQARunner:
    def __init__(self, config: Config = Config()):
        self.config = config
        self.client = build_genai_client()
        self.model = config.gemini.model

    def run(self, req: AdaptiveRunRequest) -> dict[str, Any]:
        steps = copy.deepcopy(req.steps)
        steps = self._prepare_iteration_steps(steps, req.package, req.reset_app_each_iteration)
        timeline: list[dict[str, Any]] = []
        max_iterations = max(1, min(req.max_iterations, 5))

        for iteration in range(1, max_iterations + 1):
            run_steps = copy.deepcopy(steps)
            result = self._run_once(req, run_steps, iteration)
            result_dict = result.model_dump()

            item: dict[str, Any] = {
                "iteration": iteration,
                "status": result.status,
                "steps": run_steps,
                "result": result_dict,
                "analysis": None,
                "patches": [],
            }

            if result.status == "PASS":
                item["analysis"] = {
                    "summary": "테스트가 성공했습니다.",
                    "failure_reason": "",
                    "patch_rationale": "",
                }
                timeline.append(item)
                return {
                    "status": "PASS",
                    "session_id": req.session_id,
                    "iterations": timeline,
                    "final_steps": steps,
                    "final_result": result_dict,
                }

            analysis = self.analyze_and_plan(req, run_steps, result_dict)
            patches = [StepPatch(**p).model_dump() for p in analysis.get("patches", [])]
            item["analysis"] = analysis
            item["patches"] = patches
            timeline.append(item)

            if not patches:
                break

            next_steps = self.apply_patches(steps, patches)
            if next_steps == steps:
                logger.info("Adaptive QA patch produced no changes; stopping.")
                break
            steps = next_steps

        return {
            "status": "FAIL",
            "session_id": req.session_id,
            "iterations": timeline,
            "final_steps": steps,
            "final_result": timeline[-1]["result"] if timeline else None,
        }

    def _run_once(self, req: AdaptiveRunRequest, steps: list[dict[str, Any]], iteration: int):
        testcase = TestCase(**{
            "id": f"{req.session_id}_iter_{iteration}",
            "title": f"{req.title} / adaptive iter {iteration}",
            "description": req.goal or "",
            "package": req.package,
            "steps": steps,
            "expected_results": [],
            "preconditions": [],
        })
        return QAOrchestrator(self.config).run_test(testcase.id, testcase_override=testcase)

    def _prepare_iteration_steps(
        self,
        steps: list[dict[str, Any]],
        package: str,
        reset_app_each_iteration: bool,
    ) -> list[dict[str, Any]]:
        prepared = copy.deepcopy(steps)
        if not reset_app_each_iteration:
            return prepared

        has_close = any(s.get("action") == "close_app" for s in prepared[:1])
        if not has_close:
            prepared.insert(0, {
                "action": "close_app",
                "target": package,
                "params": {"package": package},
                "description": "앱 초기화: 종료",
                "timeout": 5,
                "retry": 1,
            })

        has_launch = any(s.get("action") == "launch_app" for s in prepared[:2])
        if not has_launch:
            prepared.insert(1, {
                "action": "launch_app",
                "target": package,
                "params": {"package": package},
                "description": "앱 실행",
                "timeout": 20,
                "retry": 1,
            })
        return prepared

    def analyze_and_plan(
        self,
        req: AdaptiveRunRequest,
        steps: list[dict[str, Any]],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        heuristic = self._heuristic_repair(req, steps, result)
        if heuristic:
            return heuristic

        screenshot_path = self._latest_screenshot(result)
        prompt = self._build_repair_prompt(req, steps, result)

        try:
            contents: list[Any] = [prompt]
            if screenshot_path and screenshot_path.exists():
                contents.append(self._image_part(screenshot_path))

            response = self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            data = json.loads(response.text)
            return self._sanitize_analysis(data, len(steps))
        except Exception as exc:
            logger.warning("Adaptive QA LLM repair failed: %s", exc)
            return {
                "summary": "LLM 기반 수정안 생성에 실패했습니다.",
                "failure_reason": str(exc),
                "patch_rationale": "자동 재시도 중단",
                "patches": [],
            }

    def _heuristic_repair(
        self,
        req: AdaptiveRunRequest,
        steps: list[dict[str, Any]],
        result: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        error = result.get("error_message") or ""
        if req.package != "com.percent.aos.cooptd":
            return None

        guest_failure = "Guest login" in error or "게스트 로그인" in error
        wrong_popup = "게스트 로그인 데이터 유실 안내 팝업" in error
        if not (guest_failure and wrong_popup):
            return None

        patches: list[dict[str, Any]] = []
        guest_index = self._find_step_index(steps, target_patterns=["Guest login", "게스트 로그인"])
        if guest_index >= 0:
            patches.append({
                "op": "update_step",
                "index": guest_index,
                "updates": {
                    "target": "게스트 로그인",
                    "params": {"expect_visible": "동의합니다"},
                    "timeout": 20,
                    "retry": 2,
                    "description": "게스트 로그인 버튼 클릭",
                },
                "reason": "cooptd의 게스트 로그인은 데이터 유실 팝업이 아니라 약관 알림을 표시합니다.",
            })
            patches.append({
                "op": "insert_after",
                "index": guest_index,
                "step": {
                    "action": "find_and_tap",
                    "target": "동의합니다",
                    "description": "약관 알림 동의",
                    "params": {"expect_visible": "탭하여 넘어가기"},
                    "timeout": 45,
                    "retry": 2,
                },
                "reason": "약관 알림에서 동의해야 게스트 로그인 플로우가 계속 진행됩니다.",
            })

        return {
            "summary": "실패 원인은 앱별 기대 팝업 불일치입니다.",
            "failure_reason": error,
            "patch_rationale": "cooptd 실제 흐름에 맞춰 게스트 로그인 후 기대값을 '동의합니다'로 바꾸고 약관 동의 스텝을 추가합니다.",
            "patches": patches,
        }

    def apply_patches(self, steps: list[dict[str, Any]], patches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        next_steps = copy.deepcopy(steps)
        offset = 0
        for raw in patches:
            patch = StepPatch(**raw)
            index = patch.index + offset
            if patch.op == "update_step" and 0 <= index < len(next_steps):
                next_steps[index] = self._deep_merge(next_steps[index], patch.updates)
            elif patch.op == "insert_after" and patch.step and -1 <= index < len(next_steps):
                if not self._has_equivalent_step(next_steps, patch.step):
                    next_steps.insert(index + 1, patch.step)
                    offset += 1
            elif patch.op == "replace_step" and patch.step and 0 <= index < len(next_steps):
                next_steps[index] = patch.step
            elif patch.op == "delete_step" and 0 <= index < len(next_steps):
                next_steps.pop(index)
                offset -= 1
        return next_steps

    def _build_repair_prompt(
        self,
        req: AdaptiveRunRequest,
        steps: list[dict[str, Any]],
        result: dict[str, Any],
    ) -> str:
        allowed = ", ".join(sorted(VALID_ACTIONS))
        return f"""당신은 모바일 QA 자동화 self-healing agent입니다.
실패한 테스트 결과와 현재 화면을 보고, 테스트 스텝을 어떻게 수정해야 다음 실행에서 성공할지 판단하세요.

목표:
{req.goal or req.title}

패키지:
{req.package}

현재 스텝 JSON:
{json.dumps(steps, ensure_ascii=False, indent=2, default=str)}

실행 결과:
{json.dumps(self._compact_result(result), ensure_ascii=False, indent=2, default=str)}

반드시 JSON만 반환하세요.
스키마:
{{
  "summary": "실패 상황 요약",
  "failure_reason": "근본 원인",
  "patch_rationale": "수정 근거",
  "patches": [
    {{
      "op": "update_step|insert_after|replace_step|delete_step",
      "index": 0,
      "updates": {{}},
      "step": {{}},
      "reason": "이 패치가 필요한 이유"
    }}
  ]
}}

규칙:
- action 은 반드시 다음 중 하나여야 합니다: {allowed}. 그 외 값(예: wait_for_element)은 절대 사용하지 마세요.
- "요소가 나타날 때까지 대기"가 필요하면 새 action 을 만들지 말고 verify(기대 요소) 또는 wait(초) + expect_visible 로 표현하세요.
- 앱 버그를 테스트 스텝으로 억지 우회하지 마세요.
- 단순 대기시간 증가는 마지막 수단입니다.
- 실패가 expect_visible/expect_hidden 불일치라면 실제 화면에 보이는 안정적인 다음 UI로 기대값을 바꾸세요.
- 로그인 성공 검증은 버튼 탭 자체가 아니라 로그인 이후 화면의 특징 요소로 검증하세요.
- patch index는 현재 스텝 JSON의 0-based index입니다.
"""

    def _sanitize_analysis(self, data: dict[str, Any], step_count: int) -> dict[str, Any]:
        patches = []
        for item in data.get("patches", []):
            try:
                patch = StepPatch(**item)
            except Exception:
                continue
            if patch.index < -1 or patch.index >= step_count:
                continue
            if patch.op not in {"update_step", "insert_after", "replace_step", "delete_step"}:
                continue
            # LLM이 존재하지 않는 액션(예: wait_for_element)을 만들면 다음 반복의
            # TestCase 생성 시 enum 검증에 걸려 크래시하므로 여기서 걸러낸다.
            if not self._patch_action_valid(patch):
                logger.warning(
                    "Adaptive QA: 유효하지 않은 액션의 패치를 무시합니다 (op=%s, index=%s).",
                    patch.op, patch.index,
                )
                continue
            patches.append(patch.model_dump(exclude_none=True))
        return {
            "summary": str(data.get("summary") or ""),
            "failure_reason": str(data.get("failure_reason") or ""),
            "patch_rationale": str(data.get("patch_rationale") or ""),
            "patches": patches,
        }

    def _compact_result(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": result.get("status"),
            "error_message": result.get("error_message"),
            "steps_passed": result.get("steps_passed"),
            "steps_executed": result.get("steps_executed"),
            "step_results": result.get("step_results") or [],
            "eval_output": result.get("eval_output"),
            "screenshots": result.get("screenshots", [])[-3:],
        }

    def _latest_screenshot(self, result: dict[str, Any]) -> Optional[Path]:
        screenshots = result.get("screenshots") or []
        for path in reversed(screenshots):
            p = Path(path)
            if p.exists():
                return p
        return None

    @staticmethod
    def _image_part(image_path: Path) -> types.Part:
        return types.Part.from_bytes(data=image_path.read_bytes(), mime_type="image/png")

    @staticmethod
    def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        merged = copy.deepcopy(base)
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = AdaptiveQARunner._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _patch_action_valid(patch: StepPatch) -> bool:
        """패치가 새 스텝/액션을 도입한다면 실행 가능한 액션인지 확인한다.

        - insert_after / replace_step: patch.step.action 이 유효해야 한다.
        - update_step: updates 에 action 이 있으면 유효해야 한다. (없으면 기존 유지)
        """
        if patch.op in {"insert_after", "replace_step"}:
            action = (patch.step or {}).get("action")
            return action in VALID_ACTIONS
        if patch.op == "update_step" and "action" in patch.updates:
            return patch.updates.get("action") in VALID_ACTIONS
        return True

    @staticmethod
    def _find_step_index(steps: list[dict[str, Any]], target_patterns: list[str]) -> int:
        for idx, step in enumerate(steps):
            target = str(step.get("target") or "")
            desc = str(step.get("description") or "")
            haystack = f"{target} {desc}"
            if any(pattern in haystack for pattern in target_patterns):
                return idx
        return -1

    @staticmethod
    def _has_equivalent_step(steps: list[dict[str, Any]], step: dict[str, Any]) -> bool:
        wanted_action = step.get("action")
        wanted_target = normalize_text(step.get("target") or "")
        for existing in steps:
            if existing.get("action") == wanted_action and normalize_text(existing.get("target") or "") == wanted_target:
                return True
        return False


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()
