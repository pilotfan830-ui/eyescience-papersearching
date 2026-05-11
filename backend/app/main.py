import hashlib
import os
import secrets
import uuid
from urllib.parse import quote
from datetime import date
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .analytics import AnalyticsStore, json_bytes
from .schemas import AdminLoginRequest, PaperClickRequest, PaperDetail, SearchResponse
from .search_engine import SearchEngine

app = FastAPI(title='Paper Search API', version='0.1.0')
engine = SearchEngine()
analytics = AnalyticsStore(engine.db_backend, engine.db_path)
FRONTEND_INDEX = Path(__file__).resolve().parents[2] / 'frontend' / 'index.html'
FRONTEND_DIR = FRONTEND_INDEX.parent
ADMIN_ANALYTICS_INDEX = FRONTEND_DIR / 'admin-analytics.html'
VISITOR_COOKIE_NAME = 'pst_vid'
VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 365
ADMIN_COOKIE_NAME = 'pst_admin_auth'
ADMIN_COOKIE_MAX_AGE = 60 * 60 * 8

if FRONTEND_DIR.exists():
    app.mount('/static', StaticFiles(directory=FRONTEND_DIR), name='static')

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


def _clean_export_author(author: str) -> str:
    return ' '.join((author or '').replace('#', '').split()).strip(' ,;')


def _clean_journal_name(journal: str | None) -> str:
    raw = ' '.join((journal or '').split()).strip(' .')
    if not raw:
        return ''
    parts = [p.strip() for p in raw.split('.') if p.strip()]
    if len(parts) >= 2:
        return parts[-1]
    return raw


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get('x-forwarded-for', '')
    if forwarded.strip():
        return forwarded.split(',')[0].strip()
    real_ip = request.headers.get('x-real-ip', '').strip()
    if real_ip:
        return real_ip
    if request.client and request.client.host:
        return request.client.host
    return ''


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _ensure_visitor_id(request: Request, response: Response) -> str:
    raw_visitor_id = (request.cookies.get(VISITOR_COOKIE_NAME) or '').strip()
    if not raw_visitor_id:
        raw_visitor_id = uuid.uuid4().hex
        response.set_cookie(
            key=VISITOR_COOKIE_NAME,
            value=raw_visitor_id,
            max_age=VISITOR_COOKIE_MAX_AGE,
            httponly=True,
            samesite='lax',
        )
    return _hash_text(raw_visitor_id)


def _request_ip_hash(request: Request) -> Optional[str]:
    ip = _client_ip(request)
    return _hash_text(ip) if ip else None


def _trim_header(value: Optional[str], limit: int = 512) -> Optional[str]:
    text = (value or '').strip()
    if not text:
        return None
    return text[:limit]


def _admin_username() -> str:
    return (os.getenv('PAPER_SEARCH_ADMIN_USERNAME') or 'admin').strip() or 'admin'


def _admin_password() -> str:
    return (os.getenv('PAPER_SEARCH_ADMIN_PASSWORD') or 'admin123456').strip() or 'admin123456'


def _admin_session_secret() -> str:
    return (
        os.getenv('PAPER_SEARCH_ADMIN_SESSION_SECRET')
        or os.getenv('ADMIN_SESSION_SECRET')
        or f'{app.title}:{_admin_username()}:{_admin_password()}'
    ).strip()


def _admin_session_value() -> str:
    return _hash_text(f'{_admin_username()}:{_admin_password()}:{_admin_session_secret()}')


def _is_admin_authenticated(request: Request) -> bool:
    expected = (
        os.getenv('PAPER_SEARCH_ADMIN_TOKEN')
        or os.getenv('ADMIN_TOKEN')
        or ''
    ).strip()
    provided = (
        request.headers.get('x-admin-token')
        or request.query_params.get('admin_token')
        or ''
    ).strip()
    if expected and provided and secrets.compare_digest(provided, expected):
        return True
    cookie_value = (request.cookies.get(ADMIN_COOKIE_NAME) or '').strip()
    if cookie_value and secrets.compare_digest(cookie_value, _admin_session_value()):
        return True
    return False


def _require_admin_access(request: Request) -> None:
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail='admin login required')


def _apply_admin_cookie(response: Response) -> None:
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=_admin_session_value(),
        max_age=ADMIN_COOKIE_MAX_AGE,
        httponly=True,
        samesite='lax',
    )


def _clear_admin_cookie(response: Response) -> None:
    response.delete_cookie(key=ADMIN_COOKIE_NAME, httponly=True, samesite='lax')


@app.get('/', include_in_schema=False)
def home(request: Request):
    if FRONTEND_INDEX.exists():
        response = FileResponse(FRONTEND_INDEX)
        visitor_id = _ensure_visitor_id(request, response)
        analytics.record_page_view(
            visitor_id=visitor_id,
            ip_hash=_request_ip_hash(request),
            user_agent=_trim_header(request.headers.get('user-agent')),
            referer=_trim_header(request.headers.get('referer')),
            path=request.url.path,
            status_code=200,
        )
        return response
    raise HTTPException(status_code=404, detail='frontend not found')


