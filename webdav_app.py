"""WsgiDAV bridge for GitDisk."""

from __future__ import annotations

import asyncio
import io
import mimetypes
import tempfile
from pathlib import PurePosixPath

from wsgidav.dav_provider import DAVProvider, DAVCollection, DAVNonCollection
from wsgidav.wsgidav_app import WsgiDAVApp

from database import get_db, FileDB, DirDB, normalize_path
from github_io import GitHubReleaseAssets
from storage_service import store_local_file, delete_file_and_assets, iter_file_bytes


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


def _looks_like_dir_path(path: str) -> bool:
    """Heuristic for WebDAV clients that PROPFIND a folder before MKCOL.

    Avoid auto-creating obvious file paths like /movie.mp4 or /a/b.txt.
    Folder names with dots are still possible, but users can create those through
    clients that send MKCOL directly or the WebUI/API.
    """
    name = base_name(path)
    return bool(name) and "." not in name


class _AsyncIteratorReader(io.RawIOBase):
    """Expose an async byte iterator as a sync readable file object for WsgiDAV."""

    def __init__(self, async_iter):
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self._aiter = async_iter.__aiter__()
        self._buf = b""
        self._done = False

    def readable(self):
        return True

    def seekable(self):
        return False

    def tell(self):
        return 0

    def seek(self, offset, whence=io.SEEK_SET):
        if offset == 0 and whence == io.SEEK_SET:
            return 0
        raise OSError("seek not supported")

    def _fill(self, want: int) -> None:
        while not self._done and (want < 0 or len(self._buf) < want):
            try:
                chunk = self._loop.run_until_complete(self._aiter.__anext__())
                if chunk:
                    self._buf += chunk
            except StopAsyncIteration:
                self._done = True
                break

    def read(self, n=-1):
        if n is None:
            n = -1
        self._fill(n)
        if n < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)

    def close(self):
        try:
            self._loop.run_until_complete(self._aiter.aclose())
        except Exception:
            pass
        try:
            self._loop.close()
        except Exception:
            pass
        super().close()


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
        # WsgiDAV may call seek() for ranged responses. GitDisk WebDAV serves a
        # forward-only stream so it works for large multipart files without
        # buffering. API downloads still support Range for single-asset files.
        return False

    def get_content(self):
        return _AsyncIteratorReader(iter_file_bytes(self.file_info["id"], range_header=None))

    def begin_write(self, content_type=None):
        return UploadSink(self.path, content_type=content_type)

    def end_write(self, with_errors: bool):
        pass

    def delete(self):
        ok, errors = run_async(delete_file_and_assets(self.file_info["id"]))
        if errors:
            raise RuntimeError("; ".join(errors))
        if not ok:
            raise RuntimeError("删除失败")


class UploadSink(io.RawIOBase):
    """File-like sink returned by DAV resource begin_write()."""

    def __init__(self, dav_path: str, content_type: str | None = None):
        super().__init__()
        self.dav_path = normalize_path(dav_path)
        self.content_type = content_type
        self._tmp = tempfile.NamedTemporaryFile(delete=True)
        self._uploaded = False

    def writable(self):
        return True

    def write(self, data: bytes):
        return self._tmp.write(data)

    def close(self):
        if self._uploaded:
            return super().close()
        self._uploaded = True
        try:
            self._tmp.flush()
            name = base_name(self.dav_path)
            dest = parent_path(self.dav_path)
            mime_type = self.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
            run_async(store_local_file(
                local_path=self._tmp.name,
                file_name=name,
                mime_type=mime_type,
                dest_path=dest,
                overwrite=True,
            ))
        finally:
            try:
                self._tmp.close()
            finally:
                super().close()


class GitDiskCollection(DAVCollection):
    def __init__(self, path, environ):
        super().__init__(path, environ)

    def delete(self):
        async def do_delete():
            db = await get_db()
            try:
                ok = await DirDB(db).delete_dir(normalize_path(self.path))
                if not ok:
                    raise RuntimeError("目录不存在或不能删除")
            finally:
                await db.close()
        run_async(do_delete())

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
        return GitDiskFile(path, self.environ, {
            "id": 0,
            "file_name": name,
            "file_size": 0,
            "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "sha256": "",
            "is_multipart": 0,
            "asset_id": 0,
        })


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

        # Compatibility: some WebDAV clients check whether a directory exists by
        # issuing PROPFIND on the target path and treat a standards-compliant 404
        # as a hard failure before sending MKCOL. If the missing path looks like a
        # directory target, create it lazily on PROPFIND so "New Folder" works.
        method = (environ or {}).get("REQUEST_METHOD", "").upper()
        if method == "PROPFIND" and _looks_like_dir_path(path):
            created = run_async(self._create_dir_if_parent_exists(path))
            if created:
                return GitDiskCollection(path, environ)
        return None

    async def _create_dir_if_parent_exists(self, path: str) -> bool:
        db = await get_db()
        try:
            ddb = DirDB(db)
            parent = parent_path(path)
            if parent != "/" and not await ddb.dir_exists(parent):
                return False
            return bool(await ddb.create_dir(path))
        finally:
            await db.close()

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
