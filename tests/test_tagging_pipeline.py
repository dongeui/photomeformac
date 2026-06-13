from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Iterable, Iterator

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.core.contracts import MediaFaceInput, MediaTagInput
from app.core.settings import load_settings
from app.main import create_app
from app.models.face import Face
from app.models.person import Person
from app.models.tag import Tag
from app.services.analysis import FaceAnalysis, FaceBoundingBox, ImageFaceAnalysisResult, OpenCVFaceModelPaths
from app.services.processing.registry import MediaCatalog
from app.services.search.vocab import TagVocabularyCache


SCAN_DELAY_SECONDS = 1.1


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source_root: Path) -> Iterator[TestClient]:
    data_root = tmp_path / "data"
    derived_root = tmp_path / "derived"
    database_path = data_root / "photomine.sqlite3"

    monkeypatch.setenv("PHOTOMINE_SOURCE_ROOTS", str(source_root))
    monkeypatch.setenv("PHOTOMINE_DATA_ROOT", str(data_root))
    monkeypatch.setenv("PHOTOMINE_DERIVED_ROOT", str(derived_root))
    monkeypatch.setenv("PHOTOMINE_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("PHOTOMINE_STABILITY_WINDOW_SECONDS", "1")
    monkeypatch.setenv("PHOTOME_SYNC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("PHOTOMINE_LOG_LEVEL", "ERROR")

    app = create_app(load_settings())
    with TestClient(app) as test_client:
        yield test_client


def test_image_exif_gps_is_persisted_as_place_tag_after_processing(
    client: TestClient,
    source_root: Path,
) -> None:
    image_path = source_root / "gps-image.jpg"
    create_image_with_gps_exif(image_path, latitude=37.55, longitude=127.0)

    scan_twice(client)
    item = get_media_item(client, filename="gps-image.jpg")

    metadata_json = item["metadata_json"] or {}
    gps_payload = metadata_json["gps"]
    persist_tags(
        client,
        item["file_id"],
        [MediaTagInput(tag_type="place", tag_value=format_place_tag(gps_payload))],
    )

    detail = client.get(f"/media/{item['file_id']}").json()

    assert gps_payload["latitude"] == pytest.approx(37.55)
    assert gps_payload["longitude"] == pytest.approx(127.0)
    assert {(tag["tag_type"], tag["tag_value"]) for tag in detail["tags"]} == {
        ("place", "37.5500,127.0000"),
    }


def test_multi_face_analysis_persists_multiple_person_tags_and_faces(
    client: TestClient,
    source_root: Path,
) -> None:
    image_path = source_root / "group-photo.jpg"
    create_image(image_path)

    scan_twice(client)
    item = get_media_item(client, filename="group-photo.jpg")
    analyzer = FakeFaceAnalyzer(
        build_face_analysis_result(
            image_path,
            [
                {"name": "Alice Kim", "bbox": (6, 8, 18, 18), "confidence": 0.99, "embedding": (0.1, 0.2, 0.3)},
                {"name": "Bob Lee", "bbox": (30, 10, 20, 20), "confidence": 0.97, "embedding": (0.4, 0.5, 0.6)},
            ],
        )
    )

    persist_fake_face_analysis(client, item["file_id"], analyzer)

    detail = client.get(f"/media/{item['file_id']}").json()

    with client.app.state.database.session_factory() as session:
        people = session.scalars(select(Person).order_by(Person.display_name.asc())).all()

    assert analyzer.calls == [image_path.resolve()]
    assert {(tag["tag_type"], tag["tag_value"]) for tag in detail["tags"]} == {
        ("person", "Alice Kim"),
        ("person", "Bob Lee"),
    }
    assert len(detail["faces"]) == 2
    assert all(face["person_id"] is not None for face in detail["faces"])
    assert {person.display_name for person in people} == {"Alice Kim", "Bob Lee"}


def test_person_alias_mapping_updates_tags_search_and_dashboard(
    client: TestClient,
    source_root: Path,
) -> None:
    TagVocabularyCache.invalidate()
    image_path = source_root / "child-portrait.jpg"
    create_image(image_path)

    scan_twice(client)
    item = get_media_item(client, filename="child-portrait.jpg")
    analyzer = FakeFaceAnalyzer(
        build_face_analysis_result(
            image_path,
            [
                {"name": "person-000001", "bbox": (6, 8, 18, 18), "confidence": 0.99, "embedding": (0.1, 0.2, 0.3)},
            ],
        )
    )
    persist_fake_face_analysis(client, item["file_id"], analyzer)

    with client.app.state.database.session_factory() as session:
        person = session.scalars(select(Person)).first()
        assert person is not None
        person_id = person.id
        vocab_before = TagVocabularyCache(session).get()
        assert "쭈니" not in vocab_before.person_tags

    update = client.patch(
        f"/people/{person_id}",
        json={"display_name": "민준", "aliases": ["쭈니", "아들"]},
    )

    assert update.status_code == 200
    assert update.json()["aliases"] == ["쭈니", "아들"]
    with client.app.state.database.session_factory() as session:
        vocab_after = TagVocabularyCache(session).get()
        assert {"민준", "쭈니", "아들"}.issubset(vocab_after.person_tags)

    face_id = update.json()["sample_face_ids"][0]
    crop = client.get(f"/people/faces/{face_id}/crop")
    assert crop.status_code == 200
    assert crop.headers["content-type"] == "image/jpeg"
    detail = client.get(f"/media/{item['file_id']}").json()
    assert {
        (tag["tag_type"], tag["tag_value"])
        for tag in detail["tags"]
        if tag["tag_type"] == "person"
    } == {("person", "민준"), ("person", "쭈니"), ("person", "아들")}

    search = client.get("/search", params={"q": "쭈니 사진"})
    assert search.status_code == 200
    assert search.json()["items"][0]["file_id"] == item["file_id"]
    assert search.json()["items"][0]["tag_exact_match"] is True

    dashboard = client.get("/dashboard").text
    assert "사람 이름" in dashboard
    assert f"/people/faces/{face_id}/crop" in dashboard
    assert "쭈니, 아들" in dashboard
    assert 'class="btn-copy person-preview-trigger"' in dashboard
    assert "trove.dashboard.people_manager.open" in dashboard
    assert 'id="people-filter"' in dashboard
    assert 'id="people-sort"' in dashboard
    assert 'id="person-reassign-filter"' in dashboard
    assert "isPeopleMergeInProgress" in dashboard
    assert "peopleMergeOptionLabel" in dashboard
    assert "label === canonical ? canonical" in dashboard
    assert "병합 완료. 목록을 새로고침합니다..." in dashboard
    assert 'class="person-preview-face"' in dashboard
    assert 'class="reassign-select"' in dashboard
    assert 'class="reassign-btn"' in dashboard
    assert "개별 이동" in dashboard
    assert "/gallery?person=" not in dashboard

    preview = client.get(f"/people/{person_id}/preview").json()
    assert preview["person"]["id"] == person_id
    assert preview["items"][0]["file_id"] == item["file_id"]
    assert preview["items"][0]["asset_id"] is not None


def test_gallery_person_filter_lists_only_named_people(
    client: TestClient,
    source_root: Path,
) -> None:
    """인물 콤보박스에는 대표 이름을 저장한 인물만 노출되고, person-000001
    같은 내부 자동 ID는 제외된다."""
    TagVocabularyCache.invalidate()
    image_path = source_root / "unnamed-portrait.jpg"
    create_image(image_path)

    scan_twice(client)
    item = get_media_item(client, filename="unnamed-portrait.jpg")
    persist_fake_face_analysis(
        client,
        item["file_id"],
        FakeFaceAnalyzer(
            build_face_analysis_result(
                image_path,
                [{"name": "person-000001", "bbox": (6, 8, 18, 18), "confidence": 0.99, "embedding": (0.1, 0.2, 0.3)}],
            )
        ),
    )

    # 이름을 붙이기 전: 내부 ID뿐이라 콤보에 인물 옵션이 없다
    gallery_before = client.get("/gallery").text
    assert 'value="person-000001"' not in gallery_before

    with client.app.state.database.session_factory() as session:
        person_id = session.scalars(select(Person)).first().id
    update = client.patch(f"/people/{person_id}", json={"display_name": "민준", "aliases": []})
    assert update.status_code == 200

    # 이름을 붙인 뒤: 대표 이름이 콤보에 노출되고 내부 ID는 여전히 숨겨진다
    gallery_after = client.get("/gallery").text
    assert 'value="민준"' in gallery_after
    assert 'value="person-000001"' not in gallery_after


def test_people_merge_reassigns_faces_and_rebuilds_person_search(
    client: TestClient,
    source_root: Path,
) -> None:
    first_path = source_root / "first-person.jpg"
    second_path = source_root / "second-person.jpg"
    create_image(first_path, color=(100, 40, 40))
    create_image(second_path, color=(40, 100, 40))

    scan_twice(client)
    first_item = get_media_item(client, filename="first-person.jpg")
    second_item = get_media_item(client, filename="second-person.jpg")
    persist_fake_face_analysis(
        client,
        first_item["file_id"],
        FakeFaceAnalyzer(
            build_face_analysis_result(
                first_path,
                [{"name": "person-000001", "bbox": (6, 8, 18, 18), "confidence": 0.99, "embedding": (0.1, 0.2, 0.3)}],
            )
        ),
    )
    persist_fake_face_analysis(
        client,
        second_item["file_id"],
        FakeFaceAnalyzer(
            build_face_analysis_result(
                second_path,
                [{"name": "person-000002", "bbox": (6, 8, 18, 18), "confidence": 0.99, "embedding": (0.4, 0.5, 0.6)}],
            )
        ),
    )

    people = client.get("/people").json()
    target_id = next(person["id"] for person in people if person["display_name"] == "person-000001")
    source_id = next(person["id"] for person in people if person["display_name"] == "person-000002")

    update = client.patch(
        f"/people/{target_id}",
        json={"display_name": "민준", "aliases": ["쭈니"]},
    )
    assert update.status_code == 200
    source_update = client.patch(
        f"/people/{source_id}",
        json={"display_name": "민준후보", "aliases": ["주니어"]},
    )
    assert source_update.status_code == 200

    merge = client.post(
        "/people/merge",
        json={"target_person_id": target_id, "source_person_ids": [source_id]},
    )

    assert merge.status_code == 200
    assert merge.json()["id"] == target_id
    assert merge.json()["media_count"] == 2
    assert "민준후보" in merge.json()["aliases"]
    assert "주니어" in merge.json()["aliases"]
    assert client.get(f"/people/{source_id}").status_code == 404

    with client.app.state.database.session_factory() as session:
        face_person_ids = set(session.scalars(select(Face.person_id)).all())
        person_labels = {
            tag_value
            for tag_value in session.scalars(
                select(Tag.tag_value).where(Tag.tag_type == "person", Tag.file_id == second_item["file_id"])
            )
        }
    assert face_person_ids == {target_id}
    assert {"민준", "쭈니", "민준후보", "주니어"}.issubset(person_labels)

    search = client.get("/search", params={"q": "주니어 사진"})
    assert search.status_code == 200
    assert search.json()["items"][0]["file_id"] == second_item["file_id"]


def test_people_unmerge_restores_source_person_and_faces(
    client: TestClient,
    source_root: Path,
) -> None:
    first_path = source_root / "unmerge-first.jpg"
    second_path = source_root / "unmerge-second.jpg"
    create_image(first_path, color=(100, 40, 40))
    create_image(second_path, color=(40, 100, 40))

    scan_twice(client)
    first_item = get_media_item(client, filename="unmerge-first.jpg")
    second_item = get_media_item(client, filename="unmerge-second.jpg")
    persist_fake_face_analysis(
        client,
        first_item["file_id"],
        FakeFaceAnalyzer(
            build_face_analysis_result(
                first_path,
                [{"name": "person-000001", "bbox": (6, 8, 18, 18), "confidence": 0.99, "embedding": (0.1, 0.2, 0.3)}],
            )
        ),
    )
    persist_fake_face_analysis(
        client,
        second_item["file_id"],
        FakeFaceAnalyzer(
            build_face_analysis_result(
                second_path,
                [{"name": "person-000002", "bbox": (6, 8, 18, 18), "confidence": 0.99, "embedding": (0.4, 0.5, 0.6)}],
            )
        ),
    )

    people = client.get("/people").json()
    target_id = next(person["id"] for person in people if person["display_name"] == "person-000001")
    source_id = next(person["id"] for person in people if person["display_name"] == "person-000002")
    assert client.patch(f"/people/{target_id}", json={"display_name": "민준", "aliases": ["쭈니"]}).status_code == 200
    assert client.patch(f"/people/{source_id}", json={"display_name": "민준후보", "aliases": ["주니어"]}).status_code == 200
    assert client.post(
        "/people/merge",
        json={"target_person_id": target_id, "source_person_ids": [source_id]},
    ).status_code == 200

    manage = client.get("/people/manage").text
    assert "합쳐짐" in manage
    assert f"unmergePerson({target_id},{source_id})" in manage

    unmerge = client.post(f"/people/{target_id}/unmerge/{source_id}")

    assert unmerge.status_code == 200
    restored = unmerge.json()
    assert restored["id"] == source_id
    assert restored["display_name"] == "민준후보"
    assert "주니어" in restored["aliases"]
    assert restored["media_count"] == 1

    assert client.get(f"/people/{source_id}").status_code == 200
    target_after = client.get(f"/people/{target_id}").json()
    assert "민준후보" not in target_after["aliases"]
    assert "주니어" not in target_after["aliases"]
    assert "쭈니" in target_after["aliases"]
    assert target_after["media_count"] == 1

    with client.app.state.database.session_factory() as session:
        face_person_ids = set(session.scalars(select(Face.person_id)).all())
        second_labels = set(
            session.scalars(
                select(Tag.tag_value).where(Tag.tag_type == "person", Tag.file_id == second_item["file_id"])
            )
        )
    assert face_person_ids == {target_id, source_id}
    assert {"민준후보", "주니어"}.issubset(second_labels)
    assert "민준" not in second_labels

    search = client.get("/search", params={"q": "주니어 사진"})
    assert search.status_code == 200
    assert search.json()["items"][0]["file_id"] == second_item["file_id"]

    # 이미 분리된 source는 다시 unmerge할 수 없다.
    assert client.post(f"/people/{target_id}/unmerge/{source_id}").status_code == 404


def _normalized(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


def _write_face_embedding_file(embeddings_root: Path, ref: str, embedding: list[float]) -> None:
    path = embeddings_root / Path(ref).relative_to("embeddings")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"embedding": embedding}), encoding="utf-8")


