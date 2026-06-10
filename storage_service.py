"""Shared file storage service for API and WebDAV."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import config
from database import get_db, FileDB
from github_io import GitHubReleaseAssets, sha256_file, safe_asset_name


async def store_local_file(*, local_path: str | Path, file_name: str, mime_type: str, dest_path: str,
                           overwrite: bool = False) -> dict:
    """Store a local file as one GitHub asset or multipart assets and add DB rows.

    When overwrite=True, any existing active file(s) with the same path/name are
    removed from GitHub and the local index before the new file is indexed. This
    gives WebDAV clients normal replace semantics for PUT on an existing path.
    """
    path = Path(local_path)
    size = path.stat().st_size
    storage = GitHubReleaseAssets()
    threshold = max(1, config.GITHUB_SINGLE_UPLOAD_THRESHOLD_MB) * 1024 * 1024
    chunk_size = max(1, config.GITHUB_CHUNK_SIZE_MB) * 1024 * 1024

    if overwrite:
        db = await get_db()
        try:
            existing_files = await FileDB(db).find_all_by_path_name(dest_path, file_name)
        finally:
            await db.close()
        for existing in existing_files:
            ok, errors = await delete_file_and_assets(existing["id"])
            if errors:
                raise RuntimeError("同名文件覆盖失败，删除旧文件失败：" + "; ".join(errors))

    if size <= threshold:
        meta = await storage.upload_file(path, file_name, mime_type)
        db = await get_db()
        try:
            fdb = FileDB(db)
            file_id = await fdb.add_file(
                file_name=file_name,
                file_size=meta["file_size"],
                mime_type=meta["mime_type"],
                path=dest_path,
                sha256=meta["sha256"],
                asset_id=meta["asset_id"],
                asset_name=meta["asset_name"],
                browser_download_url=meta["browser_download_url"],
            )
        finally:
            await db.close()
        return {"ok": True, "mode": "single", "file_id": file_id, "file_name": file_name, "path": dest_path, **meta}

    total_sha = await sha256_file(path)
    chunk_count = (size + chunk_size - 1) // chunk_size
    db = await get_db()
    try:
        fdb = FileDB(db)
        file_id = await fdb.add_file(
            file_name=file_name,
            file_size=size,
            mime_type=mime_type,
            path=dest_path,
            sha256=total_sha,
            asset_id=0,
            asset_name="",
            browser_download_url="",
            is_multipart=1,
            chunk_count=chunk_count,
        )
    finally:
        await db.close()

    uploaded_assets: list[int] = []
    base = safe_asset_name(file_name)
    buffer_size = 8 * 1024 * 1024
    try:
        with path.open("rb") as src:
            for idx in range(chunk_count):
                part_name = f"{file_id}-{base}.part{idx:05d}-of-{chunk_count:05d}"
                remaining = min(chunk_size, size - idx * chunk_size)
                h = hashlib.sha256()
                written = 0
                with tempfile.NamedTemporaryFile(dir=config.UPLOAD_CACHE_DIR, delete=True) as part:
                    while remaining > 0:
                        data = src.read(min(buffer_size, remaining))
                        if not data:
                            break
                        part.write(data)
                        h.update(data)
                        written += len(data)
                        remaining -= len(data)
                    part.flush()
                    meta = await storage.upload_bytes_or_file(
                        data_source=part.name,
                        asset_name=part_name,
                        content_type="application/octet-stream",
                        size=written,
                        digest=h.hexdigest(),
                    )
                uploaded_assets.append(meta["asset_id"])
                db = await get_db()
                try:
                    await FileDB(db).add_chunk(
                        file_id_int=file_id,
                        chunk_index=idx,
                        asset_id=meta["asset_id"],
                        asset_name=meta["asset_name"],
                        chunk_size=meta["file_size"],
                        chunk_sha256=meta["sha256"],
                        browser_download_url=meta["browser_download_url"],
                    )
                finally:
                    await db.close()
    except Exception:
        for asset_id in uploaded_assets:
            try:
                await storage.delete_asset(asset_id)
            except Exception:
                pass
        db = await get_db()
        try:
            await FileDB(db).delete_index(file_id)
        finally:
            await db.close()
        raise

    return {
        "ok": True,
        "mode": "multipart",
        "file_id": file_id,
        "file_name": file_name,
        "path": dest_path,
        "file_size": size,
        "sha256": total_sha,
        "chunk_count": chunk_count,
        "chunk_size": chunk_size,
        "mime_type": mime_type,
    }


async def delete_file_and_assets(file_id: int, include_deleted: bool = False) -> tuple[bool, list[str]]:
    db = await get_db()
    try:
        fdb = FileDB(db)
        f = await fdb.get_file(file_id, include_deleted=include_deleted)
        if not f:
            return False, ["文件不存在"]
        chunks = await fdb.list_chunks(file_id) if f.get("is_multipart") else []
    finally:
        await db.close()

    errors = []
    storage = GitHubReleaseAssets()
    asset_ids = [int(c["asset_id"]) for c in chunks]
    if not f.get("is_multipart") and f.get("asset_id"):
        asset_ids.append(int(f["asset_id"]))
    for asset_id in asset_ids:
        try:
            await storage.delete_asset(asset_id)
        except Exception as e:
            errors.append(f"asset {asset_id}: {e}")
    if errors:
        return False, errors

    db = await get_db()
    try:
        ok = await FileDB(db).delete_index(file_id)
    finally:
        await db.close()
    return ok, []


async def iter_file_bytes(file_id: int, range_header: str | None = None):
    db = await get_db()
    try:
        fdb = FileDB(db)
        f = await fdb.get_file(file_id)
        if not f:
            raise FileNotFoundError("文件不存在")
        chunks = await fdb.list_chunks(file_id) if f.get("is_multipart") else []
    finally:
        await db.close()

    storage = GitHubReleaseAssets()
    if f.get("is_multipart"):
        if range_header:
            raise ValueError("分片文件暂不支持 Range 下载，请完整下载")
        for part in chunks:
            async for chunk in storage.stream_asset(part["asset_id"]):
                yield chunk
    else:
        async for chunk in storage.stream_asset(f["asset_id"], range_header=range_header):
            yield chunk
