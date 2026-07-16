"""Firebase App Distribution 연동 사전 점검.

firebase_apps.json의 모든 앱에 대해 릴리스 목록 API를 1건씩 호출해
서비스 계정 권한(App Distribution 뷰어)과 설정값을 검증한다.

사용법 (레포 루트에서):
    python scripts/check_appdist.py

판정:
    ✅ OK          — 권한/설정 정상 (최신 빌드 표시)
    ❌ 403         — 서비스 계정에 해당 프로젝트의 뷰어 역할 없음
    ❌ 404         — project_number 또는 app_id 오류
    ❌ 인증 실패    — credentials.json 미설정/잘못됨
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_distribution import AppDistributionClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    config = ROOT / "firebase_apps.json"
    if not config.exists():
        print("❌ firebase_apps.json 없음 — firebase_apps.json.example을 복사해 작성하세요.")
        return 1

    client = AppDistributionClient(config, ROOT / "apks" / "appdist")
    apps = client.apps()
    if not apps:
        print("❌ firebase_apps.json에 등록된 앱이 없습니다.")
        return 1

    try:
        client._token()
    except Exception as e:
        print(f"❌ 인증 실패 — credentials.json(서비스 계정) 확인 필요: {e}")
        return 1
    print("✅ 서비스 계정 인증 OK")

    failed = 0
    for app_key in apps:
        try:
            releases = client.list_releases(app_key, page_size=1)
            if releases:
                r = releases[0]
                print(f"✅ {app_key} — OK (최신: v{r['display_version']} ({r['build_version']}) {r['create_time'][:10]})")
            else:
                print(f"✅ {app_key} — 권한 OK, 업로드된 빌드 없음")
        except KeyError as e:
            print(f"❌ {app_key} — 설정 불완전: {e}")
            failed += 1
        except Exception as e:
            msg = str(e)
            if "403" in msg:
                print(f"❌ {app_key} — 권한 없음(403): 이 프로젝트에서 서비스 계정에 "
                      f"'Firebase App Distribution 뷰어' 역할을 부여하세요.")
            elif "404" in msg:
                print(f"❌ {app_key} — 404: project_number/app_id 값 확인 필요.")
            else:
                print(f"❌ {app_key} — 실패: {msg}")
            failed += 1

    print(f"\n{'✅ 전체 통과' if failed == 0 else f'❌ {failed}/{len(apps)}개 실패'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
