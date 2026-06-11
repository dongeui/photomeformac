"""Person management API — list, rename, and query face clusters."""

from __future__ import annotations

from html import escape
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import bindparam, delete, func, or_, select, text, update

from app.api.deps import require_state
from app.models.asset import DerivedAsset
from app.models.face import Face
from app.models.media import MediaFile
from app.models.person import Person
from app.models.semantic import SearchDocument
from app.models.tag import Tag
from app.services.image_decode import ensure_heif_support
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
  if(n===0){cnt.textContent='0개 선택됨';}
  else{cnt.innerHTML=n+'개 선택됨 · 남는 그룹: <b>'+escapeHtml(nameOf(mergeOrder[0]))+'</b>';}
  var btn=document.getElementById('merge-btn');
  btn.disabled=n<2;
  btn.textContent=n>=2?(n-1)+'개를 '+'"'+nameOf(mergeOrder[0])+'"(으)로 병합':'선택 병합 (첫 선택이 남음)';
}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
async function savePerson(id){
  var row=rowOf(id);
  if(!row){return;}
  var name=row.querySelector('.pm-name').value.trim();
  var aliases=row.querySelector('.pm-aliases').value.split(',').map(function(s){return s.trim();}).filter(Boolean);
  var r=await fetch('/people/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({display_name:name,aliases:aliases})});
  if(r.ok){location.reload();}else{var m='저장 실패';try{m=(await r.json()).detail||m;}catch(e){}alert(m);}
}
function onNameKey(e,id){if(e.isComposing||e.keyCode===229){return;}if(e.key==='Enter'){e.preventDefault();savePerson(id);}}
async function mergeSelected(){
  if(mergeOrder.length<2){return;}
  var target=mergeOrder[0];
  var sources=mergeOrder.slice(1);
  if(!confirm(sources.length+'개 그룹을 "'+nameOf(target)+'"(으)로 병합할까요?\\n선택한 다른 그룹은 사라지고 되돌릴 수 없습니다.')){return;}
  var r=await fetch('/people/merge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_person_id:target,source_person_ids:sources})});
  if(r.ok){location.reload();}else{var m='병합 실패';try{m=(await r.json()).detail||m;}catch(e){}alert(m);}
}
function filterRows(q){q=q.trim().toLowerCase();[].slice.call(document.querySelectorAll('.row')).forEach(function(r){r.style.display=(!q||(r.dataset.search||'').indexOf(q)>=0)?'':'none';});}
async function unmergePerson(targetId,sourceId){
  if(!confirm('이 사람을 병합에서 분리할까요?\\n원래 이름·애칭과 사진이 복원됩니다.')){return;}
  var r=await fetch('/people/'+targetId+'/unmerge/'+sourceId,{method:'POST'});
  if(r.ok){location.reload();}else{var m='분리 실패';try{m=(await r.json()).detail||m;}catch(e){}alert(m);}
}
"""


def _render_people_manage_html(people: list[dict]) -> str:
    if not people:
        rows_html = '<div class="empty">5회 이상 감지된 얼굴 그룹이 아직 없습니다.<br>이미지 AI 분석이 끝나면 여기에서 이름을 붙일 수 있어요.</div>'
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
                f'<img src="/people/faces/{int(face_id)}/crop" loading="lazy" decoding="async" alt="대표 얼굴">'
                if face_id is not None
                else "얼굴"
            )
            badge = (
                '<span class="badge unnamed">이름 필요</span>'
                if is_unnamed
                else '<span class="badge named">이름 있음</span>'
            )
            search_attr = escape((dn + " " + " ".join(aliases)).lower())
            placeholder = "대표 이름 입력" if is_unnamed else "이름"
            merged_sources = p.get("merged_sources") or []
            merged_chips = "".join(
                f'<span class="merged-chip">{escape(str(m["label"]))}'
                f'<button type="button" class="merged-undo" onclick="unmergePerson({pid},{int(m["id"])})" title="병합 해제 (이 사람 분리)">↩</button></span>'
                for m in merged_sources
            )
            merged_row = f'<div class="mergedrow">합쳐짐: {merged_chips}</div>' if merged_chips else ""
            parts.append(
                f'''
        <div class="row{' unnamed' if is_unnamed else ''}" data-pid="{pid}" data-search="{search_attr}">
          <input type="checkbox" class="pm-merge" data-pid="{pid}" onchange="onMergeChange(this)" aria-label="병합 선택">
          <a class="face" href="{gallery_href}" title="이 사람 사진 보기">{face_inner}</a>
          <div class="meta">
            <div class="titlerow">
              <input class="pm-name" value="{escape(name_val)}" placeholder="{placeholder}" aria-label="대표 이름" onkeydown="onNameKey(event,{pid})">
              {badge}
              <span class="keep-tag">병합 시 남음</span>
              <button class="btn" onclick="savePerson({pid})">저장</button>
            </div>
            <div class="aliasrow">
              <input class="pm-aliases" value="{escape(', '.join(aliases))}" placeholder="애칭 (쉼표로 구분)" aria-label="애칭" onkeydown="onNameKey(event,{pid})">
            </div>
            {merged_row}
          </div>
          <a class="count" href="{gallery_href}">{int(p.get('media_count') or 0)}장 · 얼굴 {int(p.get('face_count') or 0)}회</a>
        </div>'''
            )
        rows_html = "".join(parts)

    total = len(people)
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>사람 정리 · Photome</title>
<style>{_PEOPLE_MANAGE_CSS}</style>
</head>
<body>
  <div class="layout">
    <aside class="sidebar">
      <div class="side-brand">Photome</div>
      <a class="side-item" href="/gallery">모든 사진</a>
      <a class="side-item active" href="/people/manage">사람</a>
      <a class="side-item" href="/dashboard">설정</a>
    </aside>
    <section class="content">
      <div class="hdr">
        <h1>사람 정리</h1>
        <span class="sub">반복해서 나온 얼굴에 이름·애칭을 붙이세요 · {total}명</span>
        <input class="search" placeholder="이름·애칭 검색" oninput="filterRows(this.value)" aria-label="이름·애칭 검색">
      </div>
      <div class="list">
        <div class="hint">대표 얼굴이나 장수를 누르면 그 사람의 사진을 모아 봅니다. 비슷한 그룹은 선택해 병합하세요.</div>
        {rows_html}
      </div>
      <div class="footer">
        <span id="merge-count">0개 선택됨</span>
        <span class="sp"></span>
        <button class="btn danger" id="merge-btn" onclick="mergeSelected()" disabled>선택 병합 (첫 선택이 남음)</button>
      </div>
    </section>
  </div>
  <script>{_PEOPLE_MANAGE_JS}</script>
</body>
</html>"""


@router.get("/manage", response_class=HTMLResponse)
def people_manage_page(request: Request) -> HTMLResponse:
    database = require_state(request, "database")
    active = MediaFile.status.not_in(("missing", "replaced", "excluded"))
    candidate = or_(
        func.count(Face.id).filter(active) >= 5,
        Person.display_name.not_like("person-%"),
    )
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
            .having(candidate)
            .order_by(func.count(Face.id).filter(active).desc(), Person.id.asc())
            .limit(1000)
        ).all()
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
    return HTMLResponse(_render_people_manage_html(people))


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
        face.person_id = body.person_id
        # 수동 재할당은 새로운 확정 소속이므로 병합 origin 추적을 끊는다
        # (이후 unmerge가 이 얼굴을 다시 끌고 가지 않도록).
        face.merged_from_person_id = None
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
