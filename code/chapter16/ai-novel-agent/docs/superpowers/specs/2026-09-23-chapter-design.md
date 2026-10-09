---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'b0247ecf-8564-4564-b8bb-641478d36a5a'
  PropagateID: 'b0247ecf-8564-4564-b8bb-641478d36a5a'
  ReservedCode1: '7166b44e-1d6d-4d09-8624-4f8a07b3b138'
  ReservedCode2: '7166b44e-1d6d-4d09-8624-4f8a07b3b138'
---

# Phase 5 设计规格：章节生成

## 目标

基于已生成的大纲（story_outlines + story_outline_chapters），逐章调用 LLM 生成正文内容，持久化到 PostgreSQL，支持人工修订。**单章独立生成**，上下文 = 大纲 + 前章摘要 + 故事设定（角色/世界观/伏笔/风格/事件/关系）。

## 数据模型

### 表结构

**`story_chapters`**（N per outline，每大纲章节一条正文记录）
- `id` UUID PK
- `tenant_id`, `project_id`, `outline_id` UUID（复合外键 → story_outlines）
- `order_index` int — 对应大纲章节序号
- `title` varchar(200) — 从大纲章节同步
- `content` TEXT — 生成的正文
- `content_summary` varchar(2000) — 本章摘要（供后续章节作"前章摘要"上下文）
- `current_revision` int — 乐观并发控制
- `created_by_subject` varchar(255)
- `created_at`, `updated_at` timestamptz
- Unique: `(tenant_id, project_id, outline_id, chapter_index)` — 每次大纲重建后以 order_index 对齐
- FK: `(tenant_id, project_id, outline_id)` → story_outlines

**`story_chapter_revisions`**（append-only）
- `id` UUID PK
- `tenant_id`, `project_id`, `outline_id`, `chapter_id` UUID（复合外键 → story_chapters）
- `revision` int
- `snapshot` JSONB
- `subject` varchar(255)
- `created_at` timestamptz
- Unique: `(tenant_id, project_id, outline_id, chapter_id, revision)`

### Guard 触发器

主表一个 BEFORE UPDATE OR DELETE 触发器，保护不可变字段（id, tenant_id, project_id, outline_id, chapter_index, created_at, created_by_subject）。

## Pydantic Schema

```python
# chapter.py — LLM 输出
class ChapterResult(StrictModel):
    title: ShortTitle (<=200)
    content: str (>=1, <=20000)      # 正文
    summary: str (>=1, <=2000)       # 本章摘要

# Service 层模型
class ChapterPatch(StrictModel):
    expected_revision: int
    title: str | None = None
    content: str | None = None
    summary: str | None = None
    # reject_explicit_null
```

## Provider 协议

```python
class ChapterProvider(Protocol):
    provider: str
    model: str
    async def generate(self, context_json: dict[str, Any]) -> str: ...
    async def aclose(self) -> None: ...

class FakeChapterProvider:
    provider = "fake"
    model = "fake-chapter"
    # 返回固定单章 JSON

class DeepSeekChapterProvider:
    provider = "deepseek"
    # 独立 config：chapter_model/chapter_api_key/chapter_base_url, fallback to analysis_*
    # 错误码：chapter_not_configured(503), chapter_unavailable(502), chapter_invalid_output(502)
```

## ChapterService

```python
class ChapterService:
    def __init__(self, session_factory, story_bible, story_graph, outline, provider): ...

    async def generate_chapter(self, scope, subject, order_index: int) -> dict:
        """1. 校验 outline 存在且 order_index 在大纲章节范围内
        2. 聚合上下文：大纲（title/summary/key_events/notes）+ 前章摘要 + 故事设定
        3. provider.generate(context_json)
        4. 解析 ChapterResult
        5. upsert story_chapters（title/content/summary 更新，current_revision+1）
        6. 返回 chapter dict"""
    async def get_chapter(self, scope, order_index: int) -> dict | None: ...
    async def list_chapters(self, scope) -> dict: ...
    async def patch_chapter(self, scope, order_index: int, patch, subject) -> dict: ...
    async def aclose(self) -> None: ...
```

### 上下文聚合（每章）

```python
context = {
    "outline": {"title", "premise"},
    "current_chapter": {"order_index", "title", "summary", "key_events", "notes"},
    "previous_chapters": [{"order_index", "title", "summary"}],  # 已生成章节的摘要
    "characters": [...],
    "world_entries": [...],
    "foreshadowing": [...],
    "style": {...},
    "events": [...],
    "relations": [...],
}
```

### 行为约束
- 大纲不存在 → `outline_not_found(404)`
- order_index 超出大纲章节范围 → `chapter_out_of_range(400)`
- 生成的 title 会覆盖大纲章节的 title（保持一致性）；content 为空 → `chapter_invalid_output(502)`
- `generate_chapter` 幂等：同一 order_index 重复生成 → 更新现有记录并递增 revision

## API 路由

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/projects/{pid}/chapters/{order_index}/generate` | 生成单章 |
| GET | `/api/v1/projects/{pid}/chapters/{order_index}` | 查看章节内容 |
| GET | `/api/v1/projects/{pid}/chapters` | 列出章节内容 |
| PATCH | `/api/v1/projects/{pid}/chapters/{order_index}` | 修改章节 |

> 路由冲突说明：Phase 4 已占用 `/outline/chapters/{chapter_id}`（UUID 定位大纲章节）。
> Phase 5 章节内容改用独立前缀 `/chapters` + 整数 `order_index`，避免与 UUID 路径在
> Starlette 中按注册顺序匹配造成 422。

## 配置

```python
chapter_model: str = ""
chapter_api_key: SecretStr | None = None
chapter_base_url: str = ""
# 与 extraction_*/outline_* 相同 fallback 模式
```

## 迁移

`0014_chapters.py` — 2 张表 + 1 个 guard 触发器

## TDD 任务分解

| Task | 内容 | 预计测试数 |
|------|------|-----------|
| 1 | ChapterResult schema | ~12 |
| 2 | ChapterProvider 协议 + Fake/DeepSeek | ~14 |
| 3 | Chapter ORM + 迁移 0014 | ~12 |
| 4 | ChapterService | ~20 |
| 5 | API 路由 | ~14 |
| 6 | main.py 接线 + config.py | — |
| 7 | 全量回归 + Ruff + Mypy + 文档 | — |

## 后续（前端 UI，不在本轮）
- 用户要求：市面流行框架（如 Vue 3 / React），简洁、优美、非 demo 感
- 单页应用：项目选择 → 大纲查看/生成 → 章节列表 → 章节生成/阅读/编辑
- 由 FastAPI 静态挂载或独立 dev server，Token 存 localStorage
- 颜色/风格：白底、淡蓝主色（沿用既有偏好）

> AI生成