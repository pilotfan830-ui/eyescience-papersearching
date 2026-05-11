from pydantic import BaseModel
from typing import Dict, List, Optional


class PaperBrief(BaseModel):
    id: int
    title: str
    authors: str
    year: Optional[int] = None
    keywords: List[str] = []
    abstract_snippet: str
    citation: Optional[str] = None
    citation_snippet: Optional[str] = None
    final_score: float
    fts_score: float
    semantic_score: float
    author_score: Optional[float] = None
    recency_score: Optional[float] = None
    qwen_score: Optional[float] = None
    match_debug: Optional[Dict] = None
    score: float


class SearchResponse(BaseModel):
    search_event_id: Optional[int] = None
    query: str
    total: int
    semantic_enabled: bool
    timing: Optional[Dict] = None
    items: List[PaperBrief]


class PaperDetail(BaseModel):
    id: int
    title: str
    authors: List[str]
    year: Optional[int] = None
    keywords: List[str]
    abstract: str
    citation: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None


class PaperClickRequest(BaseModel):
    paper_id: int
    paper_title: str
    search_event_id: Optional[int] = None
    query: Optional[str] = None
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    sort: Optional[str] = None
