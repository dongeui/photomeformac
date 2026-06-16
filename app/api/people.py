"""Person management API — list, rename, and query face clusters."""

from __future__ import annotations

from datetime import datetime
from html import escape
from io import BytesIO
import json
from pathlib import Path
import tempfile
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import and_, bindparam, delete, func, not_, or_, select, text, update

from app.api.deps import require_state
from app.api.i18n_web import render_lang_switcher, request_translator
from app.models.asset import DerivedAsset
from app.models.face import Face
from app.models.media import MediaFile
from app.models.person import Person
from app.models.semantic import SearchDocument
from app.models.tag import Tag
from app.services.image_decode import ensure_heif_support
from app.services.processing.person_centroids import recompute_person_centroid
from app.services.semantic import SemanticCatalog
from app.services.search.hybrid import clear_query_cache
from app.services.search.vocab import TagVocabularyCache

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]

router = APIRouter(prefix="/people", tags=["people"])


class PersonResponse(BaseModel):
    id: int
    display_name: str
    aliases: list[str]
    face_count: int
    media_count: int
    sample_file_ids: list[str]
    sample_face_ids: list[int]


class PersonPreviewItem(BaseModel):
    file_id: str
    filename: str
    relative_path: str
    media_kind: str
    captured_at: Optional[str]
    asset_id: Optional[int]
    face_id: Optional[int] = None


class PersonPreviewResponse(BaseModel):
    person: PersonResponse
    items: list[PersonPreviewItem]


class RenamePersonRequest(BaseModel):
    display_name: str
    aliases: Optional[list[str]] = None


class MergePeopleRequest(BaseModel):
    target_person_id: int
    source_person_ids: list[int]


class AssignFaceRequest(BaseModel):
    person_id: Optional[int] = None


class PhotoAssignRequest(BaseModel):
    """Manually put a person in a photo — for shots the face detector missed
    (babies, HEIC, faces turned away). Either an existing person_id or a name
    (existing match or a new named person). Optional bbox (normalized x/y/w/h in
    0..1) for a manual face region that we try to embed so the model learns."""

    file_id: str
    person_id: Optional[int] = None
    name: Optional[str] = None
    bbox: Optional[dict[str, float]] = None


@router.get("", response_model=list[PersonResponse])
def list_people(request: Request) -> list[PersonResponse]:
    """List all known persons with face counts."""
    database = require_state(request, "database")
    with database.session_factory() as session:
        rows = session.execute(
            select(
                Person,
                func.count(Face.id).filter(_active_media_predicate()).label("face_count"),
                func.count(func.distinct(Face.file_id)).filter(_active_media_predicate()).label("media_count"),
            )
            .outerjoin(Face, Face.person_id == Person.id)
            .outerjoin(MediaFile, MediaFile.file_id == Face.file_id)
            .where(Person.merged_into_id.is_(None))
            .group_by(Person.id)
            .having(func.count(Face.id).filter(_active_media_predicate()) > 0)
            .order_by(func.count(Face.id).filter(_active_media_predicate()).desc())
        ).all()
        result: list[PersonResponse] = []
        for person, face_count, media_count in rows:
            sample_faces = session.scalars(
                select(Face)
                .join(MediaFile, MediaFile.file_id == Face.file_id)
                .where(Face.person_id == person.id, _active_media_predicate())
                .limit(3)
            ).all()
            result.append(
                PersonResponse(
                    id=person.id,
                    display_name=person.display_name,
                    aliases=_person_display_aliases(person),
                    face_count=face_count,
                    media_count=media_count,
                    sample_file_ids=[str(f.file_id) for f in sample_faces],
                    sample_face_ids=[int(f.id) for f in sample_faces],
                )
            )
        return result