def test_merge_and_unmerge_recompute_person_centroids(
    client: TestClient,
    source_root: Path,
) -> None:
    first_path = source_root / "centroid-first.jpg"
    second_path = source_root / "centroid-second.jpg"
    create_image(first_path, color=(100, 40, 40))
    create_image(second_path, color=(40, 100, 40))

    scan_twice(client)
    first_item = get_media_item(client, filename="centroid-first.jpg")
    second_item = get_media_item(client, filename="centroid-second.jpg")
    embedding_a = [0.1, 0.2, 0.3]
    embedding_b = [0.4, 0.5, 0.6]
    persist_fake_face_analysis(
        client,
        first_item["file_id"],
        FakeFaceAnalyzer(
            build_face_analysis_result(
                first_path,
                [{"name": "person-000001", "bbox": (6, 8, 18, 18), "confidence": 0.99, "embedding": tuple(embedding_a)}],
            )
        ),
    )
    persist_fake_face_analysis(
        client,
        second_item["file_id"],
        FakeFaceAnalyzer(
            build_face_analysis_result(
                second_path,
                [{"name": "person-000002", "bbox": (6, 8, 18, 18), "confidence": 0.99, "embedding": tuple(embedding_b)}],
            )
        ),
    )

    embeddings_root = client.app.state.settings.embeddings_root
    people = client.get("/people").json()
    target_id = next(person["id"] for person in people if person["display_name"] == "person-000001")
    source_id = next(person["id"] for person in people if person["display_name"] == "person-000002")

    # 테스트 헬퍼는 fake:// ref만 기록하므로, 실제 임베딩 파일과 ref를 만들어 준다.
    with client.app.state.database.session_factory() as session:
        for person_id, embedding in ((target_id, embedding_a), (source_id, embedding_b)):
            face = session.scalars(select(Face).where(Face.person_id == person_id)).one()
            ref = f"embeddings/faces/v1/te/{face.file_id}-face-000.json"
            _write_face_embedding_file(embeddings_root, ref, embedding)
            face.embedding_ref = ref
        session.commit()

    assert client.post(
        "/people/merge",
        json={"target_person_id": target_id, "source_person_ids": [source_id]},
    ).status_code == 200

    target_centroid_path = embeddings_root / "people" / "v1" / f"person-{target_id:06d}.json"
    merged_payload = json.loads(target_centroid_path.read_text(encoding="utf-8"))
    assert merged_payload["sample_count"] == 2
    unit_a, unit_b = _normalized(embedding_a), _normalized(embedding_b)
    expected_merged = _normalized([(a + b) / 2 for a, b in zip(unit_a, unit_b)])
    assert merged_payload["embedding"] == pytest.approx(expected_merged)

    assert client.post(f"/people/{target_id}/unmerge/{source_id}").status_code == 200

    target_after = json.loads(target_centroid_path.read_text(encoding="utf-8"))
    assert target_after["sample_count"] == 1
    assert target_after["embedding"] == pytest.approx(unit_a)
    source_centroid_path = embeddings_root / "people" / "v1" / f"person-{source_id:06d}.json"
    source_after = json.loads(source_centroid_path.read_text(encoding="utf-8"))
    assert source_after["sample_count"] == 1
    assert source_after["embedding"] == pytest.approx(unit_b)


