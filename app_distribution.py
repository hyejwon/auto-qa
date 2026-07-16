"""Firebase App Distribution 연동 — 빌드(릴리스) 목록 조회 및 APK 다운로드.

설정: firebase_apps.json (project_root)
  { "<패키지명>": { "project_number": "123456789", "app_id": "1:123456789:android:abcdef" } }

인증: GOOGLE_APPLICATION_CREDENTIALS(credentials.json) 서비스 계정.
  각 Firebase 프로젝트에서 서비스 계정에 'Firebase App Distribution 뷰어'
  (roles/firebaseappdistribution.viewer) 역할이 부여되어 있어야 한다.

전제: App Distribution에 APK로 업로드된 빌드만 지원 (AAB는 adb 직접 설치 불가).
"""
import json
import logging
import re
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_API = "https://firebaseappdistribution.googleapis.com/v1"
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class AppDistributionClient:
    def __init__(self, config_path: Path, cache_dir: Path):
        self.config_path = config_path
        self.cache_dir = cache_dir
        self._creds = None

    # ── 설정/인증 ────────────────────────────────────────────
    def apps(self) -> dict:
        """firebase_apps.json 로드. 없으면 빈 dict."""
        try:
            if self.config_path.exists():
                return json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("firebase_apps.json 파싱 실패: %s", e)
        return {}

    def configured(self) -> bool:
        """서비스 계정 인증이 가능한 상태인지 (토큰 발급 시도 없이 가볍게 확인)."""
        try:
            import google.auth
            google.auth.default(scopes=[_SCOPE])
            return True
        except Exception:
            return False

    def _token(self) -> str:
        import google.auth
        import google.auth.transport.requests
        if self._creds is None:
            self._creds, _ = google.auth.default(scopes=[_SCOPE])
        if not self._creds.valid:
            self._creds.refresh(google.auth.transport.requests.Request())
        return self._creds.token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    def _app_entry(self, app_key: str) -> dict:
        entry = self.apps().get(app_key)
        if not entry or "project_number" not in entry or "app_id" not in entry:
            raise KeyError(f"firebase_apps.json에 '{app_key}' 항목이 없거나 불완전합니다.")
        return entry

    # ── 릴리스 목록/다운로드 ─────────────────────────────────
    def list_releases(self, app_key: str, page_size: int = 20) -> list[dict]:
        entry = self._app_entry(app_key)
        url = f"{_API}/projects/{entry['project_number']}/apps/{entry['app_id']}/releases"
        resp = requests.get(url, headers=self._headers(),
                            params={"pageSize": page_size}, timeout=20)
        resp.raise_for_status()
        releases = resp.json().get("releases", [])
        out = []
        for rel in releases:
            out.append({
                "name": rel.get("name", ""),
                "display_version": rel.get("displayVersion", ""),
                "build_version": rel.get("buildVersion", ""),
                "create_time": rel.get("createTime", ""),
                "release_notes": (rel.get("releaseNotes") or {}).get("text", ""),
                "cached": self._cache_path(app_key, rel).exists(),
            })
        return out

    def download_release(self, app_key: str, release_name: str) -> Path:
        """릴리스 APK를 캐시에 다운로드 (이미 있으면 재사용). 반환: APK 경로."""
        # 목록의 binaryDownloadUri는 만료될 수 있어 단건 재조회로 신선한 URI 확보
        resp = requests.get(f"{_API}/{release_name}", headers=self._headers(), timeout=20)
        resp.raise_for_status()
        rel = resp.json()

        path = self._cache_path(app_key, rel)
        if path.exists():
            logger.info("AppDist 캐시 HIT: %s", path.name)
            return path

        uri = rel.get("binaryDownloadUri")
        if not uri:
            raise RuntimeError("binaryDownloadUri가 없습니다 — 권한(뷰어 역할) 또는 릴리스 상태 확인 필요")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        logger.info("AppDist 다운로드 시작: %s v%s(%s)",
                    app_key, rel.get("displayVersion"), rel.get("buildVersion"))
        with requests.get(uri, stream=True, timeout=600) as dl:  # 서명 URL — 인증 헤더 불필요
            dl.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in dl.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        tmp.rename(path)
        logger.info("AppDist 다운로드 완료: %s (%.1f MB)", path.name, path.stat().st_size / 1e6)
        return path

    def _cache_path(self, app_key: str, rel: dict) -> Path:
        rid = (rel.get("name") or "unknown").rsplit("/", 1)[-1]
        version = f"{rel.get('displayVersion', '')}_{rel.get('buildVersion', '')}"
        safe = re.sub(r"[^\w.\-]", "_", f"{app_key}_{version}_{rid}")
        return self.cache_dir / f"{safe}.apk"
