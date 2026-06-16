"""Person label tags shared by pipeline ingest and library reconciliation.

A file's ``person`` tags must list every label (display name + aliases) of the
people whose faces it contains. The pipeline writes these on ingest; the
reconciliation pass back-fills files indexed *before* alias-tagging existed, so
name searches that expand to "<name> + <alias>" (``require_all_persons``) don't
drop already-indexed photos. Single source of truth for both paths.
"""

from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.face import Face
from app.models.media import MediaFile
from app.models.person import Person
from app.models.tag import Tag

logger = logging.getLogger(__name__)

PERSON_TAG_TYPE = "person"


def person_label_values(persons: Iterable[Person]) -> list[str]:
    """Deduped (case-insensitive) union of search labels across ``persons``.

    Preserves first-seen order. This is exactly what a file's person tags
    should be when it contains faces of those people.
    """
    seen: set[str] = set()
    values: list[str] = []
    for person in persons:
        for label in person.search_labels():
            folded = label.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            values.append(label)
    return values


def _persons_in_file(session: Session, file_id: str) -> list[Person]:
    return list(
        session.scalars(
            select(Person)
            .join(Face, Face.person_id == Person.id)
            .where(Face.file_id == file_id)
            .distinct()
        )
    )


def reconcile_file_person_tags(
    session: Session,
    *,
    file_id: str,
    search_version: str,
    semantic_catalog=None,
) -> bool:
    """Set ``file_id``'s person tags to the full label set of its people.

    Rebuilds the search document (and FTS) when tags change. Returns True if
    anything was rewritten. Mirrors ``people._sync_single_file_person_labels``.
    """
    persons = _persons_in_file(session, file_id)
    expected = person_label_values(persons)
    existing = set(
        session.scalars(
            select(Tag.tag_value).where(
                Tag.file_id == file_id, Tag.tag_type == PERSON_TAG_TYPE
            )
        )
    )
    if existing == set(expected):
        return False
    session.execute(
        delete(Tag).where(Tag.file_id == file_id, Tag.tag_type == PERSON_TAG_TYPE)
    )
    for label in expected:
        session.add(Tag(file_id=file_id, tag_type=PERSON_TAG_TYPE, tag_value=label))
    media_file = session.get(MediaFile, file_id)
    if media_file is not None:
        if semantic_catalog is None:
            from app.services.semantic import SemanticCatalog

            semantic_catalog = SemanticCatalog(session)
        # autoflush=False라 upsert가 select(Tag)로 사람을 읽기 전에 방금 쓴 태그를
        # 먼저 확정해야 people_json이 비지 않는다.
        session.flush()
        semantic_catalog.upsert_search_document(media_file, version=search_version)
    return True


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def find_files_needing_person_label_sync(session: Session, *, limit: int) -> list[str]:
    """File ids whose person tags are missing ≥1 expected label, bounded to ``limit``.

    Only visible persons that add labels beyond their display name can drift —
    the old ingest path always wrote the display name, so display-only people
    (e.g. unnamed ``person-XXXXXX`` clusters with no aliases) never need a fix.
    """
    persons = session.scalars(
        select(Person).where(Person.merged_into_id.is_(None))
    ).all()
    candidates: list[str] = []
    seen: set[str] = set()
    for person in persons:
        labels = person.search_labels()
        if len(labels) <= 1:
            continue
        person_file_ids = [
            str(fid)
            for fid in session.scalars(
                select(Face.file_id).where(Face.person_id == person.id).distinct()
            )
        ]
        if not person_file_ids:
            continue
        for label in labels:
            folded = label.casefold()
            for chunk in _chunks(person_file_ids, 800):
                having = set(
                    session.scalars(
                        select(Tag.file_id).where(
                            Tag.file_id.in_(chunk),
                            Tag.tag_type == PERSON_TAG_TYPE,
                            func.lower(Tag.tag_value) == folded,
                        )
                    )
                )
                for fid in chunk:
                    if fid not in having and fid not in seen:
                        seen.add(fid)
                        candidates.append(fid)
                        if len(candidates) >= limit:
                            return candidates
    return candidates
