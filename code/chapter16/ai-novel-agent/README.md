---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'f938ce04-cf31-40b3-853e-113b591f96e7'
  PropagateID: 'f938ce04-cf31-40b3-853e-113b591f96e7'
  ReservedCode1: 'fd56ed81-3fcc-47d4-8876-e0fe11c1b952'
  ReservedCode2: 'fd56ed81-3fcc-47d4-8876-e0fe11c1b952'
---

# AI 小说 Agent：生产级基础底座

这是 `hello-agents` 第 16 章的生产导向项目。目前包含**小说资料导入与检索**及首版**梗概与人物分析预览**，尚不具备持久化人物记忆和长篇生成能力，也未达到生产上线条件。

当前代码包含租户安全的项目与资料 API、OIDC 身份边界、PostgreSQL 权威模型、Transactional Outbox、Temporal 编排，以及 TXT/DOCX/文本型 PDF 解析、分块、向量投影和带引用搜索。投递任务已加入租约校验、短事务与执行凭证隔离。部分能力仍需要部署方显式提供对象存储、向量模型等适配器；默认配置不能直接跑通完整上传到检索链路。

索引重建恢复代码已实现，包括逐部加载、进度恢复和过期执行隔离；迁移的空值约束与降级数据保护也已补齐。当前收尾工作是生产接线与真实服务验证。真实 PostgreSQL、MinIO、Temporal、Qdrant 联调仍是发布门禁。故事图（人物关系、事件、Neo4j 投影同步）已实现并接入 API，但 Neo4j 驱动未安装，投影同步在未配置时返回 503。写作记忆、大纲生成、章节生成与 Web 前端均已实现并接入 API。故事圣经（角色设定、世界观条目、伏笔追踪）已实现并接入 API。PDF 目前仅面向文本层，真实解析器验证与资源隔离要求见 [VALIDATION_DEBT.md](VALIDATION_DEBT.md)。

## 架构边界

- PostgreSQL 是业务事实的唯一权威数据源，项目、分支和 Outbox 事件必须在同一事务中提交。
- Temporal 只负责编排可恢复的长任务；Activity 调用应用服务，Workflow 不导入 HTTP 路由或直接访问数据库。
- Qdrant 是可重建的语义检索投影，Neo4j 是可重建的人物、事件和关系图投影。
- Qdrant 已有独立适配器、Outbox 消费逻辑与搜索接口，但生产接线及真实服务验证尚未完成；仅启动服务不会自动构建索引。Neo4j 故事图投影已实现抽象 transport + 惰性导入模式，配置 `NEO4J_PASSWORD` 后自动构建投影适配器；未配置时图同步 API 返回 503，权威图查询不受影响。
- 所有读写都从已验证的 `Principal` 获取 `tenant_id`，不得接受客户端自报租户。
- PostgreSQL 或 Temporal 不可用时就绪探针返回 `503`；可重建投影不可用时返回 `200 degraded`，且不得影响权威写入。

## 前置条件

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Docker Engine 与 Docker Compose v2
- GNU Make（可选；也可以直接执行 Makefile 中的命令）

本地只使用仓库内已锁定的依赖版本：

```bash
cd code/chapter16/ai-novel-agent
cp .env.example .env
make install
```

Windows PowerShell 可用 `Copy-Item .env.example .env` 代替 `cp`。`make install` 使用 `uv sync --frozen --group dev`，不会改写 `uv.lock`。示例口令仅适用于绑定到回环地址的本地开发环境，不得带入共享或生产环境。

## 本地运行

启动全部依赖并执行迁移：

```bash
make infra-up
make migrate
```

分别在两个终端启动 API 和 Worker：

```bash
make api
make worker
```

API 默认位于 `http://127.0.0.1:8000`，存活探针为 `/api/v1/health/live`，就绪探针为 `/api/v1/health/ready`。业务 API 要求来自 `.env` 中 OIDC Issuer 的 Bearer Token；本地 Compose 不包含身份提供商。

### Web 前端（可选）

仓库内 `web/` 是 Vue 3 + Element Plus 单页应用（大纲生成 + 章节创作界面）。构建后由
FastAPI 静态挂载，`http://127.0.0.1:8000/` 即打开前端：

```bash
cd web
npm install
npm run build
```

开发模式可独立运行 `npm run dev`（端口 5173，`/api` 代理到 8000）。详情见 [web/README.md](web/README.md)。

默认服务地址：

