# Part B：视频笔记

状态：待实现的需求清单。本文中的新表、接口和页面目前尚未实现。

目标：用户能为视频保存多条笔记，搜索、修改、删除笔记，并查看修改历史。实现明确的笔记管理即可，不需要开发知识图谱、AI 总结或复杂编辑器。

[开发指南](../dev.md) · [Part A](partA.md) · [Part C](partC.md)

## 1. 任务边界

| 必须实现 | 本次不要求 |
|---|---|
| 笔记新建、列表、详情、修改、删除 | 富文本、附件、图片上传、Markdown 渲染 |
| 按视频筛选、按标题/正文搜索、分页 | 笔记标签、共享、跨空间复制 |
| 每次保存保留版本，防止旧页面覆盖新修改 | 恢复旧版本、比较版本差异、自动保存 |
| 学习与漫游空间隔离、数据库持久化 | 改造现有对话页面或导入浏览器旧笔记 |

一个视频可以有多条笔记，每条笔记只属于一个已有视频。创建后不能改绑视频。标题不可为空，正文可为空。笔记以纯文本显示。删除笔记时一起删除它的版本记录，不删除视频或其他业务数据。

## 2. 先阅读这些代码

- `python/vivideo/storage.py`：`database(space)`、`get_video()` 和现有 `videos` 表。
- `python/vivideo/library_schema.py`：从 `media_assets` 读取视频标题的方式。
- `python/vivideo/state_api.py`：请求校验、路由、`StateError` 用法。
- `python/vivideo/app.py`：路由注册与 `/static` 挂载。

继续使用 SQLite、原生 HTML/CSS/JavaScript。使用 `storage.database(space)` 打开当前空间已有的 `space.db`，不要建立独立笔记数据库，也不要更改现有视频表。该连接已经启用外键。

## 3. 文件归属与接入方式

| 文件 | 内容 |
|---|---|
| `python/vivideo/notes_store.py` | 建表、查询、保存与版本事务 |
| `python/vivideo/notes_api.py` | 请求模型与接口，导出 `router` |
| `python/vivideo/static/part-b.html` | 笔记管理页面 |
| `python/vivideo/static/part-b.js` | 页面交互与接口请求 |
| `python/vivideo/static/part-b.css` | 页面样式 |
| `tests/test_notes.py` | 本模块测试 |

在 `app.py` 增加 `from .notes_api import router as notes_router` 和 `app.include_router(notes_router)`，保留其他注册。其他公共文件无需修改。联调前可用临时 `FastAPI()` 单独挂载本模块路由。

页面地址 `/static/part-b.html`，只加载自己的 JS/CSS，不加载现有 `app.js`，不依赖 Part A、C。最终主页面入口由项目负责人统一接入。

## 4. 数据表：按以下定义实现

每个空间独立建表，不额外保存 `space_id`。UUID 用字符串，所有时间由服务端生成，为 UTC Unix 毫秒整数。

```sql
CREATE TABLE IF NOT EXISTS video_notes (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(id),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 80),
    content TEXT NOT NULL CHECK(length(content) <= 10000),
    version INTEGER NOT NULL CHECK(version >= 1),
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS note_revisions (
    note_id TEXT NOT NULL REFERENCES video_notes(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK(version >= 1),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 80),
    content TEXT NOT NULL CHECK(length(content) <= 10000),
    saved INTEGER NOT NULL,
    PRIMARY KEY(note_id, version)
);

CREATE INDEX IF NOT EXISTS idx_video_notes_updated
    ON video_notes(updated DESC, id);

CREATE INDEX IF NOT EXISTS idx_video_notes_video_updated
    ON video_notes(video_id, updated DESC, id);
```

提供 `ensure_schema(db)`，使用逐条 `db.execute()` 执行 DDL，在本模块每次读写业务数据前调用；重复执行不报错。不要在已有业务事务中调用 `executescript()`。本阶段无需通用迁移框架。

`video_notes` 保存最新内容，`note_revisions` 保存每次成功保存时的完整快照，包含当前版本。创建时版本为 1，两张表各插入一行。以后每次成功修改版本加 1，并新增快照。标题允许同名；版本号按每条笔记分别递增。

视频信息通过 `JOIN videos LEFT JOIN media_assets` 读取，不复制到笔记表。`video_title` 使用 `COALESCE(NULLIF(media_assets.title, ''), videos.id)`；视频缓存被清理不影响笔记。

## 5. HTTP 接口合同

