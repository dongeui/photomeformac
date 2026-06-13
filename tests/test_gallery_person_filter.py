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

from app.api.gallery import _list_named_person_display_names
from app.models import annotation, asset, face, job, media, observation, person, runtime, semantic, tag  # noqa: F401
from app.models.base import Base
from app.models.media import MediaFile
from app.models.person import Person
from app.models.tag import Tag


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


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
        session.add(Person(display_name="정이한", aliases_json=["이한", "아들", "꼬맹이"]))
        for value in ("정이한", "이한", "아들", "꼬맹이"):
            session.add(Tag(file_id="f1", tag_type="person", tag_value=value))
        session.commit()

        options = _list_named_person_display_names(session)

    assert options == ["정이한"]  # 애칭(이한·아들·꼬맹이)은 빠진다


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
        session.add(Person(display_name="김건우"))
        session.add(Tag(file_id="f1", tag_type="person", tag_value="김건우"))
        session.commit()

        options = _list_named_person_display_names(session)

    assert options == ["김건우"]


def test_combo_orders_by_name_case_insensitive() -> None:
    factory = _session_factory()
    with factory() as session:
        _add_media(session)
        for name in ("정동의", "김건우", "장윤겸"):
            session.add(Person(display_name=name))
            session.add(Tag(file_id="f1", tag_type="person", tag_value=name))
        session.commit()

        options = _list_named_person_display_names(session)

    assert options == sorted(options, key=str.lower)
    assert set(options) == {"정동의", "김건우", "장윤겸"}