def test_deleted_person_media_is_hidden_from_people_manager(
    client: TestClient,
    source_root: Path,
) -> None:
    image_path = source_root / "delete-person.jpg"
    create_image(image_path)
    for index in range(100):
        create_image(source_root / f"keep-person-{index:03d}.jpg")

    scan_twice(client)
    item = get_media_item(client, filename="delete-person.jpg")
    persist_fake_face_analysis(
        client,
        item["file_id"],
        FakeFaceAnalyzer(
            build_face_analysis_result(
                image_path,
                [{"name": "person-000001", "bbox": (6, 8, 18, 18), "confidence": 0.99, "embedding": (0.1, 0.2, 0.3)}],
            )
        ),
    )
    assert client.get("/people").json()[0]["media_count"] == 1

    image_path.unlink()
    client.post("/scan")

    assert client.get("/people").json() == []


def test_media_filtering_by_tag_and_face_count_still_works(
    client: TestClient,
    source_root: Path,
) -> None:
    create_image(source_root / "seoul-family.jpg", color=(40, 80, 120))
    create_image(source_root / "busan-portrait.jpg", color=(120, 40, 80))
    create_image(source_root / "untagged.jpg", color=(80, 120, 40))

    scan_twice(client)
    items = {item["filename"]: item for item in client.get("/media").json()["items"]}

    persist_fake_face_analysis(
        client,
        items["seoul-family.jpg"]["file_id"],
        FakeFaceAnalyzer(
            build_face_analysis_result(
                source_root / "seoul-family.jpg",
                [
                    {"name": "Alice Kim", "bbox": (4, 6, 16, 16), "confidence": 0.98, "embedding": (0.1, 0.0, 0.2)},
                    {"name": "Bob Lee", "bbox": (26, 8, 18, 18), "confidence": 0.96, "embedding": (0.3, 0.2, 0.1)},
                ],
            )
        ),
        extra_tags=[MediaTagInput(tag_type="place", tag_value="Seoul")],
    )
    persist_fake_face_analysis(
        client,
        items["busan-portrait.jpg"]["file_id"],
        FakeFaceAnalyzer(
            build_face_analysis_result(
                source_root / "busan-portrait.jpg",
                [
                    {"name": "Carol Park", "bbox": (10, 10, 22, 22), "confidence": 0.97, "embedding": (0.4, 0.3, 0.2)},
                ],
            )
        ),
        extra_tags=[MediaTagInput(tag_type="place", tag_value="Busan")],
    )

    tagged = client.get("/media/filter", params={"tag": "Busan", "tag_type": "place"}).json()
    crowded = client.get(
        "/media/filter",
        params={"tag": "Seoul", "tag_type": "place", "face_count_min": 2, "face_count_max": 2},
    ).json()
    face_free = client.get("/media/filter", params={"face_count_max": 0}).json()

    assert {item["filename"] for item in tagged["items"]} == {"busan-portrait.jpg"}
    assert {item["filename"] for item in crowded["items"]} == {"seoul-family.jpg"}
    assert {item["filename"] for item in face_free["items"]} == {"untagged.jpg"}


