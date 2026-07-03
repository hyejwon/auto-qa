import os
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
from adb_controller import ADBController
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

    def __init__(self, config: Config = Config()):
        self.config = config
        self.adb = ADBController()
        self.unity = UnityAPIClient(
            adb_controller=self.adb,
            project=config.gemini.project,
            location=config.gemini.location,
            model=config.gemini.model,
            temperature=config.gemini.temperature,
            base_url=os.getenv("UNITY_API_URL", "http://127.0.0.1:37772")
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
        self._current_package: str = ""
        self._current_screen_type: str = ""
        self._last_failure_reason: str = ""
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
                    label = step.description or step.action
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
                                            result.steps_passed += 1
                                            success = True
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
                        "passed": success,
                        "vision_confidence": confidence,
                        "failure_reason": "" if success else self._last_failure_reason,
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
        screenshot_path = self.config.paths.screenshots_dir / f"screenshot_{timestamp}.png"
        self.adb.screenshot(screenshot_path)
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

            elif step.action == ActionType.WAIT:
                time.sleep(step.params.get("seconds", 2))
                return True, 1.0

            elif step.action == ActionType.BACK:
                return self._execute_back_step(step), 1.0

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

    def _verify_expected_targets(
        self,
        screenshot_path: Path,
        expect_visible: list[str],
        expect_hidden: list[str],
    ) -> bool:
        for target in expect_visible:
            success, _ = self._verify_screen(screenshot_path, target)
            if not success:
                logger.warning("Expected visible target '%s' was not found.", target)
                return False

        for target in expect_hidden:
            success, _ = self._verify_screen(screenshot_path, target)
            if success:
                logger.warning("Expected hidden target '%s' is still visible.", target)
                return False

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

    def _find_and_tap(self, step) -> bool | str:
        """공통 캐시 → 게임 캐시 → Vision 순으로 좌표 탐색. 해상도별 관리."""
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

        # 3. 캐시 미스 → Vision 탐지 → 캐시 저장
        # logger.info("Cache MISS for '%s' @ %s → Vision fallback.", target, self._resolution)
        logger.info(f"vision target:{target}")
        time.sleep(3)
        latest_path = self._wait_for_screen_stable()
        coords = self._resolve_with_vision(latest_path, target)
        if not coords:
            if not self._last_failure_reason:
                self._last_failure_reason = f"Vision으로 요소를 찾지 못함: '{target}'"
            return False
        self.adb.tap(coords["x"], coords["y"])
        self._cache_element(latest_path, target, coords["x"], coords["y"], "vision")
        self._auto_register_common(target, coords["x"], coords["y"])
        self._current_screen_type = ""
        verified = self._verify_find_and_tap_outcome(step, tap_source="Vision")
        if not verified:
            self._last_failure_reason = f"탭 성공(Vision), 화면 검증 실패: '{target}'"
        self._save_tap_debug(
            latest_path, target, coords,
            getattr(self, "_last_vision_confidence", 0.0), verified,
        )
        return True if verified else self._TAP_OK_VERIFY_FAIL

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

    def _save_tap_debug(self, screenshot_path: Path, target: str, coords: dict,
                        confidence: float, verified: bool) -> None:
        """find_and_tap 디버그 아티팩트 저장 — 버튼을 제대로 눌렀는지 추적용.

        - screenshots_debug/taps/{ts}_{target}_{PASS|FAIL}.png : bbox + 실제 탭 지점 표시
        - screenshots_debug/find_and_tap_debug.jsonl           : 스텝별 한 줄 요약 로그
        """
        try:
            debug_dir = self.config.paths.debug_dir
            taps_dir = debug_dir / "taps"
            taps_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            status = "PASS" if verified else "FAIL"
            safe_target = "".join(
                c if c.isalnum() or c in "._- " else "_" for c in (target or "")
            ).strip().replace(" ", "_")[:40] or "none"
            img_path = taps_dir / f"{ts}_{safe_target}_{status}.png"

            # 주석 이미지: bbox 사각형 + 실제 탭 지점 크로스헤어 + 라벨
            img = Image.open(screenshot_path).convert("RGB")
            draw = ImageDraw.Draw(img)
            color = (0, 200, 0) if verified else (255, 40, 40)
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
            img.save(img_path)

            record = {
                "timestamp": ts,
                "target": target,
                "tap": {"x": cx, "y": cy},
                "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                "confidence": round(float(confidence), 3),
                "resolution": self._resolution,
                "package": self._current_package,
                "verified": verified,
                "failure_reason": "" if verified else self._last_failure_reason,
                "debug_image": str(img_path),
                "source_screenshot": str(screenshot_path),
            }
            with open(debug_dir / "find_and_tap_debug.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

            logger.info("┌─ find_and_tap 디버그 저장: %s", img_path.name)
            logger.info("│  target='%s' tap=(%d,%d) conf=%.2f → %s",
                        target, cx, cy, confidence, status)
        except Exception as e:
            logger.warning("find_and_tap 디버그 저장 실패: %s", e)

    def _verify_find_and_tap_outcome(self, step, tap_source: str) -> bool:
        params = step.params or {}
        target = step.target or ""
        expect_visible = self._to_target_list(params.get("expect_visible"))
        expect_hidden = self._to_target_list(params.get("expect_hidden"))

        if not expect_visible and not expect_hidden:
            logger.info(
                "%s tap for target '%s' — no expect_visible/hidden, skipping verification.",
                tap_source, target,
            )
            return True

        deadline = time.time() + step.timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            stable_screenshot = self._wait_for_screen_stable(
                timeout=min(self.STABILITY_TIMEOUT_SEC, deadline - time.time())
            )
            if self._verify_expected_targets(stable_screenshot, expect_visible, expect_hidden):
                logger.info("%s tap verified for '%s' (attempt %d).", tap_source, target, attempt)
                return True

            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(self.POST_TAP_POLL_INTERVAL_SEC, remaining))

        logger.warning(
            "%s tap post-verification timed out for target '%s' after %ds.",
            tap_source, target, step.timeout,
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

            try:
                with Image.open(prev_path) as prev_img, Image.open(curr_path) as curr_img:
                    p = prev_img.convert("RGB")
                    c = curr_img.convert("RGB")
                    if p.size != c.size:
                        c = c.resize(p.size)
                    diff = ImageChops.difference(p, c)
                    hist = diff.histogram()
                    weighted = sum((i % 256) * cnt for i, cnt in enumerate(hist))
                    max_val = 255 * p.width * p.height * 3
                    ratio = (weighted / max_val) if max_val else 0.0
            except Exception:
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

    def _cleanup_step_files(self) -> None:
        count = 0
        for d in (self.config.paths.screenshots_dir, self.config.paths.debug_dir):
            for f in d.glob("*.png"):
                try:
                    f.unlink()
                    count += 1
                except Exception:
                    pass
        if count:
            logger.info(f"🧹 스텝 완료 — 임시 파일 {count}개 삭제")

    def _capture_runtime_screenshot(self, prefix: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = self.config.paths.debug_dir / f"{prefix}_{timestamp}.png"
        self.adb.screenshot(path)
        return path

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

        value = self.vision.read_text(screenshot_path, target)
        if value is None:
            logger.error("read_text: '%s' 텍스트를 찾지 못했습니다.", target)
            return False

        logger.info("read_text: '%s' = %s", target, value)

        if not hasattr(result, "context"):
            result.context = {}

        if save_as:
            result.context[save_as] = value

        if compare_with:
            prev = result.context.get(compare_with)
            if prev is None:
                logger.warning("read_text: compare_with '%s' 값이 없습니다.", compare_with)
                return False
            changed = prev != value
            if expect_changed is True and not changed:
                logger.error("read_text: PID 변경 기대했으나 동일함 (%s)", value)
                return False
            if expect_changed is False and changed:
                logger.error("read_text: PID 유지 기대했으나 변경됨 (%s → %s)", prev, value)
                return False
            logger.info(
                "read_text 비교: %s → %s (%s)",
                prev,
                value,
                "변경됨" if changed else "유지됨",
            )

        return True
