"""Backfill automatic visual tags from existing analysis and CLIP embeddings."""

from __future__ import annotations

import argparse

from app.core.settings import load_settings
from app.db.bootstrap import build_database_state
from app.models.media import MediaFile
from app.models.semantic import MediaAnalysisSignal, MediaAutoTagState, MediaEmbedding, MediaOCR
from app.services.analysis import auto_tags
from app.services.semantic import SemanticCatalog
from app.services.processing.registry import MediaCatalog


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill automatic visual tags.")
    parser.add_argument("--stale-only", action="store_true", help="Only rebuild rows missing the current auto-tag version.")
    args = parser.parse_args()

    settings = load_settings()
    database = build_database_state(settings)
    updated = 0
    skipped = 0
    tagged = 0

    with database.session_factory() as session:
        catalog = MediaCatalog(session)
        semantic_catalog = SemanticCatalog(session)
        items = session.query(MediaFile).filter(MediaFile.status != "missing").all()
        for media_file in items:
            state = session.get(MediaAutoTagState, media_file.file_id)
            if args.stale_only and state is not None and state.version == settings.semantic_auto_tag_version:
                skipped += 1
                continue

            analysis = session.get(MediaAnalysisSignal, media_file.file_id)
            ocr = session.get(MediaOCR, media_file.file_id)
            embedding = (
                session.query(MediaEmbedding)
                .filter(MediaEmbedding.file_id == media_file.file_id)
                .order_by(MediaEmbedding.updated_at.desc())
                .first()
            )

            signal_payload = {
                "is_screenshot_like": analysis.is_screenshot_like if analysis else False,
                "is_document_like": analysis.is_document_like if analysis else False,
                "is_text_heavy": analysis.is_text_heavy if analysis else False,
            }
            signal_tags = auto_tags.tags_from_signals(signal_payload, ocr.text_content if ocr else "")
            embedding_tags = (
                auto_tags.tags_from_embedding_file(embedding.embedding_ref, settings.embeddings_root)
                if embedding is not None
                else []
            )
            generated_tags = auto_tags.merge_auto_tags(signal_tags, embedding_tags)
            catalog.upsert_tags_for_types(media_file.file_id, ["auto"], generated_tags)
            semantic_catalog.upsert_auto_tag_state(
                media_file.file_id,
                tags=generated_tags,
                version=settings.semantic_auto_tag_version,
            )
            updated += 1
            if generated_tags:
                tagged += 1

        session.commit()

    print(f"processed={updated} skipped={skipped} tagged={tagged} version={settings.semantic_auto_tag_version}")


if __name__ == "__main__":
    main()
