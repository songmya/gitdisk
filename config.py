"""GitDisk configuration."""

from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_RELEASE_TAG = os.getenv("GITHUB_RELEASE_TAG", "gitdisk").strip() or "gitdisk"

DB_PATH = os.getenv("DB_PATH", "data/gitdisk.sqlite3")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "0"))
UPLOAD_CACHE_DIR = os.getenv("UPLOAD_CACHE_DIR", "data/cache")
WEB_AUTH_TOKEN = os.getenv("WEB_AUTH_TOKEN", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Optional outbound proxy for GitHub API/upload/download requests.
# Supports http://, https://, and socks proxies if aiohttp has the required extras.
PROXY = (
    os.getenv("PROXY", "").strip()
    or os.getenv("HTTPS_PROXY", "").strip()
    or os.getenv("HTTP_PROXY", "").strip()
    or os.getenv("ALL_PROXY", "").strip()
    or os.getenv("https_proxy", "").strip()
    or os.getenv("http_proxy", "").strip()
    or os.getenv("all_proxy", "").strip()
)

GITHUB_API_BASE = os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/")


def validate_storage_config() -> None:
    errors: list[str] = []
    if not GITHUB_TOKEN:
        errors.append("GITHUB_TOKEN 未设置")
    if not GITHUB_OWNER:
        errors.append("GITHUB_OWNER 未设置")
    if not GITHUB_REPO:
        errors.append("GITHUB_REPO 未设置")
    if errors:
        raise RuntimeError("配置错误：\n" + "\n".join(f"  - {e}" for e in errors))
