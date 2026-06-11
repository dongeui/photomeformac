"""Derived asset registry and media catalog operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence
import unicodedata
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.contracts import (
    DerivedAssetKind,
    DerivedAssetLocation,
    FileIdentity,
    FileScanRecord,
    MediaFaceInput,
    MediaMetadata,
    MediaTagInput,
)
from app.services.metadata.service import _json_safe
from app.models.asset import DerivedAsset
from app.models.face import Face
from app.models.media import MediaFile
from app.models.observation import ScanObservation
from app.models.person import Person
from app.models.semantic import MediaEmbedding
from app.models.tag import Tag


@dataclass(frozen=True)
class CatalogChange:
    file_id: str
    action: str


@dataclass(frozen=True)
class ObservationDecision:
    observation: ScanObservation
    ready: bool


def build_derived_asset_location(
    derived_root: Path,
    asset_kind: DerivedAssetKind,
    file_id: str,
    *,
    version: str = "v1",
    suffix: str = ".jpg",
) -> DerivedAssetLocation:
    shard = file_id[:2] if len(file_id) >= 2 else "xx"
    relative_path = Path(asset_kind.value) / version / shard / f"{file_id}{suffix}"
    return DerivedAssetLocation(
        kind=asset_kind,
        version=version,
        relative_path=relative_path,
        absolute_path=derived_root / relative_path,
    )


class MediaCatalog:
    def __init__(self, session: Session) -> None:
        self._session = session

    def observe_scan(
        self,
        scan_record: FileScanRecord,
        *,
        now: datetime,
        stability_window_seconds: int,
    ) -> ObservationDecision:
        observation = self._session.execute(
            select(ScanObservation).where(ScanObservation.current_path == str(scan_record.path))
        ).scalar_one_or_none()
        ready = False
        is_stable = self._is_scan_record_stable(
            scan_record,
            now=now,
            stability_window_seconds=stability_window_seconds,
        )

        if observation is None:
            stable_after_at = now + self._stability_window(stability_window_seconds)
            status = "waiting_stable"
            if is_stable:
                stable_after_at = now
                status = "ready"
                ready = True
            observation = ScanObservation(
                source_root=str(scan_record.source_root),
                current_path=str(scan_record.path),
                relative_path=str(scan_record.relative_path),
                media_kind=scan_record.media_kind.value,
                status=status,
                size_bytes=scan_record.size_bytes,
                mtime_ns=scan_record.mtime_ns,
                first_seen_at=now,
                last_seen_at=now,
                stable_after_at=stable_after_at,
                error_stage=None,
                error_message=None,
                error_count=0,
                updated_at=now,
            )
            self._session.add(observation)
        else:
            changed = (
                observation.size_bytes != scan_record.size_bytes
                or observation.mtime_ns != scan_record.mtime_ns
                or observation.media_kind != scan_record.media_kind.value
                or observation.source_root != str(scan_record.source_root)
                or observation.relative_path != str(scan_record.relative_path)
            )
            observation.source_root = str(scan_record.source_root)
            observation.relative_path = str(scan_record.relative_path)
            observation.media_kind = scan_record.media_kind.value
            observation.size_bytes = scan_record.size_bytes
            observation.mtime_ns = scan_record.mtime_ns
            observation.last_seen_at = now
            observation.updated_at = now
            if changed or observation.status == "error":
                observation.stable_after_at = now if is_stable else now + self._stability_window(stability_window_seconds)
                observation.status = "ready" if is_stable else "waiting_stable"
                observation.error_stage = None
                observation.error_message = None
                ready = is_stable
            elif observation.status == "waiting_stable" and observation.stable_after_at is not None and now >= observation.stable_after_at:
                observation.status = "ready"
                observation.error_stage = None
                observation.error_message = None
                ready = True
            elif observation.status == "ready":
                ready = True

        if observation.status == "ready":
            ready = True
        elif observation.status == "waiting_stable" and observation.stable_after_at is not None and now >= observation.stable_after_at:
            observation.status = "ready"
            observation.error_stage = None
            observation.error_message = None
            ready = True

        self._session.flush()
        return ObservationDecision(observation=observation, ready=ready)

    def mark_observation_error(
        self,
        scan_record: FileScanRecord,
        *,
        stage: str,
        message: str,
        now: datetime,
        stability_window_seconds: int,
    ) -> None:
        decision = self.observe_scan(
            scan_record,
            now=now,
            stability_window_seconds=stability_window_seconds,
        )
        observation = decision.observation
        observation.status = "error"
        observation.error_stage = stage
        observation.error_message = message[:2048]
        observation.error_count = (observation.error_count or 0) + 1
        observation.updated_at = now
        self._session.flush()

    def upsert_media_file(
        self,
        scan_record: FileScanRecord,
        identity: FileIdentity,
        metadata: MediaMetadata | None,
        *,
        now: datetime,
    ) -> CatalogChange:
        effective_media_kind = identity.media_kind
        if effective_media_kind.value == "unknown" and metadata is not None and metadata.kind.value != "unknown":
            effective_media_kind = metadata.kind
        media_file = self._session.get(MediaFile, identity.file_id)
        action = "created"

        if media_file is None:
            self._mark_replaced_rows_for_path(scan_record.path, identity.file_id, now)
            media_file = MediaFile(
                file_id=identity.file_id,
                current_path=str(scan_record.path),
                filename=scan_record.path.name,
                source_root=str(scan_record.source_root),
                relative_path=str(scan_record.relative_path),
                media_kind=effective_media_kind.value,
                status="metadata_done",
                size_bytes=identity.size_bytes,
                mtime_ns=identity.mtime_ns,
                partial_hash=identity.partial_hash,
                content_hash=None,
                fingerprint_version=identity.fingerprint_version,
                width=metadata.width if metadata else None,
                height=metadata.height if metadata else None,
                duration_seconds=metadata.duration_seconds if metadata else None,
                codec_name=metadata.codec_name if metadata else None,
                mime_type=metadata.mime_type if metadata else None,
                exif_datetime=metadata.captured_at if metadata else None,
                metadata_json=_json_safe(metadata.extra) if metadata else None,
                stable_after_at=None,
                error_stage=None,
                error_message=None,
                error_count=0,
                processed_at=now,
                first_seen_at=now,
                last_seen_at=now,
                updated_at=now,
            )
            self._session.add(media_file)
        else:
            previous_status = media_file.status
            content_changed = (
                media_file.size_bytes != identity.size_bytes
                or media_file.mtime_ns != identity.mtime_ns
                or media_file.partial_hash != identity.partial_hash
                or media_file.fingerprint_version != identity.fingerprint_version
                or media_file.media_kind != effective_media_kind.value
            )
            action = "refreshed"
            if media_file.current_path != str(scan_record.path):
                action = "moved"
            elif media_file.size_bytes != identity.size_bytes or media_file.mtime_ns != identity.mtime_ns:
                action = "updated"

            media_file.current_path = str(scan_record.path)
            media_file.filename = scan_record.path.name
            media_file.source_root = str(scan_record.source_root)
            media_file.relative_path = str(scan_record.relative_path)
            media_file.media_kind = effective_media_kind.value
            media_file.size_bytes = identity.size_bytes
            media_file.mtime_ns = identity.mtime_ns
            media_file.partial_hash = identity.partial_hash
            media_file.fingerprint_version = identity.fingerprint_version
            if metadata is not None:
                media_file.width = metadata.width
                media_file.height = metadata.height
                media_file.duration_seconds = metadata.duration_seconds
                media_file.codec_name = metadata.codec_name
                media_file.mime_type = metadata.mime_type
                media_file.exif_datetime = metadata.captured_at
                media_file.metadata_json = _json_safe(metadata.extra)
            media_file.error_stage = None
            media_file.error_message = None
            media_file.last_seen_at = now
            media_file.updated_at = now
            if content_changed or previous_status not in {"thumb_done", "analysis_done"}:
                media_file.status = "metadata_done"
            else:
                media_file.status = previous_status
            media_file.processed_at = now

        self._session.flush()
        return CatalogChange(file_id=identity.file_id, action=action)

    def replace_tags(self, file_id: str, tags: Sequence[MediaTagInput]) -> list[Tag]:
        media_file = self._require_media_file(file_id)
        normalized_tags = self._normalize_tags(tags)

        media_file.tags.clear()
        persisted_tags = [
            Tag(
                tag_type=tag.tag_type,
                tag_value=tag.tag_value,
            )
            for tag in normalized_tags
        ]
        media_file.tags.extend(persisted_tags)
        media_file.updated_at = datetime.utcnow()
        self._session.flush()
        return persisted_tags

    def replace_tags_for_types(
        self,
        file_id: str,
        managed_tag_types: Sequence[str],
        tags: Sequence[MediaTagInput],
    ) -> list[Tag]:
        media_file = self._require_media_file(file_id)
        managed_types = {tag_type.strip().lower() for tag_type in managed_tag_types if tag_type.strip()}
        normalized_tags = self._normalize_tags(tags)

        retained_tags = [
            tag
            for tag in media_file.tags
            if tag.tag_type.lower() not in managed_types
        ]
        media_file.tags[:] = retained_tags

        persisted_tags = [
            Tag(
                tag_type=tag.tag_type,
                tag_value=tag.tag_value,
            )
            for tag in normalized_tags
        ]
        media_file.tags.extend(persisted_tags)
        media_file.updated_at = datetime.utcnow()
        self._session.flush()
        return persisted_tags

    def upsert_tags(self, file_id: str, tags: Sequence[MediaTagInput]) -> list[Tag]:
        return self.replace_tags(file_id, tags)

    def upsert_tags_for_types(
        self,
        file_id: str,
        managed_tag_types: Sequence[str],
        tags: Sequence[MediaTagInput],
    ) -> list[Tag]:
        return self.replace_tags_for_types(file_id, managed_tag_types, tags)

    def replace_faces(
        self,
        file_id: str,
        faces: Sequence[MediaFaceInput],
        *,
        resolve_people_by_name: bool = False,
    ) -> list[Face]:
        media_file = self._require_media_file(file_id)
        preserved_assignments = self._matched_existing_face_person_ids(media_file.faces, faces)

        media_file.faces.clear()
        persisted_faces: list[Face] = []
        for index, face in enumerate(faces):
            normalized_face = self._normalize_face(face)
            person_id = preserved_assignments.get(index, normalized_face.person_id)
            if person_id is None and resolve_people_by_name and normalized_face.person_display_name is not None:
                person = self.get_or_create_person_by_display_name(normalized_face.person_display_name)
                person_id = person.id
            persisted_face = Face(
                person_id=person_id,
                bbox=normalized_face.bbox,
                embedding_ref=normalized_face.embedding_ref,
            )
            media_file.faces.append(persisted_face)
            persisted_faces.append(persisted_face)

        media_file.updated_at = datetime.utcnow()
        self._session.flush()
        return persisted_faces

    def upsert_faces(
        self,
        file_id: str,
        faces: Sequence[MediaFaceInput],
        *,
        resolve_people_by_name: bool = False,
    ) -> list[Face]:
        return self.replace_faces(
            file_id,
            faces,
            resolve_people_by_name=resolve_people_by_name,
        )

    def get_or_create_person_by_display_name(self, display_name: str) -> Person:
        normalized_name = display_name.strip()
        if not normalized_name:
            raise ValueError("display_name must not be blank")

        person = self._session.execute(
            select(Person)
            .where(Person.display_name == normalized_name, Person.merged_into_id.is_(None))
            .order_by(Person.id.asc())
        ).scalars().first()
        if person is None:
            # 같은 이름이 병합돼 숨겨진 경우, 숨은 사람이 아니라 병합 target에 붙인다.
            merged = self._session.execute(
                select(Person)
                .where(Person.display_name == normalized_name, Person.merged_into_id.isnot(None))
                .order_by(Person.id.asc())
            ).scalars().first()
            if merged is not None:
                person = self._resolve_merge_target(merged)
        if person is None:
            person = Person(display_name=normalized_name)
            self._session.add(person)
            self._session.flush()
        return person

    def _resolve_merge_target(self, person: Person) -> Person:
        """Follow merged_into_id links to the visible person at the end of the chain."""
        visited = {int(person.id)}
        current = person
        while current.merged_into_id is not None:
            target = self._session.get(Person, int(current.merged_into_id))
            if target is None or int(target.id) in visited:
                break
            visited.add(int(target.id))
            current = target
        return current

    def mark_missing(self, file_id: str) -> None:
        media_file = self._session.get(MediaFile, file_id)
        if media_file is None:
            return
        media_file.status = "missing"
        media_file.updated_at = datetime.utcnow()
        self._session.flush()

    def mark_media_error(self, file_id: str, *, stage: str, message: str, now: datetime) -> None:
        media_file = self._session.get(MediaFile, file_id)
        if media_file is None:
            return
        media_file.status = "error"
        media_file.error_stage = stage
        media_file.error_message = message[:2048]
        media_file.error_count = (media_file.error_count or 0) + 1
        media_file.updated_at = now
        self._session.flush()

    def mark_media_replaced(self, file_id: str, *, now: datetime) -> None:
        media_file = self._session.get(MediaFile, file_id)
        if media_file is None:
            return
        media_file.status = "replaced"
        media_file.updated_at = now
        self._session.flush()

    def set_media_status(self, file_id: str, *, status: str, now: datetime) -> None:
        media_file = self._session.get(MediaFile, file_id)
        if media_file is None:
            return
        media_file.status = status
        if status != "error":
            media_file.error_stage = None
            media_file.error_message = None
        media_file.updated_at = now
        self._session.flush()

    def exclude_media_kind(self, media_kind: str, *, now: datetime) -> int:
        rows = list(
            self._session.scalars(
                select(MediaFile).where(
                    MediaFile.media_kind == media_kind,
                    MediaFile.status != "excluded",
                )
            )
        )
        for media_file in rows:
            media_file.status = "excluded"
            media_file.error_stage = None
            media_file.error_message = None
            media_file.error_count = 0
            media_file.updated_at = now
        self._session.execute(delete(ScanObservation).where(ScanObservation.media_kind == media_kind))
        self._session.flush()
        return len(rows)

    def iter_known_paths(self, active_source_roots: set[str]):
        """Yield NFC-normalised current_path strings for all non-excluded DB files
        whose source_root is in active_source_roots. Used to pre-populate
        seen_paths for delta scanning so unchanged-directory files are not
        falsely marked missing."""
        query = select(MediaFile.source_root, MediaFile.current_path, MediaFile.status)
        for source_root, current_path, status in self._session.execute(query):
            if active_source_roots and source_root not in active_source_roots:
                continue
            if status in {"error", "replaced", "excluded"}:
                continue
            yield unicodedata.normalize("NFC", current_path)

    def iter_missing_media_paths(self, active_source_roots: set[str]):
        """Yield (source_root, current_path) for media marked missing.

        디렉터리 mtime 캐시가 unchanged 디렉터리를 건너뛰므로, missing 파일이
        디렉터리 변경 없이 되살아난 경우(NAS 재연결 등)는 스캔이 따로
        재확인해야 한다."""
        query = select(MediaFile.source_root, MediaFile.current_path).where(MediaFile.status == "missing")
        for source_root, current_path in self._session.execute(query):
            if active_source_roots and source_root not in active_source_roots:
                continue
            yield source_root, current_path

    def mark_missing_except(self, seen_paths: set[str], active_source_roots: set[str]) -> int:
        missing_count = 0
        query = select(MediaFile.file_id, MediaFile.source_root, MediaFile.current_path, MediaFile.status)
        for file_id, source_root, current_path, status in self._session.execute(query):
            if active_source_roots and source_root not in active_source_roots:
                continue
            if status in {"error", "replaced", "excluded"}:
                continue
            if unicodedata.normalize("NFC", current_path) in seen_paths:
                continue
            self.mark_missing(file_id)
            missing_count += 1
        return missing_count

    def _mark_replaced_rows_for_path(self, current_path: Path, new_file_id: str, now: datetime) -> None:
        rows = self._session.execute(
            select(MediaFile).where(
                MediaFile.current_path == str(current_path),
                MediaFile.file_id != new_file_id,
            )
        ).scalars()
        for media_file in rows:
            media_file.status = "replaced"
            media_file.updated_at = now
        self._session.flush()

    def _require_media_file(self, file_id: str) -> MediaFile:
        media_file = self._session.get(MediaFile, file_id)
        if media_file is None:
            raise ValueError(f"Unknown file_id: {file_id}")
        return media_file

    @staticmethod
    def _normalize_tags(tags: Sequence[MediaTagInput]) -> list[MediaTagInput]:
        deduped: dict[tuple[str, str], MediaTagInput] = {}
        for tag in tags:
            tag_type = tag.tag_type.strip().lower()
            tag_value = tag.tag_value.strip()
            if not tag_type or not tag_value:
                continue
            key = (tag_type, tag_value)
            if key in deduped:
                continue
            deduped[key] = MediaTagInput(tag_type=tag_type, tag_value=tag_value)
        return list(deduped.values())

    @staticmethod
    def _matched_existing_face_person_ids(
        existing_faces: Sequence[Face],
        new_faces: Sequence[MediaFaceInput],
        *,
        min_iou: float = 0.5,
    ) -> dict[int, int]:
        """Carry user-confirmed person assignments across face re-analysis.

        Face rows are replaced whenever model output is refreshed.  A drive/NAS
        remount or model-version bump must not silently reset names, aliases, or
        merge decisions for the same face in the same file_id, so match new
        detections to the previous boxes and keep the previous person_id when
        there is a clear overlap.
        """
        candidates: list[tuple[float, int, int]] = []
        for old_index, existing in enumerate(existing_faces):
            if existing.person_id is None:
                continue
            for new_index, new_face in enumerate(new_faces):
                score = _face_bbox_iou(existing.bbox or {}, new_face.bbox or {})
                if score >= min_iou:
                    candidates.append((score, old_index, new_index))

        assignments: dict[int, int] = {}
        used_old_indices: set[int] = set()
        for _, old_index, new_index in sorted(candidates, reverse=True):
            if old_index in used_old_indices or new_index in assignments:
                continue
            person_id = existing_faces[old_index].person_id
            if person_id is None:
                continue
            assignments[new_index] = person_id
            used_old_indices.add(old_index)
        return assignments

    @staticmethod
    def _normalize_face(face: MediaFaceInput) -> MediaFaceInput:
        bbox = _json_safe(face.bbox)
        if not isinstance(bbox, dict):
            raise ValueError("face bbox must be a JSON object")
        embedding_ref = face.embedding_ref.strip() if isinstance(face.embedding_ref, str) else face.embedding_ref
        person_display_name = (
            face.person_display_name.strip()
            if isinstance(face.person_display_name, str)
            else face.person_display_name
        )
        return MediaFaceInput(
            bbox=bbox,
            embedding_ref=embedding_ref or None,
            person_id=face.person_id,
            person_display_name=person_display_name or None,
        )

    @staticmethod
    def _stability_window(seconds: int):
        return timedelta(seconds=max(0, seconds))

    def _is_scan_record_stable(
        self,
        scan_record: FileScanRecord,
        *,
        now: datetime,
        stability_window_seconds: int,
    ) -> bool:
        if stability_window_seconds <= 0:
            return True
        mtime_at = datetime.fromtimestamp(scan_record.mtime_ns / 1_000_000_000, tz=timezone.utc).replace(tzinfo=None)
        return now - mtime_at >= self._stability_window(stability_window_seconds)

    def get_media(self, file_id: str) -> MediaFile | None:
        return self._session.get(MediaFile, file_id)

    def list_media(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        media_kind: str | None = None,
        source_root: str | None = None,
        query: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        tag: str | None = None,
        tag_type: str | None = None,
        face_count_min: int | None = None,
        face_count_max: int | None = None,
    ) -> list[MediaFile]:
        ids_subquery = self._filtered_media_ids_query(
            status=status,
            media_kind=media_kind,
            source_root=source_root,
            query=query,
            date_from=date_from,
            date_to=date_to,
            tag=tag,
            tag_type=tag_type,
            face_count_min=face_count_min,
            face_count_max=face_count_max,
        ).subquery()
        statement = (
            select(MediaFile)
            .join(ids_subquery, ids_subquery.c.file_id == MediaFile.file_id)
            .order_by(MediaFile.last_seen_at.desc(), MediaFile.file_id.desc())
        )
        statement = statement.limit(max(1, min(limit, 500))).offset(max(0, offset))
        return list(self._session.scalars(statement))

    def count_media(
        self,
        *,
        status: str | None = None,
        media_kind: str | None = None,
        source_root: str | None = None,
        query: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        tag: str | None = None,
        tag_type: str | None = None,
        face_count_min: int | None = None,
        face_count_max: int | None = None,
    ) -> int:
        ids_subquery = self._filtered_media_ids_query(
            status=status,
            media_kind=media_kind,
            source_root=source_root,
            query=query,
            date_from=date_from,
            date_to=date_to,
            tag=tag,
            tag_type=tag_type,
            face_count_min=face_count_min,
            face_count_max=face_count_max,
        ).subquery()
        statement = select(func.count()).select_from(ids_subquery)
        return int(self._session.scalar(statement) or 0)

    def media_status_counts(self) -> dict[str, int]:
        statement = select(MediaFile.status, func.count()).group_by(MediaFile.status)
        return {status: int(count) for status, count in self._session.execute(statement)}

    def media_kind_counts(self) -> dict[str, int]:
        statement = select(MediaFile.media_kind, func.count()).group_by(MediaFile.media_kind)
        return {media_kind: int(count) for media_kind, count in self._session.execute(statement)}

    def observation_status_counts(self) -> dict[str, int]:
        statement = select(ScanObservation.status, func.count()).group_by(ScanObservation.status)
        return {status: int(count) for status, count in self._session.execute(statement)}

    def count_observations(self, *, status: str | None = None) -> int:
        statement = select(func.count()).select_from(ScanObservation)
        if status:
            statement = statement.where(ScanObservation.status == status)
        return int(self._session.scalar(statement) or 0)

    def _filtered_media_ids_query(
        self,
        *,
        status: str | None = None,
        media_kind: str | None = None,
        source_root: str | None = None,
        query: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        tag: str | None = None,
        tag_type: str | None = None,
        face_count_min: int | None = None,
        face_count_max: int | None = None,
    ):
        statement = select(MediaFile.file_id).distinct()
        if status:
            statement = statement.where(MediaFile.status == status)
        else:
            statement = statement.where(MediaFile.status != "excluded")
        if media_kind:
            statement = statement.where(MediaFile.media_kind == media_kind)
        if source_root:
            statement = statement.where(MediaFile.source_root == source_root)
        if query:
            like_query = f"%{query}%"
            statement = statement.where(
                (MediaFile.current_path.ilike(like_query))
                | (MediaFile.relative_path.ilike(like_query))
                | (MediaFile.filename.ilike(like_query))
                | (MediaFile.file_id.ilike(like_query))
            )
        mtime_expr = func.datetime(MediaFile.mtime_ns / 1000000000, "unixepoch", "localtime")
        captured_at_expr = func.coalesce(MediaFile.exif_datetime, mtime_expr, MediaFile.processed_at, MediaFile.last_seen_at)
        if date_from is not None:
            statement = statement.where(captured_at_expr >= date_from)
        if date_to is not None:
            statement = statement.where(captured_at_expr <= date_to)
        if tag or tag_type:
            statement = statement.join(Tag, Tag.file_id == MediaFile.file_id)
            if tag:
                statement = statement.where(Tag.tag_value == tag)
            if tag_type:
                statement = statement.where(Tag.tag_type == tag_type)
        if face_count_min is not None or face_count_max is not None:
            face_counts = (
                select(Face.file_id, func.count(Face.id).label("face_count"))
                .group_by(Face.file_id)
                .subquery()
            )
            statement = statement.outerjoin(face_counts, face_counts.c.file_id == MediaFile.file_id)
            face_count_expr = func.coalesce(face_counts.c.face_count, 0)
            if face_count_min is not None:
                statement = statement.where(face_count_expr >= face_count_min)
            if face_count_max is not None:
                statement = statement.where(face_count_expr <= face_count_max)
        return statement

    def list_media_for_processing(
        self,
        *,
        statuses: tuple[str, ...] = ("metadata_done",),
        limit: int | None = None,
    ) -> list[MediaFile]:
        statement = (
            select(MediaFile)
            .where(
                MediaFile.status.in_(statuses),
                MediaFile.media_kind == "image",
            )
            .order_by(MediaFile.updated_at.asc(), MediaFile.file_id.asc())
        )
        if limit is not None:
            statement = statement.limit(max(1, limit))
        return list(self._session.scalars(statement))

    def list_media_needing_embedding(
        self,
        *,
        limit: int | None = None,
        model_name: str | None = None,
        version: str | None = None,
    ) -> list[MediaFile]:
        """Return processed media that have no matching CLIP embedding yet."""
        embedding_query = select(MediaEmbedding.file_id)
        if model_name is not None:
            embedding_query = embedding_query.where(MediaEmbedding.model_name == model_name)
        if version is not None:
            embedding_query = embedding_query.where(MediaEmbedding.version == version)
        has_embedding = embedding_query.distinct().subquery()
        statement = (
            select(MediaFile)
            .where(
                MediaFile.status.in_(("thumb_done", "analysis_done")),
                MediaFile.media_kind == "image",
                MediaFile.file_id.not_in(select(has_embedding.c.file_id)),
            )
            .order_by(MediaFile.updated_at.asc(), MediaFile.file_id.asc())
        )
        if limit is not None:
            statement = statement.limit(max(1, limit))
        return list(self._session.scalars(statement))

    def register_derived_asset(
        self,
        file_id: str,
        asset_kind: DerivedAssetKind,
        derived_path: Path,
        *,
        version: str = "v1",
        content_type: str | None = None,
        checksum: str | None = None,
    ) -> DerivedAsset:
        asset = self._session.execute(
            select(DerivedAsset).where(
                DerivedAsset.file_id == file_id,
                DerivedAsset.asset_kind == asset_kind.value,
                DerivedAsset.asset_version == version,
                DerivedAsset.derived_path == str(derived_path),
            )
        ).scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if asset is None:
            asset = DerivedAsset(
                file_id=file_id,
                asset_kind=asset_kind.value,
                asset_version=version,
                derived_path=str(derived_path),
                content_type=content_type,
                checksum=checksum,
                created_at=now,
                updated_at=now,
            )
            self._session.add(asset)
        else:
            asset.derived_path = str(derived_path)
            asset.content_type = content_type
            asset.checksum = checksum
            asset.updated_at = now

        self._session.flush()
        return asset


def _face_bbox_iou(left: dict, right: dict) -> float:
    left_x1, left_y1, left_x2, left_y2 = _face_bbox_edges(left)
    right_x1, right_y1, right_x2, right_y2 = _face_bbox_edges(right)

    intersection_width = max(0.0, min(left_x2, right_x2) - max(left_x1, right_x1))
    intersection_height = max(0.0, min(left_y2, right_y2) - max(left_y1, right_y1))
    intersection = intersection_width * intersection_height
    if intersection <= 0.0:
        return 0.0

    left_area = max(0.0, left_x2 - left_x1) * max(0.0, left_y2 - left_y1)
    right_area = max(0.0, right_x2 - right_x1) * max(0.0, right_y2 - right_y1)
    union = left_area + right_area - intersection
    if union <= 0.0:
        return 0.0
    return intersection / union


def _face_bbox_edges(bbox: dict) -> tuple[float, float, float, float]:
    x = _coerce_float(bbox.get("x"))
    y = _coerce_float(bbox.get("y"))
    width = max(0.0, _coerce_float(bbox.get("width")))
    height = max(0.0, _coerce_float(bbox.get("height")))
    return x, y, x + width, y + height


def _coerce_float(value: object) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
