from __future__ import annotations

import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qr_codec import build_payload, write_qr_image


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIR / "thermalmaster.sqlite3"
QR_CODES_DIR = PROJECT_ROOT / "qr_codes"

SITE_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")


class SiteAlreadyExistsError(RuntimeError):
    pass


class SiteNotFoundError(RuntimeError):
    pass


class SiteActiveError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_site_code(value: str) -> str:
    code = str(value or "").strip().upper()
    if not SITE_CODE_RE.fullmatch(code):
        raise ValueError("Site code must use 1-32 letters, numbers, hyphens, or underscores")
    return code


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def db_session():
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    QR_CODES_DIR.mkdir(parents=True, exist_ok=True)
    with db_session() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                qr_folder TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
                number INTEGER NOT NULL,
                payload TEXT NOT NULL UNIQUE,
                qr_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(site_id, number)
            );

            CREATE TABLE IF NOT EXISTS processing_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL CHECK(status IN ('active', 'completed', 'abandoned')),
                paused INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cow_visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES processing_sessions(id) ON DELETE RESTRICT,
                cow_id INTEGER NOT NULL REFERENCES cows(id) ON DELETE RESTRICT,
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                completed_at REAL,
                status TEXT NOT NULL CHECK(status IN ('active', 'completed', 'overridden', 'abandoned')),
                capture_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(capture_status IN ('pending', 'scheduled', 'capturing', 'completed', 'failed', 'skipped')),
                detection_count INTEGER NOT NULL DEFAULT 1,
                override_of INTEGER REFERENCES cow_visits(id) ON DELETE RESTRICT,
                UNIQUE(session_id, cow_id, id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS unique_current_visit
                ON cow_visits(session_id, cow_id)
                WHERE status IN ('active', 'completed');
            """
        )
        session_columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(processing_sessions)").fetchall()
        }
        if "test_mode" not in session_columns:
            db.execute(
                "ALTER TABLE processing_sessions ADD COLUMN test_mode INTEGER NOT NULL DEFAULT 0"
            )
        if "paused" not in session_columns:
            db.execute(
                "ALTER TABLE processing_sessions ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"
            )
        visit_columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(cow_visits)").fetchall()
        }
        if "capture_status" not in visit_columns:
            db.execute(
                "ALTER TABLE cow_visits ADD COLUMN capture_status TEXT NOT NULL DEFAULT 'pending'"
            )


def list_sites() -> list[dict[str, Any]]:
    with db_session() as db:
        rows = db.execute(
            """
            SELECT sites.code, sites.created_at, sites.qr_folder, COUNT(cows.id) AS cow_count
            FROM sites
            LEFT JOIN cows ON cows.site_id = sites.id
            GROUP BY sites.id
            ORDER BY sites.code
            """
        ).fetchall()
    return [dict(row) for row in rows]


def delete_site(code: str, capture_roots: tuple[Path, ...] = ()) -> dict[str, Any]:
    normalized = normalize_site_code(code)
    roots = (QR_CODES_DIR, *capture_roots)
    targets = [(Path(root).resolve() / normalized) for root in roots]
    if any(target.parent != Path(root).resolve() for root, target in zip(roots, targets)):
        raise ValueError("Invalid site data path")

    staged: list[tuple[Path, Path]] = []
    try:
        with db_session() as db:
            db.execute("BEGIN IMMEDIATE")
            site = db.execute("SELECT id FROM sites WHERE code = ?", (normalized,)).fetchone()
            if site is None:
                raise SiteNotFoundError(f"Site {normalized} was not found")
            site_id = int(site["id"])
            if db.execute(
                "SELECT 1 FROM processing_sessions WHERE site_id = ? AND status = 'active'",
                (site_id,),
            ).fetchone():
                raise SiteActiveError(f"Stop the active processing session for {normalized} before deleting it")

            for target in targets:
                if not target.exists() and not target.is_symlink():
                    continue
                temporary = target.with_name(f".{normalized}.deleting-{uuid.uuid4().hex}")
                target.rename(temporary)
                staged.append((target, temporary))

            visits = db.execute(
                """
                SELECT id FROM cow_visits
                WHERE session_id IN (SELECT id FROM processing_sessions WHERE site_id = ?)
                   OR cow_id IN (SELECT id FROM cows WHERE site_id = ?)
                ORDER BY id DESC
                """,
                (site_id, site_id),
            ).fetchall()
            for visit in visits:
                db.execute("DELETE FROM cow_visits WHERE id = ?", (visit["id"],))
            db.execute("DELETE FROM processing_sessions WHERE site_id = ?", (site_id,))
            db.execute("DELETE FROM cows WHERE site_id = ?", (site_id,))
            db.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    except Exception:
        for original, temporary in reversed(staged):
            if temporary.exists() or temporary.is_symlink():
                temporary.rename(original)
        raise

    for _, temporary in staged:
        if temporary.is_symlink() or temporary.is_file():
            temporary.unlink()
        elif temporary.exists():
            shutil.rmtree(temporary)
    return {"siteCode": normalized, "deleted": True}


def get_site(code: str) -> sqlite3.Row | None:
    normalized = normalize_site_code(code)
    with db_session() as db:
        return db.execute("SELECT * FROM sites WHERE code = ?", (normalized,)).fetchone()


def create_site(code: str, cow_count: int) -> dict[str, Any]:
    normalized = normalize_site_code(code)
    if not isinstance(cow_count, int) or isinstance(cow_count, bool) or not 1 <= cow_count <= 10000:
        raise ValueError("Cow count must be an integer between 1 and 10000")

    final_folder = QR_CODES_DIR / normalized
    with db_session() as db:
        if db.execute("SELECT 1 FROM sites WHERE code = ?", (normalized,)).fetchone():
            raise SiteAlreadyExistsError(f"Site {normalized} already exists")
    if final_folder.exists():
        raise SiteAlreadyExistsError(f"Site folder {final_folder} already exists")

    staging_folder = QR_CODES_DIR / f".staging-{uuid.uuid4().hex}"
    created_final_folder = False
    try:
        staging_folder.mkdir(parents=True, exist_ok=False)
        created_at = now_iso()
        generated: list[tuple[int, str, Path]] = []
        for number in range(1, cow_count + 1):
            payload = build_payload(normalized, number)
            path = staging_folder / f"cow_{number:03d}.png"
            write_qr_image(payload, path)
            generated.append((number, payload, path))

        final_folder.mkdir(parents=True, exist_ok=False)
        created_final_folder = True
        final_paths: list[tuple[int, str, Path]] = []
        for number, payload, staged_path in generated:
            final_path = final_folder / staged_path.name
            shutil.move(str(staged_path), str(final_path))
            final_paths.append((number, payload, final_path))

        with db_session() as db:
            cursor = db.execute(
                "INSERT INTO sites(code, created_at, qr_folder) VALUES (?, ?, ?)",
                (normalized, created_at, str(final_folder)),
            )
            site_id = int(cursor.lastrowid)
            for number, payload, final_path in final_paths:
                db.execute(
                    """
                    INSERT INTO cows(site_id, number, payload, qr_path, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (site_id, number, payload, str(final_path), created_at),
                )

        return {
            "siteCode": normalized,
            "cowCount": cow_count,
            "folder": str(final_folder),
            "createdAt": created_at,
        }
    except sqlite3.IntegrityError as exc:
        raise SiteAlreadyExistsError(f"Site {normalized} already exists") from exc
    finally:
        if staging_folder.exists():
            shutil.rmtree(staging_folder, ignore_errors=True)
        if created_final_folder and not (DATABASE_PATH.exists() and get_site(normalized)):
            shutil.rmtree(final_folder, ignore_errors=True)


def start_session(code: str, test_mode: bool = False) -> dict[str, Any]:
    normalized = normalize_site_code(code)
    with db_session() as db:
        site = db.execute("SELECT * FROM sites WHERE code = ?", (normalized,)).fetchone()
        if site is None:
            raise SiteNotFoundError(f"Site {normalized} has not been generated")
        db.execute(
            "UPDATE processing_sessions SET status = 'abandoned', ended_at = ? WHERE status = 'active'",
            (now_iso(),),
        )
        cursor = db.execute(
            "INSERT INTO processing_sessions(site_id, started_at, status, test_mode) VALUES (?, ?, 'active', ?)",
            (site["id"], now_iso(), 1 if test_mode else 0),
        )
        return {
            "id": int(cursor.lastrowid),
            "siteCode": normalized,
            "siteId": int(site["id"]),
            "status": "active",
            "testMode": bool(test_mode),
        }


def get_active_session() -> sqlite3.Row | None:
    with db_session() as db:
        return db.execute(
            """
            SELECT sessions.*, sites.code AS site_code
            FROM processing_sessions AS sessions
            JOIN sites ON sites.id = sessions.site_id
            WHERE sessions.status = 'active'
            ORDER BY sessions.id DESC
            LIMIT 1
            """
        ).fetchone()


def stop_active_session(status: str = "completed") -> None:
    if status not in {"completed", "abandoned"}:
        raise ValueError("Invalid session stop status")
    with db_session() as db:
        db.execute(
            "UPDATE processing_sessions SET status = ?, ended_at = ? WHERE status = 'active'",
            (status, now_iso()),
        )
        db.execute(
            "UPDATE cow_visits SET status = 'abandoned' WHERE status = 'active' AND session_id IN (SELECT id FROM processing_sessions WHERE status = ?)",
            (status,),
        )


def pause_active_session() -> bool:
    with db_session() as db:
        cursor = db.execute(
            "UPDATE processing_sessions SET paused = 1 WHERE status = 'active' AND paused = 0"
        )
        return cursor.rowcount > 0


def resume_active_session() -> bool:
    with db_session() as db:
        cursor = db.execute(
            "UPDATE processing_sessions SET paused = 0 WHERE status = 'active' AND paused = 1"
        )
        return cursor.rowcount > 0


def get_cow_by_payload(payload: str) -> sqlite3.Row | None:
    with db_session() as db:
        return db.execute(
            """
            SELECT cows.*, sites.code AS site_code
            FROM cows
            JOIN sites ON sites.id = cows.site_id
            WHERE cows.payload = ?
            """,
            (payload,),
        ).fetchone()


def get_current_visit(session_id: int, cow_id: int) -> sqlite3.Row | None:
    with db_session() as db:
        return db.execute(
            """
            SELECT * FROM cow_visits
            WHERE session_id = ? AND cow_id = ? AND status IN ('active', 'completed')
            ORDER BY id DESC LIMIT 1
            """,
            (session_id, cow_id),
        ).fetchone()


def create_active_visit(session_id: int, cow_id: int, timestamp: float, detection_count: int = 1) -> dict[str, Any]:
    with db_session() as db:
        cursor = db.execute(
            """
            INSERT INTO cow_visits(session_id, cow_id, first_seen_at, last_seen_at, status, detection_count)
            VALUES (?, ?, ?, ?, 'active', ?)
            """,
            (session_id, cow_id, timestamp, timestamp, detection_count),
        )
        return {"id": int(cursor.lastrowid), "status": "active"}


def touch_visit(visit_id: int, timestamp: float, detection_count: int) -> None:
    with db_session() as db:
        db.execute(
            "UPDATE cow_visits SET last_seen_at = ?, detection_count = ? WHERE id = ? AND status = 'active'",
            (timestamp, detection_count, visit_id),
        )


def complete_visit(visit_id: int, timestamp: float) -> None:
    with db_session() as db:
        db.execute(
            "UPDATE cow_visits SET last_seen_at = ?, completed_at = ?, status = 'completed' WHERE id = ? AND status = 'active'",
            (timestamp, timestamp, visit_id),
        )


def set_visit_capture_status(visit_id: int | None, capture_status: str) -> None:
    if visit_id is None:
        return
    allowed = {"pending", "scheduled", "capturing", "completed", "failed", "skipped"}
    if capture_status not in allowed:
        raise ValueError("Invalid visit capture status")
    with db_session() as db:
        db.execute(
            "UPDATE cow_visits SET capture_status = ? WHERE id = ?",
            (capture_status, visit_id),
        )


def override_visit(visit_id: int, timestamp: float) -> dict[str, Any]:
    with db_session() as db:
        visit = db.execute(
            """
            SELECT cow_visits.*, cows.payload
            FROM cow_visits
            JOIN cows ON cows.id = cow_visits.cow_id
            WHERE cow_visits.id = ?
            """,
            (visit_id,),
        ).fetchone()
        if visit is None:
            raise ValueError("Visit not found")
        if visit["status"] not in {"active", "completed"}:
            raise ValueError("This visit cannot be overridden")
        db.execute("UPDATE cow_visits SET status = 'overridden' WHERE id = ?", (visit_id,))
        cursor = db.execute(
            """
            INSERT INTO cow_visits(session_id, cow_id, first_seen_at, last_seen_at, status, detection_count, override_of)
            VALUES (?, ?, ?, ?, 'active', 1, ?)
            """,
            (visit["session_id"], visit["cow_id"], timestamp, timestamp, visit_id),
        )
        return {
            "id": int(cursor.lastrowid),
            "sessionId": int(visit["session_id"]),
            "cowId": int(visit["cow_id"]),
            "payload": visit["payload"],
            "status": "active",
        }


def session_summary(session_id: int | None) -> list[dict[str, Any]]:
    if session_id is None:
        return []
    with db_session() as db:
        rows = db.execute(
            """
            SELECT visits.id, visits.status, visits.capture_status,
                   visits.first_seen_at, visits.last_seen_at,
                   visits.completed_at, visits.detection_count, visits.override_of,
                   cows.number AS cow_number, cows.payload, sites.code AS site_code
            FROM cow_visits AS visits
            JOIN cows ON cows.id = visits.cow_id
            JOIN sites ON sites.id = cows.site_id
            WHERE visits.session_id = ?
            ORDER BY visits.id DESC
            LIMIT 50
            """,
            (session_id,),
        ).fetchall()
    return [dict(row) for row in rows]
