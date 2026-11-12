# Part C：观看历史与统计

状态：待实现的需求清单。观看记录写入能力已存在；本文的新历史查询、统计接口和页面尚未实现。

目标：把已有观看记录展示为可筛选、分页和删除的历史列表，并用 SQL 计算观看概览、每日统计和视频排行。重点是清晰的查询与统计，不开发新的播放器或推荐算法。

[开发指南](../dev.md) · [Part A](partA.md) · [Part B](partB.md)

## 1. 任务边界

| 必须实现 | 本次不要求 |
|---|---|
| 按时间范围、视频标题查询历史并分页 | 修改播放器、重新实现观看计时 |
| 删除单条历史记录 | 批量删除、导出、跨空间统计 |
| 观看次数、不同视频数、时长、每日统计、前五排行 | 用户画像、推荐评分、机器学习 |
| 学习与漫游空间隔离 | 大屏图表库、后台统计任务、统计汇总表 |

本模块不新增业务表，复用已有 `watch_events`、`videos`、`media_assets`。直接查询原始记录即可，不需要为每个统计数字建表。学习和漫游分别统计。

## 2. 现有规则：必须保留

阅读 `python/vivideo/storage.py` 的 `record_watch()`、`list_watch_events()`，以及 `state_api.py` 的 `WatchInput` 和 `/watch-events` 路由。

- 原有 `POST /api/spaces/{space_id}/watch-events` 负责记录行为，继续使用，不改合同。
- 只有观看秒数严格大于视频时长的四分之一才存记录；刚好等于四分之一不记录。
- 相同 `event_id` 重传是更新同一行，观看秒数取较大值，不额外增加观看次数。
- `created` 是首次成功记录的时间；`updated` 可能随后续上报变化。本模块统一按 `created` 筛选和归属日期。
- 不允许把低于阈值的观看另存为本模块日志。

现有表主要字段如下，仅作阅读说明，不重新创建或改变它：

| `watch_events` 字段 | 含义 |
|---|---|
| `id` | 观看事件 UUID，主键 |
| `video_id` | 指向 `videos.id` 的外键 |
| `session_id` | 可空的会话 ID |
| `watched_seconds` | 此事件已累计上报的有效观看秒数 |
| `duration_seconds` | 视频总秒数 |
| `created`、`updated` | UTC Unix 毫秒整数 |

## 3. 文件归属与接入方式

| 文件 | 内容 |
|---|---|
| `python/vivideo/history_store.py` | 历史查询、删除、统计 SQL 与索引初始化 |
| `python/vivideo/history_api.py` | 查询参数、时间范围校验与路由，导出 `router` |
| `python/vivideo/static/part-c.html` | 观看历史与统计页面 |
| `python/vivideo/static/part-c.js` | 页面交互和接口请求 |
| `python/vivideo/static/part-c.css` | 页面样式 |
| `tests/test_history.py` | 本模块测试 |

在 `app.py` 增加 `from .history_api import router as history_router` 和 `app.include_router(history_router)`，保留其他注册。除此之外不需要修改公共代码。可在测试中建立临时 `FastAPI()` 单独挂载本模块路由。

页面地址 `/static/part-c.html`，只加载自己的 JS/CSS，不加载现有 `app.js`。不依赖 Part A、B。最终主页面入口由负责人统一接入。

所有查询使用 `storage.database(space)` 打开当前空间的 `space.db`。继续使用 SQLite 和原生前端。

## 4. 新增索引

提供 `ensure_schema(db)`，在本模块每次查询或删除前调用；使用逐条 `db.execute()` 重复执行以下 DDL，不在业务事务中执行 `executescript()`。

```sql
CREATE INDEX IF NOT EXISTS idx_watch_events_created
    ON watch_events(created DESC, id);

CREATE INDEX IF NOT EXISTS idx_watch_events_video_created
    ON watch_events(video_id, created DESC, id);
```

