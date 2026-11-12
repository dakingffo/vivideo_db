# 部署指南

[项目概要](../README.md) · [开发指南](dev.md) · [架构说明](desgin/architecture.md)

## 运行条件

- Python 3.11+；`environment.yml` 使用 Python 3.12。
- Linux/macOS；Windows 使用 WSL，当前不支持原生 Windows 服务进程管理。
- 本地视频模式需要 FFmpeg（包含 ffprobe）及 Node 22+。
- 模型 API 需要兼容 Chat Completions 的工具调用格式；搜索和首次下载需要访问相应网络服务。

项目不需要 GPU、CUDA 或前端构建工具链。

| 文件 | 作用 |
|---|---|
| `pyproject.toml` | Python 版本约束、包元数据、命令入口及打包配置 |
| `requirements.txt` | Python 依赖清单，由 `pyproject.toml` 读取 |
| `environment.yml` | Conda 环境：Python、媒体工具、Node，以及项目的普通安装 |
| `dist/` | 可选构建产物；复制完整源码并用 Conda 安装时不需要 |

## 方式一：Conda 安装完整源码

推荐已有 Conda 的用户使用。将完整源码复制到目标机器，在项目根目录执行：

```bash
conda env create -f environment.yml
conda activate vivideo
vivideo doctor --data-dir ./data
vivideo serve --data-dir ./data
```

环境文件安装 Python、FFmpeg/ffprobe、Node 和 pip，最后执行 `pip install .`，安装项目及 Python 依赖。需要从项目根目录运行，因为 `.` 指向当前工作目录。环境创建后无需另外安装媒体工具或指定 `--tools-dir`。

如果同名环境已存在，可以激活已有环境，或创建不同名称的新环境：

```bash
conda env create -n vivideo-new -f environment.yml
conda activate vivideo-new
```

以后启动只需激活环境、回到项目根目录并运行：

```bash
conda activate vivideo
vivideo serve --data-dir ./data
```

