"""Server-rendered gallery views backed by the media catalog."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from html import escape
import json as _jsonlib
from pathlib import Path
import re
from typing import Optional
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import Select, exists, false, func, or_, select

from app.api.deps import require_state
from app.api.i18n_web import render_lang_switcher, request_translator
from app.models.annotation import MediaAnnotation
from app.models.asset import DerivedAsset
from app.models.face import Face
from app.models.media import MediaFile
from app.models.person import Person
from app.models.semantic import SearchDocument
from app.models.tag import Tag
from app.services.search import HybridSearchService
from app.services.search.backend import SqlAlchemyHybridSearchBackend
from app.services.search.hybrid import FeedbackReranker
from app.services.scanner.service import _path_exists


router = APIRouter(tags=["gallery"])

PERSON_TAG_TYPES = ("person", "people", "face")
PLACE_TAG_TYPES = ("place", "location", "place_detail", "geo", "geo_detail")
PAGE_SIZE = 48
PAGE_SIZE_OPTIONS = (50, 100, 200, 300, 500)
DEFAULT_PAGE_SIZE = 100


def _normalize_page_size(value: int | None) -> int:
    return value if value in PAGE_SIZE_OPTIONS else DEFAULT_PAGE_SIZE
GALLERY_SEARCH_LIMIT = 99999
QUICK_SEARCH_TERMS = ("얼굴", "아기", "바다", "꽃", "여행", "영수증", "baby", "beach")
SORT_NEWEST = "newest"
SORT_OLDEST = "oldest"
SORT_OPTIONS = (SORT_NEWEST, SORT_OLDEST)

_INTENT_REASON_LABELS: dict[str, str] = {
    "fallback": "스마트 검색",
    "date_relaxed": "날짜 범위 확대",
    "fuzzy_corrected": "유사어 보정",
    "auto-face": "얼굴·인물 검색",
    "auto-travel": "여행 사진 검색",
    "auto-celebration": "행사 사진 검색",
    "auto-mixed": "복합 검색",
    "auto-text-hint": "텍스트 검색",
    "auto-screen-text": "화면·캡처 검색",
    "auto-code": "문서·코드 검색",
    "auto-word-match": "단어 일치",
    "auto-phrase-code": "구문 일치",
    "planner-ocr": "텍스트 추출",
    "planner-visual": "이미지 검색",
    "manual": "직접 지정",
    "condition_visual_only": "키워드 검색",
    "condition_place_only": "장소 검색",
    "condition_person_only": "인물 검색",
    "empty": "검색어 없음",
    "degenerate": "검색 불가",
}


def _friendly_intent_label(search_meta: dict, translate=None) -> str:
    reason = search_meta.get("fallback") or search_meta.get("intent_reason", "")
    if translate is not None:
        label = translate(f"intent.{reason}")
        if label != f"intent.{reason}":
            return label
    return _INTENT_REASON_LABELS.get(reason, reason)


@router.get("/", response_class=HTMLResponse)
def home_page(
    request: Request,
    media_type: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    person: Optional[str] = Query(default=None),
    place: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    sort: str = Query(default=SORT_NEWEST),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=DEFAULT_PAGE_SIZE),
) -> HTMLResponse:
    return gallery_page(
        request,
        media_type=media_type,
        date_from=date_from,
        date_to=date_to,
        person=person,
        place=place,
        q=q,
        sort=sort,
        page=page,
        per_page=per_page,
    )


@router.get("/gallery", response_class=HTMLResponse)
# sync def — DB 조회·하이브리드 검색(CLIP 임베딩 로드)이 무거워서 FastAPI가
# threadpool에서 돌리게 한다. async def로 두면 이벤트 루프가 통째로 멈춰
# /healthz까지 막히고 '연결 끊김' 오버레이가 오탐된다.
def gallery_page(
    request: Request,
    media_type: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    person: Optional[str] = Query(default=None),
    place: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    sort: str = Query(default=SORT_NEWEST),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=DEFAULT_PAGE_SIZE),
) -> HTMLResponse:
    database = require_state(request, "database")
    settings = require_state(request, "settings")
    pipeline = require_state(request, "pipeline")
    locale, _ = request_translator(request)
    # JS에 안전하게 박을 수 있게 번역 문자열을 JSON(따옴표 포함)으로 인코딩.
    def _json(key: str, **fmt: object) -> str:
        return _jsonlib.dumps(_(key, **fmt), ensure_ascii=False)
    log_events = not pipeline.has_active_library_job()
    page_size = _normalize_page_size(per_page)
    offset = (page - 1) * page_size
    sort_order = _normalize_sort(sort)
    parsed_date_from = _parse_date(date_from)
    parsed_date_to = _parse_date(date_to)
    search_meta: dict[str, str] | None = None
    ranked_ids: list[str] | None = None

    with database.session_factory() as session:
        if q and q.strip():
            backend = SqlAlchemyHybridSearchBackend(
                session,
                embeddings_root=settings.embeddings_root,
                clip_enabled=settings.semantic_clip_enabled,
                log_events=log_events,
            )
            service = HybridSearchService(backend, reranker=FeedbackReranker(backend))
            search_results, search_meta = service.search_with_meta(
                q,
                limit=GALLERY_SEARCH_LIMIT,
                place_filter=place,
                date_from=_start_of_day(parsed_date_from),
                date_to=_end_of_day(parsed_date_to),
                mode="hybrid",
            )
            ranked_ids = [str(item["file_id"]) for item in search_results]

        ids_query = _build_gallery_ids_query(
            media_type=media_type,
            date_from=_start_of_day(parsed_date_from),
            date_to=_end_of_day(parsed_date_to),
            person=person,
            place=place,
            query=None if ranked_ids is not None else q,
            file_ids=ranked_ids,
            sort=sort_order,
            require_analysis_complete=settings.gallery_require_analysis_complete,
        )
        # 가시성 계약을 켜면 분석 미완 사진은 숨겨진다. 사라진 게 아니라 "분석 중"
        # 임을 정직하게 알리려, 노출 후보지만 아직 search_document 없는 이미지 수를 센다.
        analyzing_count = 0
        if settings.gallery_require_analysis_complete:
            analyzing_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(MediaFile)
                    .where(
                        # error는 영구 실패라 "분석 중"이 아니다 — 제외해 배너가 영원히
                        # 안 사라지는 거짓 표시를 막는다(게이트는 어차피 숨김 유지).
                        MediaFile.status.not_in(("missing", "replaced", "excluded", "error")),
                        MediaFile.media_kind == "image",
                        ~exists().where(SearchDocument.file_id == MediaFile.file_id),
                    )
                )
                or 0
            )
        # ids_query already filters to the relevant set (ranked_ids when q is set)
        # and applies ORDER BY captured_at per sort_order, so pagination is date-consistent.
        total = int(session.scalar(select(func.count()).select_from(ids_query.subquery())) or 0)
        file_ids = list(session.scalars(ids_query.limit(page_size).offset(offset)))

        items: list[MediaFile] = []
        annotation_map: dict[str, MediaAnnotation] = {}
        asset_map: dict[str, list[DerivedAsset]] = defaultdict(list)
        tag_map: dict[str, list[Tag]] = defaultdict(list)
        people_map: dict[str, list[tuple[int, str]]] = defaultdict(list)
        if file_ids:
            items = list(
                session.scalars(
                    select(MediaFile)
                    .where(MediaFile.file_id.in_(file_ids))
                )
            )
            page_rank = {file_id: index for index, file_id in enumerate(file_ids)}
            items.sort(key=lambda item: page_rank.get(item.file_id, len(page_rank)))
            for asset in session.scalars(
                select(DerivedAsset)
                .where(DerivedAsset.file_id.in_(file_ids))
                .order_by(DerivedAsset.file_id.asc(), DerivedAsset.created_at.asc(), DerivedAsset.id.asc())
            ):
                asset_map[asset.file_id].append(asset)
            for tag in session.scalars(
                select(Tag)
                .where(Tag.file_id.in_(file_ids))
                .order_by(Tag.tag_type.asc(), Tag.tag_value.asc())
            ):
                tag_map[tag.file_id].append(tag)
            for annotation in session.scalars(
                select(MediaAnnotation).where(MediaAnnotation.file_id.in_(file_ids))
            ):
                annotation_map[annotation.file_id] = annotation
            # 라이트박스 "인물" 칩: 사진 속 named 인물(id+이름). 무명 클러스터·애칭은
            # 제외하고 사람당 한 번만(중복 face가 있어도). 수동 태깅 UI가 이걸 쓴다.
            for fid_, pid_, pname_ in session.execute(
                select(Face.file_id, Person.id, Person.display_name)
                .join(Person, Person.id == Face.person_id)
                .where(Face.file_id.in_(file_ids), Person.merged_into_id.is_(None))
                .where(Person.display_name.not_like("person-%"))
            ):
                bucket = people_map[str(fid_)]
                if not any(existing[0] == int(pid_) for existing in bucket):
                    bucket.append((int(pid_), str(pname_)))

        person_options = _list_named_person_display_names(session)
        place_options = _list_tag_values(session, PLACE_TAG_TYPES)

    current_url = request.url.path
    if request.url.query:
        current_url += f"?{request.url.query}"
    # 원본 저장소(외장하드·NAS)가 지금 연결돼 있는지 루트 단위로 1회만 확인한다.
    # 분리돼 있으면 썸네일은 그대로 보이되 다운로드만 비활성화한다(스캐너와 동일
    # 기준 _path_exists). 같은 루트는 캐시해 페이지당 stat 몇 번으로 끝낸다.
    _root_online: dict[str, bool] = {}

    def _original_offline(root: str | None) -> bool:
        if not root:
            return False
        if root not in _root_online:
            _root_online[root] = _path_exists(Path(root))
        return not _root_online[root]

    cards = [
        _render_card(
            media_file=item,
            asset=_select_card_asset(asset_map.get(item.file_id, [])),
            tags=tag_map.get(item.file_id, []),
            annotation=annotation_map.get(item.file_id),
            index=index,
            next_url=f"{current_url}#card-{item.file_id}",
            original_offline=_original_offline(item.source_root),
            people=people_map.get(item.file_id, []),
            person_options=person_options,
        )
        for index, item in enumerate(items)
    ]

    page_count = max(1, (total + page_size - 1) // page_size)
    has_prev = page > 1
    has_next = offset + len(items) < total
    person_available = bool(person_options)
    place_available = bool(place_options)
    # '필터' 버튼에 활성 필터 개수를 표시해, 팝오버를 닫아둬도 기간·인물
    # 필터가 걸려 있는지 한눈에 알 수 있게 한다.
    filter_active_count = sum(
        1 for value in (date_from, date_to, person) if value and str(value).strip()
    )
    per_page_options = "".join(
        f'<option value="{escape(_per_page_url(request, n))}"{" selected" if n == page_size else ""}>{_("gallery.per_page", n=n)}</option>'
        for n in PAGE_SIZE_OPTIONS
    )
    active_filter_summary = _render_active_filter_summary(
        q=q,
        search_meta=search_meta,
        media_type=media_type,
        date_from=date_from,
        date_to=date_to,
        person=person,
        place=place,
        sort=sort_order,
    )
    # 가시성 계약: 분석 미완 사진은 숨겨진다. "사라진 게 아니라 분석 중"임을 정직하게
    # 알리는 안내 칩(분석 끝나면 자동으로 사진첩에 나타난다).
    analyzing_notice = (
        f'<span class="meta-pill analyzing">{_("gallery.analyzing", count=analyzing_count)}</span>'
        if analyzing_count > 0
        else ""
    )

    html = f"""<!doctype html>
