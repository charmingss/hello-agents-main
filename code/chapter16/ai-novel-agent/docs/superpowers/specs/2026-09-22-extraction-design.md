---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'a4ad8512-15e5-4c46-b0f5-6a2925415101'
  PropagateID: 'a4ad8512-15e5-4c46-b0f5-6a2925415101'
  ReservedCode1: '5b25a0f1-5328-4ae1-9ff4-3d10b0858075'
  ReservedCode2: '5b25a0f1-5328-4ae1-9ff4-3d10b0858075'
---

# Phase 3 Design Spec: 事件/关系/风格提取

> 日期: 2026-09-22
> 状态: DRAFT — 待用户确认
> 前序: Phase 1 (Story Bible) + Phase 2 (Neo4j Story Graph) 已完成

## 1. 问题陈述

当前分析链路（单次分析 + 全文分析）只从原文中提取两类信息：
- `summary`: 主张列表（Claim 数组，含证据引用）
- `characters`: 角色列表（含别名、描述、特质、证据）

Story Graph 已支持关系（`story_bible_relations`）和事件（`story_bible_events`）的 CRUD，Story Bible 已支持世界观条目和伏笔的 CRUD，但**所有写入都依赖人工通过 API 录入**。不存在从分析结果中自动提取事件、人物关系和叙事风格的逻辑。

Phase 3 的目标：**从已有的分析结果中自动提取事件、人物关系和叙事风格，写入 Story Graph 和 Story Bible**。

## 2. 设计决策

### 2.1 提取方式：独立提取管线（不改动现有分析 prompt）

**决策**：新建独立的 `ExtractionService`，接收一个已完成的 `source_analysis_records` 记录 + 对应的原文段落，通过**独立的 LLM 调用**提取事件/关系/风格。

**理由**：
- 现有分析 prompt 已经稳定且经过严格验证（名字必须在证据中逐字出现等），改动风险高
- 独立管线可以单独迭代 prompt 而不影响已有分析质量
- 可以对已有的历史分析结果做"回溯提取"，不需要重新分析
- `source_analysis_records.original` 的 DB CHECK 约束只放行 `summary` + `characters`，不改动约束

### 2.2 触发方式：同步 API（不引入 Temporal）

**决策**：
- 提供 `POST /api/v1/projects/{project_id}/sources/{source_id}/analyses/{analysis_id}/extract` 同步端点
- 提取结果直接写入 Story Graph + Story Bible，返回提取摘要
- 不引入 Temporal workflow（避免增加基础设施依赖），LLM 调用超时由 httpx 控制

**理由**：提取是对单个分析结果的操作，不像全文分析需要分批处理；单次 LLM 调用应在 30 秒内完成。

### 2.3 风格存储：新增 `story_bible_style_profiles` 表

**决策**：新增一张表存储叙事风格画像，每个项目一份（1:1 关系到 Story Bible）。

### 2.4 角色匹配：基于名字 + 别名的归一化匹配

**决策**：提取出的事件/关系中的角色引用，通过 NFKC + casefold 归一化后的名字/别名匹配到 Story Bible 中已有的角色。匹配失败的候选角色返回为 `unmatched`，不自动创建。

## 3. 数据模型

### 3.1 提取输出 Schema（`ExtractionResult`）

```python
class ExtractionResult(StrictModel):
    events: list[ExtractedEvent]       # 最多 50
    relations: list[ExtractedRelation] # 最多 50
    style: ExtractedStyle | None       # 可选
```

#### ExtractedEvent

```python
class ExtractedEvent(StrictModel):
    title: str           # 1..200
    description: str      # 1..2000
    occurred_at: str | None    # ISO 8601 或 None（叙事时间，非真实时间）
    location: str | None       # 1..500
    participants: list[ExtractedParticipant]  # 1..100

class ExtractedParticipant(StrictModel):
    character_name: str   # 1..120 — 原文中的角色名引用
    role: str | None       # 1..50 — 在事件中的角色（如"主角""旁观者"）
```

#### ExtractedRelation

```python
class ExtractedRelation(StrictModel):
    relation_type: str    # 1..50 — 如"师徒""敌对""恋人"
    from_character_name: str   # 1..120
    to_character_name: str     # 1..120
    description: str          # 1..500
```

