from __future__ import annotations

import csv
import json
from datetime import date, datetime, time, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    case,
    create_engine,
    distinct,
    func,
    insert,
    or_,
    select,
)
from sqlalchemy.engine import Engine


class AnalyticsStore:
    def __init__(self, db_backend: str, db_path: str) -> None:
        self.db_backend = db_backend
        self.db_path = db_path
        self.enabled = True
        self._engine = self._build_engine(db_backend=db_backend, db_path=db_path)
        self._metadata = MetaData()
        self.events = Table(
            'analytics_events',
            self._metadata,
            Column('id', Integer, primary_key=True, autoincrement=True),
            Column('event_type', String(32), nullable=False),
            Column('occurred_at', DateTime, nullable=False, server_default=func.current_timestamp()),
            Column('visitor_id', String(128), nullable=False),
            Column('ip_hash', String(128)),
            Column('user_agent', Text),
            Column('referer', Text),
            Column('path', String(255)),
            Column('query_raw', Text),
            Column('query_normalized', String(512)),
            Column('year_from', Integer),
            Column('year_to', Integer),
            Column('sort', String(32)),
            Column('result_count', Integer),
            Column('latency_ms', Float),
            Column('status_code', Integer),
            Index('idx_analytics_events_occurred_at', 'occurred_at'),
            Index('idx_analytics_events_type_time', 'event_type', 'occurred_at'),
            Index('idx_analytics_events_visitor', 'visitor_id'),
            Index('idx_analytics_events_query', 'query_normalized'),
        )
        self.backend_label = self._engine.dialect.name
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self._metadata.create_all(self._engine, checkfirst=True)

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
        filters = self._event_filters(date_from=date_from, date_to=date_to)
        stmt = select(
            func.sum(case((self.events.c.event_type == 'page_view', 1), else_=0)).label('page_views'),
            func.count(
                distinct(
                    case((self.events.c.event_type == 'page_view', self.events.c.visitor_id), else_=None)
                )
            ).label('unique_visitors'),
            func.sum(case((self.events.c.event_type == 'search', 1), else_=0)).label('searches'),
            func.count(
                distinct(
                    case((self.events.c.event_type == 'search', self.events.c.visitor_id), else_=None)
                )
            ).label('search_visitors'),
        ).select_from(self.events)
        if filters:
            stmt = stmt.where(and_(*filters))
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().one()
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
        stmt = (
            select(
                self.events.c.query_normalized.label('keyword'),
                func.count().label('searches'),
                func.count(distinct(self.events.c.visitor_id)).label('visitors'),
                func.sum(
                    case((func.coalesce(self.events.c.result_count, 0) == 0, 1), else_=0)
                ).label('zero_result_searches'),
                func.max(self.events.c.occurred_at).label('last_searched_at'),
            )
            .where(and_(*self._search_filters(
                date_from=date_from,
                date_to=date_to,
                keyword=None,
                require_nonempty_query=True,
            )))
            .group_by(self.events.c.query_normalized)
            .order_by(func.count().desc(), func.max(self.events.c.occurred_at).desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._normalize_record(row) for row in rows]

    def recent_searches(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
        page: int,
        page_size: int,
    ) -> Dict[str, Any]:
        safe_page = max(int(page or 1), 1)
        safe_page_size = min(max(int(page_size or 20), 1), 200)
        offset = (safe_page - 1) * safe_page_size
        filters = self._search_filters(
            date_from=date_from,
            date_to=date_to,
            keyword=keyword,
            require_nonempty_query=False,
        )
        total_stmt = select(func.count()).select_from(self.events).where(and_(*filters))
        rows_stmt = (
            select(
                self.events.c.id,
                self.events.c.occurred_at,
                self.events.c.visitor_id,
                self.events.c.query_raw,
                self.events.c.query_normalized,
                self.events.c.year_from,
                self.events.c.year_to,
                self.events.c.sort,
                self.events.c.result_count,
                self.events.c.latency_ms,
                self.events.c.path,
            )
            .where(and_(*filters))
            .order_by(self.events.c.occurred_at.desc(), self.events.c.id.desc())
            .limit(safe_page_size)
            .offset(offset)
        )
        with self._engine.begin() as conn:
            total = int(conn.execute(total_stmt).scalar() or 0)
            rows = conn.execute(rows_stmt).mappings().all()
        return {
            'keyword': keyword or None,
            'page': safe_page,
            'page_size': safe_page_size,
            'total': total,
            'items': [self._normalize_record(row) for row in rows],
        }

    def export_rows(
        self,
        *,
        dataset: str,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
    ) -> List[Dict[str, Any]]:
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
            writer = csv.writer(buffer)
            writer.writerow(self._default_headers(dataset))
        return ('\ufeff' + buffer.getvalue()).encode('utf-8')

    def _search_export_rows(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
    ) -> List[Dict[str, Any]]:
        filters = self._search_filters(
            date_from=date_from,
            date_to=date_to,
            keyword=keyword,
            require_nonempty_query=False,
        )
        stmt = (
            select(
                self.events.c.occurred_at,
                self.events.c.visitor_id,
                self.events.c.query_raw,
                self.events.c.query_normalized,
                self.events.c.year_from,
                self.events.c.year_to,
                self.events.c.sort,
                self.events.c.result_count,
                self.events.c.latency_ms,
                self.events.c.path,
                self.events.c.ip_hash,
                self.events.c.user_agent,
            )
            .where(and_(*filters))
            .order_by(self.events.c.occurred_at.desc(), self.events.c.id.desc())
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._normalize_record(row) for row in rows]

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
        event_date = func.date(self.events.c.occurred_at)
        filters = self._event_filters(date_from=date_from, date_to=date_to)
        stmt = (
            select(
                event_date.label('date'),
                func.sum(case((self.events.c.event_type == 'page_view', 1), else_=0)).label('page_views'),
                func.count(
                    distinct(
                        case((self.events.c.event_type == 'page_view', self.events.c.visitor_id), else_=None)
                    )
                ).label('unique_visitors'),
                func.sum(case((self.events.c.event_type == 'search', 1), else_=0)).label('searches'),
                func.count(
                    distinct(
                        case((self.events.c.event_type == 'search', self.events.c.visitor_id), else_=None)
                    )
                ).label('search_visitors'),
                func.sum(
                    case((
                        and_(
                            self.events.c.event_type == 'search',
                            func.coalesce(self.events.c.result_count, 0) == 0,
                        ),
                        1,
                    ), else_=0)
                ).label('zero_result_searches'),
            )
            .select_from(self.events)
            .group_by(event_date)
            .order_by(event_date.desc())
        )
        if filters:
            stmt = stmt.where(and_(*filters))
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._normalize_record(row) for row in rows]

    def _insert_event(self, **payload: Any) -> None:
        stmt = insert(self.events).values(**payload)
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def _event_filters(self, *, date_from: Optional[date], date_to: Optional[date]) -> List[Any]:
        filters: List[Any] = []
        if date_from:
            filters.append(self.events.c.occurred_at >= self._start_dt(date_from))
        if date_to:
            filters.append(self.events.c.occurred_at < self._end_exclusive_dt(date_to))
        return filters

    def _search_filters(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
        require_nonempty_query: bool,
    ) -> List[Any]:
        filters = [self.events.c.event_type == 'search']
        filters.extend(self._event_filters(date_from=date_from, date_to=date_to))
        if require_nonempty_query:
            filters.append(func.coalesce(self.events.c.query_normalized, '') != '')
        normalized_keyword = self._normalize_query(keyword or '')
        if normalized_keyword:
            pattern = f'%{normalized_keyword}%'
            filters.append(
                or_(
                    self.events.c.query_normalized.like(pattern),
                    func.lower(func.coalesce(self.events.c.query_raw, '')).like(pattern),
                )
            )
        return filters

    def _build_engine(self, *, db_backend: str, db_path: str) -> Engine:
        if db_backend == 'sqlite' and '://' not in db_path:
            path = Path(db_path).expanduser().resolve()
            return create_engine(
                f'sqlite:///{path.as_posix()}',
                future=True,
                pool_pre_ping=True,
                connect_args={'check_same_thread': False},
            )
        return create_engine(db_path, future=True, pool_pre_ping=True)

    def _default_headers(self, dataset: str) -> List[str]:
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
        return default_headers.get(dataset, [])

    def _start_dt(self, value: date) -> datetime:
        return datetime.combine(value, time.min)

    def _end_exclusive_dt(self, value: date) -> datetime:
        return datetime.combine(value + timedelta(days=1), time.min)

    def _normalize_query(self, text: str) -> str:
        return ' '.join(str(text or '').strip().lower().split())

    def _normalize_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        for key, value in dict(record).items():
            if isinstance(value, datetime):
                normalized[key] = value.isoformat(sep=' ', timespec='seconds')
            elif isinstance(value, date):
                normalized[key] = value.isoformat()
            else:
                normalized[key] = value
        return normalized


def json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
