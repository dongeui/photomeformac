"""Auto-absorb fragmented unnamed face clusters into named people.

Cold-start fragmentation: a single weak first crop spawns a new unnamed
``person-XXXXXX`` cluster, and same-person faces then stick to it (they are
closer to that fresh cluster than to the distant named centroid). The result
is that a known person's newest photos land in an unnamed cluster and drop out
of name search — recurring after every batch of new photos.

This heals it without user effort: any unnamed cluster whose centroid is highly
similar to a *named* person's centroid is merged into that person. Faces move
with origin tracking (``merged_from_person_id``) so the merge is reversible via
the existing unmerge path; the target centroid is recomputed; affected files
are re-tagged and their search documents rebuilt so the photos become
searchable under the person's name.

Conservative by design: only unnamed → named, and only above a threshold well
*above* the ingest match threshold (``face_match_threshold`` 0.363), so we never
fuse genuinely different people. The leftover low-similarity clusters stay
separate for the user to name/merge by hand.
"""

from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.face import Face
from app.models.person import Person
from app.services.processing.person_centroids import (
    person_centroid_path,
    recompute_person_centroid,
)
from app.services.processing.person_labels import reconcile_file_person_tags

logger = logging.getLogger(__name__)

# Unnamed auto-clusters are named ``person-000123``; everything else is a
# user-assigned name. Same pattern the people API uses to hide internal ids.
_INTERNAL_PERSON_ID_RE = re.compile(r"^person-\d+$", re.IGNORECASE)


def _is_named(person: Person) -> bool:
    name = (person.display_name or "").strip()
    return bool(name) and _INTERNAL_PERSON_ID_RE.match(name) is None


def _normalize(vector: list[float]) -> list[float] | None:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0 or not math.isfinite(norm):
        return None
    return [value / norm for value in vector]


def _load_centroid(embeddings_root: Path, person_id: int) -> list[float] | None:
    path = person_centroid_path(embeddings_root, person_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = payload.get("embedding") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    try:
        return _normalize([float(value) for value in raw])
    except (TypeError, ValueError):
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    # Both inputs are already unit-normalized, so cosine == dot product.
    if len(a) != len(b):
        return -1.0
    return sum(x * y for x, y in zip(a, b))


def _merged_aliases(target: Person, source: Person) -> list[str]:
    """Target aliases + the source's name/aliases, deduped, target name dropped.

    Mirrors the people API merge so unmerge can restore the source, and so
    search keeps finding the old cluster id. Capped to keep the column bounded.
    """
    out: list[str] = []
    seen: set[str] = set()
    target_name = (target.display_name or "").strip().casefold()
    candidates = list(target.aliases_json or [])
    candidates.append(source.display_name)
    candidates.extend(source.aliases_json or [])
    for value in candidates:
        alias = " ".join(str(value or "").strip().split())
        folded = alias.casefold()
        if not alias or folded in seen or folded == target_name:
            continue
        seen.add(folded)
        out.append(alias[:128])
    return out[:20]


def reconcile_unnamed_clusters(
    session: Session,
    *,
    embeddings_root: Path,
    search_version: str,
    similarity_threshold: float = 0.5,
    max_clusters: int = 200,
) -> dict[str, Any]:
    """Merge unnamed clusters into the named person they clearly belong to.

    Returns a summary dict. Commits once per merge so each absorption is atomic
    and the write lock is held only briefly (the live backend runs background
    scans concurrently). Idempotent: finds nothing once drained.
    """
    people = session.scalars(
        select(Person).where(Person.merged_into_id.is_(None))
    ).all()
    named = [
        (person, centroid)
        for person, centroid in (
            (person, _load_centroid(embeddings_root, int(person.id)))
            for person in people
            if _is_named(person)
        )
        if centroid is not None
    ]
    unnamed = [person for person in people if not _is_named(person)]
    if not named or not unnamed:
        return {"scanned": 0, "merged": 0, "faces_moved": 0, "has_more": False}

    scanned = merged = faces_moved = 0
    touched_targets = False
    for source in unnamed:
        if merged >= max_clusters:
            break
        source_centroid = _load_centroid(embeddings_root, int(source.id))
        if source_centroid is None:
            continue
        scanned += 1

        best_person: Person | None = None
        best_similarity = -1.0
        for person, centroid in named:
            similarity = _cosine(source_centroid, centroid)
            if similarity > best_similarity:
                best_similarity = similarity
                best_person = person
        if best_person is None or best_similarity < similarity_threshold:
            continue

        file_ids = [
            str(file_id)
            for file_id in session.scalars(
                select(Face.file_id).where(Face.person_id == source.id).distinct()
            )
        ]
        face_total = int(
            session.scalar(select(func.count()).select_from(Face).where(Face.person_id == source.id)) or 0
        )

        # Preserve each face's origin before reassigning, so unmerge restores
        # exactly this cluster. Then move the faces onto the named person.
        session.execute(
            update(Face)
            .where(Face.person_id == source.id, Face.merged_from_person_id.is_(None))
            .values(merged_from_person_id=source.id)
        )
        session.execute(
            update(Face).where(Face.person_id == source.id).values(person_id=best_person.id)
        )
        best_person.aliases_json = _merged_aliases(best_person, source)
        source.merged_into_id = best_person.id

        # Classifier + search must reflect the move: recompute the target
        # centroid from its now-larger face set, and rebuild each file's person
        # tags + search document so the photos surface under the person's name.
        recompute_person_centroid(session, embeddings_root=embeddings_root, person=best_person)
        for file_id in file_ids:
            try:
                reconcile_file_person_tags(
                    session, file_id=file_id, search_version=search_version
                )
            except Exception as exc:  # noqa: BLE001 - keep draining the batch
                logger.warning(
                    "cluster reconcile: file retag failed",
                    extra={"file_id": file_id, "error": str(exc)},
                )

        session.commit()
        merged += 1
        faces_moved += face_total
        touched_targets = True
        logger.info(
            "cluster reconcile: absorbed unnamed cluster into named person",
            extra={
                "source_person_id": int(source.id),
                "target_person_id": int(best_person.id),
                "similarity": round(best_similarity, 4),
                "faces": face_total,
            },
        )

    if touched_targets:
        from app.services.search.hybrid import clear_query_cache
        from app.services.search.vocab import TagVocabularyCache

        clear_query_cache()
        TagVocabularyCache.invalidate()

    return {
        "scanned": scanned,
        "merged": merged,
        "faces_moved": faces_moved,
        "has_more": merged >= max_clusters,
    }
