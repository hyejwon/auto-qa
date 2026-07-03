"""SR Debugger UI entry automation without SR HTTP API.

SR API is deprecated.  This module only performs app-side gestures through ADB
and optionally verifies the resulting screen with the existing Vision agent.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from adb_controller import ADBController
from config import Config
from vision_agent import GeminiVisionAgent

logger = logging.getLogger(__name__)


class SRDebuggerEnterRequest(BaseModel):
    package: str = ""
    strategies: list[dict[str, Any]] = Field(default_factory=list)
    verify_target: str = "SRDebugger"
    max_attempts: int = 2


class SRDebuggerController:
    def __init__(self, adb: Optional[ADBController] = None, config: Config = Config()):
        self.config = config
        self.adb = adb or ADBController()
        self.vision: Optional[GeminiVisionAgent] = None

    def enter(
        self,
        package: str = "",
        strategies: Optional[list[dict[str, Any]]] = None,
        verify_target: str = "SRDebugger",
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        if package:
            self.adb.launch_app(package)
            time.sleep(2)

        attempts: list[dict[str, Any]] = []
        for attempt_no in range(1, max(1, max_attempts) + 1):
            for strategy in strategies or self.default_strategies():
                self._run_strategy(strategy)
                screenshot = self._screenshot("sr_debugger_probe")
                verified = self._verify_screen(screenshot, verify_target) if verify_target else False
                record = {
                    "attempt": attempt_no,
                    "strategy": strategy,
                    "screenshot": str(screenshot),
                    "verified": verified,
                    "verify_target": verify_target,
                }
                attempts.append(record)
                if verified:
                    return {"success": True, "status": "opened", "attempts": attempts}

        return {
            "success": False,
            "status": "not_verified",
            "attempts": attempts,
            "message": "SR Debugger 진입 제스처를 실행했지만 verify_target을 화면에서 확인하지 못했습니다.",
        }

    def default_strategies(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "fixed_30_30_fast_double_tap",
                "coordinate_space": "unity_pixels",
                "x": 30,
                "y": 30,
                "count": 2,
                "interval_sec": 0.03,
                "post_wait_sec": 1.0,
            },
            {
                "name": "bottom_left_version_multi_tap",
                "coordinate_space": "screen_ratio",
                "x_ratio": 0.08,
                "y_ratio": 0.965,
                "count": 7,
                "interval_sec": 0.08,
                "post_wait_sec": 1.0,
            },
            {
                "name": "top_left_multi_tap",
                "coordinate_space": "screen_ratio",
                "x_ratio": 0.08,
                "y_ratio": 0.08,
                "count": 7,
                "interval_sec": 0.08,
                "post_wait_sec": 1.0,
            },
            {
                "name": "top_right_multi_tap",
                "coordinate_space": "screen_ratio",
                "x_ratio": 0.92,
                "y_ratio": 0.08,
                "count": 7,
                "interval_sec": 0.08,
                "post_wait_sec": 1.0,
            },
        ]

    def _run_strategy(self, strategy: dict[str, Any]) -> None:
        coordinate_space = str(strategy.get("coordinate_space") or "screen_ratio")
        count = int(strategy.get("count", 7))
        interval = float(strategy.get("interval_sec", 0.08))
        post_wait = float(strategy.get("post_wait_sec", 1.0))
        x, y = self._resolve_tap_xy(strategy, coordinate_space)

        logger.info("Trying SR Debugger gesture %s at (%d,%d) x%d", strategy.get("name"), x, y, count)
        if count > 1:
            self._tap_repeated_fast(x, y, count, interval)
        else:
            self.adb.tap(x, y, delay=interval)
        time.sleep(post_wait)

    def _tap_repeated_fast(self, x: int, y: int, count: int, interval: float) -> None:
        commands = []
        for idx in range(max(1, count)):
            commands.append(f"input tap {x} {y}")
            if idx < count - 1:
                commands.append(f"sleep {max(0.01, interval):.3f}")
        script = "; ".join(commands)
        subprocess.run(
            self.adb._adb_cmd(["shell", "sh", "-c", script]),
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _resolve_tap_xy(self, strategy: dict[str, Any], coordinate_space: str) -> tuple[int, int]:
        if "x" in strategy and "y" in strategy:
            x = int(strategy["x"])
            y = int(strategy["y"])
            if coordinate_space in {"unity", "unity_pixel", "unity_pixels"}:
                y = self.adb.height - y
            return self._clamp_xy(x, y)

        x_ratio = float(strategy.get("x_ratio", 0.08))
        y_ratio = float(strategy.get("y_ratio", 0.965))
        x = int(self.adb.width * x_ratio)
        if coordinate_space in {"unity_ratio", "unity_normalized"}:
            y = int(self.adb.height * (1.0 - y_ratio))
        else:
            y = int(self.adb.height * y_ratio)
        return self._clamp_xy(x, y)

    def _clamp_xy(self, x: int, y: int) -> tuple[int, int]:
        return (
            max(0, min(self.adb.width - 1, x)),
            max(0, min(self.adb.height - 1, y)),
        )

    def _screenshot(self, prefix: str) -> Path:
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = self.config.paths.debug_dir / f"{prefix}_{ts}.png"
        self.adb.screenshot(path)
        return path

    def _verify_screen(self, screenshot_path: Path, target: str) -> bool:
        try:
            if self.vision is None:
                self.vision = GeminiVisionAgent(
                    project=self.config.gemini.project,
                    location=self.config.gemini.location,
                    model=self.config.gemini.model,
                )
            result = self.vision.find_element(screenshot_path, target, self.config.paths.debug_dir)
            return bool(result.success)
        except Exception as exc:
            logger.warning("SR Debugger verify failed: %s", exc)
            return False
