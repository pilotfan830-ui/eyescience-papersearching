from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime, time, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

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
    inspect,
    insert,
    or_,
    select,
    text,
    UniqueConstraint,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError


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
            Column('paper_id', Integer),
            Column('paper_title', Text),
            Column('related_search_event_id', Integer),
            Index('idx_analytics_events_occurred_at', 'occurred_at'),
            Index('idx_analytics_events_type_time', 'event_type', 'occurred_at'),
            Index('idx_analytics_events_visitor', 'visitor_id'),
            Index('idx_analytics_events_query', 'query_normalized'),
            Index('idx_analytics_events_related_search', 'related_search_event_id'),
        )
        self.unique_clicks = Table(
            'search_paper_unique_clicks',
            self._metadata,
            Column('id', Integer, primary_key=True, autoincrement=True),
            Column('search_event_id', Integer, nullable=False),
            Column('visitor_id', String(128), nullable=False),
            Column('query_raw', Text),
            Column('query_normalized', String(512)),
            Column('paper_id', Integer, nullable=False),
            Column('paper_title', Text),
            Column('clicked_at', DateTime, nullable=False, server_default=func.current_timestamp()),
            UniqueConstraint('search_event_id', 'paper_id', name='uq_unique_click_search_paper'),
            Index('idx_unique_clicks_query', 'query_normalized'),
            Index('idx_unique_clicks_clicked_at', 'clicked_at'),
            Index('idx_unique_clicks_paper', 'paper_id'),
        )
        self.backend_label = self._engine.dialect.name
        self.ensure_schema()

    _DISPLAY_TZ = ZoneInfo('Asia/Shanghai')
    _DATETIME_FIELD_NAMES = {
        'occurred_at',
        'clicked_at',
        'searched_at',
        'first_clicked_at',
        'last_clicked_at',
        'last_searched_at',
        'updated_at',
    }

    def ensure_schema(self) -> None:
        self._metadata.create_all(self._engine, checkfirst=True)
        self._ensure_missing_columns()

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
    ) -> int:
        return self._insert_event(
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

    def record_paper_click(
        self,
        *,
        visitor_id: str,
        ip_hash: Optional[str],
        user_agent: Optional[str],
        referer: Optional[str],
        path: str,
        paper_id: int,
        paper_title: str,
        related_search_event_id: Optional[int],
        query_raw: Optional[str],
        year_from: Optional[int],
        year_to: Optional[int],
        sort: Optional[str],
        status_code: int = 200,
    ) -> int:
        event_id = self._insert_event(
            event_type='paper_click',
            visitor_id=visitor_id,
            ip_hash=ip_hash,
            user_agent=user_agent,
            referer=referer,
            path=path,
            query_raw=query_raw,
            query_normalized=self._normalize_query(query_raw or ''),
            year_from=year_from,
            year_to=year_to,
            sort=sort,
            result_count=None,
            latency_ms=None,
            status_code=status_code,
            paper_id=paper_id,
            paper_title=paper_title,
            related_search_event_id=related_search_event_id,
        )
        self._insert_unique_click(
            search_event_id=related_search_event_id,
            visitor_id=visitor_id,
            query_raw=query_raw,
            paper_id=paper_id,
            paper_title=paper_title,
        )
        return event_id

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
            'recent_clicks': self.recent_clicks(
                date_from=date_from,
                date_to=date_to,
                keyword=keyword,
                limit=12,
            ),
            'top_paper_clicks': self.top_paper_clicks(
                date_from=date_from,
                date_to=date_to,
                keyword=keyword,
                limit=20,
            ),
        }

    def overview(self, *, date_from: Optional[date], date_to: Optional[date]) -> Dict[str, Any]:
        filters = self._event_filters(date_from=date_from, date_to=date_to)
        unique_click_filters = self._unique_click_filters(date_from=date_from, date_to=date_to, keyword=None)
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
            unique_click_count = int(
                conn.execute(
                    select(func.count()).select_from(self.unique_clicks).where(and_(*unique_click_filters))
                ).scalar() or 0
            )
        return {
            'date_from': date_from.isoformat() if date_from else None,
            'date_to': date_to.isoformat() if date_to else None,
            'page_views': int(row['page_views'] or 0),
            'unique_visitors': int(row['unique_visitors'] or 0),
            'searches': int(row['searches'] or 0),
            'search_visitors': int(row['search_visitors'] or 0),
            'paper_clicks': unique_click_count,
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

    def recent_clicks(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        search_events = self.events.alias('search_events')
        safe_limit = min(max(int(limit or 12), 1), 100)
        filters = self._unique_click_filters(
            date_from=date_from,
            date_to=date_to,
            keyword=keyword,
        )
        stmt = (
            select(
                self.unique_clicks.c.id,
                self.unique_clicks.c.clicked_at,
                self.unique_clicks.c.visitor_id,
                self.unique_clicks.c.paper_id,
                self.unique_clicks.c.paper_title,
                self.unique_clicks.c.search_event_id.label('related_search_event_id'),
                self.unique_clicks.c.query_raw,
                self.unique_clicks.c.query_normalized,
                search_events.c.occurred_at.label('searched_at'),
            )
            .select_from(
                self.unique_clicks.outerjoin(
                    search_events,
                    search_events.c.id == self.unique_clicks.c.search_event_id,
                )
            )
            .where(and_(*filters))
            .order_by(self.unique_clicks.c.clicked_at.desc(), self.unique_clicks.c.id.desc())
            .limit(safe_limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._normalize_record(row) for row in rows]

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
        if dataset == 'clicks':
            return self._click_export_rows(date_from=date_from, date_to=date_to, keyword=keyword)
        if dataset == 'paper_click_ranking':
            return self._paper_click_ranking_rows(date_from=date_from, date_to=date_to, keyword=keyword)
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
                self.events.c.id.label('search_event_id'),
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
            rows = [self._normalize_record(row) for row in conn.execute(stmt).mappings().all()]
            if not rows:
                return rows
            related_ids = [int(row['search_event_id']) for row in rows if row.get('search_event_id') is not None]
            click_map = self._clicks_by_search_event(conn, related_ids)
        for row in rows:
            clicks = click_map.get(int(row['search_event_id']), [])
            row['clicked_paper_count'] = len(clicks)
            row['clicked_paper_ids'] = '; '.join(str(item['paper_id']) for item in clicks if item.get('paper_id') is not None)
            row['clicked_paper_titles'] = '; '.join(item['paper_title'] for item in clicks if item.get('paper_title'))
            row['clicked_at_times'] = '; '.join(item['clicked_at'] for item in clicks if item.get('clicked_at'))
        return rows

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

    def _click_export_rows(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
    ) -> List[Dict[str, Any]]:
        search_events = self.events.alias('search_events')
        filters = self._unique_click_filters(
            date_from=date_from,
            date_to=date_to,
            keyword=keyword,
        )
        stmt = (
            select(
                self.unique_clicks.c.clicked_at,
                search_events.c.occurred_at.label('searched_at'),
                self.unique_clicks.c.visitor_id,
                self.unique_clicks.c.search_event_id.label('related_search_event_id'),
                self.unique_clicks.c.query_raw,
                self.unique_clicks.c.query_normalized,
                self.unique_clicks.c.paper_id,
                self.unique_clicks.c.paper_title,
            )
            .select_from(
                self.unique_clicks.outerjoin(
                    search_events,
                    search_events.c.id == self.unique_clicks.c.search_event_id,
                )
            )
            .where(and_(*filters))
            .order_by(self.unique_clicks.c.clicked_at.desc(), self.unique_clicks.c.id.desc())
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._normalize_record(row) for row in rows]

    def _paper_click_ranking_rows(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
    ) -> List[Dict[str, Any]]:
        return self.top_paper_clicks(
            date_from=date_from,
            date_to=date_to,
            keyword=keyword,
            limit=10000,
        )

    def _insert_event(self, **payload: Any) -> int:
        stmt = insert(self.events).values(**payload)
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
            inserted_id = getattr(result, 'inserted_primary_key', None)
            if inserted_id and inserted_id[0] is not None:
                return int(inserted_id[0])
            lastrowid = getattr(result, 'lastrowid', None)
            return int(lastrowid or 0)

    def _insert_unique_click(
        self,
        *,
        search_event_id: Optional[int],
        visitor_id: str,
        query_raw: Optional[str],
        paper_id: int,
        paper_title: str,
    ) -> None:
        if not search_event_id:
            return
        search_context = self._search_context(search_event_id)
        normalized_query = self._normalize_query((search_context or {}).get('query_raw') or query_raw or '')
        stmt = insert(self.unique_clicks).values(
            search_event_id=search_event_id,
            visitor_id=(search_context or {}).get('visitor_id') or visitor_id,
            query_raw=(search_context or {}).get('query_raw') or query_raw,
            query_normalized=normalized_query,
            paper_id=paper_id,
            paper_title=paper_title,
        )
        try:
            with self._engine.begin() as conn:
                conn.execute(stmt)
        except IntegrityError:
            return

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

    def _unique_click_filters(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
    ) -> List[Any]:
        filters: List[Any] = []
        if date_from:
            filters.append(self.unique_clicks.c.clicked_at >= self._start_dt(date_from))
        if date_to:
            filters.append(self.unique_clicks.c.clicked_at < self._end_exclusive_dt(date_to))
        normalized_keyword = self._normalize_query(keyword or '')
        if normalized_keyword:
            pattern = f'%{normalized_keyword}%'
            filters.append(
                or_(
                    self.unique_clicks.c.query_normalized.like(pattern),
                    func.lower(func.coalesce(self.unique_clicks.c.query_raw, '')).like(pattern),
                    func.lower(func.coalesce(self.unique_clicks.c.paper_title, '')).like(pattern),
                )
            )
        return filters

    def top_paper_clicks(
        self,
        *,
        date_from: Optional[date],
        date_to: Optional[date],
        keyword: Optional[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        safe_limit = min(max(int(limit or 20), 1), 10000)
        filters = self._unique_click_filters(date_from=date_from, date_to=date_to, keyword=keyword)
        stmt = (
            select(
                self.unique_clicks.c.query_normalized,
                self.unique_clicks.c.paper_id,
                self.unique_clicks.c.paper_title,
                func.count().label('unique_click_count'),
                func.count(distinct(self.unique_clicks.c.visitor_id)).label('unique_visitors'),
                func.min(self.unique_clicks.c.clicked_at).label('first_clicked_at'),
                func.max(self.unique_clicks.c.clicked_at).label('last_clicked_at'),
            )
            .where(and_(*filters) if filters else text('1=1'))
            .group_by(
                self.unique_clicks.c.query_normalized,
                self.unique_clicks.c.paper_id,
                self.unique_clicks.c.paper_title,
            )
            .order_by(func.count().desc(), func.max(self.unique_clicks.c.clicked_at).desc())
            .limit(safe_limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._normalize_record(row) for row in rows]

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
                'search_event_id', 'occurred_at', 'visitor_id', 'query_raw', 'query_normalized',
                'year_from', 'year_to', 'sort', 'result_count', 'latency_ms',
                'path', 'ip_hash', 'user_agent', 'clicked_paper_count',
                'clicked_paper_ids', 'clicked_paper_titles', 'clicked_at_times',
            ],
            'keywords': [
                'keyword', 'searches', 'visitors', 'zero_result_searches', 'last_searched_at',
            ],
            'daily': [
                'date', 'page_views', 'unique_visitors', 'searches',
                'search_visitors', 'zero_result_searches',
            ],
            'clicks': [
                'clicked_at', 'searched_at', 'visitor_id', 'related_search_event_id',
                'query_raw', 'query_normalized', 'paper_id', 'paper_title',
            ],
            'paper_click_ranking': [
                'query_normalized', 'paper_id', 'paper_title', 'unique_click_count',
                'unique_visitors', 'first_clicked_at', 'last_clicked_at',
            ],
        }
        return default_headers.get(dataset, [])

    def _start_dt(self, value: date) -> datetime:
        return datetime.combine(value, time.min)

    def _end_exclusive_dt(self, value: date) -> datetime:
        return datetime.combine(value + timedelta(days=1), time.min)

    def _normalize_query(self, text: str) -> str:
        return ' '.join(str(text or '').strip().lower().split())

    def _clicks_by_search_event(self, conn, related_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
        if not related_ids:
            return {}
        stmt = (
            select(
                self.unique_clicks.c.search_event_id.label('related_search_event_id'),
                self.unique_clicks.c.paper_id,
                self.unique_clicks.c.paper_title,
                self.unique_clicks.c.clicked_at,
            )
            .where(
                self.unique_clicks.c.search_event_id.in_(related_ids)
            )
            .order_by(self.unique_clicks.c.clicked_at.asc(), self.unique_clicks.c.id.asc())
        )
        rows = [self._normalize_record(row) for row in conn.execute(stmt).mappings().all()]
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for row in rows:
            search_event_id = row.get('related_search_event_id')
            if search_event_id is None:
                continue
            grouped.setdefault(int(search_event_id), []).append(row)
        return grouped

    def _search_context(self, search_event_id: int) -> Optional[Dict[str, Any]]:
        stmt = (
            select(
                self.events.c.id,
                self.events.c.visitor_id,
                self.events.c.query_raw,
            )
            .where(
                and_(
                    self.events.c.id == search_event_id,
                    self.events.c.event_type == 'search',
                )
            )
            .limit(1)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        return self._normalize_record(row)

    def _ensure_missing_columns(self) -> None:
        inspector = inspect(self._engine)
        columns = {col['name'] for col in inspector.get_columns('analytics_events')}
        desired = {
            'paper_id': self._column_type_sql('INTEGER'),
            'paper_title': self._column_type_sql('TEXT'),
            'related_search_event_id': self._column_type_sql('INTEGER'),
        }
        for name, type_sql in desired.items():
            if name in columns:
                continue
            with self._engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE analytics_events ADD COLUMN {name} {type_sql}'))

    def _column_type_sql(self, generic_type: str) -> str:
        if self.backend_label == 'mysql':
            mapping = {
                'INTEGER': 'INT',
                'TEXT': 'TEXT',
            }
            return mapping.get(generic_type, generic_type)
        return generic_type

    def _normalize_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        for key, value in dict(record).items():
            if isinstance(value, datetime):
                normalized[key] = self._to_beijing_time(value)
            elif isinstance(value, date):
                normalized[key] = value.isoformat()
            elif isinstance(value, str) and key in self._DATETIME_FIELD_NAMES:
                normalized[key] = self._to_beijing_time(value)
            else:
                normalized[key] = value
        return normalized

    def _to_beijing_time(self, value: datetime | str) -> str:
        parsed: Optional[datetime] = None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            text_value = value.strip()
            if not text_value:
                return value
            try:
                parsed = datetime.fromisoformat(text_value.replace('Z', '+00:00'))
            except ValueError:
                return value
        if parsed is None:
            return str(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(self._DISPLAY_TZ).strftime('%Y-%m-%d %H:%M:%S')


def json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
