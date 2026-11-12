# ViVideo 本地视频架构

[项目概要](../../README.md) · [部署指南](../deployment.md) · [开发指南](../dev.md)

```text
浏览器 ── HTTP/JSON、SSE ── FastAPI 网关
                              ├─ ZMQ REQ/ROUTER ── 对话 Worker
                              │                      ├─ 模型 API（Chat Completions）
                              │                      └─ MCP stdio 子进程
                              │                          ├─ 暂存预取计划、记忆、特征
                              │                          ├─ 查询空间缓存与候选
                              │                          └─ 文字 + 可选视频 ID
                              ├─ 本地 MP4 / 封面 / VTT（Range、HEAD）
                              └─ SQLite（空间隔离）
                                      ↑
                               独立下载 Worker
                               ├─ 领取搜索、下载、延迟筛选任务
                               ├─ yt-dlp → FFmpeg → ffprobe
                               ├─ 资产校验、发布、缓存淘汰
                               └─ 必要时调用模型筛选晚到候选
```

## 回合与计划

SQLite 是状态权威。MCP 的预取计划、记忆和特征提议先暂存，只有有效回复且请求未取消、会话未删除时才随消息原子提交。MCP 子进程结束不影响已提交的预取任务。普通闲聊不触发预取。

首个领域计划 phase=clarifying，服务端不接受视频推荐。用户回答后提交 recommending 版本；模型可选 ready 或已知 pending 候选，前端只收到 ready。明确换领域时 new_topic=True 生成新的 clarifying 版本；原计划 superseded。

prepared 仅表示搜索完成，不等于下载完成。候选资产有独立状态：missing、queued、downloading、processing、ready、failed、evicted。

模型已选的 pending 候选在就绪后补入原回复。若用户回复时没有任何候选，后续缓存就绪会创建 recommend 任务：模型只能从当前计划的 ready ID 中选最多 3 条；结果再次校验当前版本、会话、就绪状态，再更新原回复，无额外的虚构用户回合。

SSE 每秒检查当前会话快照，内容变化时发送 preparation 事件，空闲发送心跳。事件不携带模型私有协议；浏览器以请求 ID 对应原消息并增量更新卡片。会话/空间切换关闭旧订阅，迟到结果不能改变当前页面。

## 队列、恢复与一致性

网关和两个 Worker 共用 VIVIDEO_DATA_DIR。任务本身持久化在每个空间的 SQLite，ZMQ 只负责对话请求；下载进程独立轮询。SQLite 不能通过远程文件系统直接当分布式数据库使用。

任务由 BEGIN IMMEDIATE 领取，持有独立 owner 与 45 秒租约；子进程运行时定期检查所有权并续租。失去所有权、取消或退出时终止整个子进程组。租约过期可恢复，最多 3 次尝试。活跃任务按 kind/target 唯一；并发默认 2，同一来源平台最多 1 个。

下载临时目录为 media/staging/<job>/<owner>，不同租约不共用文件。完成后原子移动到 media/assets/<video-owner>，持有当前租约的 Worker 才能提交 ready。DB 与文件系统没有共同事务，每分钟对账修复缺失资产、过期临时文件和孤立产物。

同一会话不同请求互斥；相同 UUID 的请求不重复执行。删除保留 tombstone，旧 Worker 不能恢复正文。新版本取消失效的自动任务，手动下载、保留资产和其他有效计划所需任务保留。已缓存文件可以继续被同空间其他会话使用。

## 媒体与容量

视频来源只能是规范 Bilibili、YouTube、Vimeo 直链。模型只提交 ID 和查询意图，不决定文件路径、命令或下载参数；下载子进程不用 shell。源链接留作追溯，播放器只取得经空间验证的本地接口。

Bilibili 通过正常元数据/播放接口解析首个分 P 的 CID、时长与格式，避开必须读取 HTML 的依赖；其他来源由 yt-dlp 解析。统一用 yt-dlp 下载，B站媒体使用 Range: bytes=0-，来源只在允许 CDN 与其受支持备选中选择；接口被限制时明确失败。FFmpeg 合并或转码为 H.264/AAC MP4，faststart，兼容 libx264 与 libopenh264；ffprobe 检查视频流与时长。字幕存在时保存 VTT，封面由视频抽帧。文件大小和处理临时空间受监控，失败不发布半成品。

