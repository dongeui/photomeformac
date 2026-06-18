from __future__ import annotations

import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import annotation, asset, face, job, media, observation, person, runtime, semantic, tag  # noqa: F401
from app.models.base import Base
from app.models.face import Face
from app.models.person import Person
from app.models.tag import Tag
from app.services.processing.cluster_reconcile import reconcile_unnamed_clusters
from app.services.processing.person_centroids import person_centroid_path


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _write_centroid(embeddings_root, person_id: int, vector: list[float]) -> None:
    path = person_centroid_path(embeddings_root, person_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"person_id": person_id, "embedding": vector}), encoding="utf-8")


def test_absorbs_similar_unnamed_cluster_and_leaves_distinct_one(tmp_path):
    embeddings_root = tmp_path / "embeddings"
    factory = _session_factory()
    with factory() as session:
        named = Person(id=40, display_name="박지호", aliases_json=["지호"])
        near = Person(id=600, display_name="person-000600", aliases_json=[])   # same person, fragmented
        far = Person(id=601, display_name="person-000601", aliases_json=[])    # genuinely different
        session.add_all([named, near, far])
        # faces (no embeddings on disk → centroid recompute is a no-op, fine)
        session.add(Face(file_id="a", person_id=600))
        session.add(Tag(file_id="a", tag_type="person", tag_value="person-000600"))
        session.add(Face(file_id="b", person_id=601))
        session.add(Tag(file_id="b", tag_type="person", tag_value="person-000601"))
        session.commit()

    _write_centroid(embeddings_root, 40, [1.0, 0.0, 0.0, 0.0])
    _write_centroid(embeddings_root, 600, [0.95, 0.31, 0.0, 0.0])  # cosine≈0.95 → merge
    _write_centroid(embeddings_root, 601, [0.0, 1.0, 0.0, 0.0])    # cosine 0 → keep

    with factory() as session:
        result = reconcile_unnamed_clusters(
            session,
            embeddings_root=embeddings_root,
            search_version="search-v4",
            similarity_threshold=0.5,
        )

    assert result["merged"] == 1
    assert result["faces_moved"] == 1

    with factory() as session:
        near = session.get(Person, 600)
        far = session.get(Person, 601)
        assert near.merged_into_id == 40          # absorbed into named
        assert far.merged_into_id is None          # distinct cluster untouched

        # face moved onto the named person, with origin tracked for unmerge
        face_a = session.scalars(select(Face).where(Face.file_id == "a")).one()
        assert face_a.person_id == 40
        assert face_a.merged_from_person_id == 600

        # file 'a' now searchable under the person's labels (slug kept for lookup)
        tags_a = set(
            session.scalars(select(Tag.tag_value).where(Tag.file_id == "a", Tag.tag_type == "person"))
        )
        assert {"박지호", "지호"} <= tags_a
        # the distinct cluster's file is unchanged
        face_b = session.scalars(select(Face).where(Face.file_id == "b")).one()
        assert face_b.person_id == 601


def test_is_idempotent_and_respects_threshold(tmp_path):
    embeddings_root = tmp_path / "embeddings"
    factory = _session_factory()
    with factory() as session:
        session.add_all([
            Person(id=40, display_name="박지호", aliases_json=[]),
            Person(id=600, display_name="person-000600", aliases_json=[]),
        ])
        session.add(Face(file_id="a", person_id=600))
        session.commit()

    _write_centroid(embeddings_root, 40, [1.0, 0.0, 0.0, 0.0])
    _write_centroid(embeddings_root, 600, [0.6, 0.8, 0.0, 0.0])  # cosine 0.6

    # threshold above the similarity → no merge
    with factory() as session:
        high = reconcile_unnamed_clusters(
            session, embeddings_root=embeddings_root, search_version="v", similarity_threshold=0.7
        )
    assert high["merged"] == 0

    # threshold below the similarity → merges once, then nothing left
    with factory() as session:
        first = reconcile_unnamed_clusters(
            session, embeddings_root=embeddings_root, search_version="v", similarity_threshold=0.5
        )
    assert first["merged"] == 1
    with factory() as session:
        second = reconcile_unnamed_clusters(
            session, embeddings_root=embeddings_root, search_version="v", similarity_threshold=0.5
        )
    assert second["merged"] == 0


def test_never_merges_two_unnamed_clusters(tmp_path):
    embeddings_root = tmp_path / "embeddings"
    factory = _session_factory()
    with factory() as session:
        session.add_all([
            Person(id=600, display_name="person-000600", aliases_json=[]),
            Person(id=601, display_name="person-000601", aliases_json=[]),
        ])
        session.add(Face(file_id="a", person_id=600))
        session.add(Face(file_id="b", person_id=601))
        session.commit()

    # near-identical centroids, but BOTH unnamed → must not fuse (no named target)
    _write_centroid(embeddings_root, 600, [1.0, 0.0, 0.0, 0.0])
    _write_centroid(embeddings_root, 601, [0.99, 0.14, 0.0, 0.0])

    with factory() as session:
        result = reconcile_unnamed_clusters(
            session, embeddings_root=embeddings_root, search_version="v", similarity_threshold=0.5
        )
    assert result["merged"] == 0
    with factory() as session:
        assert session.get(Person, 600).merged_into_id is None
        assert session.get(Person, 601).merged_into_id is None
