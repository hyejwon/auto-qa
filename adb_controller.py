import subprocess
import threading
import time
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

_UI_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def parse_ui_nodes(xml_text: str) -> list[dict]:
    """uiautomator dump XML → 텍스트/설명/체크박스 노드 목록 (좌표 포함).

    반환 필드: text, desc, rid(resource-id 마지막 조각), checkable, checked,
    x1/y1/x2/y2, cx/cy. XML이 비었거나 파싱 불가면 빈 리스트.
    """
    if not xml_text or "<node" not in xml_text:
        return []
    # uiautomator 출력 앞뒤에 안내 문구가 붙는 기종 대응
    start = xml_text.find("<?xml")
    if start < 0:
        start = xml_text.find("<hierarchy")
    try:
        root = ET.fromstring(xml_text[start:] if start >= 0 else xml_text)
    except ET.ParseError as e:
        logger.warning("ui_dump XML 파싱 실패: %s", e)
        return []
    out = []
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        checkable = node.get("checkable") == "true"
        m = _UI_BOUNDS_RE.match(node.get("bounds") or "")
        if not m or not (text or desc or checkable):
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        out.append({
            "text": text, "desc": desc,
            "rid": (node.get("resource-id") or "").rsplit("/", 1)[-1],
            "checkable": checkable,
            "checked": node.get("checked") == "true",
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
        })
    return out


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

    def __init__(self, device_id: Optional[str] = None):
        self.device_id: Optional[str] = None
        # 명시적으로 지정된 디바이스 — 다중 디바이스 서버에서 다른 폰으로 폴백하면 안 됨
        self._requested_device = (device_id or "").strip()
        self._forwarded_ports: set[tuple[int, int]] = set()
        self._forward_map: dict[int, int] = {}  # remote_port → 동적 할당된 local_port
        self._verify_connection()
        self._apply_stay_awake()
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

            # 생성자에서 디바이스가 명시된 경우: 그 디바이스가 없으면 실패 처리.
            # (다중 사용자 환경에서 남의 폰으로 폴백해 테스트가 실행되는 사고 방지)
            if self._requested_device:
                if self._requested_device not in connected_devices:
                    raise ConnectionError(
                        f"지정한 디바이스가 연결되어 있지 않습니다: {self._requested_device}"
                    )
                self.device_id = self._requested_device
                logger.info(f"Connected to device: {self.device_id}")
                return

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

    def _apply_stay_awake(self):
        """QA 기기 화면이 테스트 중 꺼지지 않도록 화면 유지 설정을 적용한다."""
        try:
            self._execute(["shell", "svc", "power", "stayon", "true"])
            # USB/AC/무선 충전 중 화면 유지. 일부 Windows 테스트 기기에서
            # svc 설정만 재연결 후 풀리는 경우가 있어 settings 값도 함께 고정한다.
            self._execute([
                "shell", "settings", "put", "global",
                "stay_on_while_plugged_in", "7",
            ])
            self._execute([
                "shell", "settings", "put", "system",
                "screen_off_timeout", "2147483647",
            ])
            logger.info("Applied stay-awake settings while powered")
        except Exception as e:
            # 실패해도 테스트는 계속 — launch_app의 ensure_screen_on이 폴백
            logger.warning(f"stayon setting failed (non-fatal): {e}")

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

    def forward_port(self, remote_port: int) -> Optional[int]:
        """디바이스별 동적 포트 포워딩 — 호스트 포트를 OS가 할당(tcp:0).

        같은 서버에서 여러 디바이스가 같은 remote_port(예: Unity 37772)를 쓸 때
        고정 local_port는 서로 덮어쓰므로, 디바이스마다 고유한 호스트 포트를 받아온다.
        성공 시 할당된 local_port, 실패 시 None.
        """
        cached = self._forward_map.get(remote_port)
        if cached:
            return cached

        if not self.device_id:
            logger.warning("adb forward skipped: no device selected")
            return None

        try:
            proc = subprocess.run(
                self._adb_cmd(["forward", "tcp:0", f"tcp:{remote_port}"]),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                logger.warning("adb forward failed: %s", (proc.stderr or "")[:200])
                return None

            local_port = int(proc.stdout.strip().splitlines()[-1])
            self._forward_map[remote_port] = local_port
            logger.info(
                "adb forward tcp:%d -> tcp:%d configured (%s)",
                local_port, remote_port, self.device_id,
            )
            return local_port
        except Exception as e:
            logger.warning("adb forward exception: %s", e)
            return None

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
    
    def ui_dump(self) -> str:
        """uiautomator로 현재 화면 UI 트리 XML 덤프 (텍스트/좌표 결정적 추출용)"""
        try:
            self._execute(["shell", "uiautomator", "dump", "/sdcard/uidump.xml"])
            return self._execute(["shell", "cat", "/sdcard/uidump.xml"])
        except Exception as e:
            logger.warning(f"ui_dump failed: {e}")
            return ""

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
    
    def is_screen_on(self) -> bool:
        """화면 켜짐 여부 확인"""
        try:
            output = self._execute(["shell", "dumpsys", "display"])
            for line in output.splitlines():
                if "mScreenState=" in line:
                    return "ON" in line.split("mScreenState=")[-1]
            # 일부 기종은 dumpsys display에 mScreenState가 없어 power로 재확인
            output = self._execute(["shell", "dumpsys", "power"])
            return "mWakefulness=Awake" in output
        except Exception as e:
            logger.warning(f"Screen state check failed: {e}")
            return True  # 확인 불가 시 켜져 있다고 간주 (POWER 토글 오동작 방지)

    def is_locked(self) -> bool:
        """잠금화면(키가드) 표시 여부. 확인 불가 시 False (잠기지 않음으로 간주)."""
        try:
            out = "\n".join([
                self._execute(["shell", "dumpsys", "window"]),
                self._execute(["shell", "dumpsys", "window", "policy"]),
            ])
            normalized = out.lower()
            for token in (
                "mkeyguardshowing=true",
                "keyguardshowing=true",
                "iskeyguardshowing=true",
                "isstatusbarkeyguard=true",
                "mshowinglockscreen=true",
                "showingandnotoccluded=true",
            ):
                if token in normalized:
                    return True
            # 최신 Android의 KeyguardServiceDelegate 출력 형식 대응.
            if "keyguardservicedelegate" in normalized:
                section = normalized.split("keyguardservicedelegate", 1)[1][:1000]
                if re.search(r"\bshowing\s*=\s*true\b", section):
                    return True
            return False
        except Exception as e:
            logger.warning(f"Lock state check failed: {e}")
            return False

    def ensure_screen_on(self) -> bool:
        """화면이 꺼져 있거나 잠겨 있으면 깨우고 잠금을 해제한다.

        1) KEYCODE_WAKEUP으로 화면 켜기
        2) wm dismiss-keyguard — 비보안 잠금(스와이프)은 이걸로 해제
        3) 여전히 잠겨 있고 ADB_UNLOCK_PIN이 설정돼 있으면: 스와이프 업 → PIN 입력 → 엔터
        """
        try:
            if self.is_screen_on() and not self.is_locked():
                return True
            # KEYCODE_WAKEUP은 꺼져 있을 때만 켜므로 POWER 토글보다 안전
            self._execute(["shell", "input", "keyevent", "KEYCODE_WAKEUP"])
            time.sleep(1)
            # 비보안 잠금(스와이프/없음)은 dismiss-keyguard 한 방으로 해제
            self._execute(["shell", "wm", "dismiss-keyguard"])
            time.sleep(0.7)
            if self.is_screen_on() and not self.is_locked():
                logger.info("Screen was off/locked — woke and dismissed keyguard")
                return True

            # 보안 잠금(PIN): 스와이프 업으로 PIN 입력 화면 진입 후 입력
            width, height = self._get_screen_size()
            self._execute(["shell", "input", "swipe",
                           str(width // 2), str(int(height * 0.8)),
                           str(width // 2), str(int(height * 0.2)), "300"])
            time.sleep(1)
            pin = os.getenv("ADB_UNLOCK_PIN", "").strip()
            if pin and self.is_locked():
                if not pin.isdigit():
                    logger.error("ADB_UNLOCK_PIN은 숫자 PIN만 지원합니다.")
                    return False
                self._execute(["shell", "input", "text", pin])
                self._execute(["shell", "input", "keyevent", "KEYCODE_ENTER"])
                time.sleep(1)
                self._execute(["shell", "wm", "dismiss-keyguard"])
                time.sleep(0.5)

            if self.is_screen_on() and not self.is_locked():
                logger.info("Screen was off/locked — unlocked%s", " with PIN" if pin else "")
                return True
            if self.is_locked():
                logger.error(
                    "잠금 해제 실패 — 보안 잠금(PIN/패턴)이 설정된 기기입니다. "
                    ".env에 ADB_UNLOCK_PIN을 설정하거나, QA 기기라면 잠금을 없애는 것을 권장: "
                    "adb shell locksettings clear --old <기존PIN> 후 "
                    "adb shell locksettings set-disabled true"
                )
            else:
                logger.error("Failed to wake screen")
            return False
        except Exception as e:
            logger.error(f"Screen wake/unlock failed: {e}")
            return False

    def launch_app(self, package: str, activity: Optional[str] = None) -> bool:
        """앱 실행"""
        try:
            self.ensure_screen_on()
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