class FakeFaceAnalyzer:
    def __init__(self, result: ImageFaceAnalysisResult) -> None:
        self._result = result
        self.calls: list[Path] = []

    def analyze_image_file(self, image_path: Path | str) -> ImageFaceAnalysisResult:
        resolved = Path(image_path).expanduser().resolve()
        self.calls.append(resolved)
        return self._result


def persist_tags(client: TestClient, file_id: str, tags: Iterable[MediaTagInput]) -> None:
    with client.app.state.database.session_factory() as session:
        catalog = MediaCatalog(session)
        catalog.upsert_tags(file_id, list(tags))
        session.commit()


def persist_fake_face_analysis(
    client: TestClient,
    file_id: str,
    analyzer: FakeFaceAnalyzer,
    *,
    extra_tags: Iterable[MediaTagInput] = (),
) -> None:
    with client.app.state.database.session_factory() as session:
        catalog = MediaCatalog(session)
        media_file = catalog.get_media(file_id)
        assert media_file is not None

        result = analyzer.analyze_image_file(media_file.current_path)
        face_inputs = [
            MediaFaceInput(
                bbox={
                    "x": face.bbox.x,
                    "y": face.bbox.y,
                    "width": face.bbox.width,
                    "height": face.bbox.height,
                    "confidence": face.bbox.confidence,
                    "landmarks": [list(point) for point in face.bbox.landmarks],
                },
                embedding_ref=f"fake://{file_id}/{face.face_index}",
                person_display_name=face.person_label_suggestion,
            )
            for face in result.faces
        ]
        tags = list(extra_tags) + [
            MediaTagInput(tag_type="person", tag_value=face.person_label_suggestion)
            for face in result.faces
        ]

        catalog.upsert_tags(file_id, tags)
        catalog.upsert_faces(file_id, face_inputs, resolve_people_by_name=True)
        session.commit()


