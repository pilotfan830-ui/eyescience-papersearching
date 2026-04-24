# Embedding Similarity Search 工具说明

## 1. 目标

第一版 embedding similarity search 不下载本地模型，统一使用 Qwen/DashScope 的 OpenAI-compatible embedding API。论文向量离线构建后持久化到 SQLite，在线搜索时只为用户 query 调一次 embedding API，再与本地论文向量做相似度计算。

默认模型固定为：

```text
text-embedding-v4
```

默认维度：

```text
1024
```

## 2. 环境变量

必选其一：

```powershell
$env:EMBEDDING_API_KEY="你的 DashScope API key"
```

如果没有 `EMBEDDING_API_KEY`，后端会依次回退读取：

```text
DASHSCOPE_API_KEY
QWEN_API_KEY
```

可选配置：

```powershell
$env:EMBEDDING_PROVIDER="api"
$env:EMBEDDING_API_URL="https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
$env:EMBEDDING_MODEL="text-embedding-v4"
$env:EMBEDDING_DIMENSIONS="1024"
$env:EMBEDDING_TIMEOUT="30"
```

说明：

- `EMBEDDING_PROVIDER=api` 是默认值。
- 只有显式设置 `EMBEDDING_PROVIDER=local` 时，才会尝试使用本地 `sentence-transformers`。
- API key 不要写入代码、文档、SQLite 或前端。

## 3. 构建论文向量

先进入项目根目录或 backend 目录均可。

Dry run，只看待构建数量，不调用 API、不写 SQLite：

```powershell
D:\anaconda3\envs\paper-search\python.exe backend\tools\build_embeddings.py --dry-run
```

正常构建：

```powershell
D:\anaconda3\envs\paper-search\python.exe backend\tools\build_embeddings.py --batch-size 16
```

强制全量重建：

```powershell
D:\anaconda3\envs\paper-search\python.exe backend\tools\build_embeddings.py --force --batch-size 16
```

输出示例：

```json
{
  "provider": "api",
  "model": "text-embedding-v4",
  "dimensions": 1024,
  "papers": 76,
  "pending": 76,
  "built": 76,
  "dry_run": false,
  "db_path": "C:\\Users\\pilot\\Desktop\\site\\paper_search_tool - codex\\papers.sqlite"
}
```

## 4. 搜索时的数据流

1. `fts_multi_retrieve(query, topn=100)` 保持当前 Qwen query rewrite + FTS5 多路召回。
2. `semantic_retrieve(query, topn=100)`：
   - 检查 SQLite 中是否有当前 provider、model、dimensions 下的新鲜论文向量。
   - 调 embedding API 给 query 生成向量。
   - 用 numpy 点积计算 query 与论文向量的 cosine similarity。
3. `search_papers(query, topk)` 合并两路候选并重排。

重排公式：

```text
final_score =
  0.55 * semantic_score +
  0.30 * fts_score +
  0.10 * title_hit +
  0.05 * keyword_hit
```

## 5. 健康检查

启动后端：

```powershell
cd backend
uvicorn app.main:app --reload
```

打开：

```text
http://127.0.0.1:8000/api/health
```

重点字段：

- `semantic_enabled`：当前语义检索是否可用。
- `semantic_error`：语义检索不可用时的原因。
- `embedding_provider`：默认 `api`。
- `embedding_model`：默认 `text-embedding-v4`。
- `embedding_dimensions`：默认 `1024`。
- `embedded_papers`：当前可用论文向量数量。
- `embedding_stale_count`：缺失或文本已变化、需要重建的论文数量。

## 6. 降级行为

以下情况搜索不会 500：

- 未配置 embedding API key。
- 没有运行过 `build_embeddings.py`。
- embedding API 超时或返回错误。
- SQLite 中论文向量与当前论文文本 hash 不匹配。

这些情况下：

- `/api/search` 仍返回 FTS/Qwen rewrite 结果。
- `semantic_enabled=false`。
- `semantic_score=0.0`。
- `/api/health` 中 `semantic_error` 会说明原因。

## 7. 验证清单

无 key 场景：

```powershell
$env:EMBEDDING_API_KEY=""
curl "http://127.0.0.1:8000/api/search?q=cataract&limit=5"
```

预期：正常返回结果，`semantic_enabled=false`。

构建前 dry run：

```powershell
D:\anaconda3\envs\paper-search\python.exe backend\tools\build_embeddings.py --dry-run
```

预期：报告 `papers=76` 和待构建数量，不写入 SQLite。

构建后：

```powershell
curl "http://127.0.0.1:8000/api/health"
```

预期：`embedded_papers` 接近或等于 76，`embedding_model=text-embedding-v4`。

语义搜索：

```powershell
curl "http://127.0.0.1:8000/api/search?q=cataract%20surgery&limit=5"
```

预期：结果中出现 cataract surgery 相关论文，且在 embedding API 可用时有非零 `semantic_score`。
