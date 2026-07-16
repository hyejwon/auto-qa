"""Firebase App Distribution 연동 — 빌드(릴리스) 목록 조회 및 APK 다운로드.

설정: firebase_apps.json (project_root)
  { "<패키지명>": { "project_number": "123456789", "app_id": "1:123456789:android:abcdef" } }

인증 (둘 중 하나):
  1. 서비스 계정 — GOOGLE_APPLICATION_CREDENTIALS(credentials.json).
     각 프로젝트에서 'Firebase App Distribution 뷰어' 역할 부여 필요.
  2. 사용자 계정 ADC — `gcloud auth application-default login`.
     해당 계정이 Firebase 프로젝트 멤버(뷰어 이상)면 별도 역할 부여 불필요.
     사용자 계정은 쿼터 프로젝트가 없어 x-goog-user-project 헤더를 자동 추가한다.

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
    def _raw_config(self) -> dict:
        try:
            if self.config_path.exists():
                return json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("firebase_apps.json 파싱 실패: %s", e)
        return {}

    def apps(self) -> dict:
        """앱 매핑만 반환 ('_'로 시작하는 메타 키는 제외)."""
        return {k: v for k, v in self._raw_config().items()
                if not k.startswith("_") and isinstance(v, dict)}

    def _quota_project(self) -> str:
        """사용자 계정 ADC용 쿼터 프로젝트.

        대상 프로젝트에 뷰어 권한만 있으면 쿼터 프로젝트로 못 쓰므로(serviceusage.use 필요),
        App Distribution API가 활성화된 별도 프로젝트를 지정할 수 있다.
        우선순위: 환경변수 APPDIST_QUOTA_PROJECT > firebase_apps.json의 "_quota_project"
        """
        import os
        return (os.getenv("APPDIST_QUOTA_PROJECT", "").strip()
                or str(self._raw_config().get("_quota_project", "")).strip())

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

    def _is_user_credentials(self) -> bool:
        """사용자 계정 ADC 여부 (서비스 계정이 아닌 gcloud 로그인 크리덴셜)."""
        try:
            from google.oauth2.credentials import Credentials as UserCredentials
            return isinstance(self._creds, UserCredentials)
        except Exception:
            return False

    def _headers(self, project_number: str = "") -> dict:
        headers = {"Authorization": f"Bearer {self._token()}"}
        # 사용자 계정 ADC는 쿼터 프로젝트가 없어 명시해야 한다.
        # (서비스 계정에 이 헤더를 보내면 serviceusage 권한이 추가로 필요해지므로 사용자 계정일 때만)
        if self._is_user_credentials():
            quota = self._quota_project() or project_number
            if quota:
                headers["x-goog-user-project"] = quota
        return headers

    def _app_entry(self, app_key: str) -> dict:
        entry = self.apps().get(app_key)
        if not entry or "project_number" not in entry or "app_id" not in entry:
            raise KeyError(f"firebase_apps.json에 '{app_key}' 항목이 없거나 불완전합니다.")
        return entry

    # ── 릴리스 목록/다운로드 ─────────────────────────────────
    def list_releases(self, app_key: str, page_size: int = 20) -> list[dict]:
        entry = self._app_entry(app_key)
        url = f"{_API}/projects/{entry['project_number']}/apps/{entry['app_id']}/releases"
        resp = requests.get(url, headers=self._headers(entry["project_number"]),
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
        entry = self._app_entry(app_key)
        resp = requests.get(f"{_API}/{release_name}",
                            headers=self._headers(entry["project_number"]), timeout=20)
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