默认每空间 20 GiB，每条 300 MiB、最长 30 分钟、最高 720p，每计划先预取 3 条。任务按单文件上限 3 倍预留临时空间，资产按实际附件总字节计费。LRU 淘汰排除保留和播放中的资产，播放租约每 30 秒续期、120 秒过期。额度不足则等待，不先破坏已有缓存。

同空间规范 URL 去重，空间之间不共享 DB、媒体、配额或用户记忆。旧链接登记为 missing 供手动下载，不批量迁移。

## 记忆、偏好与观看

消息、工具协议、会话记忆、特征、观看行为保存在空间 DB。Markdown 是原子生成的可读投影，可用 export_memories 重建。模型接收最近上下文及同一配置完整协议回合，裁剪时不拆工具调用/结果；不同模型配置不混放协议。

长期特征必须有本轮用户原话证据并经用户确认。观看记录只在实际可见播放时累计，超过总时长 25% 才入库，UUID 去重；个性化关闭时不提供已确认特征和观看行为。观看不等于喜欢。

模型配置保存在全局 settings.db，各空间独立选择；列表不返回密钥。当前为本地单用户应用，密钥明文、文件权限 0600。收藏、笔记、反馈和草稿主要保留浏览器，本地收藏同步资产保留状态。

## 前端与兼容模式

前端原生 HTML/CSS/JS，无构建步骤。左栏固定，空间和页签过渡，欢迎/会话透明度过渡，输入区稳定。推荐结果不超过 3 条时去重并静态显示，窄屏换行；更多结果循环滚动，悬停/聚焦/弹窗/观看时暂停，支持减少动态效果。

播放器绝对覆盖对话+输入区域，关闭恢复原草稿。缓存管理区提供导入、容量、状态、保留、重试、取消和清理。已清理资产不再出现在缓存列表、候选查询和计划展示中；内部状态标记用于避免对账后恢复占位。

VIVIDEO_LOCAL_VIDEO=0 保留原链接代理/官方 SDK 路径；默认本地模式不静默回退远端 iframe。来源平台失败、解析器变动和网络条件仍会影响下载，测试本地流水线通过不表示所有平台视频可下载。


## MCP 工具与上下文

每个模型回合使用独立的 MCP stdio 子进程，绑定服务端传入的空间和会话；模型不能通过工具参数选择其他作用域或任意文件路径。

| 工具 | 职责 |
|---|---|
| `read_memories` | 读取当前会话摘要 |
| `search_space_videos` | 查询当前空间已知视频 |
| `search_web_videos` | 发现规范视频直链候选 |
| `save_video_links` | 暂存已知候选链接 |
| `prepare_domain_videos` | 暂存领域与具体搜索方向，交由后台准备 |
| `query_local_videos` | 查询本地缓存及待准备候选 |
| `get_preparation_status` | 查看当前计划版本和候选状态 |
| `update_session_memory` | 暂存会话记忆摘要 |
| `propose_space_traits` | 提议长期特征及本轮用户原话证据 |
| `respond_to_user` | 必填文字、可选推荐 ID，结束模型回合 |

默认每回合最多 10 轮模型工具往返，时间预算 150 秒。模型没有返回工具调用而直接给出有效文字时，由服务端通过回复工具收尾。更新摘要缺失时，以近期用户/助手文字为记忆兜底。

服务端读取会话摘要、近期文字、同一模型配置最近 30 条持久化消息对应的完整工具回合，并以 24000 字符预算按回合整体裁剪；保留供应商推理字段和工具调用/结果配对。前端提供的历史和偏好不覆盖这些状态，私有工具协议也不出现在公开会话响应中。

标题、检索摘要和历史记忆是资料，不是工具执行指令；候选链接经规范化、按来源去重。搜索结果保留标题与摘要不等于看过视频，模型只能引用已知 ID。清晰度、容量、格式和路径由下载服务控制。

## Bilibili 适配与验证记录

B站视频网页、元数据接口、播放接口和媒体 CDN 可能出现不同错误，不能仅根据统一退出码判断失败原因。

专用适配器先建立普通匿名首页会话，再请求元数据与播放接口，取得真实 CID、当前分 P 时长和音视频格式。临时 info JSON 权限为 0600，由 yt-dlp 读取并下载；处理结束后删除，签名 CDN 地址不进入公开错误信息。

