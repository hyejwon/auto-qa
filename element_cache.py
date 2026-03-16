import sqlite3
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CachedElement:
    x: int
    y: int
    source: str       # 'unity' or 'vision'
    confidence: float


class ElementCache:
    """
    UI 요소 좌표 캐시 (SQLite)

    키: (package_name, screen_type, element_name)
    - package_name : 앱 패키지명  (예: com.percent.aos.cooptd)
    - screen_type  : 현재 화면    (예: title, lobby, shop, settings)
    - element_name : 찾는 요소명  (예: 햄버거 메뉴, 설정 메뉴)
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS element_cache (
                    package_name  TEXT NOT NULL,
                    screen_type   TEXT NOT NULL,
                    element_name  TEXT NOT NULL,
                    x             INTEGER NOT NULL,
                    y             INTEGER NOT NULL,
                    source        TEXT NOT NULL,
                    confidence    REAL DEFAULT 1.0,
                    hit_count     INTEGER DEFAULT 0,
                    last_used_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (package_name, screen_type, element_name)
                )
            """)

    def get(self, package: str, screen_type: str, element: str) -> Optional[CachedElement]:
        """캐시에서 좌표 조회. 없으면 None."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT x, y, source, confidence FROM element_cache
                WHERE package_name=? AND screen_type=? AND element_name=?
                """,
                (package, screen_type, element),
            ).fetchone()

            if not row:
                return None

            conn.execute(
                """
                UPDATE element_cache
                SET hit_count = hit_count + 1, last_used_at = CURRENT_TIMESTAMP
                WHERE package_name=? AND screen_type=? AND element_name=?
                """,
                (package, screen_type, element),
            )
            logger.info(
                "Cache HIT  [%s / %s / %s] → (%d, %d)",
                package, screen_type, element, row[0], row[1],
            )
            return CachedElement(x=row[0], y=row[1], source=row[2], confidence=row[3])

    def set(
        self,
        package: str,
        screen_type: str,
        element: str,
        x: int,
        y: int,
        source: str,
        confidence: float = 1.0,
    ) -> None:
        """좌표를 캐시에 저장 (이미 있으면 갱신)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO element_cache
                    (package_name, screen_type, element_name, x, y, source, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(package_name, screen_type, element_name) DO UPDATE SET
                    x          = excluded.x,
                    y          = excluded.y,
                    source     = excluded.source,
                    confidence = excluded.confidence,
                    last_used_at = CURRENT_TIMESTAMP
                """,
                (package, screen_type, element, x, y, source, confidence),
            )
        logger.info(
            "Cache SET  [%s / %s / %s] → (%d, %d) via %s",
            package, screen_type, element, x, y, source,
        )

    def invalidate_screen(self, package: str, screen_type: str) -> None:
        """특정 화면의 모든 캐시 항목 삭제."""
        with sqlite3.connect(self.db_path) as conn:
            deleted = conn.execute(
                "DELETE FROM element_cache WHERE package_name=? AND screen_type=?",
                (package, screen_type),
            ).rowcount
        if deleted:
            logger.info("Cache invalidated [%s / %s] — %d entries removed", package, screen_type, deleted)

    def dump(self, package: Optional[str] = None) -> list[dict]:
        """디버그용: 캐시 전체 또는 패키지별 조회."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if package:
                rows = conn.execute(
                    "SELECT * FROM element_cache WHERE package_name=? ORDER BY screen_type, element_name",
                    (package,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM element_cache ORDER BY package_name, screen_type, element_name"
                ).fetchall()
        return [dict(r) for r in rows]
