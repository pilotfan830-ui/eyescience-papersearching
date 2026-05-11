from __future__ import annotations

import csv
import json
import sqlite3
import threading
from datetime import date, datetime, timedelta
from io import StringIO
from typing import Any, Dict, List, Optional, Sequence, Tuple


class AnalyticsStore:
    def __init__(self, db_backend: str, db_path: str) -> None:
        self.db_backend = db_backend
        self.db_path = db_path
        self.enabled = db_backend == 'sqlite' and '://' not in db_path
        self._schema_lock = threading.Lock()
        if self.enabled:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        if not self.enabled:
            return
        with self._schema_lock:
            conn = self._connect()
            try:
                conn.executescript(
                    '''
                    CREATE TABLE IF NOT EXISTS analytics_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        visitor_id TEXT NOT NULL,
                        ip_hash TEXT,
                        user_agent TEXT,
                        referer TEXT,
                        path TEXT,
                        query_raw TEXT,
                        query_normalized TEXT,
                        year_from INTEGER,
                        year_to INTEGER,
                        sort TEXT,
                        result_count INTEGER,
                        latency_ms REAL,
                        status_code INTEGER
                    );

                    CREATE INDEX IF NOT EXISTS idx_analytics_events_occurred_at
                    ON analytics_events(occurred_at);

                    CREATE INDEX IF NOT EXISTS idx_analytics_events_type_time
                    ON analytics_events(event_type, occurred_at);

                    CREATE INDEX IF NOT EXISTS idx_analytics_events_visitor
                    ON analytics_events(visitor_id);

                    CREATE INDEX IF NOT EXISTS idx_analytics_events_query
                    ON analytics_events(query_normalized);
                    '''
                )
                conn.commit()
            finally:
                conn.close()

    def record_page_view(
        self,
        *,
        visitor_id: str,
        ip_hash: Optional[str],
        user_agent: Optional[str],
        referer: Optional[str],
        path: str,
        status_code: int = 200,
    ) -> None:
        self._insert_event(
            event_type='page_view',
            visitor_id=visitor_id,
            ip_hash=ip_hash,
            user_agent=user_agent,
            referer=referer,
            path=path,
            query_raw=None,
            query_normalized=None,
            year_from=None,
            year_to=None,
            sort=None,
            result_count=None,
            latency_ms=None,
            status_code=status_code,
        )

    def record_search(
        self,
        *,
        visitor_id: str,
        ip_hash: Optional[str],
        user_agent: Optional[str],
        referer: Optional[str],
        path: str,
        query_raw: str,
        year_from: Optional[int],
        year_to: Optional[int],
        sort: str,
        result_count: int,
        latency_ms: Optional[float],
        status_code: int = 200,
    ) -> None:
        self._insert_event(
            event_type='search',
            visitor_id=visitor_id,
            ip_hash=ip_hash,
            user_agent=user_agent,
            referer=referer,
            path=path,
            query_raw=query_raw,
            query_normalized=self._normalize_query(query_raw),
            year_from=year_from,
            year_to=year_to,
            sort=sort,
            result_count=result_count,
            latency_ms=latency_ms,
            status_code=status_code,
        )

    def dashboard(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
        page: int,
        page_size: int,
        keyword_limit: int,
    ) -> Dict[str, Any]:
        self._ensure_available()
        safe_page = max(int(page or 1), 1)
        safe_page_size = min(max(int(page_size or 20), 1), 200)
        safe_keyword_limit = min(max(int(keyword_limit or 20), 1), 100)
        return {
            'overview': self.overview(date_from=date_from, date_to=date_to),
            'top_keywords': self.top_keywords(
                date_from=date_from,
                date_to=date_to,
                limit=safe_keyword_limit,
            ),
            'recent_searches': self.recent_searches(
                date_from=date_from,
                date_to=date_to,
                keyword=keyword,
                page=safe_page,
                page_size=safe_page_size,
            ),
        }

    def overview(self, *, date_from: Optional[date], date_to: Optional[date]) -> Dict[str, Any]:
        self._ensure_available()
        where_sql, params = self._event_filter_sql(date_from=date_from, date_to=date_to)
        conn = self._connect()
        try:
            row = conn.execute(
                f'''
                SELECT
                    SUM(CASE WHEN event_type = 'page_view' THEN 1 ELSE 0 END) AS page_views,
                    COUNT(DISTINCT CASE WHEN event_type = 'page_view' THEN visitor_id END) AS unique_visitors,
                    SUM(CASE WHEN event_type = 'search' THEN 1 ELSE 0 END) AS searches,
                    COUNT(DISTINCT CASE WHEN event_type = 'search' THEN visitor_id END) AS search_visitors
                FROM analytics_events
                {where_sql}
                ''',
                params,
            ).fetchone()
        finally:
            conn.close()
        return {
            'date_from': date_from.isoformat() if date_from else None,
            'date_to': date_to.isoformat() if date_to else None,
            'page_views': int(row['page_views'] or 0),
            'unique_visitors': int(row['unique_visitors'] or 0),
            'searches': int(row['searches'] or 0),
            'search_visitors': int(row['search_visitors'] or 0),
        }

    def top_keywords(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        limit: int,
    ) -> List[Dict[str, Any]]:
        self._ensure_available()
        where_sql, params = self._search_filter_sql(
            date_from=date_from,
            date_to=date_to,
            keyword=None,
            require_nonempty_query=True,
        )
        conn = self._connect()
        try:
            rows = conn.execute(
                f'''
                SELECT
                    query_normalized AS keyword,
                    COUNT(*) AS searches,
                    COUNT(DISTINCT visitor_id) AS visitors,
                    SUM(CASE WHEN COALESCE(result_count, 0) = 0 THEN 1 ELSE 0 END) AS zero_result_searches,
                    MAX(occurred_at) AS last_searched_at
                FROM analytics_events
                {where_sql}
                GROUP BY query_normalized
                ORDER BY searches DESC, last_searched_at DESC
                LIMIT ?
                ''',
                [*params, limit],
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def recent_searches(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
        page: int,
        page_size: int,
    ) -> Dict[str, Any]:
        self._ensure_available()
        safe_page = max(int(page or 1), 1)
        safe_page_size = min(max(int(page_size or 20), 1), 200)
        offset = (safe_page - 1) * safe_page_size
        where_sql, params = self._search_filter_sql(
            date_from=date_from,
            date_to=date_to,
            keyword=keyword,
            require_nonempty_query=False,
        )
        conn = self._connect()
        try:
            total_row = conn.execute(
                f'''
                SELECT COUNT(*) AS total
                FROM analytics_events
                {where_sql}
                ''',
                params,
            ).fetchone()
            rows = conn.execute(
                f'''
                SELECT
                    id,
                    occurred_at,
                    visitor_id,
                    query_raw,
                    query_normalized,
                    year_from,
                    year_to,
                    sort,
                    result_count,
                    latency_ms,
                    path
                FROM analytics_events
                {where_sql}
                ORDER BY occurred_at DESC, id DESC
                LIMIT ? OFFSET ?
                ''',
                [*params, safe_page_size, offset],
            ).fetchall()
        finally:
            conn.close()
        return {
            'keyword': keyword or None,
            'page': safe_page,
            'page_size': safe_page_size,
            'total': int(total_row['total'] or 0),
            'items': [dict(row) for row in rows],
        }

    def export_rows(
        self,
        *,
        dataset: str,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
    ) -> List[Dict[str, Any]]:
        self._ensure_available()
        if dataset == 'searches':
            return self._search_export_rows(date_from=date_from, date_to=date_to, keyword=keyword)
        if dataset == 'keywords':
            return self._keyword_export_rows(date_from=date_from, date_to=date_to)
        if dataset == 'daily':
            return self._daily_export_rows(date_from=date_from, date_to=date_to)
        raise ValueError(f'unsupported dataset: {dataset}')

    def export_json_payload(
        self,
        *,
        dataset: str,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
    ) -> Dict[str, Any]:
        return {
            'dataset': dataset,
            'generated_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            'filters': {
                'date_from': date_from.isoformat() if date_from else None,
                'date_to': date_to.isoformat() if date_to else None,
                'keyword': keyword or None,
            },
            'items': self.export_rows(
                dataset=dataset,
                date_from=date_from,
                date_to=date_to,
                keyword=keyword,
            ),
        }

    def export_csv_bytes(
        self,
        *,
        dataset: str,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
    ) -> bytes:
        rows = self.export_rows(
            dataset=dataset,
            date_from=date_from,
            date_to=date_to,
            keyword=keyword,
        )
        buffer = StringIO()
        if rows:
            writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        else:
            default_headers = {
                'searches': [
                    'occurred_at', 'visitor_id', 'query_raw', 'query_normalized',
                    'year_from', 'year_to', 'sort', 'result_count', 'latency_ms',
                    'path', 'ip_hash', 'user_agent',
                ],
                'keywords': [
                    'keyword', 'searches', 'visitors', 'zero_result_searches', 'last_searched_at',
                ],
                'daily': [
                    'date', 'page_views', 'unique_visitors', 'searches',
                    'search_visitors', 'zero_result_searches',
                ],
            }
            writer = csv.writer(buffer)
            writer.writerow(default_headers.get(dataset, []))
        return ('\ufeff' + buffer.getvalue()).encode('utf-8')

    def _search_export_rows(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
    ) -> List[Dict[str, Any]]:
        where_sql, params = self._search_filter_sql(
            date_from=date_from,
            date_to=date_to,
            keyword=keyword,
            require_nonempty_query=False,
        )
        conn = self._connect()
        try:
            rows = conn.execute(
                f'''
                SELECT
                    occurred_at,
                    visitor_id,
                    query_raw,
                    query_normalized,
                    year_from,
                    year_to,
                    sort,
                    result_count,
                    latency_ms,
                    path,
                    ip_hash,
                    user_agent
                FROM analytics_events
                {where_sql}
                ORDER BY occurred_at DESC, id DESC
                ''',
                params,
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def _keyword_export_rows(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
    ) -> List[Dict[str, Any]]:
        return self.top_keywords(date_from=date_from, date_to=date_to, limit=10000)

    def _daily_export_rows(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
    ) -> List[Dict[str, Any]]:
        where_sql, params = self._event_filter_sql(date_from=date_from, date_to=date_to)
        conn = self._connect()
        try:
            rows = conn.execute(
                f'''
                SELECT
                    substr(occurred_at, 1, 10) AS date,
                    SUM(CASE WHEN event_type = 'page_view' THEN 1 ELSE 0 END) AS page_views,
                    COUNT(DISTINCT CASE WHEN event_type = 'page_view' THEN visitor_id END) AS unique_visitors,
                    SUM(CASE WHEN event_type = 'search' THEN 1 ELSE 0 END) AS searches,
                    COUNT(DISTINCT CASE WHEN event_type = 'search' THEN visitor_id END) AS search_visitors,
                    SUM(CASE WHEN event_type = 'search' AND COALESCE(result_count, 0) = 0 THEN 1 ELSE 0 END) AS zero_result_searches
                FROM analytics_events
                {where_sql}
                GROUP BY substr(occurred_at, 1, 10)
                ORDER BY date DESC
                ''',
                params,
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def _insert_event(self, **payload: Any) -> None:
        if not self.enabled:
            return
        self.ensure_schema()
        conn = self._connect()
        try:
            conn.execute(
                '''
                INSERT INTO analytics_events(
                    event_type, visitor_id, ip_hash, user_agent, referer, path,
                    query_raw, query_normalized, year_from, year_to, sort,
                    result_count, latency_ms, status_code
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    payload.get('event_type'),
                    payload.get('visitor_id'),
                    payload.get('ip_hash'),
                    payload.get('user_agent'),
                    payload.get('referer'),
                    payload.get('path'),
                    payload.get('query_raw'),
                    payload.get('query_normalized'),
                    payload.get('year_from'),
                    payload.get('year_to'),
                    payload.get('sort'),
                    payload.get('result_count'),
                    payload.get('latency_ms'),
                    payload.get('status_code'),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _event_filter_sql(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
    ) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if date_from:
            clauses.append('occurred_at >= ?')
            params.append(f'{date_from.isoformat()} 00:00:00')
        if date_to:
            end_exclusive = date_to + timedelta(days=1)
            clauses.append('occurred_at < ?')
            params.append(f'{end_exclusive.isoformat()} 00:00:00')
        if not clauses:
            return '', params
        return 'WHERE ' + ' AND '.join(clauses), params

    def _search_filter_sql(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
        require_nonempty_query: bool,
    ) -> Tuple[str, List[Any]]:
        clauses = ["event_type = 'search'"]
        params: List[Any] = []
        if date_from:
            clauses.append('occurred_at >= ?')
            params.append(f'{date_from.isoformat()} 00:00:00')
        if date_to:
            end_exclusive = date_to + timedelta(days=1)
            clauses.append('occurred_at < ?')
            params.append(f'{end_exclusive.isoformat()} 00:00:00')
        if require_nonempty_query:
            clauses.append("COALESCE(query_normalized, '') <> ''")
        normalized_keyword = self._normalize_query(keyword or '')
        if normalized_keyword:
            clauses.append('(query_normalized LIKE ? OR query_raw LIKE ?)')
            pattern = f'%{normalized_keyword}%'
            params.extend([pattern, pattern])
        return 'WHERE ' + ' AND '.join(clauses), params

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_available(self) -> None:
        if not self.enabled:
            raise RuntimeError(
                'analytics is available only when the paper database uses a local sqlite file'
            )

    def _normalize_query(self, text: str) -> str:
        return ' '.join(str(text or '').strip().lower().split())


def json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
