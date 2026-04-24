# 功能正常
_Exported on 4/20/2026 at 13:50:13 GMT+8 from Cursor (3.1.15)_

---

**User**

好像能跑了

---

**Cursor**

听起来不错——如果已经能正常跑起来，就说明环境或代码这块基本打通了。

后面如果你遇到具体报错、想加功能（比如搜索、导出、界面），或者想整理部署方式，直接说就行。

---

**User**

但是有几个bug要改，
1，你检查一下现在搜索出论文之后他弹出有没有引用信息？Sqlite的papers表里面有没有引用信息？这个是必须展示的
2，在点开论文之后弹出的小框底部增加三个按钮
(1)复制全部论文信息，点击之后复制全部论文信息到剪贴板
(2)复制引用信息，点击之后复制引用部分
(3)下载endnote格式引用链接
3，感觉现在的搜索逻辑比较笨，给我讲解一下都用了什么库/逻辑/办法，什么地方能接入大模型做语义搜索？而且还要有中英文转换，我试了一下好像没有。这个先不做，把优化方案给我写出来

---

**Cursor**

正在检查数据库结构、搜索与弹窗实现，随后实现三个按钮并撰写搜索优化方案。



正在检查 SQLite 中 `papers` 表的结构及 `citation` 字段的数据。



确认：`papers` 表有 `citation_text` 且含数据；后端已通过 `citation_text` 映射到 `citation`。问题在于前端用 `alert()` 展示长文本，易被截断且难以阅读。我们将改为模态框、在列表中显示引用摘要，并加入三个操作按钮；同时把引用写入 EndNote 导出。



将 EndNote 的 `%Z` 合并为单行，以提高导入兼容性。



---

## 1. 引用信息：库里有没有？弹窗里有没有？

**SQLite：** 你的 `papers` 表里有 **`citation_text`**（且为 NOT NULL），我用本地库抽查前几行，`citation_text` 里是有完整格式化引用的（例如以作者缩写开头的期刊式引用）。

**后端：** `search_engine.py` 里会把 `citation` / `引用` / **`citation_text`** 映射到内存里的 `Paper.citation`，详情接口 `/api/papers/{id}` 也会原样返回。

**之前像「没引用」的原因：** 前端用 **`alert()`** 堆一大段文字，在 Windows 上很容易被截断、也不方便看，所以体感像「没有引用」。

**已做改动：**

- 搜索列表每条结果增加 **「引用预览」**（来自后端新字段 `citation_snippet`，约前 140 字）。
- 详情改为 **模态框**，单独一块 **「引用」** 区域展示全文（可滚动），避免 `alert` 截断。

---

## 2. 详情底部三个按钮（已实现）

| 按钮 | 行为 |
|------|------|
| **复制全部论文信息** | 标题、作者、年份、关键词、DOI、网址、摘要、引用，拼成一段纯文本写入剪贴板。 |
| **复制引用信息** | 仅复制 `citation`；若为空会提示「当前论文没有引用文本」。 |
| **下载 EndNote (.enw)** | 请求已有接口 `GET /api/export/{id}.enw`，触发浏览器下载。 |

另外：**`.enw` 和 `.ris` 导出里已补上引用**（RIS 用 `N1`，ENW 用 `%Z` 单行备注），便于在文献管理软件里也能看到引用文本。

---

## 3. 当前搜索在干什么？为什么「有点笨」？大模型 / 中英文可以怎么接？

**用到的库与逻辑（按代码路径）：**

1. **数据：** 启动时 **`sqlite3`** 把整张 `papers` 表读进内存，每条变成 `Paper`（标题、作者、关键词、摘要、引用等）。
2. **语义相关分（可选）：** 尝试加载 **`sentence-transformers`**，模型为 **`paraphrase-multilingual-MiniLM-L12-v2`**，对「标题 + 关键词 + 摘要」拼成的句子做 embedding；查询句同样编码后做 **向量点积**（相当于余弦相似度，因为已归一化）。  
   - 注意：代码里写了 **`local_files_only=True`**，若本机 **没有** 预先缓存该模型，加载会失败，语义分支整段 **降级** 成：把查询词拆成 token，和论文文本 token 做 **简易重叠比例**（非常粗）。
3. **模糊字面值：** **`rapidfuzz.fuzz.partial_ratio`**，在「标题+关键词+摘要」拼成的大字符串上做 **部分模糊匹配**。
4. **关键词命中：** 查询串 **整体** 是否作为子串出现在上述大字符串里（先 **lower**），命中记 1，否则 0。
5. **融合打分：**  
   `0.25 × 子串命中 + 0.30 × 模糊分 + 0.45 × 语义分`，再排序取 Top-K。  
   **`numpy`** 用于语义分的批量点积。

**为什么你会觉得「笨」、且中英文切换不明显：**

- **没有查询翻译 / 扩展：** 中文查询不会自动变成英文检索词，也不会做同义词扩展；只靠多语种 embedding **在模型成功加载时** 才有一部分跨语言能力。
- **子串匹配太硬：** `query_low in text` 对中英文分词都不友好（例如中文没空格、英文短语变体）。
- **语义模型常没真正用上：** 离线环境或首次未下载模型时，长期落在 **token 重叠** 兜底上，效果会接近「分词撞词」。
- **全表线性扫描：** 数据量一大，每次搜索都要对所有论文算分，延迟会上去（没有 SQLite **FTS5** / 向量索引）。

**优化方案（先写方案，你说了暂不上线）：**

1. **保证语义路径可用：** 首次部署时拉好模型缓存，或改成可配置的模型路径；失败时打日志，避免静默降级让人误以为「语义搜索在工作」。
2. **中英双向（不接大模型也能做一版）：** 查询前用 **翻译 API** 或本地词典，生成「中文 + 英文」检索短语，再 **分别算 embedding 或 FTS**，结果 **RRF 合并**；或只用 **更强的跨语种 embedding**（如 bge-m3、e5-multilingual 等）并固定好是否归一化、是否 mean pooling。
3. **接大模型（语义 / 理解向）：**  
   - **查询改写：** LLM 把口语化问题改成「适合检索的关键词列表 + 英文扩展」。  
   - **重排序：** 先用便宜方式召回 Top 50～200，再用 LLM 或 cross-encoder 对 `(query, abstract)` 精排。  
   - **真正语义索引：** 离线算好向量写入 **sqlite-vec / pgvector / Qdrant**，在线只算查询向量做 ANN，而不是全表 dot 一遍。
4. **字词层：** 为 `title/abstract/keywords` 建 **FTS5**（中文可考虑 **jieba 分词** 后入库，或简化为 bigram）；与向量分数做加权或 RRF。
5. **观测：** 对「中搜英 / 英搜中」固定一批评测 query，看 MRR/命中率，再决定是优先「翻译 + 双路检索」还是「换 embedding + 重排」。

---

请重新打开或刷新 `frontend/index.html`，并 **重启一次后端**（schemas / search 返回值有变更）。若你希望详情里再加「关闭」按钮或 RIS 下载按钮，可以再说一下我补上。

---

**User**

帮我重构当前论文搜索逻辑，目标是：

把“全表扫描 + 子串/模糊/语义混合打分”
改成：
👉 FTS5 + embedding 双路召回 + 重排

要求：

