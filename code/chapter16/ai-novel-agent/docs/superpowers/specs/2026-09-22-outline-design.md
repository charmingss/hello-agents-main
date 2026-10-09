---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'b47fe906-d26f-410f-9c93-2c76d8b141cb'
  PropagateID: 'b47fe906-d26f-410f-9c93-2c76d8b141cb'
  ReservedCode1: '0d90698f-6571-48e0-b73f-b8d49c7ed4fa'
  ReservedCode2: '0d90698f-6571-48e0-b73f-b8d49c7ed4fa'
---

# Phase 4 设计规格：大纲生成

## 目标

从 Story Bible（角色/世界观/伏笔/风格）和 Story Graph（事件/关系）中聚合上下文，通过独立 LLM 调用生成结构化小说大纲，持久化到 PostgreSQL 并支持人工修订。

## 数据模型

### 表结构

**`story_outlines`**（1:1 per project）
- `id` UUID PK
- `tenant_id`, `project_id` UUID（复合外键 → projects）
- `title` varchar(200)
- `premise` varchar(2000) — 故事前提/梗概
- `target_chapters` int — 目标章节数
- `current_revision` int — 乐观并发控制
- `created_by_subject` varchar(255)
- `created_at`, `updated_at` timestamptz
- Unique: `(tenant_id, project_id)`
- FK: `(tenant_id, project_id)` → projects

**`story_outline_revisions`**（append-only）
- `id` UUID PK
- `tenant_id`, `project_id`, `outline_id` UUID（复合外键 → story_outlines）
- `revision` int
- `snapshot` JSONB
- `subject` varchar(255)
- `created_at` timestamptz
- Unique: `(tenant_id, project_id, outline_id, revision)`

**`story_outline_chapters`**（N per outline）
- `id` UUID PK
- `tenant_id`, `project_id`, `outline_id` UUID（复合外键 → story_outlines）
- `order_index` int — 章节序号
- `title` varchar(200)
- `summary` varchar(2000)
- `key_events` JSONB — 关键事件描述列表
- `notes` varchar(2000), nullable
- `current_revision` int
- `created_at`, `updated_at` timestamptz
- Unique: `(tenant_id, project_id, outline_id, order_index)`

**`story_outline_chapter_revisions`**（append-only）
- `id` UUID PK
- `tenant_id`, `project_id`, `outline_id`, `chapter_id` UUID
- `revision` int
- `snapshot` JSONB
- `subject` varchar(255)
- `created_at` timestamptz
- Unique: `(tenant_id, project_id, outline_id, chapter_id, revision)`

### Guard 触发器

两张主表各一个 BEFORE UPDATE OR DELETE 触发器，保护不可变字段（id, tenant_id, project_id, outline_id/chapter_id, order_index, created_at, created_by_subject）。

## Pydantic Schema

```python
# outline.py — 与 extraction.py 模式一致
class OutlineChapterResult(StrictModel):
    order_index: int (>=1, <=500)
    title: ShortText (<=200)
    summary: str (<=2000)
    key_events: list[str] (<=20, each <=500)
    notes: str | None (<=2000)

class OutlineResult(StrictModel):
    title: ShortText (<=200)
    premise: str (<=2000)
    chapters: list[OutlineChapterResult] (>=1, <=500)

# Service 层 Create/Patch 模型
class OutlineCreate(StrictModel):  # 内部使用，不直接暴露 API
    title: ShortText
    premise: str
    target_chapters: int
    chapters: list[ChapterCreate]

class ChapterCreate(StrictModel):
    order_index: int
    title: ShortText
    summary: str
    key_events: list[str]
    notes: str | None = None
    # reject_explicit_null validator

class OutlinePatch(StrictModel):
    expected_revision: int
    title: ShortText | None = None
    premise: str | None = None
    target_chapters: int | None = None
    # reject_explicit_null

class ChapterPatch(StrictModel):
    expected_revision: int
    title: ShortText | None = None
    summary: str | None = None
    key_events: list[str] | None = None
    notes: str | None = None
    # reject_explicit_null
```

## Provider 协议

```python
class OutlineProvider(Protocol):
    provider: str
    model: str
    async def generate(self, context_json: dict[str, Any]) -> str: ...
    async def aclose(self) -> None: ...

class FakeOutlineProvider:
    provider = "fake"
    model = "fake-outline"
    # 返回固定 3 章大纲 JSON

class DeepSeekOutlineProvider:
    provider = "deepseek"
    # 同 ExtractionProvider 模式：独立 config + fallback to analysis_*
    # 错误码：outline_not_configured(503), outline_unavailable(502), outline_invalid_output(502)
```

## OutlineService

```python
class OutlineService:
    def __init__(self, session_factory, story_bible, story_graph, provider): ...
    
    async def generate_outline(self, scope, subject, target_chapters: int = 10) -> dict:
        """1. 从 story_bible + story_graph 聚合上下文
        2. 调用 provider.generate(context_json)
        3. 解析 OutlineResult
        4. 如果已有 outline，更新；否则创建
        5. 删除旧章节，写入新章节
        6. 返回 outline dict"""
    
    async def get_outline(self, scope) -> dict | None: ...
    async def patch_outline(self, scope, patch, subject) -> dict: ...
    async def list_chapters(self, scope) -> dict: ...
    async def patch_chapter(self, scope, chapter_id, patch, subject) -> dict: ...
    async def aclose(self) -> None: ...
```

### 上下文聚合

```python
context = {
    "characters": [{"name", "aliases", "traits", "description"}],
    "world_entries": [{"name", "entry_type", "description"}],
    "foreshadowing": [{"title", "description", "status"}],
    "style": {"narrative_voice", "tone", "pacing", ...},
    "events": [{"title", "description", "participants"}],
    "relations": [{"relation_type", "from", "to", "description"}],
    "target_chapters": 10,
}
```

## API 路由

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/projects/{pid}/outline/generate` | 生成大纲（body: target_chapters 可选） |
| GET | `/api/v1/projects/{pid}/outline` | 查看大纲 |
| PATCH | `/api/v1/projects/{pid}/outline` | 修改大纲元信息 |
| GET | `/api/v1/projects/{pid}/outline/chapters` | 列出章节 |
| PATCH | `/api/v1/projects/{pid}/outline/chapters/{chapter_id}` | 修改章节 |

## 配置

```python
outline_model: str = ""
outline_api_key: SecretStr | None = None
outline_base_url: str = ""
# 与 extraction_* 相同的 fallback 模式
```

## 迁移

`0013_outlines.py` — 4 张表 + 2 个 guard 触发器

## TDD 任务分解

| Task | 内容 | 预计测试数 |
|------|------|-----------|
| 1 | OutlineResult + Chapter schema | ~20 |
| 2 | OutlineProvider 协议 + Fake/DeepSeek | ~10 |
| 3 | Outline ORM + 迁移 0013 | ~10 |
| 4 | OutlineService | ~15 |
| 5 | API 路由 | ~12 |
| 6 | main.py 接线 + config.py | — |
| 7 | 全量回归 + Ruff + Mypy + 文档 | — |

> AI生成