def build_face_analysis_result(image_path: Path, faces: list[dict[str, object]]) -> ImageFaceAnalysisResult:
    analyzed_faces = tuple(
        FaceAnalysis(
            face_index=index,
            person_label_suggestion=str(face["name"]),
            bbox=FaceBoundingBox(
                x=int(face["bbox"][0]),
                y=int(face["bbox"][1]),
                width=int(face["bbox"][2]),
                height=int(face["bbox"][3]),
                confidence=float(face["confidence"]),
                landmarks=((0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0)),
            ),
            embedding=tuple(float(value) for value in face["embedding"]),
        )
        for index, face in enumerate(faces)
    )
    return ImageFaceAnalysisResult(
        image_path=image_path.resolve(),
        image_width=64,
        image_height=48,
        model_paths=OpenCVFaceModelPaths(
            root=image_path.parent,
            detector_path=image_path.parent / "fake-detector.onnx",
            recognizer_path=image_path.parent / "fake-recognizer.onnx",
        ),
        faces=analyzed_faces,
    )


def get_media_item(client: TestClient, *, filename: str) -> dict[str, object]:
    response = client.get("/media", params={"limit": 500})
    response.raise_for_status()
    items = response.json()["items"]
    for item in items:
        if item["filename"] == filename:
            return item
    raise AssertionError(f"media item not found for {filename}")


