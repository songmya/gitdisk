"""WsgiDAV bridge for GitDisk."""

from __future__ import annotations

import asyncio
import io
import mimetypes
import tempfile
from pathlib import PurePosixPath

import aiosqlite
from wsgidav.dav_provider import DAVProvider, DAVCollection, DAVNonCollection
from wsgidav.wsgidav_app import WsgiDAVApp

from database import get_db, FileDB, DirDB, normalize_path
from github_io import GitHubReleaseAssets


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def parent_path(path: str) -> str:
    p = normalize_path(path)
    if p == "/":
        return "/"
    parent = str(PurePosixPath(p).parent)
    return normalize_path(parent)


def base_name(path: str) -> str:
    return PurePosixPath(normalize_path(path)).name


class GitDiskFile(DAVNonCollection):
    def __init__(self, path, environ, file_info):
        super().__init__(path, environ)
        self.file_info = file_info

    def get_content_length(self):
        return self.file_info["file_size"]

    def get_content_type(self):
        return self.file_info["mime_type"] or "application/octet-stream"

    def support_etag(self):
        return True

    def get_etag(self):
        return f'{self.file_info["id"]}-{self.file_info["sha256"][:12]}'

    def support_ranges(self):
        return not bool(self.file_info.get("is_multipart"))

    def get_content(self):
        env = self.environ or {}
        range_header = env.get("HTTP_RANGE") or env.get("http_range")
        data = run_async(self._download_all(range_header))
        return io.BytesIO(data)

    async def _download_all(self, range_header=None) -> bytes:
        storage = GitHubReleaseAssets()
        chunks_out = []
        if self.file_info.get("is_multipart"):
            db = await get_db()
            try:
                parts = await FileDB(db).list_chunks(self.file_info["id"])
            finally:
                await db.close()
            for part in parts:
                async for chunk in storage.stream_asset(part["asset_id"]):
                    chunks_out.append(chunk)
        else:
            async for chunk in storage.stream_asset(self.file_info["asset_id"], range_header=range_header):
                chunks_out.append(chunk)
        return b"".join(chunks_out)

    def delete(self):
        async def do_delete():
            db = await get_db()
            try:
                fdb = FileDB(db)
                parts = await fdb.list_chunks(self.file_info["id"]) if self.file_info.get("is_multipart") else []
            finally:
                await db.close()
            storage = GitHubReleaseAssets()
            asset_ids = [int(p["asset_id"]) for p in parts]
            if not self.file_info.get("is_multipart") and self.file_info.get("asset_id"):
                asset_ids.append(int(self.file_info["asset_id"]))
            for asset_id in asset_ids:
                await storage.delete_asset(asset_id)
            db = await get_db()
            try:
                await FileDB(db).delete_index(self.file_info["id"])
            finally:
                await db.close()
        run_async(do_delete())


class UploadBuffer(io.BytesIO):
    def __init__(self, dav_path: str):
        super().__init__()
        self.dav_path = normalize_path(dav_path)
        self.closed_once = False

    def close(self):
        if self.closed_once:
            return super().close()
        self.closed_once = True
        data = self.getvalue()
        run_async(self._upload(data))
        return super().close()

    async def _upload(self, data: bytes):
        name = base_name(self.dav_path)
        dest = parent_path(self.dav_path)
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        from app import _upload_one

        class _Upload:
            def __init__(self, file, filename, content_type):
                self.file = file
                self.filename = filename
                self.content_type = content_type

        with tempfile.NamedTemporaryFile(delete=True) as tmp:
            tmp.write(data)
            tmp.flush()
            tmp.seek(0)
            await _upload_one(_Upload(tmp, name, mime_type), dest)


class GitDiskCollection(DAVCollection):
    def __init__(self, path, environ):
        super().__init__(path, environ)

    def get_member_names(self):
        return run_async(self._member_names())

    async def _member_names(self):
        dav_path = normalize_path(self.path)
        db = await get_db()
        try:
            ddb = DirDB(db)
            fdb = FileDB(db)
            dirs = await ddb.list_dirs(dav_path)
            files = await fdb.list_files(dav_path, limit=10000)
            return [d["name"] for d in dirs] + [f["file_name"] for f in files]
        finally:
            await db.close()

    def get_member(self, name):
        child_path = normalize_path(f"{self.path.rstrip('/')}/{name}")
        info = run_async(self._lookup(child_path))
        if info["kind"] == "dir":
            return GitDiskCollection(child_path, self.environ)
        if info["kind"] == "file":
            return GitDiskFile(child_path, self.environ, info["file"])
        return None

    async def _lookup(self, child_path):
        db = await get_db()
        try:
            ddb = DirDB(db)
            if await ddb.dir_exists(child_path):
                return {"kind": "dir"}
            f = await FileDB(db).find_by_path_name(parent_path(child_path), base_name(child_path))
            if f:
                return {"kind": "file", "file": f}
            return {"kind": "none"}
        finally:
            await db.close()

    def create_collection(self, name):
        path = normalize_path(f"{self.path.rstrip('/')}/{name}")
        async def create():
            db = await get_db()
            try:
                result = await DirDB(db).create_dir(path)
                if not result:
                    raise RuntimeError("父目录不存在")
            finally:
                await db.close()
        run_async(create())

    def create_empty_resource(self, name):
        path = normalize_path(f"{self.path.rstrip('/')}/{name}")
        return UploadBuffer(path)


class GitDiskProvider(DAVProvider):
    def get_resource_inst(self, path, environ):
        path = normalize_path(path)
        if path == "/":
            return GitDiskCollection(path, environ)
        info = run_async(self._lookup(path))
        if info["kind"] == "dir":
            return GitDiskCollection(path, environ)
        if info["kind"] == "file":
            return GitDiskFile(path, environ, info["file"])
        return None

    async def _lookup(self, path):
        db = await get_db()
        try:
            ddb = DirDB(db)
            if await ddb.dir_exists(path):
                return {"kind": "dir"}
            f = await FileDB(db).find_by_path_name(parent_path(path), base_name(path))
            if f:
                return {"kind": "file", "file": f}
            return {"kind": "none"}
        finally:
            await db.close()


def create_webdav_app():
    config = {
        "provider_mapping": {"/": GitDiskProvider()},
        "simple_dc": {"user_mapping": {"*": True}},
        "http_authenticator": {"domain_controller": None},
        "verbose": 1,
    }
    return WsgiDAVApp(config)
