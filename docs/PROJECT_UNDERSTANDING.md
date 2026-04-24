# 项目理解文档

## 1. 项目定位

这是一个本地运行的论文搜索小工具，用来检索 SQLite 数据库中期刊发表的文献。项目当前以“能快速本地跑起来”为核心：前端是一个静态 HTML 页面，后端是 FastAPI 服务，数据存放在 SQLite。

当前数据规模较小，根目录 `papers.sqlite` 中有 76 篇论文；`papers.citation_text` 字段完整存在，前端已经能展示引用预览、详情引用、复制引用、下载 EndNote `.enw`。

## 2. 目录结构

- `backend/app/main.py`：FastAPI 路由层，提供健康检查、搜索、详情、RIS/EndNote 导出。
- `backend/app/search_engine.py`：检索引擎核心，负责加载论文、FTS5 召回、Qwen query rewrite、embedding 召回、重排。
- `backend/app/schemas.py`：Pydantic 返回模型。
- `backend/tools/build_embeddings.py`：离线构建论文 embedding 的命令行工具。
- `frontend/index.html`：静态单页前端。
- `docs/cursor_.md`：之前 Cursor 开发过程的对话记录。
- `papers.sqlite`：当前后端自动读取的主数据库。

## 3. 数据库现状

主表 `papers` 的关键字段：

- `paper_id`：论文 ID。
- `title`：标题。
- `authors_display`：作者显示文本。
- `keywords_text`：关键词，通常换行分隔。
- `abstract_text`：摘要。
- `citation_text`：格式化引用文本，当前 76 篇均有该字段内容。
- `url`、`journal`、`publication_year`、`volume`、`issue`、`pages`、`doi`：文献信息。

辅助表：

- `paper_authors`：作者明细。
- `paper_keywords`：关键词明细。
- `papers_fts`：数据库内已有 FTS5 虚表；当前后端另外会在启动时构建一个内存 FTS5 表用于检索。

新增 embedding 表：

- `paper_embeddings(paper_id, provider, model, dimensions, text_hash, embedding_blob, updated_at)`。
- `embedding_blob` 存储 normalized `float32` 向量。
- `text_hash` 用于判断论文文本是否变化，避免重复调用 embedding API。

## 4. 后端 API

- `GET /api/health`：返回服务状态、论文数量、Qwen rewrite 状态、embedding 状态。
- `GET /api/search?q=...&limit=20`：搜索论文，返回 `final_score`、`fts_score`、`semantic_score`、`semantic_enabled`。
- `GET /api/papers/{paper_id}`：论文详情。
- `GET /api/export/{paper_id}.ris`：RIS 导出。
- `GET /api/export/{paper_id}.enw`：EndNote 导出。

## 5. 当前检索流程

1. 启动时读取 SQLite 中的论文到内存。
2. 启动时构建内存 SQLite FTS5 索引，字段为 `title + keywords + abstract`。
3. 搜索时先调用 `query_rewrite()`：
   - 如果 `ENABLE_QWEN_REWRITE` 开启且 `QWEN_API_KEY` 存在，调用 Qwen Chat API。
   - 返回 `keywords_zh`、`keywords_en`、`must_terms`。
   - 如果未配置或调用失败，自动退回原始 query。
4. `fts_multi_retrieve()` 使用原始 query 与改写关键词做多路 FTS 召回并合并。
5. `semantic_retrieve()` 在已构建论文向量且 embedding API 可用时，调用 `text-embedding-v4` 生成 query 向量，并与 SQLite 中持久化的论文向量做余弦相似度召回。
6. `search_papers()` 合并 FTS top100 和 semantic top100，按固定公式重排：

```text
final_score =
  0.55 * semantic_score +
  0.30 * fts_score +
  0.10 * title_hit +
  0.05 * keyword_hit
```

## 6. Cursor 历史结论

Cursor 阶段已经完成过这些改造：

- 确认 `citation_text` 存在并映射到接口返回。
- 前端从 `alert()` 改为详情模态框，增加引用预览。
- 增加复制全部论文信息、复制引用、下载 EndNote `.enw`。
- 将搜索逻辑从旧的全表扫描、子串、模糊匹配，改为 FTS5 + embedding 双路召回 + 候选重排。
- 加入 Qwen query rewrite，让自然语言 query 先转成中英关键词后再进入 FTS。
- 修复 FastAPI 线程池访问 SQLite 内存 FTS 连接的线程问题，使用 `check_same_thread=False` 和锁。

## 7. 已知风险

- Qwen query rewrite 和 embedding API 都依赖外部服务；接口失败时必须保留 FTS 降级。
- API key 不能写入代码或文档，只能通过环境变量配置。
- 当前数据量很小，SQLite + numpy 全量点积足够；数据量变大后需要考虑 sqlite-vec、pgvector 或 Qdrant。
- 当前前端直接写死 `http://127.0.0.1:8000/api`，生产部署时需要改为可配置。
- 当前项目目录不是 git 仓库，变更追踪依赖人工管理。
