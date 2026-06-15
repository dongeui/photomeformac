from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.settings import load_settings
from app.main import create_app
from app.models.face import Face
from app.models.media import MediaFile
from app.models.person import Person
from app.services.processing.person_centroids import person_centroid_path

DIM = 128


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    data_root = tmp_path / "data"
    monkeypatch.setenv("PHOTOMINE_SOURCE_ROOTS", str(tmp_path / "source"))
    monkeypatch.setenv("PHOTOMINE_DATA_ROOT", str(data_root))
    monkeypatch.setenv("PHOTOMINE_DERIVED_ROOT", str(tmp_path / "derived"))
    monkeypatch.setenv("PHOTOMINE_DATABASE_PATH", str(data_root / "t.sqlite3"))
    monkeypatch.setenv("TROVE_SYNC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("PHOTOMINE_LOG_LEVEL", "ERROR")
    app = create_app(load_settings())
    with TestClient(app) as test_client:
        yield test_client


def _unit_pair(cos: float) -> tuple[list[float], list[float]]:
    a = [0.0] * DIM
    a[0] = 1.0
    b = [0.0] * DIM
    b[0] = cos
    b[1] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return a, b


def _add_person(session, *, name: str, file_id: str, embedding: list[float], embeddings_root: Path) -> int:
    person = Person(display_name=name, aliases_json=[])
    session.add(person)
    session.flush()
    session.add(
        MediaFile(
            file_id=file_id,
            current_path=f"/photos/{file_id}.jpg",
            filename=f"{file_id}.jpg",
            source_root="/photos",
            relative_path=f"{file_id}.jpg",
            media_kind="image",
            status="thumb_done",
            size_bytes=1,
            mtime_ns=1,
            partial_hash=f"h-{file_id}",
        )
    )
    # 게이트(얼굴 2회 이상) 통과용으로 같은 파일에 얼굴 2개.
    for idx in range(2):
        session.add(Face(file_id=file_id, person_id=person.id, embedding_ref=f"{file_id}-{idx}.json"))
    path = person_centroid_path(embeddings_root, int(person.id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"embedding": embedding, "sample_count": 2}), encoding="utf-8")
    return int(person.id)


def test_merge_suggestions_band_dismiss_and_above_threshold(client: TestClient) -> None:
    settings = client.app.state.settings
    database = client.app.state.database
    embeddings_root = settings.embeddings_root

    in_band = (settings.face_match_threshold + max(0.0, settings.face_match_threshold - 0.06)) / 2
    a_vec, b_vec = _unit_pair(in_band)          # 밴드 안 → 제안돼야 함
    _, dup_vec = _unit_pair(0.95)               # 임계값 초과 → 제안 안 됨(이미 같은 사람 취급)

    with database.session_factory() as session:
        pid_a = _add_person(session, name="person-000001", file_id="fa", embedding=a_vec, embeddings_root=embeddings_root)
        pid_b = _add_person(session, name="person-000002", file_id="fb", embedding=b_vec, embeddings_root=embeddings_root)
        _add_person(session, name="person-000003", file_id="fc", embedding=dup_vec, embeddings_root=embeddings_root)
        session.commit()

    res = client.get("/people/merge-suggestions").json()
    pairs = {
        tuple(sorted((s["a"]["id"], s["b"]["id"])))
        for s in res["suggestions"]
    }
    assert (min(pid_a, pid_b), max(pid_a, pid_b)) in pairs
    # 임계값 초과(0.95) 쌍은 제안에 없어야 한다.
    for s in res["suggestions"]:
        assert s["similarity"] < settings.face_match_threshold

    # 거절하면 다시 안 나온다.
    dismiss = client.post(
        "/people/merge-suggestions/dismiss",
        json={"person_id_a": pid_a, "person_id_b": pid_b},
    )
    assert dismiss.status_code == 204

    res2 = client.get("/people/merge-suggestions").json()
    pairs2 = {
        tuple(sorted((s["a"]["id"], s["b"]["id"])))
        for s in res2["suggestions"]
    }
    assert (min(pid_a, pid_b), max(pid_a, pid_b)) not in pairs2