第二个索引用于按视频分组及相关查询的优化实验。验收时通过 `EXPLAIN QUERY PLAN` 判断真实查询是否使用，不能仅凭“创建了索引”断言加速；标题子串查询不要求被普通索引加速。

## 5. 所有统计共用的口径

| 名称 | 唯一定义 |
|---|---|
| 观看次数 `event_count` | 时间范围内 `watch_events` 的行数；同一视频看两次可以是两行 |
| 视频数量 `video_count` | 范围内不同 `video_id` 的数量 |
| 单次统计时长 | `MIN(watched_seconds, duration_seconds)`，最多算一个视频总时长 |
| 总统计时长 `watched_seconds` | 所有行的单次统计时长之和 |
| 平均统计时长 `average_seconds` | 总统计时长 / 观看次数；无记录时为 0 |
| 单条进度 `progress` | `MIN(watched_seconds / duration_seconds, 1)`，范围 0～1 |

单条历史中的 `watched_seconds` 保留数据库原值，额外返回 `counted_seconds` 表示该行参与统计的截断时长。后续上报增加原值时，属于原 `created` 日期的统计也会变化，本模块不提供历史统计快照。

时间过滤统一为 `created >= from_ms AND created < to_ms`，左闭右开。接口传入的时间均为 UTC Unix 毫秒。每日归组及页面日期选择固定使用北京时间 UTC+08:00，页面明确标注时区，不随服务器时区改变。

`from_ms`、`to_ms` 必须同时提供或同时省略。省略时由服务端计算北京时间“今天及前六天”的范围，即六天前 00:00 至明天 00:00。显式范围要求 `0 <= from_ms < to_ms`，跨度最多 31×86400000 毫秒，否则返回 `422`。

前端选择起止日期时，起始日 00:00 为 `from_ms`，结束日的下一天 00:00 为 `to_ms`。请求返回已经解析的两个值，便于页面核对。

## 6. HTTP 接口合同

前缀 `/api/spaces/{space_id}`，`space_id` 使用现有 `Space` 类型，仅允许 `learning`、`roaming`。新接口不替换旧 `/watch-events`。

| 方法与相对路径 | 参数 | 成功响应 |
|---|---|---|
| `GET /watch-history` | `from_ms`、`to_ms`、`q=""`、`limit=20`、`offset=0` | `200`，分页历史 |
| `DELETE /watch-history/{event_id}` | UUID 路径参数 | `204`，无响应体 |
| `GET /watch-stats` | `from_ms`、`to_ms` | `200`，概览和前五视频 |
| `GET /watch-stats/daily` | `from_ms`、`to_ms` | `200`，逐日统计 |

历史的 `q` 去两端空白，最长 100 字，匹配展示标题的字面子串，空串匹配全部；使用绑定参数 `instr(title, ?) > 0` 等方式实现。标题从 `watch_events JOIN videos LEFT JOIN media_assets` 读取，采用 `COALESCE(NULLIF(media_assets.title, ''), videos.id)`。

历史分页 `limit` 为 1～100，`offset` 大于等于 0；按 `created DESC, id ASC` 排序，先按日期和关键词过滤再分页。`total` 是过滤后的总行数。关键词只过滤历史列表；概览、排行和每日统计仅受时间范围影响，页面应明确标注这一点。

历史响应示例：

```json
{
  "items": [
    {"event_id":"33333333-3333-4333-8333-333333333333","video_id":"demo-video-a","title":"SQL 入门","url":"https://example.com/video/a","session_id":null,"watched_seconds":30,"duration_seconds":100,"counted_seconds":30,"progress":0.3,"created":1704038460000,"updated":1704038460000}
  ],
  "total":1,"limit":20,"offset":0,
  "from_ms":1704038400000,"to_ms":1704297600000
}
```

概览响应示例，对应第 9 节测试数据：