| 服务 | 地址 | 当前用途 |
| --- | --- | --- |
| PostgreSQL | `127.0.0.1:5432` | 权威数据库 |
| Temporal | `127.0.0.1:7233` | 持久化工作流 |
| Redis | `127.0.0.1:6379` | 后续缓存与协调 |
| MinIO API / Console | `http://127.0.0.1:9000` / `http://127.0.0.1:9001` | 原文件对象存储，需显式配置适配器 |
| Qdrant HTTP / gRPC | `http://127.0.0.1:6333` / `127.0.0.1:6334` | 语义投影，生产接线待完成 |
| Neo4j HTTP / Bolt | `http://127.0.0.1:7474` / `127.0.0.1:7687` | 故事图投影服务，需配置 `NEO4J_PASSWORD` |

端口可以通过 `.env` 中对应的 `*_PORT` Compose 变量覆盖。停止容器使用 `make infra-down`；该命令保留命名卷数据。

## 验证层级

不依赖外部服务的默认验证包括单元测试、API/Compose 契约测试、静态检查和类型检查：

```bash
make verify
```

真实外部验证要求 PostgreSQL 和 Temporal 已启动并已迁移：

```bash
make infra-core
make migrate
make verify-external
```

`Makefile` 中的环境变量赋值使用 POSIX shell 语法；Windows 上请在 Git Bash 或 WSL 中运行上述 `make` 命令。若使用原生 PowerShell，可直接执行：

```powershell
$env:RUN_EXTERNAL_TESTS = "1"
uv run pytest -v
Remove-Item Env:RUN_EXTERNAL_TESTS
```

`verify-external` 会设置 `RUN_EXTERNAL_TESTS=1`，执行真实数据库租户隔离、Outbox 原子性，以及完整的 API → Temporal Worker → Activity → PostgreSQL → API smoke。smoke 固定使用请求 ID `smoke-1`，重复请求必须仍只有一个项目、一个默认 `main` 分支和一个 `project.created.v1` 事件。没有该显式开关时，外部 smoke 会被跳过，不能被报告成端到端通过。

CI 使用提交的 `uv.lock`，仅启动 PostgreSQL 与 Temporal，执行 Alembic `upgrade → downgrade → upgrade`，然后运行 Ruff、mypy 和包括外部 smoke 在内的完整 pytest。无论成功失败，CI 都会清理容器和卷。

## 常见故障

- `connection refused 127.0.0.1:7233`：Temporal 尚未完成初始化；查看 `docker compose logs temporal`。
- `connection refused 127.0.0.1:5432`：确认 PostgreSQL 健康并执行过 `make migrate`。
- `/ready` 返回 `503`：检查响应中 PostgreSQL/Temporal 的独立状态；响应不会泄露连接串和原始异常。
- `/ready` 返回 `200 degraded`：通常是 Redis、MinIO、Qdrant 或 Neo4j 不可用；权威写入仍可继续，投影可稍后重建。
- 业务 API 返回 `401`：确认 OIDC Issuer 可达，JWT 的 `iss`、`aud`、`exp`、`sub`、`tenant_id` 正确。
- 端口被占用：在 `.env` 中修改 `POSTGRES_PORT`、`TEMPORAL_PORT` 等映射，并同步应用连接配置。

## 验证债务与后续工作

### 资料分析与人物提取预览

`POST /api/v1/projects/{project_id}/sources/{source_id}/analysis-preview` 对当前租户下已完成
解析与分块的资料生成待确认预览。请求无需正文，使用与其他业务 API 相同的 Bearer Token。
结果包括所读范围的梗概、人物候选与性格依据；每条结论区分原文明示、自述/他人评价和模型推断，
并附原文引用。引用一致性校验不保证解读正确，置信度是模型自评，仍需人工确认。

首版从开头读取连续权威 section，最多 24,000 字符和 200 个 section，以完整段落为优先，
超长段落保守寻找完整句末，避免拆开引述与说话人说明。输入使用原文精确切片，
不重复拼接重叠的检索 chunk。响应标明覆盖范围及 `partial`，不将局部分析称为全文分析。
原始 PDF 的阅读顺序等解析质量问题不能由分片校验解决。

配置 `ANALYSIS_PROVIDER=deepseek`、`ANALYSIS_BASE_URL=https://api.deepseek.com`、
`ANALYSIS_MODEL`、`ANALYSIS_API_KEY` 和 `ANALYSIS_TIMEOUT_SECONDS`；模型名由部署方选择，
Embedding 配置保持独立。未设置模型名或密钥时，基础 API 正常启动，分析请求明确返回未配置状态。
部署后一次分析请求会调用模型，可能产生供应商费用；本轮开发验证只使用离线替身。