<html lang="{locale}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_("gallery.title")}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f5f5f7;
      --panel: rgba(255, 255, 255, 0.96);
      --panel-strong: #ffffff;
      --text: #1d1d1f;
      --muted: #86868b;
      --line: rgba(0, 0, 0, 0.10);
      --line-strong: rgba(0, 0, 0, 0.16);
      --accent: #0a84ff;
      --accent-deep: #0060df;
      --accent-soft: rgba(10, 132, 255, 0.12);
      --shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #1c1c1e;
        --panel: rgba(44, 44, 46, 0.96);
        --panel-strong: #2c2c2e;
        --text: #f5f5f7;
        --muted: #98989d;
        --line: rgba(255, 255, 255, 0.12);
        --line-strong: rgba(255, 255, 255, 0.22);
        --accent: #0a84ff;
        --accent-deep: #409cff;
        --accent-soft: rgba(10, 132, 255, 0.24);
        --shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    a {{ color: inherit; }}
    .shell {{
      width: min(1400px, calc(100vw - 28px));
      margin: 0 auto;
      padding: 14px 0 42px;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 12px;
      padding: 10px 2px 8px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
      flex-wrap: wrap;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(1.45rem, 2vw, 2.1rem);
      line-height: 1;
      letter-spacing: 0;
    }}
    .stat-strip {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    .stat-card {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 32px;
      padding: 5px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel);
    }}
    .stat-card strong {{
      color: var(--text);
      font-weight: 800;
    }}
    .toolbar {{
      position: sticky;
      top: 8px;
      z-index: 20;
      margin-bottom: 10px;
    }}
    form.filters {{
      display: grid;
      grid-template-columns: minmax(260px, 1fr) auto auto;
      gap: 8px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.9);
      backdrop-filter: blur(12px) saturate(130%);
      box-shadow: var(--shadow);
    }}
    .primary-search {{
      min-width: 0;
    }}
    .search-ac-wrap {{
      position: relative;
    }}
    .search-ac-list {{
      position: absolute;
      top: calc(100% + 4px);
      left: 0;
      right: 0;
      z-index: 40;
      margin: 0;
      padding: 4px;
      list-style: none;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
      max-height: 280px;
      overflow-y: auto;
    }}
    .search-ac-item {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 9px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.9rem;
      color: var(--text);
    }}
    .search-ac-item.is-active,
    .search-ac-item:hover {{
      background: var(--line);
    }}
    .search-ac-icon {{
      flex: none;
      width: 16px;
      text-align: center;
      opacity: 0.7;
      font-size: 0.82rem;
    }}
    .search-ac-value {{
      flex: 1 1 auto;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .search-ac-count {{
      flex: none;
      color: var(--muted);
      font-size: 0.76rem;
    }}
    .advanced-filters {{
      grid-column: 1 / -1;
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }}
    .advanced-filters summary {{
      color: var(--muted);
      cursor: pointer;
      font-size: 0.84rem;
      font-weight: 800;
      list-style: none;
    }}
    .advanced-filters summary::-webkit-details-marker {{ display: none; }}
    .advanced-grid {{
      display: grid;
      grid-template-columns: repeat(6, minmax(128px, 1fr));
      gap: 8px;
      margin-top: 8px;
    }}
    .search-progress {{
      position: fixed;
      top: 14px;
      left: 50%;
      z-index: 120;
      width: min(520px, calc(100vw - 28px));
      transform: translateX(-50%);
      padding: 12px;
      border: 1px solid rgba(38, 115, 107, 0.24);
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.92);
      box-shadow: 0 18px 46px rgba(23, 32, 38, 0.16);
      backdrop-filter: blur(18px) saturate(145%);
    }}
    .search-progress[hidden] {{
      display: none;
    }}
    .search-progress-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 9px;
      color: #24343c;
      font-size: 0.88rem;
      font-weight: 800;
    }}
    .search-progress-row span:last-child {{
      color: var(--accent);
      font-size: 0.74rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }}
    .search-progress-track {{
      position: relative;
      height: 9px;
      overflow: hidden;
      border-radius: 999px;
      background: rgba(38, 115, 107, 0.12);
    }}
    .search-progress-fill {{
      position: absolute;
      inset: 0;
      width: 45%;
      border-radius: inherit;
      background:
        linear-gradient(90deg, rgba(38, 115, 107, 0), rgba(38, 115, 107, 0.95), rgba(72, 190, 170, 0.92), rgba(38, 115, 107, 0));
      animation: search-progress-slide 980ms cubic-bezier(.45, 0, .2, 1) infinite;
    }}
    @keyframes search-progress-slide {{
      0% {{ transform: translateX(-115%); }}
      100% {{ transform: translateX(235%); }}
    }}
    body.is-searching .button[type="submit"] {{
      opacity: 0.72;
      pointer-events: none;
    }}
    label {{
      display: grid;
      gap: 5px;
      font-size: 0.76rem;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
      color: var(--muted);
    }}
    input, select {{
      width: 100%;
      min-height: 42px;
      padding: 10px 12px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.9);
      color: var(--text);
      font: 500 0.95rem "Inter", "Helvetica Neue", sans-serif;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      align-items: end;
      flex-wrap: wrap;
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      padding: 9px 14px;
      border-radius: 8px;
      border: 1px solid transparent;
      background: var(--accent);
      color: white;
      font-weight: 600;
      text-decoration: none;
      cursor: pointer;
      box-shadow: none;
    }}
    .button.secondary {{
      border-color: var(--line-strong);
      background: var(--panel);
      color: var(--text);
      box-shadow: none;
    }}
    .meta-bar {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin: 6px 0 12px;
      color: var(--muted);
      font-size: 0.92rem;
      flex-wrap: wrap;
    }}
    .active-filters {{
      margin: 0 0 8px;
    }}
    .active-filters-list {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .filter-chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 8px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel);
      color: #31424c;
      font-size: 0.88rem;
    }}
    .filter-chip strong {{
      color: var(--accent-deep);
      font-weight: 700;
    }}
    .control-note {{
      margin: 0;
      color: var(--muted);
      font-size: 0.72rem;
      font-weight: 600;
      letter-spacing: 0;
      text-transform: none;
    }}
    .control-unavailable input {{
      background: rgba(23, 32, 38, 0.04);
      color: rgba(23, 32, 38, 0.45);
      cursor: not-allowed;
    }}
    .quick-searches {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      margin-top: 8px;
      padding: 0 2px;
    }}
    .quick-searches span {{
      color: var(--muted);
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
    }}
    .quick-chip {{
      padding: 6px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel);
      color: #2f3f48;
      font-size: 0.84rem;
      font-weight: 700;
      text-decoration: none;
    }}
    .quick-chip.active {{
      border-color: rgba(38, 115, 107, 0.36);
      background: var(--accent-soft);
      color: var(--accent-deep);
    }}
    .sort-toggle {{
      display: inline-flex;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      overflow: hidden;
      flex-shrink: 0;
    }}
    .sort-btn {{
      padding: 7px 14px;
      font-size: 0.85rem;
      font-weight: 600;
      color: var(--muted);
      background: var(--panel);
      text-decoration: none;
      white-space: nowrap;
      border: none;
    }}
    .sort-btn + .sort-btn {{
      border-left: 1px solid var(--line-strong);
    }}
    .sort-btn.active {{
      background: var(--accent);
      color: white;
    }}
    .meta-pillset {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .meta-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 8px 10px;
      border-radius: 999px;
      background: var(--panel);
      border: 1px solid var(--line);
    }}
    .meta-pill.analyzing {{
      color: var(--accent);
      border-color: var(--accent);
      font-weight: 500;
    }}
    .gallery {{
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 12px;
    }}
    .card {{
      display: flex;
      flex-direction: column;
      overflow: hidden;
      border-radius: 10px;
      background: var(--panel-strong);
      transition: transform 160ms ease;
      content-visibility: auto;
      contain-intrinsic-size: 240px;
    }}
    .card:hover {{
      transform: scale(1.015);
    }}
    .thumb {{
      position: relative;
      display: block;
      aspect-ratio: 1 / 1;
      background:
        linear-gradient(180deg, rgba(23,32,38,0.02), rgba(23,32,38,0.1)),
        linear-gradient(135deg, rgba(23,32,38,0.1), rgba(23,32,38,0.04));
      overflow: hidden;
      cursor: zoom-in;
    }}
    .thumb img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
      transform: scale(1.01);
    }}
    .thumb img.is-retrying {{
      opacity: 0.42;
    }}
    .thumb img.is-broken {{
      opacity: 0;
    }}
    .thumb-status {{
      position: absolute;
      right: 8px;
      bottom: 8px;
      left: 8px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 30px;
      padding: 6px 9px;
      color: var(--muted);
      font-size: 0.76rem;
      font-weight: 700;
      text-align: center;
      background: rgba(255, 255, 255, 0.84);
      border: 1px solid var(--line);
      border-radius: 999px;
      box-shadow: 0 4px 14px rgba(23, 32, 38, 0.08);
      pointer-events: none;
    }}
    .placeholder {{
      display: grid;
      place-items: center;
      height: 100%;
      padding: 20px;
      color: var(--muted);
      font-size: 0.95rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .body {{
      display: grid;
      gap: 8px;
      padding: 11px 12px 13px;
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: start;
    }}
    .filename {{
      margin: 0;
      font-size: 0.98rem;
      line-height: 1.2;
      letter-spacing: -0.02em;
      word-break: break-word;
    }}
    .kind {{
      white-space: nowrap;
      padding: 5px 8px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent-deep);
      font-size: 0.8rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    .summary, .detail, .tags {{
      margin: 0;
      color: var(--muted);
      font-size: 0.88rem;
      line-height: 1.42;
    }}
    .detail {{
      font-size: 0.8rem;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
    }}
    .summary {{
      color: #2e3c45;
    }}
    .tags {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .tag {{
      padding: 4px 8px;
      border-radius: 999px;
      background: var(--accent-soft);
      border: 1px solid var(--line);
      color: var(--accent-deep);
      font-weight: 700;
    }}
    .edit-panel {{
      border-top: 1px solid var(--line);
      padding-top: 8px;
    }}
    .edit-panel summary {{
      color: var(--accent-deep);
      cursor: pointer;
      font-size: 0.82rem;
      font-weight: 700;
    }}
    .edit-form {{
      display: grid;
      gap: 8px;
      margin-top: 8px;
    }}
    .edit-form input, .edit-form textarea {{
      width: 100%;
      min-height: 36px;
      padding: 8px 10px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.92);
      color: var(--text);
      font: 500 0.84rem "Inter", "Helvetica Neue", sans-serif;
    }}
    .edit-form textarea {{
      min-height: 66px;
      resize: vertical;
    }}
    .edit-form button {{
      justify-self: start;
      min-height: 34px;
      padding: 7px 12px;
      border: 0;
      border-radius: 8px;
      background: var(--text);
      color: white;
      font: 700 0.82rem "Inter", "Helvetica Neue", sans-serif;
      cursor: pointer;
    }}
    .pagination {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-top: 22px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .lightbox {{
      position: fixed;
      inset: 0;
      z-index: 100;
      display: none;
      place-items: center;
      padding: 24px;
      background: rgba(10, 15, 18, 0.78);
      backdrop-filter: blur(14px);
    }}
    .lightbox:target {{
      display: grid;
    }}
    .lightbox-backdrop {{
      position: absolute;
      inset: 0;
      cursor: zoom-out;
    }}
    .lightbox-panel {{
      position: relative;
      z-index: 1;
      display: grid;
      gap: 10px;
      max-width: min(92vw, 1120px);
      max-height: 92vh;
    }}
    .lightbox img {{
      display: block;
      max-width: 100%;
      max-height: calc(92vh - 58px);
      object-fit: contain;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.08);
      box-shadow: 0 24px 70px rgba(0, 0, 0, 0.35);
    }}
    .lightbox-caption {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      color: white;
      font-size: 0.9rem;
    }}
    .lightbox-row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
    }}
    .lightbox-title {{
      min-width: 0;
      flex: 1 1 auto;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .lightbox-path {{
      font-size: 0.74rem;
      color: rgba(255, 255, 255, 0.6);
      word-break: break-all;
    }}
    .lb-people {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
      margin: 6px 0;
    }}
    .lb-people-label {{ font-size: 0.74rem; color: rgba(255,255,255,0.55); margin-right: 2px; }}
    .lb-person {{
      display: inline-flex; align-items: center; gap: 4px;
      font-size: 0.8rem; color: #fff;
      background: rgba(255,255,255,0.14); border-radius: 999px; padding: 3px 6px 3px 10px;
    }}
    .lb-person-x {{
      border: none; background: rgba(255,255,255,0.2); color: #fff;
      width: 16px; height: 16px; line-height: 14px; border-radius: 50%;
      cursor: pointer; font-size: 0.8rem; padding: 0;
    }}
    .lb-person-x:hover {{ background: var(--accent); }}
    .lb-person-add {{ display: inline-flex; gap: 4px; }}
    .lb-person-input {{
      font-size: 0.8rem; color: #fff; background: rgba(255,255,255,0.1);
      border: 1px solid rgba(255,255,255,0.25); border-radius: 999px; padding: 3px 10px; width: 9rem;
    }}
    .lb-person-addbtn {{
      font-size: 0.78rem; color: #fff; background: rgba(255,255,255,0.18);
      border: none; border-radius: 999px; padding: 3px 10px; cursor: pointer;
    }}
    .lb-person-addbtn:hover {{ background: var(--accent); }}
    .lightbox-actions {{
      display: flex;
      gap: 8px;
      flex: 0 0 auto;
    }}
    .lightbox-close, .lightbox-download {{
      flex: 0 0 auto;
      padding: 8px 12px;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.14);
      color: white;
      text-decoration: none;
    }}
    .lightbox-download:hover, .lightbox-close:hover {{
      background: rgba(255, 255, 255, 0.26);
    }}
    .lightbox-download.disabled {{
      opacity: 0.4;
      cursor: not-allowed;
    }}
    .lightbox-meta {{
      width: min(900px, 92vw);
      margin-top: 10px;
      padding: 10px 14px;
      background: rgba(0, 0, 0, 0.55);
      color: rgba(255, 255, 255, 0.92);
      border-radius: 10px;
      font-size: .85rem;
    }}
    .lightbox-meta > summary {{
      cursor: pointer;
      list-style: none;
      font-weight: 700;
      letter-spacing: .02em;
    }}
    .lightbox-meta > summary::-webkit-details-marker {{ display: none; }}
    .lightbox-meta > summary::before {{
      content: "ⓘ 사진 정보";
      display: inline-block;
    }}
    .lightbox-meta[open] > summary::before {{ content: "▾ 사진 정보"; }}
    .lightbox-meta > summary {{ content-visibility: visible; }}
    .lightbox-meta .meta-grid {{
      display: grid;
      grid-template-columns: max-content 1fr;
      column-gap: 12px;
      row-gap: 6px;
      margin: 10px 0 0;
      padding: 0;
    }}
    .lightbox-meta dt {{
      color: rgba(255,255,255,0.72);
      font-weight: 600;
    }}
    .lightbox-meta dd {{
      margin: 0;
      word-break: break-all;
    }}
    @media (max-width: 1100px) {{
      form.filters {{ grid-template-columns: minmax(220px, 1fr) auto auto; }}
      .advanced-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    }}
    @media (max-width: 960px) {{
      .gallery {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    }}
    @media (max-width: 720px) {{
      .shell {{ width: min(100vw - 18px, 1360px); padding-top: 14px; }}
      .topbar {{ align-items: flex-start; flex-direction: column; }}
      .toolbar {{ top: 8px; }}
      form.filters {{ grid-template-columns: 1fr auto; padding: 12px; }}
      .advanced-grid {{ grid-template-columns: 1fr 1fr; }}
      .gallery {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
      .thumb {{ aspect-ratio: 1 / 1.15; }}
    }}
    @media (max-width: 540px) {{
      form.filters {{ grid-template-columns: 1fr; }}
      .advanced-grid {{ grid-template-columns: 1fr; }}
      .gallery {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 420px) {{
      .gallery {{ grid-template-columns: 1fr; }}
    }}
    .layout {{ display: flex; align-items: stretch; min-height: 100vh; }}
    .sidebar {{
      width: 224px; flex: 0 0 224px;
      padding: 18px 14px;
      border-right: 1px solid var(--line);
      background: var(--panel);
      display: flex; flex-direction: column; gap: 16px;
      position: sticky; top: 0; height: 100vh; overflow-y: auto;
    }}
    .side-brand {{ font-size: 1.15rem; font-weight: 600; padding: 2px 8px 4px; letter-spacing: -0.01em; }}
    .side-nav {{ display: flex; flex-direction: column; gap: 2px; }}
    .side-item {{ padding: 8px 10px; border-radius: 8px; color: var(--text); text-decoration: none; font-size: 0.92rem; }}
    .side-item:hover {{ background: var(--accent-soft); }}
    .side-item.active {{ background: var(--accent-soft); color: var(--accent); font-weight: 500; }}
    .side-label {{ font-size: 0.76rem; color: var(--muted); margin-top: 4px; }}
    .side-lang {{ display: flex; gap: 4px; margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--line); }}
    .lang-link {{ font-size: 0.78rem; color: var(--muted); text-decoration: none; padding: 3px 8px; border-radius: 6px; }}
    .lang-link:hover {{ background: var(--accent-soft); }}
    .lang-link.active {{ color: var(--accent); font-weight: 600; }}
    .content {{ flex: 1; min-width: 0; display: flex; flex-direction: column; padding: 18px 22px 0; }}
    /* 상단 검색바: 자유 검색 입력 + 필터 팝오버 토글 */
    .topbar-search {{ display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }}
    .topbar-search .search-ac-wrap {{ flex: 1 1 auto; min-width: 0; }}
    .topbar-search input[type="search"] {{
      width: 100%; padding: 9px 12px; border: 1px solid var(--line); border-radius: 9px;
      background: var(--panel-strong); color: var(--text); font-size: 0.95rem;
    }}
    .filter-pop-wrap {{ position: relative; flex: none; }}
    .filter-toggle {{ white-space: nowrap; }}
    .filter-toggle.has-active {{ border-color: var(--accent); color: var(--accent); }}
    .filter-pop {{
      position: absolute; top: calc(100% + 6px); right: 0; z-index: 45;
      display: flex; flex-direction: column; gap: 8px;
      min-width: 220px; padding: 14px;
      background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.32);
    }}
    .filter-pop[hidden] {{ display: none; }}
    .filter-pop input, .filter-pop select {{
      width: 100%; padding: 7px 9px; border: 1px solid var(--line); border-radius: 8px;
      background: var(--panel-strong); color: var(--text); font-size: 0.88rem;
    }}
    .filter-pop-actions {{ display: flex; gap: 6px; margin-top: 6px; }}
    .filter-pop-actions .button {{ flex: 1; text-align: center; }}
    .content-toolbar {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }}
    .statusbar {{
      position: sticky; bottom: 0;
      display: flex; align-items: center; gap: 16px;
      padding: 9px 22px;
      border-top: 1px solid var(--line);
      background: var(--panel);
      font-size: 0.82rem; color: var(--muted);
    }}
    .statusbar .cache-note {{ color: var(--accent); font-weight: 500; }}
    .statusbar .pager {{ margin-left: auto; display: flex; gap: 8px; }}
    .backend-offline {{
      position: fixed; inset: 0; z-index: 9999;
      display: flex; align-items: center; justify-content: center;
      background: rgba(12, 14, 18, 0.82); backdrop-filter: blur(3px);
    }}
    .backend-offline[hidden] {{ display: none; }}
    .backend-offline .panel {{
      background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
      padding: 28px 34px; max-width: 420px; text-align: center;
      box-shadow: 0 18px 50px rgba(0,0,0,.35);
    }}
    .backend-offline h2 {{ margin: 0 0 10px; font-size: 1.05rem; }}
    .backend-offline p {{ margin: 0; color: var(--muted); font-size: 0.9rem; line-height: 1.55; }}
    @media (max-width: 720px) {{
      .layout {{ flex-direction: column; }}
      .sidebar {{ width: auto; flex: none; height: auto; position: static; border-right: none; border-bottom: 1px solid var(--line); }}
      .filter-pop {{ left: 0; right: auto; }}
    }}
  </style>
</head>
<body>
  <div id="search-progress" class="search-progress" aria-live="polite" aria-atomic="true" hidden>
    <div class="search-progress-row">
      <span>{_("gallery.searching")}</span>
      <span>{_("gallery.searching_short")}</span>
    </div>
    <div class="search-progress-track" aria-hidden="true">
      <div class="search-progress-fill"></div>
    </div>
  </div>
  <main class="layout">
    <aside class="sidebar">
      <div class="side-brand">Trove</div>
      <nav class="side-nav">
        <a class="side-item active" href="/gallery">{_("nav.all_photos")}</a>
        <a class="side-item" href="/people/manage">{_("nav.people")}</a>
        <!-- 설정(/dashboard)은 사이드바에서 숨기고 메뉴바 "설정 열기"로만 진입.
             페이지 자체는 오류 재처리·리소스 설정 등 관리 기능 때문에 유지한다. -->
      </nav>
      <div class="side-lang">{render_lang_switcher(locale, request)}</div>
    </aside>
    <section class="content">
      <form class="topbar-search" id="gallery-search-form" method="get" action="/gallery">
        <div class="search-ac-wrap">
          <input type="search" id="gallery-search-input" name="q" value="{escape(q or '')}" placeholder="{_('gallery.search_placeholder')}" autocomplete="off" role="combobox" aria-expanded="false" aria-autocomplete="list" aria-controls="search-ac-list">
          <ul id="search-ac-list" class="search-ac-list" role="listbox" hidden></ul>
        </div>
        <div class="filter-pop-wrap">
          <button type="button" id="filter-toggle" class="button secondary filter-toggle{' has-active' if filter_active_count else ''}" aria-expanded="false" aria-controls="filter-pop" aria-haspopup="true">{_("gallery.filter")}{f' · {filter_active_count}' if filter_active_count else ''} ▾</button>
          <div id="filter-pop" class="filter-pop" hidden>
            <div class="side-label">{_("gallery.period")}</div>
            <input type="date" name="date_from" value="{escape(date_from or '')}" aria-label="{_('gallery.start_date')}">
            <input type="date" name="date_to" value="{escape(date_to or '')}" aria-label="{_('gallery.end_date')}">
            <div class="side-label">{_("gallery.person")}</div>
            <select name="person"{" disabled" if not person_available else ""} aria-label="{_('gallery.person')}">
              {_render_person_options(person_options, person, _)}
            </select>
            <div class="filter-pop-actions">
              <button id="gallery-search-button" class="button" type="submit">{_("gallery.apply")}</button>
              <a class="button secondary" href="/gallery">{_("gallery.reset")}</a>
            </div>
          </div>
        </div>
        <input type="hidden" name="sort" value="{escape(sort_order)}">
      </form>
      <div class="content-toolbar">
        <div class="meta-pillset">
          <span class="meta-pill">{_("gallery.count_photos", count=total)}{_render_filter_hint(person, place, _)}</span>
          {_render_search_mode_pill(search_meta, _)}
          <span class="meta-pill">{_("gallery.page_of", page=page, total=page_count)}</span>
          {analyzing_notice}
        </div>
        <div style="display:flex;align-items:center;gap:10px;">
          <select aria-label="{_('gallery.per_page_label')}" onchange="if(this.value)location.href=this.value;" style="font-size:0.85rem;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:7px;padding:4px 8px;cursor:pointer;">{per_page_options}</select>
          <div class="sort-toggle">
            <a href="{escape(_sort_url(request, SORT_NEWEST))}" class="sort-btn{'  active' if sort_order == SORT_NEWEST else ''}">{_("gallery.sort_newest")}</a>
            <a href="{escape(_sort_url(request, SORT_OLDEST))}" class="sort-btn{'  active' if sort_order == SORT_OLDEST else ''}">{_("gallery.sort_oldest")}</a>
          </div>
        </div>
      </div>
      <section id="gallery" class="gallery">
        {''.join(cards) if cards else _render_empty_state(q, person, place, _, locale)}
      </section>
    </section>
  </main>
  <datalist id="lb-person-dl">{_render_datalist_options(person_options)}</datalist>
  <footer class="statusbar">
    <span>{_("gallery.count_photos", count=total)}</span>
    <span>{_("gallery.range", start=offset + 1, end=offset + len(items)) if items else _("gallery.empty")}</span>
    <span class="cache-note">{_("gallery.cache_note")}</span>
    <span class="pager">
      {_render_page_link(_("gallery.prev"), _page_url(request, page - 1), enabled=has_prev)}
      {_render_page_link(_("gallery.next"), _page_url(request, page + 1), enabled=has_next)}
    </span>
  </footer>
  <div id="backend-offline" class="backend-offline" role="alert" hidden>
    <div class="panel">
      <h2>{_("gallery.offline_title")}</h2>
      <p>{_("gallery.offline_body")}</p>
    </div>
  </div>
  <script>
    // 서버에서 현재 로케일로 미리 번역해 넣은 JS용 문자열.
    const T = {{
      searching: {_json('gallery.searching')},
      loadingResults: {_json('gallery.loading_results')},
      sorting: {_json('gallery.sorting')},
      applying: {_json('gallery.applying')},
      apply: {_json('gallery.apply')},
      previewPending: {_json('gallery.preview_pending')},
    }};
    const searchProgress = document.getElementById("search-progress");
    const searchForm = document.getElementById("gallery-search-form");
    const searchButton = document.getElementById("gallery-search-button");

    function showSearchProgress(label) {{
      if (!searchProgress) return;
      const title = searchProgress.querySelector(".search-progress-row span:first-child");
      if (title && label) title.textContent = label;
      searchProgress.hidden = false;
      document.body.classList.add("is-searching");
      if (searchButton) {{
        searchButton.disabled = true;
        searchButton.textContent = T.applying;
      }}
    }}

    // 필터 팝오버: 토글 + 바깥 클릭 시 닫기
    (function setupFilterPopover() {{
      const toggle = document.getElementById("filter-toggle");
      const pop = document.getElementById("filter-pop");
      if (!toggle || !pop) return;
      function setOpen(open) {{
        pop.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
      }}
      toggle.addEventListener("click", (e) => {{
        e.stopPropagation();
        setOpen(pop.hidden);
      }});
      document.addEventListener("click", (e) => {{
        if (!pop.hidden && !e.target.closest(".filter-pop-wrap")) setOpen(false);
      }});
      document.addEventListener("keydown", (e) => {{
        if (e.key === "Escape" && !pop.hidden) setOpen(false);
      }});
    }})();

    // 라이트박스 스크롤 유지: 썸네일을 #preview-X 앵커로 열면 히스토리 항목이
    // 쌓인다. 닫기 버튼/배경은 원래 #gallery로 점프해 페이지가 최상단으로 튀었다.
    // 대신 history.back()으로 돌아가면 브라우저가 직전 스크롤 위치를 그대로
    // 복원해줘 보던 자리를 유지한다(깜빡임 없음). :target CSS는 그대로 둔다.
    (function setupLightboxScroll() {{
      let pushedByThumb = false;
      document.querySelectorAll(".thumb").forEach((link) => {{
        link.addEventListener("click", () => {{ pushedByThumb = true; }});
      }});
      window.addEventListener("hashchange", () => {{
        if (!location.hash.startsWith("#preview-")) pushedByThumb = false;
      }});
      function closeLightbox(e) {{
        // 직접 URL로 진입한 경우 등 우리가 연 게 아니면 기본 동작(#gallery)에 맡긴다.
        if (!pushedByThumb) return;
        e.preventDefault();
        pushedByThumb = false;
        history.back();
      }}
      document.querySelectorAll(".lightbox-close, .lightbox-backdrop").forEach((link) => {{
        link.addEventListener("click", closeLightbox);
      }});
      document.addEventListener("keydown", (e) => {{
        if (e.key === "Escape" && location.hash.startsWith("#preview-") && pushedByThumb) {{
          pushedByThumb = false;
          history.back();
        }}
      }});
    }})();

    if (searchForm) {{
      searchForm.addEventListener("submit", () => showSearchProgress(T.searching));
    }}

    document.querySelectorAll(".quick-chip, .pagination a").forEach((link) => {{
      link.addEventListener("click", () => showSearchProgress(T.loadingResults));
    }});

    document.querySelectorAll(".sort-btn").forEach((link) => {{
      link.addEventListener("click", () => showSearchProgress(T.sorting));
    }});

    function attachThumbnailRecovery() {{
      document.querySelectorAll(".thumb img").forEach((image) => {{
        if (image.dataset.recoveryReady === "1") return;
        image.dataset.recoveryReady = "1";
        image.dataset.retryCount = image.dataset.retryCount || "0";
        image.addEventListener("error", () => {{
          const retries = Number(image.dataset.retryCount || "0");
          if (retries < 3) {{
            image.dataset.retryCount = String(retries + 1);
            image.classList.add("is-retrying");
            const source = new URL(image.currentSrc || image.src, window.location.href);
            source.searchParams.set("retry", String(Date.now()));
            window.setTimeout(() => {{
              image.src = source.toString();
            }}, 350 + retries * 650);
            return;
          }}
          image.classList.remove("is-retrying");
          image.classList.add("is-broken");
          const thumb = image.closest(".thumb");
          if (thumb && !thumb.querySelector(".thumb-status")) {{
            const status = document.createElement("span");
            status.className = "thumb-status";
            status.textContent = T.previewPending;
            thumb.appendChild(status);
          }}
        }});
        image.addEventListener("load", () => {{
          image.classList.remove("is-retrying", "is-broken");
          const thumb = image.closest(".thumb");
          const status = thumb ? thumb.querySelector(".thumb-status") : null;
          if (status) status.remove();
        }});
      }});
    }}

    attachThumbnailRecovery();

    window.addEventListener("pageshow", () => {{
      if (searchProgress) searchProgress.hidden = true;
      document.body.classList.remove("is-searching");
      if (searchButton) {{
        searchButton.disabled = false;
        searchButton.textContent = T.apply;
      }}
      attachThumbnailRecovery();
    }});
    (function setupAutocomplete() {{
      const input = document.getElementById("gallery-search-input");
      const list = document.getElementById("search-ac-list");
      if (!input || !list) return;
      let items = [];
      let activeIndex = -1;
      let debounceTimer = null;
      let lastQuery = "";

      function closeList() {{
        list.hidden = true;
        list.innerHTML = "";
        items = [];
        activeIndex = -1;
        input.setAttribute("aria-expanded", "false");
      }}

      function escapeHtml(value) {{
        return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      }}

      function render(suggestions) {{
        items = suggestions;
        activeIndex = -1;
        if (!suggestions.length) {{ closeList(); return; }}
        list.innerHTML = suggestions.map((s, i) => {{
          const icon = s.kind === "recent" ? "🕘" : "🏷";
          const count = (s.count !== null && s.count !== undefined)
            ? `<span class="search-ac-count">${{s.count}}</span>` : "";
          return `<li class="search-ac-item" role="option" data-index="${{i}}">`
            + `<span class="search-ac-icon">${{icon}}</span>`
            + `<span class="search-ac-value">${{escapeHtml(s.value)}}</span>${{count}}</li>`;
        }}).join("");
        list.hidden = false;
        input.setAttribute("aria-expanded", "true");
      }}

      function setActive(idx) {{
        const nodes = list.querySelectorAll(".search-ac-item");
        nodes.forEach(n => n.classList.remove("is-active"));
        if (idx >= 0 && idx < nodes.length) {{
          nodes[idx].classList.add("is-active");
          nodes[idx].scrollIntoView({{ block: "nearest" }});
        }}
        activeIndex = idx;
      }}

      function choose(idx) {{
        if (idx < 0 || idx >= items.length) return;
        input.value = items[idx].value;
        closeList();
        if (searchForm) {{
          if (searchForm.requestSubmit) searchForm.requestSubmit();
          else searchForm.submit();
        }}
      }}

      async function fetchSuggestions(q) {{
        try {{
          const res = await fetch(`/search/suggest?q=${{encodeURIComponent(q)}}&limit=8`, {{ cache: "no-store" }});
          if (!res.ok) return;
          const data = await res.json();
          if (input.value.trim() !== q) return;  // stale response
          render(data.suggestions || []);
        }} catch (_e) {{ /* ignore transient errors */ }}
      }}

      input.addEventListener("input", () => {{
        const q = input.value.trim();
        if (debounceTimer) clearTimeout(debounceTimer);
        if (q.length < 1) {{ lastQuery = ""; closeList(); return; }}
        if (q === lastQuery) return;
        lastQuery = q;
        debounceTimer = setTimeout(() => fetchSuggestions(q), 140);
      }});

      input.addEventListener("keydown", (e) => {{
        if (list.hidden) return;
        if (e.key === "ArrowDown") {{ e.preventDefault(); setActive(Math.min(activeIndex + 1, items.length - 1)); }}
        else if (e.key === "ArrowUp") {{ e.preventDefault(); setActive(Math.max(activeIndex - 1, 0)); }}
        else if (e.key === "Enter") {{ if (activeIndex >= 0) {{ e.preventDefault(); choose(activeIndex); }} }}
        else if (e.key === "Escape") {{ closeList(); }}
      }});

      list.addEventListener("mousedown", (e) => {{
        const li = e.target.closest(".search-ac-item");
        if (!li) return;
        e.preventDefault();
        choose(Number(li.dataset.index));
      }});

      document.addEventListener("click", (e) => {{
        if (!list.hidden && !e.target.closest(".search-ac-wrap")) closeList();
      }});
    }})();

    // Trove 종료 감지 하트비트. 브라우저 탭은 앱이 닫아줄 수 없으므로
    // (로컬서버+브라우저 UI 앱 공통), 백엔드가 죽으면 깨진 화면 대신 안내
    // 오버레이를 띄우고 백엔드가 돌아오면 자동으로 새로고침해 복귀한다.
    (function backendHeartbeat() {{
      const overlay = document.getElementById("backend-offline");
      if (!overlay) return;
      let failures = 0;
      let wasOffline = false;
      async function beat() {{
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 4000);
        try {{
          const res = await fetch("/healthz", {{ cache: "no-store", signal: controller.signal }});
          if (!res.ok) throw new Error(String(res.status));
          failures = 0;
          if (wasOffline) {{ location.reload(); return; }}
          overlay.hidden = true;
        }} catch (_err) {{
          failures += 1;
          // 3회 연속 실패(약 15초)에만 발동 — 일시적 지연으로 인한
          // 오탐과 그로 인한 원치 않는 자동 새로고침을 막는다.
          if (failures >= 3) {{
            overlay.hidden = false;
            wasOffline = true;
          }}
        }} finally {{
          clearTimeout(timer);
        }}
      }}
      setInterval(beat, 5000);
    }})();

    // 수동 인물 태깅: 라이트박스에서 사진에 인물을 더하거나 뺀다(P2). 성공 시
    // 새 태그/검색 상태를 반영하려 페이지를 다시 읽는다.
    async function tvAddPerson(fileId, inputEl) {{
      var name = ((inputEl && inputEl.value) || "").trim();
      if (!name) return;
      try {{
        var r = await fetch("/people/photo-assign", {{
          method: "POST", headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ file_id: fileId, name: name }})
        }});
        if (r.ok) location.reload();
      }} catch (e) {{}}
    }}
    async function tvRmPerson(fileId, personId, btn) {{
      try {{
        var r = await fetch("/people/photo-assign?file_id=" + encodeURIComponent(fileId) + "&person_id=" + personId, {{ method: "DELETE" }});
        if (r.ok) location.reload();
      }} catch (e) {{}}
    }}
    // B: 라이트박스 이미지 위에 얼굴 영역을 드래그 → 그 크롭으로 학습(검출 성공 시).
    function tvFaceBox(fileId, btn) {{
      var panel = btn.closest(".lightbox-panel");
      var img = panel && panel.querySelector("img");
      if (!img) return;
      var input = panel.querySelector(".lb-person-input");
      btn.textContent = "영역을 드래그";
      img.style.cursor = "crosshair";
      var box = null, sx = 0, sy = 0;
      function move(e) {{
        if (!box) return;
        box.style.left = Math.min(e.clientX, sx) + "px";
        box.style.top = Math.min(e.clientY, sy) + "px";
        box.style.width = Math.abs(e.clientX - sx) + "px";
        box.style.height = Math.abs(e.clientY - sy) + "px";
      }}
      async function up(e) {{
        window.removeEventListener("pointermove", move);
        var r = img.getBoundingClientRect();
        var x0 = Math.min(e.clientX, sx), y0 = Math.min(e.clientY, sy);
        var w = Math.abs(e.clientX - sx), h = Math.abs(e.clientY - sy);
        if (box) {{ box.remove(); box = null; }}
        img.style.cursor = ""; btn.textContent = "얼굴 지정";
        if (w < 8 || h < 8) return;
        var nb = {{ x: (x0 - r.left) / r.width, y: (y0 - r.top) / r.height, width: w / r.width, height: h / r.height }};
        var name = (((input && input.value) || "").trim()) || ((window.prompt("이 얼굴의 이름", "") || "").trim());
        if (!name) return;
        try {{
          var res = await fetch("/people/photo-assign", {{
            method: "POST", headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ file_id: fileId, name: name, bbox: nb }})
          }});
          if (res.ok) location.reload();
        }} catch (e) {{}}
      }}
      function down(e) {{
        e.preventDefault();
        sx = e.clientX; sy = e.clientY;
        box = document.createElement("div");
        box.style.cssText = "position:fixed;border:2px solid #fff;background:rgba(255,255,255,0.18);z-index:9999;pointer-events:none;left:" + sx + "px;top:" + sy + "px;";
        document.body.appendChild(box);
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up, {{ once: true }});
      }}
      img.addEventListener("pointerdown", down, {{ once: true }});
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(html)