```json
{
  "from_ms":1704038400000,"to_ms":1704297600000,
  "event_count":3,"video_count":2,"watched_seconds":180,"average_seconds":60,
  "top_videos":[
    {"video_id":"demo-video-a","title":"SQL 入门","event_count":2,"watched_seconds":90},
    {"video_id":"demo-video-b","title":"事务基础","event_count":1,"watched_seconds":90}
  ]
}
```

排行固定返回前五条，排序为 `watched_seconds DESC, event_count DESC, video_id ASC`；不足五条不补齐。聚合先按 `video_id` 分组，再关联标题，避免错误的多表连接把统计放大。

每日响应示例：

```json
{
  "from_ms":1704038400000,"to_ms":1704297600000,
  "timezone":"UTC+08:00",
  "items":[
    {"date":"2024-01-01","event_count":2,"video_count":1,"watched_seconds":90},
    {"date":"2024-01-02","event_count":1,"video_count":1,"watched_seconds":90},
    {"date":"2024-01-03","event_count":0,"video_count":0,"watched_seconds":0}
  ]
}
```

每日日期升序，补齐与 `[from_ms, to_ms)` 相交的北京时间日期；无记录日补零，可用 Python 补齐。每日 `video_count` 不能简单相加当作整段时间的去重视频数。

## 7. SQL 实现提示

概览必须用 SQL 聚合，不要把全部记录读回 Python 再求和：

```sql
SELECT COUNT(*) AS event_count,
       COUNT(DISTINCT video_id) AS video_count,
       COALESCE(SUM(MIN(watched_seconds, duration_seconds)), 0) AS watched_seconds
FROM watch_events
WHERE created >= ? AND created < ?;
```

平均值可根据聚合结果在 Python 中计算。按北京时间归组可使用：

```sql
SELECT date(created / 1000.0, 'unixepoch', '+8 hours') AS day,
       COUNT(*) AS event_count,
       COUNT(DISTINCT video_id) AS video_count,
       SUM(MIN(watched_seconds, duration_seconds)) AS watched_seconds
FROM watch_events
WHERE created >= ? AND created < ?
GROUP BY day
ORDER BY day;
```

历史总数和分页数据在同一只读事务中查询；概览与排行也在同一只读事务中完成，保证一次响应内部使用同一份数据快照。先初始化索引，再执行 `BEGIN`，不要在读事务内写入业务数据。

删除单条记录使用事务，按当前空间中的 `id` 删除，不存在返回 `404`。删除立即影响后续统计，不修改视频、会话或文件；它也不禁止用户后续观看同一视频并产生新的上报。

非法空间、UUID、分页、关键词或时间范围返回 `422`，不把原始 SQL 异常直接返回浏览器。所有输入使用绑定参数。

业务错误沿用 `StateError`，响应为 `{"detail":"错误说明"}`。测试如果使用临时 `FastAPI()`，还需注册与 `app.py` 相同的 `StateError` 异常处理器，不能只挂路由。

## 8. 页面需求

- 顶部选择空间、开始日期、结束日期，默认最近七天，显示“北京时间”。
- 显示四个数字：观看次数、不同视频数、统计观看时长、平均每次统计时长。
- 显示每日统计和前五视频排行，普通 HTML 表格即可，不要求图表库。
- 历史区域提供标题搜索、分页、删除按钮；标题旁显示视频总时长、观看时长、进度和记录时间。
- 明确标注“关键词仅筛选历史列表，统计按所选时间范围计算”。
- 删除前页面内确认，成功后刷新历史与全部统计；当前页因删除变空时回到上一页或第一页。
- 空库显示零值和空状态，不出现 `null`、`NaN` 或除零错误。
- 数值接口保留秒，页面可以格式化为分/秒，不把格式化文本再用于计算。
- 对文本使用 `textContent`，请求时禁用相关按钮；切换空间或范围后丢弃旧请求结果，不能串数据。
- 页面数据以服务端为准，不从 LocalStorage 拼接观看历史。