`analysis-preview` 本身仍不保存结果；需要留存时使用下方分析记录接口。当前能力不写入正式人物设定或 Neo4j，也没有前端编辑确认页面。
全文分批分析已实现：`POST .../analysis-jobs` 异步启动分批分析作业，`GET .../analysis-jobs/{job_id}`
查看进度，`POST .../analysis-jobs/{job_id}/retry` 在失败后重试。作业由 Temporal workflow 串行执行
每个批次（claim → analyze → complete），完成后调用 finalize 合并批结果并写入审核历史。恢复时
自动跳过已成功的批次。跨段人物消歧通过批次合并实现；事件/关系/风格提取和生成大纲仍为后续业务功能。

### 分析记录与人物审核

使用 `POST /api/v1/projects/{project_id}/sources/{source_id}/analyses` 生成并保存分析。
请求无需正文；服务端调用分析能力，保存前再次核对来源版本。每次成功调用创建一条新记录，
不提供自动重试或幂等保证。原 `analysis-preview` 接口仍用于不保存的预览。

`GET .../analyses` 分页查看历史，`GET .../analyses/{analysis_id}` 查看原始分析及最新审核结果。
人物卡由服务端分配稳定 ID，可通过 `PATCH .../analyses/{analysis_id}/characters/{character_id}`
提交预期审核版本、接受/拒绝/待审核决定，以及人工修订后的姓名、别名、描述和性格。
模型原始结果与引用保持不变，人工文本作为独立修订保存；接受表示审核决定，不自动成为故事设定事实。

来源重新解析或不再可用时，旧记录在原租户的有效项目内仍可查看，但会标记过期并禁止继续审核。
并发审核若版本不匹配，返回冲突，需刷新后重新决定。记录保存及审核需要先应用迁移 0008；全文分析
作业需要迁移 0009。本轮只生成离线迁移 SQL，未执行真实数据库升级。页面和 Neo4j 写入仍未包含。

### 故事圣经：角色、世界观与伏笔

故事圣经是项目的设定权威注册表，每个项目一个，首次访问时惰性创建。包含三类实体，
每类均有 append-only 修订历史和乐观并发控制（`expected_revision` 冲突检测）。

**角色**（Characters）：
- `GET /api/v1/projects/{project_id}/story-bible` 查看故事圣经元信息。
- `GET/POST .../story-bible/characters` 列表与创建角色。创建时使用 NFKC + casefold 生成
  `character_key` 防重复；`name`、`aliases`、`description`、`traits` 均有长度约束。
- `GET/PATCH .../story-bible/characters/{character_id}` 查看与修改角色。PATCH 需传
  `expected_revision`，修改后 `current_revision` 递增并写入修订历史。
- `POST .../sources/{source_id}/analyses/{analysis_id}/characters/{character_id}/promote`
  从分析审核历史中晋升已 `accepted` 的人物到故事圣经。晋升会检查分析是否过期、人物决定
  是否为 accepted、`character_key` 是否重复。

**世界观条目**（World Entries）：
- `GET/POST .../story-bible/world-entries` + `GET/PATCH .../{entry_id}`
- 支持五种类型：`location`、`organization`、`rule`、`item`、`other`。

**伏笔**（Foreshadowing）：
- `GET/POST .../story-bible/foreshadowing` + `GET/PATCH .../{foreshadowing_id}`
- `POST .../{foreshadowing_id}/resolve` 将伏笔从 `planted` 标记为 `resolved`（单向不可逆），
  需提供 `resolution` 文本。

所有路由使用 Bearer Token 鉴权，通过 `Principal` 获取 `tenant_id` 进行租户隔离。
迁移 0010 创建 7 张表（`story_bibles` + 3 对主表/修订表）并为 4 张主表各创建
BEFORE UPDATE OR DELETE 触发器保护不可变字段。测试文件受根 `.gitignore` 的
`test_*.py` 规则限制，不在 `git status` 中显示。

### 故事图：人物关系、事件与 Neo4j 投影

故事图在故事圣经之上建立人物之间的关系和叙事事件，并提供 Neo4j 图投影同步。

