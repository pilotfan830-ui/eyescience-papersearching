from dataclasses import dataclass
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence
from urllib import error, request

import numpy as np
from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import SQLAlchemyError


@dataclass
class Paper:
    id: int
    title: str
    authors: List[str]
    keywords: List[str]
    abstract: str
    citation: Optional[str]
    doi: Optional[str]
    url: Optional[str]
    year: Optional[int]
    journal: Optional[str]
    volume: Optional[str]
    issue: Optional[str]
    pages: Optional[str]
    page_start: Optional[str]
    page_end: Optional[str]


@dataclass
class QueryRewrite:
    keywords_zh: List[str]
    keywords_en: List[str]
    must_terms: List[str]


@dataclass
class ParsedQuery:
    raw_query: str
    retrieval_query: str
    include_authors: List[str]
    exclude_authors: List[str]
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    must_terms: Optional[List[str]] = None


class SearchTimer:
    def __init__(self) -> None:
        self._started_at = time.perf_counter()
        self.steps: List[Dict] = []
        self._lock = threading.Lock()

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        started_at = time.perf_counter()
        try:
            yield
        finally:
            with self._lock:
                self.steps.append({
                    'name': name,
                    'duration_ms': round((time.perf_counter() - started_at) * 1000, 3),
                })

    def to_dict(self, **extra: object) -> Dict:
        by_step: Dict[str, float] = {}
        for step in self.steps:
            name = str(step['name'])
            by_step[name] = by_step.get(name, 0.0) + float(step['duration_ms'])
        payload = {
            'total_ms': round((time.perf_counter() - self._started_at) * 1000, 3),
            'steps': self.steps,
            'by_step_ms': {name: round(value, 3) for name, value in by_step.items()},
        }
        payload.update(extra)
        return payload


@contextmanager
def _timed_step(timer: Optional[SearchTimer], name: str) -> Iterator[None]:
    if timer is None:
        yield
    else:
        with timer.step(name):
            yield


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default