## 9. 固定测试数据与独立运行

按照 [开发指南](../dev.md)准备环境，执行 `python -m pip install -e .`，启动：

```bash
VIVIDEO_DATA_DIR=./data PYTHONPATH=python python -m uvicorn vivideo.app:app --host 127.0.0.1 --port 8001
```

打开 `http://127.0.0.1:8001/static/part-c.html`。测试不依赖模型、下载、真实播放或其他模块。

自动化测试设置临时 `VIVIDEO_DATA_DIR`，通过 `storage.database('learning')` 初始化基础表，使用绑定参数插入视频 `demo-video-a`（`SQL 入门`，100 秒）和 `demo-video-b`（`事务基础`，120 秒）。插入 `videos` 时填齐 `id,url,query,created`，`query='fixture'`；URL 分别为 `https://example.com/video/a`、`https://example.com/video/b`。标题和时长插入 `media_assets`；不下载这些地址。

令 `T=1704038400000`，即北京时间 2024-01-01 00:00。为三条事件分别生成不同 UUID，`session_id=null`，`updated=created`：

| 事件 | 视频 | `created` | 观看秒数 | 总秒数 |
|---|---|---|---|---|
| E1 | a | `T + 60000` | 30 | 100 |
| E2 | a | `T + 3600000` | 60 | 100 |
| E3 | b | `T + 86400000 + 60000` | 90 | 120 |

通过参数化 SQL 插入固定时间的记录，显式查询 `[T, T+3×86400000)`，结果必须与第 6 节示例一致。另通过真实 `record_watch()` 或已有 POST 接口验证阈值与幂等，不能仅靠手工插表宣称验证了写入规则。

## 10. 实现顺序

1. 完成统一时间范围解析与校验，固定北京时间归组。
2. 编写历史 JOIN、总数、分页查询和删除。
3. 实现概览、每日聚合及排行 SQL，用固定数据核对数字。
4. 实现接口，再做独立页面，最后注册主应用路由。
5. 完成验收及索引查询计划说明。

## 11. 必须通过的验收清单

- [ ] 三条固定记录得到次数 3、不同视频数 2、总时长 180、平均 60，排行与每日示例一致。
- [ ] 同一事件重复上报只算一次；同一视频两个不同事件算两次，去重视频数仍为 1。
- [ ] 100 秒视频观看 25 秒不记录，超过 25 秒才记录，原写入接口保持兼容。
- [ ] 增加一条观看 150 秒、总长 100 秒的测试记录，该行参与统计的时长为 100，进度为 1。
- [ ] 正好等于 `from_ms` 的记录计入，正好等于 `to_ms` 的不计入；北京时间跨日归组正确。
- [ ] 默认最近七天可通过固定服务端时间测试，服务器时区改变不影响统计。
- [ ] 无记录日补零，整个范围为空时概览全为 0、排行为空；部分日期范围也符合日期归组合同。
- [ ] 历史关键词、分页、总数与排序正确；关键词不改变概览。
- [ ] 删除记录后统计同步变化，其他视频、会话、文件不受影响；重复删除返回 `404`。
- [ ] 学习与漫游隔离，非法时间范围、只提供一端、跨度超限、非法分页返回 `422`。
- [ ] 查询与页面不访问真实网络；切换空间、筛选时迟到的响应不覆盖新结果。
- [ ] 提供至少一条实际 SQL 的索引前后 `EXPLAIN QUERY PLAN`，说明是否使用及其原因。

运行：`python -m unittest discover -s tests -p 'test_history.py' -v`。测试只使用临时数据目录。

## 12. 交付清单

提交源码、测试和 `docs/desgin/partC-result.md`。结果说明包含：已有三表的关联图、统计口径、历史 JOIN、去重与分组 SQL、固定样例的手算结果、查询计划与测试结果、页面截图。不要为了表数量增加没有用途的新表，也不记录个人机器路径或本地 Git 配置。