_PEOPLE_MANAGE_CSS = """
:root{color-scheme:light dark;--bg:#f5f5f7;--panel:#fff;--line:rgba(0,0,0,.1);--text:#1d1d1f;--muted:#86868b;--accent:#0a84ff;--accent-soft:rgba(10,132,255,.12);--warn:#b25000;}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--panel:#2c2c2e;--line:rgba(255,255,255,.12);--text:#f5f5f7;--muted:#98989d;--accent:#0a84ff;--accent-soft:rgba(10,132,255,.24);--warn:#ff9f0a;}}
*{box-sizing:border-box;}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",Arial,sans-serif;background:var(--bg);color:var(--text);}
.hdr{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel);z-index:5;flex-wrap:wrap;}
.hdr h1{font-size:1.1rem;margin:0;font-weight:600;}
.hdr .sub{font-size:.82rem;color:var(--muted);}
.search{margin-left:auto;width:240px;max-width:45%;padding:7px 11px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font-size:.85rem;}
.list{max-width:900px;margin:0 auto;padding:14px 16px 30px;}
.hint{font-size:.84rem;color:var(--muted);padding:6px 4px 14px;}
.minor-row{text-align:center;padding:18px 4px 4px;}
.minor-toggle{display:inline-block;font-size:.86rem;color:var(--muted);text-decoration:none;padding:8px 16px;border:1px solid var(--line);border-radius:999px;background:var(--panel);}
.minor-toggle:hover{color:var(--text);border-color:var(--muted);}
.row{display:flex;align-items:center;gap:14px;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--panel);margin-bottom:10px;transition:background .12s,border-color .12s,box-shadow .12s;}
.row.unnamed{opacity:.85;}
.row.merge-on{border-color:var(--accent);background:var(--accent-soft);}
.row.merge-primary{border-color:#ff3b30;background:rgba(255,59,48,.10);box-shadow:inset 3px 0 0 #ff3b30;}
@media (prefers-color-scheme:dark){.row.merge-primary{background:rgba(255,69,58,.16);box-shadow:inset 3px 0 0 #ff453a;}}
.keep-tag{display:none;font-size:11px;font-weight:600;padding:2px 7px;border-radius:5px;background:rgba(255,59,48,.16);color:#ff3b30;white-space:nowrap;}
.row.merge-primary .keep-tag{display:inline-block;}
@media (prefers-color-scheme:dark){.keep-tag{color:#ff453a;background:rgba(255,69,58,.20);}}
.face{width:54px;height:54px;border-radius:10px;flex:0 0 auto;overflow:hidden;background:var(--accent-soft);display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px;text-decoration:none;}
.face img{width:100%;height:100%;object-fit:cover;}
.meta{flex:1;min-width:0;display:flex;flex-direction:column;gap:7px;}
.titlerow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.pm-name{font-size:14px;padding:6px 10px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text);min-width:150px;}
.pm-aliases{font-size:13px;padding:5px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text);width:100%;}
.badge{font-size:11px;padding:2px 7px;border-radius:5px;}
.badge.named{background:var(--accent-soft);color:var(--accent);}
.badge.unnamed{background:rgba(180,80,0,.16);color:var(--warn);}
.count{font-size:12px;color:var(--muted);white-space:nowrap;text-decoration:none;}
.count:hover{color:var(--accent);}
.btn{font-size:12px;padding:6px 11px;border-radius:7px;border:1px solid var(--line);background:var(--panel);color:var(--text);cursor:pointer;}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;}
.btn.danger{background:#ff3b30;border-color:#ff3b30;color:#fff;}
.btn.danger:disabled{background:var(--panel);border-color:var(--line);color:var(--muted);}
.btn:disabled{opacity:.45;cursor:default;}
#merge-count b{color:#ff3b30;}
.footer{position:sticky;bottom:0;display:flex;align-items:center;gap:12px;padding:10px 20px;border-top:1px solid var(--line);background:var(--panel);font-size:13px;color:var(--muted);}
.footer .sp{margin-left:auto;}
.empty{text-align:center;color:var(--muted);padding:56px 16px;}
.layout{display:flex;align-items:stretch;min-height:100vh;}
.sidebar{width:224px;flex:0 0 224px;padding:18px 14px;border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;gap:4px;position:sticky;top:0;height:100vh;}
.side-brand{font-size:1.15rem;font-weight:600;padding:2px 8px 10px;}
.side-item{padding:8px 10px;border-radius:8px;color:var(--text);text-decoration:none;font-size:.92rem;}
.side-item:hover{background:var(--accent-soft);}
.side-item.active{background:var(--accent-soft);color:var(--accent);font-weight:500;}
.side-lang{display:flex;gap:4px;margin-top:10px;padding-top:10px;border-top:1px solid var(--line);}
.lang-link{font-size:.78rem;color:var(--muted);text-decoration:none;padding:3px 8px;border-radius:6px;}
.lang-link:hover{background:var(--accent-soft);}
.lang-link.active{color:var(--accent);font-weight:600;}
.content{flex:1;min-width:0;display:flex;flex-direction:column;}
@media (max-width:720px){.layout{flex-direction:column;}.sidebar{width:auto;flex:none;height:auto;position:static;border-right:none;border-bottom:1px solid var(--line);flex-direction:row;flex-wrap:wrap;gap:4px;}}
.mergedrow{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px;flex-wrap:wrap;}
.merged-chip{display:inline-flex;align-items:center;gap:2px;background:var(--accent-soft);color:var(--accent);padding:2px 4px 2px 9px;border-radius:6px;font-weight:500;}
.merged-undo{border:none;background:transparent;color:var(--accent);cursor:pointer;font-size:14px;line-height:1;padding:0 3px;}
.merged-undo:hover{opacity:.6;}
"""

_PEOPLE_MANAGE_JS = """
var mergeOrder=[];
function rowOf(id){return document.querySelector('.row[data-pid="'+id+'"]');}
function nameOf(id){var r=rowOf(id);if(!r){return '#'+id;}var v=(r.querySelector('.pm-name').value||'').trim();return v||('그룹 #'+id);}
function onMergeChange(cb){
  var id=parseInt(cb.dataset.pid);
  if(cb.checked){if(mergeOrder.indexOf(id)<0){mergeOrder.push(id);}}
  else{mergeOrder=mergeOrder.filter(function(x){return x!==id;});}
  [].slice.call(document.querySelectorAll('.row')).forEach(function(r){r.classList.remove('merge-on','merge-primary');});
  mergeOrder.forEach(function(x,i){var r=rowOf(x);if(!r){return;}r.classList.add('merge-on');if(i===0){r.classList.add('merge-primary');}});
  var n=mergeOrder.length;
  var cnt=document.getElementById('merge-count');
  if(n===0){cnt.textContent=PT.mergeCountZero;}
  else{cnt.innerHTML=PT.mergeCountN.replace('{n}',n)+'<b>'+escapeHtml(nameOf(mergeOrder[0]))+'</b>';}
  var btn=document.getElementById('merge-btn');
  btn.disabled=n<2;
  btn.textContent=n>=2?PT.mergeButtonN.replace('{n}',n-1).replace('{name}',nameOf(mergeOrder[0])):PT.mergeButtonDefault;
}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
async function savePerson(id){
  var row=rowOf(id);
  if(!row){return;}
  var name=row.querySelector('.pm-name').value.trim();
  var aliases=row.querySelector('.pm-aliases').value.split(',').map(function(s){return s.trim();}).filter(Boolean);
  var r=await fetch('/people/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({display_name:name,aliases:aliases})});
  if(r.ok){location.reload();}else{var m=PT.saveFailed;try{m=(await r.json()).detail||m;}catch(e){}alert(m);}
}
function onNameKey(e,id){if(e.isComposing||e.keyCode===229){return;}if(e.key==='Enter'){e.preventDefault();savePerson(id);}}
async function mergeSelected(){
  if(mergeOrder.length<2){return;}
  var target=mergeOrder[0];
  var sources=mergeOrder.slice(1);
  if(!confirm(PT.confirmMerge.replace('{n}',sources.length).replace('{name}',nameOf(target)))){return;}
  var r=await fetch('/people/merge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_person_id:target,source_person_ids:sources})});
  if(r.ok){location.reload();}else{var m=PT.mergeFailed;try{m=(await r.json()).detail||m;}catch(e){}alert(m);}
}
function filterRows(q){q=q.trim().toLowerCase();[].slice.call(document.querySelectorAll('.row')).forEach(function(r){r.style.display=(!q||(r.dataset.search||'').indexOf(q)>=0)?'':'none';});}
async function unmergePerson(targetId,sourceId){
  if(!confirm(PT.confirmUnmerge)){return;}
  var r=await fetch('/people/'+targetId+'/unmerge/'+sourceId,{method:'POST'});
  if(r.ok){location.reload();}else{var m=PT.unmergeFailed;try{m=(await r.json()).detail||m;}catch(e){}alert(m);}
}
"""