1. 加 SQLite FTS5
- 索引 title + keywords + abstract
- 写 fts_retrieve(query, topn)

2. 保留 embedding 语义搜索
- 模型加载必须可观测（semantic_enabled）
- 加载失败不能静默降级
- 写 semantic_retrieve(query, topn)

3. 两路召回
- FTS top 100
- embedding top 100
- 合并去重

4. 重排（只在候选集上）
final_score =
  0.55 * semantic +
  0.30 * fts +
  0.10 * title_hit +
  0.05 * keyword_hit

5. 提供统一接口
search_papers(query, topk)

返回：
- results
- final_score / fts_score / semantic_score
- semantic_enabled

6. 删除或降级旧逻辑
- 不再用 query in text 做主逻辑
- 不再用 partial_ratio 排整段文本
- 去掉 token overlap fallback

要求：
- 尽量少改现有结构
- 代码可直接运行
- 给完整实现，不要只讲思路

---

**Cursor**

我会在现有 `SearchEngine` 上最小改动重构：先加 FTS5 建索引与双路召回，再把 `search()` 切到新 `search_papers()`，最后同步 schema 和接口返回字段并做一次本地验证。

准备开始改后端核心：我会先完整替换 `search_engine.py` 为 FTS5+embedding 双路召回与候选重排实现，再同步 `schemas.py` 和 `main.py` 的返回结构，保证接口可直接跑。

已完成代码重构，接下来我会跑一次本地快速校验（导入、搜索返回结构、`semantic_enabled`）并修正潜在报错。

已按你要求完成重构，核心已经从“全表混合打分”切到 **FTS5 + embedding 双路召回 + 候选重排**，并保持现有结构尽量不动。

### 改动完成点

- 在 `backend/app/search_engine.py` 新增并接入：
  - `fts_retrieve(query, topn)`  
    - 使用 SQLite **FTS5**（索引 `title + keywords + abstract`）
    - `topn` 召回，返回归一化后的 `fts_score`
  - `semantic_retrieve(query, topn)`  
    - 保留 embedding 检索，返回归一化 `semantic_score`
    - 不再使用 token overlap fallback
  - `search_papers(query, topk)`（统一接口）  
    - 先取 `FTS top 100` + `Embedding top 100`
    - 合并去重候选集
    - 候选集重排公式：
      - `0.55 * semantic + 0.30 * fts + 0.10 * title_hit + 0.05 * keyword_hit`
    - 返回：
      - `results`
      - 每条 `final_score / fts_score / semantic_score`
      - `semantic_enabled`
- 语义模型可观测性：
  - 增加 `self.semantic_enabled` 和 `self.semantic_error`
  - 模型加载失败会记录异常日志（**不再静默降级**）
- 旧逻辑已降级/移除：
  - 不再以 `query in text` 做主逻辑
  - 不再用 `partial_ratio` 对整段文本排序
  - 去掉 token overlap fallback
- API 对接：
  - `backend/app/main.py` 的 `/api/search` 已改为调用 `engine.search_papers(...)`
  - `backend/app/schemas.py` 已扩展搜索返回字段，增加 `semantic_enabled` 和三类分数字段（保留 `score` 兼容前端）

---

### 我本地做的验证

已跑 Python smoke test，结果显示：

- `search_papers()` 正常返回
- 返回结构包含：
  - `final_score`
  - `fts_score`
  - `semantic_score`
  - `semantic_enabled`
- 当前环境下模型本地文件不完整，`semantic_enabled=False`，并已输出明确错误日志（符合“可观测且不静默”要求）

---

如果你愿意，我下一步可以直接帮你补一段“**模型启动健康检查接口**”（例如 `/api/health` 里加 `semantic_enabled/semantic_error`），方便前端直接显示“语义检索已启用/未启用”。

---

**User**

当前环境下模型本地文件不完整，semantic_enabled=False，并已输出明确错误日志（符合“可观测且不静默”要求）

那我怎么把这个文件搞完整

---

**Cursor**

你现在是因为代码里用了 `local_files_only=True`，所以**必须本地已经有完整模型文件**。  
补齐有两种最稳的方法：

## 方法 A（最简单）：联网自动下载一次

1. 打开终端（建议在你后端的 Python 环境里）
2. 执行：

```bash
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"
```

这一步会把模型下载到 Hugging Face 本地缓存。

3. 下载完后再跑后端（你代码里仍是 `local_files_only=True`），就会读本地缓存，不再联网。

---

## 方法 B（可控）：手动下载到指定目录

### 1) 安装 huggingface hub 工具
```bash
pip install -U huggingface_hub
```

### 2) 下载模型快照
```bash
huggingface-cli download sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 --local-dir D:\models\paraphrase-multilingual-MiniLM-L12-v2
```

### 3) 代码里改成加载本地路径
把 `search_engine.py` 里这行：

```python
SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', local_files_only=True)
```

改成：

```python
SentenceTransformer(r'D:\models\paraphrase-multilingual-MiniLM-L12-v2', local_files_only=True)
```

---

## 你要确认“完整”的关键文件（目录内至少有）

- `config.json`
- `modules.json`
- `sentence_*.json`（如 `sentence_bert_config.json`）
- `tokenizer.json` / `tokenizer_config.json`
- `model.safetensors`（或 `pytorch_model.bin`）
- 可能还有 `1_Pooling/` 等子目录

---

## 快速自检（最实用）

```bash
python -c "from sentence_transformers import SentenceTransformer; m=SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', local_files_only=True); print('ok')"
```

输出 `ok` 就说明本地文件齐了。  
然后重启后端，再看 `/api/search` 返回里的 `semantic_enabled` 应该会变 `true`。

---

如果你要，我可以下一步直接帮你把代码改成：  
- 优先读 `MODEL_PATH` 环境变量（本地目录）  
- 没配就读默认 HF 缓存  
这样部署会更稳。

---

**User**

那我用A还是B啊，有什么区别

---

**Cursor**

用一句话区分：

- **A = 快速省事**（先跑起来）
- **B = 可控可复现**（更适合长期/部署）

### 选 A 的场景
- 你现在是本机开发，想马上让 `semantic_enabled=True`
- 不介意模型放在系统默认缓存目录
- 机器能联网一次

优点：一步命令，最快。  
缺点：缓存路径不直观，迁移机器/打包部署时不够稳定。

### 选 B 的场景
- 你希望模型放在固定目录（比如 `D:\models\...`）
- 以后要部署到服务器、Docker、离线环境
- 想团队成员都用同一套模型路径

优点：路径明确、可备份、可复现。  
缺点：多一步下载和配置。

---

**建议你当前阶段：先 A，后 B。**  
先用 A 验证功能跑通；后面准备上线或给别人用时，再切 B。  
这也是最省时间、风险最低的顺序。

---

**User**

为什么选择minilm

---

**Cursor**

因为你这个场景是“**中英混合论文检索 + 本地运行**”，`MiniLM`（这里是 `paraphrase-multilingual-MiniLM-L12-v2`）在这几个维度比较平衡：

- **多语言能力够用**：支持中英跨语义匹配，比纯英文模型更适合你现在数据。
- **速度快、资源省**：向量化和查询延迟低，CPU 也能跑，适合你这种 SQLite 本地工具。
- **模型体积相对小**：下载和部署负担小，不容易把开发环境拖慢。
- **生态成熟**：在 `sentence-transformers` 里直接可用，改造成本低，稳定性高。