#### ExtractedStyle

```python
class ExtractedStyle(StrictModel):
    narrative_voice: str | None    # 1..200 — 叙事视角（如"第三人称限知"）
    tense: str | None              # 1..50 — 时态（如"过去时"）
    pacing: str | None             # 1..200 — 节奏描述
    tone: str | None               # 1..200 — 基调描述
    vocabulary_level: str | None   # 1..200 — 用词风格
    sentence_structure: str | None # 1..200 — 句式特征
    notes: str | None              # 1..2000 — 其他风格备注
```

### 3.2 新增 ORM 模型：`story_bible_style_profiles`

迁移 0012。

```sql
CREATE TABLE story_bible_style_profiles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL,
    project_id      UUID NOT NULL,
    story_bible_id  UUID NOT NULL,
    narrative_voice      VARCHAR(200),
    tense                VARCHAR(50),
    pacing               VARCHAR(200),
    tone                 VARCHAR(200),
    vocabulary_level     VARCHAR(200),
    sentence_structure   VARCHAR(200),
    notes                VARCHAR(2000),
    current_revision     INTEGER NOT NULL DEFAULT 0,
    created_by_subject   VARCHAR(255) NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_style_bible
        FOREIGN KEY (tenant_id, project_id, story_bible_id)
        REFERENCES story_bibles (tenant_id, project_id, id)
        ON DELETE CASCADE,
    CONSTRAINT uk_style_per_project UNIQUE (tenant_id, project_id)
);
```

加 1 个 guard 触发器（防止直接 DELETE/UPDATE，走修订流程）。

### 3.3 风格修订表：`story_bible_style_profile_revisions`

```sql
CREATE TABLE story_bible_style_profile_revisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL,
    project_id      UUID NOT NULL,
    style_profile_id UUID NOT NULL,
    revision        INTEGER NOT NULL,
    snapshot        JSONB NOT NULL,
    subject         VARCHAR(255) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_style_rev
        FOREIGN KEY (tenant_id, project_id, style_profile_id)
        REFERENCES story_bible_style_profiles (tenant_id, project_id, id)
        ON DELETE CASCADE
);
```

## 4. 服务层设计

### 4.1 `ExtractionService`（新建 `analysis/extraction.py`）

```python
class ExtractionService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        analysis_history: SourceAnalysisHistoryService,
        story_bible: StoryBibleService,
        story_graph: StoryGraphService,
        provider: ExtractionProvider,
    ): ...

    async def extract(
        self,
        scope: TenantProjectScope,
        source_id: UUID,
        analysis_id: UUID,
        subject: str,
    ) -> ExtractionSummary: ...
```

#### `extract()` 流程

1. 从 `source_analysis_records` 加载分析结果（`original` JSON）
2. 从 `source_sections` 加载对应的原文段落（供 LLM 参考）
3. 组装 prompt（分析结果摘要 + 原文片段 → 要求提取事件/关系/风格）
4. 调用 `ExtractionProvider.generate()` 获取 JSON
5. 验证输出（`ExtractionResult` schema）
6. **角色匹配**：对每个事件/关系中的 `character_name`，在 Story Bible 中查找匹配角色
7. **写入事件**：匹配到参与人的事件 → `StoryGraphService.create_event()`
8. **写入关系**：两端都匹配到的关系 → `StoryGraphService.create_relation()`
9. **写入风格**：如有 style → upsert `story_bible_style_profiles`
10. 返回 `ExtractionSummary`

#### ExtractionSummary

```python
class ExtractionSummary(BaseModel):
    analysis_id: UUID
    events_extracted: int
    relations_extracted: int
    style_updated: bool
    unmatched_characters: list[str]  # 未匹配到 Story Bible 的角色名
    skipped_events: int              # 因参与人未匹配而跳过的事件
    skipped_relations: int           # 因角色未匹配而跳过的关系
```

### 4.2 `ExtractionProvider`（新建协议）

```python
class ExtractionProvider(Protocol):
    provider: str
    model: str

    async def generate(
        self,
        analysis_json: dict,
        sections: list[dict],
    ) -> str: ...  # 返回 ExtractionResult JSON 字符串

    async def aclose(self) -> None: ...
```

