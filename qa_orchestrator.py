import os
import re
import shutil
import threading
import json
import unicodedata
from pathlib import Path
from datetime import datetime
from typing import Any, Optional
import time
import logging

from PIL import Image, ImageChops, ImageDraw
from langfuse_disabled import get_client

from config import Config
from adb_controller import ADBController, parse_ui_nodes
from vision_agent import GeminiVisionAgent
from test_manager import TestCaseManager, TestResult, ActionType, TestCase, TestStep
from planner_node import PlannerNode
from unity_api_client import UnityAPIClient
import unity_catalog
from element_cache import ElementCache, CommonTapCache, CachedElement
from eval_agent import evaluate_result_dict
from sr_debugger import SRDebuggerController
from dotenv import load_dotenv

load_dotenv()
langfuse = get_client()
logger = logging.getLogger(__name__)

# 하단 네비게이션처럼 화면이 바뀌어도 항상 같은 자리에 있는 탭의 좌표 바로가기.
# find_and_tap의 params.tab_shortcut에 이름을 넣으면 vision 호출 없이 바로 탭한다.
# 탭 후 검증 실패 시에는 중복 입력을 막기 위해 검증만 재시도한다.
# 좌표는 실기기(1080x2316)에서 element_cache.db에 쌓인 값 기준 (2026-07-22 확인).
FIXED_TAB_COORDS: dict[str, dict[str, dict[str, tuple[int, int]]]] = {
    "com.percent.aos.cooptd": {
        "1080x2316": {
            "상점": (113, 2209),
            "마물": (330, 2205),
            "전투": (539, 2230),
            "유물": (765, 2208),
            "뽑기": (970, 2207),
        },
    },
}


