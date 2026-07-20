"""Firebase App Tester 앱을 adb로 조작해 빌드 목록 조회/설치.

App Distribution REST API 권한 없이 '테스터 초대'만으로 동작하는 폴백 경로:
  - 폰의 QA 구글 계정이 해당 앱의 테스터로 초대되어 있어야 함
  - 폰에 App Tester 설치 + 로그인 + "이 출처의 앱 설치 허용" ON (최초 1회)

추출 방식: uiautomator dump(UI 트리 XML)를 1순위로 사용 — 텍스트/좌표가
결정적으로 나오므로 Vision 추측이 없다. XML에 텍스트가 없을 때(Compose
시맨틱 미노출 등)만 Vision으로 폴백한다.
"""
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from adb_controller import ADBController, parse_ui_nodes
from vision_agent import GeminiVisionAgent

logger = logging.getLogger(__name__)

# App Tester 패키지명 — 기종/버전에 따라 다르면 .env로 재정의
APP_TESTER_PACKAGE = os.getenv("APP_TESTER_PACKAGE", "dev.firebase.appdistribution")

# "1.2.3", "1.2.3 (456)", "1.2.3(456)" 형태의 버전 텍스트
VERSION_RE = re.compile(r"\d+(?:\.\d+)+\s*(?:\(\d+\))?")
# "com.foo.bar" 형태의 패키지명 텍스트 (홈 목록 행에 노출되는 기종/버전 존재)
PACKAGE_RE = re.compile(r"^[a-zA-Z][\w]*(?:\.[\w]+){2,}$")

# App Tester 홈 화면에서 앱 이름이 아닌 UI 텍스트 (소문자 비교)
_HOME_CHROME = {
    "app tester", "firebase app tester", "firebase",
    "앱", "apps", "설정", "settings", "도움말", "help",
    "계정", "account", "다운로드됨", "downloaded", "최근 업데이트",
    "테스트 앱", "test apps", "모든 테스트 앱", "all test apps",
}
# 앱 이름 아래 줄에 붙는 부가 정보 ("출시 버전 91개" / "3 releases")
_HOME_INFO_RE = re.compile(r"^(출시 버전 \d+개|\d+\s+releases?)$", re.IGNORECASE)
# 초대 대기 중 항목의 이름 접미사 / 수락 버튼
_PENDING_RE = re.compile(r"\s*\((대기 중|pending)\)\s*$", re.IGNORECASE)
_INVITE_TEXTS = {"초대 수락", "accept invitation"}

# 앱 진입 시 데이터 수집 동의 화면 (이름/기기 제조업체/모델/OS 수집)
_CONSENT_HINTS = ("제조업체", "manufacturer", "수집", "collect")
_CONSENT_CONFIRM = ("확인", "동의", "계속", "시작하기", "ok", "continue", "accept", "agree", "get started")
# "이 기기에서 테스트 시작" 같은 버튼은 부분 일치로 매칭
_CONSENT_CONFIRM_SUBSTR = ("테스트 시작", "start testing")


def _is_consent_confirm(text: str) -> bool:
    t = text.strip().lower()
    return t in _CONSENT_CONFIRM or any(s in t for s in _CONSENT_CONFIRM_SUBSTR)


