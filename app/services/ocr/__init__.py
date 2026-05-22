"""OCR service package."""

from app.services.ocr.service import OCRBlock, OCRResult, extract, extract_text, is_enabled

__all__ = ["OCRBlock", "OCRResult", "extract", "extract_text", "is_enabled"]