def scan_twice(client: TestClient) -> tuple[dict, dict]:
    first = client.post("/scan").json()
    time.sleep(SCAN_DELAY_SECONDS)
    second = client.post("/scan").json()
    return first, second


def create_image(path: Path, *, color: tuple[int, int, int] = (40, 80, 120)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (64, 48), color=color)
    image.save(path, format="JPEG")


def create_image_with_gps_exif(path: Path, *, latitude: float, longitude: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (64, 48), color=(60, 90, 130))
    exif = Image.Exif()
    exif[34853] = {
        1: "N" if latitude >= 0 else "S",
        2: to_dms(abs(latitude)),
        3: "E" if longitude >= 0 else "W",
        4: to_dms(abs(longitude)),
        29: "2026:04:23",
        7: (1.0, 2.0, 3.0),
    }
    image.save(path, format="JPEG", exif=exif)


def format_place_tag(gps_payload: dict[str, object]) -> str:
    latitude = float(gps_payload["latitude"])
    longitude = float(gps_payload["longitude"])
    return f"{latitude:.4f},{longitude:.4f}"


def to_dms(value: float) -> tuple[float, float, float]:
    degrees = int(value)
    minutes_float = (value - degrees) * 60
    minutes = int(minutes_float)
    seconds = (minutes_float - minutes) * 60
    return (float(degrees), float(minutes), float(seconds))
