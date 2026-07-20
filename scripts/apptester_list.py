"""App Tester 빌드 목록 추출 검증 스크립트 (사무실 PC용).

폰에 App Tester가 로그인돼 있는 상태에서, uiautomator XML로 빌드 목록이
결정적으로 읽히는지 UI 없이 확인한다.

사용법 (레포 루트, 폰 연결 상태에서):
    python scripts/apptester_list.py --apps                # 프로젝트(앱) 목록
    python scripts/apptester_list.py --game "Coop TD"      # 특정 앱의 빌드 목록
    python scripts/apptester_list.py --game "Coop TD" --device R39N403X6VH

먼저 XML 덤프 자체가 되는지만 보려면:
    python scripts/apptester_list.py --dump-only
"""
import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", default="", help="App Tester에 표시되는 앱 이름")
    parser.add_argument("--device", default="", help="adb 디바이스 시리얼 (생략 시 자동)")
    parser.add_argument("--apps", action="store_true",
                        help="App Tester 홈의 프로젝트(앱) 목록 출력")
    parser.add_argument("--dump-only", action="store_true",
                        help="현재 화면의 UI 트리 텍스트 노드만 출력 (App Tester 조작 없음)")
    args = parser.parse_args()

    from app_tester_driver import AppTesterDriver, APP_TESTER_PACKAGE

    driver = AppTesterDriver(args.device or None, Path("screenshots_debug/apptester"))

    if args.dump_only:
        nodes = driver._nodes()
        if not nodes:
            print("❌ ui_dump에서 텍스트 노드를 얻지 못함 — Vision 폴백이 필요한 화면")
            return 1
        print(f"✅ 텍스트 노드 {len(nodes)}개:")
        for n in nodes:
            label = n["text"] or f"(desc) {n['desc']}"
            print(f"  [{n['cx']},{n['cy']}] {label}")
        return 0

    if args.apps:
        print(f"[*] App Tester({APP_TESTER_PACKAGE}) 실행 → 프로젝트(앱) 목록 조회 중...")
        try:
            games = driver.list_games()
        except Exception as e:
            print(f"❌ 실패: {e}")
            return 1
        if not games:
            print("⚠️ 프로젝트 목록이 비어 있음 — 로그인/테스터 초대 확인")
            return 1
        print(f"✅ 프로젝트 {len(games)}개:")
        for g in games:
            extra = " · ".join(x for x in (g["package"], g["info"]) if x)
            print(f"  {g['name']}" + (f"  ({extra})" if extra else ""))
        return 0

    if not args.game:
        print("--game, --apps 또는 --dump-only 를 지정하세요.")
        return 1

    print(f"[*] App Tester({APP_TESTER_PACKAGE}) 실행 → '{args.game}' 빌드 목록 조회 중...")
    try:
        builds = driver.list_builds(args.game)
    except Exception as e:
        print(f"❌ 실패: {e}")
        return 1
    if not builds:
        print("⚠️ 빌드 목록이 비어 있음 — 테스터 초대/로그인/앱 이름 확인")
        return 1
    print(f"✅ 빌드 {len(builds)}개:")
    for b in builds:
        print(f"  {b['version']}" + (f"  ({b['info']})" if b['info'] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
