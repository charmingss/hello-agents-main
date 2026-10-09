---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '32b5c4df-f2a4-4e21-bd1c-9dc13246a166'
  PropagateID: '32b5c4df-f2a4-4e21-bd1c-9dc13246a166'
  ReservedCode1: '10443cb4-f25c-419f-992a-e8bb3f2a0aae'
  ReservedCode2: '10443cb4-f25c-419f-992a-e8bb3f2a0aae'
---

# Phase 3 交接文档：事件/关系/风格提取

**日期**: 2026-09-22  
**阶段**: Phase 3 — 事件/关系/风格提取  
**状态**: 全部完成，离线测试通过

## 完成状态

| Task | 描述 | 状态 | 测试数 |
|------|------|------|--------|
| Task 1 | ExtractionResult schema | 完成 | 28/28 |
| Task 2 | ExtractionProvider 协议 + Fake/DeepSeek | 完成 | 11/11 |
| Task 3 | StyleProfile ORM + 迁移 0012 | 完成 | 10/10 |
| Task 4 | StoryBibleService 风格画像方法 | 完成 | 10/10 |
| Task 5 | ExtractionService 全链路 | 完成 | 10/10 |
| Task 6 | API 路由 (extract + style) | 完成 | 12/12 |
| Task 7 | main.py 接线 + config.py 配置 | 完成 | — |
| 回归 | 全量回归 + Ruff + Mypy + 文档 | 完成 | 1050 passed |

**Phase 3 新增测试合计**: 81 个，全部通过

## 全量回归

- **1050 passed** (Phase 2 基线 975 + 75 净增)
- **7 failed** — 全部预先存在：
  - `test_source_migration_matches_model_metadata` (source migration 与 model metadata 不一致)
  - 6 个 `test_story_bible_service.py` (Phase 1 遗留：`patch_character`/`create_world_entry`/`promote_character`/`list_foreshadowing` 方法未实现)
- **8 errors** — 全部环境门禁 (RUN_EXTERNAL_TESTS=1 / TEST_DATABASE_URL)
- **Phase 3 修复的 bug**: `create_character` 中 `NameError: name 'item' is not defined` → 修复为 `self._character_dict(character)`

## 新增源码文件

| 文件 | 说明 |
|------|------|
| `src/novel_agent/analysis/extraction.py` | ExtractionResult schema (ExtractedParticipant/Event/Relation/Style/Result) |
| `src/novel_agent/analysis/extraction_provider.py` | ExtractionProvider 协议 + FakeExtractionProvider + DeepSeekExtractionProvider |
| `src/novel_agent/analysis/extraction_service.py` | ExtractionService + ExtractionSummary + _normalize_name |
| `src/novel_agent/db/models/style_profile.py` | StyleProfile + StyleProfileRevision ORM |
| `migrations/versions/0012_style_profiles.py` | 迁移 (2 张表 + guard trigger) |
| `src/novel_agent/api/routes/extraction.py` | 提取 API 路由 (POST extract) |

## 修改的源码文件

| 文件 | 修改内容 |
|------|----------|
| `src/novel_agent/analysis/story_bible.py` | 新增 StyleProfileData/Patch + get/upsert/patch_style_profile 方法；修复 create_character 返回值 bug；修复 get_bible 缺少 _bible 调用 |
| `src/novel_agent/api/routes/story_bible.py` | 新增 GET/PATCH style 端点 |
| `src/novel_agent/db/models/__init__.py` | 注册 StyleProfile/StyleProfileRevision |
| `src/novel_agent/config.py` | 新增 extraction_model/extraction_api_key/extraction_base_url 三个字段 + 独立验证器 |
| `src/novel_agent/main.py` | 导入 DeepSeekExtractionProvider + ExtractionService；lifespan 创建并注入 extraction_service；finally 调用 aclose() |

## 测试文件 (受 .gitignore 忽略)

| 文件 | 测试数 |
|------|--------|
| `tests/unit/test_extraction_schema.py` | 28 |
| `tests/unit/test_extraction_provider.py` | 11 |
| `tests/unit/test_style_profile_database.py` | 10 |
| `tests/unit/test_style_profile_service.py` | 10 |
| `tests/unit/test_extraction_service.py` | 10 |
| `tests/integration/test_extraction_api.py` | 12 |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/projects/{pid}/sources/{sid}/analyses/{aid}/extract` | 执行提取，返回 ExtractionSummary |
| GET | `/api/v1/projects/{pid}/story-bible/style` | 查看风格画像 |
| PATCH | `/api/v1/projects/{pid}/story-bible/style` | 修改风格画像 (需 expected_revision) |

## 配置项

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `EXTRACTION_MODEL` | "" | 提取模型名，空时 fallback 到 ANALYSIS_MODEL |
| `EXTRACTION_API_KEY` | None | 提取 API 密钥，空时 fallback 到 ANALYSIS_API_KEY |
| `EXTRACTION_BASE_URL` | "" | 提取 API base URL，空时 fallback 到 ANALYSIS_BASE_URL |

## 未验证项

- 真实 PostgreSQL 迁移 0012 执行
- 真实 DeepSeek API 提取调用
- 真实 Neo4j 图投影同步
- 真实 Temporal 工作流
- 真实 Qdrant 向量搜索
- Git 暂存/提交

## 后续计划

1. Phase 0 — 真实服务联调 (本机配置不支持，暂缓)
2. Phase 4 — 大纲生成
3. Phase 5 — 章节生成 + 前端 UI

> AI生成