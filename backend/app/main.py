from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response

from .schemas import PaperDetail, SearchResponse
from .search_engine import SearchEngine

app = FastAPI(title='Paper Search API', version='0.1.0')
engine = SearchEngine()
FRONTEND_INDEX = Path(__file__).resolve().parents[2] / 'frontend' / 'index.html'

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


@app.get('/', include_in_schema=False)
def home():
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX)
    raise HTTPException(status_code=404, detail='frontend not found')


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
    }
    payload.update(engine.embedding_status())
    return payload


@app.get('/api/search', response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=1),
    limit: int = 20,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    sort: str = Query('best_match', pattern='^(best_match|most_recent)$'),
):
    payload = engine.search_papers(
        q,
        topk=limit,
        year_from=year_from,
        year_to=year_to,
        sort=sort,
        include_timing=True,
    )
    items = payload['results']
    return SearchResponse(
        query=q,
        total=len(items),
        semantic_enabled=payload['semantic_enabled'],
        timing=payload.get('timing'),
        items=items,
    )


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
