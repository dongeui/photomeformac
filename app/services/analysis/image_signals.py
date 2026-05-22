"""
Lightweight image analysis tags for reranking and gallery filters.
"""
from __future__ import annotations

from pathlib import Path

import cv2


def extract_analysis(image_path: str, ocr_text: str) -> dict:
    image = cv2.imread(str(Path(image_path)), cv2.IMREAD_COLOR)
    if image is None:
        return _empty_analysis(ocr_text, image_path)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 80, 180)
    edge_density = float((edges > 0).sum()) / float(edges.size or 1)
    brightness = float(gray.mean()) / 255.0

    text_lines = [line.strip() for line in ocr_text.splitlines() if line.strip()]
    text_char_count = len("".join(text_lines))
    text_line_count = len(text_lines)

    lower_name = Path(image_path).name.lower()
    screenshot_name = any(token in lower_name for token in ("screenshot", "screen", "스크린샷"))

    is_text_heavy = text_char_count >= 24 or text_line_count >= 3
    is_document_like = (
        (is_text_heavy and brightness >= 0.6 and edge_density >= 0.01)
        or text_char_count >= 60
    )
    is_screenshot_like = screenshot_name or (
        is_text_heavy and edge_density >= 0.015 and brightness >= 0.35
    )

    return {
        "text_char_count": text_char_count,
        "text_line_count": text_line_count,
        "edge_density": round(edge_density, 5),
        "brightness": round(brightness, 5),
        "is_text_heavy": int(is_text_heavy),
        "is_document_like": int(is_document_like),
        "is_screenshot_like": int(is_screenshot_like),
    }


def _empty_analysis(ocr_text: str, image_path: str) -> dict:
    text_lines = [line.strip() for line in ocr_text.splitlines() if line.strip()]
    lower_name = Path(image_path).name.lower()
    return {
        "text_char_count": len("".join(text_lines)),
        "text_line_count": len(text_lines),
        "edge_density": 0.0,
        "brightness": 0.0,
        "is_text_heavy": int(len("".join(text_lines)) >= 24 or len(text_lines) >= 3),
        "is_document_like": 0,
        "is_screenshot_like": int(any(token in lower_name for token in ("screenshot", "screen", "스크린샷"))),
    }
