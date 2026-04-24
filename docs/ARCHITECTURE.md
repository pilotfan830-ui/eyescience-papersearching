# 架构说明

## 数据库设计（建议）

- `papers(id, title, abstract, citation, doi, url, year, created_at, updated_at)`
- `paper_authors(id, paper_id, author_name, author_order)`
- `paper_keywords(id, paper_id, keyword)`

索引建议：
- `papers(title)`、`papers(year)`
- `paper_authors(paper_id, author_name)`
- `paper_keywords(paper_id, keyword)`

SQLite 阶段可加 FTS5 虚表 `papers_fts(title, abstract, keywords_text)`；迁移 PostgreSQL 后改为 `tsvector + pg_trgm + pgvector`。

## API 设计

- `GET /api/health` 健康检查
- `GET /api/search?q=...&limit=20` 搜索
- `GET /api/papers/{paper_id}` 详情
- `GET /api/export/{paper_id}.ris` 导出 RIS
- `GET /api/export/{paper_id}.enw` 导出 EndNote

## 检索策略

- 第 1 层：关键词命中（title/keywords/abstract）
- 第 2 层：模糊匹配（RapidFuzz）
- 第 3 层：语义相似度（SentenceTransformer embedding）
- 最终融合分：`0.25*keyword + 0.30*fuzzy + 0.45*semantic`

## 页面原型建议（移动端优先）

- 顶部搜索框 + 搜索按钮
- 卡片列表：标题、作者、年份、摘要片段
- 点击卡片进入详情弹窗/页面：完整作者、摘要、引用、DOI、网址、导出按钮
- 设计保持单栏、12px 内边距、16px 可点击控件

## 部署方式

### 本地
- FastAPI + Uvicorn 直接运行
- Nginx 可选反向代理

### 生产
- Docker 打包（API + 静态前端）
- 数据库初期 SQLite；迁移 PostgreSQL 时保留 API 层不变，仅替换数据访问层