前缀 `/api/spaces/{space_id}`，`space_id` 使用现有 `Space` 类型，仅允许 `learning`、`roaming`。笔记 ID 为 UUID；视频 ID 为非空字符串，最长 100 字，不是 UUID。

| 方法与相对路径 | 请求 | 成功响应 |
|---|---|---|
| `GET /notes` | `video_id` 可选、`q=""`、`limit=20`、`offset=0` | `200`，分页笔记摘要 |
| `POST /notes` | `video_id`、`title`、`content` | `201`，完整笔记，`version=1` |
| `GET /notes/{note_id}` | 无 | `200`，完整笔记 |
| `PUT /notes/{note_id}` | `title`、`content`、`expected_version` | `200`，保存后的完整笔记 |
| `DELETE /notes/{note_id}` | 无 | `204`，无响应体 |
| `GET /notes/{note_id}/revisions` | `limit=20`、`offset=0` | `200`，分页版本列表，包含当前版本 |

标题先 `strip()`，长度 1～80；正文保留原始换行和首尾空白，长度 0～10000。`content` 必须提供，空正文传 `""`。`expected_version` 是大于等于 1 的整数。请求模型设置 Pydantic `ConfigDict(extra='forbid')`，修改时传入 `video_id` 等未声明字段返回 `422`。

分页 `limit` 为 1～100，`offset` 大于等于 0。`q` 去两端空白，最长 100 字；匹配标题或正文的字面子串，空串匹配全部。可用绑定参数的 `instr(title, ?) > 0 OR instr(content, ?) > 0`，不需要全文检索引擎。与 `video_id` 筛选同时出现时使用 AND；不存在的视频筛选结果为空。

笔记列表按 `updated DESC, id ASC` 排序，版本按 `version DESC` 排序。过滤后计数再分页，`total` 不受分页影响。

完整笔记响应示例：

```json
{
  "id":"22222222-2222-4222-8222-222222222222",
  "video_id":"demo-video-a",
  "video_title":"SQL 入门",
  "title":"事务的三个例子",
  "content":"例子一：转账需要一起成功或一起失败。",
  "version":1,
  "created":1704038400000,
  "updated":1704038400000
}
```

列表返回 `{"items":[...],"total":1,"limit":20,"offset":0}`。摘要包含完整响应中除 `content` 外的所有字段，再增加 `excerpt`：正文的前 80 个字符，不追加省略号。不要让列表返回所有笔记全文。

版本列表也使用以上分页外层，条目为 `{"note_id":"…","version":1,"title":"…","content":"…","saved":1704038400000}`。修改请求示例：

```json
{"title":"事务的例子","content":"更新后的正文","expected_version":1}
```

## 6. 保存事务：照这个顺序实现

创建时，使用 `BEGIN IMMEDIATE` 开始写事务，在同一连接内确认视频属于当前空间、插入最新笔记、插入版本 1。任何一步失败都回滚。

修改时使用 `BEGIN IMMEDIATE` 开启写事务，然后执行带版本条件的更新：

```sql
UPDATE video_notes
SET title = ?, content = ?, version = version + 1, updated = ?
WHERE id = ? AND version = ?;
```

检查 `rowcount`。为 0 时，在当前空间查笔记是否存在：不存在返回 `404`，存在则返回 `409`，表示页面版本已过期。更新成功后读取新的版本，并在同一事务插入 `note_revisions` 快照；两个写操作必须一起提交或一起回滚。

即使提交的内容没有变化，成功保存也会增加一个版本。这样版本规则固定，不必实现内容比较。两个页面同时持有版本 1 时，第一个保存成为版本 2，第二个仍携带 `expected_version=1`，必须被拒绝，不能覆盖前者。

删除时检查笔记存在并删除，通过外键级联清理版本。列表、详情与版本查询都只访问当前空间数据库。所有 SQL 使用绑定参数。

| 场景 | 结果 |
|---|---|
| 视频不在当前空间、笔记不存在 | `404` |
| 标题为空或超长、正文超长、缺少正文、非法 UUID 或分页参数 | `422` |
| `expected_version` 与当前版本不一致 | `409`，不改变笔记或版本记录 |
| 修改后保存快照失败 | 事务回滚，最新笔记也不改变 |
| 非法空间 | `422` |

业务错误沿用 `StateError`，响应为 `{"detail":"错误说明"}`。测试如果使用临时 `FastAPI()`，还需注册与 `app.py` 相同的 `StateError` 异常处理器，不能只挂路由。

