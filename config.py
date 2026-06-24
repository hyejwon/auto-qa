from dataclasses import dataclass
from pathlib import Path
import sys

@dataclass
class PathConfig:
    """경로 설정"""
    # 현재 실행 파일(.exe)이 있는 '진짜' 폴더 위치
    if getattr(sys, 'frozen', False):
        exe_root = Path(sys.executable).parent
        bundle_root = Path(sys._MEIPASS)
    else:
        exe_root = Path(__file__).parent
        bundle_root = exe_root

    # [수정] 프로그램이 파일을 쓰거나 사용자가 관리하는 폴더 (exe 옆)
    project_root = exe_root 
    screenshots_dir: Path = project_root / "screenshots"
    debug_dir: Path = project_root / "screenshots_debug"
    results_dir: Path = project_root / "test_results"
    testcases_dir: Path = project_root / "testcases"
    recordings_dir: Path = project_root / "recordings"
    apks_dir: Path = project_root / "apks"
    cache_db: Path = project_root / "element_cache.db"
    common_cache_db: Path = project_root / "common_tap_cache.db"

    # [유지] 빌드 시 내부에 포함시킨 정적 자원 (임시 폴더 안)
    templates_dir: Path = bundle_root / "templates"
    cache_db: Path = project_root / "element_cache.db"
    common_cache_db: Path = project_root / "common_tap_cache.db"

    def __post_init__(self):
        """디렉토리 자동 생성"""
        for path in [self.screenshots_dir, self.debug_dir,
                     self.results_dir, self.testcases_dir, self.recordings_dir,
                     self.apks_dir, self.templates_dir]:
            path.mkdir(parents=True, exist_ok=True)

@dataclass
class GeminiConfig:
    """Gemini API 설정"""
    model: str = "gemini-3.1-flash-lite"
    temperature: float = 0.1
    max_retries: int = 3
    gateway_url: str = "https://llm-gateway.111percent.net/llm/google"
    # 하위 호환: Vertex AI 직접 연결 시 사용 (gateway 없을 때)
    project: str = ""
    location: str = "global"

class Config:
    """전체 설정 통합"""
    paths = PathConfig()
    gemini = GeminiConfig()