如果换更大模型（比如更强的 bge/e5 多语模型），通常会：

- 召回质量可能更好（尤其长文本语义）
- 但推理更慢、占内存更多、部署更重

所以当下选 `MiniLM` 的理由是：**先把“可用 + 可跑 + 可维护”做到位**。  
后面你可以按评测结果再升级（比如同一套检索框架里只替换 embedding 模型）。

---

**User**

换成下载sentence-transformers/all-MiniLM-L6-v2吧，那个太大了

---

**Cursor**

我把后端 embedding 模型切到 `sentence-transformers/all-MiniLM-L6-v2`，并顺手做一次快速导入测试，确保能正常加载这个新模型名。

已切换为 `sentence-transformers/all-MiniLM-L6-v2`：

- 修改文件：`backend/app/search_engine.py`
- 当前加载配置仍是 `local_files_only=True`（不联网）

我也帮你跑了测试，结果是：

- 代码正常运行
- 但本地还没有这个模型缓存，所以 `semantic_enabled=False`

你现在只要下载一次即可（方法 A）：

```bash
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
```

下载完成后重启后端，再查 `/api/search`，`semantic_enabled` 就会变成 `true`。

---

**User**

给我方法B的下载？

---

**Cursor**

可以，下面就是 **方法 B（手动下载到固定目录）**，按这个做就行。

## 1) 安装下载工具
在你的 Python 环境里执行：

```bash
pip install -U huggingface_hub
```

## 2) 下载到你指定目录（示例放 D 盘）
```bash
huggingface-cli download sentence-transformers/all-MiniLM-L6-v2 --local-dir D:\models\all-MiniLM-L6-v2
```

如果你没装 `huggingface-cli`，也可以用 Python 方式下载（等价）：

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='sentence-transformers/all-MiniLM-L6-v2', local_dir=r'D:\models\all-MiniLM-L6-v2', local_dir_use_symlinks=False)"
```

## 3) 改代码加载本地目录
把 `backend/app/search_engine.py` 里模型加载改成：

```python
self._sentence_model = SentenceTransformer(
    r'D:\models\all-MiniLM-L6-v2',
    local_files_only=True,
)
```

## 4) 本地验证
```bash
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer(r'D:\models\all-MiniLM-L6-v2', local_files_only=True); print('ok')"
```

看到 `ok` 就说明模型完整可用。

## 5) 重启后端确认
重启服务后调用 `/api/search`，看返回里的 `semantic_enabled` 是否为 `true`。

---

如果你愿意，我下一步可以直接帮你把 `search_engine.py` 改成优先读环境变量 `MODEL_PATH`，这样你以后换目录不用再改代码。

---

**User**

下不下来，先不弄这个，给我i重启后端的代码我测试一下

---

**Cursor**

我先给你一套可直接复制的 Windows 重启命令（含先杀占用 8000 端口），你执行后就能马上测。

可以，先不用模型下载，先把后端重启起来测试。

在 **PowerShell** 里执行这几行（可直接复制）：

```powershell
# 1) 进入项目后端目录
cd "c:\Users\pilot\Desktop\site\paper_search_tool\backend"

# 2) 如果 8000 端口被旧进程占用，先杀掉
$pidOn8000 = (Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess)
if ($pidOn8000) { Stop-Process -Id $pidOn8000 -Force }

# 3) 启动后端
uvicorn app.main:app --reload
```

启动后你会看到类似：
- `Uvicorn running on http://127.0.0.1:8000`

然后你就可以测：
- 健康检查：`http://127.0.0.1:8000/api/health`
- 搜索接口：`http://127.0.0.1:8000/api/search?q=test&limit=5`

如果报 `uvicorn` 找不到，就先执行：

```powershell
pip install -r requirements.txt
```

如果你愿意，我下一条可以直接给你一行“后台启动 + 日志落文件”的命令，方便你边开前端边看日志。

---

**User**

