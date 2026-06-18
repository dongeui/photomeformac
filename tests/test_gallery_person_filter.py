"""인물 필터 콤보 옵션 소스(_list_named_person_display_names) 회귀 가드.

콤보에는 대표 이름(Person.display_name)만 노출한다:
  - 애칭(alias)은 같은 'person' 태그로 저장되지만 콤보에서는 제외
  - person-000123 같은 내부 자동 ID 제외
  - 인물 태그가 달린(사진이 있는) 사람만 노출
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.gallery import _build_gallery_ids_query, _list_named_person_display_names
from app.models import annotation, asset, face, job, media, observation, person, runtime, semantic, tag  # noqa: F401
from app.models.base import Base
from app.models.media import MediaFile
from app.models.person import Person
from app.models.semantic import SearchDocument
from app.models.tag import Tag


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _add_image(session, file_id: str, *, with_search_doc: bool) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        MediaFile(
            file_id=file_id, current_path=f"/p/{file_id}.jpg", filename=f"{file_id}.jpg",
            source_root="/p", relative_path=f"{file_id}.jpg", media_kind="image",
            status="thumb_done", size_bytes=1, mtime_ns=1, partial_hash="h",
            first_seen_at=now, last_seen_at=now, updated_at=now, processed_at=now,
        )
    )
    if with_search_doc:
        session.add(SearchDocument(
            file_id=file_id, version="search-v4", source_updated_at=now, search_text=file_id,
        ))


def test_gallery_gate_hides_unanalyzed_photos_when_required() -> None:
    factory = _session_factory()
    with factory() as session:
        _add_image(session, "analyzed", with_search_doc=True)
        _add_image(session, "pending", with_search_doc=False)
        session.commit()

        # 가시성 계약 ON: 분석 완료(search_document 있음)만 노출
        gated = set(session.scalars(_build_gallery_ids_query(
            media_type=None, date_from=None, date_to=None, person=None, place=None,
            query=None, require_analysis_complete=True,
        )))
        assert gated == {"analyzed"}

        # OFF(기존 동작): 둘 다 노출
        ungated = set(session.scalars(_build_gallery_ids_query(
            media_type=None, date_from=None, date_to=None, person=None, place=None,
            query=None, require_analysis_complete=False,
        )))
        assert ungated == {"analyzed", "pending"}


def test_gallery_gate_does_not_filter_explicit_search_result_ids() -> None:
    factory = _session_factory()
    with factory() as session:
        _add_image(session, "pending", with_search_doc=False)
        session.commit()
        # 검색 결과(file_ids 지정)는 이미 search_document 출신이므로 게이트 미적용 —
        # 명시된 id는 그대로 반환되어야 한다(이중 필터로 결과가 사라지면 안 됨).
        result = set(session.scalars(_build_gallery_ids_query(
            media_type=None, date_from=None, date_to=None, person=None, place=None,
            query=None, file_ids=["pending"], require_analysis_complete=True,
        )))
        assert result == {"pending"}


def _add_media(session) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        MediaFile(
            file_id="f1", current_path="/p/a.jpg", filename="a.jpg", source_root="/p",
            relative_path="a.jpg", media_kind="image", status="analysis_done",
            size_bytes=1, mtime_ns=1, partial_hash="h",
            first_seen_at=now, last_seen_at=now, updated_at=now, processed_at=now,
        )
    )


def test_combo_lists_only_representative_names_not_aliases() -> None:
    factory = _session_factory()
    with factory() as session:
        _add_media(session)
        # 대표 이름 + 애칭들이 전부 같은 'person' 태그로 저장돼 있다
        session.add(Person(display_name="박지호", aliases_json=["지호", "꼬마", "깜찍이"]))
        for value in ("박지호", "지호", "꼬마", "깜찍이"):
            session.add(Tag(file_id="f1", tag_type="person", tag_value=value))
        session.commit()

        options = _list_named_person_display_names(session)

    assert options == ["박지호"]  # 애칭(지호·꼬마·깜찍이)은 빠진다


def test_combo_excludes_internal_ids_and_orphan_named_people() -> None:
    factory = _session_factory()
    with factory() as session:
        _add_media(session)
        # 내부 ID 인물(이름 미저장)
        session.add(Person(display_name="person-000001"))
        session.add(Tag(file_id="f1", tag_type="person", tag_value="person-000001"))
        # 이름은 있으나 사진(태그)이 없는 인물 → 콤보에 넣어도 결과 0이므로 제외
        session.add(Person(display_name="유령이름"))
        # 정상: 이름 + 태그 둘 다 있음
        session.add(Person(display_name="최유진"))
        session.add(Tag(file_id="f1", tag_type="person", tag_value="최유진"))
        session.commit()

        options = _list_named_person_display_names(session)

    assert options == ["최유진"]


def test_combo_orders_by_name_case_insensitive() -> None:
    factory = _session_factory()
    with factory() as session:
        _add_media(session)
        for name in ("김민준", "최유진", "이서연"):
            session.add(Person(display_name=name))
            session.add(Tag(file_id="f1", tag_type="person", tag_value=name))
        session.commit()

        options = _list_named_person_display_names(session)

    assert options == sorted(options, key=str.lower)
    assert set(options) == {"김민준", "최유진", "이서연"}
