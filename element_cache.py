import sqlite3
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

logger = logging.getLogger(__name__)


def _connect(db_path: Path) -> sqlite3.Connection:
    """병렬 테스트 대비 커넥션 — WAL + busy_timeout으로 동시 쓰기 락 에러 방지."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@dataclass
class CachedElement:
    x: int
    y: int
    source: str       # 'unity' or 'vision'
    confidence: float


class CommonTapCache:
    """
    게임 공통 UI 좌표 캐시 (SQLite) — 해상도별 관리

    패키지에 관계없이 동일한 요소 (동의합니다, 이용약관 등)의 좌표를 저장.
    키: (element_name, resolution)
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with _connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS common_tap (
                    element_name  TEXT NOT NULL,
                    resolution    TEXT NOT NULL,
                    x             INTEGER NOT NULL,
                    y             INTEGER NOT NULL,
                    source        TEXT NOT NULL DEFAULT 'vision',
                    confidence    REAL DEFAULT 1.0,
                    hit_count     INTEGER DEFAULT 0,
                    last_used_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (element_name, resolution)
                )
            """)

    def get(self, element: str, resolution: str) -> Optional[CachedElement]:
        """공통 좌표 조회. 부분 매칭 지원 (element가 DB의 element_name에 포함되면 HIT)."""
        normalized = element.strip().replace(" ", "")
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT element_name, x, y, source, confidence FROM common_tap WHERE resolution=?",
                (resolution,),
            ).fetchall()

            for row in rows:
                db_key = row[0].strip().replace(" ", "")
                if db_key in normalized or normalized in db_key:
                    conn.execute(
                        """
                        UPDATE common_tap
                        SET hit_count = hit_count + 1, last_used_at = CURRENT_TIMESTAMP
                        WHERE element_name=? AND resolution=?
                        """,
                        (row[0], resolution),
                    )
                    logger.info(
                        "CommonTap HIT  [%s @ %s] → (%d, %d)",
                        row[0], resolution, row[1], row[2],
                    )
                    return CachedElement(x=row[1], y=row[2], source=row[3], confidence=row[4])
        return None

    def set(self, element: str, resolution: str, x: int, y: int,
            source: str = "vision", confidence: float = 1.0) -> None:
        """공통 좌표 저장."""
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO common_tap (element_name, resolution, x, y, source, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(element_name, resolution) DO UPDATE SET
                    x = excluded.x, y = excluded.y,
                    source = excluded.source, confidence = excluded.confidence,
                    last_used_at = CURRENT_TIMESTAMP
                """,
                (element, resolution, x, y, source, confidence),
            )
        logger.info("CommonTap SET  [%s @ %s] → (%d, %d) via %s",
                     element, resolution, x, y, source)

    def dump(self, resolution: Optional[str] = None) -> List[dict]:
        """디버그용 조회."""
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if resolution:
                rows = conn.execute(
                    "SELECT * FROM common_tap WHERE resolution=? ORDER BY element_name",
                    (resolution,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM common_tap ORDER BY resolution, element_name"
                ).fetchall()
        return [dict(r) for r in rows]


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
        with _connect(self.db_path) as conn:
            # 해상도별 좌표 캐시 (v2)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS element_cache_v2 (
                    package_name  TEXT NOT NULL,
                    screen_type   TEXT NOT NULL,
                    element_name  TEXT NOT NULL,
                    resolution    TEXT NOT NULL,
                    x             INTEGER NOT NULL,
                    y             INTEGER NOT NULL,
                    source        TEXT NOT NULL,
                    confidence    REAL DEFAULT 1.0,
                    hit_count     INTEGER DEFAULT 0,
                    last_used_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (package_name, screen_type, element_name, resolution)
                )
            """)
            self._migrate_v1(conn)

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        """v1 테이블이 있으면 720x1280 해상도로 마이그레이션."""
        try:
            rows = conn.execute("SELECT * FROM element_cache LIMIT 1").fetchone()
            if rows is None:
                return
            conn.execute("""
                INSERT OR IGNORE INTO element_cache_v2
                    (package_name, screen_type, element_name, resolution, x, y, source, confidence, hit_count, last_used_at, created_at)
                SELECT package_name, screen_type, element_name, '720x1280', x, y, source, confidence, hit_count, last_used_at, created_at
                FROM element_cache
            """)
            conn.execute("DROP TABLE element_cache")
            logger.info("Migrated element_cache → element_cache_v2 (720x1280)")
        except sqlite3.OperationalError:
            pass  # v1 테이블 없음

    def get(self, package: str, screen_type: str, element: str,
            resolution: str = "") -> Optional[CachedElement]:
        """캐시에서 좌표 조회. 없으면 None."""
        with _connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT x, y, source, confidence FROM element_cache_v2
                WHERE package_name=? AND screen_type=? AND element_name=? AND resolution=?
                """,
                (package, screen_type, element, resolution),
            ).fetchone()

            if not row:
                return None

            conn.execute(
                """
                UPDATE element_cache_v2
                SET hit_count = hit_count + 1, last_used_at = CURRENT_TIMESTAMP
                WHERE package_name=? AND screen_type=? AND element_name=? AND resolution=?
                """,
                (package, screen_type, element, resolution),
            )
            logger.info(
                "Cache HIT  [%s / %s / %s @ %s] → (%d, %d)",
                package, screen_type, element, resolution, row[0], row[1],
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
        resolution: str = "",
    ) -> None:
        """좌표를 캐시에 저장 (이미 있으면 갱신)."""
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO element_cache_v2
                    (package_name, screen_type, element_name, resolution, x, y, source, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(package_name, screen_type, element_name, resolution) DO UPDATE SET
                    x          = excluded.x,
                    y          = excluded.y,
                    source     = excluded.source,
                    confidence = excluded.confidence,
                    last_used_at = CURRENT_TIMESTAMP
                """,
                (package, screen_type, element, resolution, x, y, source, confidence),
            )
        logger.info(
            "Cache SET  [%s / %s / %s @ %s] → (%d, %d) via %s",
            package, screen_type, element, resolution, x, y, source,
        )

    def invalidate_screen(self, package: str, screen_type: str,
                          resolution: str = "") -> None:
        """특정 화면의 캐시 항목 삭제. resolution 지정 시 해당 해상도만."""
        with _connect(self.db_path) as conn:
            if resolution:
                deleted = conn.execute(
                    "DELETE FROM element_cache_v2 WHERE package_name=? AND screen_type=? AND resolution=?",
                    (package, screen_type, resolution),
                ).rowcount
            else:
                deleted = conn.execute(
                    "DELETE FROM element_cache_v2 WHERE package_name=? AND screen_type=?",
                    (package, screen_type),
                ).rowcount
        if deleted:
            logger.info("Cache invalidated [%s / %s] — %d entries removed", package, screen_type, deleted)

    def dump(self, package: Optional[str] = None) -> list[dict]:
        """디버그용: 캐시 전체 또는 패키지별 조회."""
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if package:
                rows = conn.execute(
                    "SELECT * FROM element_cache_v2 WHERE package_name=? ORDER BY resolution, screen_type, element_name",
                    (package,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM element_cache_v2 ORDER BY package_name, resolution, screen_type, element_name"
                ).fetchall()
        return [dict(r) for r in rows]