(paper-search) c:\Users\pilot\Desktop\site\paper_search_tool\backend>uvicorn app.main:app --reload
INFO:     Will watch for changes in these directories: ['c:\\Users\\pilot\\Desktop\\site\\paper_search_tool\\backend']
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Started reloader process [9112] using WatchFiles
INFO:     Started server process [18932]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     127.0.0.1:51557 - "GET /api/search?q=research&limit=20 HTTP/1.1" 500 Internal Server Error
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\uvicorn\protocols\http\httptools_impl.py", line 426, in run_asgi
    result = await app(  # type: ignore[func-returns-value]
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\uvicorn\middleware\proxy_headers.py", line 84, in __call__
    return await self.app(scope, receive, send)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\fastapi\applications.py", line 1054, in __call__
    await super().__call__(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\applications.py", line 123, in __call__
    await self.middleware_stack(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\errors.py", line 186, in __call__
    raise exc
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\errors.py", line 164, in __call__
    await self.app(scope, receive, _send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\cors.py", line 93, in __call__
    await self.simple_response(scope, receive, send, request_headers=headers)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\cors.py", line 148, in simple_response
    await self.app(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\exceptions.py", line 65, in __call__
    await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 64, in wrapped_app
    raise exc
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 53, in wrapped_app
    await app(scope, receive, sender)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 756, in __call__
    await self.middleware_stack(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 776, in app
    await route.handle(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 297, in handle
    await self.app(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 77, in app
    await wrap_app_handling_exceptions(app, request)(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 64, in wrapped_app
    raise exc
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 53, in wrapped_app
    await app(scope, receive, sender)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 72, in app
    response = await func(request)
               ^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\fastapi\routing.py", line 278, in app
    raw_response = await run_endpoint_function(
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\fastapi\routing.py", line 193, in run_endpoint_function
    return await run_in_threadpool(dependant.call, **values)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\concurrency.py", line 42, in run_in_threadpool
    return await anyio.to_thread.run_sync(func, *args)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\anyio\to_thread.py", line 63, in run_sync
    return await get_async_backend().run_sync_in_worker_thread(
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\anyio\_backends\_asyncio.py", line 2518, in run_sync_in_worker_thread
    return await future
           ^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\anyio\_backends\_asyncio.py", line 1002, in run
    result = context.run(func, *args)
             ^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\main.py", line 27, in search
    payload = engine.search_papers(q, topk=limit)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\search_engine.py", line 219, in search_papers
    fts_scores = self.fts_retrieve(query, topn=100)
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\search_engine.py", line 164, in fts_retrieve
    rows = self._fts_conn.execute(
           ^^^^^^^^^^^^^^^^^^^^^^^
sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread. The object was created in thread id 22708 and this is thread id 20828.
INFO:     127.0.0.1:61793 - "GET /api/search?q=research&limit=20 HTTP/1.1" 500 Internal Server Error
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\uvicorn\protocols\http\httptools_impl.py", line 426, in run_asgi
    result = await app(  # type: ignore[func-returns-value]
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\uvicorn\middleware\proxy_headers.py", line 84, in __call__
    return await self.app(scope, receive, send)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\fastapi\applications.py", line 1054, in __call__
    await super().__call__(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\applications.py", line 123, in __call__
    await self.middleware_stack(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\errors.py", line 186, in __call__
    raise exc
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\errors.py", line 164, in __call__
    await self.app(scope, receive, _send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\cors.py", line 93, in __call__
    await self.simple_response(scope, receive, send, request_headers=headers)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\cors.py", line 148, in simple_response
    await self.app(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\exceptions.py", line 65, in __call__
    await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 64, in wrapped_app
    raise exc
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 53, in wrapped_app
    await app(scope, receive, sender)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 756, in __call__
    await self.middleware_stack(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 776, in app
    await route.handle(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 297, in handle
    await self.app(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 77, in app
    await wrap_app_handling_exceptions(app, request)(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 64, in wrapped_app
    raise exc
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 53, in wrapped_app
    await app(scope, receive, sender)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 72, in app
    response = await func(request)
               ^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\fastapi\routing.py", line 278, in app
    raw_response = await run_endpoint_function(
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\fastapi\routing.py", line 193, in run_endpoint_function
    return await run_in_threadpool(dependant.call, **values)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\concurrency.py", line 42, in run_in_threadpool
    return await anyio.to_thread.run_sync(func, *args)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\anyio\to_thread.py", line 63, in run_sync
    return await get_async_backend().run_sync_in_worker_thread(
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\anyio\_backends\_asyncio.py", line 2518, in run_sync_in_worker_thread
    return await future
           ^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\anyio\_backends\_asyncio.py", line 1002, in run
    result = context.run(func, *args)
             ^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\main.py", line 27, in search
    payload = engine.search_papers(q, topk=limit)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\search_engine.py", line 219, in search_papers
    fts_scores = self.fts_retrieve(query, topn=100)
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\search_engine.py", line 164, in fts_retrieve
    rows = self._fts_conn.execute(
           ^^^^^^^^^^^^^^^^^^^^^^^
sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread. The object was created in thread id 22708 and this is thread id 20828.
INFO:     127.0.0.1:63833 - "GET /api/search?q=research&limit=20 HTTP/1.1" 500 Internal Server Error
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\uvicorn\protocols\http\httptools_impl.py", line 426, in run_asgi
    result = await app(  # type: ignore[func-returns-value]
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\uvicorn\middleware\proxy_headers.py", line 84, in __call__
    return await self.app(scope, receive, send)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\fastapi\applications.py", line 1054, in __call__
    await super().__call__(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\applications.py", line 123, in __call__
    await self.middleware_stack(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\errors.py", line 186, in __call__
    raise exc
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\errors.py", line 164, in __call__
    await self.app(scope, receive, _send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\cors.py", line 93, in __call__
    await self.simple_response(scope, receive, send, request_headers=headers)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\cors.py", line 148, in simple_response
    await self.app(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\exceptions.py", line 65, in __call__
    await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 64, in wrapped_app
    raise exc
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 53, in wrapped_app
    await app(scope, receive, sender)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 756, in __call__
    await self.middleware_stack(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 776, in app
    await route.handle(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 297, in handle
    await self.app(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 77, in app
    await wrap_app_handling_exceptions(app, request)(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 64, in wrapped_app
    raise exc
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 53, in wrapped_app
    await app(scope, receive, sender)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 72, in app
    response = await func(request)
               ^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\fastapi\routing.py", line 278, in app
    raw_response = await run_endpoint_function(
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\fastapi\routing.py", line 193, in run_endpoint_function
    return await run_in_threadpool(dependant.call, **values)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\concurrency.py", line 42, in run_in_threadpool
    return await anyio.to_thread.run_sync(func, *args)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\anyio\to_thread.py", line 63, in run_sync
    return await get_async_backend().run_sync_in_worker_thread(
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\anyio\_backends\_asyncio.py", line 2518, in run_sync_in_worker_thread
    return await future
           ^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\anyio\_backends\_asyncio.py", line 1002, in run
    result = context.run(func, *args)
             ^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\main.py", line 27, in search
    payload = engine.search_papers(q, topk=limit)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\search_engine.py", line 219, in search_papers
    fts_scores = self.fts_retrieve(query, topn=100)
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\search_engine.py", line 164, in fts_retrieve
    rows = self._fts_conn.execute(
           ^^^^^^^^^^^^^^^^^^^^^^^
sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread. The object was created in thread id 22708 and this is thread id 20828.
INFO:     127.0.0.1:64715 - "GET /api/search?q=research&limit=20 HTTP/1.1" 500 Internal Server Error
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\uvicorn\protocols\http\httptools_impl.py", line 426, in run_asgi
    result = await app(  # type: ignore[func-returns-value]
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\uvicorn\middleware\proxy_headers.py", line 84, in __call__
    return await self.app(scope, receive, send)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\fastapi\applications.py", line 1054, in __call__
    await super().__call__(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\applications.py", line 123, in __call__
    await self.middleware_stack(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\errors.py", line 186, in __call__
    raise exc
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\errors.py", line 164, in __call__
    await self.app(scope, receive, _send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\cors.py", line 93, in __call__
    await self.simple_response(scope, receive, send, request_headers=headers)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\cors.py", line 148, in simple_response
    await self.app(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\middleware\exceptions.py", line 65, in __call__
    await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 64, in wrapped_app
    raise exc
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 53, in wrapped_app
    await app(scope, receive, sender)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 756, in __call__
    await self.middleware_stack(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 776, in app
    await route.handle(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 297, in handle
    await self.app(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 77, in app
    await wrap_app_handling_exceptions(app, request)(scope, receive, send)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 64, in wrapped_app
    raise exc
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\_exception_handler.py", line 53, in wrapped_app
    await app(scope, receive, sender)
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\routing.py", line 72, in app
    response = await func(request)
               ^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\fastapi\routing.py", line 278, in app
    raw_response = await run_endpoint_function(
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\fastapi\routing.py", line 193, in run_endpoint_function
    return await run_in_threadpool(dependant.call, **values)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\starlette\concurrency.py", line 42, in run_in_threadpool
    return await anyio.to_thread.run_sync(func, *args)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\anyio\to_thread.py", line 63, in run_sync
    return await get_async_backend().run_sync_in_worker_thread(
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\anyio\_backends\_asyncio.py", line 2518, in run_sync_in_worker_thread
    return await future
           ^^^^^^^^^^^^
  File "D:\anaconda3\envs\paper-search\Lib\site-packages\anyio\_backends\_asyncio.py", line 1002, in run
    result = context.run(func, *args)
             ^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\main.py", line 27, in search
    payload = engine.search_papers(q, topk=limit)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\search_engine.py", line 219, in search_papers
    fts_scores = self.fts_retrieve(query, topn=100)
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\search_engine.py", line 164, in fts_retrieve
    rows = self._fts_conn.execute(
           ^^^^^^^^^^^^^^^^^^^^^^^
sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread. The object was created in thread id 22708 and this is thread id 20828.


搜不出来了

---

**Cursor**

定位到了，问题是 FTS 内存连接在主线程创建、请求在线程池里执行触发了 SQLite 线程限制。我现在修成线程安全访问（`check_same_thread=False` + 查询加锁），改完你重启即可恢复。

你这个报错我已经修掉了，原因正是线程问题。

我做了两处修复（`backend/app/search_engine.py`）：

- FTS 内存库连接改为：`sqlite3.connect(':memory:', check_same_thread=False)`
- 对 FTS 查询加互斥锁：`threading.Lock()`，避免并发访问同一连接

这样 FastAPI 在线程池处理请求时就不会再触发  
`SQLite objects created in a thread can only be used in that same thread.`

现在你只需要：

1. 停掉当前 `uvicorn`（Ctrl+C）  
2. 重新启动：
   ```powershell
   cd "c:\Users\pilot\Desktop\site\paper_search_tool\backend"
   uvicorn app.main:app --reload
   ```
3. 再访问：
   `http://127.0.0.1:8000/api/search?q=research&limit=20`

说明：现在语义模型还是没下载，所以 `semantic_enabled` 仍可能是 `false`，但**不会再 500**，搜索会走 FTS 路径正常返回。

---

**User**

也就是说，现在还是纯FTS搜索，没有embedding？

---

**Cursor**

对，**你当前环境下是“仅 FTS 生效”**。

因为现在 `all-MiniLM-L6-v2` 还没在本地缓存成功，`semantic_enabled=False`，所以：

- `semantic_retrieve()` 返回空
- 最终候选基本来自 `fts_retrieve()`
- 重排里 `semantic_score` 为 0，仅靠 `fts + title_hit + keyword_hit`

等你把模型下载好后，`semantic_enabled=True`，就会自动变成你要的 **FTS + embedding 双路召回**。

---

**User**

这个项目现在的前后端是分离的吗？都采用的什么架构？

---

**Cursor**

是，当前是**前后端分离（轻量版）**。

## 现在的形态

- **前端**：`frontend/index.html`  
  - 纯静态页面（原生 HTML/CSS/JS）
  - 通过 `fetch` 调用后端 API（`http://127.0.0.1:8000/api/...`）
- **后端**：`backend/app/main.py`（FastAPI）  
  - 提供搜索、详情、导出等 REST 接口
- **数据层**：SQLite（项目根目录 `.sqlite`）  
  - 启动时由 `SearchEngine` 读库到内存对象
  - 同时建立 FTS5 索引（内存 FTS 表）

## 架构可以概括为

- **前端架构**：静态单页 + API 调用（无前端框架）
- **后端架构**：FastAPI 单体服务（路由层 + 检索引擎层）
- **检索架构**：  
  - 当前有效：FTS5 召回 + 候选重排  
  - 条件启用：Embedding 召回（模型可用时自动并入双路）
- **部署架构**：本地双进程开发模式  
  - 前端：直接打开静态文件  
  - 后端：`uvicorn` 独立运行

如果你想，我可以再给你画一个“当前架构图 + 下一步可演进架构（React/Vue 前端、Nginx、向量库）”的落地版本。

---

**User**

这个embedding能不能不下载模型本体，我搞个api进去？或者我直接就搞一个模型api，还能把前面那部分【将用户语言转化为关键词】的事情一并做了？因为我是有qwenmax的一个token包可以先用着

---

**Cursor**

可以，**完全可以不下载本地 embedding 模型**，直接走 API。  
而且你说的两件事可以一起做：

1) 用模型 API 做「用户问题 -> 检索关键词（中英扩展）」  
2) 用模型 API 做 embedding（或直接做重排）

你有 Qwen Max token，这条路很适合你当前阶段。

## 推荐落地方式（按稳妥程度）

### 方案 A（最推荐，先快跑）
- **FTS5 继续做主召回**
- 新增一步：`query_rewrite()` 调 Qwen Max，把用户自然语言改写成：
  - `keywords_zh`（中文关键词）
  - `keywords_en`（英文关键词）
  - `must_terms`（必含词，可选）
- 把改写后的 query 喂给 FTS5（可分多次召回后合并）

优点：不依赖本地模型；立刻提升“提问句 -> 检索词”效果。  
缺点：还不是真 embedding 召回。

---

### 方案 B（你提到的完整版）
- `query_rewrite()`：Qwen Max 生成中英关键词
- `semantic_retrieve()`：调用 embedding API 得到 query 向量
- 文档向量：
  - 要么离线批量调 API 生成，存 SQLite（推荐）
  - 要么每次现算（不推荐，慢且贵）
- 双路召回：`FTS top100 + embedding top100`（你现在这套可直接复用）
- 重排继续用你现在那套加权

优点：真正语义召回 + 可控成本。  
缺点：需要做一次“文档向量离线构建”。

---

## 你关心的核心问题：能不能一并做“语言转关键词”？

**能，而且非常建议做。**  
这一步对中文口语 query、跨中英文术语特别有效，通常比直接拿原 query 去 FTS 好很多。

---

## 成本/延迟建议

- 改写用大模型（Qwen Max）
- embedding 尽量用更便宜的 embedding 模型 API（若 Qwen 有 embedding 端点优先用）
- 加缓存：
  - query 改写缓存（按 query hash）
  - query embedding 缓存
- 设置超时与降级：
  - API 超时就退回纯 FTS，服务不挂

---

如果你同意，我可以下一步直接给你改成：
- 新增 `QWEN_API_KEY` 配置
- 实现 `query_rewrite()`（Qwen Max）
- 把 `search_papers()` 接入“改写后 FTS 检索”
- 先不改文档向量（先跑通你最需要的“语言转关键词”）  
这样风险最低、见效最快。

---

**User**

所谓【真正的语义召回】重要吗？若我已经有qwen来转关键词了？

---

**Cursor**

短答：**重要，但不是第一优先级**。  
你现在有 Qwen 做“问题 -> 关键词扩展”，已经能明显提升效果，先上线完全合理。

怎么判断要不要上“真正语义召回”：

- **关键词改写已足够**：你的查询多是明确术语（药名、方法名、病种名），FTS 命中不错  
  -> 先不急着上 embedding。
- **必须上语义召回**：用户常用描述性问法、跨表达方式很大（同义改写、隐含意图）  
  -> 仅关键词会漏召回，embedding 价值高。

一个实用判断标准（你可以直接拿来用）：

- 看 Top20 结果里“明显相关却没被召回”的比例  
- 如果漏召回经常发生（比如 >20-30% 的测试 query 都出现），就该上语义召回。

建议路线（性价比最高）：

1. **先做 Qwen 关键词改写 + FTS**（你现在这步）  
2. 加一套小评测集（20-50条真实查询）  
3. 再决定要不要加 embedding 双路召回  
4. 如果加，也保持“可降级到纯 FTS”，保证稳定与成本可控

所以结论是：  
**Qwen 改写并不能完全替代语义召回，但在当前阶段可以先替代 70% 需求。**  
等你验证到“漏召回瓶颈”再上 embedding，最稳。

---

**User**

相当于我还得做个训练/验证集是吧？

相当于我还得做个训练/验证集来验证语义召回的能力？

---

**Cursor**

对，**你不一定要“训练集”**，但强烈建议有一个**评测集（验证集）**来判断语义召回值不值得上。

你现在这个阶段其实只需要：

- 一份小型 **query 测试集**（20~50 条）
- 每条 query 标注“哪些论文算相关”（哪怕先粗标 3~10 条）
- 对比：
  - `FTS + Qwen关键词改写`
  - `FTS + Qwen + embedding`
- 看指标（如 Recall@20、MRR@10）和人工体验

### 重点
- **不训练模型也能评估**（你现在主要是检索策略评估，不是模型微调）
- 只有当你后面要微调 embedding/reranker，才需要正式训练集

如果你愿意，我下一步可以直接给你一个最小可用的评测模板（CSV 字段 + 评测脚本结构），你可以当天就跑出对比结果。

---

**User**

先不做这个，做方案 A（最推荐，先快跑）
FTS5 继续做主召回
新增一步：query_rewrite() 调 Qwen Max，把用户自然语言改写成：
keywords_zh（中文关键词）
keywords_en（英文关键词）
must_terms（必含词，可选）
把改写后的 query 喂给 FTS5（可分多次召回后合并）
优点：不依赖本地模型；立刻提升“提问句 -> 检索词”效果。
缺点：还不是真 embedding 召回。

---

**Cursor**

明白，我直接按方案 A 落地：接入 Qwen 的 `query_rewrite()`，把改写结果做多路 FTS 召回合并，并保持现有接口兼容可跑。先检查依赖再改代码。

我会在 `search_engine.py` 里新增 Qwen 改写模块（API 调用 + 结构化解析 + 多路 FTS 合并），并保持你现有 `/api/search` 输出不变，避免前端再改。改完我会跑一次本地 smoke test。

已按你说的方案 A 落地完成：**FTS5 主召回 + Qwen Query Rewrite**（不依赖本地 embedding 模型）。

### 我做了什么

主要改在 `backend/app/search_engine.py`：

- 新增 `query_rewrite(query)`：
  - 调 Qwen Chat API（OpenAI 兼容格式）
  - 结构化返回：`keywords_zh` / `keywords_en` / `must_terms`
  - API 失败会自动回退原始 query（不影响服务可用性）

- 新增 `fts_multi_retrieve(query, topn)`：
  - 基于以下多路 query 分别召回并合并去重：
    - 原始 query
    - 中文关键词
    - 英文关键词
    - must_terms
    - 组合 query（原始 + 部分改写词）
  - 各路结果按分数合并（取 max）

- `search_papers()` 改为用 `fts_multi_retrieve(..., topn=100)` 做 FTS 召回主路径。

- 保持现有接口兼容：
  - `/api/search` 返回结构不变（前端不用改）
  - 仍返回 `semantic_enabled`，只是你现在模型没装时为 `false`

---

### 你现在要配的环境变量

在启动后端前，设置：

- `QWEN_API_KEY`（必填）
- `QWEN_MODEL`（可选，默认 `qwen-max`）
- `QWEN_CHAT_URL`（可选，默认  
  `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`）

PowerShell 示例：

```powershell
$env:QWEN_API_KEY="你的token"
$env:QWEN_MODEL="qwen-max"
$env:QWEN_CHAT_URL="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
cd "c:\Users\pilot\Desktop\site\paper_search_tool\backend"
uvicorn app.main:app --reload
```

---

### 说明

- 当前仍可能看到 `semantic_enabled=false`（因为本地 embedding 模型没下载），这是正常的。
- 但搜索已经不是“仅原始 query 的纯 FTS”，而是“**Qwen 改写增强后的多路 FTS**”。  
- 所以你现在就可以直接测试“自然语言提问 -> 更好的关键词检索”效果。

---

**User**

能不能也有一个开关，如果我还没把qwen的api填进去/填进去无法调用，则普通查询

---

**Cursor**

可以，我给你加一个明确开关：即使配置了 key 也能手动关；没配 key 或调用失败则自动退回普通查询。现在就改。

可以，已经加好了，而且行为正是你要的“有开关 + 自动降级”。

### 现在的逻辑

`backend/app/search_engine.py` 已支持：

- **总开关**：`ENABLE_QWEN_REWRITE`
  - `1/true/yes/on` => 开启改写（默认）
  - `0/false/no/off` => 强制关闭，直接普通查询（原始 FTS）
- **没填 `QWEN_API_KEY`** => 自动普通查询
- **填了 key 但调用失败** => 自动普通查询（不会影响搜索接口）

### 你怎么用

PowerShell 示例（关闭改写）：

```powershell
$env:ENABLE_QWEN_REWRITE="0"
uvicorn app.main:app --reload
```

PowerShell 示例（开启改写）：

```powershell
$env:ENABLE_QWEN_REWRITE="1"
$env:QWEN_API_KEY="你的key"
uvicorn app.main:app --reload
```

这样你就可以先稳定跑普通查询，等 API 可用了再一键打开。

---

**User**

再加一个简单的前端展示功能：比如说我搜索了research关键词，则在下面显示的文章里出现了关键词的地方高亮（淡淡荧光黄底色？）

---

**Cursor**

我来加一个轻量高亮：按你输入的查询词，把结果卡片里的标题、摘要预览、引用预览中匹配片段标成淡荧光黄。改完你刷新前端就能看到。

已加好前端关键词高亮功能了。

现在你搜索比如 `research`，结果列表里这些位置会出现**淡荧光黄底色**高亮：

- 标题
- 摘要预览
- 引用预览

实现文件：`frontend/index.html`  
你刷新页面后直接可见（后端不用改）。

如果你想，我还可以下一步加一个小开关（“高亮开/关”）放在搜索框旁边。

---

**User**

所以现在还是没有用AI功能的。我需要AI辅助的地方有：1，使网页支持中英文互相转换FTS，现在是不行的。2，将用户的问题转为搜索关键词？3，要不直接让AI现算？4，我现在的网页能自带全局中英文翻译功能吗？就是弄一个选项选中文还是英文。

我能提供的：大模型的api接口，我还有一些token可以用。

跟我说一下你的解决方案

---

**Cursor**

可以，给你一个**可落地、按成本递进**的方案。你现在有大模型 API 和 token，完全够做。

## 先说结论

- 你提的 4 点都能做。
- 不建议“一步到位全 AI 现算”，成本和延迟都高。
- 最优是：**FTS 继续主检索 + AI 做 query 重写/翻译 + 可选 AI 重排**，前端再加中英文 UI 切换。

---

## 对应你 4 个需求的方案

### 1) 中英文互转后再 FTS（重点）
当前 FTS 对跨语言天然弱，这个必须加一层 AI 改写。

做法：
- 用户输入 `query`
- 调 LLM 产出结构化：
  - `keywords_zh`
  - `keywords_en`
  - `must_terms`
- 用这些词做多路 FTS 召回并合并（你现在已接近这个框架）

效果：  
中文问英文论文、英文问中文论文，会明显改善。

---

### 2) 把用户问题转搜索关键词（必须做）
这个就是上面那层 `query_rewrite`。  
另外建议加两点：
- **缓存**：同一 query 的改写结果缓存（省 token）
- **失败降级**：API 超时/报错自动回原始 query FTS（你已经做了）

---

### 3) “直接让 AI 现算”可不可以？
可以，但分两种：

- **不推荐**：每篇论文都丢给 LLM 算相关性  
  - 太贵、太慢，不适合检索系统
- **推荐**：先 FTS 召回 Top 30~100，再让 AI 只对候选做重排  
  - 成本可控，效果提升明显

所以“AI 现算”应该放在**重排层**，不是全库层。

---

### 4) 前端全局中英文翻译（UI 切换）
可以做，而且建议做成两层：

1. **界面文案 i18n（本地字典）**  
   - 按钮、标题、提示词等固定文案  
   - 快、稳定、零成本
2. **内容翻译（可选）**  
   - 论文标题/摘要/引用的机器翻译  
   - 建议“按需翻译”（点击后翻译）+ 缓存，不要默认每条都翻，省 token

---

## 推荐实施顺序（最快见效）

1. **P1（你马上可用）**
   - Query Rewrite（中英关键词 + must_terms）
   - 多路 FTS 合并召回
   - API 失败自动降级（已具备）
2. **P2**
   - 前端语言切换（中文/英文 UI）
   - 搜索词高亮（你已加）
3. **P3**
   - AI 重排（只重排 TopN）
4. **P4（可选）**
   - 内容翻译（点击翻译、缓存结果）

---

## 你提供 API 后，我可以直接接入这些能力

需要你给我这些信息（不贴 token 明文也行，告诉我变量名）：

- 接口 base URL
- 模型名（如 `qwen-max`）
- 鉴权方式（Bearer token）
- 是否有单独 embedding 接口（如果有，后续可升级双路语义召回）

我拿到后可以直接给你落一版：
- `query_rewrite` 强化版（中英互转 + must terms）
- 前端中英文切换开关
- 可选 “AI 重排” 开关（环境变量控制）

---

**User**

什么叫bearer token

---

**Cursor**

`Bearer token` 就是一种最常见的 API 身份认证方式，意思是：

- 你拿着一串密钥（token）
- 放到请求头里
- 服务端看到这串 token 就认定“你有权限”

格式固定是：

```http
Authorization: Bearer 你的token
```

### 例子（Python）
```python
headers = {
  "Authorization": "Bearer sk-xxxxxx",
  "Content-Type": "application/json"
}
```

### 你这里怎么理解
你说有 Qwen 的 token 包，通常就是要这样传。  
也就是我代码里现在用的这行逻辑：

- `Authorization: Bearer <QWEN_API_KEY>`

### 安全提醒
- token 不要写死在代码里
- 放环境变量（比如 `QWEN_API_KEY`）
- 不要提交到 git / 发到群里

---

**User**

api key：sk-be9d4c3329a84fd0aa3e254561176730
Base URL（请求地址）：https://dashscope.aliyuncs.com/compatible-mode/v1
模型名字qwen-max-2025-01-25
不知道有没有单独embedding接口


---

**Cursor**

很好，这些信息够用了。先提醒一句：你刚才把 API Key 明文发出来了，**建议马上去 DashScope 控制台旋转/重置这个 key**，然后用新 key 继续。

你现在可以直接这样接入（不改代码）：

```powershell
# 在当前终端会话生效
$env:QWEN_API_KEY="你的新key"
$env:QWEN_CHAT_URL="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
$env:QWEN_MODEL="qwen-max-2025-01-25"
$env:ENABLE_QWEN_REWRITE="1"

cd "c:\Users\pilot\Desktop\site\paper_search_tool\backend"
uvicorn app.main:app --reload
```

如果你想长期生效（每次开机都不用再输）：

```powershell
setx QWEN_API_KEY "你的新key"
setx QWEN_CHAT_URL "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
setx QWEN_MODEL "qwen-max-2025-01-25"
setx ENABLE_QWEN_REWRITE "1"
```

然后重开一个终端再启动后端。

---

关于你说的“有没有 embedding 接口”：

- DashScope 是有 embedding 能力的，但是否走同一个兼容地址、具体模型名要看你开通的服务。
- 你可以先不管 embedding，当前已经能用 **Qwen 做 query 改写 + FTS 多路召回**，这一步就能明显改善中英互搜。
- 等你要上真正语义召回，我再帮你接成“可开关的 embedding 路径”。

如果你愿意，我下一步可以直接帮你把 `/api/health` 加上：
- `rewrite_enabled`
- `qwen_model`
这样你一眼就能看到是否真的启用了 AI 改写。

---

**User**

把 /api/health 加上：

rewrite_enabled
qwen_model 这样你一眼就能看到是否真的启用了 AI 改写。

---

**Cursor**

我来直接加到健康检查接口里：返回 `rewrite_enabled` 和 `qwen_model`，你重启后访问 `/api/health` 就能看到。

已经加好了。

现在 `GET /api/health` 会返回：

- `rewrite_enabled`
- `qwen_model`

示例返回结构：

```json
{
  "ok": true,
  "papers": 76,
  "rewrite_enabled": false,
  "qwen_model": "qwen-max"
}
```

你重启后端后访问 `http://127.0.0.1:8000/api/health` 就能一眼确认 AI 改写是否启用。

---

**User**

那我现在怎么加上ai功能重启后端？给我具体的命令

---

**Cursor**

在你当前 `paper-search` 环境里，直接按这个顺序执行（PowerShell）：

```powershell
# 1) 进入后端目录
cd "c:\Users\pilot\Desktop\site\paper_search_tool\backend"

# 2) 配置 AI 改写相关环境变量
$env:QWEN_API_KEY="你的新API_KEY"
$env:QWEN_CHAT_URL="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
$env:QWEN_MODEL="qwen-max-2025-01-25"
$env:ENABLE_QWEN_REWRITE="1"

# 3) 重启后端
uvicorn app.main:app --reload
```

启动后验证是否生效：

```powershell
curl http://127.0.0.1:8000/api/health
```

你要看到类似：

- `"rewrite_enabled": true`
- `"qwen_model": "qwen-max-2025-01-25"`

---

如果 8000 端口被占用，先执行这个再启动：

```powershell
$pidOn8000 = (Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess)
if ($pidOn8000) { Stop-Process -Id $pidOn8000 -Force }
```

---

**User**

(paper-search) c:\Users\pilot\Desktop\site\paper_search_tool\backend>$env:QWEN_API_KEY="sk-ad65f4e546ae413093b2d5b30dc796fb"
文件名、目录名或卷标语法不正确。

(paper-search) c:\Users\pilot\Desktop\site\paper_search_tool\backend>$env:QWEN_CHAT_URL="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
文件名、目录名或卷标语法不正确。

(paper-search) c:\Users\pilot\Desktop\site\paper_search_tool\backend>$env:QWEN_MODEL="qwen-max-2025-01-25"
文件名、目录名或卷标语法不正确。

(paper-search) c:\Users\pilot\Desktop\site\paper_search_tool\backend>$env:ENABLE_QWEN_REWRITE="1"


---

**Cursor**

你现在这个终端是 **cmd**，不是 PowerShell，所以 `$env:...` 会报错。

用 cmd 写法就行：

```bat
cd /d c:\Users\pilot\Desktop\site\paper_search_tool\backend

set QWEN_API_KEY=你的新API_KEY
set QWEN_CHAT_URL=https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
set QWEN_MODEL=qwen-max-2025-01-25
set ENABLE_QWEN_REWRITE=1

uvicorn app.main:app --reload
```

然后验证：

```bat
curl http://127.0.0.1:8000/api/health
```

看到 `rewrite_enabled: true` 就说明 AI 改写启用了。

另外你又贴出了明文 key，建议尽快去 DashScope 控制台**重置/旋转**这个 key，再用新 key。

---

**User**

curl http://127.0.0.1:8000/api/health这个验证在哪里验

---

**Cursor**

就在你**启动后端的同一个终端窗口**里验证就行（cmd）。

### 方法 1：终端里直接验（推荐）
在 `uvicorn app.main:app --reload` 跑着时，新开一个终端窗口，执行：

```bat
curl http://127.0.0.1:8000/api/health
```

会返回一段 JSON，看里面是否有：

- `"rewrite_enabled": true`
- `"qwen_model": "qwen-max-2025-01-25"`

### 方法 2：浏览器里验
直接在浏览器地址栏打开：

`http://127.0.0.1:8000/api/health`

也会看到同样 JSON。

---

如果你 `curl` 说不是命令，就用这个（cmd 一定可用）：

```bat
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/health').read().decode())"
```

---

**User**

C:\Users\pilot>curl http://127.0.0.1:8000/api/health
{"ok":true,"papers":76,"rewrite_enabled":true,"qwen_model":"qwen-max-2025-01-25"}
C:\Users\pilot>

---

**Cursor**

很好，状态是对的：

- `rewrite_enabled: true` ✅
- `qwen_model: qwen-max-2025-01-25` ✅

说明你现在已经启用了 AI 改写链路（query rewrite + 多路 FTS）。

接下来你可以直接去前端测几组：
- 中文问英文主题（如“强化学习在医疗影像的应用”）
- 英文问中文主题（如 “postoperative pain management”）

如果你愿意，我下一步可以再给你加一个 `/api/search/debug`（只开发环境用），把本次实际生成的 `keywords_zh/keywords_en/must_terms` 回显出来，方便你验证 AI 到底改写了什么。

---

**User**

我用中文搜还是什么都没有，是太慢了还是搜不出来？另外中英文选项按钮在哪里

---

**Cursor**

你这个现象不是“慢”，更像是“中文 query 在 FTS 上没召回到”。另外你说得对：中英文切换按钮我还没加到页面里。  
我现在给你做两件事：**后端加中文兜底召回**（避免空结果）+ **前端加中文/英文切换按钮**。

你反馈得很准，两个点我都处理了：

- **不是“太慢”**，更可能是中文 query 没召回到；
- **中英文按钮之前确实没有**，现在已经加上了。

### 现在有什么变化

1. **前端已加中英文按钮**
- 位置：搜索框右侧，按钮文字是 `English` / `中文`
- 功能：切换界面文案（搜索按钮、提示词、结果区标签等）

2. **后端加了“中文空结果兜底”**
- 若多路 FTS 没结果，会走一层轻量 lexical fallback，尽量避免页面全空

---

### 你现在要做的

1. 重启后端（让新代码生效）  
2. 刷新前端页面（`Ctrl+F5`）  
3. 再试中文 query

---

### 如果你还搜不到中文结果

通常是这两种情况：

- `QWEN_REWRITE` 实际没生效（虽然之前 health 显示 true，但可能你重启后环境变量没带上）
- 数据库内容本身主要是英文，你的中文 query 没被成功改写为英文关键词

你可以先用这个确认：

- `http://127.0.0.1:8000/api/health` 看 `rewrite_enabled` 是否还是 `true`

如果你愿意，我下一步可以加一个 `debug` 返回，把本次 query 实际改写成了哪些 `keywords_zh/keywords_en/must_terms` 直接显示出来，这样就能立刻定位“是改写没成功，还是召回策略问题”。

---

**User**

刚刚在后端看到了bugFailed to load semantic model: No module named 'sentence_transformers'
Traceback (most recent call last):
  File "c:\Users\pilot\Desktop\site\paper_search_tool\backend\app\search_engine.py", line 156, in _ensure_embeddings
    from sentence_transformers import SentenceTransformer
ModuleNotFoundError: No module named 'sentence_transformers'


---

**Cursor**

这个报错是环境里没装依赖，不是你业务逻辑坏了。

### 处理方法（在你当前 `paper-search` 环境执行）

```bat
cd /d c:\Users\pilot\Desktop\site\paper_search_tool\backend
pip install -r requirements.txt
```

如果你只想补这个包，也可以：

```bat
pip install sentence-transformers
```

然后重启后端：

```bat
uvicorn app.main:app --reload
```

---

另外说明一下：  
你现在主路径是 `Qwen 改写 + FTS`，所以就算没有 `sentence_transformers`，搜索也能跑；只是会一直打这条语义模型加载错误日志。  
如果你不打算用本地 embedding，我也可以帮你把这条日志降级成一次性 warning，避免刷屏。

---

**User**

ERROR: pip's dependency resolver does not currently take into account all the packages that are installed. This behaviour is the source of the following dependency conflicts.
sqlmodel 0.0.8 requires pydantic<2.0.0,>=1.8.2, but you have pydantic 2.13.2 which is incompatible.
sqlmodel 0.0.8 requires SQLAlchemy<=1.4.41,>=1.4.17, but you have sqlalchemy 2.0.49 which is incompatible.
Successfully installed MarkupSafe-3.0.3 annotated-types-0.7.0 jinja2-3.1.6 joblib-1.5.3 mpmath-1.3.0 networkx-3.6.1 pydantic-2.13.2 pydantic-core-2.46.2 rapidfuzz-3.14.5 regex-2026.4.4 safetensors-0.7.0 scikit-learn-1.8.0 scipy-1.17.1 sentence-transformers-5.4.1 setuptools-81.0.0 sqlalchemy-2.0.49 sympy-1.14.0 threadpoolctl-3.6.0 tokenizers-0.22.2 torch-2.11.0 transformers-5.5.4 typing-inspection-0.4.2 uvicorn-0.44.0

---

**Cursor**

这是 **依赖冲突警告**，不是安装失败。你现在重点看两件事：

- `sentence-transformers` 已经安装成功了 ✅
- 但环境里有旧包 `sqlmodel 0.0.8`，它要求老版本 `pydantic/sqlalchemy`，和你当前项目冲突 ⚠️

## 对你当前项目的影响

- 你的这个项目（当前代码）主要用 `FastAPI + Pydantic v2 + SQLAlchemy 2`，**能跑**。
- 这个警告大概率来自同一个环境里以前装过别的项目依赖（`sqlmodel`）。

## 你现在怎么做（最稳）

1. 先直接启动看看：
```bat
uvicorn app.main:app --reload
```

2. 验证接口：
- `http://127.0.0.1:8000/api/health`
- `http://127.0.0.1:8000/api/search?q=research&limit=5`

如果都正常，这个 warning 可先忽略。

---

## 长期建议（推荐）

给这个项目单独建一个干净虚拟环境，避免历史包污染。  
这样不会再被 `sqlmodel 0.0.8` 干扰。

如果你要，我下一条可以给你一套 **Windows 一键重建干净环境** 的命令（conda 版和 venv 版各一套）。

---

**User**

搜索引擎的代码写在哪里了

---

**Cursor**

搜索引擎的核心代码在后端这里：

- **`backend/app/search_engine.py`**：检索引擎实现（FTS5 索引、召回、重排、Qwen query rewrite、论文加载等）
- **`backend/app/main.py`**：API 路由层（`/api/search` 会调用 `engine.search_papers(...)`）
- **`backend/app/schemas.py`**：搜索/详情接口返回的数据结构定义（Pydantic）