**人物关系**（Relations）：
- `GET/POST .../story-bible/relations` + `GET/PATCH .../relations/{relation_id}`
- 关系是有向的（`from_character_id` → `to_character_id`），使用 `relation_type` + 双端角色
  生成 `relation_key` 防重复；禁止自环关系。
- PATCH 需传 `expected_revision`，修改后 `current_revision` 递增并写入 append-only 修订历史。

**事件**（Events）：
- `GET/POST .../story-bible/events` + `GET/PATCH .../events/{event_id}`
- 事件包含标题、描述、可选发生时间/地点，以及参与者列表（`character_id` + `role` +
  `order_index`）；禁止重复参与者。
- PATCH 需传 `expected_revision`，支持更新参与者列表。

**图查询与投影同步**：
- `GET .../projects/{project_id}/graph` 返回权威图视图（人物节点、事件节点、关系边、参与边），
  直接从 PostgreSQL 读取，离线可用。
- `POST .../story-bible/graph/sync` 将权威图投影到 Neo4j（先删后写）。未配置 Neo4j 时返回 503
  `graph_unconfigured`；投影失败返回 503 `graph_projection_{code}`。

PostgreSQL 是唯一权威源；Neo4j 是通过事件构建、可重建的派生投影。Neo4j 驱动采用抽象 transport
+ 惰性导入 + fake 测试模式（与 Qdrant 适配器一致）。迁移 0011 创建 4 张表
（`story_bible_relations` + 修订表、`story_bible_events` + 修订表）和 2 个 guard 触发器。
测试文件同样受根 `.gitignore` 的 `test_*.py` 规则限制。

### 搜索运行时的显式接线

部署方可调用 `create_app(settings, embedding_provider=embedding, search_projection=projection)`，
由 API lifespan 使用自身的数据库 session factory 组合 `SqlAlchemySearchAuthority` 和
`SourceSearchService`。两个适配器须同时提供且 EmbeddingIdentity 完全一致；不能再同时传入
`source_search_service`。适配器由调用方管理生命周期，API 不负责关闭它们。
构造与启动阶段不调用嵌入或向量搜索；实际查询仍经过 PostgreSQL 的项目权限、投影状态与引用校验。

不传适配器时仍返回 `search_not_configured`。这只是搜索侧的组合入口，默认启动命令尚不会
创建向量供应商客户端、投影 worker 或重建任务。DeepSeek 已有资料分析适配器，写作生成入口
尚待实现；密钥与模型名由使用者后续配置，Embedding 供应商独立选择。

本机未运行的真实服务测试必须如实记录在 [VALIDATION_DEBT.md](VALIDATION_DEBT.md) 中。CI 工作流提供自动化执行环境，但只有一次真实 CI 运行成功后，相关发布门禁才算关闭。

### 投影 worker 的显式接线

部署入口可在异步函数中使用以下组合；`embedding` 和 `projection` 是调用方已构造的真实适配器，
二者必须使用相同的 EmbeddingIdentity：

```python
from novel_agent.outbox.runtime import projection_runtime

async with projection_runtime(
    settings, embedding_provider=embedding, projection=projection, owner="projection-worker-1"
) as runner:
    processed = await runner.run_once()
```

上下文组合 PostgreSQL 权威读取、投影 checkpoint 与 Outbox runner，并负责关闭自己的数据库引擎；
Embedding 和向量适配器仍由调用方关闭。进入上下文不会认领事件或调用模型；只有 `run_once()`
才执行一次投递尝试，没有待处理事件时返回 `False`。异常与租约丢失按既有 runner 语义向调用方传播。
此入口不启动常驻轮询、不自动创建 Qdrant collection，也不替代 Temporal ingestion worker。
需要持续处理时，可在同一上下文内调用轮询函数：

```python
import asyncio
from novel_agent.outbox.runtime import poll_projection

stop_event = asyncio.Event()
async with projection_runtime(
    settings, embedding_provider=embedding, projection=projection, owner="projection-worker-1"
) as runner:
    await poll_projection(runner, stop_event=stop_event, poll_interval_seconds=1.0)
```

部署方在停止时设置 `stop_event`。轮询按顺序处理事件；空队列或租约丢失后等待指定间隔，
停止事件可立即唤醒这段等待。有积压任务时也会让出事件循环。停止不会中断当前投递，
但结束后不再认领下一条；强制取消及其他异常向外传播，由运行时上下文清理引擎。
这不是强制停止超时保证：正在执行的外部 I/O 仍需供应商设置超时。
OS 信号绑定、供应商配置和真实服务恢复验证仍待后续实现。

 当前阶段是 `outline-generation`：故事圣经（角色、世界观、伏笔）、故事图（人物关系、事件、Neo4j 投影同步）和大纲生成已实现并通过离线测试；真实 Neo4j 联调、写作记忆、章节生成仍为后续工作。本地定向测试通过不等同于真实服务端到端链路通过。