@router.get("/media/{file_id}/download")
def media_download(request: Request, file_id: str) -> Response:
    database = require_state(request, "database")
    with database.session_factory() as session:
        media_file = session.get(MediaFile, file_id)
        if media_file is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    path = Path(media_file.current_path)
    if not path.is_file():
        # 폴더 전환·이동으로 원본 경로가 해제된 사진 — 깨진 다운로드 대신 안내
        locale, _ = request_translator(request)
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html lang="{locale}"><head><meta charset="utf-8"><title>{_('gallery.download_unavailable_title')} · Trove</title>
<style>body{{font-family:-apple-system,sans-serif;background:#0c0e12;color:#e8eaed;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0}}
.panel{{max-width:440px;padding:32px;text-align:center;background:#16181d;border:1px solid #2a2d34;border-radius:14px}}
h1{{font-size:1.05rem;margin:0 0 10px}}p{{color:#9aa0a6;font-size:.9rem;line-height:1.6;margin:0}}
code{{font-size:.78rem;color:#7d8590;word-break:break-all}}</style></head>
<body><div class="panel">
<h1>{_('gallery.download_unavailable_title')}</h1>
<p>{_('gallery.download_unavailable_body')}<br><br>
<code>{escape(media_file.current_path)}</code></p>
</div></body></html>""",
            status_code=status.HTTP_410_GONE,
        )
    return FileResponse(
        path,
        filename=media_file.filename,
        headers={"Content-Disposition": f'attachment; filename="{media_file.filename}"'},
    )


@router.get("/gallery/assets/{asset_id}")
def gallery_asset(request: Request, asset_id: int) -> FileResponse:
    database = require_state(request, "database")
    settings = require_state(request, "settings")
    with database.session_factory() as session:
        asset = session.get(DerivedAsset, asset_id)
        if asset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="derived asset not found",
                headers={"Cache-Control": "no-store"},
            )

    path = _resolve_asset_path(settings.derived_root, asset.derived_path)
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="derived asset file missing",
            headers={"Cache-Control": "no-store"},
        )
    return FileResponse(
        path,
        media_type=asset.content_type or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _build_gallery_ids_query(
    *,
    media_type: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    person: str | None,
    place: str | None,
    query: str | None,
    file_ids: list[str] | None = None,
    sort: str = SORT_NEWEST,
    require_analysis_complete: bool = False,
) -> Select:
    statement = select(MediaFile.file_id).where(
        MediaFile.status.not_in(("missing", "replaced", "excluded"))
    )
    if file_ids is not None:
        if not file_ids:
            return statement.where(false())
        statement = statement.where(MediaFile.file_id.in_(file_ids))
    elif require_analysis_complete:
        # 가시성 계약: 브라우즈(검색 외)에서는 분석이 끝난 사진만 노출한다.
        # search_document은 파일별 분석 패스의 마지막 단계에서 기록되므로, 그 존재가
        # 곧 "분석 완료 + 검색가능"을 뜻한다 → "보이는데 검색 안 됨"을 차단.
        # 검색 결과(file_ids 지정)는 이미 search_document에서 나오므로 게이트 불필요.
        statement = statement.where(
            exists().where(SearchDocument.file_id == MediaFile.file_id)
        )

    if media_type:
        statement = statement.where(MediaFile.media_kind == media_type)
    if query:
        like_query = f"%{query}%"
        statement = statement.where(
            or_(
                MediaFile.current_path.ilike(like_query),
                MediaFile.relative_path.ilike(like_query),
                MediaFile.filename.ilike(like_query),
                MediaFile.file_id.ilike(like_query),
            )
        )

    captured_at_expr = _captured_at_expr()
    if date_from is not None:
        statement = statement.where(captured_at_expr >= date_from)
    if date_to is not None:
        statement = statement.where(captured_at_expr <= date_to)
    if person:
        statement = statement.where(_tag_exists_clause(person, PERSON_TAG_TYPES))
    if place:
        statement = statement.where(_tag_exists_clause(place, PLACE_TAG_TYPES))

    if sort == SORT_OLDEST:
        return statement.order_by(captured_at_expr.asc(), MediaFile.file_id.asc())
    return statement.order_by(captured_at_expr.desc(), MediaFile.file_id.desc())


def _tag_exists_clause(tag_value: str, tag_types: tuple[str, ...]):
    normalized = tag_value.strip()
    return exists(
        select(Tag.id).where(
            Tag.file_id == MediaFile.file_id,
            Tag.tag_type.in_(tag_types),
            func.lower(Tag.tag_value) == normalized.lower(),
        )
    )


def _captured_at_expr():
    mtime_expr = func.datetime(MediaFile.mtime_ns / 1000000000, "unixepoch", "localtime")
    return func.coalesce(MediaFile.exif_datetime, mtime_expr, MediaFile.processed_at, MediaFile.last_seen_at)


def _list_tag_values(session, tag_types: tuple[str, ...]) -> list[str]:
    statement = (
        select(Tag.tag_value)
        .where(Tag.tag_type.in_(tag_types))
        .group_by(Tag.tag_value)
        .order_by(func.lower(Tag.tag_value).asc())
        .limit(200)
    )
    return [value for value in session.scalars(statement) if not _is_coordinate_tag(str(value))]


def _list_named_person_display_names(session) -> list[str]:
    """인물 필터 콤보 옵션. 대표 이름(Person.display_name)만 노출한다 —
    애칭(alias)과 person-000123 같은 내부 자동 ID는 제외된다. 실제 인물
    태그가 달린(=사진이 있는) 사람만 보이게 태그 존재를 함께 확인한다.
    애칭도 같은 'person' 태그로 저장되므로, 태그값이 아니라 Person 테이블의
    대표 이름을 직접 source로 삼아야 애칭이 섞이지 않는다."""
    statement = (
        select(Person.display_name)
        .where(Person.display_name.not_like("person-%"))
        .where(
            Person.display_name.in_(
                select(Tag.tag_value).where(Tag.tag_type.in_(PERSON_TAG_TYPES))
            )
        )
        .distinct()
        .order_by(func.lower(Person.display_name).asc())
        .limit(200)
    )
    return list(session.scalars(statement))


def _is_coordinate_tag(value: str) -> bool:
    return bool(re.match(r"^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$", value.strip()))


def _select_card_asset(assets: list[DerivedAsset]) -> DerivedAsset | None:
    preferred_order = {"thumb": 0, "keyframe": 1, "preview": 2}
    if not assets:
        return None
    return min(
        assets,
        key=lambda asset: (preferred_order.get(asset.asset_kind, 99), asset.id),
    )


def _render_card(
    *,
    media_file: MediaFile,
    asset: DerivedAsset | None,
    tags: list[Tag],
    annotation: MediaAnnotation | None,
    index: int,
    next_url: str,
    original_offline: bool = False,
    people: list[tuple[int, str]] | None = None,
    person_options: list[str] | None = None,
) -> str:
    eager = index < 6
    loading_attr = "eager" if eager else "lazy"
    fetchpriority_attr = "high" if index < 4 else "auto"
    image_html = (
        f'<img src="/gallery/assets/{asset.id}" alt="{escape(media_file.filename)}" '
        f'loading="{loading_attr}" decoding="async" fetchpriority="{fetchpriority_attr}" '
        f'sizes="(max-width: 420px) 100vw, (max-width: 720px) 50vw, (max-width: 1100px) 33vw, 24vw" '
        f'width="{media_file.width or 512}" height="{media_file.height or 640}">'
        if asset is not None
        else f'<div class="placeholder">{escape(_media_kind_label(media_file.media_kind))}</div>'
    )
    title = _display_title(media_file, annotation)
    preview_id = f"preview-{asset.id}" if asset is not None else ""
    thumb_href = f"#{preview_id}" if asset is not None else "#gallery"
    # 원본 저장소가 분리돼 있으면 다운로드 버튼을 비활성화한다(뱃지 없이 disabled
    # 상태로만 표시). 연결돼 있으면 정상 링크 — 클릭 시 원본이 그새 빠졌으면
    # 다운로드 엔드포인트가 "원본 다운로드 불가" 안내를 띄운다.
    download_html = (
        '<span class="lightbox-download disabled" aria-disabled="true" '
        'title="원본 미연결 — 저장소를 연결하세요">↓ 원본</span>'
        if original_offline
        else f'<a class="lightbox-download" href="/media/{escape(media_file.file_id)}/download" download="{escape(media_file.filename)}" title="원본 다운로드">↓ 원본</a>'
    )
    fid_js = media_file.file_id
    chips = "".join(
        f'<span class="lb-person">{escape(name)}'
        f'<button type="button" class="lb-person-x" title="제거" '
        f"onclick=\"tvRmPerson('{fid_js}',{pid},this)\">×</button></span>"
        for pid, name in (people or [])
    )
    people_html = (
        f'<div class="lb-people">'
        f'<span class="lb-people-label">인물</span>{chips}'
        f'<span class="lb-person-add">'
        f'<input type="text" class="lb-person-input" list="lb-person-dl" placeholder="인물 추가" '
        f'aria-label="인물 추가" '
        f"onkeydown=\"if(event.key==='Enter'){{event.preventDefault();tvAddPerson('{fid_js}',this);}}\">"
        f"<button type=\"button\" class=\"lb-person-addbtn\" onclick=\"tvAddPerson('{fid_js}',this.previousElementSibling)\">추가</button>"
        f"<button type=\"button\" class=\"lb-person-addbtn lb-facebox\" title=\"이름 입력 후 얼굴 영역을 드래그하면 그 얼굴로 학습합니다\" onclick=\"tvFaceBox('{fid_js}',this)\">얼굴 지정</button>"
        f"</span></div>"
    )
    lightbox_html = (
        f"""
      <div id="{preview_id}" class="lightbox" aria-label="{escape(title)} 미리보기">
        <a class="lightbox-backdrop" href="#gallery" aria-label="미리보기 닫기"></a>
        <div class="lightbox-panel">
          <img src="/gallery/assets/{asset.id}" alt="{escape(title)} 크게 보기">
          <div class="lightbox-caption">
            <div class="lightbox-row">
              <span class="lightbox-title">{escape(title)}</span>
              <div class="lightbox-actions">
                {download_html}
                <a class="lightbox-close" href="#gallery">닫기</a>
              </div>
            </div>
            {people_html}
            <span class="lightbox-path" title="{escape(media_file.current_path)}">{escape(media_file.current_path)}</span>
          </div>
        </div>
      </div>
        """
        if asset is not None
        else ""
    )
    return f"""
      <article id="card-{escape(media_file.file_id)}" class="card">
        <a class="thumb" href="{thumb_href}" aria-label="{escape(title)} 크게 보기">{image_html}</a>
      </article>
      {lightbox_html}
    """


def _display_title(media_file: MediaFile, annotation: MediaAnnotation | None) -> str:
    if annotation and annotation.title:
        return annotation.title
    return media_file.filename


def _media_kind_label(value: str | None) -> str:
    labels = {
        "image": "사진",
    }
    return labels.get(value or "", value or "미디어")


def _render_media_type_options(selected: str | None) -> str:
    options = [("", "전체"), ("image", "사진")]
    rendered: list[str] = []
    for value, label in options:
        is_selected = ' selected' if selected == value or (selected is None and value == "") else ""
        rendered.append(f'<option value="{escape(value)}"{is_selected}>{escape(label)}</option>')
    return "".join(rendered)


def _normalize_sort(value: str | None) -> str:
    normalized = (value or SORT_NEWEST).strip().casefold()
    return normalized if normalized in SORT_OPTIONS else SORT_NEWEST


def _render_sort_options(selected: str) -> str:
    labels = {
        SORT_NEWEST: "최신순",
        SORT_OLDEST: "오래된순",
    }
    return "".join(
        f'<option value="{escape(value)}"{" selected" if selected == value else ""}>{escape(label)}</option>'
        for value, label in labels.items()
    )


def _sort_label(sort: str) -> str:
    return "오래된순" if sort == SORT_OLDEST else "최신순"


def _render_datalist_options(values: list[str]) -> str:
    return "".join(f'<option value="{escape(value)}"></option>' for value in values)


# 빈 검색 결과에 보여줄 예시 검색어(칩). 검색 렉시콘은 한/영 별명을 모두
# 매핑하므로 로케일에 맞는 단어를 보여주되, 어느 쪽이든 검색은 동작한다.
_EMPTY_EXAMPLE_TERMS = {
    "ko": ["바다", "산", "케이크", "카페", "벚꽃", "단풍", "음식", "서울"],
    "en": ["beach", "mountain", "cake", "cafe", "cherry blossom", "autumn leaves", "food", "Seoul"],
}


def _render_empty_state(q: str | None, person: str | None, place: str | None, _, locale: str = "ko") -> str:
    if q or person or place:
        message = _("gallery.empty_no_results", query=q or person or place)
        terms = _EMPTY_EXAMPLE_TERMS.get(locale, _EMPTY_EXAMPLE_TERMS["ko"])
        hint = _("gallery.empty_hint", examples=", ".join(terms[:6]))
        chips = "".join(
            f'<a class="quick-chip" href="/gallery?q={escape(quote(term), quote=True)}">{escape(term)}</a>'
            for term in terms
        )
        examples_html = f"""
          <p class="detail" style="margin-top:8px">{_("gallery.empty_note")}</p>
          <div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:6px">{chips}</div>
        """
    else:
        message = _("gallery.empty_none")
        hint = _("gallery.empty_none_hint")
        examples_html = ""
    return f"""
      <article class="card">
        <div class="body">
          <p class="summary">{escape(message)}</p>
          <p class="detail">{hint}</p>
          {examples_html}
        </div>
      </article>
    """


def _render_quick_searches(request: Request, active_query: str | None) -> str:
    active = (active_query or "").strip().casefold()
    return "".join(
        f'<a class="quick-chip{" active" if term.casefold() == active else ""}" href="{escape(_quick_search_url(request, term))}">{escape(term)}</a>'
        for term in QUICK_SEARCH_TERMS
    )


def _sort_url(request: Request, sort_value: str) -> str:
    params = dict(request.query_params)
    params["sort"] = sort_value
    params.pop("page", None)
    return f"/gallery?{urlencode(params)}"


def _quick_search_url(request: Request, term: str) -> str:
    params = dict(request.query_params)
    params["q"] = term
    params.pop("page", None)
    return f"/gallery?{urlencode(params)}"


def _render_filter_hint(person: str | None, place: str | None, _) -> str:
    hints: list[str] = []
    if person:
        hints.append(_("gallery.hint_person", value=escape(person)))
    if place:
        hints.append(_("gallery.hint_place", value=escape(place)))
    if not hints:
        return ""
    return " · " + ", ".join(hints)


def _render_active_filter_summary(
    *,
    q: str | None,
    search_meta: dict[str, str] | None = None,
    media_type: str | None,
    date_from: str | None,
    date_to: str | None,
    person: str | None,
    place: str | None,
    sort: str,
) -> str:
    filters: list[tuple[str, str]] = []
    if q:
        filters.append(("검색어", q))
        if search_meta:
            filters.append(("검색 방식", _friendly_intent_label(search_meta)))
    if media_type:
        filters.append(("종류", _media_kind_label(media_type)))
    if date_from:
        filters.append(("시작일", date_from))
    if date_to:
        filters.append(("종료일", date_to))
    if person:
        filters.append(("인물", person))
    if place:
        filters.append(("장소", place))
    # sort is now shown as toggle buttons in the meta-bar, not as a filter pill

    if not filters:
        return '<span class="filter-chip"><strong>적용된 조건</strong> 없음</span>'

    return "".join(
        f'<span class="filter-chip"><strong>{escape(label)}</strong> {escape(value)}</span>'
        for label, value in filters
    )


def _render_search_mode_pill(search_meta: dict[str, str] | None, _) -> str:
    if not search_meta:
        return ""
    return f'<span class="meta-pill">{escape(_friendly_intent_label(search_meta, _))}</span>'


def _page_url(request: Request, page: int) -> str:
    params = dict(request.query_params)
    params["page"] = str(max(1, page))
    return f"/gallery?{urlencode(params)}"


def _per_page_url(request: Request, size: int) -> str:
    params = dict(request.query_params)
    params["per_page"] = str(size)
    params.pop("page", None)
    return f"/gallery?{urlencode(params)}"


def _render_person_options(values: list[str], selected: str | None, _) -> str:
    chosen = selected or ""
    options = [f'<option value=""{"" if chosen else " selected"}>{_("gallery.person_all")}</option>']
    for value in values:
        sel = " selected" if value == chosen else ""
        options.append(f'<option value="{escape(value)}"{sel}>{escape(value)}</option>')
    return "".join(options)


def _render_page_link(label: str, href: str, *, enabled: bool) -> str:
    if enabled:
        return f'<a class="button secondary" href="{escape(href)}">{escape(label)}</a>'
    return f'<span class="button secondary" style="opacity:.45; pointer-events:none;">{escape(label)}</span>'


def _resolve_asset_path(derived_root: Path, derived_path: str) -> Path:
    candidate = Path(derived_path)
    resolved_root = derived_root.resolve()
    if candidate.is_absolute():
        try:
            candidate.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="derived asset file missing") from exc
        return candidate

    resolved_path = (resolved_root / candidate).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="derived asset file missing") from exc
    return resolved_path


def _parse_date(value: Optional[str]) -> Optional[date]:
    if value is None or value.strip() == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _start_of_day(value: Optional[date]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.combine(value, time.min)


def _end_of_day(value: Optional[date]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.combine(value, time.max)