实现：`DeepSeekExtractionProvider`（复用 DeepSeek httpx 客户端模式，独立 prompt）。

### 4.3 风格画像服务（`StoryBibleService` 扩展）

在 `StoryBibleService` 中新增方法：
- `get_style_profile(scope) -> dict | None`
- `upsert_style_profile(scope, data, subject) -> dict`
- `patch_style_profile(scope, patch, subject) -> dict`

## 5. API 端点

### 5.1 提取端点

```
POST /api/v1/projects/{project_id}/sources/{source_id}/analyses/{analysis_id}/extract
```

- 请求体：无（empty POST，同 analysis-preview 模式）
- 响应：200 + `ExtractionSummary`
- 错误映射：
  - 404 `analysis_not_found` — 分析记录不存在
  - 404 `story_bible_not_found` — 项目无 Story Bible
  - 502 `extraction_invalid_output` — LLM 输出无法解析
  - 503 `extraction_unconfigured` — 未配置提取 Provider
  - 503 `analysis_not_configured` — 分析 Provider 未配置

### 5.2 风格画像端点

```
GET   /api/v1/projects/{project_id}/story-bible/style
PATCH /api/v1/projects/{project_id}/story-bible/style
```

- GET：返回当前风格画像，不存在返回 404 `style_not_found`
- PATCH：upsert 风格画像（如果不存在则创建），需要 `expected_revision`（乐观锁）

## 6. 配置

`config.py` 新增：
```python
extraction_model: str = ""          # 空则用 analysis_model
extraction_api_key: str | None = None  # 空则用 analysis_api_key
extraction_base_url: str = ""       # 空则用 analysis_base_url
```

如果 `extraction_api_key` 为空且 `analysis_api_key` 也为空 → 503 `extraction_unconfigured`。

## 7. 验证策略（TDD）

| 测试文件 | 测试内容 | 数量（估） |
|---------|---------|-----------|
| `test_extraction_schema.py` | ExtractionResult/Event/Relation/Style schema 验证 | 8 |
| `test_extraction_provider.py` | FakeExtractionProvider + DeepSeekExtractionProvider 协议 | 6 |
| `test_extraction_service.py` | ExtractionService 全链路（fake provider + fake session） | 18 |
| `test_style_profile_database.py` | ORM 模型 + 迁移契约 | 5 |
| `test_style_profile_service.py` | StoryBibleService 风格方法 | 8 |
| `test_extraction_api.py` | API 端点（auth + error mapping + wiring） | 12 |
| 合计 | | ~57 |

## 8. 文件清单

### 新建
- `src/novel_agent/analysis/extraction.py` — ExtractionService + schema
- `src/novel_agent/analysis/extraction_provider.py` — ExtractionProvider 协议 + DeepSeek 实现
- `src/novel_agent/api/routes/extraction.py` — 提取 API 路由
- `src/novel_agent/db/models/style_profile.py` — StyleProfile ORM
- `migrations/versions/0012_style_profiles.py` — 迁移
- `tests/unit/test_extraction_schema.py`
- `tests/unit/test_extraction_provider.py`
- `tests/unit/test_extraction_service.py`
- `tests/unit/test_style_profile_database.py`
- `tests/unit/test_style_profile_service.py`
- `tests/integration/test_extraction_api.py`

### 修改
- `src/novel_agent/analysis/story_bible.py` — 新增风格画像方法
- `src/novel_agent/api/routes/story_bible.py` — 新增风格画像端点
- `src/novel_agent/main.py` — 注入 ExtractionService
- `src/novel_agent/config.py` — 新增 extraction 配置
- `README.md` / `VALIDATION_DEBT.md` — 文档更新

## 9. 不做的事

- 不改动现有 `Candidate` schema 或分析 prompt
- 不改动 `source_analysis_records` 的 CHECK 约束
- 不引入 Temporal workflow（同步调用）
- 不自动创建未匹配的角色（返回 unmatched 列表，由人工决定）
- 不做事件/关系去重（由 Story Graph 的 `relation_key` 和 event unique 约束处理）

> AI生成