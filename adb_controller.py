import subprocess
import threading
import time
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

def _runtime_roots() -> list[Path]:
    """Return likely resource roots for normal and PyInstaller execution."""
    if getattr(sys, "frozen", False):
        roots = [Path(sys.executable).parent]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass))
        return roots
    return [Path(__file__).parent]


def _resolve_adb_executable() -> str:
    env_path = os.getenv("ADB_PATH", "").strip()
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))

    adb_name = "adb.exe" if os.name == "nt" else "adb"
    for root in _runtime_roots():
        candidates.extend([
            root / "platform-tools" / adb_name,
            root / "adb" / adb_name,
            root / adb_name,
        ])

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return shutil.which("adb") or "adb"


class ADBController:
    """ADB 명령 래퍼 클래스"""

    CHUNK_SECONDS = 180  # screenrecord 최대 제한 (3분)
    ADB_BIN = _resolve_adb_executable()

    def __init__(self):
        self.device_id: Optional[str] = None
        self._forwarded_ports: set[tuple[int, int]] = set()
        self._verify_connection()
        self.width, self.height = self._get_screen_size()
        # 녹화 상태
        self._recording: bool = False
        self._rec_stop: threading.Event = threading.Event()
        self._rec_thread: Optional[threading.Thread] = None
        self._rec_files: list[Path] = []
        self._rec_session: str = ""
        self._rec_output_dir: Optional[Path] = None

    def _verify_connection(self):
        """디바이스 연결 확인"""
        try:
            result = subprocess.run(
                self._adb_cmd(["devices"]),
                capture_output=True, text=True, timeout=5
            )
            lines = [line for line in result.stdout.split("\n")[1:] if "\t" in line and "device" in line]
            if not lines:
                raise ConnectionError("No device connected")

            connected_devices = [line.split("\t")[0] for line in lines]
            preferred_device = os.getenv("ADB_DEVICE", "").strip()

            if preferred_device and preferred_device in connected_devices:
                self.device_id = preferred_device
            else:
                if preferred_device and preferred_device not in connected_devices:
                    logger.warning(
                        "Preferred device '%s' not found. Falling back to '%s'.",
                        preferred_device,
                        connected_devices[0],
                    )
                self.device_id = connected_devices[0]

            logger.info(f"Connected to device: {self.device_id}")
        except Exception as e:
            logger.error(f"Device connection failed: {e}")
            raise

    def _adb_cmd(self, cmd: list[str]) -> list[str]:
        if self.device_id:
            return [self.ADB_BIN, "-s", self.device_id] + cmd
        return [self.ADB_BIN] + cmd

    def shell(self, command: str) -> str:
        """adb shell 명령 실행"""
        return self._execute(["shell"] + command.split())

    def _get_screen_size(self) -> tuple:
        """연결된 디바이스의 화면 크기 자동 감지"""
        try:
            output = self._execute(["shell", "wm", "size"])
            if "x" in output:
                parts = output.split()[-1].split("x")
                if len(parts) == 2:
                    return (int(parts[0]), int(parts[1]))
        except Exception:
            pass
        return (720, 1280)

    def _execute(self, cmd: list, capture: bool = True) -> str:
        """ADB 명령 실행"""
        full_cmd = self._adb_cmd(cmd)
        logger.debug(f"Executing: {' '.join(full_cmd)}")
        
        try:
            result = subprocess.run(
                full_cmd,
                capture_output=capture,
                text=True,
                timeout=30
            )
            return result.stdout.strip() if capture else ""
        except subprocess.TimeoutExpired:
            logger.error(f"Command timeout: {' '.join(full_cmd)}")
            raise
        except Exception as e:
            logger.error(f"Command failed: {e}")
            raise

    def ensure_forward(self, local_port: int = 37772, remote_port: int = 37772) -> bool:
        """adb forward tcp:<local_port> tcp:<remote_port>"""
        port_key = (local_port, remote_port)
        if port_key in self._forwarded_ports:
            return True

        if not self.device_id:
            logger.warning("adb forward skipped: no device selected")
            return False

        try:
            proc = subprocess.run(
                self._adb_cmd(
                    ["forward", f"tcp:{local_port}", f"tcp:{remote_port}"]
                ),
                capture_output=True,
                timeout=10,
            )
            if proc.returncode != 0:
                logger.warning(
                    "adb forward failed: %s",
                    proc.stderr.decode(errors="ignore")[:200],
                )
                return False

            self._forwarded_ports.add(port_key)
            logger.info("adb forward tcp:%d -> tcp:%d configured", local_port, remote_port)
            return True
        except Exception as e:
            logger.warning("adb forward exception: %s", e)
            return False
    
    def tap(self, x: int, y: int, delay: float = 0.5) -> bool:
        """화면 탭"""
        try:
            # 화면 범위 검증
            if not (0 <= x <= self.width and 0 <= y <= self.height):
                logger.warning(f"Coordinates out of bounds: ({x}, {y})")
                return False
            
            self._execute(["shell", "input", "tap", str(x), str(y)])
            time.sleep(delay)
            logger.info(f"Tapped at ({x}, {y})")
            return True
        except Exception as e:
            logger.error(f"Tap failed: {e}")
            return False
    
    def swipe(self, x1: int, y1: int, x2: int, y2: int, 
              duration: int = 300, delay: float = 0.5) -> bool:
        """스와이프"""
        try:
            self._execute([
                "shell", "input", "swipe",
                str(x1), str(y1), str(x2), str(y2), str(duration)
            ])
            time.sleep(delay)
            logger.info(f"Swiped from ({x1},{y1}) to ({x2},{y2})")
            return True
        except Exception as e:
            logger.error(f"Swipe failed: {e}")
            return False
    
    def screenshot(self, save_path: Optional[Path] = None, retries: int = 3) -> bytes:
        """스크린샷 캡처 (타임아웃 시 재시도)"""
        last_err = None
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    self._adb_cmd(["exec-out", "screencap", "-p"]),
                    capture_output=True,
                    timeout=15
                )
                if save_path:
                    save_path.write_bytes(result.stdout)
                return result.stdout
            except subprocess.TimeoutExpired as e:
                last_err = e
                logger.warning(f"Screenshot timeout (attempt {attempt + 1}/{retries})")
                time.sleep(1)
            except Exception as e:
                logger.error(f"Screenshot failed: {e}")
                raise
        raise last_err
    
    def get_current_activity(self) -> str:
        """현재 Activity 확인"""
        try:
            output = self._execute([
                "shell", "dumpsys", "window", "|", "grep", "mCurrentFocus"
            ])
            return output
        except Exception as e:
            logger.error(f"Get activity failed: {e}")
            return ""
    
    def press_back(self, delay: float = 0.5) -> bool:
        """뒤로가기 버튼"""
        try:
            self._execute(["shell", "input", "keyevent", "KEYCODE_BACK"])
            time.sleep(delay)
            return True
        except Exception as e:
            logger.error(f"Back press failed: {e}")
            return False
    
    def press_home(self, delay: float = 0.5) -> bool:
        """홈 버튼"""
        try:
            self._execute(["shell", "input", "keyevent", "KEYCODE_HOME"])
            time.sleep(delay)
            return True
        except Exception as e:
            logger.error(f"Home press failed: {e}")
            return False
    
    def launch_app(self, package: str, activity: Optional[str] = None) -> bool:
        """앱 실행"""
        try:
            if activity:
                target = f"{package}/{activity}"
            else:
                # 메인 Activity 자동 탐지
                target = package
            
            self._execute(["shell", "monkey", "-p", package, "-c", 
                          "android.intent.category.LAUNCHER", "1"])
            time.sleep(2)
            logger.info(f"Launched app: {package}")
            return True
        except Exception as e:
            logger.error(f"App launch failed: {e}")
            return False
        
    # def get_ui_dump(self) -> str:
    #     """
    #     UI Hierarchy 덤프 가져오기
    #     Returns: XML 문자열
    #     """
    #     try:
    #         # UI 덤프를 디바이스 임시 파일에 저장
    #         dump_path = "/sdcard/window_dump.xml"
    #         self._execute(["shell", "uiautomator", "dump", dump_path])
            
    #         # 파일 내용 읽기
    #         result = self._execute(["shell", "cat", dump_path])
            
    #         # 임시 파일 삭제
    #         self._execute(["shell", "rm", dump_path])
            
    #         logger.info("UI dump retrieved successfully")
    #         return result
    #     except Exception as e:
    #         logger.error(f"UI dump failed: {e}")
    #         return ""
    
    # def verify_ui_text(self, expected_text: str, match_type: str = "contains") -> bool:
    #     """
    #     UI Dump에서 특정 텍스트 존재 확인
        
    #     Args:
    #         expected_text: 찾을 텍스트
    #         match_type: 매칭 방식 (contains, exact, regex)
    #     """
    #     try:
    #         ui_dump = self.get_ui_dump()
    #         if not ui_dump:
    #             return False
            
    #         if match_type == "contains":
    #             found = expected_text in ui_dump
    #         elif match_type == "exact":
    #             # XML 파싱 후 정확한 매칭
    #             import xml.etree.ElementTree as ET
    #             root = ET.fromstring(ui_dump)
    #             found = any(
    #                 elem.get('text') == expected_text or 
    #                 elem.get('content-desc') == expected_text or
    #                 elem.get('resource-id') == expected_text
    #                 for elem in root.iter()
    #             )
    #         elif match_type == "regex":
    #             import re
    #             found = bool(re.search(expected_text, ui_dump))
    #         else:
    #             logger.warning(f"Unknown match_type: {match_type}")
    #             return False
            
    #         if found:
    #             logger.info(f"Text found in UI: {expected_text}")
    #         else:
    #             logger.warning(f"Text NOT found in UI: {expected_text}")
            
    #         return found
    #     except Exception as e:
    #         logger.error(f"UI text verification failed: {e}")
    #         return False    


    # def find_and_click(text: str):
    #     # UI 덤프
    #     subprocess.run('adb shell uiautomator dump /sdcard/ui.xml', shell=True)
    #     subprocess.run('adb pull /sdcard/ui.xml', shell=True)
        
    #     tree = ET.parse('ui.xml')
    #     for node in tree.iter('node'):
    #         if node.get('text') == text:
    #             bounds = node.get('bounds')
    #             # bounds="[x1,y1][x2,y2]" 파싱
    #             coords = bounds.replace('][', ',').replace('[', '').replace(']', '').split(',')
    #             x = (int(coords[0]) + int(coords[2])) // 2
    #             y = (int(coords[1]) + int(coords[3])) // 2
    #             subprocess.run(f'adb shell input tap {x} {y}', shell=True)
    #             print(f"클릭: {text} ({x}, {y})")
    #             return True
    #     print(f"버튼 못 찾음: {text}")
    #     return False

    # find_and_click("동의")
    # find_and_click("확인")

    # def login_google(self):
    #     """
    #     PIN -> email -> password 
    #     """
        
    #     # PIN 입력
    #     self._execute([
    #             "shell", "input", "text", "0000"
    #         ])
        
    #     self._execute([
    #             "shell", "input", "keyevent", "66"
    #         ]) 

    #     # email 
    #     self._execute([
    #             "shell", "input", "text", "qa_google_02@111percent.net"
    #         ])

    #     self._execute([
    #             "shell", "input", "keyevent", "66"
    #         ])  
    #     return 1
    
    def install_apk(self, apk_path: Path | str) -> tuple[bool, str]:
        """APK 설치."""
        apk_path = Path(apk_path)
        if not apk_path.exists():
            return False, f"파일 없음: {apk_path}"
        try:
            proc = subprocess.run(
                self._adb_cmd(["install", "-r", "-d", str(apk_path)]),
                capture_output=True,
                text=True,
                timeout=180,
            )
            output = (proc.stdout + proc.stderr).strip()
            if proc.returncode == 0 and "Success" in output:
                logger.info("APK installed: %s", apk_path.name)
                return True, f"✅ 설치 완료: {apk_path.name}"
            logger.warning("APK install failed: %s", output)
            return False, f"❌ 설치 실패: {output[:300]}"
        except subprocess.TimeoutExpired:
            return False, "❌ 타임아웃 (180초 초과)"
        except Exception as e:
            logger.error("APK install exception: %s", e)
            return False, f"❌ 오류: {e}"

    def uninstall_app(self, package: str) -> tuple[bool, str]:
        """앱 삭제 (uninstall)."""
        if not package or not package.strip():
            return False, "패키지명이 비어 있습니다."
        try:
            proc = subprocess.run(
                self._adb_cmd(["uninstall", package.strip()]),
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = (proc.stdout + proc.stderr).strip()
            if proc.returncode == 0 and "Success" in output:
                logger.info("App uninstalled: %s", package)
                return True, f"✅ 삭제 완료: {package}"
            logger.warning("App uninstall failed: %s", output)
            return False, f"❌ 삭제 실패: {output[:300]}"
        except subprocess.TimeoutExpired:
            return False, "❌ 타임아웃 (60초 초과)"
        except Exception as e:
            logger.error("App uninstall exception: %s", e)
            return False, f"❌ 오류: {e}"

    def close_app(self, package: str) -> bool:
        """앱 종료"""
        try:
            self._execute(["shell", "am", "force-stop", package])
            logger.info(f"Closed app: {package}")
            return True
        except Exception as e:
            logger.error(f"App close failed: {e}")
            return False

    # ─── Google 계정 ───────────────────────────────────────────────

    def get_google_accounts(self) -> list[str]:
        """디바이스에 등록된 Google 계정 목록 반환."""
        try:
            output = self._execute([
                "shell", "dumpsys", "account"
            ])
            accounts = []
            for line in output.splitlines():
                line = line.strip()
                if "Account {" in line and "com.google" in line:
                    # Account {name=foo@gmail.com, type=com.google}
                    name_part = [p for p in line.split(",") if "name=" in p]
                    if name_part:
                        email = name_part[0].split("name=")[-1].strip()
                        accounts.append(email)
            logger.info(f"Google accounts found: {accounts}")
            return accounts
        except Exception as e:
            logger.error(f"get_google_accounts failed: {e}")
            return []

    def ensure_google_account(self, email: str) -> bool:
        """
        지정한 Google 계정이 디바이스에 등록되어 있는지 확인한다.
        등록되어 있으면 True, 없으면 False를 반환하고 경고 로그를 출력한다.

        사용법:
            if not adb.ensure_google_account("qa@example.com"):
                raise RuntimeError("테스트 전 디바이스에 Google 계정을 수동으로 등록하세요.")
        """
        accounts = self.get_google_accounts()
        matched = any(email.lower() in a.lower() for a in accounts)
        if matched:
            logger.info(f"Google account verified: {email}")
        else:
            logger.warning(
                f"Google account '{email}' NOT found on device. "
                f"Registered accounts: {accounts}. "
                "디바이스 설정 > 계정 > Google에서 수동으로 계정을 추가한 뒤 다시 실행하세요."
            )
        return matched

    # ─── 화면 녹화 ────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start_recording(self, output_dir: Path, session_name: str = "") -> bool:
        """
        화면 녹화 시작.
        3분(CHUNK_SECONDS) 단위로 자동 분할하여 output_dir 에 저장.
        Returns True if started successfully.
        """
        if self._recording:
            logger.warning("Recording already in progress")
            return False

        if not session_name:
            session_name = datetime.now().strftime("%Y%m%d_%H%M%S")

        self._rec_stop.clear()
        self._rec_files = []
        self._rec_session = session_name
        self._rec_output_dir = output_dir
        self._rec_thread = threading.Thread(
            target=self._record_loop, daemon=True
        )
        self._rec_thread.start()
        self._recording = True
        logger.info(f"Recording started — session: {session_name}, output: {output_dir}")
        return True

    def stop_recording(self) -> list[Path]:
        """
        화면 녹화 중지.
        Returns 저장된 청크 파일 목록.
        """
        if not self._recording:
            logger.warning("No recording in progress")
            return []

        self._rec_stop.set()
        # 디바이스 위 screenrecord 프로세스에 SIGINT 전달
        subprocess.run(
            self._adb_cmd(["shell", "pkill", "-SIGINT", "screenrecord"]),
            capture_output=True, timeout=5,
        )
        if self._rec_thread:
            self._rec_thread.join(timeout=20)

        self._recording = False
        saved = list(self._rec_files)
        logger.info(f"Recording stopped — {len(saved)} chunk(s) saved")
        return saved

    def _record_loop(self) -> None:
        """3분 청크 단위 녹화 루프 (백그라운드 스레드에서 실행)."""
        chunk = 0
        while not self._rec_stop.is_set():
            device_path = f"/sdcard/rec_{self._rec_session}_{chunk:03d}.mp4"
            local_path = self._rec_output_dir / f"rec_{self._rec_session}_{chunk:03d}.mp4"

            logger.info(f"Recording chunk {chunk} → {local_path.name}")

            proc = subprocess.Popen(
                self._adb_cmd([
                    "shell", "screenrecord",
                    "--time-limit", str(self.CHUNK_SECONDS),
                    device_path,
                ]),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # 청크 완료 또는 stop 이벤트 대기
            while proc.poll() is None:
                if self._rec_stop.is_set():
                    subprocess.run(
                        self._adb_cmd(["shell", "pkill", "-SIGINT", "screenrecord"]),
                        capture_output=True, timeout=5,
                    )
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                time.sleep(0.5)

            # screenrecord 가 moov 아톰을 기록 완료할 때까지 대기
            time.sleep(4)

            # 디바이스 → 로컬 pull
            pull = subprocess.run(
                self._adb_cmd(["pull", device_path, str(local_path)]),
                capture_output=True, text=True, timeout=60,
            )
            if pull.returncode == 0 and local_path.exists():
                self._rec_files.append(local_path)
                logger.info(f"Chunk saved: {local_path.name}")
            else:
                logger.warning(f"Pull failed (chunk {chunk}): {pull.stderr.strip()}")

            # 디바이스 임시 파일 삭제
            subprocess.run(
                self._adb_cmd(["shell", "rm", "-f", device_path]),
                capture_output=True,
            )

            chunk += 1
            if self._rec_stop.is_set():
                break