@app.get('/admin/analytics', include_in_schema=False)
def admin_analytics_page(request: Request):
    if not _is_admin_authenticated(request):
        redirect_to = f'/?admin_login=1&next={quote("/admin/analytics", safe="/")}'
        return RedirectResponse(url=redirect_to, status_code=307)
    if ADMIN_ANALYTICS_INDEX.exists():
        return FileResponse(ADMIN_ANALYTICS_INDEX)
    raise HTTPException(status_code=404, detail='admin analytics page not found')


@app.post('/api/admin/login')
def admin_login(payload: AdminLoginRequest):
    username = (payload.username or '').strip()
    password = payload.password or ''
    if not (
        secrets.compare_digest(username, _admin_username())
        and secrets.compare_digest(password, _admin_password())
    ):
        raise HTTPException(status_code=401, detail='invalid admin credentials')
    response = Response(
        content=json_bytes({'ok': True, 'username': username}),
        media_type='application/json; charset=utf-8',
    )
    _apply_admin_cookie(response)
    return response


@app.post('/api/admin/logout')
def admin_logout():
    response = Response(
        content=json_bytes({'ok': True}),
        media_type='application/json; charset=utf-8',
    )
    _clear_admin_cookie(response)
    return response


@app.get('/api/health')
def health():
    payload = {
        'ok': True,
        'papers': len(engine.papers),
        'db_backend': engine.db_backend,
        'db_source': engine.db_display,
        'rewrite_enabled': engine.rewrite_enabled,
        'qwen_rerank_enabled': engine.qwen_rerank_enabled,
        'qwen_model': engine._qwen_model,
        'startup_timing': engine.startup_timing,
        'last_rewrite_debug': engine.get_last_rewrite_debug(),
        'last_rerank_debug': engine.get_last_rerank_debug(),
        'last_author_debug': engine.get_last_author_debug(),
        'analytics_enabled': analytics.enabled,
        'analytics_backend': analytics.backend_label,
    }
    payload.update(engine.embedding_status())
    return payload


@app.get('/api/search', response_model=SearchResponse)
def search(
    request: Request,
    response: Response,
    q: str = Query(..., min_length=1),
    limit: int = 20,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    sort: str = Query('best_match', pattern='^(best_match|most_recent)$'),
):
    visitor_id = _ensure_visitor_id(request, response)
    payload = engine.search_papers(
        q,
        topk=limit,
        year_from=year_from,
        year_to=year_to,
        sort=sort,
        include_timing=True,
    )
    items = payload['results']
    latency_ms = None
    if payload.get('timing'):
        latency_ms = payload['timing'].get('total_ms')
    search_event_id = analytics.record_search(
        visitor_id=visitor_id,
        ip_hash=_request_ip_hash(request),
        user_agent=_trim_header(request.headers.get('user-agent')),
        referer=_trim_header(request.headers.get('referer')),
        path=request.url.path,
        query_raw=q,
        year_from=year_from,
        year_to=year_to,
        sort=sort,
        result_count=len(items),
        latency_ms=float(latency_ms) if latency_ms is not None else None,
        status_code=200,
    )
    return SearchResponse(
        search_event_id=search_event_id or None,
        query=q,
        total=len(items),
        semantic_enabled=payload['semantic_enabled'],
        timing=payload.get('timing'),
        items=items,
    )


@app.post('/api/analytics/paper-click')
def track_paper_click(
    payload: PaperClickRequest,
    request: Request,
    response: Response,
):
    visitor_id = _ensure_visitor_id(request, response)
    click_event_id = analytics.record_paper_click(
        visitor_id=visitor_id,
        ip_hash=_request_ip_hash(request),
        user_agent=_trim_header(request.headers.get('user-agent')),
        referer=_trim_header(request.headers.get('referer')),
        path=request.url.path,
        paper_id=payload.paper_id,
        paper_title=payload.paper_title,
        related_search_event_id=payload.search_event_id,
        query_raw=payload.query,
        year_from=payload.year_from,
        year_to=payload.year_to,
        sort=payload.sort,
        status_code=200,
    )
    return {'ok': True, 'click_event_id': click_event_id or None}