### 大纲生成

大纲管线从故事圣经和故事图聚合上下文，通过独立 LLM 调用生成结构化小说大纲，持久化到 PostgreSQL 并支持人工修订。

**大纲 API**：
- `POST /api/v1/projects/{project_id}/outline/generate`（body 可选 `target_chapters`，默认 10）
  聚合角色/世界观/伏笔/风格画像/事件/关系上下文 → 调用大纲 Provider 生成 JSON →
  解析后创建或重建 `story_outlines` 与 `story_outline_chapters`（每次生成递增 outline revision 并重写全部章节）。
- `GET .../outline` 查看大纲元信息（title/premise/target_chapters/current_revision）。
- `PATCH .../outline` 修改大纲元信息，需传 `expected_revision`，成功后 `current_revision` 递增并写入修订历史。
- `GET .../outline/chapters` 列出全部章节（按 `order_index` 排序）。
- `PATCH .../outline/chapters/{chapter_id}` 修改单个章节（title/summary/key_events/notes），需传 `expected_revision`。

错误码：未配置模型或密钥返回 503（`outline_not_configured`）；Provider 调用失败或输出无效返回 502；修订冲突 409；大纲/章节不存在 404。

大纲 Provider 支持 `outline_model`/`outline_api_key`/`outline_base_url` 独立配置，
未设置时 fallback 到 `analysis_*` 配置。迁移 0013 创建 `story_outlines`、
`story_outline_revisions`、`story_outline_chapters`、`story_outline_chapter_revisions` 四张表，
含 2 个 guard 触发器。测试文件受根 `.gitignore` 的 `test_*.py` 规则限制。

### 章节生成

章节管线基于已生成的大纲，逐章调用 LLM 生成正文。上下文 = 大纲（标题/梗概/关键事件/备注）
+ 前章摘要 + 故事设定（角色/世界观/伏笔/风格画像/事件/关系），单章独立生成，情节通过
前章摘要保持连贯。

**章节 API**（独立前缀 `/chapters`，用整数 `order_index` 定位，避免与大纲章节的 UUID 路径冲突）：
- `POST /api/v1/projects/{project_id}/chapters/{order_index}/generate`
  生成单章：校验大纲存在且 `order_index` 在大纲范围内 → 聚合上下文 → 调用章节 Provider →
  解析后 upsert `story_chapters`（同一 order_index 重复生成会更新并递增 revision）。
- `GET .../chapters` 列出已生成章节。
- `GET .../chapters/{order_index}` 查看单章内容。
- `PATCH .../chapters/{order_index}` 修改单章（title/content/summary），需传 `expected_revision`。

错误码：未配置模型或密钥返回 503（`chapter_not_configured`）；Provider 调用失败或输出无效返回 502；
大纲不存在 404；order_index 超出大纲范围 400；修订冲突 409；章节不存在 404。

章节 Provider 支持 `chapter_model`/`chapter_api_key`/`chapter_base_url` 独立配置，
未设置时 fallback 到 `analysis_*` 配置。迁移 0014 创建 `story_chapters` 与
`story_chapter_revisions` 两张表，含 1 个 guard 触发器。测试文件受根 `.gitignore` 的
`test_*.py` 规则限制。

### 写作记忆

写作记忆管线从已生成的章节正文中自动提取关键事实（角色、世界观、情节、伏笔、关系），
累积成可查询、可修订的项目级记忆库，供后续章节创作保持设定一致性。记忆归属于大纲
（每个项目一条大纲），对同章重复提取采用幂等策略：先将旧同章 active 条目标记为
`superseded`，再插入新条目，避免记忆无限膨胀。

**记忆 API**：
- `POST /api/v1/projects/{project_id}/memories/extract`（body 必传 `chapter_index`）
  对指定章节执行记忆提取。服务端加载章节正文，聚合角色（story bible）与已有记忆去重锚点，
  调用记忆 Provider 生成关键事实列表，持久化 `story_memories`（active）并写入修订历史。
  未配置模型或密钥返回 503；Provider 调用失败或输出无效返回 502；order_index 超出范围 400；
  章节不存在 404。