class QAOrchestrator:
    """QA 자동화 오케스트레이터"""

    # 공통 캐시에 등록된 요소명 — Vision 감지 시 현재 해상도로 자동 등록
    COMMON_TAP_ELEMENTS = {"동의합니다", "개인정보처리방침", "이용약관"}
    
    
    POST_TAP_DELAY_SEC = 0.7
    POST_TAP_VERIFY_TIMEOUT_SEC = 2.0
    POST_TAP_POLL_INTERVAL_SEC = 0.3
    STABILITY_POLL_INTERVAL_SEC = 0.5
    STABILITY_TIMEOUT_SEC = 10.0
    STABILITY_THRESHOLD = 0.01
    # expect_visible/hidden 검증 시도 상한 — 시도마다 Vision 호출이 나가므로
    # step.timeout까지 무한 반복하지 않고 이 횟수에서 끊는다
    VERIFY_MAX_ATTEMPTS = 3

    def __init__(self, config: Config = Config(), device_id: Optional[str] = None):
        self.config = config
        self.adb = ADBController(device_id)
        self.unity = UnityAPIClient(
            adb_controller=self.adb,
            project=config.gemini.project,
            location=config.gemini.location,
            model=config.gemini.model,
            temperature=config.gemini.temperature,
            # 명시적 URL이 없으면 None → UnityAPIClient가 디바이스별 동적 포트로 포워딩
            base_url=os.getenv("UNITY_API_URL") or None
        )
        self.sr_debugger = SRDebuggerController(adb=self.adb, config=config)
        self.vision = GeminiVisionAgent(
            project=config.gemini.project,
            location=config.gemini.location,
            model=config.gemini.model
        )
        self.vision_lite = GeminiVisionAgent(
            project=config.gemini.project,
            location=config.gemini.location,
            model=config.gemini.vision_lite_model
        )
        self.test_manager = TestCaseManager(config.paths.testcases_dir)
        self.planner = PlannerNode(
            project=config.gemini.project,
            location=config.gemini.location,
            model=config.gemini.planner_model
        )
        self.cache = ElementCache(config.paths.cache_db)
        self.common_cache = CommonTapCache(config.paths.common_cache_db)
        self._resolution = f"{self.adb.width}x{self.adb.height}"
        # 병렬 실행 시 파일명 충돌 방지용 디바이스 태그 (예: 192.168.0.5:5555 → 192.168.0.5_5555)
        self._file_tag = "".join(
            c if c.isalnum() or c in "._-" else "_" for c in (self.adb.device_id or "device")
        )
        self._current_package: str = ""
        self._current_screen_type: str = ""
        self._last_failure_reason: str = ""
        self._last_pass_detail: str = ""  # 통과 스텝의 근거 (읽은 값, 증감량 등)
        self._package_apk_map: dict[str, str] = {}

    def run_natural_language_test(
        self,
        scenario: str,
        package_name: str = "",
        save_yaml: bool = True
    ) -> TestResult:
        logger.info("=" * 60)
        logger.info("Natural Language Test Execution Started")
        logger.info("=" * 60)
        logger.info(f"Scenario: {scenario}")

        logger.info("\n[Step 1] Generating test plan from natural language...")
        test_plan, yaml_path = self.planner.generate_and_save(
            scenario=scenario,
            output_dir=self.config.paths.testcases_dir,
            package_name=package_name
        )

        if save_yaml:
            logger.info(f"Test plan saved to: {yaml_path}")

        logger.info("\n[Step 2] Loading generated test case...")
        self.test_manager._load_testcases()
        test_id = yaml_path.stem

        logger.info(f"\n[Step 3] Executing test: {test_id}")
        result = self.run_test(test_id)

        return result
    
    def run_test(
        self,
        test_id: str,
        stop_event: threading.Event | None = None,
        testcase_override: TestCase | None = None,
    ) -> TestResult:
        """단일 테스트 실행. testcase_override가 주어지면 파일 대신 해당 객체를 사용."""

        if testcase_override is not None:
            testcase = testcase_override
        else:
            testcase = self.test_manager.get_testcase(test_id)
        if not testcase:
            raise ValueError(f"Test case not found: {test_id}")

        total = len(testcase.steps)

        with langfuse.start_as_current_observation(
            as_type="span",
            name="run_test",
            input={"test_id": test_id, "title": testcase.title, "package": testcase.package, "steps": total},
        ) as test_span:

            logger.info("━" * 52)
            logger.info(f"  테스트 시작: {testcase.title}")
            logger.info(f"  패키지: {testcase.package}  |  스텝 수: {total}")
            logger.info("━" * 52)

            self._package_apk_map = self._load_package_apk_map()
            self._current_package = testcase.package or ""
            self._collect_device_catalog("run_start")
            self._current_screen_type = ""
            self._stop_event = stop_event  # _wait_for_screen_stable 에서 참조

            # stayon은 화면 꺼짐만 방지하며 보안 키가드는 해제하지 못한다.
            # PIN 화면을 테스트 화면으로 오판하기 전에 실행 시작 시 잠금을 처리한다.
            if not self.adb.ensure_screen_on():
                raise RuntimeError(
                    "테스트 기기 화면 잠금을 해제하지 못했습니다. "
                    ".env에 ADB_UNLOCK_PIN을 설정하거나 QA 기기의 화면 잠금을 제거하세요."
                )

            # preconditions: google_account:<email> 형식 처리
            for precond in testcase.preconditions:
                if precond.startswith("google_account:"):
                    email = precond.split(":", 1)[1].strip()
                    if not self.adb.ensure_google_account(email):
                        raise RuntimeError(
                            f"Precondition 실패: Google 계정 '{email}'이 디바이스에 없습니다. "
                            "설정 > 계정 > Google에서 수동 등록 후 재실행하세요."
                        )

            result = TestResult(
                test_id=testcase.id,
                title=testcase.title,
                status="RUNNING",
                start_time=datetime.now()
            )
            # 한 실행 안에서만 쓰는 제어 플래그. 화면을 매 스텝마다 다시 Vision으로
            # 확인하지 않고 앞 스텝의 실제 실행/통과 결과로 후속 분기를 결정한다.
            # read_text 등이 저장하는 사용자 결과 context와 섞이지 않도록 별도로 둔다.
            flow_flags: set[str] = set()
            try:
                for idx, step in enumerate(testcase.steps):
                    if stop_event and stop_event.is_set():
                        logger.warning("⏹️ 사용자 중단 — 테스트를 중지합니다.")
                        result.status = "FAIL"
                        result.error_message = "사용자에 의해 중단됨"
                        break

                    self._last_failure_reason = ""
                    self._last_pass_detail = ""
                    self._last_tap_performed = False
                    self._last_step_evidence = None
                    step_skipped = False
                    skip_reason = ""
                    label = step.description or step.action
                    self._current_step_number = idx + 1
                    self._current_step_label = str(label)
                    self._current_step_action = getattr(step.action, "value", str(step.action))
                    target_info = f"  → 대상: {step.target}" if step.target else ""
                    logger.info("")
                    logger.info(f"┌─ [{idx + 1}/{total}] {label}")
                    if target_info:
                        logger.info(f"│  {target_info.strip()}")

                    params = step.params or {}
                    run_if_flag = str(params.get("run_if_flag") or "").strip()
                    skip_if_flag = str(params.get("skip_if_flag") or "").strip()
                    flag_skip_reason = ""
                    if run_if_flag and run_if_flag not in flow_flags:
                        flag_skip_reason = f"선행 흐름 '{run_if_flag}' 미실행"
                    elif skip_if_flag and skip_if_flag in flow_flags:
                        flag_skip_reason = f"완료 흐름 '{skip_if_flag}' 충족"

                    if flag_skip_reason:
                        step_skipped = True
                        skip_reason = f"실행 흐름 조건 — {flag_skip_reason}"
                        logger.info(f"│  → {flag_skip_reason} — Vision 확인 없이 스텝 건너뜀")
                        logger.info("└─ ⏭️ 건너뜀 (실행 흐름 조건)")
                        result.steps_executed += 1
                        result.steps_skipped += 1
                        result.step_results.append({
                            "step": idx + 1,
                            "label": label,
                            "action": getattr(step.action, "value", str(step.action)),
                            "target": step.target or "",
                            "passed": False,
                            "skipped": True,
                            "skip_reason": skip_reason,
                            "vision_confidence": 1.0,
                            "failure_reason": "",
                            "pass_reason": "",
                        })
                        self._cleanup_step_files()
                        continue

                    # skip_if_visible: 액션 종류와 무관하게(예: skip_tutorial, close_app,
                    # launch_app처럼 vision과 무관한 액션도) 지정된 대상이 이미 화면에 보이면
                    # 이 스텝 자체를 실행하지 않고 건너뛴다. "이미 끝난 상태"를 나타내는 화면이
                    # 보일 때 재부팅/치트 호출 등 불필요한 동작을 반복하지 않기 위함.
                    skip_if_visible = params.get("skip_if_visible")
                    if skip_if_visible:
                        check_path = self._capture_runtime_screenshot(prefix="skip_check")
                        check_result = self.vision_lite.find_element(
                            check_path, skip_if_visible, self.config.paths.debug_dir,
                            state_context=self._sctx,
                        )
                        if check_result.success and check_result.bbox:
                            step_skipped = True
                            skip_reason = (
                                f"이미 완료 상태 노출 — '{skip_if_visible}'"
                            )
                            logger.info(f"│  → skip_if_visible: '{skip_if_visible}' 이미 화면에 보임 — 스텝 건너뜀")
                            logger.info("└─ ⏭️ 건너뜀 (skip_if_visible 조건 충족)")
                            result.steps_executed += 1
                            result.steps_skipped += 1
                            result.step_results.append({
                                "step": idx + 1,
                                "label": label,
                                "action": getattr(step.action, "value", str(step.action)),
                                "target": step.target or "",
                                "passed": False,
                                "skipped": True,
                                "skip_reason": skip_reason,
                                "vision_confidence": check_result.confidence,
                                "failure_reason": "",
                                "pass_reason": "",
                            })
                            self._cleanup_step_files()
                            continue

                    with langfuse.start_as_current_observation(
                        as_type="span",
                        name=f"step_{idx + 1}_{step.action}",
                        input={"step": idx + 1, "action": step.action, "target": step.target, "description": label},
                    ) as step_span:
                        economy_summary_len_before = len(result.economy_summary)
                        success, confidence = self._execute_step(step, result)
                        tap_ok_verify_fail = (success == self._TAP_OK_VERIFY_FAIL)
                        success = bool(success) and not tap_ok_verify_fail
                        result.steps_executed += 1

                        if success:
                            result.steps_passed += 1
                            logger.info("└─ ✅ 완료")
                        else:
                            if step.retry > 1 and not self._uses_internal_retry(step):
                                for retry_count in range(step.retry - 1):
                                    if tap_ok_verify_fail:
                                        logger.warning(
                                            f"│  ↩ 검증 재시도 {retry_count + 1}/{step.retry - 1} (탭 성공, 검증 실패) ..."
                                        )
                                        time.sleep(2)
                                        if self._verify_find_and_tap_outcome(step, tap_source="Retry"):
                                            confidence = max(
                                                confidence,
                                                getattr(self, "_last_post_verify_confidence", 0.0),
                                            )
                                            result.steps_passed += 1
                                            success = True
                                            self._last_failure_reason = ""
                                            expected = self._to_target_list(
                                                (step.params or {}).get("expect_visible")
                                            )
                                            self._last_pass_detail = (
                                                f"'{step.target or ''}' 탭 후 검증 재시도 성공"
                                                + (f" → '{', '.join(expected)}' 노출 확인" if expected else "")
                                            )
                                            logger.info("└─ ✅ 완료 (검증 재시도 성공)")
                                            break
                                    else:
                                        logger.warning(
                                            f"│  ↩ 재시도 {retry_count + 1}/{step.retry - 1} ..."
                                        )
                                        time.sleep(2)
                                        # read_text/read_screen/read_items가 재시도마다 economy_summary에
                                        # 행을 또 추가하므로, 이전 시도 행은 버리고 마지막 시도 결과만 남긴다
                                        del result.economy_summary[economy_summary_len_before:]
                                        retry_success, _ = self._execute_step(step, result)
                                        if retry_success and retry_success != self._TAP_OK_VERIFY_FAIL:
                                            result.steps_passed += 1
                                            success = True
                                            logger.info("└─ ✅ 완료 (재시도 성공)")
                                            break
                                        elif retry_success == self._TAP_OK_VERIFY_FAIL:
                                            tap_ok_verify_fail = True

                            if not success:
                                optional_target_missing = (
                                    (step.params or {}).get("optional")
                                    and not self._last_tap_performed
                                )
                                if optional_target_missing:
                                    # 선택 스텝: 조건부 팝업처럼 안 나올 수도 있는 대상 — 실패해도 건너뛰고 계속
                                    step_skipped = True
                                    skip_reason = "선택 스텝 대상 미노출 (정상)"
                                    result.steps_skipped += 1
                                    logger.info("└─ ⏭️ 건너뜀 (선택 스텝 — 대상 미노출)")
                                else:
                                    logger.error("└─ ❌ 실패")
                                    if self._last_failure_reason:
                                        logger.error(f"│    사유: {self._last_failure_reason}")
                                    result.status = "FAIL"
                                    reason_suffix = f" — {self._last_failure_reason}" if self._last_failure_reason else ""
                                    result.error_message = f"Step {idx + 1} 실패: {label}{reason_suffix}"

                        step_span.update(output={"passed": success, "vision_confidence": confidence})

                    step_result = {
                        "step": idx + 1,
                        "label": label,
                        "action": getattr(step.action, "value", str(step.action)),
                        "target": step.target or "",
                        "passed": success,
                        "skipped": step_skipped,
                        "skip_reason": skip_reason,
                        "vision_confidence": confidence,
                        "failure_reason": "" if (success or step_skipped) else self._last_failure_reason,
                        "pass_reason": self._last_pass_detail if success else "",
                    }
                    if success:
                        set_flag = str(params.get("set_flag_on_pass") or "").strip()
                        if set_flag:
                            flow_flags.add(set_flag)
                            logger.info("│  → 실행 흐름 플래그 설정: %s", set_flag)
                    if self._last_step_evidence:
                        step_result.update(self._last_step_evidence)
                    result.step_results.append(step_result)

                    self._cleanup_step_files()

                    if result.status == "FAIL":
                        break

                if result.status != "FAIL":
                    result.status = "PASS"

            except Exception as e:
                logger.error(f"테스트 실행 오류: {e}")
                result.status = "FAIL"
                result.error_message = str(e)

            finally:
                result.end_time = datetime.now()
                duration = (result.end_time - result.start_time).total_seconds()
                logger.info("")
                logger.info("━" * 52)
                icon = "✅ PASS" if result.status == "PASS" else "❌ FAIL"
                logger.info(
                    f"  결과: {icon}  |  {result.steps_passed} 통과"
                    f" · {result.steps_skipped} 건너뜀 / {total}"
                    f"  |  {duration:.1f}초"
                )
                logger.info("━" * 52)

                try:
                    eval_output = evaluate_result_dict(result.model_dump())
                    result.eval_output = eval_output
                    logger.info(
                        f"  Eval: final_score={eval_output['final_score']} "
                        f"severity={eval_output['flow']['severity']}"
                    )
                except Exception as e:
                    logger.warning(f"Eval 실행 실패 (테스트 결과에는 영향 없음): {e}")

                if getattr(result, "cheat_log", None):
                    logger.info("─" * 52)
                    logger.info("  이 실행에서 쓰인 치트/프로퍼티 %d건", len(result.cheat_log))
                    for entry in result.cheat_log:
                        logger.info(
                            "   %2d) step %-3s %-14s %-38s %s%s",
                            entry["seq"], entry["step"], entry["kind"],
                            entry["target"], entry["detail"],
                            "" if entry["ok"] else "  ← 실패",
                        )
                self._collect_device_catalog("run_end")
                self.test_manager.save_result(result, self.config.paths.results_dir)

                test_span.update(output={
                    "status": result.status,
                    "steps_passed": result.steps_passed,
                    "steps_skipped": result.steps_skipped,
                    "steps_executed": result.steps_executed,
                    "duration": duration,
                })

            langfuse.flush()
            return result

    def _collect_device_catalog(self, phase: str) -> None:
        """현재 씬의 v2 치트/프로퍼티를 카탈로그에 누적 병합한다.

        치트는 씬 단위로 등록되므로 한 번에 전부 모이지 않는다. 실행 시작(보통 로비)과
        종료(테스트가 도달한 화면) 두 시점에서 찍으면, 테스트를 돌릴수록 그 게임의
        카탈로그가 자동으로 채워진다 — 플래너가 자연어에서 바로 치트 스텝을 만들 때 쓰는
        근거 데이터다. 실패해도 테스트 진행에는 영향을 주지 않는다.
        """
        if not self._current_package:
            return
        try:
            snapshot = self.unity.catalog_snapshot_v2()
            if not snapshot.get("cheats") and not snapshot.get("properties"):
                return
            counts = unity_catalog.merge_snapshot(self._current_package, snapshot)
            if counts.get("new"):
                logger.info(
                    "[%s] 치트 카탈로그 갱신: 신규 %d개 (누적 치트 %d / 프로퍼티 %d)",
                    phase, counts["new"], counts["cheats"], counts["properties"],
                )
        except Exception as exc:
            logger.debug("치트 카탈로그 수집 건너뜀 (%s): %s", phase, exc)

    def _execute_step(self, step, result: TestResult) -> tuple[bool, float]:
        """개별 스텝 실행 — (success, vision_confidence) 반환"""
        self._refresh_state_context(step)
        screenshot_path: Optional[Path] = None
        if step.action in {
            ActionType.READ_TEXT,
            ActionType.READ_ITEMS,
            ActionType.READ_SCREEN,
        }:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            screenshot_path = (
                self.config.paths.screenshots_dir
                / f"screenshot_{self._file_tag}_{timestamp}.png"
            )
            self.adb.screenshot(screenshot_path)
            self._publish_live_screenshot(screenshot_path)
            result.screenshots.append(str(screenshot_path))

        try:
            if step.action == ActionType.FIND_AND_TAP:
                result_val = self._find_and_tap(step)
                if result_val == self._TAP_OK_VERIFY_FAIL:
                    return self._TAP_OK_VERIFY_FAIL, 0.0
                return bool(result_val), 1.0

            elif step.action == ActionType.SWIPE:
                params = step.params
                ok = self.adb.swipe(
                    params.get("x1", 0), params.get("y1", 0),
                    params.get("x2", 0), params.get("y2", 0)
                )
                return ok, 1.0

            elif step.action == ActionType.SCROLL:
                return self._scroll_action(step), 1.0

            elif step.action == ActionType.WAIT:
                return self._execute_wait_step(step), 1.0

            elif step.action == ActionType.BACK:
                return self._execute_back_step(step), 1.0

            elif step.action == ActionType.DISMISS_POPUPS:
                return self._dismiss_popups(step), 1.0

            elif step.action == ActionType.HOME:
                return self.adb.press_home(), 1.0

            elif step.action == ActionType.LAUNCH_APP:
                package = step.params.get("package")
                if package:
                    self.adb.shell(f"pm grant {package} android.permission.POST_NOTIFICATIONS ")
                    self._current_screen_type = ""
                    launched = self.adb.launch_app(package)
                    if launched:
                        self._wait_for_screen_stable()
                        # 화면 안정 판정이 너무 일찍 끝나 다음 스텝이 아직 다 안 뜬 화면을
                        # 만나는 경우가 있어, 다음 스텝 진행 전 최소 2초는 강제로 대기한다.
                        time.sleep(2)
                    else:
                        self._last_failure_reason = f"앱 실행 실패: {package}"
                    return launched, 1.0
                self._last_failure_reason = "launch_app: package가 지정되지 않음"
                return False, 1.0

            elif step.action == ActionType.CLOSE_APP:
                package = step.params.get("package")
                if package:
                    self._current_screen_type = ""
                    return self.adb.close_app(package), 1.0
                return False, 1.0

            elif step.action == ActionType.VERIFY:
                # params.scene이 있으면 화면 판독 대신 앱 내부 v2 치트 목록으로 씬을 판정한다
                # (Vision 호출 0회). "지금 로비인가 전투인가"류 확인은 이 경로가 정확하고 싸다.
                if (step.params or {}).get("scene"):
                    return self._verify_scene_step(step), 1.0
                fresh_path = self._capture_runtime_screenshot(prefix="verify")
                success, confidence = self._verify_screen(fresh_path, step.target, step)
                return success, confidence

            elif step.action == ActionType.READ_TEXT:
                assert screenshot_path is not None
                return self._read_text_step(screenshot_path, step, result), 1.0

            elif step.action == ActionType.READ_ITEMS:
                assert screenshot_path is not None
                return self._read_items_step(screenshot_path, step, result), 1.0

            elif step.action == ActionType.READ_SCREEN:
                assert screenshot_path is not None
                return self._read_screen_step(screenshot_path, step, result), 1.0

            elif step.action in (ActionType.SKIP_TUTORIAL, ActionType.TUTORIAL_PASS):
                params = step.params or {}
                pkg = params.get("package") or self._current_package or step.target or ""
                skipped = self.unity.skip_tutorial(package=pkg)
                self._log_cheat_usage(
                    result, "skip_tutorial", pkg, "패키지별 튜토리얼 스킵 치트", skipped)
                return skipped, 1.0

            elif step.action == ActionType.REPEAT_UNTIL:
                return self._repeat_until_step(step, result), 1.0

            elif step.action == ActionType.CALL_CHEAT:
                return self._call_cheat_step(step, result), 1.0

            elif step.action == ActionType.SET_PROPERTY:
                return self._set_property_step(step, result), 1.0

            elif step.action == ActionType.CHECK_PROPERTY:
                return self._check_property_step(step, result), 1.0

            elif step.action == ActionType.ENTER_SR_DEBUGGER:
                params = step.params or {}
                result = self.sr_debugger.enter(
                    package=params.get("package") or step.target or self._current_package,
                    strategies=params.get("strategies") or [],
                    verify_target=params.get("verify_target") or "SRDebugger",
                    max_attempts=int(params.get("max_attempts", 2)),
                )
                if not result.get("success"):
                    self._last_failure_reason = result.get("message") or result.get("status") or "SR Debugger 진입 실패"
                return bool(result.get("success")), 1.0

            elif step.action == ActionType.INSTALL_APP:
                apk_filename = step.params.get("apk") or step.target
                if not apk_filename:
                    logger.error("install_app: apk 파일명이 없습니다. params.apk 또는 target에 지정하세요.")
                    return False, 1.0
                apk_path = self._resolve_apk_path(str(apk_filename))
                if not apk_path.exists():
                    self._last_failure_reason = f"APK 파일 없음: {apk_path}"
                ok, msg = self.adb.install_apk(apk_path)
                logger.info(f"│  {msg}")
                if ok:
                    time.sleep(2)
                return ok, 1.0

            elif step.action == ActionType.UNINSTALL_APP:
                package = step.params.get("package") or step.target or self._current_package
                if not package:
                    logger.error("uninstall_app: package가 없습니다. params.package 또는 target에 지정하세요.")
                    return False, 1.0
                ok, msg = self.adb.uninstall_app(package)
                logger.info(f"│  {msg}")
                if ok:
                    self._current_screen_type = ""
                return ok, 1.0

            elif step.action == ActionType.INPUT_TEXT:
                text = step.params.get("text", "")
                if not text:
                    logger.warning("input_text: text가 비어 있습니다.")
                    return False, 1.0
                escaped = text.replace(" ", "%s").replace("'", "\\'")
                self.adb._execute(["shell", "input", "text", escaped])
                time.sleep(0.5)
                return True, 1.0

            else:
                logger.warning(f"Unknown action type: {step.action}")
                return False, 1.0

        except Exception as e:
            logger.error(f"Step execution failed: {e}")
            self._last_failure_reason = str(e)
            return False, 0.0

    def _execute_wait_step(self, step) -> bool:
        """고정 시간 또는 앱 상태 조건을 중단 가능하게 기다린다."""
        params = step.params or {}
        condition_keys = (
            "until_scene",
            "until_property",
            "until_unity_button",
            "until_unity_button_hidden",
            "until_visible",
            "until_hidden",
        )
        has_condition = any(params.get(key) for key in condition_keys)
        if not has_condition:
            seconds = self._to_float(params.get("seconds"), 2.0)
            if self._sleep_interruptible(seconds):
                self._last_pass_detail = f"{seconds:g}초 대기 완료"
                return True
            self._last_failure_reason = "wait: 중단 요청으로 종료"
            return False

        timeout_seconds = self._to_float(
            params.get("timeout_seconds"), float(step.timeout)
        )
        poll_interval = max(
            0.1,
            self._to_float(params.get("poll_interval_seconds"), 1.0),
        )
        try:
            consecutive_required = max(
                1, min(5, int(params.get("consecutive_matches", 1)))
            )
        except (TypeError, ValueError):
            consecutive_required = 1
        deadline = time.monotonic() + timeout_seconds
        last_detail = ""
        consecutive_matches = 0

        while True:
            if self._stop_requested():
                self._last_failure_reason = "wait: 중단 요청으로 종료"
                return False

            matched, last_detail = self._wait_conditions_met(params)
            if matched:
                consecutive_matches += 1
                if consecutive_matches >= consecutive_required:
                    self._last_pass_detail = f"조건 대기 완료: {last_detail}"
                    logger.info("│  → %s", self._last_pass_detail)
                    return True
            else:
                consecutive_matches = 0

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not self._sleep_interruptible(min(poll_interval, remaining)):
                self._last_failure_reason = "wait: 중단 요청으로 종료"
                return False

        requested = ", ".join(
            f"{key}={params.get(key)!r}"
            for key in condition_keys
            if params.get(key)
        )
        self._last_failure_reason = (
            f"wait: {timeout_seconds:g}초 안에 조건을 만족하지 못함 "
            f"({requested}; 마지막 상태: {last_detail or '확인 불가'})"
        )
        logger.error(self._last_failure_reason)
        return False

    def _wait_conditions_met(self, params: dict) -> tuple[bool, str]:
        """지정된 모든 wait 조건을 한 번씩 확인한다."""
        details: list[str] = []

        until_scene = str(params.get("until_scene") or "").strip()
        if until_scene:
            expected = [
                value.strip().lower()
                for value in until_scene.split("|")
                if value.strip()
            ]
            prefixes = [
                value.lower() for value in self.unity.current_scene_prefixes_v2()
            ]
            scene_ok = bool(prefixes) and any(
                value in prefixes for value in expected
            )
            details.append(f"scene={prefixes or 'unavailable'}")
            if not scene_ok:
                return False, "; ".join(details)

        until_property = params.get("until_property")
        if until_property:
            if not isinstance(until_property, dict):
                return False, "until_property 형식 오류"
            prop_id = str(until_property.get("id") or "").strip()
            has_expected = (
                "equals" in until_property or "expect_value" in until_property
            )
            expected_value = until_property.get(
                "equals", until_property.get("expect_value")
            )
            if not prop_id or not has_expected:
                return False, "until_property에는 id와 equals가 필요"
            item = self.unity.get_property_values_v2([prop_id]).get(prop_id)
            if not item or item.get("Error"):
                return False, f"{prop_id}=unavailable"
            actual = item.get("Value")
            if isinstance(actual, dict) and "value" in actual:
                actual = actual["value"]
            details.append(f"{prop_id}={actual!r}")
            if not self._property_value_matches(expected_value, actual):
                return False, "; ".join(details)

        until_unity_button = params.get("until_unity_button")
        if until_unity_button:
            match = self.unity.find_exact_button(until_unity_button)
            details.append(
                f"unity_button={'found' if match is not None else 'not_found'}"
            )
            if match is None:
                return False, "; ".join(details)

        until_unity_button_hidden = params.get("until_unity_button_hidden")
        if until_unity_button_hidden:
            if not until_unity_button:
                return (
                    False,
                    "until_unity_button_hidden에는 API 정상 응답 확인용 "
                    "until_unity_button도 필요",
                )
            match = self.unity.find_exact_button(until_unity_button_hidden)
            hidden = match is None
            details.append(f"unity_button_hidden={hidden}")
            if not hidden:
                return False, "; ".join(details)

        visible = self._to_target_list(params.get("until_visible"))
        hidden = self._to_target_list(params.get("until_hidden"))
        if visible or hidden:
            probe = self._capture_runtime_screenshot(prefix="wait_condition")
            detections = self.vision_lite.find_elements(
                probe,
                visible + hidden,
                self.config.paths.debug_dir,
            )
            if len(detections) != len(visible) + len(hidden):
                return False, "화면 조건 판독 결과 부족"

            for target, detection in zip(visible, detections[:len(visible)]):
                found = bool(detection.success and detection.bbox)
                details.append(f"visible:{target}={found}")
                if getattr(detection, "error", None) or not found:
                    return False, "; ".join(details)

            hidden_results = detections[len(visible):]
            for target, detection in zip(hidden, hidden_results):
                if getattr(detection, "error", None):
                    return False, f"hidden:{target}=판독 오류"
                found = bool(detection.success and detection.bbox)
                details.append(f"hidden:{target}={not found}")
                if found:
                    return False, "; ".join(details)

        return True, "; ".join(details)

    def _stop_requested(self) -> bool:
        stop_event = getattr(self, "_stop_event", None)
        return bool(stop_event and stop_event.is_set())

    def _sleep_interruptible(self, seconds: float) -> bool:
        if seconds <= 0:
            return not self._stop_requested()
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None:
            return not stop_event.wait(seconds)
        time.sleep(seconds)
        return True

    def _load_package_apk_map(self) -> dict[str, str]:
        for base in [self.config.paths.project_root, self.config.paths.bundle_root]:
            map_path = base / "package_apk_map.json"
            if not map_path.exists():
                continue
            try:
                data = json.loads(map_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
            except Exception as e:
                logger.warning("package_apk_map.json 로드 실패(%s): %s", map_path, e)
        return {}

    def _resolve_apk_path(self, apk_filename: str) -> Path:
        apks_dir = self.config.paths.apks_dir
        direct = apks_dir / apk_filename
        if direct.exists():
            return direct

        normalized_name = unicodedata.normalize("NFC", apk_filename)
        for existing in apks_dir.glob("*.apk"):
            if unicodedata.normalize("NFC", existing.name) == normalized_name:
                logger.info("│  APK 파일명 정규화: %s → %s", apk_filename, existing.name)
                return existing

        mapped_apk = self._package_apk_map.get(self._current_package)
        if mapped_apk:
            mapped_path = apks_dir / mapped_apk
            if mapped_path.exists():
                logger.info(
                    "│  APK 매핑 적용: %s 대신 %s (%s)",
                    apk_filename,
                    mapped_apk,
                    self._current_package,
                )
                return mapped_path

        return direct

    def _execute_back_step(self, step) -> bool:
        """뒤로가기 + 선택적 화면 검증을 원자적으로 수행"""
        params = step.params or {}
        wait_seconds = self._to_float(params.get("wait_seconds"), self.POST_TAP_DELAY_SEC)
        expect_visible = self._to_target_list(params.get("expect_visible") or step.target)
        expect_hidden = self._to_target_list(params.get("expect_hidden"))
        max_attempts = 2 if (expect_visible or expect_hidden) else 1

        for attempt in range(max_attempts):
            if not self.adb.press_back(delay=0):
                return False

            if wait_seconds > 0:
                time.sleep(wait_seconds)

            if not expect_visible and not expect_hidden:
                return True

            screenshot_path = self._capture_runtime_screenshot(prefix=f"post_back_{attempt + 1}")
            if self._verify_expected_targets(screenshot_path, expect_visible, expect_hidden):
                if attempt == 1:
                    logger.info("Back verification succeeded after one additional back press.")
                return True

            if attempt == 0:
                logger.warning(
                    "Back verification failed. Pressing back one more time before failing."
                )

        return False

    def _dismiss_popups(self, step) -> bool:
        """명시된 팝업만 반복해서 닫고 최종 화면이 안정되면 성공한다."""
        params = step.params or {}
        stop_targets = self._to_target_list(
            params.get("stop_when_visible") or params.get("expect_visible") or step.target
        )
        raw_rules = params.get("rules")
        if not stop_targets:
            self._last_failure_reason = "dismiss_popups: stop_when_visible이 필요합니다."
            return False
        if not isinstance(raw_rules, list) or not raw_rules:
            self._last_failure_reason = "dismiss_popups: rules가 1개 이상 필요합니다."
            return False

        allowed_actions = {"back", "tap", "tap_center"}
        rules: list[dict] = []
        for index, rule in enumerate(raw_rules, start=1):
            if not isinstance(rule, dict):
                self._last_failure_reason = f"dismiss_popups: rules[{index}]가 object가 아닙니다."
                return False
            target = str(rule.get("target") or "").strip()
            action = str(rule.get("action") or "").strip()
            if not target or action not in allowed_actions:
                self._last_failure_reason = (
                    f"dismiss_popups: rules[{index}]에는 target과 "
                    f"action({', '.join(sorted(allowed_actions))})이 필요합니다."
                )
                return False
            rules.append({"target": target, "action": action})

        try:
            max_count = max(0, min(20, int(params.get("max_count", 4))))
        except (TypeError, ValueError):
            max_count = 4
        timeout_seconds = self._to_float(params.get("timeout_seconds"), float(step.timeout))
        quiet_seconds = self._to_float(params.get("quiet_seconds"), 1.5)
        poll_interval = max(0.1, self._to_float(params.get("poll_interval_seconds"), 0.5))
        complete_after_target = str(params.get("complete_after_target") or "").strip()

        deadline = time.monotonic() + timeout_seconds
        stable_since: Optional[float] = None
        dismissed = 0
        initial_wait = self._to_float(params.get("initial_wait_seconds"), self.POST_TAP_DELAY_SEC)
        remaining = max(0.1, deadline - time.monotonic())
        screenshot_path = self._wait_for_screen_stable(
            timeout=min(self.STABILITY_TIMEOUT_SEC, remaining),
            min_wait=initial_wait,
        )

        while time.monotonic() <= deadline:
            rule, coords, stop_visible = self._analyze_dismiss_frame(
                screenshot_path, rules, stop_targets
            )
            if rule:
                stable_since = None
                if dismissed >= max_count:
                    self._last_failure_reason = (
                        f"dismiss_popups: 최대 처리 횟수 {max_count}회를 초과했습니다 "
                        f"(추가 팝업: '{rule['target']}')."
                    )
                    return False

                action = rule["action"]
                self._save_interrupt_debug(screenshot_path, {
                    "is_interrupt": True,
                    "kind": "dismiss_popups",
                    "close_method": action,
                    "description": rule["target"],
                    "rule": rule,
                })
                if action == "back":
                    success = self.adb.press_back(delay=0)
                elif action == "tap_center":
                    success = self.adb.tap(self.adb.width // 2, self.adb.height // 2, delay=0)
                else:
                    success = bool(coords) and self.adb.tap(coords["x"], coords["y"], delay=0)

                if not success:
                    self._last_failure_reason = (
                        f"dismiss_popups: '{rule['target']}' 팝업의 {action} 실행에 실패했습니다."
                    )
                    return False

                dismissed += 1
                self._current_screen_type = ""
                logger.info(
                    "dismiss_popups %d/%d: '%s' -> %s",
                    dismissed, max_count, rule["target"], action,
                )
                remaining = max(0.1, deadline - time.monotonic())
                screenshot_path = self._wait_for_screen_stable(
                    timeout=min(self.STABILITY_TIMEOUT_SEC, remaining)
                )
                if complete_after_target and rule["target"] == complete_after_target:
                    self._last_failure_reason = ""
                    self._last_pass_detail = (
                        f"마지막 지정 팝업 '{complete_after_target}'까지 "
                        f"총 {dismissed}개 처리 완료"
                    )
                    self._save_interrupt_debug(screenshot_path, {
                        "is_interrupt": False,
                        "kind": "dismiss_popups",
                        "evidence_phase": "final_verification",
                        "description": complete_after_target,
                        "result": "PASS",
                        "completion_mode": "last_expected_popup_dismissed",
                    })
                    return True
                continue

            if stop_visible:
                now = time.monotonic()
                if stable_since is None:
                    stable_since = now
                if now - stable_since >= quiet_seconds:
                    self._last_failure_reason = ""
                    self._last_pass_detail = (
                        f"팝업 {dismissed}개 처리 후 '{', '.join(stop_targets)}' 안정 상태 확인"
                    )
                    self._save_interrupt_debug(screenshot_path, {
                        "is_interrupt": False,
                        "kind": "dismiss_popups",
                        "evidence_phase": "final_verification",
                        "description": ", ".join(stop_targets),
                        "result": "PASS",
                    })
                    return True
            else:
                stable_since = None

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
            screenshot_path = self._capture_runtime_screenshot(prefix="dismiss_popups")

        self._last_failure_reason = (
            f"dismiss_popups: {dismissed}개 처리 후 제한시간 {timeout_seconds:.1f}초 안에 "
            f"최종 화면 '{', '.join(stop_targets)}'을 확인하지 못했습니다."
        )
        return False

    def _analyze_dismiss_frame(
        self,
        screenshot_path: Path,
        rules: list[dict],
        stop_targets: list[str],
    ) -> tuple[Optional[dict], Optional[dict], bool]:
        """팝업 규칙과 종료 조건을 동일 프레임에서 VLM 1회로 판정한다."""
        targets = [rule["target"] for rule in rules] + stop_targets
        results = self.vision_lite.find_elements(
            screenshot_path, targets, self.config.paths.debug_dir
        )
        rule_results = results[:len(rules)]
        stop_results = results[len(rules):]
        for rule, result in zip(rules, rule_results):
            if result.success and result.bbox:
                coords = result.bbox.to_pixels(self.adb.width, self.adb.height)
                return rule, coords, False
        stop_visible = bool(stop_results) and all(
            result.success and result.bbox for result in stop_results
        )
        return None, None, stop_visible

    def _verify_expected_targets(
        self,
        screenshot_path: Path,
        expect_visible: list[str],
        expect_hidden: list[str],
    ) -> bool:
        targets = expect_visible + expect_hidden
        results = self.vision_lite.find_elements(
            screenshot_path, targets, self.config.paths.debug_dir
        )
        visible_results = results[:len(expect_visible)]
        hidden_results = results[len(expect_visible):]

        verification_confidence = 1.0
        for target, result in zip(expect_visible, visible_results):
            verification_confidence = min(verification_confidence, result.confidence)
            if not result.success or not result.bbox:
                self._last_expected_confidence = result.confidence
                logger.warning("Expected visible target '%s' was not found.", target)
                return False

        for target, result in zip(expect_hidden, hidden_results):
            if result.success and result.bbox:
                self._last_expected_confidence = result.confidence
                logger.warning("Expected hidden target '%s' is still visible.", target)
                return False

        self._last_expected_confidence = verification_confidence
        return True

    @staticmethod
    def _to_target_list(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            targets = []
            for item in value:
                if isinstance(item, str) and item.strip():
                    targets.append(item)
            return targets
        return []

    def _describe_postcondition(self, params: dict) -> str:
        """탭 대상이 아니라 실제로 실패한 후조건을 사용자에게 표시한다."""
        visible = self._to_target_list(params.get("expect_visible"))
        hidden = self._to_target_list(params.get("expect_hidden"))
        details = []
        if visible:
            details.append(f"노출 기대 '{', '.join(visible)}'")
        if hidden:
            details.append(f"숨김 기대 '{', '.join(hidden)}'")
        return ", ".join(details) or "후조건이 충족되지 않음"

    @staticmethod
    def _uses_internal_retry(step) -> bool:
        params = step.params or {}
        if step.action == ActionType.DISMISS_POPUPS:
            return True
        if step.action == ActionType.BACK:
            return bool(params.get("expect_visible") or params.get("expect_hidden") or step.target)
        return False

    @staticmethod
    def _to_float(value, default: float) -> float:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return default

    def _get_screen_type(self, screenshot_path: Path) -> str:
        """현재 화면 타입 반환. 미감지 시 Vision으로 탐지 후 캐싱."""
        if self._current_screen_type:
            return self._current_screen_type
        state = self.vision.analyze_screen_state(screenshot_path)
        screen_type = state.get("screen_type", "unknown")
        self._current_screen_type = screen_type
        logger.info("Screen type detected: %s", screen_type)
        return screen_type

    # 탭 성공했으나 검증만 실패했음을 나타내는 센티널
    _TAP_OK_VERIFY_FAIL = "TAP_OK_VERIFY_FAIL"

    # 대상 미발견 시 인터럽트 팝업 복구 최대 횟수 (팝업이 겹쳐 뜨는 경우 대비)
    INTERRUPT_RECOVERY_MAX = 3  # 로그인 직후 퀘스트/이벤트 팝업이 연달아 뜨는 경우 대비

    def _try_recover_interrupt(self, screenshot_path: Path) -> bool:
        """예상 밖 인터럽트 팝업(이벤트/공지/오류)이면 닫는다. 복구했으면 True.

        복구 액션은 닫기 버튼 탭 / 뒤로가기만 허용 (화이트리스트) —
        결제 시트·획득 팝업 등 테스트가 의도한 화면은 프롬프트에서 제외되며,
        임의 버튼을 눌러 기기 상태를 오염시키지 않는다.
        경로 우회는 하지 않는다: 복구 후에도 원래 대상을 다시 찾을 뿐이며,
        복구 불가면 정직하게 FAIL로 남긴다.
        """
        analysis = self.vision.detect_interrupt(screenshot_path)
        if not analysis or not analysis.get("is_interrupt"):
            return False

        kind = analysis.get("kind", "?")
        desc = analysis.get("description", "")
        method = analysis.get("close_method")
        logger.info("⚠️ 인터럽트 팝업 감지 [%s]: %s", kind, desc)
        self._save_interrupt_debug(screenshot_path, analysis)

        if method == "tap":
            box = analysis.get("close_box_2d")
            if box and len(box) == 4:
                ymin, xmin, ymax, xmax = box
                x = int((xmin + xmax) / 2 / 1000 * self.adb.width)
                y = int((ymin + ymax) / 2 / 1000 * self.adb.height)
                self.adb.tap(x, y)
                logger.info("│  닫기 버튼 탭 (%d, %d)", x, y)
            else:
                self.adb.press_back()
                logger.info("│  닫기 버튼 좌표 없음 — 뒤로가기로 대체")
        elif method == "tap_center":
            # "계속하려면 화면을 눌러주세요" 류 — 아무 곳이나 눌러 닫는 팝업
            self.adb.tap(self.adb.width // 2, self.adb.height // 2)
            logger.info("│  안내 문구 팝업 — 화면 중앙 탭으로 닫기")
        elif method == "back":
            self.adb.press_back()
            logger.info("│  뒤로가기로 팝업 닫기")
        else:
            logger.info("│  닫기 방법 불명 — 복구 중단")
            return False
        return True

    def _save_interrupt_debug(self, screenshot_path: Path, analysis: dict) -> None:
        """인터럽트 복구가 '무엇을 보고' 팝업으로 판단했는지 남긴다 — 오판 분석용.

        - screenshots_debug/taps/{ts}_{device}_INTERRUPT_{kind}.png : 판단 당시 화면
        - screenshots_debug/interrupt_debug.jsonl                   : 판단 내용 한 줄
        (taps/와 같은 이유로 cleanup이 지우지 않는 위치 사용)
        """
        try:
            debug_dir = self.config.paths.debug_dir
            taps_dir = debug_dir / "taps"
            taps_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            kind = str(analysis.get("kind", "unknown"))[:20]
            img_path = taps_dir / f"{ts}_{self._file_tag}_INTERRUPT_{kind}.png"
            Image.open(screenshot_path).convert("RGB").save(img_path)
            record = {
                "timestamp": ts,
                "evidence_captured_at": datetime.fromtimestamp(
                    screenshot_path.stat().st_mtime
                ).isoformat(timespec="milliseconds"),
                "evidence_phase": analysis.get("evidence_phase", "popup_detection"),
                "step_number": getattr(self, "_current_step_number", None),
                "step_label": getattr(self, "_current_step_label", ""),
                "step_action": getattr(self, "_current_step_action", ""),
                "device": self._file_tag,
                "analysis": analysis,
                "debug_image": str(img_path),
            }
            with open(debug_dir / "interrupt_debug.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.info("┌─ 인터럽트 판단 근거 저장: %s", img_path.name)
        except Exception as e:
            logger.warning("인터럽트 디버그 저장 실패: %s", e)

    def _find_and_tap(self, step) -> bool | str:
        """정확 Unity → 명시적 안전 캐시 → Vision 순으로 좌표를 찾는다."""
        self._last_post_verify_screenshot = None
        target = step.target
        if not target:
            logger.error("find_and_tap action requires target")
            self._last_failure_reason = "target이 지정되지 않음"
            return False
        params = step.params or {}

        # TO-DO 데이터 쌓이면 그때 db 연결 
        # # 1. 공통 캐시 조회 (게임 무관, 해상도별)
        # common = self.common_cache.get(target, self._resolution)
        # if common:
        #     logger.info("CommonTap HIT for '%s' @ %s → (%d, %d).",
        #                 target, self._resolution, common.x, common.y)
        #     self._wait_for_screen_stable()
        #     self.adb.tap(common.x, common.y)
        #     self._current_screen_type = ""
        #     verified = self._verify_find_and_tap_outcome(step, tap_source="CommonCache")
        #     if not verified:
        #         self._last_failure_reason = f"탭 성공(CommonCache), 화면 검증 실패: '{target}'"
        #     return True if verified else self._TAP_OK_VERIFY_FAIL

        # # 2. 게임별 캐시 조회 (패키지 + 화면 + 해상도)
        # cached = self._lookup_cache(target)
        # if cached:
        #     logger.info("Cache HIT for '%s' @ %s → (%d, %d).",
        #                 target, self._resolution, cached.x, cached.y)
        #     self._wait_for_screen_stable()
        #     self.adb.tap(cached.x, cached.y)
        #     self._current_screen_type = ""
        #     verified = self._verify_find_and_tap_outcome(step, tap_source="Cache")
        #     if not verified:
        #         self._last_failure_reason = f"탭 성공(Cache), 화면 검증 실패: '{target}'"
        #     return True if verified else self._TAP_OK_VERIFY_FAIL

        # 이메일 대상(구글 계정 선택 등)은 결정적으로만 처리 — Vision 유사 매칭이
        # qa_google_01/02 같은 한 글자 차이 계정을 혼동하고, 인터럽트 복구가
        # 계정 팝업을 닫으려다 다른 계정을 눌러 잘못 로그인하는 사고 방지.
        if "@" in target and " " not in target.strip():
            return self._tap_account_email(step, target.strip())

        # Profile에 실기기에서 확인한 Unity 이름이 있을 때만 쓰는 결정적 경로.
        # 정확히 하나가 일치하지 않으면 아직 탭하지 않았으므로 Vision으로 폴백 가능하다.
        unity_name = params.get("unity_name")
        if unity_name and not params.get("then_tap"):
            exact = self.unity.find_exact_button(unity_name)
            if exact:
                point = self.unity.unity_to_screen_coords(
                    exact.button, self.adb.width, self.adb.height
                )
                if point:
                    evidence_path = self._capture_runtime_screenshot(
                        prefix="unity_exact"
                    )
                    coords = self._coords_with_debug_box(point["x"], point["y"])
                    return self._tap_resolved_once(
                        step,
                        evidence_path,
                        coords,
                        source="UnityExact",
                        confidence=1.0,
                    )
            logger.info(
                "Unity exact MISS/AMBIGUOUS for '%s' (%r) → Vision fallback.",
                target,
                unity_name,
            )

        # 좌표 캐시는 Profile이 안정적인 화면 범위와 후조건을 명시한 경우에만 읽는다.
        # 캐시로 이미 탭했다면 검증 실패 후 Vision으로 다시 누르지 않는다.
        cache_scope = str(params.get("cache_scope") or "").strip()
        cache_safe = bool(params.get("cache_safe"))
        has_postcondition = bool(
            params.get("expect_visible") or params.get("expect_hidden")
        )
        if (
            cache_safe
            and cache_scope
            and has_postcondition
            and not params.get("then_tap")
            and self._current_package
        ):
            cached = self.cache.get(
                self._current_package,
                cache_scope,
                target,
                self._resolution,
            )
            if cached:
                if (
                    0 <= cached.x < self.adb.width
                    and 0 <= cached.y < self.adb.height
                ):
                    evidence_path = self._capture_runtime_screenshot(
                        prefix="scoped_cache"
                    )
                    coords = self._coords_with_debug_box(cached.x, cached.y)
                    return self._tap_resolved_once(
                        step,
                        evidence_path,
                        coords,
                        source="ScopedCache",
                        confidence=cached.confidence,
                        invalidate_cache_scope=cache_scope,
                    )
                self.cache.invalidate_element(
                    self._current_package,
                    cache_scope,
                    target,
                    self._resolution,
                )

        # 탭 바로가기: params.tab_shortcut이 FIXED_TAB_COORDS에 있으면 vision 없이 즉시 탭.
        # 한 번 탭한 뒤에는 상태 중복 변경을 막기 위해 검증만 재시도한다.
        tab_shortcut = params.get("tab_shortcut")
        if tab_shortcut:
            fixed = self._resolve_tab_shortcut(tab_shortcut)
            if fixed:
                evidence_path = self._capture_runtime_screenshot(
                    prefix="tab_shortcut"
                )
                return self._tap_resolved_once(
                    step,
                    evidence_path,
                    fixed,
                    source="TabShortcut",
                    confidence=1.0,
                )

        # 3. 캐시 미스 → Vision 탐지 → 캐시 저장
        # logger.info("Cache MISS for '%s' @ %s → Vision fallback.", target, self._resolution)
        logger.info(f"vision target:{target}")
        # 전환 시작 여유만 두고, 실제 대기는 stable check가 담당 (기존 3초 고정 대기 축소)
        time.sleep(1)
        latest_path = self._wait_for_screen_stable()
        coords = self._resolve_with_vision(latest_path, target)

        # scroll_search 스텝은 첫 화면에서 미발견이 정상(스크롤해야 나옴) —
        # 인터럽트 분석을 먼저 돌리면 상점/목록 화면을 팝업으로 오판해 뒤로가기로
        # 이탈할 수 있으므로, 스크롤 탐색을 먼저 한다.
        if not coords and params.get("scroll_search"):
            latest_path, coords = self._scroll_search(step, target, latest_path)

        # 대상 미발견 시 인터럽트 복구: 이벤트/공지/오류 팝업이 가리고 있으면 닫고 재탐색.
        # optional 스텝은 "안 나올 수 있는 대상"이라 미발견이 정상 — 복구를 시도하지 않는다.
        if not coords and not params.get("optional") and not params.get("no_recovery"):
            for attempt in range(self.INTERRUPT_RECOVERY_MAX):
                if not self._try_recover_interrupt(latest_path):
                    break
                latest_path = self._wait_for_screen_stable()
                coords = self._resolve_with_vision(latest_path, target)
                if coords:
                    logger.info("✅ 인터럽트 복구 후 대상 재발견: '%s' (복구 %d회)", target, attempt + 1)
                    break
            # 복구로 팝업을 닫았다면 가려져 있던 목록일 수 있으니 스크롤 탐색 1회 재시도
            if not coords and params.get("scroll_search"):
                latest_path, coords = self._scroll_search(step, target, latest_path)

        if not coords:
            if not self._last_failure_reason:
                self._last_failure_reason = f"Vision으로 요소를 찾지 못함: '{target}'"
            # 못 찾은 경우에도 실패 당시 화면을 남긴다 — cleanup이 지우지 않는 taps/ 에 저장
            self._save_tap_debug(
                latest_path, target, None,
                getattr(self, "_last_vision_confidence", 0.0), False,
            )
            return False
        # tap_point: "center" — 대상이 화면에 보이는지 확인만 하고, 탭은 화면 정중앙에
        # (획득 팝업처럼 "아무 곳이나 눌러 닫기" 화면용)
        if params.get("tap_point") == "center":
            tap_x, tap_y = self.adb.width // 2, self.adb.height // 2
            coords = {**coords, "x": tap_x, "y": tap_y}
            logger.info("tap_point=center — '%s' 확인 후 화면 정중앙 (%d, %d) 탭", target, tap_x, tap_y)
        self.adb.tap(coords["x"], coords["y"])
        self._last_tap_performed = True
        self._current_screen_type = ""

        # then_tap: 첫 탭 후 이어서 탭할 대상 (예: 팝업 옵션 선택 → 확인 버튼) — 한 스텝으로 처리
        then_target = params.get("then_tap")
        if then_target:
            then_path = self._wait_for_screen_stable()
            then_coords = self._resolve_with_vision(then_path, then_target)
            if not then_coords:
                self._last_failure_reason = f"연속 탭 대상을 찾지 못함: '{then_target}' ('{target}' 탭 후)"
                self._save_tap_debug(
                    then_path, then_target, None,
                    getattr(self, "_last_vision_confidence", 0.0), False,
                )
                return False
            self.adb.tap(then_coords["x"], then_coords["y"])
            logger.info("연속 탭: '%s' → '%s'", target, then_target)

        verified = self._verify_find_and_tap_outcome(step, tap_source="Vision")
        if not verified:
            self._last_failure_reason = (
                f"탭 성공(Vision), 후조건 검증 실패: "
                f"{self._describe_postcondition(params)}"
            )
        else:
            exp = params.get("expect_visible")
            self._last_pass_detail = (
                f"'{target}' 탭 ({coords['x']},{coords['y']})"
                + (f" → '{exp}' 노출 확인" if exp else "")
            )
            if cache_safe and cache_scope:
                self._cache_element(
                    latest_path,
                    target,
                    coords["x"],
                    coords["y"],
                    "vision",
                    screen_key=cache_scope,
                )
            self._auto_register_common(target, coords["x"], coords["y"])
        self._save_tap_debug(
            latest_path, target, coords,
            getattr(self, "_last_vision_confidence", 0.0), verified,
        )
        return True if verified else self._TAP_OK_VERIFY_FAIL

    @staticmethod
    def _coords_with_debug_box(x: int, y: int, radius: int = 24) -> dict:
        return {
            "x": x,
            "y": y,
            "x1": x - radius,
            "y1": y - radius,
            "x2": x + radius,
            "y2": y + radius,
        }

    def _tap_resolved_once(
        self,
        step,
        screenshot_path: Path,
        coords: dict,
        *,
        source: str,
        confidence: float,
        invalidate_cache_scope: str = "",
    ) -> bool | str:
        """결정적 좌표를 한 번만 탭하고 이후에는 후조건만 확인한다."""
        target = step.target or ""
        if not self.adb.tap(coords["x"], coords["y"]):
            self._last_failure_reason = f"{source} 좌표 탭 실패: '{target}'"
            self._save_tap_debug(
                screenshot_path, target, coords, confidence, False
            )
            return False

        self._last_tap_performed = True
        self._current_screen_type = ""
        verified = self._verify_find_and_tap_outcome(step, tap_source=source)
        if not verified:
            if invalidate_cache_scope and self._current_package:
                self.cache.invalidate_element(
                    self._current_package,
                    invalidate_cache_scope,
                    target,
                    self._resolution,
                )
            self._last_failure_reason = (
                f"탭 성공({source}), 후조건 검증 실패: "
                f"{self._describe_postcondition(step.params or {})}"
            )
        else:
            expected = self._to_target_list(
                (step.params or {}).get("expect_visible")
            )
            self._last_pass_detail = (
                f"'{target}' {source} 탭 ({coords['x']},{coords['y']})"
                + (f" → '{', '.join(expected)}' 노출 확인" if expected else "")
            )

        self._save_tap_debug(
            screenshot_path, target, coords, confidence, verified
        )
        return True if verified else self._TAP_OK_VERIFY_FAIL

    def _tap_account_email(self, step, email: str) -> bool | str:
        """구글 계정 선택 등 이메일 대상 탭 — UI 트리 텍스트 정확 일치만 허용.

        요청한 계정이 화면에 없으면 절대 다른 항목을 누르지 않고,
        기기 등록 계정을 확인해 원인(미등록 vs 화면 미노출)을 명확히 남긴다.
        """
        time.sleep(1)
        latest_path = self._wait_for_screen_stable()
        email_l = email.lower()
        node = None
        for attempt in range(3):  # 다이얼로그 로딩 지연 대비
            nodes = parse_ui_nodes(self.adb.ui_dump())
            node = next(
                (n for n in nodes if n["text"].strip().lower() == email_l), None)
            if node:
                break
            time.sleep(2)
        if node is None:
            accounts = self.adb.get_google_accounts()
            if email_l not in (a.lower() for a in accounts):
                self._last_failure_reason = (
                    f"기기에 구글 계정 '{email}' 미등록 (등록된 계정: "
                    f"{', '.join(accounts) or '없음'}) — 폰에 계정 추가 후 다시 실행"
                )
            else:
                self._last_failure_reason = (
                    f"화면에서 계정 '{email}'을 찾지 못함 (기기에는 등록되어 있음)"
                )
            logger.error(self._last_failure_reason)
            self._save_tap_debug(latest_path, email, None, 0.0, False)
            return False
        coords = {"x": node["cx"], "y": node["cy"],
                  "x1": node["x1"], "y1": node["y1"],
                  "x2": node["x2"], "y2": node["y2"]}
        logger.info("계정 이메일 XML 정확 일치 탭: '%s' → (%d, %d)", email, coords["x"], coords["y"])
        self.adb.tap(coords["x"], coords["y"])
        self._last_tap_performed = True
        self._current_screen_type = ""
        verified = self._verify_find_and_tap_outcome(step, tap_source="XML")
        if not verified:
            self._last_failure_reason = (
                f"탭 성공(XML), 후조건 검증 실패: "
                f"{self._describe_postcondition(step.params or {})}"
            )
        else:
            self._last_pass_detail = f"계정 '{email}' XML 정확 일치 탭 ({coords['x']},{coords['y']})"
        self._save_tap_debug(latest_path, email, coords, 1.0, verified)
        return True if verified else self._TAP_OK_VERIFY_FAIL

    # scroll_search 기본값
    SCROLL_SEARCH_MAX_DEFAULT = 5      # 스크롤 상한 (무한 루프 방지 1)
    SCROLL_END_THRESHOLD = 0.005       # 스크롤 전후 변화율이 이보다 작으면 리스트 끝 (무한 루프 방지 2)

    def _scroll_one_page(self, direction: str, fraction: float = 0.35) -> None:
        """화면 중앙 세로선 기준 스크롤 (up=이전 내용으로, down=다음 내용으로).

        fraction: 화면 높이 대비 이동 비율 (기본 0.35 = 기존 동작 그대로).
        마물 탭처럼 카드 그리드라 한 행 높이가 작은 화면은 fraction을 줄여서
        (예: 카드 행 높이 / 화면 높이) 촘촘히 훑어야 카드가 화면 경계에 걸쳐
        vision이 놓치는 일이 적다.
        """
        w, h = self.adb.width, self.adb.height
        x = w // 2
        fraction = max(0.05, min(0.9, fraction))
        half_gap = fraction / 2
        top = 0.5 - half_gap
        bottom = 0.5 + half_gap
        if direction == "up":
            self.adb.swipe(x, int(h * top), x, int(h * bottom), duration=400)
        else:
            self.adb.swipe(x, int(h * bottom), x, int(h * top), duration=400)

    def _scroll_action(self, step) -> bool:
        """선언적 스크롤 액션.

        params:
          direction: down(기본) | up | top(맨 위까지) | bottom(맨 끝까지)
          times: 횟수 (up/down만, 기본 1)
        top/bottom은 화면이 더 이상 변하지 않을 때까지 스크롤 (최대 10회).
        """
        params = step.params or {}
        direction = str(params.get("direction", "down")).lower()
        times = max(1, int(params.get("times", 1)))
        scroll_fraction = self._to_float(params.get("scroll_fraction"), 0.35)

        if direction in ("top", "bottom"):
            one_dir = "up" if direction == "top" else "down"
            prev = self._wait_for_screen_stable()
            for i in range(10):
                if getattr(self, '_stop_event', None) and self._stop_event.is_set():
                    break
                self._scroll_one_page(one_dir, fraction=scroll_fraction)
                curr = self._wait_for_screen_stable()
                ratio = self._image_change_ratio(prev, curr)
                if ratio is not None and ratio < self.SCROLL_END_THRESHOLD:
                    logger.info("scroll %s: 끝 도달 (%d회 스크롤)", direction, i + 1)
                    break
                prev = curr
            return True

        if direction not in ("up", "down"):
            self._last_failure_reason = f"scroll direction 값 오류: '{direction}' (up/down/top/bottom)"
            logger.error(self._last_failure_reason)
            return False
        for _ in range(times):
            self._scroll_one_page(direction, fraction=scroll_fraction)
            time.sleep(0.5)
        logger.info("scroll %s x%d 완료", direction, times)
        return True

    def _scroll_search(self, step, target: str, latest_path: Path) -> tuple[Path, Optional[dict]]:
        """대상을 찾을 때까지 한 페이지씩 스크롤하며 재탐색.

        중단 조건: 대상 발견 / max_scrolls 도달 / 스크롤해도 화면이 안 변함(리스트 끝) / 사용자 중단.
        반환: (마지막 스크린샷 경로, 좌표 or None)
        """
        params = step.params or {}
        max_scrolls = int(params.get("max_scrolls", self.SCROLL_SEARCH_MAX_DEFAULT))
        direction = "up" if str(params.get("scroll_direction", "down")).lower() == "up" else "down"
        # 카드 그리드처럼 한 행이 작은 화면은 scroll_fraction을 줄여서 촘촘히 스크롤해야
        # 카드가 화면 경계에 걸쳐 vision이 놓치는 일을 줄일 수 있다 (기본은 기존 동작 그대로).
        scroll_fraction = self._to_float(params.get("scroll_fraction"), 0.35)

        for i in range(max_scrolls):
            if getattr(self, '_stop_event', None) and self._stop_event.is_set():
                logger.warning("scroll_search: 사용자 중단")
                break
            before_path = latest_path
            self._scroll_one_page(direction, fraction=scroll_fraction)
            latest_path = self._wait_for_screen_stable()

            ratio = self._image_change_ratio(before_path, latest_path)
            if ratio is not None and ratio < self.SCROLL_END_THRESHOLD:
                logger.info("scroll_search: 화면 변화 없음(%.4f) — 리스트 끝 도달, 중단 (%d회 스크롤)", ratio, i + 1)
                break

            coords = self._resolve_with_vision(latest_path, target)
            if coords:
                logger.info("✅ scroll_search: %d회 스크롤 후 '%s' 발견", i + 1, target)
                return latest_path, coords
        else:
            logger.info("scroll_search: 최대 스크롤(%d회) 도달 — '%s' 미발견", max_scrolls, target)

        self._last_failure_reason = f"스크롤 탐색으로도 '{target}'을 찾지 못함"
        return latest_path, None

    def _lookup_cache(self, target: str) -> Optional[CachedElement]:
        """현재 패키지 + 화면 + 해상도 기준으로 게임별 캐시 조회."""
        if not self._current_package:
            return None
        if not self._current_screen_type:
            return None
        return self.cache.get(
            self._current_package, self._current_screen_type,
            target, self._resolution,
        )

    def _auto_register_common(self, target: str, x: int, y: int) -> None:
        """공통 요소를 Vision으로 찾은 경우, 현재 해상도로 공통 캐시에 자동 등록."""
        normalized = target.strip().replace(" ", "")
        for common_name in self.COMMON_TAP_ELEMENTS:
            if common_name.replace(" ", "") in normalized or normalized in common_name.replace(" ", ""):
                existing = self.common_cache.get(common_name, self._resolution)
                if not existing:
                    self.common_cache.set(common_name, self._resolution, x, y, source="vision")
                    logger.info("Auto-registered common tap: '%s' @ %s → (%d, %d)",
                                common_name, self._resolution, x, y)
                return

    def _resolve_tab_shortcut(self, tab_name: str) -> Optional[dict]:
        """FIXED_TAB_COORDS에서 현재 패키지+해상도에 등록된 탭 고정 좌표 조회.
        _save_tap_debug가 기대하는 bbox 형태(x1/y1/x2/y2)까지 채워서 반환한다
        (vision 경로의 BoundingBox.to_pixels()와 동일한 키 구성 — 없으면 디버그 저장이 깨짐)."""
        coords = FIXED_TAB_COORDS.get(self._current_package, {}).get(self._resolution, {}).get(tab_name)
        if not coords:
            return None
        x, y = coords
        r = 24  # 디버그 이미지에 그릴 가상 바운딩 박스 반경
        return {"x": x, "y": y, "x1": x - r, "y1": y - r, "x2": x + r, "y2": y + r}

    def _cache_element(
        self,
        screenshot_path: Path,
        target: str,
        x: int,
        y: int,
        source: str,
        *,
        screen_key: str = "",
    ) -> None:
        if not self._current_package:
            return
        screen_type = screen_key or self._get_screen_type(screenshot_path)
        self.cache.set(self._current_package, screen_type, target, x, y, source,
                       resolution=self._resolution)

    def _resolve_with_vision(self, screenshot_path: Path, target: str) -> Optional[dict]:
        # 정확 Unity/명시적 캐시가 모두 실패해 실제 Vision 폴백이 필요한 시점에만
        # 상태 컨텍스트를 수집한다.
        self._refresh_state_context()
        vision_result = self.vision.find_element(
            screenshot_path, target, self.config.paths.debug_dir, state_context=self._sctx
        )
        if not vision_result.success or not vision_result.bbox:
            if vision_result.error:
                self._last_failure_reason = f"Vision API 오류: {vision_result.error}"
                logger.error("Vision API error for %s: %s", target, vision_result.error)
            else:
                self._last_failure_reason = (
                    f"Vision으로 요소를 찾지 못함: '{target}' "
                    f"(신뢰도: {vision_result.confidence:.2f})"
                )
                logger.error("Element not found by Vision: %s", target)
            self._last_vision_confidence = 0.0
            return None
        self._last_vision_confidence = vision_result.confidence
        return vision_result.bbox.to_pixels(self.adb.width, self.adb.height)

    def _save_tap_debug(self, screenshot_path: Path, target: str, coords: Optional[dict],
                        confidence: float, verified: bool) -> None:
        """find_and_tap 디버그 아티팩트 저장 — 탭 또는 후조건 판정 시점 증거.

        - screenshots_debug/taps/{ts}_{target}_{PASS|FAIL|NOTFOUND}.png : 판정에 사용한 프레임
        - screenshots_debug/find_and_tap_debug.jsonl                    : 스텝별 한 줄 요약 로그
        후조건이 있으면 탭 전 화면이 아닌 최종 검증 화면을 남긴다.
        """
        try:
            debug_dir = self.config.paths.debug_dir
            taps_dir = debug_dir / "taps"
            taps_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            status = "PASS" if verified else ("FAIL" if coords else "NOTFOUND")
            safe_target = "".join(
                c if c.isalnum() or c in "._- " else "_" for c in (target or "")
            ).strip().replace(" ", "_")[:40] or "none"
            post_verify_path = getattr(self, "_last_post_verify_screenshot", None)
            has_post_verify = bool(post_verify_path and Path(post_verify_path).exists())
            evidence_path = Path(post_verify_path) if has_post_verify else screenshot_path
            evidence_phase = "post_verification" if has_post_verify else "pre_tap"
            img_path = taps_dir / f"{ts}_{self._file_tag}_{safe_target}_{status}.png"

            # 후조건이 있으면 실제 PASS/FAIL 판정 프레임을 증거로 남긴다.
            img = Image.open(evidence_path).convert("RGB")
            draw = ImageDraw.Draw(img)
            color = (0, 200, 0) if verified else (255, 40, 40)
            if coords and not has_post_verify:
                x1, y1 = coords["x1"], coords["y1"]
                x2, y2 = coords["x2"], coords["y2"]
                cx, cy = coords["x"], coords["y"]
                draw.rectangle([x1, y1, x2, y2], outline=color, width=4)
                r = 16
                draw.line([cx - r, cy, cx + r, cy], fill=color, width=3)
                draw.line([cx, cy - r, cx, cy + r], fill=color, width=3)
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=3)
                label = f"{target} | tap=({cx},{cy}) conf={confidence:.2f} {status}"
                draw.text((max(x1, 5), max(y1 - 16, 2)), label, fill=color)
            elif has_post_verify:
                label = f"POST VERIFY {status}: {target}"
                draw.rectangle([0, 0, img.width, 28], fill=(0, 0, 0))
                draw.text((6, 6), label, fill=color)
            else:
                label = f"NOT FOUND: {target} | conf={confidence:.2f} | {self._last_failure_reason}"
                draw.text((5, 5), label, fill=color)
            img.save(img_path)

            try:
                evidence_captured_at = datetime.fromtimestamp(
                    evidence_path.stat().st_mtime
                ).isoformat(timespec="milliseconds")
            except OSError:
                evidence_captured_at = ""

            record = {
                "timestamp": ts,
                "evidence_captured_at": evidence_captured_at,
                "evidence_phase": evidence_phase,
                "step_number": getattr(self, "_current_step_number", None),
                "step_label": getattr(self, "_current_step_label", ""),
                "step_action": getattr(self, "_current_step_action", "find_and_tap"),
                "device": self._file_tag,
                "target": target,
                "tap": {"x": coords["x"], "y": coords["y"]} if coords else None,
                "bbox": {"x1": coords["x1"], "y1": coords["y1"],
                         "x2": coords["x2"], "y2": coords["y2"]} if coords else None,
                "confidence": round(float(confidence), 3),
                "resolution": self._resolution,
                "package": self._current_package,
                "verified": verified,
                "failure_reason": "" if verified else self._last_failure_reason,
                "debug_image": str(img_path),
                "source_screenshot": str(screenshot_path),
                "verification_screenshot": str(post_verify_path) if has_post_verify else "",
            }
            with open(debug_dir / "find_and_tap_debug.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

            self._last_step_evidence = {
                "evidence_image": str(img_path),
                "evidence_timestamp": ts,
                "evidence_captured_at": evidence_captured_at,
                "evidence_phase": evidence_phase,
            }

            logger.info("┌─ find_and_tap 디버그 저장: %s", img_path.name)
            tap_info = f"tap=({coords['x']},{coords['y']})" if coords else "tap=없음(미발견)"
            logger.info("│  target='%s' %s conf=%.2f → %s",
                        target, tap_info, confidence, status)
        except Exception as e:
            logger.warning("find_and_tap 디버그 저장 실패: %s", e)

    def _verify_find_and_tap_outcome(self, step, tap_source: str) -> bool:
        params = step.params or {}
        target = step.target or ""
        expect_visible = self._to_target_list(params.get("expect_visible"))
        expect_hidden = self._to_target_list(params.get("expect_hidden"))
        self._last_post_verify_confidence = 0.0
        self._last_post_verify_screenshot = None

        if tap_source != "Retry":
            wait_seconds = self._to_float(params.get("wait_seconds"), 0.0)
            if wait_seconds > 0:
                logger.info("Post-tap wait: %.1fs", wait_seconds)
                if not self._sleep_interruptible(wait_seconds):
                    self._last_failure_reason = "탭 후 대기 중 중단 요청으로 종료"
                    return False

        if not expect_visible and not expect_hidden:
            self._last_post_verify_confidence = 1.0
            logger.info(
                "%s tap for target '%s' — no expect_visible/hidden, skipping verification.",
                tap_source, target,
            )
            return True

        deadline = time.time() + step.timeout
        attempt = 0
        while time.time() < deadline and attempt < self.VERIFY_MAX_ATTEMPTS:
            attempt += 1
            stable_screenshot = self._wait_for_screen_stable(
                timeout=min(self.STABILITY_TIMEOUT_SEC, deadline - time.time())
            )
            self._last_post_verify_screenshot = stable_screenshot
            if self._verify_expected_targets(stable_screenshot, expect_visible, expect_hidden):
                self._last_post_verify_confidence = getattr(
                    self, "_last_expected_confidence", 1.0
                )
                logger.info("%s tap verified for '%s' (attempt %d).", tap_source, target, attempt)
                return True

            remaining = deadline - time.time()
            if remaining <= 0 or attempt >= self.VERIFY_MAX_ATTEMPTS:
                break
            time.sleep(min(self.POST_TAP_POLL_INTERVAL_SEC, remaining))

        logger.warning(
            "%s tap post-verification failed for target '%s' (%d attempts, timeout %ds).",
            tap_source, target, attempt, step.timeout,
        )
        return False

    def _wait_for_screen_stable(self, timeout: float | None = None, min_wait: float = 0.5) -> Path:
        timeout = self.STABILITY_TIMEOUT_SEC if timeout is None else max(0.0, timeout)
        interval = self.STABILITY_POLL_INTERVAL_SEC
        threshold = self.STABILITY_THRESHOLD

        self._sleep_interruptible(min_wait)
        prev_path = self._capture_runtime_screenshot(prefix="stable_check")
        deadline = time.time() + timeout

        while time.time() < deadline:
            if getattr(self, '_stop_event', None) and self._stop_event.is_set():
                logger.warning("Screen stable wait interrupted by stop event.")
                return prev_path
            if not self._sleep_interruptible(interval):
                return prev_path
            curr_path = self._capture_runtime_screenshot(prefix="stable_check")

            ratio = self._image_change_ratio(prev_path, curr_path)
            if ratio is None:
                prev_path = curr_path
                continue

            logger.info("Screen stability check: change_ratio=%.4f", ratio)
            if ratio < threshold:
                return curr_path
            prev_path = curr_path

        logger.warning(
            "Screen did not stabilize within %.1fs — proceeding with last capture.", timeout
        )
        return prev_path

    @staticmethod
    def _image_change_ratio(path_a: Path, path_b: Path) -> Optional[float]:
        """두 스크린샷의 픽셀 변화 비율 (0.0=동일). 비교 불가 시 None."""
        try:
            with Image.open(path_a) as img_a, Image.open(path_b) as img_b:
                a = img_a.convert("RGB")
                b = img_b.convert("RGB")
                if a.size != b.size:
                    b = b.resize(a.size)
                diff = ImageChops.difference(a, b)
                hist = diff.histogram()
                weighted = sum((i % 256) * cnt for i, cnt in enumerate(hist))
                max_val = 255 * a.width * a.height * 3
                return (weighted / max_val) if max_val else 0.0
        except Exception:
            return None

    def _cleanup_step_files(self) -> None:
        count = 0
        for d in (self.config.paths.screenshots_dir, self.config.paths.debug_dir):
            for f in d.glob("*.png"):
                if d == self.config.paths.screenshots_dir and f.name.startswith("live_"):
                    continue
                try:
                    f.unlink()
                    count += 1
                except Exception:
                    pass
        if count:
            logger.info(f"🧹 스텝 완료 — 임시 파일 {count}개 삭제")

    def _capture_runtime_screenshot(self, prefix: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = self.config.paths.debug_dir / f"{prefix}_{self._file_tag}_{timestamp}.png"
        self.adb.screenshot(path)
        self._publish_live_screenshot(path)
        return path

    def _publish_live_screenshot(self, source_path: Path) -> None:
        """판정에 사용할 가장 최신 캡처를 실행 중 미리보기에 원자적으로 반영한다."""
        try:
            live_path = self.config.paths.screenshots_dir / f"live_{self._file_tag}.png"
            temp_path = live_path.with_suffix(".tmp")
            shutil.copyfile(source_path, temp_path)
            temp_path.replace(live_path)
        except Exception as exc:
            logger.warning("Live screenshot publish failed: %s", exc)

    def _verify_screen(self, screenshot_path: Path, target: str, step=None) -> tuple[bool, float]:
        if not target:
            logger.error("verify action requires target")
            self._last_failure_reason = "verify target이 지정되지 않음"
            return False, 0.0
        vision_result = self.vision_lite.find_element(
            screenshot_path, target, self.config.paths.debug_dir, state_context=self._sctx
        )
        if not vision_result.success or not vision_result.bbox:
            # scroll_search: 첫 화면에 없으면 스크롤하며 재탐색 (find_and_tap과 동일한 로직 재사용).
            # verify는 탭하지 않으므로 발견 좌표 자체는 버리고 "찾았다"는 결과만 쓴다.
            if step is not None and (step.params or {}).get("scroll_search"):
                latest_path, coords = self._scroll_search(step, target, screenshot_path)
                if coords:
                    self._last_pass_detail = f"'{target}' 화면에서 확인됨 (스크롤 탐색)"
                    self._save_read_debug(latest_path, target, self._last_pass_detail, True, "verify")
                    return True, 1.0
                self._save_read_debug(latest_path, target, self._last_failure_reason, False, "verify")
                return False, 0.0
            self._last_failure_reason = (
                f"화면에서 '{target}'을 찾지 못함 (신뢰도: {vision_result.confidence:.2f})"
            )
            self._save_read_debug(screenshot_path, target, self._last_failure_reason, False, "verify")
            return False, vision_result.confidence
        self._last_pass_detail = f"'{target}' 화면에서 확인됨"
        self._save_read_debug(screenshot_path, target, self._last_pass_detail, True, "verify")
        return True, vision_result.confidence

    def _evaluate_value_assertion(self, label: str, value: str, params: dict,
                                  result: TestResult) -> tuple[bool, str]:
        """read_text/read_screen 공용: save_as/compare_with/expect_* 판정 로직.
        (값 읽기는 호출부에서 이미 완료된 상태) 반환: (ok, detail/failure 메시지)

        compare_with이 있으면 result.economy_summary에 {name, before, after, delta, passed}
        행을 추가한다 — 리포트 화면에서 재화/아이템 전후 비교를 표로 한눈에 보여주기 위함
        (스텝 텍스트를 일일이 안 읽어도 되도록, 2026-07-23 추가).
        """
        save_as = params.get("save_as")
        compare_with = params.get("compare_with")
        expect_changed = params.get("expect_changed")
        expect_increase = params.get("expect_increase")
        expect_decrease = params.get("expect_decrease")
        expect_delta = params.get("expect_delta")
        expect_delta_from = params.get("expect_delta_from")
        delta_tolerance = params.get("delta_tolerance", 0)

        if not hasattr(result, "context"):
            result.context = {}

        detail = f"'{label}' = '{value}'"
        if save_as:
            result.context[save_as] = value
            detail += f" — '{save_as}'로 저장"

        # expect_delta_from: 앞서 화면에서 읽어 저장한 값을 그대로 기대 증감량으로 쓴다.
        # 예) 결과 화면의 보상 골드를 reward_gold로 저장 → 아웃게임 골드가 그만큼 늘었는지
        # 검증. 기대값을 템플릿에 하드코딩할 수 없는(매번 달라지는) 재화 검증용.
        # 부호는 delta_sign으로 준다: 기본 +1(증가), 소비 검증이면 -1.
        if expect_delta is None and expect_delta_from:
            source_raw = result.context.get(expect_delta_from)
            if source_raw is None:
                return False, f"expect_delta_from '{expect_delta_from}' 값이 없습니다."
            source_num = self._to_number(source_raw)
            if source_num is None:
                return False, (
                    f"expect_delta_from '{expect_delta_from}' 값을 숫자로 읽을 수 없습니다 "
                    f"({source_raw!r})."
                )
            sign_raw = params.get("delta_sign", 1)
            try:
                sign = -1.0 if float(sign_raw) < 0 else 1.0
            except (TypeError, ValueError):
                sign = 1.0
            expect_delta = abs(source_num) * sign
            detail += f" (기대 증감 {expect_delta:+g} ← '{expect_delta_from}')"

        if not compare_with:
            return True, detail

        prev = result.context.get(compare_with)
        if prev is None:
            return False, f"compare_with '{compare_with}' 값이 없습니다."

        def record(ok: bool, detail: str, delta_text: Optional[str]) -> tuple[bool, str]:
            if not hasattr(result, "economy_summary"):
                result.economy_summary = []
            result.economy_summary.append({
                "name": label,
                "before": prev,
                "after": value,
                "delta": delta_text,
                "passed": ok,
            })
            return ok, detail

        # 정확한 증감량 검증 (재화 지급/차감 수치까지 정밀 확인용)
        if expect_delta is not None:
            prev_n, curr_n = self._to_number(prev), self._to_number(value)
            if prev_n is None or curr_n is None:
                return record(False, f"숫자 비교 불가 ({prev!r} → {value!r})", None)
            actual_delta = curr_n - prev_n
            if abs(actual_delta - float(expect_delta)) > delta_tolerance:
                return record(False, (f"{expect_delta:+g} 변화 기대했으나 "
                              f"{prev_n:g} → {curr_n:g} ({actual_delta:+g})"), f"{actual_delta:+g}")
            return record(True, f"'{label}' {prev_n:g} → {curr_n:g} ({actual_delta:+g}) — 기대값과 일치",
                          f"{actual_delta:+g}")

        # 숫자 증감 검증 (재화 지급/차감 확인용) — 단순 변경 여부보다 강한 검증
        if expect_increase or expect_decrease:
            prev_n, curr_n = self._to_number(prev), self._to_number(value)
            if prev_n is None or curr_n is None:
                return record(False, f"숫자 비교 불가 ({prev!r} → {value!r})", None)
            if expect_increase and curr_n <= prev_n:
                return record(False, f"증가 기대했으나 {prev_n:g} → {curr_n:g}", f"{curr_n - prev_n:+g}")
            if expect_decrease and curr_n >= prev_n:
                return record(False, f"감소 기대했으나 {prev_n:g} → {curr_n:g}", f"{curr_n - prev_n:+g}")
            delta = curr_n - prev_n
            return record(True, (f"'{label}' {prev_n:g} → {curr_n:g} ({delta:+g}) — "
                          + ("증가 확인" if expect_increase else "감소 확인")), f"{delta:+g}")

        changed = prev != value
        prev_n, curr_n = self._to_number(prev), self._to_number(value)
        delta_text = f"{curr_n - prev_n:+g}" if (prev_n is not None and curr_n is not None) else None
        if expect_changed is True and not changed:
            return record(False, f"변경 기대했으나 동일함 ({value})", delta_text)
        if expect_changed is False and changed:
            return record(False, f"유지 기대했으나 변경됨 ({prev} → {value})", delta_text)
        return record(True, f"'{label}' {prev} → {value} ({'변경됨' if changed else '유지됨'})", delta_text)

    def _read_text_step(self, screenshot_path: Path, step, result: TestResult) -> bool:
        target = step.target
        if not target:
            logger.error("read_text action requires target")
            return False

        params = step.params or {}
        value = self.vision.read_text(screenshot_path, target, state_context=self._sctx)
        if value is None:
            self._last_failure_reason = f"read_text: '{target}' 텍스트를 찾지 못함"
            logger.error(self._last_failure_reason)
            self._save_read_debug(screenshot_path, target, self._last_failure_reason, False)
            return False

        logger.info("read_text: '%s' = %s", target, value)
        ok, detail = self._evaluate_value_assertion(target, value, params, result)
        if not ok:
            self._last_failure_reason = f"read_text: {detail}"

        if ok:
            self._last_pass_detail = detail
            logger.info("read_text 통과: %s", detail)
        else:
            logger.error(self._last_failure_reason)
        # 증거 스크린샷 — 읽은 값/비교 결과를 라벨로 새겨 리포트 갤러리에 노출
        self._save_read_debug(
            screenshot_path, target, detail if ok else self._last_failure_reason, ok)
        return ok

    # Vision에게 화면만이 아니라 게임이 알려준 상태도 함께 준다.
    # 스텝마다 1회만 조회해 그 스텝 안의 모든 Vision 호출에서 재사용한다.
    _STATE_CONTEXT_ACTIONS = {
        ActionType.VERIFY,
        ActionType.READ_TEXT,
        ActionType.READ_ITEMS,
        ActionType.READ_SCREEN,
    }

    def _refresh_state_context(self, step=None) -> None:
        """스텝 시작 시 게임 상태를 1회 조회해 캐시한다 (Vision 프롬프트 주입용)."""
        if step is not None and step.action not in self._STATE_CONTEXT_ACTIONS:
            return
        if (
            step is not None
            and step.action == ActionType.VERIFY
            and (step.params or {}).get("scene")
        ):
            return
        try:
            self._state_context = self.unity.state_context_text()
        except Exception as exc:
            logger.debug("state_context 갱신 실패: %s", exc)
            self._state_context = ""

    @property
    def _sctx(self) -> str:
        return getattr(self, "_state_context", "") or ""

    def _log_cheat_usage(self, result: TestResult, kind: str, target: str,
                         detail: str, ok: bool) -> None:
        """이 실행에서 실제로 쓰인 치트/프로퍼티를 기록한다.

        어떤 검증이 어떤 내부 상태 조작에 기대고 있는지 리포트에서 바로 보기 위한 것.
        상태 주입(state_context)이 매 스텝 조회하는 프로퍼티는 여기 남기지 않는다 —
        "쓰인 내역"만 남겨야 의미가 있다.
        """
        if not hasattr(result, "cheat_log") or result.cheat_log is None:
            result.cheat_log = []
        entry = {
            "seq": len(result.cheat_log) + 1,
            "step": result.steps_executed + 1,
            "kind": kind,
            "target": target,
            "detail": detail,
            "ok": ok,
        }
        result.cheat_log.append(entry)
        logger.info("│  ⚙ [%s] %s — %s%s", kind, target, detail, "" if ok else "  (실패)")

    def _repeat_until_step(self, step, result: TestResult) -> bool:
        """조건이 만족될 때까지 하위 스텝 묶음을 반복 실행한다.

        웨이브 디펜스처럼 "준비 → 배치 → 진행"을 N번 되풀이해야 하는 흐름을 스텝 수십 개로
        펼쳐 쓰지 않기 위한 액션. 웨이브 수가 다른 스테이지에도 그대로 재사용된다.

        params:
          steps            : 반복할 스텝 목록 (일반 스텝과 같은 스키마)
          until_visible    : 이 대상이 화면에 보이면 종료 (Vision)
          until_hidden     : 이 대상이 화면에서 사라지면 종료 (Vision)
          until_scene      : 이 씬 접두사가 되면 종료 (치트 목록 기반, Vision 호출 없음)
          max_iterations   : 최대 반복 횟수 (기본 20) — 무한 루프 방지
          timeout_seconds  : 전체 제한 시간 (기본 step.timeout)
          check_every      : N회 반복마다 조건 확인 (기본 1). Vision 조건일 때 호출 절약용
          strict           : true면 하위 스텝이 실패하는 즉시 중단 (기본 false)

        기본값이 strict=false인 이유 — 반복 루프에서는 하위 스텝의 실패가 정상적인 경우가
        많다. 예: 크레딧이 모자라 소환이 안 되거나, 전투 중이라 시작 버튼이 없는 회차.
        """
        params = step.params or {}
        raw_steps = params.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            self._last_failure_reason = "repeat_until: params.steps(list)가 필요합니다."
            logger.error(self._last_failure_reason)
            return False

        try:
            body = [TestStep(**sub) for sub in raw_steps]
        except (TypeError, ValueError) as exc:
            self._last_failure_reason = f"repeat_until: 하위 스텝 파싱 실패 — {exc}"
            logger.error(self._last_failure_reason)
            return False

        until_visible = str(params.get("until_visible") or "").strip()
        until_hidden = str(params.get("until_hidden") or "").strip()
        until_scene = str(params.get("until_scene") or "").strip()
        if not (until_visible or until_hidden or until_scene):
            self._last_failure_reason = (
                "repeat_until: until_visible / until_hidden / until_scene 중 하나가 필요합니다."
            )
            logger.error(self._last_failure_reason)
            return False

        try:
            max_iterations = max(1, min(200, int(params.get("max_iterations", 20))))
        except (TypeError, ValueError):
            max_iterations = 20
        try:
            check_every = max(1, int(params.get("check_every", 1)))
        except (TypeError, ValueError):
            check_every = 1
        timeout_seconds = self._to_float(params.get("timeout_seconds"), float(step.timeout))
        strict = bool(params.get("strict"))
        deadline = time.monotonic() + timeout_seconds

        def condition_met() -> bool:
            if until_scene:
                expected = [t.strip().lower() for t in until_scene.split("|") if t.strip()]
                prefixes = [p.lower() for p in self.unity.current_scene_prefixes_v2()]
                return any(name in prefixes for name in expected)
            probe = self._capture_runtime_screenshot(prefix="repeat")
            target = until_visible or until_hidden
            found = self.vision_lite.find_element(
                probe, target, self.config.paths.debug_dir, state_context=self._sctx
            )
            visible = bool(found.success and found.bbox)
            return visible if until_visible else (not visible)

        for iteration in range(max_iterations):
            if iteration % check_every == 0 and condition_met():
                self._last_pass_detail = (
                    f"반복 {iteration}회 후 종료 조건 충족 "
                    f"({until_visible or until_hidden or ('씬=' + until_scene)})"
                )
                logger.info("│  → %s", self._last_pass_detail)
                return True

            if time.monotonic() > deadline:
                break

            logger.info("│  ↻ repeat_until %d/%d", iteration + 1, max_iterations)
            for sub in body:
                if getattr(self, '_stop_event', None) and self._stop_event.is_set():
                    self._last_failure_reason = "repeat_until: 중단 요청으로 종료"
                    return False
                ok, _ = self._execute_step(sub, result)
                if strict and not ok:
                    self._last_failure_reason = (
                        f"repeat_until(strict): 하위 스텝 실패 — {sub.description or sub.target}"
                    )
                    logger.error(self._last_failure_reason)
                    return False

        # 마지막 반복 뒤 한 번 더 확인
        if condition_met():
            self._last_pass_detail = f"반복 {max_iterations}회 후 종료 조건 충족"
            return True

        self._last_failure_reason = (
            f"repeat_until: {max_iterations}회 / {timeout_seconds:.0f}초 안에 종료 조건을 "
            f"만족하지 못함 ({until_visible or until_hidden or ('씬=' + until_scene)})"
        )
        logger.error(self._last_failure_reason)
        return False

    def _verify_scene_step(self, step) -> bool:
        """앱 내부 v2 치트 목록의 id 접두사로 현재 씬을 판정한다.

        치트는 씬 단위로 등록되므로 목록에 잡히는 접두사가 곧 현재 씬이다
        (이지스 디펜스: 전투 화면=`ingame`, 로비=`outgame`). 화면을 Vision으로 읽지 않아
        스크린샷 해석 오차가 없고 호출 비용도 없다.

        params.scene: 기대 씬 접두사. "ingame" 또는 "ingame|outgame"처럼 |로 여러 개 허용.
        접두사 이름은 게임마다 다르므로 템플릿이 지정한다.
        """
        params = step.params or {}
        expected = [
            token.strip().lower()
            for token in str(params.get("scene") or "").split("|")
            if token.strip()
        ]
        if not expected:
            self._last_failure_reason = "verify: params.scene이 비어 있습니다."
            logger.error(self._last_failure_reason)
            return False

        prefixes = [p.lower() for p in self.unity.current_scene_prefixes_v2()]
        if not prefixes:
            self._last_failure_reason = (
                "verify(scene): 앱 내부 v2 치트 목록을 읽지 못했습니다 "
                "(로딩 중이거나 치트 서버 미동작)."
            )
            logger.error(self._last_failure_reason)
            return False

        matched = [name for name in expected if name in prefixes]
        if matched:
            self._last_pass_detail = (
                f"씬 판정: 기대 '{'|'.join(expected)}' — 현재 등록 접두사 {prefixes} 에 "
                f"'{matched[0]}' 존재"
            )
            logger.info("│  → %s", self._last_pass_detail)
            return True

        self._last_failure_reason = (
            f"verify(scene): 기대 씬 '{'|'.join(expected)}'이 아님 — "
            f"현재 등록 접두사 {prefixes}"
        )
        logger.error(self._last_failure_reason)
        return False

    # ── v2 치트/프로퍼티 스텝 ──────────────────────────────────────────
    # Unity 앱 내부 v2 API(sr_api.md)를 스텝에서 직접 쓰기 위한 액션들.
    # UI로는 만들 수 없는 상태(목표 웨이브, 몬스터 소환, 재화, 무적 등)를 세팅하거나,
    # 화면에 안 보이는 내부 값을 근거로 판정할 때 쓴다.

    @staticmethod
    def _resolve_v2_id(step) -> str:
        params = step.params or {}
        return str(params.get("id") or step.target or "").strip()

    def _call_cheat_step(self, step, result: TestResult = None) -> bool:
        params = step.params or {}
        cheat_id = self._resolve_v2_id(step)
        if not cheat_id:
            self._last_failure_reason = "call_cheat: params.id 또는 target에 치트 id가 필요합니다."
            logger.error(self._last_failure_reason)
            return False

        args = params.get("args") or {}
        if not isinstance(args, dict):
            self._last_failure_reason = "call_cheat: params.args는 객체(dict)여야 합니다."
            logger.error(self._last_failure_reason)
            return False

        # 치트는 씬 단위로 등록된다 — 현재 씬에 없는 치트를 "적용할 대상 없음"으로
        # 넘기고 싶으면 params.not_found_ok를 켠다.
        ok = self.unity.execute_cheat_v2(
            cheat_id, args, not_found_ok=bool(params.get("not_found_ok"))
        )
        if result is not None:
            self._log_cheat_usage(
                result, "call_cheat", cheat_id,
                f"args={args}" if args else "인자 없음", ok,
            )
        if ok:
            self._last_pass_detail = (
                f"치트 '{cheat_id}' 실행" + (f" args={args}" if args else "")
            )
            wait_seconds = self._to_float(params.get("wait_seconds"), 0.0)
            if wait_seconds > 0:
                logger.info("call_cheat 적용 대기: %.1fs", wait_seconds)
                time.sleep(wait_seconds)
        else:
            self._last_failure_reason = f"call_cheat: 치트 '{cheat_id}' 실행 실패"
        return ok

    def _set_property_step(self, step, result: TestResult = None) -> bool:
        params = step.params or {}
        prop_id = self._resolve_v2_id(step)
        if not prop_id:
            self._last_failure_reason = "set_property: params.id 또는 target에 프로퍼티 id가 필요합니다."
            logger.error(self._last_failure_reason)
            return False
        if "value" not in params:
            self._last_failure_reason = "set_property: params.value가 필요합니다."
            logger.error(self._last_failure_reason)
            return False

        applied = self.unity.set_property_v2(prop_id, params["value"])
        if applied is None:
            self._last_failure_reason = (
                f"set_property: '{prop_id}'에 {params['value']!r} 쓰기 실패 "
                "(쓰기 불가/타입·범위 오류이거나 현재 씬에 없는 프로퍼티)"
            )
            return False

        shown = applied.get("Display")
        if not isinstance(shown, str) or not shown.strip():
            shown = applied.get("Value")
        if result is not None:
            self._log_cheat_usage(result, "set_property", prop_id, f"= {shown}", True)
        self._last_pass_detail = f"'{prop_id}' = {shown} 적용"
        wait_seconds = self._to_float(params.get("wait_seconds"), 0.0)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        return True

    @staticmethod
    def _property_value_matches(expected: Any, actual: Any) -> bool:
        """expect_value 비교 — bool/숫자/문자열 표기 차이를 흡수한다."""
        if isinstance(expected, bool) or isinstance(actual, bool):
            def to_bool(v: Any) -> Optional[bool]:
                if isinstance(v, bool):
                    return v
                if isinstance(v, str):
                    lowered = v.strip().lower()
                    if lowered in {"true", "on", "1", "켜짐", "활성"}:
                        return True
                    if lowered in {"false", "off", "0", "꺼짐", "비활성"}:
                        return False
                return None
            return to_bool(expected) is not None and to_bool(expected) == to_bool(actual)
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            return abs(float(expected) - float(actual)) < 1e-9
        return str(expected).strip().lower() == str(actual).strip().lower()

    def _check_property_step(self, step, result: TestResult) -> bool:
        params = step.params or {}
        prop_id = self._resolve_v2_id(step)
        if not prop_id:
            self._last_failure_reason = "check_property: params.id 또는 target에 프로퍼티 id가 필요합니다."
            logger.error(self._last_failure_reason)
            return False

        values = self.unity.get_property_values_v2([prop_id])
        item = values.get(prop_id)
        if item is None:
            self._last_failure_reason = (
                f"check_property: '{prop_id}' 값을 읽지 못했습니다 "
                "(현재 씬에 등록되지 않았거나 읽기 불가)"
            )
            logger.error(self._last_failure_reason)
            return False

        read_error = item.get("Error")
        if read_error:
            self._last_failure_reason = f"check_property: '{prop_id}' 읽기 오류 — {read_error}"
            logger.error(self._last_failure_reason)
            return False

        raw = item.get("Value")
        display = item.get("Display")
        label = str(params.get("label") or prop_id)
        logger.info("check_property: %s = %r (display=%r)", prop_id, raw, display)

        # choiceable-float 같은 타입은 Value가 {"value": 1.0, "suffix": "x", ...} 형태로 온다
        # (예: debug.time_scale) — 판정에는 안쪽 실제 값을 쓴다.
        if isinstance(raw, dict) and "value" in raw:
            raw = raw["value"]

        if "expect_value" in params and not self._property_value_matches(params["expect_value"], raw):
            self._last_failure_reason = (
                f"check_property: '{label}' 기대값 {params['expect_value']!r} != 실제 {raw!r}"
            )
            logger.error(self._last_failure_reason)
            return False

        # save_as/compare_with/expect_delta 등 숫자 비교는 단위가 붙을 수 있는 Display 대신
        # 원본 Value를 쓴다. 값이 없을 때만 Display로 대체한다.
        if raw is None and isinstance(display, str) and display.strip():
            assert_text = display
        else:
            assert_text = str(raw)

        ok, detail = self._evaluate_value_assertion(label, assert_text, params, result)
        self._log_cheat_usage(result, "check_property", prop_id, f"읽은 값 {raw!r}", ok)
        if ok:
            self._last_pass_detail = detail
        else:
            self._last_failure_reason = f"check_property: {detail}"
            logger.error(self._last_failure_reason)
        return ok

    def _read_screen_step(self, screenshot_path: Path, step, result: TestResult) -> bool:
        """한 화면에 같이 보이는 여러 항목(재화 값 여러 개 + 카드 존재 확인 등)을
        vision 호출 1번으로 모아서 확인한다 — 매번 탭 이동/개별 호출 없이 한 번에 검증.

        params.items: [{"name", "description", save_as?, compare_with?, expect_*?, optional?}, ...]
        - description에 값을 읽을 대상("다이아 수량")인지 존재만 확인할 조건("검귀 카드 —
          다이아/자물쇠 아이콘 없음")인지 구체적으로 적어야 vision이 올바르게 판단한다.
        - 값이 있는 항목은 read_text와 동일한 save_as/compare_with/expect_* 규칙을 그대로 쓴다.
        - 값 없이 존재만 확인하는 항목은 found 여부만 판정한다 (compare_with 미지원).
        """
        params = step.params or {}
        items_param = params.get("items")
        if not items_param or not isinstance(items_param, list):
            self._last_failure_reason = "read_screen: params.items(list)가 필요합니다."
            logger.error(self._last_failure_reason)
            return False

        vision_items = [
            {"name": it.get("name"), "description": it.get("description", "")}
            for it in items_param if it.get("name")
        ]
        if not vision_items:
            self._last_failure_reason = "read_screen: items에 유효한 name이 없습니다."
            logger.error(self._last_failure_reason)
            return False

        results = self.vision.read_screen_batch(
            screenshot_path, vision_items, state_context=self._sctx)
        results_by_name = {r.get("name"): r for r in results if isinstance(r, dict)}

        # scroll_search: 항목 중 일부(예: 스크롤해야 보이는 카드)가 안 잡히면 스크롤하며
        # 배치 호출을 다시 시도한다. 상단 고정 표시줄(재화 등)은 스크롤해도 그대로 보이는
        # 화면이 많아서, 스크롤이 필요한 항목과 즉시 보이는 항목을 같은 화면에서 한 번에
        # 묶어 확인할 수 있다 (예: 마신석 수량 + 스크롤 필요한 검귀 카드).
        if params.get("scroll_search"):
            required_names = {it.get("name") for it in items_param if not it.get("optional")}
            max_scrolls = int(params.get("max_scrolls", self.SCROLL_SEARCH_MAX_DEFAULT))
            scroll_fraction = self._to_float(params.get("scroll_fraction"), 0.35)
            for i in range(max_scrolls):
                missing = {n for n in required_names if not results_by_name.get(n, {}).get("found")}
                if not missing:
                    break
                if getattr(self, '_stop_event', None) and self._stop_event.is_set():
                    logger.warning("read_screen scroll_search: 사용자 중단")
                    break
                before_path = screenshot_path
                self._scroll_one_page("down", fraction=scroll_fraction)
                screenshot_path = self._wait_for_screen_stable()
                ratio = self._image_change_ratio(before_path, screenshot_path)
                if ratio is not None and ratio < self.SCROLL_END_THRESHOLD:
                    logger.info("read_screen scroll_search: 화면 변화 없음(%.4f) — 리스트 끝, 중단 (%d회 스크롤)",
                                ratio, i + 1)
                    break
                new_results = self.vision.read_screen_batch(
                    screenshot_path, vision_items, state_context=self._sctx)
                for r in new_results:
                    if isinstance(r, dict) and r.get("name"):
                        results_by_name[r["name"]] = r
            logger.info("read_screen scroll_search: 최종 미발견 항목=%s",
                        {n for n in required_names if not results_by_name.get(n, {}).get("found")} or "없음")

        target_label = step.target or ", ".join(str(it.get("name")) for it in items_param)

        if not hasattr(result, "context"):
            result.context = {}

        ok = True
        notes: list[str] = []
        for it in items_param:
            name = it.get("name")
            r = results_by_name.get(name)
            if r is None or not r.get("found"):
                if it.get("optional"):
                    notes.append(f"{name}: 미확인(선택 항목, 건너뜀)")
                    continue
                notes.append(f"{name}: 화면에서 확인하지 못함")
                ok = False
                continue

            value = r.get("value")
            if value is None:
                # 존재 확인 전용 항목 (예: 검귀 카드) — found=true면 조건 충족
                notes.append(f"{name}: 확인됨")
                if it.get("save_as"):
                    result.context[it["save_as"]] = True
                continue

            item_ok, item_detail = self._evaluate_value_assertion(name, value, it, result)
            notes.append(f"{name}: {item_detail}" if not item_ok else item_detail)
            if not item_ok:
                ok = False

        detail_text = " / ".join(notes) if notes else "결과 없음"
        if ok:
            self._last_pass_detail = detail_text
            logger.info("read_screen 통과: %s", detail_text)
        else:
            self._last_failure_reason = detail_text
            logger.error("read_screen 실패: %s", detail_text)

        self._save_read_debug(screenshot_path, target_label, detail_text, ok, "read_screen")
        return ok

    def _read_items_step(self, screenshot_path: Path, step, result: TestResult) -> bool:
        """화면의 아이템 목록(보유/미보유 상태)을 스캔해 저장하고, 이전 스냅샷과 비교 검증.
        여러 재화/아이템을 동시에 지급하는 상품(뉴비패키지 등)을 정밀 검증할 때
        구매 전/후 각각 read_items로 마물·유물 탭 등을 스냅샷 떠서 compare_with로 비교한다.
        """
        target = step.target
        if not target:
            logger.error("read_items action requires target")
            return False

        params = step.params or {}
        save_as = params.get("save_as")
        compare_with = params.get("compare_with")
        expect_new_owned_count = params.get("expect_new_owned_count")
        expect_new_owned = params.get("expect_new_owned")
        expect_no_change = params.get("expect_no_change")

        items = self.vision.read_item_states(screenshot_path, target)
        if not items:
            self._last_failure_reason = f"read_items: '{target}'에서 항목을 찾지 못함"
            logger.error(self._last_failure_reason)
            self._save_read_debug(screenshot_path, target, self._last_failure_reason, False, "read_items")
            return False

        owned_names = [i["name"] for i in items if i.get("owned")]
        logger.info("read_items: '%s' = %d개 (보유 %d개)", target, len(items), len(owned_names))

        if not hasattr(result, "context"):
            result.context = {}

        detail = f"'{target}' 항목 {len(items)}개 (보유 {len(owned_names)}개)"
        if save_as:
            result.context[save_as] = items
            detail += f" — '{save_as}'로 저장"

        ok = True
        if compare_with:
            prev = result.context.get(compare_with)
            if prev is None:
                self._last_failure_reason = f"read_items: compare_with '{compare_with}' 값이 없습니다."
                ok = False
            else:
                prev_owned = {i.get("name"): bool(i.get("owned")) for i in prev}
                newly_owned = [i["name"] for i in items
                               if i.get("owned") and not prev_owned.get(i["name"], False)]
                newly_lost = [i["name"] for i in items
                              if not i.get("owned") and prev_owned.get(i["name"], False)]
                detail = (f"'{target}' 신규 보유 {len(newly_owned)}개 {newly_owned} / "
                          f"신규 미보유 {len(newly_lost)}개 {newly_lost}")

                if expect_new_owned_count is not None and len(newly_owned) != expect_new_owned_count:
                    self._last_failure_reason = (
                        f"read_items: 신규 보유 {expect_new_owned_count}개 기대했으나 "
                        f"{len(newly_owned)}개 ({newly_owned})")
                    ok = False
                if ok and expect_new_owned:
                    missing = [n for n in expect_new_owned if n not in newly_owned]
                    if missing:
                        self._last_failure_reason = f"read_items: 신규 보유 기대 항목 누락 — {missing}"
                        ok = False
                if ok and expect_no_change and (newly_owned or newly_lost):
                    self._last_failure_reason = f"read_items: 변경 없음을 기대했으나 변경됨 — {detail}"
                    ok = False

                if not hasattr(result, "economy_summary"):
                    result.economy_summary = []
                result.economy_summary.append({
                    "name": target,
                    "before": f"보유 {sum(prev_owned.values())}개",
                    "after": f"보유 {len(owned_names)}개",
                    "delta": f"신규 {len(newly_owned)}개" + (f" {newly_owned}" if newly_owned else ""),
                    "passed": ok,
                })

        if ok:
            self._last_pass_detail = detail
            logger.info("read_items 통과: %s", detail)
        else:
            logger.error(self._last_failure_reason)
        self._save_read_debug(
            screenshot_path, target, detail if ok else self._last_failure_reason, ok, "read_items")
        return ok

    def _save_read_debug(self, screenshot_path: Path, target: str,
                         detail: str, passed: bool, action_label: str = "read_text") -> None:
        """read_text/read_items 증거 이미지 저장 — 읽은 값과 판정 근거를 이미지에 새겨
        taps 갤러리(find_and_tap_debug.jsonl)에 함께 노출한다."""
        try:
            debug_dir = self.config.paths.debug_dir
            taps_dir = debug_dir / "taps"
            taps_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            status = "PASS" if passed else "FAIL"
            safe_target = "".join(
                c if c.isalnum() or c in "._- " else "_" for c in (target or "")
            ).strip().replace(" ", "_")[:40] or "none"
            img_path = taps_dir / f"{ts}_{self._file_tag}_read_{safe_target}_{status}.png"

            img = Image.open(screenshot_path).convert("RGB")
            draw = ImageDraw.Draw(img)
            color = (0, 200, 0) if passed else (255, 40, 40)
            draw.text((5, 5), f"{action_label} {status}: {detail}", fill=color)
            img.save(img_path)

            try:
                evidence_captured_at = datetime.fromtimestamp(
                    screenshot_path.stat().st_mtime
                ).isoformat(timespec="milliseconds")
            except OSError:
                evidence_captured_at = ""

            record = {
                "timestamp": ts,
                "evidence_captured_at": evidence_captured_at,
                "evidence_phase": "final_verification",
                "step_number": getattr(self, "_current_step_number", None),
                "step_label": getattr(self, "_current_step_label", ""),
                "step_action": getattr(self, "_current_step_action", action_label),
                "device": self._file_tag,
                "target": f"[읽기] {target}",
                "tap": None,
                "bbox": None,
                "confidence": 1.0,
                "resolution": self._resolution,
                "package": self._current_package,
                "verified": passed,
                "failure_reason": "" if passed else detail,
                "pass_reason": detail if passed else "",
                "debug_image": str(img_path),
                "source_screenshot": str(screenshot_path),
            }
            with open(debug_dir / "find_and_tap_debug.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._last_step_evidence = {
                "evidence_image": str(img_path),
                "evidence_timestamp": ts,
                "evidence_captured_at": evidence_captured_at,
                "evidence_phase": "final_verification",
            }
            logger.info("┌─ %s 증거 저장: %s", action_label, img_path.name)
        except Exception as e:
            logger.warning("%s 증거 저장 실패: %s", action_label, e)

    @staticmethod
    def _to_number(text) -> Optional[float]:
        """'1,234개' 같은 표시 문자열에서 숫자만 추출. 파싱 불가면 None."""
        if text is None:
            return None
        s = re.sub(r"[^\d.\-]", "", str(text))
        if not s or s in ("-", ".", "-."):
            return None
        try:
            return float(s)
        except ValueError:
            return None
