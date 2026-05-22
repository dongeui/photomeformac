"""Embedding service package."""

from app.services.embedding.clip import encode_image, encode_text, embedding_from_bytes, ensure_models, is_ready, status

__all__ = ["encode_image", "encode_text", "embedding_from_bytes", "ensure_models", "is_ready", "status"]