@app.get('/api/admin/analytics')
def admin_analytics(
    request: Request,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    keyword: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    keyword_limit: int = 20,
):
    _require_admin_access(request)
    try:
        return analytics.dashboard(
            date_from=date_from,
            date_to=date_to,
            keyword=keyword,
            page=page,
            page_size=page_size,
            keyword_limit=keyword_limit,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get('/api/admin/analytics/export')
def admin_analytics_export(
    request: Request,
    dataset: str = Query('searches', pattern='^(searches|keywords|daily|clicks|paper_click_ranking)$'),
    format: str = Query('csv', pattern='^(csv|json)$'),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    keyword: Optional[str] = None,
):
    _require_admin_access(request)
    try:
        if format == 'json':
            content = json_bytes(
                analytics.export_json_payload(
                    dataset=dataset,
                    date_from=date_from,
                    date_to=date_to,
                    keyword=keyword,
                )
            )
            filename = f'analytics_{dataset}_{date.today().isoformat()}.json'
            return Response(
                content=content,
                media_type='application/json; charset=utf-8',
                headers={'Content-Disposition': f'attachment; filename="{filename}"'},
            )
        content = analytics.export_csv_bytes(
            dataset=dataset,
            date_from=date_from,
            date_to=date_to,
            keyword=keyword,
        )
        filename = f'analytics_{dataset}_{date.today().isoformat()}.csv'
        return Response(
            content=content,
            media_type='text/csv; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get('/api/papers/{paper_id}', response_model=PaperDetail)
def detail(paper_id: int):
    p = engine.get_detail(paper_id)
    if not p:
        raise HTTPException(status_code=404, detail='paper not found')
    return PaperDetail(
        id=p.id,
        title=p.title,
        authors=p.authors,
        year=p.year,
        keywords=p.keywords,
        abstract=p.abstract,
        citation=p.citation,
        doi=p.doi,
        url=p.url,
    )


def _to_ris(p) -> str:
    lines = ['TY  - JOUR', f'TI  - {p.title}']
    journal = _clean_journal_name(p.journal)
    for a in p.authors:
        clean = _clean_export_author(a)
        if clean:
            lines.append(f'AU  - {clean}')
    if journal:
        lines.append(f'JO  - {journal}')
        lines.append(f'T2  - {journal}')
    if p.year:
        lines.append(f'PY  - {p.year}')
    if p.volume:
        lines.append(f'VL  - {p.volume}')
    if p.issue:
        lines.append(f'IS  - {p.issue}')
    if p.page_start:
        lines.append(f'SP  - {p.page_start}')
    if p.page_end:
        lines.append(f'EP  - {p.page_end}')
    elif p.pages:
        lines.append(f'SP  - {p.pages}')
    if p.doi:
        lines.append(f'DO  - {p.doi}')
    if p.url:
        lines.append(f'UR  - {p.url}')
    if p.abstract:
        lines.append(f'AB  - {p.abstract}')
    if p.citation:
        lines.append(f'N1  - {p.citation}')
    lines.append('ER  -')
    return '\r\n'.join(lines) + '\r\n'


@app.get('/api/export/{paper_id}.ris', response_class=PlainTextResponse)
def export_ris(paper_id: int):
    p = engine.get_detail(paper_id)
    if not p:
        raise HTTPException(status_code=404, detail='paper not found')
    return PlainTextResponse(
        _to_ris(p),
        media_type='application/x-research-info-systems; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="paper_{paper_id}.ris"'},
    )


@app.get('/api/export/{paper_id}.enw', response_class=PlainTextResponse)
def export_endnote(paper_id: int):
    p = engine.get_detail(paper_id)
    if not p:
        raise HTTPException(status_code=404, detail='paper not found')
    lines = [f'%0 Journal Article', f'%T {p.title}']
    journal = _clean_journal_name(p.journal)
    if journal:
        lines.append(f'%B {journal}')
    if p.year:
        lines.append(f'%D {p.year}')
    if p.volume:
        lines.append(f'%V {p.volume}')
    if p.issue:
        lines.append(f'%N {p.issue}')
    if p.pages:
        lines.append(f'%P {p.pages}')
    elif p.page_start and p.page_end:
        lines.append(f'%P {p.page_start}-{p.page_end}')
    elif p.page_start:
        lines.append(f'%P {p.page_start}')
    if p.doi:
        lines.append(f'%R {p.doi}')
    if p.url:
        lines.append(f'%U {p.url}')
    if p.abstract:
        lines.append(f'%X {p.abstract}')
    if p.citation:
        cit_one = ' '.join(p.citation.replace('\r\n', '\n').split())
        lines.append(f'%Z {cit_one}')
    # Keep authors last; some Windows EndNote filters treat unknown following tags
    # as author continuations when non-ASCII names are present.
    for a in p.authors:
        clean = _clean_export_author(a)
        if clean:
            lines.append(f'%A {clean}')
    content = ('\ufeff' + '\r\n'.join(lines) + '\r\n\r\n').encode('utf-8')
    return Response(
        content=content,
        media_type='application/x-endnote-refer',
        headers={'Content-Disposition': f'attachment; filename="paper_{paper_id}.enw"'},
    )


if __name__ == '__main__':
    uvicorn.run('app.main:app', host='0.0.0.0', port=421, reload=True)