- 视频网页返回 412 时，元数据和播放接口可能仍然可用；接口本身受限则明确失败，不自动登录或伪造浏览器验证。
- 部分媒体 CDN 对普通 GET 返回 567，媒体格式附带标准 `Range: bytes=0-` 请求头；元数据请求不附带 Range。
- 主地址不在支持范围时，选择 API 提供的受支持备用地址，保持 bilivideo.com / bilivideo.cn 来源校验。
- 多分 P 视频当前只下载首个分 P，以该分 P 的 CID 和实际时长校验，标题标注 P1，不将整合集总时长当作当前视频时长。
- 试看、时长明显不足、无效媒体来源和超出限制的产物不发布。可识别的访问限制与格式错误使用固定的可读错误信息，避免暴露提供方响应或签名地址。

2026-11-13 的单次真实验证：原失败样本 `BV1gmW36VEuK` 首个分 P 完成下载、音视频合并、封面生成及 ffprobe 校验；时长 666.69 秒，视频和封面共 10,201,367 字节。验证使用临时库，未批量重试用户任务；它证明该样本在当时网络可用，不代表所有视频持续可用。

参考：[yt-dlp 网页 412 问题 #14830](https://github.com/yt-dlp/yt-dlp/issues/14830)、[CDN 567 与 Range 请求 #17847](https://github.com/yt-dlp/yt-dlp/issues/17847)、[yt-dlp](https://github.com/yt-dlp/yt-dlp)、[FFmpeg faststart](https://ffmpeg.org/ffmpeg-formats.html)、[Starlette FileResponse](https://starlette.dev/responses/#fileresponse)。

## 观看行为与持久化边界

有效观看需要页面可见、播放器至少一半位于视口内、没有弹窗遮挡，并有实际媒体进度增加。暂停、缓冲、跳转进度、后台及不可靠总时长不累计；倍速按实际经过时间计算，不能通过拖到末尾取得观看时长。

严格超过总时长 1/4 才提交事件，恰好 1/4 或更短不创建记录；同次观看 UUID 去重，继续观看更新最大累计值。后端校验空间、视频、可选会话归属及本地权威时长。旧版嵌入回退无法可靠读取进度时不记录 iframe 停留时间。

SQLite 是完整对话和记忆的权威数据源，Markdown 为原子替换生成的可读投影，不能通过外部编辑文件自动写回 DB。后续缓存就绪可能更新原回复的视频卡片；相同请求 ID 的成功重放不会创建新回合，但可包含新就绪视频。

删除会话后，正文、会话记忆文件和未确认特征提议删除，保留删除标记、已确认偏好及可复用媒体。收藏、笔记、反馈和草稿主要在浏览器中，本地收藏同步服务端资产保留状态。当前没有完整观看统计页或视频内容向量索引。

## 主要接口

下表中空间接口前缀为 `/api/spaces/{space}`，完整参数见运行时 `/docs`。

| 接口 | 用途 |
|---|---|
| `POST /api/chat` | 模型回合，携带会话和请求 UUID |
| `GET/POST .../sessions`，`GET/DELETE .../sessions/{id}` | 会话与历史 |
| `POST .../requests/{id}/cancel` | 取消尚未提交的回合 |
| `GET .../sessions/{id}/preparation`，`GET .../sessions/{id}/events` | 准备快照及 SSE |
| `GET .../videos` | 已知来源链接 |
| `GET/POST .../local-videos` | 本地目录与手动导入 |
| `PATCH/DELETE .../local-videos/{id}`，`POST .../local-videos/{id}/download` | 保留、清理和重试 |
| `GET/HEAD .../local-videos/{id}/stream\|cover\|subtitles` | 本地媒体文件 |
| `POST .../local-videos/{id}/lease` | 续租播放保护 |
| `GET/PUT .../cache`，`POST .../download-jobs/{id}/cancel` | 容量、资产、任务及取消 |
| `GET .../videos/{id}/metadata\|playback` | 时长、封面和播放配置 |
| `GET/POST .../watch-events` | 合格观看记录 |
| `GET/POST .../traits`，`DELETE .../traits/{id}` | 已确认偏好 |
| `POST .../trait-proposals/{id}/decision` | 确认、修改或忽略特征提议 |
| `/api/model-configs` 及子路径 | 模型配置增删改查、空间选择 |
| `GET /api/health`，`GET /api/service-status?space_id=learning` | 网关/ZMQ、数据库/模型状态 |
