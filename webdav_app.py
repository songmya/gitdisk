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
from github_io import GitHubReleaseAssets, sha256_file


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
        return True

    def get_content(self):
        env = self.environ or {}
        range_header = env.get("HTTP_RANGE") or env.get("http_range")
        data = run_async(self._download_all(range_header))
        return io.BytesIO(data)

    async def _download_all(self, range_header=None) -> bytes:
        chunks = []
        async for chunk in GitHubReleaseAssets().stream_asset(self.file_info["asset_id"], range_header=range_header):
            chunks.append(chunk)
        return b"".join(chunks)

    def delete(self):
        async def do_delete():
            db = await get_db()
            try:
                await FileDB(db).soft_delete(self.file_info["id"], deleted_by="webdav")
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
        with tempfile.NamedTemporaryFile(delete=True) as tmp:
            tmp.write(data)
            tmp.flush()
            meta = await GitHubReleaseAssets().upload_file(tmp.name, name, mime_type)
        db = await get_db()
        try:
            ddb = DirDB(db)
            if dest != "/" and not await ddb.dir_exists(dest):
                # WebDAV clients often create parent collections first, but be forgiving for root only.
                raise RuntimeError(f"目录不存在：{dest}")
            await FileDB(db).add_file(
                file_name=name,
                file_size=meta["file_size"],
                mime_type=meta["mime_type"],
                path=dest,
                sha256=meta["sha256"],
                asset_id=meta["asset_id"],
                asset_name=meta["asset_name"],
                browser_download_url=meta["browser_download_url"],
            )
        finally:
            await db.close()


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
