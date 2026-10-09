---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '649acdc1-1970-470a-a95b-0ba667bec105'
  PropagateID: '649acdc1-1970-470a-a95b-0ba667bec105'
  ReservedCode1: 'a7da2dbc-158c-4457-bd58-63d01bef2abe'
  ReservedCode2: 'a7da2dbc-158c-4457-bd58-63d01bef2abe'
---

# Phase 6 设计规格：写作记忆

## 目标

从已生成的章节正文中自动提取关键事实（谁在哪、发生什么、伏笔变化、人物关系变化），
累积成**写作记忆库**。后续章节生成时自动携带相关记忆，保证长篇情节一致性、
避免重复描述和前后矛盾。

## 核心概念

- **记忆条目（Memory Item）**：一条可追溯来源章节的关键事实，带类别与状态。
- **提取**：章节生成后（或人工触发），调用 LLM 从正文提取事实，写入记忆库。
- **携带**：后续章节生成的上下文聚合中，自动带上与当前章相关的记忆摘要
  （Phase 5 ChapterService 后续增强点，本轮先建库，接入留到下一步）。

## 数据模型

### 表结构

**`story_memories`**（N per outline）
- `id` UUID PK
- `tenant_id`, `project_id`, `outline_id` UUID（复合外键 → story_outlines）
- `chapter_index` int — 来源章节序号
- `category` varchar(30) — `character` | `world` | `plot` | `foreshadowing` | `relation`
- `content` varchar(2000) — 事实描述（LLM 提取或人工修订）
- `status` varchar(20) — `active` | `superseded`（后续章节推翻/修正旧事实）
- `current_revision` int — 乐观并发
- `created_by_subject` varchar(255)
- `created_at`, `updated_at` timestamptz
- Unique: `(tenant_id, project_id, outline_id, id)`
- FK: `(tenant_id, project_id, outline_id)` → story_outlines

**`story_memory_revisions`**（append-only）
- `id` UUID PK
- `tenant_id`, `project_id`, `outline_id`, `memory_id`（复合外键 → story_memories）
- `revision` int
- `snapshot` JSONB（{category, content, status, chapter_index}）
- `subject` varchar(255)
- `created_at` timestamptz
- Unique: `(tenant_id, project_id, outline_id, memory_id, revision)`

### Guard 触发器

主表一个 BEFORE UPDATE OR DELETE 触发器：保护不可变字段
（id, tenant_id, project_id, outline_id, chapter_index, created_at, created_by_subject）。

## Pydantic Schema

```python
# memory.py — LLM 输出
class MemoryItem(StrictModel):
    category: Literal["character", "world", "plot", "foreshadowing", "relation"]
    content: str (1-500)

class MemoryResult(StrictModel):
    items: list[MemoryItem]  # 1-100 条

# Service 层模型
class MemoryPatch(StrictModel):
    expected_revision: int
    content: str | None = None
    status: Literal["active", "superseded"] | None = None
    # reject_explicit_null
```

## Provider 协议

```python
class MemoryProvider(Protocol):
    provider: str
    model: str
    async def generate(self, context_json: dict) -> str: ...
    async def aclose(self) -> None: ...

class FakeMemoryProvider: ...
class DeepSeekMemoryProvider:
    # 独立 config：memory_model/memory_api_key/memory_base_url, fallback to analysis_*
    # 错误码：memory_not_configured(503), memory_unavailable(502), memory_invalid_output(502)
```

## MemoryService

```python
class MemoryService:
    def __init__(self, session_factory, story_bible, story_graph, outline, chapters, provider): ...

    async def extract_memory(self, scope, subject, chapter_index) -> dict:
        """1. 校验 outline + chapter_index 范围
        2. 读章节正文（story_chapters）+ 已有记忆 + 故事设定
        3. provider.generate(context_json)
        4. 解析 MemoryResult
        5. 写入 story_memories（每章一次提取，先标记旧同章记忆 superseded，再插入新条目）
        6. 返回本次提取摘要"""
    async def list_memories(self, scope, status: str | None = None) -> dict: ...
    async def get_memory(self, scope, memory_id) -> dict | None: ...
    async def patch_memory(self, scope, memory_id, patch, subject) -> dict: ...
    async def aclose(self) -> None: ...
```

### 提取上下文

```python
context = {
    "chapter": {"order_index", "title", "content"},  # 待提取正文
    "characters": [...],   # 角色名/设定（实体锚点）
    "world_entries": [...],
    "foreshadowing": [...],
    "existing_memories": [{"category", "content", "status"}],  # 供去重/状态判断
}
```

### 行为约束
- 大纲不存在 → `outline_not_found(404)`
- chapter_index 超出范围 → `chapter_out_of_range(400)`
- 章节正文不存在 → `chapter_not_found(404)`
- 提取幂等：同章重复提取会把该章旧条目标记 superseded，再插入新条目
- 人工 PATCH 可修改 content/status，递增 revision，写入 revision 表

## API 路由

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/projects/{pid}/memories/extract`（body: chapter_index） | 提取章节记忆 |
| GET | `/api/v1/projects/{pid}/memories`（可选 ?status=active） | 列出记忆 |
| GET | `/api/v1/projects/{pid}/memories/{memory_id}` | 查看单条 |
| PATCH | `/api/v1/projects/{pid}/memories/{memory_id}` | 修订记忆 |

## 配置

```python
memory_model: str = ""
memory_api_key: SecretStr | None = None
memory_base_url: str = ""
```

## 迁移

`0015_memories.py` — 2 张表 + 1 个 guard 触发器

## TDD 任务分解

| Task | 内容 | 预计测试数 |
|------|------|-----------|
| 1 | MemoryResult schema | ~10 |
| 2 | MemoryProvider 协议 + Fake/DeepSeek | ~13 |
| 3 | Memory ORM + 迁移 0015 | ~10 |
| 4 | MemoryService | ~18 |
| 5 | API 路由 | ~12 |
| 6 | main.py 接线 + config.py | — |
| 7 | 全量回归 + Ruff + Mypy + 文档 | — |

## 后续（不在本轮）
- ChapterService 生成章节时自动携带记忆上下文
- 前端"记忆库"页面（查看/搜索/手工补录）

> AI生成