`conda activate` 不会重新安装项目。需要修改代码并自动重载时，按 [开发指南](dev.md#可编辑安装与普通安装)切换为可编辑安装，不必修改环境文件。

## 方式二：普通 Python 虚拟环境

先通过目标机器的包管理器准备 FFmpeg、ffprobe、Node 22+，并加入 PATH，再安装项目：

```bash
cd /path/to/vivideo
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
vivideo doctor --data-dir ./data
vivideo serve --data-dir ./data
```

已有媒体工具但未加入 PATH 时，给 `doctor` 和 `serve` 指定同一个工具目录：

```bash
vivideo doctor --data-dir ./data --tools-dir /path/to/media-tools/bin
vivideo serve --data-dir ./data --tools-dir /path/to/media-tools/bin
```

`vivideo` 不在 PATH 时可使用 `python -m vivideo`，参数相同。Python 的 `requires-python` 只检查版本兼容性，pip 不会安装 Python 解释器或这些系统程序。

## 方式三：安装 wheel

适用于只分发安装包、不复制源码的情况。构建方法见 [开发指南](dev.md#构建与分发)。

把 `dist/vivideo-0.1.0-py3-none-any.whl` 复制到目标机器的工作目录，例如 `~/vivideo/`。先建立包含媒体工具的环境：

```bash
conda create -n vivideo -c conda-forge python=3.12 ffmpeg "nodejs>=22" -y
conda activate vivideo
cd ~/vivideo
python -m pip install ./vivideo-0.1.0-py3-none-any.whl
vivideo doctor --data-dir ./data
vivideo serve --data-dir ./data
```

这里假设 wheel 已复制到 `~/vivideo/`。在原项目根目录安装现成产物时，文件路径是 `./dist/vivideo-0.1.0-py3-none-any.whl`。

wheel 包含 Python 模块和前端 HTML/CSS/JS，不包含 Python 解释器、外部工具、数据库、模型密钥或已下载视频。目标机器可以改用 venv，但仍需自行提供媒体工具。wheel 安装后不需要设置 PYTHONPATH，也不需要保留源码。

## 环境检查、启动与停止

`vivideo doctor` 检查工具是否可找到、数据目录是否可写，并显示 Python 与应用版本。输出中 `missing` 为空且 `data_writable` 为 `true` 表示这些基础检查正常；它不验证模型 API、搜索或来源平台的实际可用性，也不会启动服务或下载视频。

`vivideo serve` 同时启动 FastAPI、对话 Worker 和下载 Worker。浏览器打开 http://127.0.0.1:8000，API 文档位于 http://127.0.0.1:8000/docs。

Ctrl+C 或 SIGTERM 会统一停止子服务；普通模式中任一子服务异常退出，其余服务也会停止。普通 `serve` 不监视代码变化，自动重载只用于 [开发模式](dev.md#自动重载与前端刷新)。

常用启动选项：

```bash
vivideo serve --port 8001 --data-dir ./data
vivideo serve --mode demo --data-dir ./data
vivideo serve --no-local-video --data-dir ./data
```

- `--mode demo`：只启动网关，查看示例界面。
- `--no-local-video`：使用旧版链接/嵌入播放器，不启动下载 Worker。
- 本地模式不会在下载失败时静默退回远端播放器，真实模型出错也不会切回示例回复。

## 模型 API 与搜索配置

在页面左上角齿轮中添加模型配置，填写名称、Base URL、模型 ID 和 API Key，再为当前空间选择配置。Base URL 填到 `/v1` 等服务前缀，程序追加 `/chat/completions`。配置列表可复用，空间各自选择模型。

页头数据库状态来自实际 SQLite 查询；模型状态来自当前配置访问 `/models` 的结果及模型列表匹配。检测每 30 秒刷新、模型结果缓存 60 秒，不额外生成测试回复。已连接不代表生成和工具调用已验证；不支持该接口或未列出模型时显示未验证。

默认使用 DDGS 搜索，也可通过环境变量选择服务：

```bash
VIVIDEO_SEARCH_PROVIDER=searxng SEARXNG_URL=http://127.0.0.1:8080 vivideo serve --data-dir ./data
VIVIDEO_SEARCH_PROVIDER=tavily TAVILY_API_KEY=your-search-key vivideo serve --data-dir ./data
```

SearXNG 需要开放 JSON 搜索。环境变量由 shell 或服务管理器传入；项目不自动读取任意 `.env` 文件。

## 数据目录与迁移

数据目录优先级：

1. `--data-dir`，或没有该参数时的 `VIVIDEO_DATA_DIR`。
2. 直接加载源码或可编辑安装：项目根目录的 `data/`。
3. 普通安装（包括 `pip install .` 和 wheel）：用户数据目录，Linux 通常为 `~/.local/share/vivideo`，macOS 为 `~/Library/Application Support/vivideo`。

因此本指南统一显式传入 `--data-dir`。相对路径随当前工作目录变化，长期部署建议使用绝对路径。数据库不写入 site-packages，实际路径可用 `doctor` 查看。

```text
data/
├── settings.db                  # 模型配置与各空间选择
└── spaces/
    ├── learning/
    │   ├── space.db             # 会话、记忆、偏好、视频、任务、观看记录
    │   ├── memory.md
    │   ├── sessions/<uuid>/memory.md
    │   └── media/
    │       ├── staging/<job_id>/<lease_owner>/
    │       └── assets/<asset_id>/
    └── roaming/                 # 独立数据库、媒体和配额
```

Git 仅跟踪 `data/spaces/learning/` 和 `data/spaces/roaming/` 下 `sessions/`、`media/assets/`、`media/staging/` 的空目录占位文件。数据库、模型 API 配置、空间和会话记忆、视频及下载临时文件均不纳入版本管理，运行时按需创建。

迁移前停止旧服务，将整个 `data/` 复制到目标目录，再用 `--data-dir` 指向它。备份包含明文存储的模型 API 密钥；文件权限为 0600。浏览器 LocalStorage 中的收藏、笔记、反馈和草稿不会随服务端数据自动迁移，本地视频保留状态保存在服务端。

三个服务需要共用同一台机器上的数据目录。当前 SQLite 设计面向单机部署，不支持多机共享文件系统同时写入。

## 缓存与运行参数

资料库支持导入链接、查看准备状态与容量、保留、重试、取消和清理。清理后条目从列表移除；保留视频与播放租约有效的视频受到清理保护。播放租约每 30 秒续期，关闭后最多保留 120 秒保护期。

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `VIVIDEO_HOST` / `VIVIDEO_PORT` | `127.0.0.1` / `8000` | 网关监听地址与端口，CLI 参数可覆盖 |
| `VIVIDEO_DATA_DIR` | 取决于安装方式 | 持久化目录，`--data-dir` 可覆盖 |
| `VIVIDEO_ZMQ_ENDPOINT` | `tcp://127.0.0.1:5555` | 网关与对话 Worker 通信地址 |
| `VIVIDEO_MODE` / `VIVIDEO_LOCAL_VIDEO` | `zmq` / `1` | 对话运行模式与本地视频开关 |
| `VIVIDEO_WORKER_CONCURRENCY` | `8` | 对话 Worker 并发 |
| `VIVIDEO_TURN_TIMEOUT` / `VIVIDEO_MAX_TOKENS` | `150` / `2400` | 回合时间预算（秒）与模型输出 token 上限 |
| `VIVIDEO_MEDIA_QUOTA_BYTES` | 20 GiB | 每空间默认缓存容量，页面设置可覆盖 |
| `VIVIDEO_VIDEO_MAX_BYTES` | 300 MiB | 单视频及附件上限 |
| `VIVIDEO_VIDEO_MAX_SECONDS` | `1800` | 单视频最长 30 分钟 |
| `VIVIDEO_PREFETCH_COUNT` | `3` | 每计划先准备的条数，范围 1～5 |
| `VIVIDEO_DOWNLOAD_CONCURRENCY` | `2` | 后台总并发，同来源平台最多 1 个任务 |
| `VIVIDEO_SEARCH_PROVIDER` | `ddgs` | 搜索服务，可选 `searxng` / `tavily` |

环境变量中的容量以字节填写；表中的 GiB/MiB 是便于阅读的表示。清晰度最高 720p，每次检索最多保存 10 个候选。任务按单文件上限的 3 倍预留下载与处理空间，额度不足时等待清理。普通缓存按最近访问时间淘汰。

`VIVIDEO_FFMPEG_BIN`、`VIVIDEO_FFPROBE_BIN`、`VIVIDEO_NODE_BIN` 可分别指定工具路径；`VIVIDEO_JS_RUNTIME` 可指定其他 yt-dlp 支持的运行时。版本条件见 [yt-dlp EJS 文档](https://github.com/yt-dlp/yt-dlp/wiki/EJS)。

## 独立进程与多实例

安装后可分别启动各服务，各终端统一设置数据目录和 ZMQ 地址：

```bash
export VIVIDEO_DATA_DIR=/path/to/vivideo-data
export VIVIDEO_ZMQ_ENDPOINT=tcp://127.0.0.1:5555

vivideo worker
vivideo downloader
uvicorn vivideo.app:app --host 127.0.0.1 --port 8000
```

上述三条启动命令分别在三个终端执行。媒体工具需要在各进程的 PATH 中，或统一设置对应路径变量。

同机启动多个完整实例时，分别指定 HTTP 端口、ZMQ 地址和数据目录：

```bash
vivideo serve --port 8001 --zmq-endpoint tcp://127.0.0.1:5556 --data-dir /path/to/second-instance
```

## 局域网与 systemd

可信局域网访问：

```bash
vivideo serve --host 0.0.0.0 --port 8000 --data-dir /path/to/vivideo-data
```

其他设备访问 `http://服务器局域网IP:8000`。当前无账号隔离和接口鉴权；公网部署需要认证与 HTTPS 反向代理。

下面是 Linux systemd 示例，账号、路径需要替换为目标机器配置。目标环境应已安装 ViVideo，媒体工具需在 PATH 中或通过环境变量指定：

```ini
[Unit]
Description=ViVideo
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=vivideo
WorkingDirectory=/var/lib/vivideo
Environment=VIVIDEO_DATA_DIR=/var/lib/vivideo
Environment=PATH=/opt/vivideo/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/vivideo/.venv/bin/vivideo serve --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=multi-user.target
```

## 常见问题

- **doctor 提示缺少工具，但原来能运行**：先确认激活了正确环境。Conda 安装的工具位于该环境 PATH；旧 `.tools/media/bin` 中的工具可通过 `--tools-dir` 使用。工具存在不代表源码和 wheel 使用相同的数据目录，需同时检查 `data_dir`。
- **安装 wheel 提示文件不存在**：在项目根目录用 `./dist/文件名.whl`，在复制后的目录用实际保存路径。
- **普通安装后修改源码没生效**：普通安装读取安装目录副本，按 [开发指南](dev.md#可编辑安装与普通安装)切换。
- **B站下载失败**：先查看缓存列表中的具体原因。网页 412、媒体 CDN 567、权限、时长和格式限制应分开处理，适配细节见 [架构说明](desgin/architecture.md#bilibili-适配与验证记录)。更新代码并重启后，对旧失败条目手动重试；历史失败记录不会自动批量重试。
