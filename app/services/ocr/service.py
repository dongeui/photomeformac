"""OCR helpers with a small engine abstraction."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from app.services.image_decode import ensure_heif_support

OCR_LANG = os.environ.get("PHOTOME_OCR_LANG", "kor+eng")
OCR_ENGINE = os.environ.get("PHOTOME_OCR_ENGINE", "tesseract").casefold()


@dataclass
class OCRBlock:
    level: str
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int


@dataclass
class OCRResult:
    text: str
    blocks: list[OCRBlock]
    engine: str


def is_enabled() -> bool:
    return bool(OCR_LANG.strip())


def extract_text(image_path: str) -> str:
    return extract(image_path).text


def extract(image_path: str) -> OCRResult:
    if not is_enabled():
        return OCRResult("", [], OCR_ENGINE)

    path = Path(image_path)
    if not path.exists():
        return OCRResult("", [], OCR_ENGINE)

    if OCR_ENGINE == "easyocr":
        return _extract_easyocr(path)
    if OCR_ENGINE == "paddleocr":
        return _extract_paddleocr(path)
    return _extract_tesseract(path)


def _extract_tesseract(path: Path) -> OCRResult:
    candidates = []
    blocks: list[OCRBlock] = []

    try:
        import pytesseract

        ensure_heif_support()
        with Image.open(path) as image:
            for prepared, config in (
                (_prepare_image(image), "--oem 1 --psm 6"),
                (_prepare_threshold_image(image), "--oem 1 --psm 11"),
            ):
                text = pytesseract.image_to_string(prepared, lang=OCR_LANG, config=config)
                normalized = _normalize_text(text)
                if normalized:
                    candidates.append(normalized)
            blocks = _extract_tesseract_blocks(_prepare_image(image))
    except Exception:
        return OCRResult("", [], "tesseract")

    if not candidates:
        return OCRResult("", blocks, "tesseract")
    return OCRResult(_merge_candidates(candidates), blocks, "tesseract")


def _extract_tesseract_blocks(image: Image.Image) -> list[OCRBlock]:
    import pytesseract

    data = pytesseract.image_to_data(image, lang=OCR_LANG, config="--oem 1 --psm 6", output_type=pytesseract.Output.DICT)
    blocks: list[OCRBlock] = []
    for idx, raw_text in enumerate(data.get("text", [])):
        text = str(raw_text).strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][idx])
        except Exception:
            confidence = -1.0
        if confidence < 0:
            continue
        blocks.append(
            OCRBlock(
                level="word",
                text=text,
                confidence=confidence / 100.0,
                left=int(data["left"][idx]),
                top=int(data["top"][idx]),
                width=int(data["width"][idx]),
                height=int(data["height"][idx]),
            )
        )
    return blocks[:1000]


def _extract_easyocr(path: Path) -> OCRResult:
    try:
        import easyocr

        reader = _easyocr_reader()
        raw = reader.readtext(str(path), detail=1, paragraph=False)
    except Exception:
        return _extract_tesseract(path)

    lines = []
    blocks: list[OCRBlock] = []
    for box, text, confidence in raw:
        normalized = str(text).strip()
        if not normalized or float(confidence) < 0.5:
            continue
        xs = [int(point[0]) for point in box]
        ys = [int(point[1]) for point in box]
        left, top = min(xs), min(ys)
        blocks.append(OCRBlock("line", normalized, float(confidence), left, top, max(xs) - left, max(ys) - top))
        lines.append(normalized)
    return OCRResult(_merge_candidates(["\n".join(lines)]), blocks, "easyocr")


def _extract_paddleocr(path: Path) -> OCRResult:
    try:
        from paddleocr import PaddleOCR

        reader = _paddle_reader()
        raw = reader.ocr(str(path), cls=True)
    except Exception:
        return _extract_tesseract(path)

    lines = []
    blocks: list[OCRBlock] = []
    for page in raw or []:
        for item in page or []:
            if len(item) < 2:
                continue
            box, rec = item[0], item[1]
            text, confidence = rec[0], float(rec[1])
            normalized = str(text).strip()
            if not normalized or confidence < 0.5:
                continue
            xs = [int(point[0]) for point in box]
            ys = [int(point[1]) for point in box]
            left, top = min(xs), min(ys)
            blocks.append(OCRBlock("line", normalized, confidence, left, top, max(xs) - left, max(ys) - top))
            lines.append(normalized)
    return OCRResult(_merge_candidates(["\n".join(lines)]), blocks, "paddleocr")


def _easyocr_reader():
    if not hasattr(_easyocr_reader, "_reader"):
        import easyocr

        _easyocr_reader._reader = easyocr.Reader(["ko", "en"], gpu=False)
    return _easyocr_reader._reader


def _paddle_reader():
    if not hasattr(_paddle_reader, "_reader"):
        from paddleocr import PaddleOCR

        _paddle_reader._reader = PaddleOCR(use_angle_cls=True, lang="korean", show_log=False)
    return _paddle_reader._reader


def _prepare_image(image: Image.Image) -> Image.Image:
    grayscale = ImageOps.grayscale(image)
    boosted = ImageOps.autocontrast(grayscale)
    if min(boosted.size) < 1200:
        boosted = boosted.resize((boosted.width * 2, boosted.height * 2))
    return boosted


def _prepare_threshold_image(image: Image.Image) -> Image.Image:
    prepared = _prepare_image(image)
    thresholded = prepared.point(lambda px: 255 if px > 170 else 0)
    return thresholded


def _normalize_text(text: str) -> str:
    parts = [line.strip() for line in text.splitlines()]
    compact = [part for part in parts if part]
    return "\n".join(compact)


def _merge_candidates(candidates: list[str]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        for line in candidate.splitlines():
            normalized = line.strip()
            if not normalized:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            lines.append(normalized)
    return "\n".join(lines)
