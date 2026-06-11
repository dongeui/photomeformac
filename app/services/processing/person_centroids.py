"""Person centroid file maintenance shared by the pipeline and people API.

얼굴 분류는 사람별 센트로이드(평균 임베딩) 파일과의 코사인 유사도로 동작한다.
인물 병합/병합 해제 직후에는 얼굴 소속이 바뀌므로, 영향을 받은 사람의
센트로이드를 현재 소속 얼굴들의 저장된 임베딩에서 다시 계산해 두어야
같은 사람의 새 사진이 병합 결과대로 분류된다.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.face import Face
from app.models.person import Person

logger = logging.getLogger(__name__)


def person_centroid_path(embeddings_root: Path, person_id: int) -> Path:
    return embeddings_root / "people" / "v1" / f"person-{person_id:06d}.json"


def recompute_person_centroid(session: Session, *, embeddings_root: Path, person: Person) -> bool:
    """Rebuild a person's centroid from the stored embeddings of its current faces.

    Returns True if the centroid file was rewritten. Faces whose embedding files
    are missing or unreadable are skipped; if none are readable the existing
    centroid file is left untouched (legacy faces may predate embedding storage).
    """
    try:
        refs = session.scalars(
            select(Face.embedding_ref).where(
                Face.person_id == person.id, Face.embedding_ref.is_not(None)
            )
        ).all()
        vectors = [
            vector
            for vector in (_read_face_embedding(embeddings_root, str(ref)) for ref in refs)
            if vector is not None
        ]
        if not vectors:
            return False
        dimension = len(vectors[0])
        normalized = [
            unit
            for unit in (_normalize(vector) for vector in vectors if len(vector) == dimension)
            if unit is not None
        ]
        if not normalized:
            return False
        mean = [sum(vector[index] for vector in normalized) / len(normalized) for index in range(dimension)]
        centroid = _normalize(mean)
        if centroid is None:
            return False
        _write_json_atomic(
            person_centroid_path(embeddings_root, int(person.id)),
            {
                "person_id": int(person.id),
                "person": person.display_name,
                "sample_count": len(normalized),
                "embedding": centroid,
                "updated_at": datetime.utcnow().isoformat(),
            },
        )
        return True
    except Exception as exc:
        logger.warning(
            "person centroid recompute failed",
            extra={"person_id": int(person.id), "error": str(exc)},
        )
        return False


def _read_face_embedding(embeddings_root: Path, ref: str) -> list[float] | None:
    path = Path(ref)
    if not path.is_absolute():
        try:
            path = embeddings_root / path.relative_to("embeddings")
        except ValueError:
            path = embeddings_root / path
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    raw = payload.get("embedding") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    try:
        return [float(value) for value in raw]
    except (TypeError, ValueError):
        return None


def _normalize(vector: list[float]) -> list[float] | None:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0 or not math.isfinite(norm):
        return None
    return [value / norm for value in vector]


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
            tmp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(path)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