class AppTesterDriver:
    def __init__(self, device_id: Optional[str], work_dir: Path):
        self.adb = ADBController(device_id)
        self._vision: Optional[GeminiVisionAgent] = None
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)

    @property
    def vision(self) -> GeminiVisionAgent:
        """Vision 폴백은 실제로 필요할 때만 초기화 (API 키 없이도 XML 경로 사용 가능)"""
        if self._vision is None:
            self._vision = GeminiVisionAgent()
        return self._vision

    # ── UI 트리(XML) 헬퍼 — 결정적 추출 ─────────────────────
    def _nodes(self) -> list[dict]:
        """현재 화면의 텍스트/설명/체크박스 노드들 (좌표 포함)"""
        return parse_ui_nodes(self.adb.ui_dump())

    def _tap_node(self, predicate: Callable[[dict], bool], scrolls: int = 0) -> bool:
        """조건에 맞는 노드를 찾아 탭. 못 찾으면 스크롤하며 재시도."""
        for i in range(scrolls + 1):
            for n in self._nodes():
                if predicate(n):
                    self.adb.tap(n["cx"], n["cy"])
                    time.sleep(1.5)
                    return True
            if i < scrolls:
                self._scroll_down()
        return False

    @staticmethod
    def _same_row(a: dict, b: dict) -> bool:
        return max(a["y1"], b["y1"]) < min(a["y2"], b["y2"])

    # ── Vision 폴백 ─────────────────────────────────────────
    def _screenshot(self, tag: str) -> Path:
        path = self.work_dir / f"apptester_{tag}_{int(time.time()*1000)}.png"
        self.adb.screenshot(path)
        return path

    def _vision_tap(self, target: str, tag: str, scrolls: int = 0) -> bool:
        for i in range(scrolls + 1):
            shot = self._screenshot(f"{tag}_{i}")
            result = self.vision.find_element(shot, target)
            if result.success and result.bbox:
                c = result.bbox.to_pixels(self.adb.width, self.adb.height)
                self.adb.tap(c["x"], c["y"])
                time.sleep(1.5)
                return True
            if i < scrolls:
                self._scroll_down()
        return False

    # ── 공통 동작 ───────────────────────────────────────────
    def _scroll_down(self) -> None:
        w, h = self.adb.width, self.adb.height
        self.adb.swipe(w // 2, int(h * 0.70), w // 2, int(h * 0.35), duration=400)
        time.sleep(1)

    def _launch_home(self) -> None:
        """App Tester 재시작 → 홈(앱 목록) 화면 (상태 일관성 확보)"""
        self.adb.close_app(APP_TESTER_PACKAGE)
        time.sleep(1)
        self.adb.launch_app(APP_TESTER_PACKAGE)
        time.sleep(3)

    def _open_game(self, game_name: str) -> bool:
        """App Tester 재시작 후 게임 진입 (+ 데이터 수집 동의 화면 처리)"""
        self._launch_home()
        name = game_name.strip().lower()
        if self._tap_node(
            lambda n: name in n["text"].lower() or name in n["desc"].lower(),
            scrolls=3,
        ):
            self._accept_consent()
            return True
        logger.info("XML에서 '%s' 미발견 — Vision 폴백", game_name)
        if self._vision_tap(f"앱 목록의 '{game_name}' 항목", "game", scrolls=3):
            self._accept_consent()
            return True
        return False

    def _accept_consent(self) -> None:
        """버전 목록 앞의 데이터 수집 동의 화면 — 체크박스 체크 후 확인 버튼.

        동의 화면이 아니면 아무것도 하지 않는다. (앱 최초 진입 시에만 표시됨)
        """
        for attempt in range(3):
            nodes = self._nodes()
            blob = " ".join(n["text"] + " " + n["desc"] for n in nodes).lower()
            if not any(h in blob for h in _CONSENT_HINTS):
                return
            logger.info("데이터 수집 동의 화면 감지 (시도 %d)", attempt + 1)
            checked_already = any(n["checkable"] and n["checked"] for n in nodes)
            if checked_already:
                logger.info("│  체크박스 이미 체크됨 — 확인 버튼만 탐색")
            else:
                box = next((n for n in nodes if n["checkable"] and not n["checked"]), None)
                if box:
                    self.adb.tap(box["cx"], box["cy"])
                    logger.info("│  체크박스 탭 (%d, %d)", box["cx"], box["cy"])
                    time.sleep(1)
                else:
                    # 체크박스가 XML에 checkable로 안 잡히는 기종(Compose 등) —
                    # "…동의합니다" 라벨 텍스트를 대신 탭 (라벨 탭도 체크박스를 토글함)
                    label = next(
                        (n for n in nodes
                         if ("동의" in n["text"] or "agree" in n["text"].lower()
                             or "동의" in n["desc"] or "agree" in n["desc"].lower())
                         and not _is_consent_confirm(n["text"])
                         and not _is_consent_confirm(n["desc"])), None)
                    if label:
                        self.adb.tap(label["cx"], label["cy"])
                        logger.info("│  동의 라벨 탭 (%d, %d): %s",
                                    label["cx"], label["cy"],
                                    (label["text"] or label["desc"])[:40])
                        time.sleep(1)
                    elif attempt == 0:
                        logger.info("│  체크박스/동의 라벨 미발견 — Vision 폴백")
                        self._vision_tap("데이터 수집 동의 체크박스", "consent")
            confirm = next(
                (n for n in self._nodes()
                 if _is_consent_confirm(n["text"]) or _is_consent_confirm(n["desc"])), None)
            if confirm:
                self.adb.tap(confirm["cx"], confirm["cy"])
                logger.info("│  확인 버튼 탭: %s", confirm["text"] or confirm["desc"])
                time.sleep(1.5)
            else:
                logger.warning("│  확인 버튼 미발견 — 재시도")

    # ── 공개 API ────────────────────────────────────────────
    @staticmethod
    def _is_home_info(text: str) -> bool:
        """앱 이름이 아니라 행의 부가 정보/버튼 텍스트인지"""
        return bool(_HOME_INFO_RE.match(text)) or text.strip().lower() in _INVITE_TEXTS

    @classmethod
    def _is_game_name(cls, n: dict) -> bool:
        """홈 목록 노드가 앱(프로젝트) 이름인지 — chrome/버전/패키지/이메일/부가정보 제외"""
        text = n["text"]
        if not text or len(text) > 40:
            return False
        if text.lower() in _HOME_CHROME or "@" in text:
            return False
        if VERSION_RE.fullmatch(text) or PACKAGE_RE.match(text):
            return False
        if cls._is_home_info(text):
            return False
        return True

    def list_games(self, max_pages: int = 6) -> list[dict]:
        """홈 화면의 앱(프로젝트) 목록. [{name, info, package}] — XML 우선, 실패 시 Vision.

        package는 목록 행에 패키지명 텍스트가 노출되는 경우에만 채워진다.
        "출시 버전 N개" 같은 아랫줄 텍스트는 바로 위 앱 이름의 info로 붙인다.
        """
        self._launch_home()
        games: dict[str, dict] = {}
        prev_sig = ""
        stalls = 0
        for page in range(max_pages):
            nodes = self._nodes()
            name_nodes = [n for n in nodes if self._is_game_name(n)]
            # 앱 이름 아랫줄의 패키지명/부가 정보 노드를 바로 위의 앱 이름에 귀속
            # (홈 목록 행 구성: 앱 이름 → 패키지명 → "출시 버전 N개")
            attached_info: dict[int, list[str]] = {id(n): [] for n in name_nodes}
            attached_pkg: dict[int, str] = {}
            for n in nodes:
                text = n["text"]
                if not text:
                    continue
                is_pkg = bool(PACKAGE_RE.match(text))
                if not is_pkg and not self._is_home_info(text):
                    continue
                above = [g for g in name_nodes if g["y1"] <= n["y1"] and g is not n]
                if not above:
                    continue
                owner = max(above, key=lambda g: g["y1"])
                if n["cy"] - owner["y2"] >= 250:
                    continue
                if is_pkg:
                    attached_pkg.setdefault(id(owner), text)
                else:
                    attached_info[id(owner)].append(text)
            for gn in name_nodes:
                raw = gn["text"]
                pending = bool(_PENDING_RE.search(raw))
                name = _PENDING_RE.sub("", raw).strip()
                if not name:
                    continue
                row = [n for n in nodes
                       if n is not gn and n["text"] and self._same_row(gn, n)]
                package = next(
                    (n["text"] for n in row if PACKAGE_RE.match(n["text"])),
                    attached_pkg.get(id(gn), ""))
                info_parts = [n["text"] for n in row if not PACKAGE_RE.match(n["text"])]
                info_parts += attached_info[id(gn)]
                if pending:
                    info_parts = [p for p in info_parts
                                  if p.strip().lower() not in _INVITE_TEXTS]
                    info_parts.append("초대 대기 중")
                info = " · ".join(info_parts)
                existing = games.get(name)
                if existing:
                    # 이전 페이지에서 아랫줄이 잘린 채 등록된 항목 보완
                    if package and not existing["package"]:
                        existing["package"] = package
                    if info and not existing["info"]:
                        existing["info"] = info
                    continue
                games[name] = {"name": name, "info": info, "package": package}
            if page == 0 and not nodes:
                # UI 트리에 텍스트가 전혀 없음 → Vision 폴백 (1회)
                logger.info("ui_dump 텍스트 없음 — Vision으로 앱 목록 추출")
                shot = self._screenshot("games_vision")
                for it in self.vision.extract_items(
                    shot, "테스트 가능한 앱(프로젝트) 목록 — 각 항목의 앱 이름과 부가 정보"
                ):
                    name = str(it.get("text", "")).strip()
                    if name and name not in games:
                        games[name] = {
                            "name": name,
                            "info": str(it.get("info", "")).strip(),
                            "package": "",
                        }
                break
            sig = "|".join(n["text"] for n in nodes)
            if sig == prev_sig:
                stalls += 1
                if stalls >= 2:  # 두 번 연속 그대로 → 목록 끝
                    break
                time.sleep(2)  # 로딩 지연으로 화면이 안 바뀌었을 수 있음 — 한 번 더
            else:
                stalls = 0
            prev_sig = sig
            if page < max_pages - 1:
                self._scroll_down()
        return list(games.values())

    def list_builds(self, game_name: str, max_pages: int = 6) -> list[dict]:
        """게임의 빌드(릴리스) 목록. [{version, info}] — XML 우선, 실패 시 Vision."""
        if not self._open_game(game_name):
            raise RuntimeError(
                f"App Tester에서 '{game_name}'을 찾지 못함 — 테스터 초대/로그인 확인 필요"
            )
        builds: dict[str, str] = {}
        prev_sig = ""
        stalls = 0
        for page in range(max_pages):
            nodes = self._nodes()
            # 버전은 정확 일치만 — "버전 x.y.z 설치됨" 배너, 릴리스 노트 속
            # 버전 문자열, "333.95 MB" 같은 날짜/크기 줄 오탐 방지
            version_nodes = [n for n in nodes
                             if n["text"] and VERSION_RE.fullmatch(n["text"].strip())]
            if version_nodes:
                # 버전 아랫줄의 짧은 부가 정보(날짜/크기)를 위의 버전에 귀속.
                # 릴리스 노트/빌드명 같은 긴 텍스트는 제외.
                extras: dict[int, list[str]] = {id(v): [] for v in version_nodes}
                for n in nodes:
                    t = n["text"].strip()
                    if not t or len(t) > 80 or VERSION_RE.fullmatch(t):
                        continue
                    if t.lower() in _HOME_CHROME:  # 탭/헤더 라벨
                        continue
                    above = [v for v in version_nodes if v["y1"] <= n["y1"]]
                    if not above:
                        continue
                    owner = max(above, key=lambda v: v["y1"])
                    if n["cy"] - owner["y2"] < 200:
                        extras[id(owner)].append(t)
                for vn in version_nodes:
                    ver = vn["text"].strip()
                    if ver not in builds:
                        builds[ver] = " · ".join(extras[id(vn)])
            elif page == 0 and not nodes:
                # UI 트리에 텍스트가 전혀 없음 → Vision 폴백 (1회)
                logger.info("ui_dump 텍스트 없음 — Vision으로 빌드 목록 추출")
                shot = self._screenshot("builds_vision")
                for it in self.vision.extract_items(
                    shot, "빌드(릴리스) 목록 항목 — 각 항목의 버전 번호와 날짜/크기 정보"
                ):
                    text = str(it.get("text", "")).strip()
                    if text and text not in builds:
                        builds[text] = str(it.get("info", "")).strip()

            sig = "|".join(n["text"] for n in nodes)
            if sig == prev_sig:
                stalls += 1
                if stalls >= 2:  # 두 번 연속 그대로 → 목록 끝
                    break
                time.sleep(2)  # 로딩 지연으로 화면이 안 바뀌었을 수 있음 — 한 번 더
            else:
                stalls = 0
            prev_sig = sig
            if page < max_pages - 1:
                self._scroll_down()
        return [{"version": v, "info": info} for v, info in builds.items()]

    def install_build(self, game_name: str, version: str,
                      download_timeout: int = 600) -> tuple[bool, str]:
        """지정 버전 빌드를 다운로드하고 설치. (성공 여부, 메시지)"""
        if not self._open_game(game_name):
            return False, f"'{game_name}'을 App Tester에서 찾지 못함 (테스터 초대 확인)"

        ver_key = version.strip()
        if not self._start_download(ver_key):
            return False, f"버전 '{version}'을 목록에서 찾지 못함"

        # 행 탭으로 카드가 펼쳐진 경우 → 카드 안의 '다운로드' 버튼 →
        # (설치 버튼) → (시스템 설치 팝업) → '열기' 노출까지 폴링
        deadline = time.time() + download_timeout
        while time.time() < deadline:
            time.sleep(8)
            nodes = self._nodes()
            texts = {n["text"] for n in nodes} | {n["desc"] for n in nodes}
            self._tap_download_near(nodes, ver_key)
            # 설치 진행 계열 버튼이 보이면 누른다 (XML 기준 정확 매칭)
            for label in ("설치", "Install", "확인", "업데이트"):
                node = next((n for n in nodes
                             if n["text"] == label or n["desc"] == label), None)
                if node:
                    self.adb.tap(node["cx"], node["cy"])
                    time.sleep(2)
            if any(t in ("열기", "Open") for t in texts):
                # 설치 화면 정리 — '완료'를 눌러 닫고 복귀
                done = next((n for n in nodes
                             if n["text"] in ("완료", "Done")), None)
                if done:
                    self.adb.tap(done["cx"], done["cy"])
                    time.sleep(1)
                return True, f"버전 {version} 설치 완료"
        return False, f"설치 완료 확인 실패 (제한시간 {download_timeout}초) — 화면 확인 필요"

    @staticmethod
    def _is_download_label(n: dict) -> bool:
        return (n["rid"] in ("download_button", "download_label")
                or n["text"].strip() in ("다운로드", "Download")
                or n["desc"].strip() in ("다운로드", "Download"))

    def _download_btn_near(self, nodes: list[dict], row: Optional[dict]) -> Optional[dict]:
        """버전 행 아래 펼쳐진 카드 영역의 다운로드 버튼 (없으면 None)"""
        if row is None:
            return None
        return next(
            (n for n in nodes
             if n is not row and self._is_download_label(n)
             and n["y1"] >= row["y1"] and n["y1"] - row["y2"] < 600),
            None)

    def _tap_download_near(self, nodes: list[dict], version: str) -> bool:
        """선택한 버전 카드 주변의 '다운로드' 버튼 탭.

        다른 버전 행의 다운로드 버튼을 잘못 누르지 않도록,
        버전 텍스트가 보일 때는 그 카드 영역만 허용한다.
        """
        row = next((n for n in nodes if n["text"] and version in n["text"]), None)
        if row:
            btn = self._download_btn_near(nodes, row)
        else:
            # 카드가 펼쳐지며 버전 텍스트가 화면 밖으로 밀린 경우 —
            # 화면에 다운로드 버튼이 하나뿐일 때만 탭
            dls = [n for n in nodes if self._is_download_label(n)]
            btn = dls[0] if len(dls) == 1 else None
        if btn:
            self.adb.tap(btn["cx"], btn["cy"])
            time.sleep(2)
            return True
        return False

    def _start_download(self, version: str) -> bool:
        """버전 카드의 다운로드 버튼을 눌러 다운로드 시작.

        행 탭은 카드 펼침/접힘 토글이라(최신 버전 카드는 기본 펼침),
        버튼이 이미 보이면 바로 누르고, 없을 때만 행을 탭해 카드를 펼친다.
        """
        expanded = False
        for i in range(7):
            nodes = self._nodes()
            row = next((n for n in nodes
                        if n["text"] and version in n["text"]), None)
            if row:
                btn = self._download_btn_near(nodes, row)
                if btn:
                    self.adb.tap(btn["cx"], btn["cy"])
                    time.sleep(2)
                    return True
                if not expanded:
                    # 접힌 카드 → 행을 탭해 펼치고 다음 루프에서 버튼 탐색
                    self.adb.tap(row["cx"], row["cy"])
                    expanded = True
                    time.sleep(1.5)
                else:
                    # 펼쳤는데도 버튼이 안 보임 → 카드가 화면 아래로 잘렸을 수 있음
                    expanded = False
                    self._scroll_down()
                continue
            if not nodes and i == 0:
                # XML 불가 → Vision 폴백
                return self._vision_tap(
                    f"버전 '{version}' 빌드 항목의 다운로드 버튼", "download", scrolls=6)
            self._scroll_down()
        return False
