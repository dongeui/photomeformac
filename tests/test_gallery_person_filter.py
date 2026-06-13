"""인물 필터 콤보 옵션 소스(_list_tag_values) 회귀 가드.

콤보에는 대표 이름을 저장한 인물만 노출하고 person-000123 같은 내부 자동
ID는 제외한다. 핵심 회귀: 내부 ID('person-...')는 ASCII라 한글 이름보다
먼저 정렬되므로, 내부 ID가 LIMIT(200)을 넘으면 파이썬 사후 필터링으로는
이름이 한 명도 안 남는다. 제외는 반드시 LIMIT 이전(SQL)에 일어나야 한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.gallery import PERSON_TAG_TYPES, _list_tag_values
from app.models import annotation, asset, face, job, media, observation, person, runtime, semantic, tag  # noqa: F401
from app.models.base import Base
from app.models.media import MediaFile
from app.models.tag import Tag


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_person_options_keep_named_people_when_internal_ids_exceed_limit() -> None:
    factory = _session_factory()
    with factory() as session:
        now = datetime.now(timezone.utc)
        media_file = MediaFile(
            file_id="f1",
            current_path="/photos/a.jpg",
            filename="a.jpg",
            source_root="/photos",
            relative_path="a.jpg",
            media_kind="image",
            status="analysis_done",
            size_bytes=1,
            mtime_ns=1,
            partial_hash="h",
            first_seen_at=now,
            last_seen_at=now,
            updated_at=now,
            processed_at=now,
        )
        session.add(media_file)
        # 내부 ID 250개(LIMIT 200 초과) — 전부 'person-...'라 한글 이름보다 먼저 정렬된다
        for i in range(250):
            session.add(Tag(file_id="f1", tag_type="person", tag_value=f"person-{i:06d}"))
        # 대표 이름을 저장한 인물(한글) — 정렬상 내부 ID들보다 뒤에 온다
        for name in ("정동의", "장윤겸", "김건우"):
            session.add(Tag(file_id="f1", tag_type="person", tag_value=name))
        session.commit()

        options = _list_tag_values(session, PERSON_TAG_TYPES, exclude_internal_person=True)

    assert set(options) == {"정동의", "장윤겸", "김건우"}
    assert not any(value.startswith("person-0") for value in options)


def test_person_options_without_exclusion_still_lists_all() -> None:
    """exclude 플래그가 꺼져 있으면(기존 동작) 내부 ID도 포함된다 — place 등 타 용도 보호."""
    factory = _session_factory()
    with factory() as session:
        now = datetime.now(timezone.utc)
        session.add(
            MediaFile(
                file_id="f1", current_path="/p/a.jpg", filename="a.jpg", source_root="/p",
                relative_path="a.jpg", media_kind="image", status="analysis_done",
                size_bytes=1, mtime_ns=1, partial_hash="h",
                first_seen_at=now, last_seen_at=now, updated_at=now, processed_at=now,
            )
        )
        session.add(Tag(file_id="f1", tag_type="person", tag_value="person-000001"))
        session.add(Tag(file_id="f1", tag_type="person", tag_value="정동의"))
        session.commit()

        options = _list_tag_values(session, PERSON_TAG_TYPES)

    assert "person-000001" in options
    assert "정동의" in options
