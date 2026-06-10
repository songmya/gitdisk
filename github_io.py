"""GitHub Release Assets storage driver."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

import config


class GitHubStorageError(RuntimeError):
    pass


def safe_asset_name(file_name: str) -> str:
    name = file_name.replace("/", "_").replace("\\", "_").strip() or "upload.bin"
    name = re.sub(r"\s+", " ", name)
    return name


def unique_asset_name(file_name: str) -> str:
    stem = safe_asset_name(file_name)
    ts = time.strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha1(f"{stem}-{time.time_ns()}".encode()).hexdigest()[:8]
    return f"{ts}-{digest}-{stem}"


def guess_mime(path: str, fallback: str = "application/octet-stream") -> str:
    return mimetypes.guess_type(path)[0] or fallback


async def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class GitHubReleaseAssets:
    def __init__(self) -> None:
        config.validate_storage_config()
        self.api = config.GITHUB_API_BASE
        self.owner = config.GITHUB_OWNER
        self.repo = config.GITHUB_REPO
        self.tag = config.GITHUB_RELEASE_TAG
        self.headers = {
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "GitDisk/0.1",
        }

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.request(method, url, **kwargs) as resp:
                if resp.status == 204:
                    return {"ok": True}
                try:
                    data = await resp.json()
                except Exception:
                    data = {"text": await resp.text()}
                if resp.status >= 400:
                    msg = data.get("message") or data.get("text") or f"HTTP {resp.status}"
                    raise GitHubStorageError(f"GitHub API {method} {url} failed: {resp.status} {msg}")
                return data

    async def ensure_release(self) -> dict:
        release_url = f"{self.api}/repos/{self.owner}/{self.repo}/releases/tags/{quote(self.tag)}"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(release_url) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status != 404:
                    text = await resp.text()
                    raise GitHubStorageError(f"获取 release 失败: {resp.status} {text[:200]}")

        create_url = f"{self.api}/repos/{self.owner}/{self.repo}/releases"
        payload = {
            "tag_name": self.tag,
            "name": "GitDisk Storage",
            "body": "Storage release managed by GitDisk.",
            "draft": False,
            "prerelease": False,
        }
        return await self._request("POST", create_url, json=payload)

    async def upload_file(self, local_path: str | Path, file_name: str, mime_type: str | None = None) -> dict:
        release = await self.ensure_release()
        upload_url = release["upload_url"].split("{")[0]
        asset_name = unique_asset_name(file_name)
        path = Path(local_path)
        content_type = mime_type or guess_mime(file_name)
        size = path.stat().st_size
        digest = await sha256_file(path)

        url = f"{upload_url}?name={quote(asset_name)}"
        headers = dict(self.headers)
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(size)
        async with aiohttp.ClientSession(headers=headers) as session:
            with open(path, "rb") as f:
                async with session.post(url, data=f) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {"text": await resp.text()}
                    if resp.status >= 400:
                        msg = data.get("message") or data.get("text") or f"HTTP {resp.status}"
                        raise GitHubStorageError(f"上传 asset 失败: {resp.status} {msg}")
        return {
            "asset_id": int(data["id"]),
            "asset_name": data.get("name", asset_name),
            "file_size": int(data.get("size") or size),
            "browser_download_url": data.get("browser_download_url", ""),
            "sha256": digest,
            "mime_type": content_type,
        }

    async def delete_asset(self, asset_id: int) -> dict:
        url = f"{self.api}/repos/{self.owner}/{self.repo}/releases/assets/{asset_id}"
        return await self._request("DELETE", url)

    async def stream_asset(self, asset_id: int, range_header: str | None = None):
        """Yield bytes from an asset through GitHub API.

        Uses Accept: application/octet-stream so private repo downloads stay server-side.
        """
        url = f"{self.api}/repos/{self.owner}/{self.repo}/releases/assets/{asset_id}"
        headers = dict(self.headers)
        headers["Accept"] = "application/octet-stream"
        if range_header:
            headers["Range"] = range_header
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status not in (200, 206):
                    text = await resp.text()
                    raise GitHubStorageError(f"下载 asset 失败: {resp.status} {text[:200]}")
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    if chunk:
                        yield chunk
