"""SQLite database layer for GitDisk."""

from __future__ import annotations

from pathlib import Path
import aiosqlite

from config import DB_PATH


async def get_db() -> aiosqlite.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    db = await get_db()
    try:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                mime_type TEXT DEFAULT 'application/octet-stream',
                path TEXT DEFAULT '/',
                tags TEXT DEFAULT '',
                sha256 TEXT DEFAULT '',
                storage_kind TEXT DEFAULT 'github_release_asset',
                asset_id INTEGER NOT NULL,
                asset_name TEXT NOT NULL,
                browser_download_url TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                deleted INTEGER DEFAULT 0,
                deleted_at TEXT DEFAULT '',
                deleted_by TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS dirs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                parent_path TEXT DEFAULT '/',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
            CREATE INDEX IF NOT EXISTS idx_files_name ON files(file_name);
            CREATE INDEX IF NOT EXISTS idx_files_deleted ON files(deleted);
            CREATE INDEX IF NOT EXISTS idx_dirs_path ON dirs(path);
            CREATE INDEX IF NOT EXISTS idx_dirs_parent ON dirs(parent_path);
            """
        )
        await db.commit()
    finally:
        await db.close()


def normalize_path(path: str | None) -> str:
    if not path:
        return "/"
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    return "/" + "/".join(parts) if parts else "/"


class FileDB:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def add_file(self, *, file_name: str, file_size: int, mime_type: str, path: str,
                       sha256: str, asset_id: int, asset_name: str,
                       browser_download_url: str = "", tags: str = "") -> int:
        await self.db.execute(
            """INSERT INTO files
               (file_name, file_size, mime_type, path, tags, sha256, asset_id, asset_name, browser_download_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (file_name, file_size, mime_type, normalize_path(path), tags, sha256,
             asset_id, asset_name, browser_download_url),
        )
        await self.db.commit()
        cursor = await self.db.execute("SELECT last_insert_rowid()")
        row = await cursor.fetchone()
        return int(row[0])

    async def get_file(self, file_id: int, include_deleted: bool = False) -> dict | None:
        sql = "SELECT * FROM files WHERE id=?" if include_deleted else "SELECT * FROM files WHERE id=? AND deleted=0"
        cursor = await self.db.execute(sql, (file_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def find_by_path_name(self, path: str, file_name: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT * FROM files WHERE path=? AND file_name=? AND deleted=0 ORDER BY id DESC LIMIT 1",
            (normalize_path(path), file_name),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_files(self, path: str = "/", search: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
        if search:
            cursor = await self.db.execute(
                """SELECT * FROM files WHERE deleted=0 AND (file_name LIKE ? OR tags LIKE ?)
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (f"%{search}%", f"%{search}%", limit, offset),
            )
        else:
            cursor = await self.db.execute(
                """SELECT * FROM files WHERE deleted=0 AND path=?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (normalize_path(path), limit, offset),
            )
        return [dict(r) for r in await cursor.fetchall()]

    async def count_files(self, path: str = "/", search: str = "") -> int:
        if search:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM files WHERE deleted=0 AND (file_name LIKE ? OR tags LIKE ?)",
                (f"%{search}%", f"%{search}%"),
            )
        else:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM files WHERE deleted=0 AND path=?", (normalize_path(path),)
            )
        row = await cursor.fetchone()
        return int(row[0])

    async def soft_delete(self, file_id: int, deleted_by: str = "api") -> bool:
        cursor = await self.db.execute(
            """UPDATE files SET deleted=1, deleted_at=datetime('now','localtime'), deleted_by=?,
               updated_at=datetime('now','localtime') WHERE id=? AND deleted=0""",
            (deleted_by, file_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def restore(self, file_id: int) -> bool:
        cursor = await self.db.execute(
            """UPDATE files SET deleted=0, deleted_at='', deleted_by='', updated_at=datetime('now','localtime')
               WHERE id=? AND deleted=1""",
            (file_id,),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def purge(self, file_id: int) -> bool:
        cursor = await self.db.execute("DELETE FROM files WHERE id=? AND deleted=1", (file_id,))
        await self.db.commit()
        return cursor.rowcount > 0

    async def list_deleted(self, limit: int = 50, offset: int = 0) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT * FROM files WHERE deleted=1
               ORDER BY COALESCE(NULLIF(deleted_at,''), created_at) DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def stats(self) -> dict:
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(file_size),0) AS total_size FROM files WHERE deleted=0"
        )
        row = await cursor.fetchone()
        return dict(row)


class DirDB:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def create_dir(self, path: str) -> str | None:
        path = normalize_path(path)
        if path == "/":
            return "/"
        parts = path.strip("/").split("/")
        parent = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        parent = normalize_path(parent)
        if parent != "/" and not await self.dir_exists(parent):
            return None
        try:
            await self.db.execute(
                "INSERT INTO dirs (name, path, parent_path) VALUES (?, ?, ?)",
                (parts[-1], path, parent),
            )
            await self.db.commit()
            return path
        except aiosqlite.IntegrityError:
            return path

    async def dir_exists(self, path: str) -> bool:
        path = normalize_path(path)
        if path == "/":
            return True
        cursor = await self.db.execute("SELECT 1 FROM dirs WHERE path=?", (path,))
        return await cursor.fetchone() is not None

    async def list_dirs(self, parent_path: str = "/") -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM dirs WHERE parent_path=? ORDER BY name", (normalize_path(parent_path),)
        )
        return [dict(r) for r in await cursor.fetchall()]