class SearchEngine:
    def __init__(self, db_path: Optional[str] = None) -> None:
        startup_timer = SearchTimer()
        self._logger = logging.getLogger(__name__)
        self._sql_engine: Optional[Engine] = None
        with startup_timer.step('detect_database'):
            self._configure_data_source(db_path)
        with startup_timer.step('load_papers'):
            self.papers: List[Paper] = self._load_papers()
        with startup_timer.step('build_paper_lookup'):
            self._paper_by_id: Dict[int, Paper] = {p.id: p for p in self.papers}
            self._paper_idx_by_id: Dict[int, int] = {p.id: i for i, p in enumerate(self.papers)}
            years = [p.year for p in self.papers if p.year]
            self._min_year = min(years) if years else None
            self._max_year = max(years) if years else None
        with startup_timer.step('configure_ai_and_cache'):
            self._sentence_model = None
            self._embedding_matrix: Optional[np.ndarray] = None
            self._embedding_paper_ids: List[int] = []
            self._embedding_init_attempted = False
            self.semantic_enabled = False
            self.semantic_error: Optional[str] = None
            self.embedding_provider = (os.getenv('EMBEDDING_PROVIDER') or 'api').strip().lower()
            self.embedding_api_key = (
                os.getenv('EMBEDDING_API_KEY')
                or os.getenv('DASHSCOPE_API_KEY')
                or os.getenv('QWEN_API_KEY')
                or ''
            ).strip()
            self.embedding_api_url = (
                os.getenv('EMBEDDING_API_URL')
                or 'https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings'
            ).strip()
            self.embedding_model = (os.getenv('EMBEDDING_MODEL') or 'text-embedding-v4').strip()
            self.embedding_dimensions = _int_env('EMBEDDING_DIMENSIONS', 1024)
            self.embedding_timeout = _float_env('EMBEDDING_TIMEOUT', 30.0)
            self.local_embedding_model = (
                os.getenv('LOCAL_EMBEDDING_MODEL')
                or 'sentence-transformers/all-MiniLM-L6-v2'
            ).strip()
            self._embedding_lock = threading.Lock()
            self._qwen_api_key = (os.getenv('QWEN_API_KEY') or '').strip()
            self._qwen_model = (os.getenv('QWEN_MODEL') or 'qwen-max').strip()
            self._qwen_url = (
                os.getenv('QWEN_CHAT_URL')
                or 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
            ).strip()
            rewrite_flag = (os.getenv('ENABLE_QWEN_REWRITE') or '1').strip().lower()
            self._rewrite_switch_on = rewrite_flag in {'1', 'true', 'yes', 'on'}
            self.rewrite_enabled = self._rewrite_switch_on and bool(self._qwen_api_key)
            rerank_flag = (os.getenv('ENABLE_QWEN_RERANK') or '1').strip().lower()
            self._qwen_rerank_switch_on = rerank_flag in {'1', 'true', 'yes', 'on'}
            self.qwen_rerank_enabled = self._qwen_rerank_switch_on and bool(self._qwen_api_key)
            self._qwen_rerank_topn = _int_env('QWEN_RERANK_TOPN', 10)
            self._search_parallel_workers = _int_env('SEARCH_PARALLEL_WORKERS', 4)
            self._rewrite_max_queries = _int_env('REWRITE_MAX_QUERIES', 2)
            self._rewrite_wait_grace_seconds = _float_env('REWRITE_WAIT_GRACE_SECONDS', 0.15)
            self._rewrite_cjk_wait_seconds = _float_env('REWRITE_CJK_WAIT_SECONDS', 2.5)
            self._rewrite_min_raw_hits = _int_env('REWRITE_MIN_RAW_HITS', 8)
            self._rewrite_low_confidence = _float_env('REWRITE_LOW_CONFIDENCE', 0.35)
            self._rank_cache_ttl = _float_env('SEARCH_CACHE_TTL', 300.0)
            self._rank_cache: Dict[str, tuple[float, List[Dict], bool]] = {}
            self._rank_cache_lock = threading.Lock()
            self._fts_lock = threading.Lock()
            self._rewrite_debug_lock = threading.Lock()
            self._rerank_debug_lock = threading.Lock()
            self._author_debug_lock = threading.Lock()
            self._last_rewrite_debug: Dict[str, Any] = {
                'status': 'not_run',
                'query': None,
                'retrieval_query': None,
                'rewrite_enabled': self.rewrite_enabled,
            }
            self._last_rerank_debug: Dict[str, Any] = {
                'status': 'not_run',
                'query': None,
                'rerank_enabled': self.qwen_rerank_enabled,
            }
            self._last_author_debug: Dict[str, Any] = {
                'status': 'not_run',
                'query': None,
                'author_recall_enabled': False,
            }
        with startup_timer.step('build_fts_index'):
            self._fts_conn = self._build_fts_index()
        self.startup_timing = startup_timer.to_dict()

    def _set_last_rewrite_debug(self, payload: Mapping[str, Any]) -> None:
        safe_payload = json.loads(json.dumps(dict(payload), ensure_ascii=False, default=str))
        with self._rewrite_debug_lock:
            self._last_rewrite_debug = safe_payload

    def get_last_rewrite_debug(self) -> Dict[str, Any]:
        with self._rewrite_debug_lock:
            return json.loads(json.dumps(self._last_rewrite_debug, ensure_ascii=False, default=str))

    def _set_last_rerank_debug(self, payload: Mapping[str, Any]) -> None:
        safe_payload = json.loads(json.dumps(dict(payload), ensure_ascii=False, default=str))
        with self._rerank_debug_lock:
            self._last_rerank_debug = safe_payload

    def get_last_rerank_debug(self) -> Dict[str, Any]:
        with self._rerank_debug_lock:
            return json.loads(json.dumps(self._last_rerank_debug, ensure_ascii=False, default=str))

    def _set_last_author_debug(self, payload: Mapping[str, Any]) -> None:
        safe_payload = json.loads(json.dumps(dict(payload), ensure_ascii=False, default=str))
        with self._author_debug_lock:
            self._last_author_debug = safe_payload

    def get_last_author_debug(self) -> Dict[str, Any]:
        with self._author_debug_lock:
            return json.loads(json.dumps(self._last_author_debug, ensure_ascii=False, default=str))

    def _configure_data_source(self, db_path: Optional[str]) -> None:
        explicit_source = (
            db_path
            or os.getenv('PAPER_DB_URL')
            or os.getenv('DATABASE_URL')
            or ''
        ).strip()
        if explicit_source:
            self.db_path = explicit_source if '://' in explicit_source else str(Path(explicit_source).expanduser())
            self.db_backend = self._backend_from_source(self.db_path)
            self.db_display = self._safe_source_display(self.db_path)
            return

        host = (os.getenv('PAPER_DB_HOST') or '').strip()
        if host:
            database = (
                os.getenv('PAPER_DB_NAME')
                or os.getenv('PAPER_DB_DATABASE')
                or ''
            ).strip()
            if not database:
                raise ValueError('PAPER_DB_NAME is required when PAPER_DB_HOST is set.')
            url = URL.create(
                'mysql+pymysql',
                username=(os.getenv('PAPER_DB_USER') or 'root').strip() or None,
                password=(os.getenv('PAPER_DB_PASSWORD') or '') or None,
                host=host,
                port=_int_env('PAPER_DB_PORT', 3306),
                database=database,
                query={'charset': (os.getenv('PAPER_DB_CHARSET') or 'utf8mb4').strip() or 'utf8mb4'},
            )
            self.db_path = url.render_as_string(hide_password=False)
            self.db_backend = self._backend_from_source(self.db_path)
            self.db_display = url.render_as_string(hide_password=True)
            return

        self.db_path = self._auto_detect_db()
        self.db_backend = 'sqlite'
        self.db_display = self.db_path

    def _backend_from_source(self, source: str) -> str:
        if '://' not in source:
            return 'sqlite'
        return make_url(source).get_backend_name()

    def _safe_source_display(self, source: str) -> str:
        if '://' not in source:
            return source
        return make_url(source).render_as_string(hide_password=True)

    def _auto_detect_db(self) -> str:
        # __file__ = .../backend/app/search_engine.py
        # parents[0]=app, [1]=backend, [2]=project root (where papers.sqlite is expected)
        base = Path(__file__).resolve().parents[2]
        sqlite_files = sorted(base.glob('*.sqlite'))
        if not sqlite_files:
            raise FileNotFoundError(
                'No sqlite database found in project root. '
                'Set PAPER_DB_URL or PAPER_DB_HOST/PAPER_DB_NAME to use a remote database.'
            )
        return str(sqlite_files[0])

    def _existing_columns(self, conn: sqlite3.Connection, table: str) -> List[str]:
        rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
        return [r[1] for r in rows]

    def _choose(self, cols: List[str], candidates: List[str]) -> Optional[str]:
        low = {c.lower(): c for c in cols}
        for c in candidates:
            if c.lower() in low:
                return low[c.lower()]
        return None

    def _paper_column_map(self, cols: List[str]) -> Dict[str, Optional[str]]:
        id_c = self._choose(cols, ['id']) or cols[0]
        title_c = self._choose(cols, ['title', '标题']) or cols[0]
        # 兼容多种导出/建表字段命名
        abstract_c = self._choose(cols, ['abstract', '摘要', 'abstract_text'])
        year_c = self._choose(cols, ['year', '年份', 'publication_year'])
        doi_c = self._choose(cols, ['doi'])
        url_c = self._choose(cols, ['url', '网址', 'link'])
        citation_c = self._choose(cols, ['citation', '引用', 'citation_text'])
        authors_c = self._choose(cols, ['authors', 'author', '作者', 'authors_display'])
        keywords_c = self._choose(cols, ['keywords', 'keyword', '关键词', 'keywords_text'])
        journal_c = self._choose(cols, ['journal', '期刊'])
        volume_c = self._choose(cols, ['volume', '卷'])
        issue_c = self._choose(cols, ['issue', '期'])
        pages_c = self._choose(cols, ['pages', '页码'])
        page_start_c = self._choose(cols, ['page_start', 'start_page', '起始页'])
        page_end_c = self._choose(cols, ['page_end', 'end_page', '结束页'])
        return {
            'id': id_c,
            'title': title_c,
            'abstract': abstract_c,
            'year': year_c,
            'doi': doi_c,
            'url': url_c,
            'citation': citation_c,
            'authors': authors_c,
            'keywords': keywords_c,
            'journal': journal_c,
            'volume': volume_c,
            'issue': issue_c,
            'pages': pages_c,
            'page_start': page_start_c,
            'page_end': page_end_c,
        }

    def _selected_paper_columns(self, column_map: Mapping[str, Optional[str]]) -> List[str]:
        selected = [column_map['id'], column_map['title']]
        for c in [
            column_map['abstract'],
            column_map['year'],
            column_map['doi'],
            column_map['url'],
            column_map['citation'],
            column_map['authors'],
            column_map['keywords'],
            column_map['journal'],
            column_map['volume'],
            column_map['issue'],
            column_map['pages'],
            column_map['page_start'],
            column_map['page_end'],
        ]:
            if c:
                selected.append(c)
        return list(dict.fromkeys(selected))

    def _split_text_list(self, value: Any) -> List[str]:
        text = str(value or '')
        if not text:
            return []
        return [item.strip() for item in re.split(r'[;,\n\r]+', text) if item and item.strip()]

    def _optional_text(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _optional_year(self, value: Any) -> Optional[int]:
        if value in {None, ''}:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _paper_from_row(
        self,
        row: Mapping[str, Any],
        column_map: Mapping[str, Optional[str]],
    ) -> Paper:
        authors = self._split_text_list(row.get(column_map['authors'])) if column_map['authors'] else []
        keywords = self._split_text_list(row.get(column_map['keywords'])) if column_map['keywords'] else []
        return Paper(
            id=int(row[column_map['id']]),
            title=str(row[column_map['title']] or ''),
            authors=[a for a in authors if a],
            keywords=[k for k in keywords if k],
            abstract=str(row.get(column_map['abstract']) or '') if column_map['abstract'] else '',
            citation=self._optional_text(row.get(column_map['citation'])) if column_map['citation'] else None,
            doi=self._optional_text(row.get(column_map['doi'])) if column_map['doi'] else None,
            url=self._optional_text(row.get(column_map['url'])) if column_map['url'] else None,
            year=self._optional_year(row.get(column_map['year'])) if column_map['year'] else None,
            journal=self._optional_text(row.get(column_map['journal'])) if column_map['journal'] else None,
            volume=self._optional_text(row.get(column_map['volume'])) if column_map['volume'] else None,
            issue=self._optional_text(row.get(column_map['issue'])) if column_map['issue'] else None,
            pages=self._optional_text(row.get(column_map['pages'])) if column_map['pages'] else None,
            page_start=self._optional_text(row.get(column_map['page_start'])) if column_map['page_start'] else None,
            page_end=self._optional_text(row.get(column_map['page_end'])) if column_map['page_end'] else None,
        )

    def _load_papers(self) -> List[Paper]:
        if self.db_backend == 'sqlite' and '://' not in self.db_path:
            return self._load_papers_sqlite()
        return self._load_papers_sqlalchemy()

    def _load_papers_sqlite(self) -> List[Paper]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        tables = {r[0].lower() for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        paper_table = 'papers' if 'papers' in tables else next(iter(tables))
        column_map = self._paper_column_map(self._existing_columns(conn, paper_table))
        selected = self._selected_paper_columns(column_map)
        sql = f"SELECT {', '.join(selected)} FROM {paper_table}"
        papers: List[Paper] = []
        for row in conn.execute(sql).fetchall():
            papers.append(self._paper_from_row(dict(row), column_map))
        if 'paper_keywords' in tables:
            kw_cols = self._existing_columns(conn, 'paper_keywords')
            kw_paper_c = self._choose(kw_cols, ['paper_id'])
            kw_text_c = self._choose(kw_cols, ['keyword', '关键词'])
            kw_order_c = self._choose(kw_cols, ['keyword_order', 'order'])
            if kw_paper_c and kw_text_c:
                order_sql = f' ORDER BY {kw_order_c}' if kw_order_c else ''
                kw_rows = conn.execute(
                    f'SELECT {kw_paper_c}, {kw_text_c} FROM paper_keywords{order_sql}'
                ).fetchall()
                by_id = {p.id: p for p in papers}
                for kw_row in kw_rows:
                    paper = by_id.get(int(kw_row[kw_paper_c]))
                    keyword = str(kw_row[kw_text_c] or '').strip()
                    if paper and keyword and keyword not in paper.keywords:
                        paper.keywords.append(keyword)
        conn.close()
        return papers

    def _get_sql_engine(self) -> Engine:
        if self._sql_engine is None:
            self._sql_engine = create_engine(self.db_path, pool_pre_ping=True)
        return self._sql_engine

    def _load_papers_sqlalchemy(self) -> List[Paper]:
        engine = self._get_sql_engine()
        inspector = inspect(engine)
        tables = {name.lower(): name for name in inspector.get_table_names()}
        if not tables:
            raise RuntimeError(f'No tables found in {self.db_display}')
        paper_table_name = tables.get('papers') or next(iter(tables.values()))
        column_map = self._paper_column_map([col['name'] for col in inspector.get_columns(paper_table_name)])
        selected = self._selected_paper_columns(column_map)
        metadata = MetaData()
        try:
            papers_table = Table(paper_table_name, metadata, autoload_with=engine)
            stmt = select(*(papers_table.c[name] for name in selected))
            papers: List[Paper] = []
            with engine.connect() as conn:
                for row in conn.execute(stmt).mappings().all():
                    papers.append(self._paper_from_row(row, column_map))

                if 'paper_keywords' in tables:
                    kw_table_name = tables['paper_keywords']
                    kw_cols = [col['name'] for col in inspector.get_columns(kw_table_name)]
                    kw_paper_c = self._choose(kw_cols, ['paper_id'])
                    kw_text_c = self._choose(kw_cols, ['keyword', '关键词'])
                    kw_order_c = self._choose(kw_cols, ['keyword_order', 'order'])
                    if kw_paper_c and kw_text_c:
                        kw_table = Table(kw_table_name, metadata, autoload_with=engine)
                        kw_stmt = select(kw_table.c[kw_paper_c], kw_table.c[kw_text_c])
                        if kw_order_c:
                            kw_stmt = kw_stmt.order_by(kw_table.c[kw_order_c])
                        by_id = {p.id: p for p in papers}
                        for kw_row in conn.execute(kw_stmt).mappings().all():
                            paper_id = self._optional_year(kw_row.get(kw_paper_c))
                            keyword = self._optional_text(kw_row.get(kw_text_c))
                            paper = by_id.get(paper_id) if paper_id is not None else None
                            if paper and keyword and keyword not in paper.keywords:
                                paper.keywords.append(keyword)
            return papers
        except SQLAlchemyError as exc:
            raise RuntimeError(f'Failed to load papers from {self.db_display}: {exc}') from exc

    def _build_fts_index(self) -> sqlite3.Connection:
        conn = sqlite3.connect(':memory:', check_same_thread=False)
        conn.execute(
            '''
            CREATE VIRTUAL TABLE papers_fts
            USING fts5(
                paper_id UNINDEXED,
                title,
                authors,
                keywords,
                abstract
            )
            '''
        )
        rows = []
        for p in self.papers:
            rows.append((
                p.id,
                p.title or '',
                ' '.join(p.authors or []),
                ' '.join(p.keywords or []),
                p.abstract or '',
            ))
        conn.executemany(
            'INSERT INTO papers_fts(paper_id, title, authors, keywords, abstract) VALUES (?, ?, ?, ?, ?)',
            rows,
        )
        conn.commit()
        return conn

    def _embedding_key(self) -> tuple[str, str, int]:
        return (self.embedding_provider, self.embedding_model, self.embedding_dimensions)

    def embedding_text(self, paper: Paper) -> str:
        parts = [
            paper.title or '',
            'Keywords: ' + ', '.join(paper.keywords or []),
            paper.abstract or '',
        ]
        return '\n'.join([p.strip() for p in parts if p and p.strip()])

    def embedding_text_hash(self, text: str) -> str:
        return hashlib.sha256((text or '').encode('utf-8')).hexdigest()

    def _embedding_endpoint(self) -> str:
        url = (self.embedding_api_url or '').strip().rstrip('/')
        if not url:
            return 'https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings'
        if url.endswith('/embeddings'):
            return url
        return f'{url}/embeddings'

    def _embedding_store_supported(self) -> bool:
        return self.db_backend == 'sqlite' and '://' not in self.db_path

    def _embedding_table_exists(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_embeddings'"
        ).fetchone()
        return row is not None

    def _ensure_embedding_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS paper_embeddings (
                paper_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                text_hash TEXT NOT NULL,
                embedding_blob BLOB NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (paper_id, provider, model, dimensions)
            )
            '''
        )
        conn.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_paper_embeddings_lookup
            ON paper_embeddings(provider, model, dimensions)
            '''
        )
        conn.commit()

    def _normalize_vectors(self, vectors: Sequence[Sequence[float]]) -> np.ndarray:
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        if arr.shape[1] != self.embedding_dimensions:
            raise ValueError(
                f'Embedding dimensions mismatch: expected {self.embedding_dimensions}, got {arr.shape[1]}'
            )
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms

    def _embed_texts_api(self, texts: Sequence[str]) -> np.ndarray:
        if not self.embedding_api_key:
            raise RuntimeError('embedding API key is not configured')
        payload = {
            'model': self.embedding_model,
            'input': list(texts),
            'dimensions': self.embedding_dimensions,
            'encoding_format': 'float',
        }
        req = request.Request(
            self._embedding_endpoint(),
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.embedding_api_key}',
            },
            method='POST',
        )
        try:
            with request.urlopen(req, timeout=self.embedding_timeout) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')
        except error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore')
            raise RuntimeError(f'embedding API HTTP {exc.code}: {body[:500]}') from exc
        data = json.loads(raw)
        items = data.get('data') or []
        if len(items) != len(texts):
            raise RuntimeError(f'embedding API returned {len(items)} vectors for {len(texts)} texts')
        items = sorted(items, key=lambda x: int(x.get('index', 0)))
        vectors = [item.get('embedding') for item in items]
        if any(v is None for v in vectors):
            raise RuntimeError('embedding API response is missing embedding values')
        return self._normalize_vectors(vectors)

    def _embed_texts_local(self, texts: Sequence[str]) -> np.ndarray:
        if self._sentence_model is None:
            from sentence_transformers import SentenceTransformer
            self._sentence_model = SentenceTransformer(self.local_embedding_model, local_files_only=True)
        vectors = self._sentence_model.encode(list(texts), normalize_embeddings=True)
        return self._normalize_vectors(vectors)

    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        clean = [str(t or '').strip() for t in texts]
        if not clean:
            return np.empty((0, self.embedding_dimensions), dtype=np.float32)
        if self.embedding_provider == 'local':
            return self._embed_texts_local(clean)
        return self._embed_texts_api(clean)

    def warm_ai_components(self) -> Dict:
        timer = SearchTimer()
        with timer.step('semantic_embedding_matrix_load'):
            matrix_loaded = self._load_embedding_matrix()
        local_model_loaded = False
        if self.embedding_provider == 'local':
            try:
                with timer.step('local_embedding_model_load'):
                    self.embed_texts(['warmup'])
                local_model_loaded = True
            except Exception as exc:
                self.semantic_error = str(exc)
                self._logger.warning('Local embedding model warmup failed: %s', exc)
        with timer.step('qwen_client_config_check'):
            rewrite_enabled = self.rewrite_enabled
            rerank_enabled = self.qwen_rerank_enabled
        return timer.to_dict(
            semantic_matrix_loaded=matrix_loaded,
            local_model_loaded=local_model_loaded,
            rewrite_enabled=rewrite_enabled,
            qwen_rerank_enabled=rerank_enabled,
        )

    def _fresh_embedding_rows(self) -> List[sqlite3.Row]:
        if not self._embedding_store_supported():
            return []
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if not self._embedding_table_exists(conn):
                return []
            provider, model, dimensions = self._embedding_key()
            rows = conn.execute(
                '''
                SELECT paper_id, text_hash, embedding_blob
                FROM paper_embeddings
                WHERE provider = ? AND model = ? AND dimensions = ?
                ORDER BY paper_id
                ''',
                (provider, model, dimensions),
            ).fetchall()
            expected_hash = {
                p.id: self.embedding_text_hash(self.embedding_text(p))
                for p in self.papers
            }
            return [r for r in rows if expected_hash.get(int(r['paper_id'])) == r['text_hash']]
        finally:
            conn.close()

    def _load_embedding_matrix(self, force: bool = False) -> bool:
        with self._embedding_lock:
            if self._embedding_matrix is not None and not force:
                return len(self._embedding_paper_ids) > 0
            rows = self._fresh_embedding_rows()
            vectors: List[np.ndarray] = []
            paper_ids: List[int] = []
            for row in rows:
                vec = np.frombuffer(row['embedding_blob'], dtype=np.float32)
                if vec.shape[0] != self.embedding_dimensions:
                    continue
                vectors.append(vec)
                paper_ids.append(int(row['paper_id']))
            if not vectors:
                self._embedding_matrix = None
                self._embedding_paper_ids = []
                return False
            matrix = np.vstack(vectors).astype(np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._embedding_matrix = matrix / norms
            self._embedding_paper_ids = paper_ids
            return True

    def embedding_status(self) -> Dict:
        if not self._embedding_store_supported():
            return {
                'semantic_enabled': False,
                'semantic_error': (
                    'persisted paper embeddings are currently supported only for local sqlite sources'
                ),
                'embedding_provider': self.embedding_provider,
                'embedding_model': self.embedding_model,
                'embedding_dimensions': self.embedding_dimensions,
                'embedded_papers': 0,
                'embedding_stale_count': len(self.papers),
            }
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if not self._embedding_table_exists(conn):
                fresh = 0
            else:
                provider, model, dimensions = self._embedding_key()
                rows = conn.execute(
                    '''
                    SELECT paper_id, text_hash
                    FROM paper_embeddings
                    WHERE provider = ? AND model = ? AND dimensions = ?
                    ''',
                    (provider, model, dimensions),
                ).fetchall()
                expected_hash = {
                    p.id: self.embedding_text_hash(self.embedding_text(p))
                    for p in self.papers
                }
                fresh = sum(
                    1
                    for r in rows
                    if expected_hash.get(int(r['paper_id'])) == r['text_hash']
                )
        finally:
            conn.close()
        stale = max(len(self.papers) - fresh, 0)
        can_query = self.embedding_provider == 'local' or bool(self.embedding_api_key)
        status_error = self.semantic_error
        if not can_query:
            status_error = 'embedding API key is not configured'
        elif fresh == 0:
            status_error = 'no fresh paper embeddings found; run backend/tools/build_embeddings.py'
        elif status_error in {
            'embedding API key is not configured',
            'no fresh paper embeddings found; run backend/tools/build_embeddings.py',
        }:
            status_error = None
        return {
            'semantic_enabled': bool(can_query and fresh > 0 and not status_error),
            'semantic_error': status_error,
            'embedding_provider': self.embedding_provider,
            'embedding_model': self.embedding_model,
            'embedding_dimensions': self.embedding_dimensions,
            'embedded_papers': fresh,
            'embedding_stale_count': stale,
        }

    def build_paper_embeddings(
        self,
        force: bool = False,
        batch_size: int = 16,
        dry_run: bool = False,
    ) -> Dict:
        if not self._embedding_store_supported():
            return {
                'provider': self.embedding_provider,
                'model': self.embedding_model,
                'dimensions': self.embedding_dimensions,
                'papers': len(self.papers),
                'pending': 0,
                'built': 0,
                'dry_run': dry_run,
                'error': 'persisted paper embeddings are currently supported only for local sqlite sources',
            }
        batch_size = max(int(batch_size or 1), 1)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if not dry_run:
                self._ensure_embedding_table(conn)
            table_exists = self._embedding_table_exists(conn)
            provider, model, dimensions = self._embedding_key()
            stored: Dict[int, str] = {}
            if table_exists:
                rows = conn.execute(
                    '''
                    SELECT paper_id, text_hash
                    FROM paper_embeddings
                    WHERE provider = ? AND model = ? AND dimensions = ?
                    ''',
                    (provider, model, dimensions),
                ).fetchall()
                stored = {int(r['paper_id']): str(r['text_hash']) for r in rows}

            todo = []
            for paper in self.papers:
                text = self.embedding_text(paper)
                if not text:
                    continue
                text_hash = self.embedding_text_hash(text)
                if force or stored.get(paper.id) != text_hash:
                    todo.append((paper, text, text_hash))

            result = {
                'provider': provider,
                'model': model,
                'dimensions': dimensions,
                'papers': len(self.papers),
                'pending': len(todo),
                'built': 0,
                'dry_run': dry_run,
            }
            if dry_run or not todo:
                return result

            for start in range(0, len(todo), batch_size):
                chunk = todo[start:start + batch_size]
                vectors = self.embed_texts([item[1] for item in chunk])
                rows = []
                for (paper, _text, text_hash), vector in zip(chunk, vectors):
                    rows.append((
                        paper.id,
                        provider,
                        model,
                        dimensions,
                        text_hash,
                        vector.astype(np.float32).tobytes(),
                    ))
                conn.executemany(
                    '''
                    INSERT OR REPLACE INTO paper_embeddings(
                        paper_id, provider, model, dimensions, text_hash, embedding_blob, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ''',
                    rows,
                )
                conn.commit()
                result['built'] += len(rows)
            self._load_embedding_matrix(force=True)
            return result
        finally:
            conn.close()

    def _extract_json_obj(self, text: str) -> Optional[Dict]:
        txt = (text or '').strip()
        if not txt:
            return None
        try:
            return json.loads(txt)
        except Exception:
            pass
        m = re.search(r'\{.*\}', txt, flags=re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None

    def _clean_rewrite_candidate(self, raw_query: str, candidate: str) -> str:
        raw = (raw_query or '').strip()
        text = re.sub(r'\s+', ' ', str(candidate or '').strip())
        if not text:
            return ''
        has_cjk = bool(re.search(r'[\u4e00-\u9fff]', text))
        has_ascii = bool(re.search(r'[A-Za-z]', text))
        cjk_chars = re.findall(r'[\u4e00-\u9fff]', text)
        ascii_tokens = re.findall(r'[A-Za-z]+(?:-[A-Za-z]+)?', text)
        if has_cjk and len(cjk_chars) < 2:
            return ''
        if has_ascii and not has_cjk and len(ascii_tokens) == 1 and len(ascii_tokens[0]) <= 1:
            return ''
        if has_cjk and has_ascii and not self._is_mixed_language_query(raw):
            return ''
        return text

    def _direct_english_rewrite(self, raw_query: str, rewrite: QueryRewrite) -> str:
        raw = (raw_query or '').strip()
        if not self._contains_cjk(raw):
            return ''

        for term in rewrite.keywords_en:
            cleaned = self._clean_rewrite_candidate(raw, term)
            if cleaned and re.search(r'[A-Za-z]', cleaned) and not re.search(r'[\u4e00-\u9fff]', cleaned):
                return cleaned

        for term in self._local_query_expansions(raw):
            cleaned = self._clean_rewrite_candidate(raw, term)
            if cleaned and re.search(r'[A-Za-z]', cleaned) and not re.search(r'[\u4e00-\u9fff]', cleaned):
                return cleaned

        return ''

    def query_rewrite(self, query: str) -> QueryRewrite:
        q = (query or '').strip()
        if not q:
            return QueryRewrite([], [], [])
        if not self._rewrite_switch_on:
            return QueryRewrite([], [], [])
        if not self._qwen_api_key:
            return QueryRewrite([], [], [])

        system_prompt = (
            'You are a conservative academic paper search query rewriter. '
            'Your job is narrow translation and terminology normalization, not broad expansion. '
            'Return JSON only with keys: keywords_zh, keywords_en, must_terms. '
            'Use short phrases only. '
            'Keep the original intent and scope. Do not add adjacent diseases, broader topics, symptoms, '
            'treatments, populations, or speculative related concepts. '
            'Prefer exact cross-lingual translation and one close academic term if needed. '
            'Limit output strictly: keywords_zh <= 1, keywords_en <= 2, must_terms <= 1. '
            'If the query is Chinese, keywords_en[0] must be the direct English medical/academic translation '
            'of the main query, suitable to run as a standalone FTS query. '
            'Do not place broader related concepts before the direct translation. '
            'If the query is English, keywords_zh should contain at most one exact Chinese translation. '
            'Default to must_terms = []. '
            'Use must_terms only when the query contains an indispensable literal token that must be preserved verbatim. '
            'Never output a single character or single letter in must_terms. '
            'Only use must_terms for truly indispensable literal terms. '
            'Do not include explanation.'
        )
        user_prompt = (
            f'User query: {q}\n'
            'Return only high-value rewrite terms with minimal noise.'
        )
        payload = {
            'model': self._qwen_model,
            'temperature': 0.1,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            'response_format': {'type': 'json_object'},
        }
        req = request.Request(
            self._qwen_url,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self._qwen_api_key}',
            },
            method='POST',
        )
        try:
            with request.urlopen(req, timeout=12) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')
            data = json.loads(raw)
            content = (
                data.get('choices', [{}])[0]
                .get('message', {})
                .get('content', '')
            )
            obj = self._extract_json_obj(content) or {}
            kw_zh = [str(x).strip() for x in (obj.get('keywords_zh') or []) if str(x).strip()]
            kw_en = [str(x).strip() for x in (obj.get('keywords_en') or []) if str(x).strip()]
            must = [str(x).strip() for x in (obj.get('must_terms') or []) if str(x).strip()]
            must = [
                x for x in must
                if (
                    (len(re.findall(r'[\u4e00-\u9fff]', x)) >= 2)
                    or (len(''.join(re.findall(r'[A-Za-z0-9]+', x))) >= 3)
                )
            ]
            return QueryRewrite(
                keywords_zh=list(dict.fromkeys(kw_zh))[:1],
                keywords_en=list(dict.fromkeys(kw_en))[:2],
                must_terms=list(dict.fromkeys(must))[:1],
            )
        except Exception as exc:
            self._logger.warning('Query rewrite failed, fallback to raw query: %s', exc)
            self.rewrite_enabled = False
            return QueryRewrite([], [], [])

    def _semantic_scores(self, query: str, timer: Optional[SearchTimer] = None) -> Optional[np.ndarray]:
        q = (query or '').strip()
        if not q:
            return None
        if not self._embedding_store_supported():
            self.semantic_enabled = False
            self.semantic_error = (
                'semantic search is unavailable because persisted paper embeddings only support local sqlite sources'
            )
            return None
        if self.embedding_provider != 'local' and not self.embedding_api_key:
            self.semantic_enabled = False
            self.semantic_error = 'embedding API key is not configured'
            return None
        with _timed_step(timer, 'semantic_embedding_matrix_load'):
            matrix_loaded = self._load_embedding_matrix()
        if not matrix_loaded:
            self.semantic_enabled = False
            self.semantic_error = 'no fresh paper embeddings found; run backend/tools/build_embeddings.py'
            return None
        try:
            with _timed_step(timer, 'semantic_query_embedding_ai'):
                qv = self.embed_texts([q])[0]
        except Exception as exc:
            self.semantic_enabled = False
            self.semantic_error = str(exc)
            self._logger.warning('Query embedding failed, fallback to FTS only: %s', exc)
            return None
        self.semantic_enabled = True
        self.semantic_error = None
        with _timed_step(timer, 'semantic_similarity_dot'):
            return np.dot(self._embedding_matrix, qv)

    def fts_retrieve(self, query: str, topn: int = 100) -> Dict[int, float]:
        q = query.strip()
        if not q:
            return {}

        try:
            with self._fts_lock:
                rows = self._fts_conn.execute(
                    '''
                    SELECT paper_id, bm25(papers_fts) AS score
                    FROM papers_fts
                    WHERE papers_fts MATCH ?
                    ORDER BY score ASC
                    LIMIT ?
                    ''',
                    (q, topn),
                ).fetchall()
        except sqlite3.OperationalError:
            escaped = '"{}"'.format(q.replace('"', '""'))
            with self._fts_lock:
                rows = self._fts_conn.execute(
                    '''
                    SELECT paper_id, bm25(papers_fts) AS score
                    FROM papers_fts
                    WHERE papers_fts MATCH ?
                    ORDER BY score ASC
                    LIMIT ?
                    ''',
                    (escaped, topn),
                ).fetchall()
        if not rows:
            return {}

        raw_scores = [float(r[1]) for r in rows]
        min_s, max_s = min(raw_scores), max(raw_scores)
        out: Dict[int, float] = {}
        if max_s == min_s:
            for pid, _ in rows:
                out[int(pid)] = 1.0
            return out

        for pid, raw in rows:
            norm = (max_s - float(raw)) / (max_s - min_s)
            out[int(pid)] = float(np.clip(norm, 0.0, 1.0))
        return out

    def _merge_score_maps(self, base: Dict[int, float], extra: Dict[int, float]) -> Dict[int, float]:
        out = dict(base)
        for k, v in extra.items():
            if k in out:
                out[k] = max(out[k], v)
            else:
                out[k] = v
        return out

    def _contains_cjk(self, text: str) -> bool:
        return bool(re.search(r'[\u4e00-\u9fff]', text or ''))

    def _fallback_terms(self, query: str) -> List[str]:
        q = (query or '').strip()
        if not q:
            return []
        if self._contains_cjk(q):
            q = re.sub(r'\s+', '', q)
            if len(q) <= 2:
                return [q]
            grams = [q[i:i + 2] for i in range(len(q) - 1)]
            return list(dict.fromkeys([q] + grams))
        parts = [x.strip().lower() for x in q.split() if x.strip()]
        return list(dict.fromkeys(parts or [q.lower()]))

    def _local_query_expansions(self, query: str) -> List[str]:
        q = (query or '').strip().lower()
        if not q:
            return []
        synonym_map = {
            '白内障': ['cataract', 'cataract surgery', 'congenital cataract'],
            '近视': ['myopia', 'high myopia'],
            '高度近视': ['high myopia'],
            '青光眼': ['glaucoma'],
            '糖尿病视网膜病变': ['diabetic retinopathy'],
            '视网膜病变': ['retinopathy'],
            '视网膜': ['retina', 'retinal'],
            '眼压': ['intraocular pressure'],
            '干眼': ['dry eye'],
            '斜视': ['strabismus'],
            '弱视': ['amblyopia'],
            '角膜': ['cornea', 'corneal'],
            '眼底': ['fundus'],
            '人工智能': ['artificial intelligence'],
            '黄斑': ['macular'],
            '葡萄膜炎': ['uveitis'],
            '视力': ['visual acuity'],
            '屈光': ['refractive', 'refraction'],
            '泪器病': ['lacrimal disease', 'lacrimal', 'nasolacrimal', 'lacrimal duct'],
            '泪器': ['lacrimal', 'nasolacrimal', 'lacrimal duct'],
        }
        expansions: List[str] = []
        for term, mapped in synonym_map.items():
            if term in q:
                expansions.extend(mapped)
            for candidate in mapped:
                if candidate in q:
                    expansions.append(term)
        return list(dict.fromkeys(expansions))

    def lexical_fallback_retrieve(self, query: str, topn: int = 100) -> Dict[int, float]:
        terms = self._fallback_terms(query)
        if not terms:
            return {}
        scored: List[tuple[int, float]] = []
        for p in self.papers:
            text = f"{p.title} {' '.join(p.keywords)} {p.abstract}".lower()
            hits = sum(1 for t in terms if t and t.lower() in text)
            if hits > 0:
                scored.append((p.id, hits / max(len(terms), 1)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return {pid: float(score) for pid, score in scored[:topn]}

    def keyword_retrieve(self, query: str, topn: int = 100) -> Dict[int, float]:
        raw_query = (query or '').strip().lower()
        terms = self._fallback_terms(query)
        if not terms:
            return {}
        scored: List[tuple[int, float]] = []
        for p in self.papers:
            keywords = [k.strip().lower() for k in (p.keywords or []) if k.strip()]
            if not keywords:
                continue
            joined = ' '.join(keywords)
            query_parts = [x for x in raw_query.split() if x]
            if raw_query and any(raw_query == keyword for keyword in keywords):
                best = 1.0
            elif raw_query and raw_query in joined:
                best = 0.9
            elif len(query_parts) > 1:
                matched = sum(1 for part in query_parts if part in joined)
                best = 0.35 * (matched / len(query_parts)) if matched else 0.0
            else:
                best = 0.0
                for term in terms:
                    t = term.lower().strip()
                    if not t:
                        continue
                    for keyword in keywords:
                        if t == keyword:
                            best = max(best, 1.0)
                        elif t in keyword or keyword in t:
                            best = max(best, 0.85)
            if best > 0:
                scored.append((p.id, best))
        scored.sort(key=lambda x: x[1], reverse=True)
        return {pid: float(score) for pid, score in scored[:topn]}

    def author_retrieve(self, query: str, topn: int = 100) -> Dict[int, float]:
        raw_query = (query or '').strip().lower()
        terms = [t.lower().strip() for t in self._fallback_terms(query) if t.strip()]
        if not raw_query and not terms:
            return {}
        surname = self._extract_author_surname(query)
        scored: List[tuple[int, float]] = []
        for p in self.papers:
            author_text = ' '.join(p.authors or []).lower()
            if not author_text:
                continue
            score = 0.0
            if surname and self._has_cjk_author_surname(p, surname):
                score = max(score, 0.92)
            if raw_query and raw_query in author_text:
                score = 1.0
            latin_terms = [
                t for t in re.findall(r'[a-z]+', raw_query)
                if len(t) >= 2 and t not in {'the', 'and', 'for', 'paper', 'article', 'recent', 'years'}
            ]
            if latin_terms:
                matched = sum(1 for t in latin_terms if t in author_text)
                if matched:
                    score = max(score, matched / len(latin_terms))
            cjk_terms = [
                t for t in terms
                if self._contains_cjk(t)
                and len(t) >= 2
                and t not in {'近两', '最近', '两年', '近三', '三年', '文章', '论文', '作者'}
            ]
            if cjk_terms:
                matched = sum(1 for t in cjk_terms if t in author_text)
                if matched >= 2:
                    score = max(score, 1.0)
                elif matched == 1:
                    score = max(score, 0.55)
            if score > 0:
                scored.append((p.id, float(np.clip(score, 0.0, 1.0))))
        scored.sort(key=lambda x: x[1], reverse=True)
        return {pid: score for pid, score in scored[:topn]}

    def _extract_author_surname(self, query: str) -> Optional[str]:
        q = query or ''
        m = re.search(r'姓\s*([\u4e00-\u9fff])', q)
        if m:
            return m.group(1)
        m = re.search(r'([\u4e00-\u9fff])\s*(?:院长|医生|医师|主任|教授|老师|作者|的文章|的论文)', q)
        if m:
            return m.group(1)
        if re.fullmatch(r'[\u4e00-\u9fff]', q.strip()):
            return q.strip()
        return None

    def _has_cjk_author_surname(self, paper: Paper, surname: str) -> bool:
        for author in paper.authors or []:
            for name in re.findall(r'\(([\u4e00-\u9fff][\u4e00-\u9fff\s]*)\)', author):
                if name.strip().startswith(surname):
                    return True
            stripped = author.strip()
            if re.match(r'^[\u4e00-\u9fff]', stripped) and stripped.startswith(surname):
                return True
        return False

    def _extract_excluded_authors(self, query: str) -> List[str]:
        q = query or ''
        patterns = [
            r'(?:不要|不看|排除|去掉|剔除|过滤掉|不包括|不含|别要)\s*([\u4e00-\u9fff]{2,6}?)(?:的(?:文章|论文|文献|研究)|文章|论文|文献|研究|[,，;；。]|$)',
            r'(?:作者\s*)?(?:不是|非)\s*([\u4e00-\u9fff]{2,6}?)(?:的(?:文章|论文|文献|研究)|文章|论文|文献|研究|[,，;；。]|$)',
            r'(?:exclude|without|not)\s+([A-Za-z][A-Za-z .\'-]{1,40}?)(?:\s+(?:papers?|articles?|studies?)|[,，;；.]|$)',
        ]
        authors: List[str] = []
        for pattern in patterns:
            for match in re.finditer(pattern, q, flags=re.IGNORECASE):
                name = match.group(1).strip(' ,，。.;；:：')
                if name:
                    authors.append(name)
        return list(dict.fromkeys(authors))

    def _extract_included_authors(self, query: str) -> List[str]:
        q = query or ''
        patterns = [
            r'(?:作者(?:是|为|:|：)?|只看|仅看|只要|包括|包含)\s*([\u4e00-\u9fff]{2,6}?)(?:的(?:文章|论文|文献|研究)|文章|论文|文献|研究|[,，;；。]|$)',
            r'([\u4e00-\u9fff]{2,6}?)(?:的)(?:文章|论文|文献|研究)',
            r'^([\u4e00-\u9fff]{2,6}?)(?:的).*(?:相关文章|相关论文|相关文献|相关研究|文章|论文|文献|研究)',
            r'(?:author\s*:?\s*|by\s+|include\s+)([A-Za-z][A-Za-z .\'-]{1,40}?)(?:\s+(?:papers?|articles?|studies?)|[,，;；.]|$)',
        ]
        authors: List[str] = []
        excluded = set(self._extract_excluded_authors(q))
        for pattern in patterns:
            for match in re.finditer(pattern, q, flags=re.IGNORECASE):
                name = match.group(1).strip(' ,，。.;；:：')
                name = re.sub(r'^(?:只看|仅看|只要|包括|包含|作者是|作者为|作者)', '', name).strip()
                if re.match(r'^(?:不要|不看|排除|去掉|剔除|过滤掉|不包括|不含|别要)', name):
                    continue
                if name and name not in excluded and self._known_author_name(name):
                    authors.append(name)
        return list(dict.fromkeys(authors))

    def _strip_exclusion_clauses(self, query: str) -> str:
        q = query or ''
        patterns = [
            r'(?:[,，;；。]\s*)?(?:不要|不看|排除|去掉|剔除|过滤掉|不包括|不含|别要)\s*[\u4e00-\u9fff]{2,6}(?:的)?(?:文章|论文|文献|研究)?',
            r'(?:[,，;；。]\s*)?(?:作者\s*)?(?:不是|非)\s*[\u4e00-\u9fff]{2,6}(?:的)?(?:文章|论文|文献|研究)?',
            r'(?:[,，;；。]\s*)?(?:exclude|without|not)\s+[A-Za-z][A-Za-z .\'-]{1,40}(?:\s+(?:papers?|articles?|studies?))?',
        ]
        for pattern in patterns:
            q = re.sub(pattern, ' ', q, flags=re.IGNORECASE)
        return re.sub(r'\s+', ' ', q).strip(' ,，。.;；:：')

    def _strip_include_author_clauses(self, query: str) -> str:
        q = query or ''
        patterns = [
            r'(?:[,，;；。]\s*)?(?:作者(?:是|为|:|：)?|只看|仅看|只要|包括|包含)\s*[\u4e00-\u9fff]{2,6}(?:的)?(?:文章|论文|文献|研究)?',
            r'(?:[,，;；。]\s*)?(?:author\s*:?\s*|by\s+|include\s+)[A-Za-z][A-Za-z .\'-]{1,40}(?:\s+(?:papers?|articles?|studies?))?',
        ]
        for pattern in patterns:
            q = re.sub(pattern, ' ', q, flags=re.IGNORECASE)
        return re.sub(r'\s+', ' ', q).strip(' ,，。.;；:：')

    def _strip_generic_intent_words(self, query: str) -> str:
        q = query or ''
        q = re.sub(r'(?:的)?(?:相关文章|相关论文|相关文献|相关研究|文章|论文|文献|研究)$', '', q)
        q = re.sub(r'\b(?:papers?|articles?|studies?|literature)\b$', '', q, flags=re.IGNORECASE)
        return re.sub(r'\s+', ' ', q).strip(' ,，。.;；:：')

    def _known_author_name(self, name: str) -> bool:
        name_norm = self._normalize_author_match_text(name)
        if not name_norm:
            return False
        for paper in self.papers:
            author_text = ' '.join(paper.authors or [])
            if name_norm in self._normalize_author_match_text(author_text):
                return True
        return False

    def parse_query(
        self,
        query: str,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
    ) -> ParsedQuery:
        raw = (query or '').strip()
        inferred_year_from, inferred_year_to = self._infer_year_range(raw, year_from, year_to)
        exclude_authors = self._extract_excluded_authors(raw)
        no_exclusion_query = self._strip_exclusion_clauses(raw)
        include_authors = self._extract_included_authors(no_exclusion_query)

        retrieval_query = no_exclusion_query
        retrieval_query = self._strip_include_author_clauses(retrieval_query)
        retrieval_query = re.sub(
            r'(?:今年|去年|(?:近|最近)\s*[一二两三四五六七八九十\d]+\s*年)',
            ' ',
            retrieval_query,
        )
        retrieval_query = self._strip_generic_intent_words(retrieval_query)
        retrieval_query = re.sub(r'\s+', ' ', retrieval_query).strip(' ,，。.;；:：')
        if not retrieval_query:
            retrieval_query = raw
        must_terms = self._local_query_expansions(retrieval_query)
        return ParsedQuery(
            raw_query=raw,
            retrieval_query=retrieval_query,
            include_authors=include_authors,
            exclude_authors=exclude_authors,
            year_from=inferred_year_from,
            year_to=inferred_year_to,
            must_terms=must_terms,
        )

    def _normalize_author_match_text(self, text: str) -> str:
        return re.sub(r'[\s,，。.;；:：()（）#\'"-]+', '', (text or '').lower())

    def _paper_has_excluded_author(self, paper: Paper, excluded_authors: Sequence[str]) -> bool:
        if not excluded_authors:
            return False
        author_text = ' '.join(paper.authors or [])
        author_text_low = author_text.lower()
        author_text_norm = self._normalize_author_match_text(author_text)
        for name in excluded_authors:
            name_low = name.lower().strip()
            name_norm = self._normalize_author_match_text(name)
            if name_low and name_low in author_text_low:
                return True
            if name_norm and name_norm in author_text_norm:
                return True
        return False

    def _paper_has_any_author(self, paper: Paper, include_authors: Sequence[str]) -> bool:
        if not include_authors:
            return True
        author_text = ' '.join(paper.authors or [])
        author_text_low = author_text.lower()
        author_text_norm = self._normalize_author_match_text(author_text)
        for name in include_authors:
            name_low = name.lower().strip()
            name_norm = self._normalize_author_match_text(name)
            if name_low and name_low in author_text_low:
                return True
            if name_norm and name_norm in author_text_norm:
                return True
        return False

    def _author_intent_enabled(self, parsed: ParsedQuery) -> bool:
        raw = (parsed.raw_query or '').strip()
        if parsed.include_authors or parsed.exclude_authors:
            return True
        if re.match(r'^[\u4e00-\u9fff]{2,6}的', raw) and re.search(
            r'(?:相关文章|相关论文|相关文献|相关研究|文章|论文|文献|研究|教授|医生|主任|老师)',
            raw,
        ):
            return True
        if self._extract_author_surname(raw):
            return True
        if re.search(r'(?:作者|教授|医生|医师|主任|院长|老师|by\s+|author\s*:?)', raw, flags=re.IGNORECASE):
            return True
        if raw and len(raw) <= 24 and self._known_author_name(raw):
            return True
        return False

    def _is_mixed_language_query(self, query: str) -> bool:
        q = query or ''
        return bool(re.search(r'[\u4e00-\u9fff]', q) and re.search(r'[A-Za-z]', q))

    def _is_colloquial_query(self, query: str) -> bool:
        q = (query or '').strip().lower()
        if not q:
            return False
        patterns = [
            r'怎么',
            r'哪些',
            r'有没有',
            r'帮我',
            r'想找',
            r'推荐',
            r'是什么',
            r'关系',
            r'相关',
            r'最新',
            r'经典',
            r'how',
            r'what',
            r'which',
            r'help me',
        ]
        return bool(any(re.search(pattern, q) for pattern in patterns) or re.search(r'[?？]', q))

    def _select_rewrite_queries(
        self,
        raw_query: str,
        candidates: Sequence[str],
        limit: int = 2,
    ) -> List[str]:
        raw = (raw_query or '').strip()
        raw_norm = raw.lower()
        ranked: List[tuple[float, int, str]] = []
        seen = {raw_norm}
        for idx, candidate in enumerate(candidates):
            text = self._clean_rewrite_candidate(raw, str(candidate or '').strip())
            norm = text.lower()
            if not text or norm in seen:
                continue
            seen.add(norm)
            ranked.append((self._rewrite_candidate_priority(raw, text), idx, text))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [text for _, _, text in ranked[:max(limit, 0)]]

    def _rewrite_candidates(self, raw_query: str, rewrite: QueryRewrite) -> List[str]:
        raw = (raw_query or '').strip()
        selected: List[str] = []
        candidates: List[str] = []
        selected_norms: set[str] = set()

        if self._contains_cjk(raw):
            direct_translation = self._direct_english_rewrite(raw, rewrite)
            if direct_translation:
                selected.append(direct_translation)
                selected_norms.add(direct_translation.lower())
            for term in rewrite.keywords_en:
                if term and term.strip().lower() not in selected_norms:
                    candidates.append(term)
            candidates.extend(self._local_query_expansions(raw)[:2])
        else:
            candidates.extend(self._local_query_expansions(raw)[:2])
            candidates.extend(rewrite.keywords_en[:2])

        if re.search(r'[A-Za-z]', raw):
            candidates.extend(rewrite.keywords_zh[:1])
        if not self._contains_cjk(raw):
            candidates.extend(rewrite.must_terms[:1])
        candidates.extend(rewrite.keywords_zh[1:2])
        candidates = [c for c in candidates if str(c or '').strip().lower() not in selected_norms]

        remaining = max(2 - len(selected), 0)
        if remaining <= 0:
            return selected[:2]
        return selected + self._select_rewrite_queries(raw, candidates, limit=remaining)

    def _requires_cjk_rewrite_fts(self, query: str) -> bool:
        q = (query or '').strip()
        return bool(q and self._contains_cjk(q))

    def _should_expand_with_rewrite(
        self,
        query: str,
        raw_fts_scores: Mapping[int, float],
    ) -> bool:
        q = (query or '').strip()
        if self._requires_cjk_rewrite_fts(q):
            return True
        raw_count = len(raw_fts_scores)
        raw_top_score = max((float(v) for v in raw_fts_scores.values()), default=0.0)
        short_query = len(re.findall(r'[\w\u4e00-\u9fff]+', q)) <= 2 or len(q) <= 8
        return bool(
            raw_count < self._rewrite_min_raw_hits
            or raw_top_score < self._rewrite_low_confidence
            or short_query
            or self._is_mixed_language_query(q)
            or self._is_colloquial_query(q)
        )

    def _rewrite_wait_timeout(self, query: str) -> float:
        q = (query or '').strip()
        if self._requires_cjk_rewrite_fts(q):
            return max(self._rewrite_wait_grace_seconds, self._rewrite_cjk_wait_seconds)
        return max(self._rewrite_wait_grace_seconds, 0.0)

    def _rewrite_candidate_priority(self, raw_query: str, candidate: str) -> float:
        raw = (raw_query or '').strip()
        text = (candidate or '').strip()
        text_low = text.lower()
        if not text:
            return -1.0

        tokens = re.findall(r'[a-z]+(?:-[a-z]+)?|\d+(?:\.\d+)?', text_low)
        score = float(min(len(tokens), 4) * 10 + min(len(text_low), 40) * 0.2)

        procedure_intent = bool(re.search(r'手术|术后|术式|surgery|surgical|procedure|operative|operation', raw, re.I))
        therapy_intent = bool(re.search(r'治疗|疗法|用药|management|treatment|therapy', raw, re.I))
        diagnosis_intent = bool(re.search(r'诊断|筛查|检查|成像|oct|diagnos|screen|imaging', raw, re.I))

        if self._contains_cjk(raw) and re.search(r'[a-z]', text_low):
            score += 8.0
        if procedure_intent and re.search(r'\b(surgery|surgical|procedure|operative|operation|phaco|phacoemulsification)\b', text_low):
            score += 35.0
        if therapy_intent and re.search(r'\b(treatment|therapy|management|drug|medication)\b', text_low):
            score += 20.0
        if diagnosis_intent and re.search(r'\b(diagnosis|diagnostic|screening|imaging|oct)\b', text_low):
            score += 20.0
        return score

    def _fts_weight_profile(self, query: str) -> tuple[float, float]:
        q = (query or '').strip()
        if self._is_mixed_language_query(q):
            # Mixed-language queries benefit from keeping raw and rewrite lexical evidence close.
            return 0.17, 0.15
        if self._contains_cjk(q):
            return 0.12, 0.20
        return 0.24, 0.06

    def _run_raw_fts_recall(
        self,
        query: str,
        topn: int = 100,
        timer: Optional[SearchTimer] = None,
    ) -> Dict[str, Any]:
        with _timed_step(timer, 'raw_fts'):
            raw_fts_scores = self.fts_retrieve(query, topn=topn)
            keyword_scores = self.keyword_retrieve(query, topn=topn)
            merged_scores = self._merge_score_maps(raw_fts_scores, keyword_scores)
            used_fallback = False
            if not merged_scores:
                used_fallback = True
                with _timed_step(timer, 'lexical_fallback_retrieve'):
                    merged_scores = self.lexical_fallback_retrieve(query, topn=topn)
            return {
                'merged_scores': merged_scores,
                'raw_fts_scores': raw_fts_scores,
                'keyword_scores': keyword_scores,
                'used_fallback': used_fallback,
                'fts_calls': 1,
            }

    def _run_rewrite_fts_recall(
        self,
        queries: Sequence[str],
        topn: int = 100,
        timer: Optional[SearchTimer] = None,
    ) -> Dict[str, float]:
        merged: Dict[int, float] = {}
        with _timed_step(timer, 'rewrite_fts'):
            # Keep rewrite recall intentionally shallow to avoid serial FTS fan-out.
            for rewrite_query in queries[:2]:
                merged = self._merge_score_maps(merged, self.fts_retrieve(rewrite_query, topn=topn))
        return merged

    def _run_semantic_recall(
        self,
        query: str,
        topn: int = 100,
        timer: Optional[SearchTimer] = None,
    ) -> Dict[int, float]:
        with _timed_step(timer, 'embedding_recall'):
            return self.semantic_retrieve(query, topn=topn, timer=timer)

    def _run_author_recall(
        self,
        query: str,
        topn: int = 100,
        timer: Optional[SearchTimer] = None,
    ) -> Dict[int, float]:
        with _timed_step(timer, 'author_recall'):
            return self.author_retrieve(query, topn=topn)

    def _run_query_rewrite(
        self,
        query: str,
        timer: Optional[SearchTimer] = None,
    ) -> QueryRewrite:
        with _timed_step(timer, 'query_rewrite'):
            return self.query_rewrite(query)

    def _resolve_future(
        self,
        future: Optional[Future],
        default: Any,
        label: str,
    ) -> Any:
        if future is None:
            return default
        try:
            return future.result()
        except Exception as exc:
            self._logger.warning('%s failed, continue with degraded results: %s', label, exc)
            return default

    def fts_multi_retrieve(
        self,
        query: str,
        topn: int = 100,
        timer: Optional[SearchTimer] = None,
    ) -> Dict[int, float]:
        with _timed_step(timer, 'query_rewrite_ai'):
            rewrite = self.query_rewrite(query)
        with _timed_step(timer, 'build_recall_queries'):
            recall_queries: List[str] = []
            q = query.strip()
            if q:
                recall_queries.append(q)
                recall_queries.extend(self._local_query_expansions(q))

            for term in rewrite.keywords_zh:
                recall_queries.append(term)
                recall_queries.extend(self._local_query_expansions(term))
            for term in rewrite.keywords_en:
                recall_queries.append(term)
            for term in rewrite.must_terms:
                recall_queries.append(term)
                recall_queries.extend(self._local_query_expansions(term))

            # Combined query usually improves FTS ranking quality.
            combo_parts = [q] + rewrite.keywords_zh[:4] + rewrite.keywords_en[:4] + rewrite.must_terms[:4]
            combo = ' '.join([x for x in combo_parts if x and x.strip()]).strip()
            if combo and combo not in recall_queries:
                recall_queries.append(combo)

            recall_queries = list(dict.fromkeys([x for x in recall_queries if x.strip()]))[:20]
        merged: Dict[int, float] = {}
        for rq in recall_queries:
            with _timed_step(timer, 'fts_retrieve'):
                fts_scores = self.fts_retrieve(rq, topn=topn)
            merged = self._merge_score_maps(merged, fts_scores)
            with _timed_step(timer, 'keyword_retrieve'):
                keyword_scores = self.keyword_retrieve(rq, topn=topn)
            merged = self._merge_score_maps(merged, keyword_scores)
            with _timed_step(timer, 'author_retrieve'):
                author_scores = self.author_retrieve(rq, topn=topn)
            merged = self._merge_score_maps(merged, author_scores)
        if not merged:
            # FTS5 on CJK long phrases may miss results; fallback avoids blank page.
            with _timed_step(timer, 'lexical_fallback_retrieve'):
                merged = self.lexical_fallback_retrieve(query, topn=topn)
        return merged

    def semantic_retrieve(
        self,
        query: str,
        topn: int = 100,
        timer: Optional[SearchTimer] = None,
    ) -> Dict[int, float]:
        sem = self._semantic_scores(query.strip(), timer=timer)
        if sem is None:
            return {}

        with _timed_step(timer, 'semantic_topn_extract'):
            topn = min(max(topn, 1), len(sem))
            idxs = np.argpartition(sem, -topn)[-topn:]
            idxs = idxs[np.argsort(sem[idxs])[::-1]]
            out: Dict[int, float] = {}
            for idx in idxs:
                pid = self._embedding_paper_ids[int(idx)]
                norm = (float(sem[int(idx)]) + 1.0) / 2.0
                out[pid] = float(np.clip(norm, 0.0, 1.0))
        return out

    def _year_allowed(
        self,
        paper: Paper,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
    ) -> bool:
        if year_from is None and year_to is None:
            return True
        if paper.year is None:
            return False
        if year_from is not None and paper.year < year_from:
            return False
        if year_to is not None and paper.year > year_to:
            return False
        return True

    def _infer_year_range(
        self,
        query: str,
        year_from: Optional[int],
        year_to: Optional[int],
    ) -> tuple[Optional[int], Optional[int]]:
        if year_from is not None or year_to is not None:
            return year_from, year_to
        q = query or ''
        current_year = date.today().year
        if '今年' in q:
            return current_year, current_year
        if '去年' in q:
            return current_year - 1, current_year - 1
        m = re.search(r'(?:近|最近)\s*([一二两三四五六七八九十\d]+)\s*年', q)
        if not m:
            return year_from, year_to
        raw = m.group(1)
        cn = {
            '一': 1,
            '二': 2,
            '两': 2,
            '三': 3,
            '四': 4,
            '五': 5,
            '六': 6,
            '七': 7,
            '八': 8,
            '九': 9,
            '十': 10,
        }
        years = int(raw) if raw.isdigit() else cn.get(raw)
        if not years:
            return year_from, year_to
        # With year-level metadata, include the boundary calendar year.
        return current_year - years, current_year

    def _recency_score(self, paper: Paper) -> float:
        if not paper.year or self._min_year is None or self._max_year is None:
            return 0.0
        if self._max_year == self._min_year:
            return 1.0
        return float(np.clip((paper.year - self._min_year) / (self._max_year - self._min_year), 0.0, 1.0))

    def _qwen_rerank(self, query: str, candidates: List[Dict]) -> Dict[int, float]:
        if not self.qwen_rerank_enabled or not candidates:
            self._set_last_rerank_debug({
                'status': 'disabled' if not self.qwen_rerank_enabled else 'no_candidates',
                'query': query,
                'rerank_enabled': self.qwen_rerank_enabled,
                'candidate_count': len(candidates),
                'topn_requested': max(self._qwen_rerank_topn, 1),
                'scored_count': 0,
                'error': None,
            })
            return {}
        limited = candidates[:max(self._qwen_rerank_topn, 1)]
        paper_lines = []
        candidate_ids: List[int] = []
        for item in limited:
            p = self._paper_by_id.get(int(item['id']))
            if not p:
                continue
            candidate_ids.append(int(p.id))
            abstract = (p.abstract or '')[:450].replace('\n', ' ')
            keywords = ', '.join(p.keywords or [])
            paper_lines.append(
                f"ID: {p.id}\nTitle: {p.title}\nYear: {p.year or ''}\nKeywords: {keywords}\nAbstract: {abstract}"
            )
        if not paper_lines:
            self._set_last_rerank_debug({
                'status': 'no_candidates',
                'query': query,
                'rerank_enabled': self.qwen_rerank_enabled,
                'candidate_count': len(candidates),
                'topn_requested': max(self._qwen_rerank_topn, 1),
                'candidate_ids': [],
                'scored_count': 0,
                'error': None,
            })
            return {}
        system_prompt = (
            'You rerank academic paper search results. '
            'Return JSON only with key scores. '
            'scores must be an array of objects: {"id": integer, "score": number}. '
            'Score relevance from 0 to 1. Do not include explanation.'
        )
        user_prompt = (
            f'Query: {query}\n\nCandidates:\n\n'
            + '\n\n---\n\n'.join(paper_lines)
        )
        payload = {
            'model': self._qwen_model,
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            'response_format': {'type': 'json_object'},
        }
        req = request.Request(
            self._qwen_url,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self._qwen_api_key}',
            },
            method='POST',
        )
        try:
            with request.urlopen(req, timeout=18) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')
            data = json.loads(raw)
            content = (
                data.get('choices', [{}])[0]
                .get('message', {})
                .get('content', '')
            )
            obj = self._extract_json_obj(content) or {}
            scores = obj.get('scores') or []
            out: Dict[int, float] = {}
            for row in scores:
                try:
                    pid = int(row.get('id'))
                    score = float(row.get('score'))
                except Exception:
                    continue
                if pid in self._paper_by_id:
                    out[pid] = float(np.clip(score, 0.0, 1.0))
            self._set_last_rerank_debug({
                'status': 'success' if out else 'empty',
                'query': query,
                'rerank_enabled': self.qwen_rerank_enabled,
                'candidate_count': len(candidates),
                'topn_requested': max(self._qwen_rerank_topn, 1),
                'candidate_ids': candidate_ids,
                'scored_count': len(out),
                'scored_ids': list(out.keys()),
                'error': None,
            })
            return out
        except Exception as exc:
            self._logger.warning('Qwen rerank failed, keep local ranking: %s', exc)
            self._set_last_rerank_debug({
                'status': 'error',
                'query': query,
                'rerank_enabled': self.qwen_rerank_enabled,
                'candidate_count': len(candidates),
                'topn_requested': max(self._qwen_rerank_topn, 1),
                'candidate_ids': candidate_ids,
                'scored_count': 0,
                'error': str(exc),
            })
            return {}

    def _rank_cache_key(
        self,
        query: str,
        year_from: Optional[int],
        year_to: Optional[int],
    ) -> str:
        return json.dumps(
            {
                'query': query or '',
                'year_from': year_from,
                'year_to': year_to,
                'rewrite': self.rewrite_enabled,
                'rerank': self.qwen_rerank_enabled,
                'semantic_provider': self.embedding_provider,
                'semantic_model': self.embedding_model,
                'semantic_dimensions': self.embedding_dimensions,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _get_rank_cache(self, key: str) -> Optional[tuple[List[Dict], bool]]:
        if self._rank_cache_ttl <= 0:
            return None
        now = time.time()
        with self._rank_cache_lock:
            cached = self._rank_cache.get(key)
            if not cached:
                return None
            created_at, scored, semantic_enabled = cached
            if now - created_at > self._rank_cache_ttl:
                self._rank_cache.pop(key, None)
                return None
            return ([dict(item) for item in scored], semantic_enabled)

    def _set_rank_cache(self, key: str, scored: List[Dict], semantic_enabled: bool) -> None:
        if self._rank_cache_ttl <= 0:
            return
        with self._rank_cache_lock:
            if len(self._rank_cache) > 64:
                oldest_key = min(self._rank_cache.items(), key=lambda item: item[1][0])[0]
                self._rank_cache.pop(oldest_key, None)
            self._rank_cache[key] = (time.time(), [dict(item) for item in scored], semantic_enabled)

    def search_papers(
        self,
        query: str,
        topk: int = 20,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        sort: str = 'best_match',
        include_timing: bool = False,
        use_cache: bool = True,
    ) -> Dict:
        timer = SearchTimer() if include_timing else None
        num_rewrite_queries_generated = 0
        num_rewrite_queries_executed = 0
        num_total_fts_calls = 0
        author_recall_enabled = False
        rewrite_expansion_enabled = False
        rewrite_queries_generated: List[str] = []
        rewrite_queries_executed: List[str] = []
        rewrite_status = 'not_run'
        rewrite_source = 'none'
        rewrite_error: Optional[str] = None
        rewrite_result_payload: Dict[str, List[str]] = {
            'keywords_zh': [],
            'keywords_en': [],
            'must_terms': [],
        }
        cache_key = ''
        with _timed_step(timer, 'rank_cache_key'):
            cache_key = self._rank_cache_key(query, year_from, year_to)
        cached = None
        if use_cache:
            with _timed_step(timer, 'rank_cache_lookup'):
                cached = self._get_rank_cache(cache_key)
        if cached:
            with _timed_step(timer, 'sort_results'):
                scored, semantic_enabled = cached
                scored.sort(key=lambda x: (x['final_score'], x['year'] or 0), reverse=True)
                if sort == 'most_recent':
                    scored = sorted(
                        scored[:30],
                        key=lambda x: (x['year'] or 0, x['final_score']),
                        reverse=True,
                    )
            payload = {
                'results': scored[:topk],
                'semantic_enabled': semantic_enabled,
                'cache_hit': True,
            }
            self._set_last_author_debug({
                'status': 'cache_hit',
                'query': query,
                'retrieval_query': None,
                'author_query': None,
                'author_recall_enabled': False,
                'author_candidate_count': 0,
                'author_result_count': 0,
                'author_recall_ms': 0.0,
            })
            self._set_last_rerank_debug({
                'status': 'cache_hit',
                'query': query,
                'rerank_enabled': self.qwen_rerank_enabled,
                'candidate_count': 0,
                'topn_requested': max(self._qwen_rerank_topn, 1),
                'candidate_ids': [],
                'scored_count': 0,
                'scored_ids': [],
                'blended_count': 0,
                'rerank_ms': 0.0,
                'error': None,
            })
            self._set_last_rewrite_debug({
                'status': 'cache_hit',
                'cache_hit': True,
                'query': query,
                'retrieval_query': None,
                'rewrite_enabled': self.rewrite_enabled,
                'rewrite_source': 'cache',
                'rewrite_error': None,
                'rewrite_result': {
                    'keywords_zh': [],
                    'keywords_en': [],
                    'must_terms': [],
                },
                'rewrite_queries_generated': [],
                'rewrite_queries_executed': [],
                'rewrite_expansion_enabled': False,
                'num_rewrite_queries_generated': 0,
                'num_rewrite_queries_executed': 0,
                'num_total_fts_calls': 0,
                'query_rewrite_ms': 0.0,
                'raw_fts_hit_count': 0,
                'raw_fts_top_score': 0.0,
                'should_expand_with_rewrite': False,
                'force_cjk_rewrite_fts': False,
            })
            if timer:
                payload['timing'] = timer.to_dict(
                    cache_hit=True,
                    num_rewrite_queries_generated=0,
                    num_rewrite_queries_executed=0,
                    num_total_fts_calls=0,
                    author_recall_enabled=False,
                    rewrite_expansion_enabled=False,
                    rewrite_queries_generated=[],
                    rewrite_queries_executed=[],
                )
            return payload

        with _timed_step(timer, 'parse_query'):
            parsed = self.parse_query(query, year_from=year_from, year_to=year_to)
        retrieval_query = parsed.retrieval_query
        query_low = retrieval_query.lower().strip()
        author_recall_enabled = self._author_intent_enabled(parsed)
        author_query: Optional[str] = None

        raw_payload: Dict[str, Any] = {
            'merged_scores': {},
            'raw_fts_scores': {},
            'keyword_scores': {},
            'used_fallback': False,
            'fts_calls': 0,
        }
        sem_scores: Dict[int, float] = {}
        author_scores: Dict[int, float] = {}
        rewrite_scores: Dict[int, float] = {}

        with ThreadPoolExecutor(max_workers=max(self._search_parallel_workers, 2)) as executor:
            # Raw FTS, semantic recall, author recall and rewrite can run independently.
            raw_future = executor.submit(self._run_raw_fts_recall, retrieval_query, 100, timer)
            semantic_future = executor.submit(self._run_semantic_recall, retrieval_query, 100, timer)
            leading_author = re.match(r'^([\u4e00-\u9fff]{2,6})的', parsed.raw_query or '')
            author_query = (
                parsed.include_authors[0]
                if parsed.include_authors else
                (leading_author.group(1) if leading_author else parsed.raw_query)
            )
            author_future = (
                executor.submit(self._run_author_recall, author_query, 100, timer)
                if author_recall_enabled else None
            )
            rewrite_future = (
                executor.submit(self._run_query_rewrite, retrieval_query, timer)
                if self.rewrite_enabled else None
            )
            if rewrite_future:
                rewrite_status = 'scheduled'
                rewrite_source = 'qwen'
            else:
                rewrite_status = 'disabled'
                rewrite_source = 'local_expansion'

            raw_payload = self._resolve_future(raw_future, raw_payload, 'raw fts recall')
            sem_scores = self._resolve_future(semantic_future, {}, 'embedding recall')
            author_scores = self._resolve_future(author_future, {}, 'author recall')

            raw_scores = dict(raw_payload.get('merged_scores') or {})
            raw_fts_scores = dict(raw_payload.get('raw_fts_scores') or {})
            num_total_fts_calls = int(raw_payload.get('fts_calls') or 0)

            rewrite_result: Optional[QueryRewrite] = None
            should_expand_with_rewrite = bool(
                rewrite_future and self._should_expand_with_rewrite(retrieval_query, raw_fts_scores)
            )
            force_cjk_rewrite_fts = self._requires_cjk_rewrite_fts(retrieval_query)
            if rewrite_future:
                # Rewrite is an optional enhancement now: use it only if it is ready in time
                # and raw lexical recall suggests expansion is worth the extra FTS calls.
                if rewrite_future.done():
                    rewrite_status = 'completed'
                    rewrite_result = self._resolve_future(
                        rewrite_future,
                        QueryRewrite([], [], []),
                        'query rewrite',
                    )
                elif should_expand_with_rewrite:
                    try:
                        rewrite_result = rewrite_future.result(
                            timeout=self._rewrite_wait_timeout(retrieval_query)
                        )
                        rewrite_status = 'completed'
                    except TimeoutError:
                        rewrite_result = None
                        rewrite_status = 'timeout'
                        rewrite_error = 'rewrite wait timed out'
                    except Exception as exc:
                        self._logger.warning('query rewrite failed, skip expansion: %s', exc)
                        rewrite_result = None
                        rewrite_status = 'error'
                        rewrite_error = str(exc)
                else:
                    rewrite_status = 'skipped'
                if rewrite_result:
                    rewrite_result_payload = {
                        'keywords_zh': list(rewrite_result.keywords_zh),
                        'keywords_en': list(rewrite_result.keywords_en),
                        'must_terms': list(rewrite_result.must_terms),
                    }
                    rewrite_queries_generated = self._rewrite_candidates(retrieval_query, rewrite_result)
                    num_rewrite_queries_generated = len(rewrite_queries_generated)
                    if num_rewrite_queries_generated > 0:
                        rewrite_status = 'generated'
                    elif rewrite_status == 'completed':
                        rewrite_status = 'empty'
                if should_expand_with_rewrite and rewrite_queries_generated:
                    rewrite_limit = 1 if force_cjk_rewrite_fts else max(self._rewrite_max_queries, 0)
                    # Chinese queries always get one English rewrite FTS to preserve cross-lingual recall.
                    rewrite_queries_executed = rewrite_queries_generated[:rewrite_limit]
                    num_rewrite_queries_executed = len(rewrite_queries_executed)
                    rewrite_expansion_enabled = num_rewrite_queries_executed > 0
                    if rewrite_expansion_enabled:
                        rewrite_status = 'executed'
                        rewrite_scores = self._run_rewrite_fts_recall(
                            rewrite_queries_executed,
                            topn=100,
                            timer=timer,
                        )
                        num_total_fts_calls += num_rewrite_queries_executed
            else:
                raw_scores = dict(raw_payload.get('merged_scores') or {})
                raw_fts_scores = dict(raw_payload.get('raw_fts_scores') or {})
                if self._should_expand_with_rewrite(retrieval_query, raw_fts_scores):
                    rewrite_queries_generated = self._select_rewrite_queries(
                        retrieval_query,
                        self._local_query_expansions(retrieval_query),
                        limit=max(self._rewrite_max_queries, 0),
                    )
                    num_rewrite_queries_generated = len(rewrite_queries_generated)
                    rewrite_limit = 1 if self._requires_cjk_rewrite_fts(retrieval_query) else max(self._rewrite_max_queries, 0)
                    rewrite_queries_executed = rewrite_queries_generated[:rewrite_limit]
                    num_rewrite_queries_executed = len(rewrite_queries_executed)
                    rewrite_expansion_enabled = num_rewrite_queries_executed > 0
                    if rewrite_expansion_enabled:
                        rewrite_status = 'local_fallback'
                        rewrite_scores = self._run_rewrite_fts_recall(
                            rewrite_queries_executed,
                            topn=100,
                            timer=timer,
                        )
                        num_total_fts_calls += num_rewrite_queries_executed

        raw_scores = dict(raw_payload.get('merged_scores') or {})
        raw_fts_scores = dict(raw_payload.get('raw_fts_scores') or {})
        raw_fts_top_score = max((float(v) for v in raw_fts_scores.values()), default=0.0)
        author_recall_ms = 0.0
        query_rewrite_ms = 0.0
        if timer:
            author_recall_ms = float(timer.to_dict().get('by_step_ms', {}).get('author_recall', 0.0) or 0.0)
            query_rewrite_ms = float(timer.to_dict().get('by_step_ms', {}).get('query_rewrite', 0.0) or 0.0)
        self._set_last_author_debug({
            'status': 'executed' if author_recall_enabled else 'skipped',
            'query': query,
            'retrieval_query': retrieval_query,
            'author_query': author_query,
            'author_recall_enabled': author_recall_enabled,
            'author_candidate_count': len(author_scores),
            'author_result_count': len(author_scores),
            'author_recall_ms': round(author_recall_ms, 3),
        })
        self._set_last_rewrite_debug({
            'status': rewrite_status,
            'cache_hit': False,
            'query': query,
            'retrieval_query': retrieval_query,
            'rewrite_enabled': self.rewrite_enabled,
            'rewrite_source': rewrite_source,
            'rewrite_error': rewrite_error,
            'rewrite_result': rewrite_result_payload,
            'rewrite_queries_generated': list(rewrite_queries_generated),
            'rewrite_queries_executed': list(rewrite_queries_executed),
            'rewrite_expansion_enabled': rewrite_expansion_enabled,
            'num_rewrite_queries_generated': num_rewrite_queries_generated,
            'num_rewrite_queries_executed': num_rewrite_queries_executed,
            'num_total_fts_calls': num_total_fts_calls,
            'query_rewrite_ms': round(query_rewrite_ms, 3),
            'raw_fts_hit_count': len(raw_fts_scores),
            'raw_fts_top_score': round(raw_fts_top_score, 4),
            'should_expand_with_rewrite': bool(
                rewrite_future and self._should_expand_with_rewrite(retrieval_query, raw_fts_scores)
            ),
            'force_cjk_rewrite_fts': force_cjk_rewrite_fts,
        })

        with _timed_step(timer, 'merge_candidates'):
            candidate_features: Dict[int, Dict[str, float | bool | int]] = {}

            def ensure_feature(pid: int) -> Dict[str, float | bool | int]:
                return candidate_features.setdefault(pid, {
                    'raw_fts_hit': False,
                    'rewrite_fts_hit': False,
                    'embedding_hit': False,
                    'author_hit': False,
                    'raw_fts_score': 0.0,
                    'rewrite_fts_score': 0.0,
                    'embedding_score': 0.0,
                    'author_score': 0.0,
                    'hit_count': 0,
                })

            for pid, score in raw_scores.items():
                feature = ensure_feature(int(pid))
                feature['raw_fts_hit'] = True
                feature['raw_fts_score'] = max(float(feature['raw_fts_score']), float(score))
            for pid, score in rewrite_scores.items():
                feature = ensure_feature(int(pid))
                feature['rewrite_fts_hit'] = True
                feature['rewrite_fts_score'] = max(float(feature['rewrite_fts_score']), float(score))
            for pid, score in sem_scores.items():
                feature = ensure_feature(int(pid))
                feature['embedding_hit'] = True
                feature['embedding_score'] = max(float(feature['embedding_score']), float(score))
            for pid, score in author_scores.items():
                feature = ensure_feature(int(pid))
                feature['author_hit'] = True
                feature['author_score'] = max(float(feature['author_score']), float(score))
            for feature in candidate_features.values():
                feature['hit_count'] = int(sum(
                    1 for field in ('raw_fts_hit', 'rewrite_fts_hit', 'embedding_hit', 'author_hit')
                    if feature[field]
                ))

            candidate_ids = set(candidate_features.keys())
        if not candidate_ids:
            payload = {
                'results': [],
                'semantic_enabled': self.semantic_enabled,
                'cache_hit': False,
            }
            if timer:
                payload['timing'] = timer.to_dict(
                    cache_hit=False,
                    num_rewrite_queries_generated=num_rewrite_queries_generated,
                    num_rewrite_queries_executed=num_rewrite_queries_executed,
                    num_total_fts_calls=num_total_fts_calls,
                    author_recall_enabled=author_recall_enabled,
                    rewrite_expansion_enabled=rewrite_expansion_enabled,
                    rewrite_queries_generated=rewrite_queries_generated,
                    rewrite_queries_executed=rewrite_queries_executed,
                )
            return payload

        scored = []
        with _timed_step(timer, 'local_rank'):
            for pid in candidate_ids:
                p = self._paper_by_id.get(pid)
                if not p:
                    continue
                if not self._paper_has_any_author(p, parsed.include_authors):
                    continue
                if self._paper_has_excluded_author(p, parsed.exclude_authors):
                    continue
                if not self._year_allowed(p, year_from=parsed.year_from, year_to=parsed.year_to):
                    continue

                title_low = (p.title or '').lower()
                keywords_low = [k.lower() for k in (p.keywords or [])]
                title_hit = 1.0 if query_low and query_low in title_low else 0.0
                keyword_hit = 1.0 if query_low and any(query_low in k for k in keywords_low) else 0.0

                feature = candidate_features.get(pid) or {}
                raw_fts_score = float(feature.get('raw_fts_score', 0.0))
                rewrite_fts_score = float(feature.get('rewrite_fts_score', 0.0))
                semantic_score = float(feature.get('embedding_score', 0.0))
                author_score = float(feature.get('author_score', 0.0))
                hit_count = int(feature.get('hit_count', 0))
                hit_bonus = min(hit_count, 4) / 4.0
                fts_score = max(raw_fts_score, rewrite_fts_score)
                raw_fts_weight, rewrite_fts_weight = self._fts_weight_profile(retrieval_query)
                recency_score = self._recency_score(p)
                final_score = (
                    0.55 * semantic_score
                    + raw_fts_weight * raw_fts_score
                    + rewrite_fts_weight * rewrite_fts_score
                    + 0.60 * author_score
                    + 0.10 * title_hit
                    + 0.05 * keyword_hit
                    + 0.03 * recency_score
                    + 0.04 * hit_bonus
                )

                snippet = p.abstract[:180] + ('...' if len(p.abstract) > 180 else '')
                cit = (p.citation or '').strip()
                cit_snip = (cit[:140] + '…') if len(cit) > 140 else cit
                scored.append({
                    'id': p.id,
                    'title': p.title,
                    'authors': ', '.join(p.authors),
                    'year': p.year,
                    'keywords': p.keywords,
                    'abstract_snippet': snippet,
                    'citation': cit or None,
                    'citation_snippet': cit_snip or None,
                    'final_score': round(float(final_score), 4),
                    'fts_score': round(float(fts_score), 4),
                    'semantic_score': round(float(semantic_score), 4),
                    'author_score': round(float(author_score), 4),
                    'recency_score': round(float(recency_score), 4),
                    'qwen_score': None,
                    'match_debug': {
                        'sources': [
                            source for source, enabled in [
                                ('raw_fts', bool(feature.get('raw_fts_hit'))),
                                ('rewrite_fts', bool(feature.get('rewrite_fts_hit'))),
                                ('embedding', bool(feature.get('embedding_hit'))),
                                ('author', bool(feature.get('author_hit'))),
                            ] if enabled
                        ],
                        'raw_fts_hit': bool(feature.get('raw_fts_hit')),
                        'rewrite_fts_hit': bool(feature.get('rewrite_fts_hit')),
                        'embedding_hit': bool(feature.get('embedding_hit')),
                        'author_hit': bool(feature.get('author_hit')),
                        'raw_fts_score': round(float(raw_fts_score), 4),
                        'rewrite_fts_score': round(float(rewrite_fts_score), 4),
                        'embedding_score': round(float(semantic_score), 4),
                        'author_score': round(float(author_score), 4),
                        'hit_count': hit_count,
                        'raw_fts_weight': round(float(raw_fts_weight), 4),
                        'rewrite_fts_weight': round(float(rewrite_fts_weight), 4),
                    },
                    # Backward compatibility for current frontend rendering.
                    'score': round(float(final_score), 4),
                })
        with _timed_step(timer, 'initial_sort'):
            scored.sort(key=lambda x: x['final_score'], reverse=True)
        with _timed_step(timer, 'rerank'):
            qwen_scores = self._qwen_rerank(retrieval_query, scored)
        blended_count = 0
        if qwen_scores:
            with _timed_step(timer, 'blend_qwen_scores'):
                for item in scored:
                    qwen_score = qwen_scores.get(int(item['id']))
                    if qwen_score is None:
                        continue
                    blended_count += 1
                    item['qwen_score'] = round(float(qwen_score), 4)
                    blended = 0.75 * float(item['final_score']) + 0.25 * qwen_score
                    item['final_score'] = round(float(blended), 4)
                    item['score'] = item['final_score']
        with _timed_step(timer, 'sort_results'):
            scored.sort(key=lambda x: (x['final_score'], x['year'] or 0), reverse=True)
            if sort == 'most_recent':
                scored = sorted(
                    scored[:30],
                    key=lambda x: (x['year'] or 0, x['final_score']),
                    reverse=True,
                )
        if use_cache:
            with _timed_step(timer, 'rank_cache_store'):
                self._set_rank_cache(cache_key, scored, self.semantic_enabled)
        rerank_ms = 0.0
        if timer:
            rerank_ms = float(timer.to_dict().get('by_step_ms', {}).get('rerank', 0.0) or 0.0)
        rerank_debug = self.get_last_rerank_debug()
        rerank_debug['blended_count'] = blended_count
        rerank_debug['rerank_ms'] = round(rerank_ms, 3)
        self._set_last_rerank_debug(rerank_debug)
        payload = {
            'results': scored[:topk],
            'semantic_enabled': self.semantic_enabled,
            'cache_hit': False,
        }
        if timer:
            payload['timing'] = timer.to_dict(
                cache_hit=False,
                num_rewrite_queries_generated=num_rewrite_queries_generated,
                num_rewrite_queries_executed=num_rewrite_queries_executed,
                num_total_fts_calls=num_total_fts_calls,
                author_recall_enabled=author_recall_enabled,
                rewrite_expansion_enabled=rewrite_expansion_enabled,
                rewrite_queries_generated=rewrite_queries_generated,
                rewrite_queries_executed=rewrite_queries_executed,
            )
        return payload

    def search(self, query: str, limit: int = 20) -> List[Dict]:
        return self.search_papers(query, topk=limit)['results']

    def get_detail(self, paper_id: int) -> Optional[Paper]:
        for p in self.papers:
            if p.id == paper_id:
                return p
        return None