- `GET .../memories` 列出当前大纲的记忆，支持按 `status`（active/superseded）过滤。
- `GET .../memories/{memory_id}` 查看单条记忆。
- `PATCH .../memories/{memory_id}` 修改记忆内容（`category`/`content`），需传
  `expected_revision`，成功后 `current_revision` 递增并写入 append-only 修订历史。

记忆 `category` 取值：`character`（角色）、`world`（世界观）、`plot`（情节）、
`foreshadowing`（伏笔）、`relation`（关系）；`status` 取值 `active`/`superseded`。
错误码：未配置 503（`memory_not_configured`）；Provider 失败/输出无效 502
（`memory_unavailable`/`memory_invalid_output`）；order_index 越界 400；章节/记忆不存在 404；
修订冲突 409。

记忆 Provider 支持 `memory_model`/`memory_api_key`/`memory_base_url` 独立配置，
未设置时 fallback 到 `analysis_*` 配置。迁移 0015 创建 `story_memories` 与
`story_memory_revisions` 两张表（复合外键 → `story_outlines`），含 1 个 guard 触发器。
测试文件受根 `.gitignore` 的 `test_*.py` 规则限制。

### 事件/关系/风格提取

提取管线在分析历史之上构建，通过独立 LLM 调用从已有分析结果中自动提取事件、人物关系和叙事风格，写入故事图和故事圣经。

**提取 API**：
- `POST /api/v1/projects/{project_id}/sources/{source_id}/analyses/{analysis_id}/extract`
  对指定分析记录执行提取。服务端加载分析详情，构建角色名→ID 映射（NFKC + casefold 归一化），
  调用提取 Provider 生成事件/关系/风格的 JSON，解析后写入故事图（events/relations）和故事圣经（style profile）。
  返回提取摘要（`ExtractionSummary`）：已提取事件数、已提取关系数、风格是否更新、未匹配角色名列表、
  跳过的事件/关系数。未配置模型或密钥时返回 503；Provider 输出无效时返回 502。

**风格画像 API**：
- `GET /api/v1/projects/{project_id}/story-bible/style` 查看当前项目的叙事风格画像。
- `PATCH .../story-bible/style` 修改风格画像，需传 `expected_revision`，修改后 `current_revision` 递增并写入修订历史。

提取 Provider 支持 `extraction_model`/`extraction_api_key`/`extraction_base_url` 独立配置，
未设置时 fallback 到 `analysis_*` 配置。迁移 0012 创建 `story_bible_style_profiles` 主表和修订表，
含 guard 触发器。测试文件受根 `.gitignore` 的 `test_*.py` 规则限制。

### 全文分批分析

`POST /api/v1/projects/{project_id}/sources/{source_id}/analysis-jobs` 对当前租户下已完成解析与
分块的资料启动全文分批分析。请求无需正文，使用与其他业务 API 相同的 Bearer Token。服务端规划
批次、创建作业记录并异步调度 Temporal workflow，返回 202 和初始状态。

`GET .../analysis-jobs/{job_id}` 返回作业的当前状态（queued/running/succeeded/failed）、批次进度
和错误码。`queued` 表示作业已创建但 workflow 尚未开始执行；`POST .../analysis-jobs/{job_id}/retry` 
在作业失败后请求重试，仅当作业处于终态失败时允许；返回 202。

Workflow 串行处理每个批次：领取批次（claim）→ 调用模型分析（analyze）→ 写入批次结果（complete）。
批次失败标记 `failed` 状态码并终止作业（Temporal 最多重试 4 次后终止）。全部批次成功后调用 `finalize` 合并所有批次结果并写入
人工审核历史记录，生成一条与单次分析记录格式相同的 `SourceAnalysisHistory`。

恢复行为：workflow 重新执行时，`next_batch_index` 跳过已 `succeeded` 的批次，从下一个 `pending`
批次继续。如果作业已处于终态（succeeded/failed），`next_batch_index` 返回 409 错误。

本轮实现仅通过离线 fake 验证：单元测试覆盖 workflow 确定性、串行执行、批次跳过、失败码映射；
集成测试覆盖 API 路由的鉴权、错误映射和状态转换。真实 PostgreSQL、Temporal、模型联调仍待完成。
迁移 0009 的 upgrade/downgrade SQL 已离线生成，未执行真实数据库升级。测试文件受根 `.gitignore`
的 `test_*.py` 规则限制，不在 `git status` 中显示。

> AI生成