## 7. 页面需求

- 提供空间选择器、视频筛选、关键词搜索、笔记列表和分页。
- “新建笔记”先通过现有 `GET /api/spaces/{space_id}/videos` 选择视频。接口返回 `videos` 数组，至少使用 `id`、`url`，缺标题时显示 `id`。空库时禁用新建并提示。
- 编辑区为普通标题输入框与正文 `textarea`，支持保存、取消、删除。
- 打开笔记时保存其 `version`；提交修改时传这个版本，不能先悄悄读取最新版本再提交。
- 遇到 `409`，保留用户尚未保存的文本，显示“笔记已被其他页面修改，请复制当前内容后重新加载”，提供重新加载按钮；不自动重试覆盖。
- 提供“修改历史”区域，显示版本号、保存时间和纯文本快照，不要求恢复功能。
- 切换笔记或空间前，如有未保存内容，先进行页面内确认；取消切换则保留编辑器。
- 请求期间禁用保存/删除；失败保留输入。用 `textContent` 或输入框的 `value` 展示数据，不直接拼入 `innerHTML`。
- 记录请求所属空间和选中笔记，旧响应不得覆盖当前界面。刷新后从服务端读取，LocalStorage 不是笔记数据源。

## 8. 独立测试数据与运行

按照 [开发指南](../dev.md)准备环境并执行 `python -m pip install -e .`。独立启动：

```bash
VIVIDEO_DATA_DIR=./data PYTHONPATH=python python -m uvicorn vivideo.app:app --host 127.0.0.1 --port 8001
```

打开 `http://127.0.0.1:8001/static/part-b.html`。不依赖模型、下载服务或其他模块。

测试通过临时 `VIVIDEO_DATA_DIR` 隔离数据，用 `storage.database('learning')` 建立基础表并插入视频：

```sql
INSERT INTO videos(id, url, query, created) VALUES (?, ?, ?, ?);
INSERT INTO media_assets(video_id, title, author, duration) VALUES (?, ?, ?, ?);
```

准备 `demo-video-a`（URL `https://example.com/video/a`，标题 `SQL 入门`，100 秒）和 `demo-video-b`（URL `https://example.com/video/b`，标题 `事务基础`，120 秒）；`query` 为 `fixture`，`created` 为固定毫秒时间。这些仅为元数据，不应访问对应网址。漫游空间另建独立数据。

测试可在临时 `FastAPI()` 上挂自己的路由，使用 `TestClient`。失败回滚测试可在写入版本记录前注入异常，不要为测试修改生产数据或依赖真实模型。

## 9. 实现顺序

1. 建表，实现创建、列表、详情、删除，验证外键和空间隔离。
2. 实现带 `expected_version` 的修改，以及版本快照事务。
3. 实现搜索、分页、版本列表和 API 校验。
4. 编写独立页面及冲突提示，最后注册主应用路由。
5. 完成以下验收与结果说明。

## 10. 必须通过的验收清单

- [ ] 同一视频可创建两条笔记，标题可重复，刷新和重启后仍存在。
- [ ] 创建产生版本 1；连续修改后为版本 3，历史有 1、2、3 三条快照。
- [ ] 两个客户端先后使用 `expected_version=1` 保存，后者返回 `409`，成功内容与历史不被改写。
- [ ] 在最新笔记更新后、快照写入前注入异常，两张表均回到原状态。
- [ ] 标题去空白校验、空正文、正文上限、非法版本和 UUID 的处理符合合同。
- [ ] 搜索覆盖标题和正文，视频筛选、排序、分页总数正确。
- [ ] 删除笔记后版本行清空，视频和其他笔记仍存在。
- [ ] 不存在的视频不能创建笔记；学习空间的笔记 ID 在漫游空间返回 `404`。
- [ ] `<script>`、引号和换行按文本显示，不能执行 SQL 或脚本。
- [ ] 发生冲突或网络失败时，编辑框未保存的文字仍保留；空间切换不串数据。

运行：`python -m unittest discover -s tests -p 'test_notes.py' -v`。测试使用临时目录，不访问网络。

## 11. 交付清单

提交源码、测试和 `docs/desgin/partB-result.md`。结果说明包括：视频—笔记—版本的一对多关系图、字段与约束、搜索与列表 SQL、带版本条件更新的解释、事务回滚测试、冲突场景截图。只描述本项目功能，不记录个人机器路径或本地 Git 配置。
