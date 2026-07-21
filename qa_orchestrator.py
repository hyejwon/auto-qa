import os
import re
import shutil
import threading
import json
import unicodedata
from pathlib import Path
from datetime import datetime
from typing import Optional
import time
import logging

from PIL import Image, ImageChops, ImageDraw
from langfuse_disabled import get_client

from config import Config
from adb_controller import ADBController, parse_ui_nodes
from vision_agent import GeminiVisionAgent
from test_manager import TestCaseManager, TestResult, ActionType, TestCase
from planner_node import PlannerNode
from unity_api_client import UnityAPIClient
from element_cache import ElementCache, CommonTapCache, CachedElement
from eval_agent import evaluate_result_dict
from sr_debugger import SRDebuggerController
from dotenv import load_dotenv

load_dotenv()
langfuse = get_client()
logger = logging.getLogger(__name__)

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
            model="gemini-2.5-flash"
        )
        self.test_manager = TestCaseManager(config.paths.testcases_dir)
        self.planner = PlannerNode(
            project=config.gemini.project,
            location=config.gemini.location,
            model=config.gemini.model
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
            try:
                for idx, step in enumerate(testcase.steps):
                    if stop_event and stop_event.is_set():
                        logger.warning("⏹️ 사용자 중단 — 테스트를 중지합니다.")
                        result.status = "FAIL"
                        result.error_message = "사용자에 의해 중단됨"
                        break

                    self._last_failure_reason = ""
                    self._last_pass_detail = ""
                    step_skipped = False
                    label = step.description or step.action
                    self._current_step_number = idx + 1
                    self._current_step_label = str(label)
                    self._current_step_action = getattr(step.action, "value", str(step.action))
                    target_info = f"  → 대상: {step.target}" if step.target else ""
                    logger.info("")
                    logger.info(f"┌─ [{idx + 1}/{total}] {label}")
                    if target_info:
                        logger.info(f"│  {target_info.strip()}")

                    with langfuse.start_as_current_observation(
                        as_type="span",
                        name=f"step_{idx + 1}_{step.action}",
                        input={"step": idx + 1, "action": step.action, "target": step.target, "description": label},
                    ) as step_span:
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
                                        retry_success, _ = self._execute_step(step, result)
                                        if retry_success and retry_success != self._TAP_OK_VERIFY_FAIL:
                                            result.steps_passed += 1
                                            success = True
                                            logger.info("└─ ✅ 완료 (재시도 성공)")
                                            break
                                        elif retry_success == self._TAP_OK_VERIFY_FAIL:
                                            tap_ok_verify_fail = True

                            if not success:
                                if (step.params or {}).get("optional"):
                                    # 선택 스텝: 조건부 팝업처럼 안 나올 수도 있는 대상 — 실패해도 건너뛰고 계속
                                    step_skipped = True
                                    logger.info("└─ ⏭️ 건너뜀 (선택 스텝 — 대상 미노출)")
                                else:
                                    logger.error("└─ ❌ 실패")
                                    if self._last_failure_reason:
                                        logger.error(f"│    사유: {self._last_failure_reason}")
                                    result.status = "FAIL"
                                    reason_suffix = f" — {self._last_failure_reason}" if self._last_failure_reason else ""
                                    result.error_message = f"Step {idx + 1} 실패: {label}{reason_suffix}"

                        step_span.update(output={"passed": success, "vision_confidence": confidence})

                    result.step_results.append({
                        "step": idx + 1,
                        "label": label,
                        "action": getattr(step.action, "value", str(step.action)),
                        "target": step.target or "",
                        "passed": success,
                        "skipped": step_skipped,
                        "vision_confidence": confidence,
                        "failure_reason": "" if (success or step_skipped) else self._last_failure_reason,
                        "pass_reason": self._last_pass_detail if success else "",
                    })

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
                    f"  결과: {icon}  |  {result.steps_passed}/{total} 통과"
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

                self.test_manager.save_result(result, self.config.paths.results_dir)

                test_span.update(output={
                    "status": result.status,
                    "steps_passed": result.steps_passed,
                    "steps_executed": result.steps_executed,
                    "duration": duration,
                })

            langfuse.flush()
            return result

    def _execute_step(self, step, result: TestResult) -> tuple[bool, float]:
        """개별 스텝 실행 — (success, vision_confidence) 반환"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        screenshot_path = self.config.paths.screenshots_dir / f"screenshot_{self._file_tag}_{timestamp}.png"
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
                time.sleep(step.params.get("seconds", 2))
                return True, 1.0

            elif step.action == ActionType.BACK:
                return self._execute_back_step(step), 1.0

            elif step.action == ActionType.DISMISS_POPUPS:
                return self._dismiss_popups(step, screenshot_path), 1.0

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
                fresh_path = self._capture_runtime_screenshot(prefix="verify")
                success, confidence = self._verify_screen(fresh_path, step.target)
                return success, confidence

            elif step.action == ActionType.READ_TEXT:
                return self._read_text_step(screenshot_path, step, result), 1.0

            elif step.action in (ActionType.SKIP_TUTORIAL, ActionType.TUTORIAL_PASS):
                params = step.params or {}
                pkg = params.get("package") or self._current_package or step.target or ""
                return self.unity.skip_tutorial(package=pkg), 1.0

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

    def _dismiss_popups(self, step, initial_screenshot: Path) -> bool:
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
            rule, coords = self._find_dismiss_popup_rule(screenshot_path, rules)
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
                continue

            if self._dismiss_stop_visible(screenshot_path, stop_targets):
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

    def _find_dismiss_popup_rule(
        self,
        screenshot_path: Path,
        rules: list[dict],
    ) -> tuple[Optional[dict], Optional[dict]]:
        """현재 화면과 일치하는 첫 번째 팝업 규칙과 좌표를 반환한다."""
        for rule in rules:
            try:
                result = self.vision_lite.find_element(
                    screenshot_path, rule["target"], self.config.paths.debug_dir
                )
            except Exception as exc:
                logger.warning("dismiss_popups rule detection failed for '%s': %s", rule["target"], exc)
                continue
            if result.success and result.bbox:
                coords = result.bbox.to_pixels(self.adb.width, self.adb.height)
                return rule, coords
        return None, None

    def _dismiss_stop_visible(self, screenshot_path: Path, targets: list[str]) -> bool:
        for target in targets:
            visible, _ = self._verify_screen(screenshot_path, target)
            if not visible:
                return False
        return True

    def _verify_expected_targets(
        self,
        screenshot_path: Path,
        expect_visible: list[str],
        expect_hidden: list[str],
    ) -> bool:
        verification_confidence = 1.0
        for target in expect_visible:
            success, confidence = self._verify_screen(screenshot_path, target)
            verification_confidence = min(verification_confidence, confidence)
            if not success:
                self._last_expected_confidence = confidence
                logger.warning("Expected visible target '%s' was not found.", target)
                return False

        for target in expect_hidden:
            success, confidence = self._verify_screen(screenshot_path, target)
            if success:
                self._last_expected_confidence = confidence
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
        """공통 캐시 → 게임 캐시 → Vision 순으로 좌표 탐색. 해상도별 관리."""
        self._last_post_verify_screenshot = None
        target = step.target
        if not target:
            logger.error("find_and_tap action requires target")
            self._last_failure_reason = "target이 지정되지 않음"
            return False

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

        # 3. 캐시 미스 → Vision 탐지 → 캐시 저장
        # logger.info("Cache MISS for '%s' @ %s → Vision fallback.", target, self._resolution)
        logger.info(f"vision target:{target}")
        # 전환 시작 여유만 두고, 실제 대기는 stable check가 담당 (기존 3초 고정 대기 축소)
        time.sleep(1)
        latest_path = self._wait_for_screen_stable()
        coords = self._resolve_with_vision(latest_path, target)

        params = step.params or {}

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
        self._cache_element(latest_path, target, coords["x"], coords["y"], "vision")
        self._auto_register_common(target, coords["x"], coords["y"])
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
            self._last_failure_reason = f"탭 성공(Vision), 화면 검증 실패: '{target}'"
        else:
            exp = params.get("expect_visible")
            self._last_pass_detail = (
                f"'{target}' 탭 ({coords['x']},{coords['y']})"
                + (f" → '{exp}' 노출 확인" if exp else "")
            )
        self._save_tap_debug(
            latest_path, target, coords,
            getattr(self, "_last_vision_confidence", 0.0), verified,
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
        self._current_screen_type = ""
        verified = self._verify_find_and_tap_outcome(step, tap_source="XML")
        if not verified:
            self._last_failure_reason = f"탭 성공(XML), 화면 검증 실패: '{email}'"
        else:
            self._last_pass_detail = f"계정 '{email}' XML 정확 일치 탭 ({coords['x']},{coords['y']})"
        self._save_tap_debug(latest_path, email, coords, 1.0, verified)
        return True if verified else self._TAP_OK_VERIFY_FAIL

    # scroll_search 기본값
    SCROLL_SEARCH_MAX_DEFAULT = 5      # 스크롤 상한 (무한 루프 방지 1)
    SCROLL_END_THRESHOLD = 0.005       # 스크롤 전후 변화율이 이보다 작으면 리스트 끝 (무한 루프 방지 2)

    def _scroll_one_page(self, direction: str) -> None:
        """화면 중앙 세로선 기준 한 페이지 스크롤 (up=이전 내용으로, down=다음 내용으로)."""
        w, h = self.adb.width, self.adb.height
        x = w // 2
        if direction == "up":
            self.adb.swipe(x, int(h * 0.35), x, int(h * 0.70), duration=400)
        else:
            self.adb.swipe(x, int(h * 0.70), x, int(h * 0.35), duration=400)

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

        if direction in ("top", "bottom"):
            one_dir = "up" if direction == "top" else "down"
            prev = self._wait_for_screen_stable()
            for i in range(10):
                if getattr(self, '_stop_event', None) and self._stop_event.is_set():
                    break
                self._scroll_one_page(one_dir)
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
            self._scroll_one_page(direction)
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

        for i in range(max_scrolls):
            if getattr(self, '_stop_event', None) and self._stop_event.is_set():
                logger.warning("scroll_search: 사용자 중단")
                break
            before_path = latest_path
            self._scroll_one_page(direction)
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

    def _cache_element(self, screenshot_path: Path, target: str, x: int, y: int, source: str) -> None:
        if not self._current_package:
            return
        screen_type = self._get_screen_type(screenshot_path)
        self.cache.set(self._current_package, screen_type, target, x, y, source,
                       resolution=self._resolution)

    def _resolve_with_vision(self, screenshot_path: Path, target: str) -> Optional[dict]:
        vision_result = self.vision.find_element(
            screenshot_path, target, self.config.paths.debug_dir
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

        if not expect_visible and not expect_hidden:
            self._last_post_verify_confidence = 1.0
            logger.info(
                "%s tap for target '%s' — no expect_visible/hidden, skipping verification.",
                tap_source, target,
            )
            return True

        if tap_source != "Retry":
            wait_seconds = self._to_float(params.get("wait_seconds"), 0.0)
            if wait_seconds > 0:
                logger.info("Post-tap verification wait: %.1fs", wait_seconds)
                time.sleep(wait_seconds)

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
        timeout = timeout or self.STABILITY_TIMEOUT_SEC
        interval = self.STABILITY_POLL_INTERVAL_SEC
        threshold = self.STABILITY_THRESHOLD

        time.sleep(min_wait)
        prev_path = self._capture_runtime_screenshot(prefix="stable_check")
        deadline = time.time() + timeout

        while time.time() < deadline:
            if getattr(self, '_stop_event', None) and self._stop_event.is_set():
                logger.warning("Screen stable wait interrupted by stop event.")
                return prev_path
            time.sleep(interval)
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

    def _verify_screen(self, screenshot_path: Path, target: str) -> tuple[bool, float]:
        if not target:
            logger.error("verify action requires target")
            self._last_failure_reason = "verify target이 지정되지 않음"
            return False, 0.0
        vision_result = self.vision_lite.find_element(
            screenshot_path, target, self.config.paths.debug_dir
        )
        if not vision_result.success or not vision_result.bbox:
            self._last_failure_reason = (
                f"화면에서 '{target}'을 찾지 못함 (신뢰도: {vision_result.confidence:.2f})"
            )
            return False, vision_result.confidence
        return True, vision_result.confidence

    def _read_text_step(self, screenshot_path: Path, step, result: TestResult) -> bool:
        target = step.target
        if not target:
            logger.error("read_text action requires target")
            return False

        params = step.params or {}
        save_as = params.get("save_as")
        compare_with = params.get("compare_with")
        expect_changed = params.get("expect_changed")
        expect_increase = params.get("expect_increase")
        expect_decrease = params.get("expect_decrease")

        value = self.vision.read_text(screenshot_path, target)
        if value is None:
            self._last_failure_reason = f"read_text: '{target}' 텍스트를 찾지 못함"
            logger.error(self._last_failure_reason)
            self._save_read_debug(screenshot_path, target, self._last_failure_reason, False)
            return False

        logger.info("read_text: '%s' = %s", target, value)

        if not hasattr(result, "context"):
            result.context = {}

        detail = f"'{target}' = '{value}'"
        if save_as:
            result.context[save_as] = value
            detail += f" — '{save_as}'로 저장"

        ok = True
        if compare_with:
            prev = result.context.get(compare_with)
            if prev is None:
                self._last_failure_reason = f"read_text: compare_with '{compare_with}' 값이 없습니다."
                ok = False
            # 숫자 증감 검증 (재화 지급/차감 확인용) — 단순 변경 여부보다 강한 검증
            elif expect_increase or expect_decrease:
                prev_n, curr_n = self._to_number(prev), self._to_number(value)
                if prev_n is None or curr_n is None:
                    self._last_failure_reason = f"read_text: 숫자 비교 불가 ({prev!r} → {value!r})"
                    ok = False
                elif expect_increase and curr_n <= prev_n:
                    self._last_failure_reason = f"read_text: 증가 기대했으나 {prev_n:g} → {curr_n:g}"
                    ok = False
                elif expect_decrease and curr_n >= prev_n:
                    self._last_failure_reason = f"read_text: 감소 기대했으나 {prev_n:g} → {curr_n:g}"
                    ok = False
                else:
                    delta = curr_n - prev_n
                    detail = (f"'{target}' {prev_n:g} → {curr_n:g} ({delta:+g}) — "
                              + ("증가 확인" if expect_increase else "감소 확인"))
            else:
                changed = prev != value
                if expect_changed is True and not changed:
                    self._last_failure_reason = f"read_text: 변경 기대했으나 동일함 ({value})"
                    ok = False
                elif expect_changed is False and changed:
                    self._last_failure_reason = f"read_text: 유지 기대했으나 변경됨 ({prev} → {value})"
                    ok = False
                else:
                    detail = f"'{target}' {prev} → {value} ({'변경됨' if changed else '유지됨'})"

        if ok:
            self._last_pass_detail = detail
            logger.info("read_text 통과: %s", detail)
        else:
            logger.error(self._last_failure_reason)
        # 증거 스크린샷 — 읽은 값/비교 결과를 라벨로 새겨 리포트 갤러리에 노출
        self._save_read_debug(
            screenshot_path, target, detail if ok else self._last_failure_reason, ok)
        return ok

    def _save_read_debug(self, screenshot_path: Path, target: str,
                         detail: str, passed: bool) -> None:
        """read_text 증거 이미지 저장 — 읽은 값과 판정 근거를 이미지에 새겨
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
            draw.text((5, 5), f"read_text {status}: {detail}", fill=color)
            img.save(img_path)

            record = {
                "timestamp": ts,
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
            logger.info("┌─ read_text 증거 저장: %s", img_path.name)
        except Exception as e:
            logger.warning("read_text 증거 저장 실패: %s", e)

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
