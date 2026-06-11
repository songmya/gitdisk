# GitDisk - 基于 GitHub Release Assets 的轻量网盘

GitDisk 是一个把 **GitHub Release Assets** 当作对象存储后端的轻量个人网盘服务。它由本地 SQLite 维护目录、文件索引、分片信息和回收站状态，文件内容实际上传到指定 GitHub 仓库的 Release Assets 中，并通过 GitDisk 服务端代理下载，避免把 GitHub Token 暴露给浏览器或 WebDAV 客户端。

> 适合：个人轻量文件盘、文档同步、小规模备份、临时文件中转。<br>
> 不适合：多人高并发网盘、大规模冷存储、高频写入、公开 CDN、对断点续传/秒传/复杂权限有强需求的生产系统。

---

## 目录

- [核心特性](#核心特性)
- [工作原理](#工作原理)
- [运行要求](#运行要求)
- [准备 GitHub 存储仓库和 Token](#准备-github-存储仓库和-token)
- [快速开始：Docker Compose 推荐](#快速开始docker-compose-推荐)
- [快速开始：本地 Python 运行](#快速开始本地-python-运行)
- [WebUI 使用说明](#webui-使用说明)
- [WebDAV 使用说明](#webdav-使用说明)
- [API 使用说明](#api-使用说明)
- [配置项说明](#配置项说明)
- [大文件与分片机制](#大文件与分片机制)
- [鉴权与反向代理建议](#鉴权与反向代理建议)
- [Docker 镜像与 GitHub Actions](#docker-镜像与-github-actions)
- [数据目录、备份与迁移](#数据目录备份与迁移)
- [常见问题](#常见问题)
- [排障指南](#排障指南)
- [安全注意事项](#安全注意事项)
- [开发说明](#开发说明)

---

## 核心特性

- **GitHub Release Assets 存储**：文件上传到指定仓库的指定 Release 下。
- **本地 SQLite 索引**：目录树、文件名、大小、MIME、SHA256、分片 asset id 等信息保存在本地数据库。
- **WebUI 管理界面**：浏览目录、搜索文件、上传、下载、删除、查看回收站。
- **WebDAV 挂载**：服务挂载在 `/dav`，支持 Finder、Windows 网络位置、RaiDrive、Mountain Duck、rclone、Zotero、Infuse 等客户端。
- **大文件自动分片**：超过阈值的文件会拆成多个 GitHub Release Assets，下载时由服务端顺序拼接。
- **服务端代理下载**：私有仓库文件也由 GitDisk 通过 GitHub API 读取，不向客户端暴露 Token。
- **删除同步远端**：WebUI/API/WebDAV 删除会同步删除 GitHub Release Asset 和本地索引。
- **可选 Bearer Token 鉴权**：通过 `WEB_AUTH_TOKEN` 给上传、下载、删除等接口增加简单鉴权。
- **GitHub 出站代理支持**：可通过 `PROXY` 或标准代理环境变量解决访问 GitHub 不稳定的问题。

---

## 工作原理

GitDisk 由三部分组成：

1. **FastAPI 服务**
   - 提供 WebUI、REST API 和下载代理。
   - 启动时初始化 SQLite 数据库。

2. **GitHub Release Assets 存储驱动**
   - 自动查找或创建 `GITHUB_RELEASE_TAG` 对应的 Release。
   - 上传文件到 Release Assets。
   - 下载时通过 GitHub API 的 asset id 读取二进制内容。
   - 删除时调用 GitHub API 删除 asset。

3. **SQLite 元数据数据库**
   - 保存虚拟目录结构。
   - 保存每个文件对应的 GitHub asset id、asset 名称、SHA256、大小、MIME 类型等。
   - 大文件分片时，主文件记录保存在 `files` 表，分片保存在 `file_chunks` 表。

GitHub Release Assets 本身没有目录概念，GitDisk 的目录是由 SQLite 虚拟出来的。因此：

- **不要只备份 GitHub Release Assets，而忽略 SQLite 数据库**。
- 如果丢失 SQLite，只能手动根据 asset 名称重建一部分信息，原目录结构不一定能恢复。

---

## 运行要求

### Docker 方式

- Docker
- Docker Compose v2
- 一个可访问 GitHub API 的网络环境
- 一个 GitHub 仓库和有权限的 Personal Access Token

### 本地 Python 方式

- Python 3.12+ 推荐
- pip
- 可访问 GitHub API 的网络环境
- 一个 GitHub 仓库和有权限的 Personal Access Token

---

## 准备 GitHub 存储仓库和 Token

### 1. 创建存储仓库

你可以新建一个仓库作为 GitDisk 存储后端，例如：

```text
gitdisk-storage
```

仓库可以是 **私有仓库**，也可以是 **公开仓库**。如果只是个人网盘，建议使用私有仓库。

### 2. 创建 GitHub Personal Access Token

推荐使用 **Fine-grained personal access token**，只授权目标存储仓库，权限尽量最小：

- Repository access：只选择 GitDisk 用来存储文件的仓库
- Contents：Read and write
- Metadata：Read（GitHub 默认需要）

GitDisk 需要这些能力：

- 读取指定 tag 的 Release
- 创建指定 tag 的 Release
- 上传 Release Asset
- 下载私有仓库 Release Asset
- 删除 Release Asset

如果使用 classic PAT：

- 私有仓库通常需要 `repo`
- 公共仓库可尝试 `public_repo`，但更推荐 fine-grained token 精准授权

### 3. 重要提醒

- `GITHUB_TOKEN` 是敏感信息，不要提交到 Git。
- 项目 `.gitignore` 已忽略 `.env`，请把真实 Token 放在 `.env` 中。
- 浏览器/WebDAV 客户端不需要知道 GitHub Token，GitDisk 会在服务端代理 GitHub API。

---

## 快速开始：Docker Compose 推荐

进入项目目录：

```bash
cd /path/to/gitdisk
```

复制配置文件：

```bash
cp .env.example .env
```

编辑 `.env`：

```env
GITHUB_TOKEN=你的_GitHub_PAT
GITHUB_OWNER=你的_GitHub用户名或组织名
GITHUB_REPO=gitdisk-storage
GITHUB_RELEASE_TAG=gitdisk

# 可选：如果服务器访问 GitHub 不稳定，配置代理
PROXY=

# SQLite 数据库路径，Docker 中建议保持默认并把 ./data 挂载出来
DB_PATH=data/gitdisk.sqlite3

# 0 表示不限单文件大小；实际仍受 GitHub、磁盘缓存和网络影响
MAX_FILE_SIZE_MB=0

# GitHub Release Asset 单个文件限制约 2GiB，默认 1900MB 以下比较稳妥
GITHUB_CHUNK_SIZE_MB=1900
GITHUB_SINGLE_UPLOAD_THRESHOLD_MB=1900

UPLOAD_CACHE_DIR=data/cache

# 可选：设置后，上传/下载/删除等接口需要 Authorization: Bearer <token>
WEB_AUTH_TOKEN=

LOG_LEVEL=INFO
```

启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f gitdisk
```

打开 WebUI：

```text
http://服务器IP:8090
```

WebDAV 地址：

```text
http://服务器IP:8090/dav/
```

停止服务：

```bash
docker compose down
```

升级镜像/代码后重建：

```bash
docker compose up -d --build
```

---

## 快速开始：本地 Python 运行

进入项目目录：

```bash
cd /path/to/gitdisk
```

创建虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
```

安装依赖：

```bash
pip install -r requirements.txt
```

复制并编辑配置：

```bash
cp .env.example .env
nano .env
```

可选：提前验证/创建 GitHub Release：

```bash
python scripts/init_storage.py
```

启动服务：

```bash
uvicorn app:app --host 0.0.0.0 --port 8090
```

打开：

```text
http://127.0.0.1:8090
```

---

## WebUI 使用说明

访问首页：

```text
http://服务器IP:8090/
```

WebUI 主要功能：

- 查看文件数量、总容量、目录数量
- 新建目录
- 进入目录/返回上级目录
- 搜索文件名或标签
- 多文件上传
- 下载文件
- 删除文件
- 查看文件详情
- 查看回收站入口
- 显示 WebDAV 地址

### Bearer Token 输入框

如果 `.env` 中设置了：

```env
WEB_AUTH_TOKEN=某个随机长字符串
```

那么 WebUI 上传、下载、删除等请求需要带：

```http
Authorization: Bearer 某个随机长字符串
```

页面右上方的 `Bearer Token` 输入框就是给这种场景使用的。填入 Token 后再上传/下载/删除即可。

如果 `WEB_AUTH_TOKEN` 为空，则不启用这个鉴权。此时强烈建议只在可信内网使用，或放在带登录保护的反向代理后面。

---

## WebDAV 使用说明

WebDAV 挂载地址：

```text
http://服务器IP:8090/dav/
```

如果本机测试：

```text
http://127.0.0.1:8090/dav/
```

### curl 示例

查看根目录：

```bash
curl -X PROPFIND http://127.0.0.1:8090/dav/ -H 'Depth: 1'
```

创建目录：

```bash
curl -X MKCOL http://127.0.0.1:8090/dav/books
```

上传文件：

```bash
curl -T ./example.pdf http://127.0.0.1:8090/dav/books/example.pdf
```

下载文件：

```bash
curl -o example.pdf http://127.0.0.1:8090/dav/books/example.pdf
```

删除文件：

```bash
curl -X DELETE http://127.0.0.1:8090/dav/books/example.pdf
```

删除空目录：

```bash
curl -X DELETE http://127.0.0.1:8090/dav/books
```

### rclone 挂载示例

配置一个 WebDAV remote：

```bash
rclone config
```

选择：

```text
Storage: webdav
url: http://服务器IP:8090/dav/
vendor: other
```

列目录：

```bash
rclone lsd gitdisk:
rclone ls gitdisk:
```

复制文件：

```bash
rclone copy ./docs gitdisk:/docs -P
```

### WebDAV 行为说明

- WebDAV 上传同名文件时，GitDisk 会先删除旧文件对应的 GitHub asset 和本地索引，再写入新文件，实现常见客户端期望的覆盖语义。
- WebDAV 支持创建目录和删除空目录。
- 一些 WebDAV 客户端会在 `MKCOL` 前先对目标目录发 `PROPFIND`。GitDisk 对“看起来像目录”的缺失路径做了兼容处理，可提升新建文件夹成功率。
- 当前 WebDAV 下载使用前向流式读取，不声明 Range 支持，避免大文件下载时为了 seek 造成缓冲压力。

---

## API 使用说明

### 鉴权

如果设置了 `WEB_AUTH_TOKEN`，下列涉及写入/下载/删除的接口需要请求头：

```http
Authorization: Bearer <WEB_AUTH_TOKEN>
```

例如：

```bash
curl -H "Authorization: Bearer $WEB_AUTH_TOKEN" http://127.0.0.1:8090/api/files
```

当前 `GET /api/stats` 和 `GET /api/files` 不强制鉴权；上传、下载、删除、目录创建、回收站接口会走鉴权依赖。

### 获取统计信息

```http
GET /api/stats
```

示例：

```bash
curl http://127.0.0.1:8090/api/stats
```

返回示例：

```json
{
  "file_count": 12,
  "total_size": 1048576,
  "total_size_fmt": "1.0 MB",
  "dir_count": 3,
  "storage": "github_release_assets"
}
```

### 列出文件和目录

```http
GET /api/files?path=/&search=&page=1&limit=50
```

示例：

```bash
curl 'http://127.0.0.1:8090/api/files?path=/books&page=1&limit=50'
```

参数：

| 参数 | 说明 |
|---|---|
| `path` | 当前目录，默认 `/` |
| `search` | 搜索关键字；非空时搜索文件名和 tags |
| `page` | 页码，默认 `1` |
| `limit` | 每页数量，默认 `50` |

### 创建目录

```http
POST /api/dirs?path=/books
```

示例：

```bash
curl -X POST \
  -H "Authorization: Bearer $WEB_AUTH_TOKEN" \
  'http://127.0.0.1:8090/api/dirs?path=/books'
```

说明：父目录必须存在。创建已存在目录会返回成功。

### 上传文件

```http
POST /api/upload?path=/books
Content-Type: multipart/form-data
```

字段名必须是 `files`，支持多文件：

```bash
curl -X POST \
  -H "Authorization: Bearer $WEB_AUTH_TOKEN" \
  -F 'files=@./a.pdf' \
  -F 'files=@./b.epub' \
  'http://127.0.0.1:8090/api/upload?path=/books'
```

### 下载文件

```http
GET /api/download/{file_id}
```

示例：

```bash
curl -L \
  -H "Authorization: Bearer $WEB_AUTH_TOKEN" \
  -o output.bin \
  'http://127.0.0.1:8090/api/download/1'
```

单 asset 文件支持 HTTP Range：

```bash
curl -H "Range: bytes=0-1023" \
  -H "Authorization: Bearer $WEB_AUTH_TOKEN" \
  'http://127.0.0.1:8090/api/download/1' -o part.bin
```

注意：分片文件暂不支持 Range 下载，请完整下载。

### 删除文件

```http
DELETE /api/files/{file_id}
```

示例：

```bash
curl -X DELETE \
  -H "Authorization: Bearer $WEB_AUTH_TOKEN" \
  'http://127.0.0.1:8090/api/files/1'
```

当前删除语义是：**立即删除 GitHub asset，并删除本地索引**。这不是仅标记删除。

### 回收站接口

项目保留了回收站相关接口，供后续软删除模式使用：

```http
GET /api/trash
POST /api/trash/{file_id}/restore
DELETE /api/trash/{file_id}
```

但当前 WebUI/API 常规删除会直接删除远端 asset 和索引，所以一般不会产生可恢复的回收站文件，除非未来改为 soft delete 或手动调用数据库层软删除。

---

## 配置项说明

`.env.example` 中包含全部常用配置：

| 变量 | 默认值 | 必填 | 说明 |
|---|---:|:---:|---|
| `GITHUB_TOKEN` | 无 | 是 | GitHub Personal Access Token。用于创建 Release、上传/下载/删除 asset。 |
| `GITHUB_OWNER` | 无 | 是 | 存储仓库所属用户或组织。 |
| `GITHUB_REPO` | `gitdisk-storage` 示例 | 是 | 存储仓库名。 |
| `GITHUB_RELEASE_TAG` | `gitdisk` | 否 | 用作存储容器的 Release tag。不存在时会自动创建。 |
| `PROXY` | 空 | 否 | GitHub API/upload/download 出站代理，例如 `http://user:pass@host:port`。 |
| `DB_PATH` | `data/gitdisk.sqlite3` | 否 | SQLite 数据库路径。Docker 中建议保持在 `/app/data` 下。 |
| `MAX_FILE_SIZE_MB` | `0` | 否 | 单文件大小限制，`0` 表示不限制。 |
| `GITHUB_CHUNK_SIZE_MB` | `1900` | 否 | 分片大小，单位 MB。建议低于 GitHub 单 asset 约 2GiB 限制。 |
| `GITHUB_SINGLE_UPLOAD_THRESHOLD_MB` | `1900` | 否 | 超过该大小时启用分片上传，单位 MB。 |
| `UPLOAD_CACHE_DIR` | `data/cache` | 否 | 上传和分片临时缓存目录。 |
| `WEB_AUTH_TOKEN` | 空 | 否 | 可选 Bearer Token。为空则不启用 API 鉴权。 |
| `LOG_LEVEL` | `INFO` | 否 | Python 日志级别，如 `DEBUG`、`INFO`、`WARNING`。 |
| `GITHUB_API_BASE` | `https://api.github.com` | 否 | GitHub API 地址；通常无需修改，企业版 GitHub 可按需设置。 |

### 代理环境变量

如果 `PROXY` 为空，程序还会依次读取：

- `HTTPS_PROXY`
- `HTTP_PROXY`
- `ALL_PROXY`
- `https_proxy`
- `http_proxy`
- `all_proxy`

---

## 大文件与分片机制

GitHub Release Asset 单文件大小限制约为 2GiB。GitDisk 默认：

```env
GITHUB_CHUNK_SIZE_MB=1900
GITHUB_SINGLE_UPLOAD_THRESHOLD_MB=1900
```

含义：

- 小于等于 1900MB：作为单个 asset 上传。
- 大于 1900MB：拆成多个分片 asset 上传。

分片 asset 命名大致类似：

```text
<file_id>-<原文件名>.part00000-of00003
<file_id>-<原文件名>.part00001-of00003
<file_id>-<原文件名>.part00002-of00003
```

SQLite 会记录每个分片的：

- 分片序号
- GitHub asset id
- asset 名称
- 分片大小
- 分片 SHA256

下载分片文件时，GitDisk 会按顺序从 GitHub 读取每个分片并拼接输出。

### 临时缓存

上传时会先落到 `UPLOAD_CACHE_DIR`，然后再上传到 GitHub。分片上传时也会在该目录创建临时分片文件。

因此请确保：

- `UPLOAD_CACHE_DIR` 所在磁盘有足够空间。
- Docker 部署时 `./data:/app/data` 已持久化。
- 大文件上传期间不要清理 `data/cache`。

---

## 鉴权与反向代理建议

`WEB_AUTH_TOKEN` 是一个简单的 Bearer Token 保护，不是完整用户系统。

### 内网自用

如果只在可信内网使用，可以：

```env
WEB_AUTH_TOKEN=
```

然后通过内网 IP 访问。

### 暴露到公网

如果要公网访问，建议至少：

1. 设置高强度随机 `WEB_AUTH_TOKEN`。
2. 放在 Nginx/Caddy/Traefik 等反向代理后。
3. 启用 HTTPS。
4. 在反向代理层增加 Basic Auth、OAuth、Authelia、Cloudflare Access 等更完整的访问控制。
5. 限制上传大小和请求超时。

Nginx 反代示例：

```nginx
server {
    listen 443 ssl http2;
    server_name gitdisk.example.com;

    client_max_body_size 0;

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

---

## Docker 镜像与 GitHub Actions

项目包含 GitHub Actions 工作流：

```text
.github/workflows/docker.yml
```

触发条件：

- push 到 `master`
- push 到 `main`
- push tag，格式 `v*.*.*`
- 手动 `workflow_dispatch`

工作流会构建并推送 Docker Hub 镜像，标签包括：

- `latest`（默认分支）
- 分支名
- Git tag
- `sha-<commit>`

当前项目已配置过的 Docker Hub 镜像示例：

```text
songmy94/gitdisk:latest
```

如果你 fork 后想发布自己的镜像，需要在 GitHub 仓库 Secrets 中配置：

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`

然后把 workflow 中的镜像名保持为：

```yaml
images: ${{ secrets.DOCKERHUB_USERNAME }}/gitdisk
```

---

## 数据目录、备份与迁移

默认 Docker Compose 挂载：

```yaml
volumes:
  - ./data:/app/data
```

重要文件：

```text
data/gitdisk.sqlite3      # SQLite 主数据库
data/gitdisk.sqlite3-wal  # WAL 文件，服务运行中可能存在
data/gitdisk.sqlite3-shm  # 共享内存文件，服务运行中可能存在
data/cache/               # 上传临时缓存
```

### 备份建议

至少备份：

- `.env`（注意安全保存，里面有 Token）
- `data/gitdisk.sqlite3`
- 如果服务运行中使用 WAL 模式，也要一起备份 `-wal` 和 `-shm`，或者先停止服务再备份数据库文件

停止服务后备份：

```bash
docker compose down
tar czf gitdisk-backup-$(date +%F).tar.gz .env data/gitdisk.sqlite3*
docker compose up -d
```

### 迁移步骤

1. 在新机器复制项目或镜像。
2. 复制 `.env`。
3. 复制 `data/gitdisk.sqlite3*`。
4. 启动服务。
5. 确认 `GITHUB_OWNER`、`GITHUB_REPO`、`GITHUB_RELEASE_TAG` 指向原存储仓库和 Release。

---

## 常见问题

### 1. GitDisk 会把文件提交到 Git 仓库吗？

不会。文件内容上传到 **GitHub Release Assets**，不是 git commit。

### 2. Release Assets 有目录吗？

没有。目录结构由本地 SQLite 维护。

### 3. 私有仓库可以用吗？

可以。GitDisk 服务端使用 `GITHUB_TOKEN` 通过 GitHub API 读写私有仓库的 Release Assets，客户端只连接 GitDisk。

### 4. Token 会不会暴露给浏览器？

不会。GitHub Token 只在服务端 `.env` 中使用。浏览器看到的是 GitDisk 的 API。

### 5. 删除文件后 GitHub 上也会删吗？

会。当前删除是同步删除 GitHub asset 和本地索引。

### 6. 为什么 README 里有回收站接口，但删除后看不到可恢复文件？

当前常规删除是硬删除。回收站接口是为后续软删除模式保留的，普通删除不会产生可恢复记录。

### 7. 分片文件支持拖动进度条播放视频吗？

目前不适合。单 asset 文件的 API 下载支持 Range；分片文件暂不支持 Range。WebDAV 为了保持前向流式读取，也不声明 Range 支持。

### 8. 上传大文件失败怎么办？

检查：

- GitHub Token 权限是否足够
- 网络到 GitHub 是否稳定
- `PROXY` 是否需要配置
- `UPLOAD_CACHE_DIR` 磁盘空间是否足够
- 反向代理是否限制了上传大小或超时
- GitHub API rate limit 是否触发

### 9. 能不能多个 GitDisk 实例共用同一个 SQLite？

不建议。SQLite 更适合单实例本地使用。多实例并发写入可能带来锁和一致性问题。

### 10. 能不能直接在 GitHub Release 页面手动删除 asset？

不建议。手动删除会导致 SQLite 中仍保留索引，下载时找不到远端 asset。应通过 GitDisk 删除。

---

## 排障指南

### 启动时报 `GITHUB_TOKEN 未设置`

检查 `.env` 是否存在，且服务是否正确加载：

```bash
cat .env
```

Docker Compose 中确认：

```yaml
env_file: .env
```

### 上传时报 401/403

通常是 Token 权限问题：

- Token 是否过期？
- Fine-grained Token 是否授权了正确仓库？
- 是否有 Contents Read and write 权限？
- 仓库 owner/repo 是否写错？

### 上传时报 404

可能是：

- `GITHUB_OWNER` 或 `GITHUB_REPO` 写错
- Token 没有访问该私有仓库的权限
- 使用组织仓库时，Token 未获组织授权

### 访问 GitHub 超时

配置代理：

```env
PROXY=http://user:pass@host:port
```

或通过环境变量：

```bash
export HTTPS_PROXY=http://user:pass@host:port
export HTTP_PROXY=http://user:pass@host:port
```

### WebDAV 客户端无法新建文件夹

GitDisk 已兼容部分客户端的 `PROPFIND -> MKCOL` 行为。如果仍失败：

- 确认父目录存在。
- 尽量不要创建带点号的目录名，例如 `a.b`，某些兼容逻辑会把它判断为文件路径。
- 换用标准 WebDAV 客户端或 curl 验证：

```bash
curl -X MKCOL http://127.0.0.1:8090/dav/test
```

### WebDAV 上传同名文件后出现异常

WebDAV 同名上传会删除旧文件再写入新文件。如果删除旧 GitHub asset 失败，覆盖会中止。检查日志中 GitHub API 删除 asset 的错误信息。

### 数据库和 GitHub Release Assets 不一致

可能原因：

- 手动删除了 Release Asset
- 上传过程中服务被强制停止
- 数据库文件丢失或回滚

处理方式：

- 如果是单个文件，建议通过 GitDisk 删除残留索引，或手动清理数据库和 Release Assets。
- 如果大量不一致，先备份数据库，再根据 Release Assets 和 `files/file_chunks` 表核对。

---

## 安全注意事项

- 不要提交 `.env`。
- 不要把 GitHub Token 写进 README、issue、日志截图或前端代码。
- 公开部署时不要只依赖“地址没人知道”。
- `WEB_AUTH_TOKEN` 只是简单共享密钥，不是多用户权限系统。
- 如果使用反向代理，请启用 HTTPS。
- 如果存储敏感文件，优先使用私有 GitHub 仓库，并考虑上传前本地加密。
- GitHub Release Assets 不是专门的对象存储服务，请遵守 GitHub 服务条款和使用限制。

---

## 开发说明

### 项目结构

```text
.
├── app.py                    # FastAPI 入口、WebUI/API、挂载 WebDAV
├── config.py                 # 环境变量配置
├── database.py               # SQLite 初始化和数据访问层
├── github_io.py              # GitHub Release Assets 驱动
├── storage_service.py        # 上传、分片、删除、下载拼接的共享服务
├── webdav_app.py             # WsgiDAV 适配层
├── scripts/
│   └── init_storage.py       # 创建/验证 GitHub Release
├── www/
│   └── index.html            # 单页 WebUI
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .github/workflows/docker.yml
```

### 本地检查

语法检查：

```bash
python3 -m py_compile app.py config.py database.py github_io.py storage_service.py webdav_app.py scripts/init_storage.py
```

启动开发服务：

```bash
uvicorn app:app --reload --host 0.0.0.0 --port 8090
```

查看 Git 状态：

```bash
git status --short
```

---

## License

当前仓库未显式声明 License。使用、分发或二次开发前请先确认项目所有者的授权意图。
