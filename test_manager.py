from typing import List, Dict, Optional
from pathlib import Path
from datetime import datetime
import yaml
import json
from pydantic import BaseModel
from enum import Enum
import logging

logger = logging.getLogger(__name__)

class ActionType(str, Enum):
    """액션 타입"""
    FIND_AND_TAP = "find_and_tap"
    SWIPE = "swipe"
    SCROLL = "scroll"
    WAIT = "wait"
    VERIFY = "verify"
    BACK = "back"
    DISMISS_POPUPS = "dismiss_popups"
    HOME = "home"
    LAUNCH_APP = "launch_app"
    CLOSE_APP = "close_app"
    READ_TEXT = "read_text"
    READ_ITEMS = "read_items"
    READ_SCREEN = "read_screen"
    SKIP_TUTORIAL = "skip_tutorial"
    TUTORIAL_PASS = "tutorial_pass"
    CALL_CHEAT = "call_cheat"
    SET_PROPERTY = "set_property"
    CHECK_PROPERTY = "check_property"
    REPEAT_UNTIL = "repeat_until"
    ENTER_SR_DEBUGGER = "enter_sr_debugger"
    INSTALL_APP = "install_app"
    UNINSTALL_APP = "uninstall_app"
    INPUT_TEXT = "input_text"


class TestStep(BaseModel):
    """테스트 스텝"""
    action: ActionType
    target: Optional[str] = None  # find_and_tap의 경우 찾을 요소
    params: Dict = {}  # swipe 좌표, wait 시간 등
    description: str = ""
    timeout: int = 30
    retry: int = 3

class TestCase(BaseModel):
    """테스트 케이스"""
    id: str
    title: str
    description: str = ""
    package: str  # 앱 패키지명
    steps: List[TestStep]
    preconditions: List[str] = []
    expected_results: List[str] = []
    source_scenario: str = ""  # 원본 자연어 시나리오

class TestResult(BaseModel):
    """테스트 결과"""
    test_id: str
    title: str
    status: str  # PASS/FAIL/BLOCKED
    start_time: datetime
    end_time: Optional[datetime] = None
    steps_executed: int = 0
    steps_passed: int = 0
    steps_skipped: int = 0
    error_message: Optional[str] = None
    screenshots: List[str] = []
    step_results: List[Dict] = []  # 스텝별 통과 여부
    context: Dict = {}  # read_text 등으로 저장한 값
    cheat_log: List[Dict] = []  # 이 실행에서 실제로 쓰인 치트/프로퍼티 내역 —
    # [{"seq", "step", "kind", "target", "detail", "ok"}, ...]
    # kind: call_cheat / set_property / check_property / skip_tutorial
    economy_summary: List[Dict] = []  # read_text/read_screen/read_items의 compare_with 결과 —
    # 리포트 화면에 재화/아이템 전후 비교 표로 보여주기 위한 구조화된 목록
    # [{"name", "before", "after", "delta", "passed"}, ...]
    langfuse_trace_id: Optional[str] = None
    eval_output: Optional[Dict] = None
    
class TestCaseManager:
    """테스트케이스 관리자"""
    
    def __init__(self, testcases_dir: Path):
        self.testcases_dir = testcases_dir
        self.testcases: Dict[str, TestCase] = {}
        self._load_testcases()
    
    def _load_testcases(self):
        """YAML 파일에서 테스트케이스 로드"""
        for yaml_file in self.testcases_dir.glob("*.yaml"):
            try:
                with open(yaml_file) as f:
                    data = yaml.safe_load(f)
                    tc = TestCase(**data)
                    self.testcases[tc.id] = tc
                    logger.info(f"Loaded test case: {tc.id}")
            except Exception as e:
                logger.error(f"Failed to load {yaml_file}: {e}")
    
    def get_testcase(self, test_id: str) -> Optional[TestCase]:
        """테스트케이스 조회"""
        return self.testcases.get(test_id)
    
    def save_result(self, result: TestResult, results_dir: Path):
        """테스트 결과 저장"""
        timestamp = result.start_time.strftime("%Y%m%d_%H%M%S")
        result_file = results_dir / f"{result.test_id}_{timestamp}.json"
        
        with open(result_file, 'w', encoding='utf-8') as f:
            json.dump(result.model_dump(), f, indent=2, ensure_ascii=False, default=str)
        
        logger.info(f"Test result saved: {result_file}")
