from __future__ import annotations
from dataclasses import dataclass
MAX_SOURCE_VISUAL_OBJECTS = 1000
MAX_SOURCE_VISUAL_ALL_IMAGE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_VISUAL_REMOTE_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_SOURCE_VISUAL_REMOTE_REQUEST_ATTEMPTS = 256
MAX_SOURCE_VISUAL_TOTAL_PIXELS = 256000000
MAX_SOURCE_VISUAL_OCR_OBJECTS = 256
MAX_SOURCE_VISUAL_TABLE_CELLS = 1000000
MAX_SOURCE_VISUAL_TABLE_TEXT_CHARS = 16 * 1024 * 1024

class SourceVisualExtractionBudgetError(ValueError):
    pass

@dataclass
class SourceVisualExtractionBudget:
    visual_objects: int = 0
    all_image_bytes: int = 0
    remote_download_bytes: int = 0
    remote_request_attempts: int = 0
    total_pixels: int = 0
    ocr_objects: int = 0
    table_cells: int = 0
    table_text_chars: int = 0

    def reserve_visual_objects(self, count: int=1) -> None:
        if count < 0 or self.visual_objects + count > MAX_SOURCE_VISUAL_OBJECTS:
            raise SourceVisualExtractionBudgetError('Source visual extraction exceeded the visual object budget.')
        self.visual_objects += count

    def account_image_bytes(self, size: int) -> None:
        if size < 0 or self.all_image_bytes + size > MAX_SOURCE_VISUAL_ALL_IMAGE_BYTES:
            raise SourceVisualExtractionBudgetError('Source visual extraction exceeded the cumulative image byte budget.')
        self.all_image_bytes += size

    def account_remote_download(self, size: int) -> None:
        if size < 0 or self.remote_download_bytes + size > MAX_SOURCE_VISUAL_REMOTE_DOWNLOAD_BYTES:
            raise SourceVisualExtractionBudgetError('Source visual extraction exceeded the remote download byte budget.')
        self.remote_download_bytes += size

    def reserve_remote_request(self) -> None:
        if self.remote_request_attempts >= MAX_SOURCE_VISUAL_REMOTE_REQUEST_ATTEMPTS:
            raise SourceVisualExtractionBudgetError('Source visual extraction exceeded the remote request attempt budget.')
        self.remote_request_attempts += 1

    def account_image_pixels(self, width: int, height: int) -> None:
        pixels = width * height
        if width <= 0 or height <= 0 or pixels < 0 or (self.total_pixels + pixels > MAX_SOURCE_VISUAL_TOTAL_PIXELS):
            raise SourceVisualExtractionBudgetError('Source visual extraction exceeded the cumulative decompressed pixel budget.')
        self.total_pixels += pixels

    def reserve_ocr_objects(self, count: int=1) -> None:
        if count < 0 or self.ocr_objects + count > MAX_SOURCE_VISUAL_OCR_OBJECTS:
            raise SourceVisualExtractionBudgetError('Source visual extraction exceeded the OCR object budget.')
        self.ocr_objects += count

    def account_table(self, rows: list[list[str]]) -> None:
        cells = sum((len(row) for row in rows))
        text_chars = sum((len(str(cell)) for row in rows for cell in row))
        if self.table_cells + cells > MAX_SOURCE_VISUAL_TABLE_CELLS:
            raise SourceVisualExtractionBudgetError('Source visual extraction exceeded the table cell budget.')
        if self.table_text_chars + text_chars > MAX_SOURCE_VISUAL_TABLE_TEXT_CHARS:
            raise SourceVisualExtractionBudgetError('Source visual extraction exceeded the table text budget.')
            self.table_cells += cells
            self.table_text_chars += text_chars