def _render_people_manage_html(
    people: list[dict],
    request: Request,
    *,
    minor_count: int = 0,
    include_minor: bool = False,
) -> str:
    locale, _ = request_translator(request)
    if not people:
        rows_html = f'<div class="empty">{_("people.empty")}</div>'
    else:
        parts = []
        for p in people:
            pid = int(p["id"])
            dn = str(p["display_name"])
            aliases = [str(a) for a in p.get("aliases", []) if str(a).strip()]
            is_unnamed = dn.startswith("person-") and not aliases
            name_val = "" if is_unnamed else dn
            gallery_href = "/gallery?person=" + quote(dn)
            face_id = p.get("face_id")
            face_inner = (
                f'<img src="/people/faces/{int(face_id)}/crop" loading="lazy" decoding="async" alt="{_("people.face_alt")}">'
                if face_id is not None
                else _("people.face_fallback")
            )
            badge = (
                f'<span class="badge unnamed">{_("people.badge_unnamed")}</span>'
                if is_unnamed
                else f'<span class="badge named">{_("people.badge_named")}</span>'
            )
            search_attr = escape((dn + " " + " ".join(aliases)).lower())
            placeholder = _("people.name_placeholder_unnamed") if is_unnamed else _("people.name_placeholder")
            merged_sources = p.get("merged_sources") or []
            merged_chips = "".join(
                f'<span class="merged-chip">{escape(str(m["label"]))}'
                f'<button type="button" class="merged-undo" onclick="unmergePerson({pid},{int(m["id"])})" title="{_("people.unmerge_title")}">↩</button></span>'
                for m in merged_sources
            )
            merged_row = f'<div class="mergedrow">{_("people.merged_label")} {merged_chips}</div>' if merged_chips else ""
            parts.append(
                f'''
        <div class="row{' unnamed' if is_unnamed else ''}" data-pid="{pid}" data-search="{search_attr}">
          <input type="checkbox" class="pm-merge" data-pid="{pid}" onchange="onMergeChange(this)" aria-label="{_("people.merge_select_aria")}">
          <a class="face" href="{gallery_href}" title="{_("people.view_photos")}">{face_inner}</a>
          <div class="meta">
            <div class="titlerow">
              <input class="pm-name" value="{escape(name_val)}" placeholder="{placeholder}" aria-label="{_("people.name_aria")}" onkeydown="onNameKey(event,{pid})">
              {badge}
              <span class="keep-tag">{_("people.keep_on_merge")}</span>
              <button class="btn" onclick="savePerson({pid})">{_("people.save")}</button>
            </div>
            <div class="aliasrow">
              <input class="pm-aliases" value="{escape(', '.join(aliases))}" placeholder="{_("people.alias_placeholder")}" aria-label="{_("people.alias_aria")}" onkeydown="onNameKey(event,{pid})">
            </div>
            {merged_row}
          </div>
          <a class="count" href="{gallery_href}">{_("people.count", media=int(p.get('media_count') or 0), face=int(p.get('face_count') or 0))}</a>
        </div>'''
            )
        rows_html = "".join(parts)

    total = len(people)
    if include_minor:
        minor_toggle_html = (
            f'<a class="minor-toggle" href="/people/manage">{_("people.show_major")}</a>'
        )
    elif minor_count > 0:
        minor_toggle_html = (
            f'<a class="minor-toggle" href="/people/manage?include_minor=1">'
            f'{_("people.show_minor", n=minor_count)}</a>'
        )
    else:
        minor_toggle_html = ""
    people_t = {
        "mergeCountZero": _("people.merge_count_zero"),
        "mergeCountN": _("people.merge_count_n"),
        "mergeButtonDefault": _("people.merge_button_default"),
        "mergeButtonN": _("people.merge_button_n"),
        "saveFailed": _("people.save_failed"),
        "mergeFailed": _("people.merge_failed"),
        "unmergeFailed": _("people.unmerge_failed"),
        "confirmMerge": _("people.confirm_merge"),
        "confirmUnmerge": _("people.confirm_unmerge"),
    }
    t_json = json.dumps(people_t, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="{locale}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_("people.title")} · Trove</title>
<style>{_PEOPLE_MANAGE_CSS}</style>
</head>
<body>
  <div class="layout">
    <aside class="sidebar">
      <div class="side-brand">Trove</div>
      <a class="side-item" href="/gallery">{_("nav.all_photos")}</a>
      <a class="side-item active" href="/people/manage">{_("nav.people")}</a>
      <!-- 설정(/dashboard)은 사이드바에서 숨김 — 진입은 메뉴바 "설정 열기" -->
      <div class="side-lang">{render_lang_switcher(locale, request)}</div>
    </aside>
    <section class="content">
      <div class="hdr">
        <h1>{_("people.title")}</h1>
        <span class="sub">{_("people.header_sub", total=total)}</span>
        <input class="search" placeholder="{_("people.search_placeholder")}" oninput="filterRows(this.value)" aria-label="{_("people.search_placeholder")}">
      </div>
      <div class="list">
        <div class="hint">{_("people.hint")}</div>
        {rows_html}
        <div class="minor-row">{minor_toggle_html}</div>
      </div>
      <div class="footer">
        <span id="merge-count">{_("people.merge_count_zero")}</span>
        <span class="sp"></span>
        <button class="btn danger" id="merge-btn" onclick="mergeSelected()" disabled>{_("people.merge_button_default")}</button>
      </div>
    </section>
  </div>
  <script>const PT = {t_json};
{_PEOPLE_MANAGE_JS}</script>
</body>
</html>"""


@router.get("/manage", response_class=HTMLResponse)
def people_manage_page(
    request: Request,
    include_minor: bool = Query(False),
) -> HTMLResponse:
    database = require_state(request, "database")
    active = MediaFile.status.not_in(("missing", "replaced", "excluded"))
    # 경량 임베딩의 과분할로 생기는 노이즈 클러스터를 거른다: 사진 10장 이하
    # '그리고' 얼굴 10회 이하인 무명 그룹은 숨긴다(둘 중 하나라도 넘으면 표시).
    # 사용자가 이름 붙인 인물은 적게 나와도 항상 표시한다. include_minor면 게이트를
    # 풀어 숨겨둔 '기타 얼굴'까지 보여준다(숫자 옵션 대신 '더 보기' 토글로 노출).
    candidate = or_(
        func.count(func.distinct(Face.file_id)).filter(active) > 10,
        func.count(Face.id).filter(active) > 10,
        Person.display_name.not_like("person-%"),
    )
    has_faces = func.count(Face.id).filter(active) > 0
    people: list[dict] = []
    with database.session_factory() as session:
        rows = session.execute(
            select(
                Person,
                func.count(Face.id).filter(active).label("face_count"),
                func.count(func.distinct(Face.file_id)).filter(active).label("media_count"),
            )
            .outerjoin(Face, Face.person_id == Person.id)
            .outerjoin(MediaFile, MediaFile.file_id == Face.file_id)
            .where(Person.merged_into_id.is_(None))
            .group_by(Person.id)
            .having(has_faces if include_minor else candidate)
            .order_by(func.count(Face.id).filter(active).desc(), Person.id.asc())
            .limit(1000)
        ).all()
        # 게이트에 걸려 숨겨진 '기타 얼굴' 인물 수(얼굴은 있으나 노이즈 기준).
        minor_count = int(
            session.scalar(
                select(func.count()).select_from(
                    select(Person.id)
                    .outerjoin(Face, Face.person_id == Person.id)
                    .outerjoin(MediaFile, MediaFile.file_id == Face.file_id)
                    .where(Person.merged_into_id.is_(None))
                    .group_by(Person.id)
                    .having(and_(has_faces, not_(candidate)))
                    .subquery()
                )
            )
            or 0
        )
        # 각 target에 병합돼 숨겨진 source 목록 (배치 조회로 N+1 회피)
        merged_rows = session.scalars(
            select(Person).where(Person.merged_into_id.isnot(None))
        ).all()
        merged_by_target: dict[int, list[dict]] = {}
        for m in merged_rows:
            label = str(m.display_name)
            if _INTERNAL_PERSON_ID_RE.match(label.strip()):
                m_aliases = [
                    str(a) for a in (m.aliases_json or [])
                    if str(a).strip() and not _INTERNAL_PERSON_ID_RE.match(str(a).strip())
                ]
                label = m_aliases[0] if m_aliases else f"그룹 #{int(m.id)}"
            merged_by_target.setdefault(int(m.merged_into_id), []).append({"id": int(m.id), "label": label})
        for person, face_count, media_count in rows:
            raw_aliases = person.aliases_json if isinstance(person.aliases_json, list) else []
            aliases = [
                str(a)
                for a in raw_aliases
                if str(a).strip() and not _INTERNAL_PERSON_ID_RE.match(str(a).strip())
            ]
            sample = session.execute(
                select(Face.id)
                .join(MediaFile, MediaFile.file_id == Face.file_id)
                .where(Face.person_id == person.id, MediaFile.media_kind == "image", active)
                .order_by(Face.id.asc())
                .limit(1)
            ).first()
            people.append(
                {
                    "id": int(person.id),
                    "display_name": str(person.display_name),
                    "aliases": aliases,
                    "face_count": int(face_count or 0),
                    "media_count": int(media_count or 0),
                    "face_id": int(sample[0]) if sample else None,
                    "merged_sources": merged_by_target.get(int(person.id), []),
                }
            )
    return HTMLResponse(
        _render_people_manage_html(
            people, request, minor_count=minor_count, include_minor=include_minor
        )
    )


@router.get("/{person_id}", response_model=PersonResponse)
def get_person(person_id: int, request: Request) -> PersonResponse:
    database = require_state(request, "database")
    with database.session_factory() as session:
        person = _get_visible_person(session, person_id)
        face_count = _person_face_count(session, person_id)
        sample_faces = session.scalars(
            select(Face)
            .join(MediaFile, MediaFile.file_id == Face.file_id)
            .where(Face.person_id == person_id, _active_media_predicate())
            .limit(3)
        ).all()
        return PersonResponse(
            id=person.id,
            display_name=person.display_name,
            aliases=_person_display_aliases(person),
            face_count=face_count,
            media_count=_person_media_count(session, person_id),
            sample_file_ids=[str(f.file_id) for f in sample_faces],
            sample_face_ids=[int(f.id) for f in sample_faces],
        )


@router.post("/merge", response_model=PersonResponse)
def merge_people(body: MergePeopleRequest, request: Request) -> PersonResponse:
    """Merge multiple face clusters into one user-selected person."""
    source_ids = _normalize_merge_source_ids(body.source_person_ids, body.target_person_id)
    if not source_ids:
        raise HTTPException(status_code=422, detail="select at least one source person")

    database = require_state(request, "database")
    search_version = request.app.state.settings.semantic_search_version
    with database.session_factory() as session:
        target = session.get(Person, body.target_person_id)
        if target is None or target.merged_into_id is not None:
            raise HTTPException(status_code=404, detail="Target person not found")
        # 이미 병합돼 숨겨진 사람은 source로 다시 쓸 수 없다(병합 체인 꼬임 방지).
        sources = session.scalars(
            select(Person).where(Person.id.in_(source_ids), Person.merged_into_id.is_(None))
        ).all()
        found_source_ids = {int(person.id) for person in sources}
        missing_ids = [person_id for person_id in source_ids if person_id not in found_source_ids]
        if missing_ids:
            raise HTTPException(status_code=404, detail=f"Source person not found: {missing_ids[0]}")

        old_labels = set(_person_labels(target))
        for source in sources:
            old_labels.update(_person_labels(source))

        merged_aliases = _merge_person_aliases(target, sources)
        # 얼굴을 target으로 옮기기 전에, 아직 origin이 없는 얼굴은 현재 소속을
        # merged_from_person_id로 기록한다(최초 origin 보존 → unmerge로 정확히 복원).
        session.execute(
            update(Face)
            .where(Face.person_id.in_(source_ids), Face.merged_from_person_id.is_(None))
            .values(merged_from_person_id=Face.person_id)
        )
        session.execute(update(Face).where(Face.person_id.in_(source_ids)).values(person_id=target.id))
        target.aliases_json = merged_aliases
        # 삭제 대신 숨김(soft-hide): source의 이름/별칭을 보존해 unmerge 시 복원한다.
        for source in sources:
            source.merged_into_id = target.id

        _sync_person_search_labels(session, target, old_labels=old_labels, search_version=search_version)
        session.commit()
        clear_query_cache()
        TagVocabularyCache.invalidate()
        session.refresh(target)
        # 분류기에도 병합을 반영: target 센트로이드를 합쳐진 얼굴 전체로 재계산해
        # 같은 사람의 새 사진이 또 새 그룹으로 빠지지 않게 한다.
        recompute_person_centroid(
            session, embeddings_root=request.app.state.settings.embeddings_root, person=target
        )
        return _person_response(session, target)


@router.post("/{target_id}/unmerge/{source_id}", response_model=PersonResponse)
def unmerge_person(target_id: int, source_id: int, request: Request) -> PersonResponse:
    """Undo one source of a merge: move its faces back and unhide it.

    Flexible per-source unmerge — restores exactly the faces that came from
    `source_id` (tracked via Face.merged_from_person_id) and reveals the source
    person again with its original name/aliases intact (it was soft-hidden, not
    deleted). The source's name/aliases are removed from the target's aliases.
    """
    database = require_state(request, "database")
    search_version = request.app.state.settings.semantic_search_version
    with database.session_factory() as session:
        source = session.get(Person, source_id)
        if source is None or source.merged_into_id != target_id:
            raise HTTPException(status_code=404, detail="merged source not found for this target")
        target = session.get(Person, target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Target person not found")

        old_target_labels = set(_person_labels(target))

        # source에서 온 얼굴만 골라 되돌린다(origin 추적값으로 정확히 분리).
        session.execute(
            update(Face)
            .where(Face.merged_from_person_id == source_id)
            .values(person_id=source_id, merged_from_person_id=None)
        )
        # 숨김 해제 → 이름/별칭은 보존돼 있어 그대로 복원된다.
        source.merged_into_id = None
        # target이 흡수했던 source 이름/별칭을 target alias에서 제거.
        source_labels = {label.casefold() for label in _person_labels(source)}
        target.aliases_json = [
            alias for alias in (target.aliases_json or [])
            if alias.casefold() not in source_labels
        ]

        # 검색 라벨 재동기화. source 파일에는 병합 시절 붙은 target 라벨이 남아
        # 있으므로 old_labels로 넘겨 지운다. source를 먼저 돌려야 두 사람이 같이
        # 나온 파일에서 target 라벨이 지워졌다가 target 동기화로 다시 복원된다.
        _sync_person_search_labels(session, source, old_labels=old_target_labels, search_version=search_version)
        _sync_person_search_labels(session, target, old_labels=old_target_labels, search_version=search_version)
        session.commit()
        clear_query_cache()
        TagVocabularyCache.invalidate()
        session.refresh(source)
        # 센트로이드도 분리 결과대로 양쪽 모두 재계산한다.
        embeddings_root = request.app.state.settings.embeddings_root
        recompute_person_centroid(session, embeddings_root=embeddings_root, person=source)
        recompute_person_centroid(session, embeddings_root=embeddings_root, person=target)
        return _person_response(session, source)


@router.patch("/{person_id}", response_model=PersonResponse)
def rename_person(
    person_id: int,
    body: RenamePersonRequest,
    request: Request,
) -> PersonResponse:
    """Update the display name for a person (face cluster)."""
    database = require_state(request, "database")
    requested_name = body.display_name.strip()
    aliases = _normalize_aliases(_validate_user_aliases(body.aliases or []))
    new_name = requested_name
    if (not new_name or _INTERNAL_PERSON_ID_RE.match(new_name)) and aliases:
        new_name = aliases[0]
        aliases = [alias for alias in aliases if alias.casefold() != new_name.casefold()]
    if not new_name:
        raise HTTPException(status_code=422, detail="display_name must not be empty")
    if _INTERNAL_PERSON_ID_RE.match(new_name):
        raise HTTPException(status_code=422, detail="Set a real name or add at least one alias")
    with database.session_factory() as session:
        person = _get_visible_person(session, person_id)
        old_labels = _person_labels(person)
        person.display_name = new_name
        person.aliases_json = aliases
        _sync_person_search_labels(
            session,
            person,
            old_labels=old_labels,
            search_version=request.app.state.settings.semantic_search_version,
        )
        session.commit()
        clear_query_cache()
        TagVocabularyCache.invalidate()
        session.refresh(person)
        return _person_response(session, person)


@router.get("/{person_id}/media", response_model=list[str])
def list_person_media(
    person_id: int,
    request: Request,
    limit: int = 50,
) -> list[str]:
    """Return file_ids of media containing this person."""
    database = require_state(request, "database")
    with database.session_factory() as session:
        _get_visible_person(session, person_id)
        file_ids = session.scalars(
            select(Face.file_id)
            .where(Face.person_id == person_id)
            .distinct()
            .limit(limit)
        ).all()
        return [str(fid) for fid in file_ids]


@router.get("/{person_id}/preview", response_model=PersonPreviewResponse)
def preview_person_media(
    person_id: int,
    request: Request,
    limit: int = 48,
) -> PersonPreviewResponse:
    """Return lightweight media cards for the dashboard person preview modal."""
    database = require_state(request, "database")
    bounded_limit = min(max(int(limit), 1), 96)
    with database.session_factory() as session:
        person = _get_visible_person(session, person_id)
        rows = session.execute(
            select(MediaFile, DerivedAsset, func.min(Face.id).label("face_id"))
            .join(Face, Face.file_id == MediaFile.file_id)
            .outerjoin(
                DerivedAsset,
                (DerivedAsset.file_id == MediaFile.file_id) & (DerivedAsset.asset_kind == "thumb"),
            )
            .where(Face.person_id == person_id, _active_media_predicate())
            .group_by(MediaFile.file_id)
            .order_by(_captured_at_expr().desc(), MediaFile.file_id.desc())
            .limit(bounded_limit)
        ).all()
        items = [
            PersonPreviewItem(
                file_id=str(media_file.file_id),
                filename=media_file.filename,
                relative_path=media_file.relative_path,
                media_kind=media_file.media_kind,
                captured_at=media_file.exif_datetime.isoformat() if media_file.exif_datetime else None,
                asset_id=int(asset.id) if asset is not None else None,
                face_id=int(face_id) if face_id is not None else None,
            )
            for media_file, asset, face_id in rows
        ]
        return PersonPreviewResponse(person=_person_response(session, person), items=items)


@router.patch("/faces/{face_id}", status_code=204)
def assign_face(face_id: int, body: AssignFaceRequest, request: Request) -> Response:
    """Reassign or unassign a single face to a different person."""
    database = require_state(request, "database")
    search_version = request.app.state.settings.semantic_search_version
    with database.session_factory() as session:
        face = session.get(Face, face_id)
        if face is None:
            raise HTTPException(status_code=404, detail="Face not found")
        if body.person_id is not None:
            target = session.get(Person, body.person_id)
            if target is None or target.merged_into_id is not None:
                raise HTTPException(status_code=404, detail="Target person not found")
        file_id = str(face.file_id)
        previous_person_id = face.person_id
        face.person_id = body.person_id
        # 수동 재할당은 새로운 확정 소속이므로 병합 origin 추적을 끊는다
        # (이후 unmerge가 이 얼굴을 다시 끌고 가지 않도록).
        face.merged_from_person_id = None
        _sync_single_file_person_labels(session, file_id, search_version=search_version)
        session.commit()
        clear_query_cache()
        TagVocabularyCache.invalidate()
        # 사용자 교정을 분류기에 반영: 얼굴이 빠진 쪽/들어간 쪽 센트로이드 재계산.
        embeddings_root = request.app.state.settings.embeddings_root
        for person_id in {previous_person_id, body.person_id} - {None}:
            person = session.get(Person, int(person_id))
            if person is not None:
                recompute_person_centroid(session, embeddings_root=embeddings_root, person=person)
    return Response(status_code=204)


def _resolve_or_create_person(session, *, person_id: int | None, name: str | None) -> Person:
    """Find the target person by id, or by name (existing match), else create a
    new named person. Used by manual photo tagging."""
    if person_id is not None:
        person = session.get(Person, person_id)
        if person is None or person.merged_into_id is not None:
            raise HTTPException(status_code=404, detail="Target person not found")
        return person
    label = " ".join((name or "").strip().split())
    if not label or _INTERNAL_PERSON_ID_RE.match(label):
        raise HTTPException(status_code=422, detail="a valid person name or id is required")
    folded = label.casefold()
    # Reuse an existing non-merged person if the name matches its display name or
    # any alias — so "정이한" tags the same person the detector already knows.
    for person in session.scalars(select(Person).where(Person.merged_into_id.is_(None))):
        if folded in {value.casefold() for value in _person_labels(person)}:
            return person
    person = Person(display_name=label, aliases_json=[])
    session.add(person)
    session.flush()
    return person


def _try_embed_face_region(
    request: Request, session, media: MediaFile, bbox_norm: dict[str, float], settings
) -> tuple[dict[str, Any], tuple[float, ...]] | None:
    """Crop the user's normalized box from the photo and try to detect+embed a
    face in it. Returns (full-image-pixel bbox, embedding) on success, else None
    (caller falls back to a boxless tag). Zooming into the box sometimes lets the
    detector lock on a face it missed on the whole frame."""
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None or Image is None:
        return None
    asset = session.scalars(
        select(DerivedAsset)
        .where(DerivedAsset.file_id == media.file_id, DerivedAsset.asset_kind == "thumb")
        .order_by(DerivedAsset.id.asc())
        .limit(1)
    ).first()
    source_path = Path(media.current_path)
    if not source_path.is_file():
        if asset is None:
            return None
        source_path = _resolve_derived_path(settings.derived_root, asset.derived_path)
    if not source_path.is_file():
        return None

    tmp_path: Path | None = None
    try:
        ensure_heif_support()
        with Image.open(source_path) as image:
            image = image.convert("RGB")
            width, height = image.size
            x = max(0.0, float(bbox_norm.get("x") or 0)) * width
            y = max(0.0, float(bbox_norm.get("y") or 0)) * height
            w = max(0.0, float(bbox_norm.get("width", bbox_norm.get("w", 0)) or 0)) * width
            h = max(0.0, float(bbox_norm.get("height", bbox_norm.get("h", 0)) or 0)) * height
            if w < 1 or h < 1:
                return None
            pad = max(w, h) * 0.25
            left, top = int(max(0, x - pad)), int(max(0, y - pad))
            right, bottom = int(min(width, x + w + pad)), int(min(height, y + h + pad))
            if right <= left or bottom <= top:
                return None
            crop = image.crop((left, top, right, bottom))
            # 작은 크롭은 검출기가 잡기 쉽게 키운다.
            if max(crop.size) < 320:
                scale = 320 / max(crop.size)
                crop = crop.resize((int(crop.width * scale), int(crop.height * scale)))
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
                tmp_path = Path(handle.name)
                crop.save(handle, format="JPEG", quality=92)
            face_bbox = {"x": int(x), "y": int(y), "width": int(w), "height": int(h), "manual": True}
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        return None

    try:
        result = pipeline.detect_largest_face_embedding(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if result is None:
        return None
    embedding, _detected_bbox = result
    return face_bbox, embedding


def _store_manual_face_embedding(
    embeddings_root: Path, *, file_id: str, face_index: int, person: Person,
    bbox: dict[str, Any], embedding: tuple[float, ...],
) -> str:
    """Write a manual face embedding to the same store the pipeline uses, so the
    centroid recompute picks it up. Returns the ``embeddings/...`` relative ref."""
    shard = file_id[:2] if len(file_id) >= 2 else "xx"
    filename = f"{file_id}-face-{face_index:03d}.json"
    abs_path = Path(embeddings_root) / "faces" / "v1" / shard / filename
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "file_id": file_id, "face_index": int(face_index), "person_id": int(person.id),
        "person": person.display_name, "bbox": bbox, "embedding": list(embedding),
        "updated_at": datetime.utcnow().isoformat(),
    }
    tmp = abs_path.with_name(abs_path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    tmp.replace(abs_path)
    return f"embeddings/faces/v1/{shard}/{filename}"


@router.post("/photo-assign", response_model=PersonResponse)
def assign_person_to_photo(body: PhotoAssignRequest, request: Request) -> PersonResponse:
    """Manually put a person in a photo the detector missed.

    Models the association as a Face row so it flows through the existing
    reconcile/search/centroid machinery and survives re-sync (unlike a bare
    person tag, which the face-driven reconcile would wipe). A boxless face
    (no embedding) makes the photo findable by name; a boxed face whose region
    we can embed also teaches the classifier (best-effort — wired next).
    """
    database = require_state(request, "database")
    settings = request.app.state.settings
    search_version = settings.semantic_search_version
    with database.session_factory() as session:
        media = session.get(MediaFile, body.file_id)
        if media is None:
            raise HTTPException(status_code=404, detail="photo not found")
        person = _resolve_or_create_person(session, person_id=body.person_id, name=body.name)

        learned = False
        if body.bbox:
            # B: try to learn from the drawn region (detect + embed → real face).
            region = _try_embed_face_region(request, session, media, body.bbox, settings)
            if region is not None:
                face_bbox, embedding = region
                face = Face(file_id=body.file_id, person_id=person.id, bbox=face_bbox, embedding_ref=None)
                session.add(face)
                session.flush()  # stable id → unique embedding filename
                face.embedding_ref = _store_manual_face_embedding(
                    settings.embeddings_root, file_id=body.file_id, face_index=int(face.id),
                    person=person, bbox=face_bbox, embedding=embedding,
                )
                learned = True

        if not learned:
            # A (or B fallback when detection missed): boxless association.
            # Idempotent — don't stack duplicate boxless rows for the same person.
            already = session.scalars(
                select(Face).where(Face.file_id == body.file_id, Face.person_id == person.id)
            ).all()
            if not any((face.embedding_ref is None and not face.bbox) for face in already):
                session.add(Face(file_id=body.file_id, person_id=person.id, bbox={}, embedding_ref=None))
                session.flush()

        _sync_single_file_person_labels(session, body.file_id, search_version=search_version)
        session.commit()
        clear_query_cache()
        TagVocabularyCache.invalidate()
        if learned:
            # 사용자 확정 얼굴을 분류기에 반영 → 미래 자동매칭 개선(학습신호).
            recompute_person_centroid(session, embeddings_root=settings.embeddings_root, person=person)
        session.refresh(person)
        return _person_response(session, person)


@router.delete("/photo-assign", status_code=204)
def remove_person_from_photo(file_id: str, person_id: int, request: Request) -> Response:
    """Remove a *manual* (boxless, embedding-less) person association from a photo.

    Detector-found faces are not touched here — those are managed via the face
    assignment endpoint. Only the hand-added "this person is in this photo" rows
    are removed."""
    database = require_state(request, "database")
    search_version = request.app.state.settings.semantic_search_version
    with database.session_factory() as session:
        # Manual = no embedding AND no box. Filter the box in Python so we stay
        # dialect-agnostic (JSON {} comparison differs across SQLite/Postgres).
        candidates = session.scalars(
            select(Face).where(
                Face.file_id == file_id,
                Face.person_id == person_id,
                Face.embedding_ref.is_(None),
            )
        ).all()
        deleted = 0
        for face in candidates:
            if not face.bbox:
                session.delete(face)
                deleted += 1
        if deleted:
            # 태그 재계산 전에 삭제를 확정해야 persons_in_file에서 빠진다
            # (안 그러면 _sync가 아직 살아있는 얼굴을 보고 태그를 되살린다).
            session.flush()
            _sync_single_file_person_labels(session, file_id, search_version=search_version)
            session.commit()
            clear_query_cache()
            TagVocabularyCache.invalidate()
    return Response(status_code=204)


@router.get("/faces/{face_id}/crop")
def face_crop(face_id: int, request: Request) -> Response:
    """Return a small local-only face crop for person-name assignment UI."""
    if Image is None:
        raise HTTPException(status_code=503, detail="pillow is required for face crop rendering")
    database = require_state(request, "database")
    settings = require_state(request, "settings")
    with database.session_factory() as session:
        row = session.execute(
            select(Face, MediaFile)
            .join(MediaFile, MediaFile.file_id == Face.file_id)
            .where(Face.id == face_id)
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="face not found")
        face, media_file = row
        asset = session.scalars(
            select(DerivedAsset)
            .where(DerivedAsset.file_id == media_file.file_id, DerivedAsset.asset_kind == "thumb")
            .order_by(DerivedAsset.id.asc())
            .limit(1)
        ).first()

    source_path = Path(media_file.current_path)
    scale_from_original = False
    if not source_path.is_file():
        if asset is None:
            raise HTTPException(status_code=404, detail="face source image missing")
        source_path = _resolve_derived_path(settings.derived_root, asset.derived_path)
        scale_from_original = True
    if not source_path.is_file():
        raise HTTPException(status_code=404, detail="face source image missing")

    try:
        ensure_heif_support()
        with Image.open(source_path) as image:
            image = image.convert("RGB")
            bbox = _scaled_face_bbox(
                face.bbox or {},
                image_size=image.size,
                original_size=(media_file.width, media_file.height),
                scale_from_original=scale_from_original,
            )
            cropped = image.crop(bbox)
            cropped.thumbnail((180, 180))
            output = BytesIO()
            cropped.save(output, format="JPEG", quality=88, optimize=True)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"face crop failed: {exc}") from exc

    return Response(
        output.getvalue(),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


import re as _re
_INTERNAL_PERSON_ID_RE = _re.compile(r"^person-\d+$", _re.IGNORECASE)


def _get_visible_person(session, person_id: int) -> Person:
    """Load a person, treating merged-away (soft-hidden) people as not found."""
    person = session.get(Person, person_id)
    if person is None or person.merged_into_id is not None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


def _person_aliases(person: Person) -> list[str]:
    """All aliases including internal person-XXXXXXXX IDs (used for search tag expansion)."""
    raw = person.aliases_json or []
    if not isinstance(raw, list):
        return []
    return _normalize_aliases([str(value) for value in raw])


def _person_display_aliases(person: Person) -> list[str]:
    """Human-readable aliases only — internal person-XXXXXXXX IDs are excluded from UI."""
    return [a for a in _person_aliases(person) if not _INTERNAL_PERSON_ID_RE.match(a)]


def _person_labels(person: Person) -> set[str]:
    labels = {person.display_name.strip()}
    labels.update(_person_aliases(person))
    return {label for label in labels if label}


def _normalize_aliases(values: list[str]) -> list[str]:
    seen: set[str] = set()
    aliases: list[str] = []
    for value in values:
        alias = " ".join(str(value).strip().split())
        folded = alias.casefold()
        if not alias or folded in seen:
            continue
        seen.add(folded)
        aliases.append(alias[:128])
    return aliases[:20]


def _validate_user_aliases(values: list[str]) -> list[str]:
    """Filter out internal cluster IDs from user-supplied alias lists."""
    return [a for a in values if not _INTERNAL_PERSON_ID_RE.match(a.strip())]


def _person_media_count(session, person_id: int) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(Face.file_id)))
            .join(MediaFile, MediaFile.file_id == Face.file_id)
            .where(Face.person_id == person_id, _active_media_predicate())
        )
        or 0
    )


def _person_face_count(session, person_id: int) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Face)
            .join(MediaFile, MediaFile.file_id == Face.file_id)
            .where(Face.person_id == person_id, _active_media_predicate())
        )
        or 0
    )


def _person_response(session, person: Person) -> PersonResponse:
    sample_faces = session.scalars(
        select(Face)
        .join(MediaFile, MediaFile.file_id == Face.file_id)
        .where(Face.person_id == person.id, _active_media_predicate())
        .limit(3)
    ).all()
    return PersonResponse(
        id=person.id,
        display_name=person.display_name,
        aliases=_person_display_aliases(person),
        face_count=_person_face_count(session, person.id),
        media_count=_person_media_count(session, person.id),
        sample_file_ids=[str(f.file_id) for f in sample_faces],
        sample_face_ids=[int(f.id) for f in sample_faces],
    )


def _active_media_predicate():
    return MediaFile.status.not_in(("missing", "replaced", "excluded"))


def _captured_at_expr():
    mtime_expr = func.datetime(MediaFile.mtime_ns / 1000000000, "unixepoch", "localtime")
    return func.coalesce(MediaFile.exif_datetime, mtime_expr, MediaFile.processed_at, MediaFile.last_seen_at)


def _normalize_merge_source_ids(source_ids: list[int], target_person_id: int) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for source_id in source_ids:
        person_id = int(source_id)
        if person_id == int(target_person_id) or person_id in seen:
            continue
        seen.add(person_id)
        normalized.append(person_id)
    return normalized


def _merge_person_aliases(target: Person, sources: list[Person]) -> list[str]:
    aliases = list(_person_aliases(target))
    for source in sources:
        aliases.append(source.display_name)
        aliases.extend(_person_aliases(source))
    return [alias for alias in _normalize_aliases(aliases) if alias.casefold() != target.display_name.casefold()]


def _sync_single_file_person_labels(session, file_id: str, *, search_version: str) -> None:
    """Re-sync all person tags for a single file after a face reassignment."""
    persons_in_file = session.scalars(
        select(Person)
        .join(Face, Face.person_id == Person.id)
        .where(Face.file_id == file_id)
        .distinct()
    ).all()
    session.execute(
        delete(Tag).where(Tag.file_id == file_id, Tag.tag_type == "person")
    )
    for person in persons_in_file:
        for label in _person_labels(person):
            session.add(Tag(file_id=file_id, tag_type="person", tag_value=label))
    media_file = session.get(MediaFile, file_id)
    if media_file:
        # 세션은 autoflush=False라, upsert_search_document가 select(Tag)로 사람을
        # 채우기 전에 방금 추가/삭제한 person 태그를 먼저 확정해야 한다(안 그러면
        # people_json이 비어 검색문서에 인물이 안 들어간다).
        session.flush()
        SemanticCatalog(session).upsert_search_document(media_file, version=search_version)


def _sync_person_search_labels(session, person: Person, *, old_labels: set[str], search_version: str) -> None:
    labels = _person_labels(person)
    affected_file_ids = [
        str(file_id)
        for file_id in session.scalars(
            select(Face.file_id).where(Face.person_id == person.id).distinct()
        )
    ]
    if not affected_file_ids:
        return

    removable = {label.casefold() for label in (old_labels | labels) if label}
    if removable:
        session.execute(
            delete(Tag).where(
                Tag.file_id.in_(affected_file_ids),
                Tag.tag_type == "person",
                func.lower(Tag.tag_value).in_(list(removable)),
            )
        )

    for file_id in affected_file_ids:
        for label in labels:
            session.add(Tag(file_id=file_id, tag_type="person", tag_value=label))
    _invalidate_search_documents(session, affected_file_ids)


def _invalidate_search_documents(session, file_ids: list[str]) -> None:
    if not file_ids:
        return
    session.execute(delete(SearchDocument).where(SearchDocument.file_id.in_(file_ids)))
    for table_name in ("search_documents_fts", "search_documents_fts_ko"):
        try:
            session.execute(
                text(f"DELETE FROM {table_name} WHERE file_id IN :file_ids").bindparams(
                    bindparam("file_ids", expanding=True)
                ),
                {"file_ids": list(file_ids)},
            )
        except Exception:
            continue


def _resolve_derived_path(derived_root: Path, derived_path: str) -> Path:
    candidate = Path(derived_path)
    if candidate.is_absolute():
        return candidate
    root = derived_root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="face source image missing") from exc
    return resolved


def _scaled_face_bbox(
    bbox: dict,
    *,
    image_size: tuple[int, int],
    original_size: tuple[Optional[int], Optional[int]],
    scale_from_original: bool,
) -> tuple[int, int, int, int]:
    image_width, image_height = image_size
    x = float(bbox.get("x") or 0)
    y = float(bbox.get("y") or 0)
    width = max(1.0, float(bbox.get("width") or image_width))
    height = max(1.0, float(bbox.get("height") or image_height))
    if scale_from_original and original_size[0] and original_size[1]:
        x *= image_width / float(original_size[0])
        width *= image_width / float(original_size[0])
        y *= image_height / float(original_size[1])
        height *= image_height / float(original_size[1])
    pad = max(width, height) * 0.38
    left = max(0, int(round(x - pad)))
    top = max(0, int(round(y - pad)))
    right = min(image_width, int(round(x + width + pad)))
    bottom = min(image_height, int(round(y + height + pad)))
    if right <= left or bottom <= top:
        return (0, 0, image_width, image_height)
    return (left, top, right, bottom)
