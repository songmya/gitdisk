"""GitDisk FastAPI app."""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Query, HTTPException, Request, Depends
from typing import Annotated
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.wsgi import WSGIMiddleware

import config
from database import init_db, get_db, FileDB, DirDB, normalize_path
from github_io import GitHubReleaseAssets, GitHubStorageError
from storage_service import store_local_file, delete_file_and_assets, iter_file_bytes

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO))
logger = logging.getLogger("gitdisk")

app = FastAPI(title="GitDisk")
WWW = Path(__file__).parent / "www"
WWW.mkdir(exist_ok=True)
Path(config.UPLOAD_CACHE_DIR).mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
async def startup() -> None:
    await init_db()


def format_size(size: int) -> str:
    n = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


async def require_auth(request: Request) -> None:
    if not config.WEB_AUTH_TOKEN:
        return
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {config.WEB_AUTH_TOKEN}":
        raise HTTPException(401, "Unauthorized")


@app.get("/", response_class=HTMLResponse)
async def index():
    idx = WWW / "index.html"
    return HTMLResponse(idx.read_text(encoding="utf-8"))


@app.get("/api/stats")
async def api_stats():
    db = await get_db()
    try:
        fdb = FileDB(db)
        stats = await fdb.stats()
        cursor = await db.execute("SELECT COUNT(*) FROM dirs")
        dir_count = (await cursor.fetchone())[0]
        return {
            "file_count": stats["count"],
            "total_size": stats["total_size"],
            "total_size_fmt": format_size(stats["total_size"]),
            "dir_count": dir_count,
            "storage": "github_release_assets",
        }
    finally:
        await db.close()


@app.get("/api/files")
async def api_files(path: str = Query("/"), search: str = Query(""), page: int = 1, limit: int = 50):
    db = await get_db()
    try:
        fdb = FileDB(db)
        ddb = DirDB(db)
        offset = max(0, page - 1) * limit
        files = await fdb.list_files(path=path, search=search, limit=limit, offset=offset)
        for f in files:
            f["size_fmt"] = format_size(f["file_size"])
        dirs = [] if search else await ddb.list_dirs(path)
        total = await fdb.count_files(path=path, search=search)
        return {"files": files, "dirs": dirs, "total": total, "page": page, "limit": limit}
    finally:
        await db.close()


@app.post("/api/dirs", dependencies=[Depends(require_auth)])
async def api_create_dir(path: str = Query(...)):
    db = await get_db()
    try:
        ddb = DirDB(db)
        result = await ddb.create_dir(path)
        if not result:
            raise HTTPException(400, "父目录不存在")
        return {"ok": True, "path": result}
    finally:
        await db.close()


async def _upload_one(file: UploadFile, dest_path: str) -> dict:
    filename = file.filename or "upload.bin"
    mime_type = file.content_type or "application/octet-stream"
    suffix = Path(filename).suffix
    tmp_path = Path(config.UPLOAD_CACHE_DIR) / f"{uuid.uuid4().hex}{suffix}.upload"
    try:
        with tmp_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        size = tmp_path.stat().st_size
        if config.MAX_FILE_SIZE_MB > 0 and size > config.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise HTTPException(413, f"文件超过 MAX_FILE_SIZE_MB={config.MAX_FILE_SIZE_MB}MB：{filename}")

        return await store_local_file(
            local_path=tmp_path,
            file_name=filename,
            mime_type=mime_type,
            dest_path=dest_path,
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.post("/api/upload", dependencies=[Depends(require_auth)])
async def api_upload(files: Annotated[list[UploadFile], File(alias="files")], path: str = Query("/")):
    dest_path = normalize_path(path)

    db = await get_db()
    try:
        ddb = DirDB(db)
        if dest_path != "/" and not await ddb.dir_exists(dest_path):
            raise HTTPException(400, f"目录不存在：{dest_path}")
    finally:
        await db.close()

    results = []
    for file in files:
        try:
            results.append(await _upload_one(file, dest_path))
        except GitHubStorageError as e:
            raise HTTPException(502, str(e))
    return {"ok": True, "count": len(results), "files": results}


@app.get("/api/download/{file_id}", dependencies=[Depends(require_auth)])
async def api_download(file_id: int, request: Request):
    db = await get_db()
    try:
        fdb = FileDB(db)
        f = await fdb.get_file(file_id)
        if not f:
            raise HTTPException(404, "文件不存在")
    finally:
        await db.close()

    range_header = request.headers.get("range")
    if f.get("is_multipart") and range_header:
        raise HTTPException(416, "分片文件暂不支持 Range 下载，请完整下载")

    async def body():
        async for chunk in iter_file_bytes(file_id, range_header=range_header):
            yield chunk

    headers = {
        "Content-Disposition": f'attachment; filename="{f["file_name"]}"',
    }
    if not f.get("is_multipart"):
        headers["Accept-Ranges"] = "bytes"
    status_code = 206 if range_header and not f.get("is_multipart") else 200
    return StreamingResponse(body(), media_type=f["mime_type"] or "application/octet-stream", headers=headers, status_code=status_code)


@app.delete("/api/files/{file_id}", dependencies=[Depends(require_auth)])
async def api_delete(file_id: int):
    """Delete the GitHub asset and local index immediately."""
    db = await get_db()
    try:
        fdb = FileDB(db)
        f = await fdb.get_file(file_id)
        if not f:
            raise HTTPException(404, "文件不存在")
    finally:
        await db.close()

    ok, errors = await delete_file_and_assets(file_id)
    if errors:
        raise HTTPException(502, "GitHub asset 删除失败：" + "; ".join(errors))
    return {"ok": ok, "message": "已从 GitHub 和本地索引删除"}


@app.get("/api/trash", dependencies=[Depends(require_auth)])
async def api_trash(limit: int = 50, offset: int = 0):
    db = await get_db()
    try:
        fdb = FileDB(db)
        files = await fdb.list_deleted(limit=limit, offset=offset)
        for f in files:
            f["size_fmt"] = format_size(f["file_size"])
        return {"files": files}
    finally:
        await db.close()


@app.post("/api/trash/{file_id}/restore", dependencies=[Depends(require_auth)])
async def api_restore(file_id: int):
    db = await get_db()
    try:
        fdb = FileDB(db)
        ok = await fdb.restore(file_id)
        if not ok:
            raise HTTPException(404, "回收站文件不存在")
        return {"ok": True}
    finally:
        await db.close()


@app.delete("/api/trash/{file_id}", dependencies=[Depends(require_auth)])
async def api_purge(file_id: int):
    db = await get_db()
    try:
        fdb = FileDB(db)
        f = await fdb.get_file(file_id, include_deleted=True)
        if not f or not f.get("deleted"):
            raise HTTPException(404, "回收站文件不存在")
    finally:
        await db.close()

    ok, errors = await delete_file_and_assets(file_id, include_deleted=True)
    return {"ok": ok, "asset_delete_errors": errors}


# WebDAV mounted under /dav.
try:
    from webdav_app import create_webdav_app
    app.mount("/dav", WSGIMiddleware(create_webdav_app()))
except Exception as e:
    logger.warning("WebDAV mount disabled: %s", e)
