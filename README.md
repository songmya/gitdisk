# GitDisk - GitHub Release Assets 网盘

GitDisk 是一个受 TGDrive 启发的轻量网盘服务：

- 文件内容存储在 GitHub Release Assets
- 本地 SQLite 保存目录、文件索引、标签和回收站状态
- 提供 Web API / 简易 WebUI / WebDAV

> 适合个人轻量文件盘、文档同步、小规模备份。不建议用于大规模网盘、高频写入、超大文件或公开 CDN。

## 功能

- 📤 上传文件到 GitHub Release Assets，大文件自动分片
- 📥 代理下载文件，不向浏览器暴露 GitHub Token
- 📁 目录索引由 SQLite 管理
- 🔍 文件列表和搜索
- 🗑️ WebUI/API 删除会同步删除 GitHub asset 和本地索引
- ♻️ 保留回收站 API，用于后续软删除模式
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

### 私有仓库支持与 Token 权限

支持私有仓库。GitDisk 的上传、下载、删除都由服务端通过 `GITHUB_TOKEN` 调 GitHub API 完成，浏览器和 WebDAV 客户端不会直接接触 GitHub Token。

推荐使用 Fine-grained personal access token，并只授权目标仓库：

- Repository access：只选择用于存储的那个仓库
- Contents：Read and write
- Metadata：Read（GitHub 默认需要）

如果使用 classic PAT：

- 私有仓库：通常需要 `repo`
- 公共仓库：可用 `public_repo`，但如果要写 Release assets，仍建议用 fine-grained token 精准授权

GitDisk 需要这些能力：

- 读取/创建指定 tag 的 Release
- 上传 Release Asset
- 下载私有仓库 Release Asset
- 删除 Release Asset（彻底删除回收站文件时）

### 代理配置

如果机器访问 GitHub 不稳定，可以在 `.env` 中配置：

```env
PROXY=http://user:pass@host:port
```

也会读取常见环境变量：`HTTPS_PROXY`、`HTTP_PROXY`、`ALL_PROXY` 及小写形式。

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

### WebDAV 使用

WebDAV 挂载地址：

```text
http://<服务器IP>:8090/dav
```

示例：

```bash
# 查看根目录
curl -X PROPFIND http://127.0.0.1:8090/dav/ -H 'Depth: 1'

# 创建目录
curl -X MKCOL http://127.0.0.1:8090/dav/books

# 上传文件
curl -T ./example.pdf http://127.0.0.1:8090/dav/books/example.pdf

# 下载文件
curl -o example.pdf http://127.0.0.1:8090/dav/books/example.pdf

# 删除文件：会同步删除 GitHub Release Asset 和本地索引
curl -X DELETE http://127.0.0.1:8090/dav/books/example.pdf
```

也可以在支持 WebDAV 的客户端里挂载该地址，例如 macOS Finder、Windows 网络位置、RaiDrive、Mountain Duck、rclone 等。

当前 WebDAV 侧和 WebUI/API 一样支持：

- 大文件分片上传
- 分片文件顺序拼接下载
- 删除时同步删除所有 GitHub assets 和本地索引

注意：分片文件暂不支持 Range/seek 下载；普通单 asset 文件支持 Range。

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
| `PROXY` | 空 | 可选 GitHub 出站代理 |
| `DB_PATH` | `data/gitdisk.sqlite3` | SQLite 路径 |
| `MAX_FILE_SIZE_MB` | `0` | 单文件限制；0 表示不限 |
| `GITHUB_CHUNK_SIZE_MB` | `1900` | GitHub Release Asset 分片大小，单位 MB |
| `GITHUB_SINGLE_UPLOAD_THRESHOLD_MB` | `1900` | 超过该大小自动分片，单位 MB |
| `UPLOAD_CACHE_DIR` | `data/cache` | 上传临时缓存目录 |
| `WEB_AUTH_TOKEN` | 空 | 可选 API Bearer Token；为空则不启用鉴权 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## API 摘要

- `GET /api/stats`
- `GET /api/files?path=/&search=`
- `POST /api/dirs?path=/books`
- `POST /api/upload?path=/books` multipart form `files`（可多文件）
- `GET /api/download/{file_id}`
- `DELETE /api/files/{file_id}` 同步删除 GitHub asset + 本地索引
- `GET /api/trash`
- `POST /api/trash/{file_id}/restore`
- `DELETE /api/trash/{file_id}` 彻底删除 asset + 索引

## 注意事项

- GitHub API 有 rate limit，不适合高频大量上传。
- GitHub Release Asset 单文件限制约 2GiB；GitDisk 默认超过 1900MB 自动拆成多个 asset。
- 分片文件下载会由服务端顺序拼接；当前暂不支持分片文件的 HTTP Range 下载。
- Release Assets 不提供真正目录，目录由 SQLite 维护。
- 如果 `.env` 中配置了 Token，不要提交 `.env`。
- 私有仓库下载必须经本服务代理，避免 token 泄漏。
