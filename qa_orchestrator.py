import threading
from pathlib import Path
from datetime import datetime
from typing import Optional
import time
import logging

from PIL import Image, ImageChops
from langsmith import traceable

from config import Config
from adb_controller import ADBController
from vision_agent import GeminiVisionAgent
from test_manager import TestCaseManager, TestResult, ActionType
from planner_node import PlannerNode  # 추가
from unity_api_client import UnityAPIClient
from element_cache import ElementCache


logger = logging.getLogger(__name__)

class QAOrchestrator:
    """QA 자동화 오케스트레이터"""

    # 고정 좌표 매핑 — Vision 호출 없이 바로 탭 (키: target 부분 매칭)
    FIXED_TAP_TARGETS: dict[str, tuple[int, int]] = {
        "동의합니다": (360, 823),
        "개인정보처리방침": (360, 1015),
        "이용약관": (360, 955),
    }

    POST_TAP_DELAY_SEC = 0.7
    POST_TAP_VERIFY_TIMEOUT_SEC = 2.0
    POST_TAP_POLL_INTERVAL_SEC = 0.3
    STABILITY_POLL_INTERVAL_SEC = 0.5
    STABILITY_TIMEOUT_SEC = 10.0
    STABILITY_THRESHOLD = 0.01
    
    def __init__(self, config: Config = Config()):
        self.config = config
        self.adb = ADBController()
        #self.adb = None
        self.unity = UnityAPIClient(
            adb_controller=self.adb,
            project=config.gemini.project,
            location=config.gemini.location,
            model=config.gemini.model,
            temperature=config.gemini.temperature,
            base_url="http://host.docker.internal:37772"
        )
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
        self._current_package: str = ""
        self._current_screen_type: str = ""
    def run_natural_language_test(
        self,
        scenario: str,
        package_name: str = "",
        save_yaml: bool = True
    ) -> TestResult:
        """
        자연어 시나리오를 받아서 자동으로 테스트 실행
        
        Args:
            scenario: 자연어 테스트 시나리오
            package_name: 앱 패키지명
            save_yaml: YAML 파일로 저장 여부
        
        Returns:
            TestResult: 테스트 실행 결과
        """
        logger.info("="*60)
        logger.info("Natural Language Test Execution Started")
        logger.info("="*60)
        logger.info(f"Scenario: {scenario}")
        
        # 1. Planner로 테스트 플랜 생성
        logger.info("\n[Step 1] Generating test plan from natural language...")
        test_plan, yaml_path = self.planner.generate_and_save(
            scenario=scenario,
            output_dir=self.config.paths.testcases_dir,
            package_name=package_name
        )
        
        if save_yaml:
            logger.info(f"Test plan saved to: {yaml_path}")
        
        # 2. 생성된 테스트케이스 로드
        logger.info("\n[Step 2] Loading generated test case...")
        self.test_manager._load_testcases()  # 새로 생성된 YAML 재로드
        test_id = yaml_path.stem
        
        # 3. 테스트 실행
        logger.info(f"\n[Step 3] Executing test: {test_id}")
        result = self.run_test(test_id)
        
        return result
    

    @traceable(
        run_type="chain",
        name="qa_test_run",
        process_inputs=lambda inputs: {
            "test_id": inputs.get("test_id"),
            "package": getattr(inputs.get("testcase_override"), "package", ""),
            "title": getattr(inputs.get("testcase_override"), "title", ""),
            "steps_count": len(getattr(inputs.get("testcase_override"), "steps", [])),
        },
    )
    def run_test(
        self,
        test_id: str,
        stop_event: threading.Event | None = None,
        testcase_override: "TestCase | None" = None,
    ) -> TestResult:
        """단일 테스트 실행. testcase_override가 주어지면 파일 대신 해당 객체를 사용."""
        if testcase_override is not None:
            testcase = testcase_override
        else:
            testcase = self.test_manager.get_testcase(test_id)
        if not testcase:
            raise ValueError(f"Test case not found: {test_id}")
        
        total = len(testcase.steps)
        logger.info("━" * 52)
        logger.info(f"  테스트 시작: {testcase.title}")
        logger.info(f"  패키지: {testcase.package}  |  스텝 수: {total}")
        logger.info("━" * 52)

        self._current_package = testcase.package or ""
        self._current_screen_type = ""

        result = TestResult(
            test_id=testcase.id,
            title=testcase.title,
            status="RUNNING",
            start_time=datetime.now()
        )

        try:
            # 앱 실행
        
            if testcase.package:
                logger.info(f"▶ 앱 실행 중: {testcase.package}")
                self.adb.launch_app(testcase.package)
                self._wait_for_screen_stable()

            # 각 스텝 실행
            for idx, step in enumerate(testcase.steps):
                # 중단 요청 확인
                if stop_event and stop_event.is_set():
                    logger.warning("⏹️ 사용자 중단 — 테스트를 중지합니다.")
                    result.status = "FAIL"
                    result.error_message = "사용자에 의해 중단됨"
                    break

                label = step.description or step.action
                target_info = f"  → 대상: {step.target}" if step.target else ""
                logger.info("")
                logger.info(f"┌─ [{idx + 1}/{total}] {label}")
                if target_info:
                    logger.info(f"│  {target_info.strip()}")

                step_result = self._execute_step(step, result)
                tap_ok_verify_fail = (step_result == self._TAP_OK_VERIFY_FAIL)
                success = bool(step_result) and not tap_ok_verify_fail
                result.steps_executed += 1

                if success:
                    result.steps_passed += 1
                    logger.info("└─ ✅ 완료")
                else:
                    if step.retry > 1 and not self._uses_internal_retry(step):
                        for retry_count in range(step.retry - 1):
                            if tap_ok_verify_fail:
                                # 탭은 성공했으므로 검증만 재시도
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
                                # 탭 자체가 실패했으므로 스텝 전체 재시도
                                logger.warning(
                                    f"│  ↩ 재시도 {retry_count + 1}/{step.retry - 1} ..."
                                )
                                time.sleep(2)
                                retry_result = self._execute_step(step, result)
                                if retry_result and retry_result != self._TAP_OK_VERIFY_FAIL:
                                    result.steps_passed += 1
                                    success = True
                                    logger.info("└─ ✅ 완료 (재시도 성공)")
                                    break
                                elif retry_result == self._TAP_OK_VERIFY_FAIL:
                                    # 재시도에서 탭은 성공 → 이후 검증만 재시도로 전환
                                    tap_ok_verify_fail = True

                    if not success:
                        logger.error("└─ ❌ 실패")
                        result.status = "FAIL"
                        result.error_message = f"Step {idx + 1} 실패: {label}"

                result.step_results.append({
                    "step": idx + 1,
                    "label": label,
                    "passed": success,
                })

                # 스텝 완료 후 임시 스크린샷 정리
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
            self.test_manager.save_result(result, self.config.paths.results_dir)

        return result
    
    def _execute_step(self, step, result: TestResult) -> bool:
        """개별 스텝 실행"""
        # 스크린샷 캡처
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        screenshot_path = self.config.paths.screenshots_dir / f"screenshot_{timestamp}.png"
        self.adb.screenshot(screenshot_path)
        result.screenshots.append(str(screenshot_path))
        
        try:
            if step.action == ActionType.FIND_AND_TAP:
                result_val = self._find_and_tap(step)
                if result_val == self._TAP_OK_VERIFY_FAIL:
                    return self._TAP_OK_VERIFY_FAIL
                return bool(result_val)
            
            elif step.action == ActionType.SWIPE:
                params = step.params
                return self.adb.swipe(
                    params.get("x1", 0), params.get("y1", 0),
                    params.get("x2", 0), params.get("y2", 0)
                )
            
            elif step.action == ActionType.WAIT:
                time.sleep(step.params.get("seconds", 2))
                return True
            
            elif step.action == ActionType.BACK:
                return self._execute_back_step(step)
            
            elif step.action == ActionType.HOME:
                return self.adb.press_home()
            
            elif step.action == ActionType.LAUNCH_APP:
                package = step.params.get("package")
                if package:
                    self._current_screen_type = ""
                    launched = self.adb.launch_app(package)
                    if launched:
                        self._wait_for_screen_stable()
                    return launched
                return False

            elif step.action == ActionType.CLOSE_APP:
                package = step.params.get("package")
                if package:
                    self._current_screen_type = ""
                    return self.adb.close_app(package)
                return False
            
            elif step.action == ActionType.VERIFY:
                fresh_path = self._capture_runtime_screenshot(prefix="verify")
                return self._verify_screen(fresh_path, step.target)

            elif step.action == ActionType.READ_TEXT:
                return self._read_text_step(screenshot_path, step, result)

            elif step.action == ActionType.SKIP_TUTORIAL:
                return self.unity.skip_tutorial(package=self._current_package)

            else:
                logger.warning(f"Unknown action type: {step.action}")
                return False
                
        except Exception as e:
            logger.error(f"Step execution failed: {e}")
            return False    

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
            if not self._verify_screen(screenshot_path, target):
                logger.warning(
                    "Expected visible target '%s' was not found.",
                    target,
                )
                return False

        for target in expect_hidden:
            if self._verify_screen(screenshot_path, target):
                logger.warning(
                    "Expected hidden target '%s' is still visible.",
                    target,
                )
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
        """고정 좌표 우선 → Vision으로 요소를 찾아서 클릭.
        Returns: True(성공), False(탭 실패), _TAP_OK_VERIFY_FAIL(탭 성공+검증 실패)
        """
        target = step.target
        if not target:
            logger.error("find_and_tap action requires target")
            return False

        # 고정 좌표 매칭
        fixed = self._match_fixed_target(target)
        if fixed:
            logger.info("Fixed coordinate HIT for '%s' → (%d, %d).", target, fixed[0], fixed[1])
            self._wait_for_screen_stable()
            self.adb.tap(fixed[0], fixed[1])
            self._current_screen_type = ""
            verified = self._verify_find_and_tap_outcome(step, tap_source="Fixed")
            return True if verified else self._TAP_OK_VERIFY_FAIL

        logger.info("Vision for target '%s'.", target)
        time.sleep(3)
        latest_path = self._wait_for_screen_stable()
        coords = self._resolve_with_vision(latest_path, target)
        if not coords:
            return False
        self.adb.tap(coords["x"], coords["y"])
        self._cache_element(latest_path, target, coords["x"], coords["y"], "vision")
        self._current_screen_type = ""
        verified = self._verify_find_and_tap_outcome(step, tap_source="Vision")
        return True if verified else self._TAP_OK_VERIFY_FAIL

    def _match_fixed_target(self, target: str) -> tuple[int, int] | None:
        """FIXED_TAP_TARGETS에서 target과 부분 매칭되는 고정 좌표 반환."""
        normalized = target.strip().replace(" ", "")
        for key, coords in self.FIXED_TAP_TARGETS.items():
            if key in normalized:
                return coords
        return None

    def _cache_element(self, screenshot_path: Path, target: str, x: int, y: int, source: str) -> None:
        """현재 화면 타입 기준으로 요소 좌표를 캐시에 저장."""
        if not self._current_package:
            return
        screen_type = self._get_screen_type(screenshot_path)
        self.cache.set(self._current_package, screen_type, target, x, y, source)

    def _resolve_with_vision(self, screenshot_path: Path, target: str) -> Optional[dict]:
        """Vision으로 좌표 반환. 실패 시 None."""
        vision_result = self.vision.find_element(
            screenshot_path, target, self.config.paths.debug_dir
        )
        if not vision_result.success or not vision_result.bbox:
            logger.error("Element not found by Vision: %s", target)
            return None
        return vision_result.bbox.to_pixels(self.adb.width, self.adb.height)

    def _verify_find_and_tap_outcome(self, step, tap_source: str) -> bool:
        params = step.params or {}
        target = step.target or ""
        expect_visible = self._to_target_list(params.get("expect_visible"))
        expect_hidden = self._to_target_list(params.get("expect_hidden"))

        if not expect_visible and not expect_hidden:
            logger.info("%s tap for target '%s' — no expect_visible/hidden, skipping verification.", tap_source, target)
            return True

        # 안정화 → 검증 → 실패 시 재대기, step.timeout 내 반복
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
            # 다음 화면 변화를 기다리기 위해 짧게 대기
            time.sleep(min(self.POST_TAP_POLL_INTERVAL_SEC, remaining))

        logger.warning(
            "%s tap post-verification timed out for target '%s' after %ds.",
            tap_source, target, step.timeout,
        )
        return False

    def _wait_for_screen_stable(self, timeout: float | None = None, min_wait: float = 0.5) -> Path:
        """화면이 안정화될 때까지 폴링. 안정화된 스크린샷 경로 반환."""
        timeout = timeout or self.STABILITY_TIMEOUT_SEC
        interval = self.STABILITY_POLL_INTERVAL_SEC
        threshold = self.STABILITY_THRESHOLD

        time.sleep(min_wait)
        prev_path = self._capture_runtime_screenshot(prefix="stable_check")
        deadline = time.time() + timeout

        while time.time() < deadline:
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

        logger.warning("Screen did not stabilize within %.1fs — proceeding with last capture.", timeout)
        return prev_path

    def _cleanup_step_files(self) -> None:
        """스텝 완료 후 screenshots_dir, debug_dir 의 png 파일 일괄 삭제."""
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

    def _verify_screen(self, screenshot_path: Path, target: str) -> bool:
        if not target:
            logger.error("verify action requires target")
            return False
        vision_result = self.vision_lite.find_element(
            screenshot_path, target, self.config.paths.debug_dir
        )
        if not vision_result.success or not vision_result.bbox:
            return False
        return True

    def _read_text_step(self, screenshot_path: Path, step, result: TestResult) -> bool:
        """화면에서 텍스트를 읽어 result.context에 저장하고, 이전 값과 비교 검증"""
        target = step.target
        if not target:
            logger.error("read_text action requires target")
            return False

        params = step.params or {}
        save_as = params.get("save_as")
        compare_with = params.get("compare_with")
        expect_changed = params.get("expect_changed")  # True/False

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


