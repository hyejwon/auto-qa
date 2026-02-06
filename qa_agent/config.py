# qa_agent/config.py
import os
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _get_first_adb_device() -> str:
    """ADB로 연결된 첫 번째 디바이스 ID 반환, 없으면 기본값"""
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.split("\n")[1:]:
            if "\t" in line:
                device_id = line.split("\t")[0]
                if device_id:
                    return device_id
    except Exception:
        pass
    return "emulator-5554"


@dataclass
class QAConfig:
    USE_STABLE_SCREEN: bool = False
    project: str = os.getenv("VERTEX_PROJECT", "percent-vertex-test")
    location: str = os.getenv("VERTEX_LOCATION", "global")
    model: str = os.getenv("VERTEX_MODEL", "gemini-2.5-pro")

    debug: bool = os.getenv("QA_MCP_DEBUG", "0") == "1"
    max_steps: int = int(os.getenv("QA_MAX_STEPS", "100"))
    max_find_attempts: int = int(os.getenv("QA_MAX_FIND_ATTEMPTS", "3"))

    package_name: str = os.getenv("QA_PACKAGE", "com.percent.aos.cooptd")
    #device_id: str = field(default_factory=lambda: os.getenv("QA_DEVICE") or _get_first_adb_device())
    device_id = "127.0.0.1:5565"
    # Default to the *current* interpreter to keep Debug/Run environments consistent.
    # You can override with QA_MCP_CMD if you need a different Python.
    mcp_command: str = os.getenv("QA_MCP_CMD", sys.executable)
    mcp_args: list[str] = None

    def __post_init__(self):
        if self.mcp_args is None:
            repo_root = Path(__file__).resolve().parent.parent
            self.mcp_args = [str(repo_root / "mcp_server.py")]
