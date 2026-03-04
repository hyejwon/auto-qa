import threading
from pathlib import Path
from datetime import datetime
import time
import logging

from PIL import Image, ImageChops

from config import Config
from adb_controller import ADBController
from vision_agent import GeminiVisionAgent
from test_manager import TestCaseManager, TestResult, ActionType
from planner_node import PlannerNode  # 추가
from unity_api_client import UnityAPIClient


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class QAOrchestrator:
    """QA 자동화 오케스트레이터"""

    UNITY_MIN_SCORE = 0.55
    SCREEN_CHANGE_THRESHOLD = 0.01
    POST_TAP_DELAY_SEC = 0.7
    VISION_ONLY_TARGETS = (
        "이용약관",
        "terms of use",
        "termsofuse",
        "button_termsofuse",
        "개인정보처리방침",
        "개인정보 처리방침",
        "privacy policy",
        "privacy-policy",
    )
    
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
        self.test_manager = TestCaseManager(config.paths.testcases_dir)
        # Planner Node 추가
        self.planner = PlannerNode(
            project=config.gemini.project,
            location=config.gemini.location,
            model=config.gemini.model
        )
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
    

    def run_test(
        self,
        test_id: str,
        stop_event: threading.Event | None = None,
    ) -> TestResult:
        """단일 테스트 실행"""
        testcase = self.test_manager.get_testcase(test_id)
        if not testcase:
            raise ValueError(f"Test case not found: {test_id}")
        
        total = len(testcase.steps)
        logger.info("━" * 52)
        logger.info(f"  테스트 시작: {testcase.title}")
        logger.info(f"  패키지: {testcase.package}  |  스텝 수: {total}")
        logger.info("━" * 52)

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
                time.sleep(3)

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

                success = self._execute_step(step, result)
                result.steps_executed += 1

                if success:
                    result.steps_passed += 1
                    logger.info("└─ ✅ 완료")
                else:
                    if step.retry > 1 and not self._uses_internal_retry(step):
                        for retry_count in range(step.retry - 1):
                            logger.warning(
                                f"│  ↩ 재시도 {retry_count + 1}/{step.retry - 1} ..."
                            )
                            time.sleep(2)
                            if self._execute_step(step, result):
                                result.steps_passed += 1
                                success = True
                                logger.info("└─ ✅ 완료 (재시도 성공)")
                                break

                    if not success:
                        logger.error("└─ ❌ 실패")
                        result.status = "FAIL"
                        result.error_message = f"Step {idx + 1} 실패: {label}"

                result.step_results.append({
                    "step": idx + 1,
                    "label": label,
                    "passed": success,
                })

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
                return self._find_and_tap(screenshot_path, step.target)
            
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
                    return self.adb.launch_app(package)
                return False
            
            elif step.action == ActionType.CLOSE_APP:
                package = step.params.get("package")
                if package:
                    return self.adb.close_app(package)
                return False
            
            # elif step.action == ActionType.VERIFY_UI_DUMP:
            #     # UI Dump 검증
            #     expected_text = step.params.get("expected_text", "")
            #     match_type = step.params.get("match_type", "contains")
                
            #     success = self.adb.verify_ui_text(expected_text, match_type)
                
            #     # UI Dump도 파일로 저장 (디버깅용)
            #     if not success:
            #         ui_dump = self.adb.get_ui_dump()
            #         dump_file = self.config.paths.debug_dir / f"ui_dump_{timestamp}.xml"
            #         dump_file.write_text(ui_dump, encoding='utf-8')
            #         logger.info(f"UI dump saved for debugging: {dump_file}")
                
            #     return success
            
            elif step.action == ActionType.VERIFY:
                return self._verify_screen(screenshot_path, step.target)
            
            else:
                logger.warning(f"Unknown action type: {step.action}")
                return False
                
        except Exception as e:
            logger.error(f"Step execution failed: {e}")
            return False    

    def _execute_back_step(self, step) -> bool:
        """뒤로가기 + 선택적 화면 검증을 원자적으로 수행"""
        params = step.params or {}
        wait_seconds = float(params.get("wait_seconds", self.POST_TAP_DELAY_SEC))
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
            if self._verify_back_outcome(screenshot_path, expect_visible, expect_hidden):
                if attempt == 1:
                    logger.info("Back verification succeeded after one additional back press.")
                return True

            if attempt == 0:
                logger.warning(
                    "Back verification failed. Pressing back one more time before failing."
                )

        return False

    def _verify_back_outcome(
        self,
        screenshot_path: Path,
        expect_visible: list[str],
        expect_hidden: list[str],
    ) -> bool:
        for target in expect_visible:
            if not self._verify_screen(screenshot_path, target):
                logger.warning(
                    "Back verification failed: expected visible target '%s' was not found.",
                    target,
                )
                return False

        for target in expect_hidden:
            if self._verify_screen(screenshot_path, target):
                logger.warning(
                    "Back verification failed: expected hidden target '%s' is still visible.",
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
        if step.action != ActionType.BACK:
            return False
        params = step.params or {}
        return bool(params.get("expect_visible") or params.get("expect_hidden") or step.target)

    def _find_and_tap(self, screenshot_path: Path, target: str) -> bool:
        """요소 찾아서 클릭 (Unity API 우선, Vision 폴백)"""
        if not target:
            logger.error("find_and_tap action requires target")
            return False

        if self._should_force_vision(target):
            logger.info("Target '%s' is configured as Vision-only. Skipping Unity.", target)
            latest_path = self._capture_runtime_screenshot(prefix="vision_only")
            return self._find_and_tap_with_vision(latest_path, target)

        if self._find_and_tap_with_unity(target):
            if self._verify_screen_changed(screenshot_path):
                return True
            logger.warning(
                "Unity click post-verification failed for target '%s'. Falling back to Vision.",
                target,
            )
        else:
            logger.info("Unity match not found for target '%s'. Falling back to Vision.", target)

        latest_path = self._capture_runtime_screenshot(prefix="vision_fallback")
        return self._find_and_tap_with_vision(latest_path, target)

    def _find_and_tap_with_unity(self, target: str) -> bool:
        match = self.unity.find_best_button(target, min_score=self.UNITY_MIN_SCORE)
        if not match:
            return False

        coords = self.unity.unity_to_screen_coords(
            button=match.button,
            screen_width=self.adb.width,
            screen_height=self.adb.height,
        )
        if not coords:
            logger.warning("Unity matched target '%s' but coordinates were invalid.", target)
            return False

        button_name = (
            match.button.get("SpecifiedName")
            or match.button.get("GameObjectName")
            or match.button.get("Name")
            or target
        )
        logger.info(
            "Unity matched '%s' (score=%.2f) -> '%s' at (%d, %d)",
            target,
            match.score,
            button_name,
            coords["x"],
            coords["y"],
        )
        return self.adb.tap(coords["x"], coords["y"])

    def _find_and_tap_with_vision(self, screenshot_path: Path, target: str) -> bool:
        vision_result = self.vision.find_element(
            screenshot_path,
            target,
            self.config.paths.debug_dir,
        )
        if not vision_result.success or not vision_result.bbox:
            logger.error("Element not found by Vision: %s", target)
            return False

        pixel_coords = vision_result.bbox.to_pixels(self.adb.width, self.adb.height)
        return self.adb.tap(pixel_coords["x"], pixel_coords["y"])

    def _capture_runtime_screenshot(self, prefix: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = self.config.paths.debug_dir / f"{prefix}_{timestamp}.png"
        self.adb.screenshot(path)
        return path

    def _verify_screen_changed(self, before_path: Path) -> bool:
        time.sleep(self.POST_TAP_DELAY_SEC)
        after_path = self._capture_runtime_screenshot(prefix="post_tap")

        try:
            with Image.open(before_path) as before_source, Image.open(after_path) as after_source:
                before_img = before_source.convert("RGB")
                after_img = after_source.convert("RGB")

                if before_img.size != after_img.size:
                    after_img = after_img.resize(before_img.size)

                diff = ImageChops.difference(before_img, after_img)
                histogram = diff.histogram()
                if not histogram:
                    return False

                weighted_sum = sum((index % 256) * count for index, count in enumerate(histogram))
                max_sum = 255 * before_img.width * before_img.height * 3
                change_ratio = (weighted_sum / max_sum) if max_sum else 0.0

                logger.info("Post-click screen change ratio: %.4f", change_ratio)
                return change_ratio >= self.SCREEN_CHANGE_THRESHOLD
        except Exception as exc:
            logger.warning("Screen-change verification failed: %s", exc)
            return False

    def _verify_screen(self, screenshot_path: Path, target: str) -> bool:
        if not target:
            logger.error("verify action requires target")
            return False

        if self._should_force_vision(target):
            logger.info("Verify target '%s' is configured as Vision-only. Skipping Unity.", target)
            vision_result = self.vision.find_element(
                screenshot_path,
                target,
                self.config.paths.debug_dir,
            )
            return vision_result.success

        if self.unity.verify_element(target, min_score=self.UNITY_MIN_SCORE):
            logger.info("Verified by Unity API: %s", target)
            return True

        logger.info("Unity verification failed for '%s'. Falling back to Vision.", target)
        vision_result = self.vision.find_element(
            screenshot_path,
            target,
            self.config.paths.debug_dir,
        )
        return vision_result.success

    @classmethod
    def _should_force_vision(cls, target: str) -> bool:
        normalized = target.strip().lower()
        compact = normalized.replace(" ", "")
        for keyword in cls.VISION_ONLY_TARGETS:
            lowered = keyword.lower()
            if lowered in normalized or lowered.replace(" ", "") in compact:
                return True
        return False
