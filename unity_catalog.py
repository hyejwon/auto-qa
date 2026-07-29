"""게임별 v2 치트/프로퍼티 카탈로그 누적 캐시.

왜 필요한가 — 치트와 프로퍼티는 **씬 단위로 등록**된다. 로비에서 목록을 조회하면
`outgame.*`만, 전투 화면에서 조회하면 `ingame.*`만 잡힌다(2026-07-27 이지스 디펜스
실기기 확인). 그래서 한 번의 조회로는 그 게임의 전체 치트를 알 수 없다.

이 모듈은 조회할 때마다 스냅샷을 패키지별 카탈로그에 **누적 병합**한다. 로비에서 한 번,
전투에서 한 번 찍히면 그 게임의 카탈로그가 완성되고, 이후에는 디바이스를 다시 몰지 않아도
플래너가 그 게임에서 쓸 수 있는 치트/프로퍼티를 전부 알 수 있다.

플래너는 이 카탈로그를 프롬프트로 받아 `call_cheat` / `set_property` / `check_property`
스텝을 **실제로 존재하는 id와 인자 스펙에 맞춰** 생성한다 — 게임마다 사람이 직접 돌려보며
템플릿을 쓰지 않아도 되게 하는 것이 목적이다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_PATH = Path(__file__).parent / "unity_catalog.json"


def _load_all(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("카탈로그 로드 실패 (%s): %s", path, exc)
        return {}


def _save_all(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("카탈로그 저장 실패 (%s): %s", path, exc)


def merge_snapshot(
    package: str,
    snapshot: Dict[str, List[Dict[str, Any]]],
    path: Path = DEFAULT_CATALOG_PATH,
) -> Dict[str, int]:
    """현재 씬 스냅샷을 패키지 카탈로그에 누적 병합. 반환: 누적 후 개수."""
    if not package:
        return {"cheats": 0, "properties": 0, "new": 0}

    data = _load_all(path)
    entry = data.setdefault(package, {"cheats": {}, "properties": {}, "scenes_seen": []})

    new_count = 0
    for kind in ("cheats", "properties"):
        bucket = entry.setdefault(kind, {})
        for item in snapshot.get(kind) or []:
            item_id = item.get("Id")
            if not isinstance(item_id, str) or not item_id:
                continue
            if item_id not in bucket:
                new_count += 1
            bucket[item_id] = item

    # 어떤 씬에서 찍혔는지 카테고리 최상위로 대략 기록 — 씬 커버리지 확인용
    scenes = {
        str(item.get("Category") or "").split("/")[0]
        for item in (snapshot.get("cheats") or [])
        if item.get("Category")
    }
    seen = set(entry.get("scenes_seen") or []) | scenes
    entry["scenes_seen"] = sorted(seen)
    entry["updated_at"] = datetime.now().isoformat(timespec="seconds")

    data[package] = entry
    _save_all(path, data)

    counts = {
        "cheats": len(entry.get("cheats") or {}),
        "properties": len(entry.get("properties") or {}),
        "new": new_count,
    }
    logger.info(
        "카탈로그 병합 [%s]: 치트 %d개 / 프로퍼티 %d개 (신규 %d)",
        package, counts["cheats"], counts["properties"], new_count,
    )
    return counts


def load(package: str, path: Path = DEFAULT_CATALOG_PATH) -> Dict[str, Any]:
    return _load_all(path).get(package) or {}


def _param_text(param: Dict[str, Any]) -> str:
    name = param.get("Name")
    ptype = param.get("Type")
    parts = [f"{name}:{ptype}"]
    if param.get("DefaultValue") is not None:
        parts.append(f"기본={param['DefaultValue']}")
    enum_names = param.get("EnumNames")
    if isinstance(enum_names, list) and enum_names:
        parts.append("값=" + "|".join(str(v) for v in enum_names))
    elif param.get("Min") is not None or param.get("Max") is not None:
        parts.append(f"범위={param.get('Min')}~{param.get('Max')}")
    if param.get("Required"):
        parts.append("필수")
    return " ".join(parts)


def to_planner_text(package: str, path: Path = DEFAULT_CATALOG_PATH) -> str:
    """플래너 프롬프트에 넣을 카탈로그 텍스트. 카탈로그가 없으면 빈 문자열."""
    entry = load(package, path)
    cheats: Dict[str, Any] = entry.get("cheats") or {}
    props: Dict[str, Any] = entry.get("properties") or {}
    if not cheats and not props:
        return ""

    lines: List[str] = []

    if cheats:
        lines.append("**치트 (call_cheat로 실행):**")
        for cheat in sorted(cheats.values(), key=lambda c: (c.get("Category") or "", c.get("Id") or "")):
            params = cheat.get("Parameters") or []
            arg_text = ""
            if params:
                arg_text = "  args: " + ", ".join(_param_text(p) for p in params)
            desc = (cheat.get("Description") or "").strip().replace("\n", " ")
            lines.append(
                f"- `{cheat.get('Id')}` [{cheat.get('Category')}] {cheat.get('DisplayName')}"
                + (f" — {desc}" if desc else "")
                + arg_text
            )

    if props:
        lines.append("")
        lines.append("**프로퍼티 (check_property로 읽기 / set_property로 쓰기):**")
        for prop in sorted(props.values(), key=lambda p: (p.get("Category") or "", p.get("Id") or "")):
            rw = []
            if prop.get("CanRead"):
                rw.append("읽기")
            if prop.get("CanWrite"):
                rw.append("쓰기")
            enum_names = prop.get("EnumNames")
            enum_text = ""
            if isinstance(enum_names, list) and enum_names:
                enum_text = " 값=" + "|".join(str(v) for v in enum_names)
            lines.append(
                f"- `{prop.get('Id')}` [{prop.get('Category')}] {prop.get('DisplayName')}"
                f" ({prop.get('Type')}, {'/'.join(rw) or '접근불가'}){enum_text}"
            )

    scenes = entry.get("scenes_seen") or []
    if scenes:
        lines.append("")
        lines.append(f"(수집된 씬 카테고리: {', '.join(scenes)} — updated {entry.get('updated_at')})")

    return "\n".join(lines)


def summary(package: str = "", path: Path = DEFAULT_CATALOG_PATH) -> Dict[str, Any]:
    """UI/디버그용 요약."""
    data = _load_all(path)
    if package:
        entry = data.get(package) or {}
        return {
            "package": package,
            "cheats": len(entry.get("cheats") or {}),
            "properties": len(entry.get("properties") or {}),
            "scenes_seen": entry.get("scenes_seen") or [],
            "updated_at": entry.get("updated_at"),
        }
    return {
        pkg: {
            "cheats": len(e.get("cheats") or {}),
            "properties": len(e.get("properties") or {}),
            "scenes_seen": e.get("scenes_seen") or [],
            "updated_at": e.get("updated_at"),
        }
        for pkg, e in data.items()
    }
