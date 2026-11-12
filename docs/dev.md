# 开发指南

[项目概要](../README.md) · [部署指南](deployment.md) · [架构说明](desgin/architecture.md)

## 模块开发任务

以下文档是待实现需求，包含固定的数据表、接口、独立页面、实现顺序和验收用例：

| 模块 | 需求清单 |
|---|---|
| 视频目录与标签检索 | [Part A](desgin/partA.md) |
| 视频笔记 | [Part B](desgin/partB.md) |
| 观看历史与统计 | [Part C](desgin/partC.md) |

各模块可使用独立测试数据开发；共同的 `app.py` 路由注册与主页面入口由负责人整合。当前数据库继续使用 SQLite，并保持空间隔离。

## 准备环境

在项目根目录创建并激活完整环境：

```bash
conda env create -f environment.yml
conda activate vivideo
```

已有可用的 `vivideo` 或其他环境时直接激活即可，不必重复创建。也可以按 [部署指南](deployment.md#方式二普通-python-虚拟环境)使用 venv，并提供所需媒体工具。

`environment.yml` 保持普通安装 `pip install .`，不需要为了开发修改文件。切换为可编辑安装：

```bash
python -m pip install -e .
vivideo doctor --data-dir ./data
```

需要构建工具和浏览器测试依赖时，改用：

```bash
python -m pip install -e '.[dev]'
```

## 可编辑安装与普通安装

两种方式安装的都是当前 Python 环境中的同一个 `vivideo` 项目：

| 安装方式 | 加载位置 | 修改源码后的行为 |
|---|---|---|
| `pip install .` 或安装 wheel | 环境中的安装副本 | 需重新安装才会更新副本 |
| `pip install -e .` | 当前源码目录 | 无需重新安装即可读取修改后的文件 |

在同一环境执行 `python -m pip install -e .` 会替换已有的普通安装，不会并存两份 ViVideo，也不会重新创建 Conda 环境。首次切换后无需每次修改代码都重复安装。

如果之后通过环境更新或 `pip install .` 再次做了普通安装，可重新执行 `python -m pip install -e .` 切回开发模式。不同 Conda 环境各自安装，需确认当前激活的环境。

两种安装方式的默认数据目录不同。为了持续使用同一份会话、配置和缓存，开发命令统一显式指定 `--data-dir ./data`，并在项目根目录运行。

## 自动重载与前端刷新

先停止旧服务，再启动开发模式：

```bash
vivideo serve --reload --data-dir ./data
```

| 修改内容 | 如何生效 |
|---|---|
| 应用 Python 文件 | 自动重启 FastAPI、对话 Worker、下载 Worker |
| 前端 HTML/CSS/JS | 刷新浏览器；前端没有构建步骤 |
| Python 依赖 | 重新执行可编辑安装，再重启服务 |
| CLI 启动参数或监督进程自身逻辑 | 停止后重新运行启动命令 |
| 数据库、视频文件、字节码缓存 | 不触发代码重载 |

监视范围是 `python/vivideo/` 中 Python 文件的新增、修改和删除。连续保存会合并处理；代码错误造成子服务退出时，监督进程继续等待修复，保存修复后重启全部服务。

这里的重载通过自动重启实现，进行中的模型请求会中断，下载按队列恢复策略处理。前端不提供浏览器自动刷新或框架级 HMR。普通安装和 wheel 安装使用源码副本，`--reload` 会提示先切换为可编辑安装。

默认的 `vivideo serve --data-dir ./data` 不自动重载，适合日常使用和部署。统一使用 CLI，已移除旧开发启动脚本。

## 代码导航

```text
python/vivideo/
├── cli.py / __main__.py         # 命令入口、进程监督、自动重载
├── paths.py                    # 源码/安装环境的数据与工具路径
├── app.py                      # FastAPI 网关、静态资源、聊天入口
├── worker.py / agent.py         # ZMQ 对话服务与模型工具循环
├── mcp_server.py               # 每回合隔离的 MCP 工具
├── storage.py / model_configs.py # 会话存储、Markdown 投影、模型配置
├── library_schema.py           # 本地媒体相关表结构
├── library.py / library_api.py  # 预取、任务、缓存、SSE 和本地媒体接口
├── download_worker.py          # 搜索/下载/延迟筛选任务执行
├── bilibili_source.py          # B站 API 与媒体格式适配
├── search.py / media.py         # 搜索及旧版远端媒体模式
├── service_status.py           # 数据库与模型连接状态
├── export_memories.py          # 从数据库重建 Markdown
└── static/
    ├── index.html              # 页面结构
    ├── style.css               # 布局与动画
    ├── app.js                  # 页面状态、会话、缓存和播放交互
    └── watch-accumulator.js     # 有效观看计时
```

数据库状态、工具暂存、计划版本与下载文件发布的约束见 [架构说明](desgin/architecture.md)。运行时 `/docs` 提供完整接口参数。

## 测试

在可编辑安装的环境中运行 Python 测试：

```bash
python -m unittest discover -s tests -v
```

观看计时使用独立 JavaScript 检查：

```bash
node tests/watch_accumulator.test.js
```

Python 测试覆盖网关、真实 ZMQ/MCP 调用、模型协议、记忆和偏好确认、任务恢复与取消、配额、本地播放和 B站适配。下载流水线使用微小本地 HTTP 视频，真实调用 yt-dlp、FFmpeg 和 ffprobe，平台解析用测试数据；缺少下载依赖时相应测试可能跳过，应先运行 `doctor`。

可选浏览器集成：

```bash
python -m pip install -e '.[dev]'
python -m playwright install chromium
python tests/browser_local_video.py
VIVIDEO_BROWSER_LATE=1 python tests/browser_local_video.py
```

第一条浏览器路径验证用户回答时已有候选，第二条验证候选晚到后自动筛选。测试使用临时数据库、本地模型 HTTP 服务和视频样例，不调用用户保存的真实模型 API；覆盖 SSE 卡片更新、播放、观看阈值、缓存操作和手机布局。可通过 `VIVIDEO_BROWSER_BIN` 指定 Chromium 路径。

测试数据的通过不等于第三方平台始终可下载。涉及平台适配时，额外使用少量真实样本验证，并把网络、接口失败和媒体校验失败分开判断。

## 构建与分发

需要发给另一台机器安装时再构建，源码开发不需要 `dist/`：

```bash
python -m pip install -e '.[dev]'
python -m build
```

输出：

```text
dist/
├── vivideo-0.1.0-py3-none-any.whl
└── vivideo-0.1.0.tar.gz
```

wheel 包含 Python 和静态网页文件；源码包额外包含环境清单、开发与部署文档。数据库、密钥、下载视频和 `.tools` 不应包含在任何分发包中。`dist/` 是可重新生成的产物，可以删除；源码部署只需完整项目与环境清单。

同版本 wheel 重新构建后更新安装，可显式执行：

```bash
python -m pip install --force-reinstall /path/to/vivideo-0.1.0-py3-none-any.whl
```

安装到其他机器的具体步骤和数据迁移见 [部署指南](deployment.md)。
