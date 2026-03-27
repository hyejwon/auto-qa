from dataclasses import dataclass
from pathlib import Path

@dataclass
class PathConfig:
    """경로 설정"""
    project_root: Path = Path(__file__).parent
    screenshots_dir: Path = project_root / "screenshots"
    debug_dir: Path = project_root / "screenshots_debug"
    results_dir: Path = project_root / "test_results"
    testcases_dir: Path = project_root / "testcases"
    recordings_dir: Path = project_root / "recordings"
    apks_dir: Path = project_root / "apks"
    templates_dir: Path = project_root / "templates"
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
    project: str = "percent-vertex-test"
    location: str = "global"
    model: str = "gemini-3.1-flash-lite-preview"
    temperature: float = 0.1
    max_retries: int = 3

class Config:
    """전체 설정 통합"""
    paths = PathConfig()
    gemini = GeminiConfig()