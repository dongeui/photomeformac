from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import annotation, asset, face, job, media, observation, person, runtime, semantic, tag  # noqa: F401
from app.models.base import Base
from app.models.face import Face
from app.models.person import Person
from app.models.tag import Tag
from app.services.processing.person_labels import (
    find_files_needing_person_label_sync,
    person_label_values,
    reconcile_file_person_tags,
)


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_search_labels_dedupes_display_name_and_normalizes():
    person = Person(display_name="박지호", aliases_json=["지호", "박지호", "  막둥이 ", "지호"])
    assert person.search_labels() == ["박지호", "지호", "막둥이"]
    # display-only people (unnamed clusters) yield a single label
    assert Person(display_name="person-000600", aliases_json=[]).search_labels() == ["person-000600"]


def test_person_label_values_union_across_people():
    a = Person(display_name="박지호", aliases_json=["지호"])
    b = Person(display_name="person-000600", aliases_json=[])
    assert person_label_values([a, b]) == ["박지호", "지호", "person-000600"]


def test_finder_flags_files_missing_alias_and_reconcile_backfills():
    factory = _session_factory()
    with factory() as session:
        named = Person(id=40, display_name="박지호", aliases_json=["지호", "막둥이"])
        cluster = Person(id=600, display_name="person-000600", aliases_json=[])
        session.add_all([named, cluster])
        session.flush()

        # old-style file: tagged display name only (missing aliases) → drift
        session.add(Face(file_id="old", person_id=40))
        session.add(Tag(file_id="old", tag_type="person", tag_value="박지호"))
        # fully-synced file: already carries all labels → no drift
        session.add(Face(file_id="full", person_id=40))
        for label in ("박지호", "지호", "막둥이"):
            session.add(Tag(file_id="full", tag_type="person", tag_value=label))
        # display-only cluster file → never needs a fix
        session.add(Face(file_id="cluster", person_id=600))
        session.add(Tag(file_id="cluster", tag_type="person", tag_value="person-000600"))
        session.commit()

        candidates = find_files_needing_person_label_sync(session, limit=100)
        assert candidates == ["old"]

        assert reconcile_file_person_tags(session, file_id="old", search_version="search-v4") is True
        session.commit()

        old_tags = set(
            session.scalars(
                select(Tag.tag_value).where(Tag.file_id == "old", Tag.tag_type == "person")
            )
        )
        assert old_tags == {"박지호", "지호", "막둥이"}

        # idempotent: nothing left to do
        assert find_files_needing_person_label_sync(session, limit=100) == []
        assert reconcile_file_person_tags(session, file_id="old", search_version="search-v4") is False


def test_reconcile_unions_multiple_people_and_drops_stale_tags():
    factory = _session_factory()
    with factory() as session:
        a = Person(id=40, display_name="박지호", aliases_json=["지호"])
        b = Person(id=41, display_name="이서연", aliases_json=["서연"])
        session.add_all([a, b])
        session.add_all([Face(file_id="grp", person_id=40), Face(file_id="grp", person_id=41)])
        # a stale person tag for someone no longer in the file must be removed
        session.add(Tag(file_id="grp", tag_type="person", tag_value="없는사람"))
        session.commit()

        assert reconcile_file_person_tags(session, file_id="grp", search_version="search-v4") is True
        session.commit()

        tags = set(
            session.scalars(
                select(Tag.tag_value).where(Tag.file_id == "grp", Tag.tag_type == "person")
            )
        )
        assert tags == {"박지호", "지호", "이서연", "서연"}
