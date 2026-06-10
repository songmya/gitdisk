# GitDisk - GitHub Release Assets 网盘

GitDisk 是一个受 TGDrive 启发的轻量网盘服务：

- 文件内容存储在 GitHub Release Assets
- 本地 SQLite 保存目录、文件索引、标签和回收站状态
- 提供 Web API / 简易 WebUI / WebDAV

> 适合个人轻量文件盘、文档同步、小规模备份。不建议用于大规模网盘、高频写入、超大文件或公开 CDN。

## 功能

- 📤 上传文件到 GitHub Release Assets
- 📥 代理下载文件，不向浏览器暴露 GitHub Token
- 📁 目录索引由 SQLite 管理
- 🔍 文件列表和搜索
- 🗑️ 删除进入回收站
- ♻️ 恢复 / 彻底删除索引和 GitHub asset
- 🌐 WebDAV：挂载在 `/dav`

## 快速开始

### 1. 配置

```bash
cp .env.example .env
```

填入：

```env
GITHUB_TOKEN=ghp_xxx
GITHUB_OWNER=yourname
GITHUB_REPO=gitdisk-storage
GITHUB_RELEASE_TAG=gitdisk
```

Token 建议只授予目标私有仓库需要的最小权限。

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 运行

```bash
uvicorn app:app --host 0.0.0.0 --port 8090
```

打开：

```text
http://127.0.0.1:8090
```

WebDAV：

```text
http://127.0.0.1:8090/dav
```

### 4. Docker

```bash
docker compose up -d --build
```

## 配置项

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `GITHUB_TOKEN` | 必填 | GitHub PAT |
| `GITHUB_OWNER` | 必填 | 仓库 owner |
| `GITHUB_REPO` | 必填 | 仓库名 |
| `GITHUB_RELEASE_TAG` | `gitdisk` | 存储用 Release tag |
| `DB_PATH` | `data/gitdisk.sqlite3` | SQLite 路径 |
| `MAX_FILE_SIZE_MB` | `0` | 单文件限制；0 表示不限 |
| `UPLOAD_CACHE_DIR` | `data/cache` | 上传临时缓存目录 |
| `WEB_AUTH_TOKEN` | 空 | 可选 API Bearer Token；为空则不启用鉴权 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## API 摘要

- `GET /api/stats`
- `GET /api/files?path=/&search=`
- `POST /api/dirs?path=/books`
- `POST /api/upload?path=/books` multipart form `file`
- `GET /api/download/{file_id}`
- `DELETE /api/files/{file_id}` 软删除
- `GET /api/trash`
- `POST /api/trash/{file_id}/restore`
- `DELETE /api/trash/{file_id}` 彻底删除 asset + 索引

## 注意事项

- GitHub API 有 rate limit，不适合高频大量上传。
- Release Assets 不提供真正目录，目录由 SQLite 维护。
- 如果 `.env` 中配置了 Token，不要提交 `.env`。
- 私有仓库下载必须经本服务代理，避免 token 泄漏。
