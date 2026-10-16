from __future__ import annotations
import stat
import zipfile
from pathlib import Path, PurePosixPath
MAX_SOURCE_ARCHIVE_ENTRIES = 4096
MAX_SOURCE_ARCHIVE_ENTRY_BYTES = 64 * 1024 * 1024
MAX_SOURCE_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_SOURCE_ARCHIVE_COMPRESSION_RATIO = 200.0
MIN_RATIO_CHECK_BYTES = 1024 * 1024

class SourceArchiveError(ValueError):
    pass

class SafeSourceArchive:

    def __init__(self, path: Path) -> None:
        try:
            self._archive = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise SourceArchiveError('Source archive is not a valid ZIP container.') from exc
        self._entries: dict[str, zipfile.ZipInfo] = {}
        self._read_sizes: dict[str, int] = {}
        try:
            self._validate_directory()
        except Exception:
            self._archive.close()
            raise

    def __enter__(self) -> SafeSourceArchive:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._archive.close()

    def namelist(self) -> list[str]:
        return list(self._entries)

    def read(self, name: str, *, max_bytes: int | None=None) -> bytes:
        info = self._entries.get(name)
        if info is None:
            raise KeyError(name)
        limit = min(MAX_SOURCE_ARCHIVE_ENTRY_BYTES, max_bytes if max_bytes is not None else MAX_SOURCE_ARCHIVE_ENTRY_BYTES)
        if limit < 0 or info.file_size > limit:
            raise SourceArchiveError('Source archive entry exceeds its decompression budget.')
        chunks: list[bytes] = []
        total = 0
        try:
            with self._archive.open(info, 'r') as handle:
                while True:
                    remaining = limit - total
                    chunk = handle.read(min(64 * 1024, remaining + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise SourceArchiveError('Source archive entry exceeds its decompression budget.')
                    chunks.append(chunk)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise SourceArchiveError('Source archive entry could not be read safely.') from exc
        prior_size = self._read_sizes.get(name)
        if prior_size is None:
            if sum(self._read_sizes.values()) + total > MAX_SOURCE_ARCHIVE_TOTAL_BYTES:
                raise SourceArchiveError('Source archive exceeds its total decompression budget.')
            self._read_sizes[name] = total
        elif prior_size != total:
            raise SourceArchiveError('Source archive entry size changed while reading.')
        return b''.join(chunks)

    def _validate_directory(self) -> None:
        archive_infos = self._archive.infolist()
        if len(archive_infos) > MAX_SOURCE_ARCHIVE_ENTRIES:
            raise SourceArchiveError('Source archive contains too many entries.')
        infos = [info for info in archive_infos if not info.is_dir()]
        declared_total = 0
        for info in infos:
            name = info.filename
            if name in self._entries:
                raise SourceArchiveError('Source archive contains duplicate entries.')
            if not _safe_archive_name(name) or _zip_entry_is_symlink(info):
                raise SourceArchiveError('Source archive contains an unsafe entry path.')
            if info.flag_bits & 1:
                raise SourceArchiveError('Encrypted source archive entries are unsupported.')
            if info.file_size < 0 or info.file_size > MAX_SOURCE_ARCHIVE_ENTRY_BYTES:
                raise SourceArchiveError('Source archive entry exceeds its decompression budget.')
            declared_total += info.file_size
            if declared_total > MAX_SOURCE_ARCHIVE_TOTAL_BYTES:
                raise SourceArchiveError('Source archive exceeds its total decompression budget.')
            compressed_size = max(1, info.compress_size)
            compression_ratio = info.file_size / compressed_size
            if info.file_size >= MIN_RATIO_CHECK_BYTES and compression_ratio > MAX_SOURCE_ARCHIVE_COMPRESSION_RATIO:
                raise SourceArchiveError('Source archive entry has an unsafe compression ratio.')
            self._entries[name] = info

def _safe_archive_name(name: str) -> bool:
    if not name or '\x00' in name or '\\' in name:
        return False
    normalized = PurePosixPath(name)
    return bool(not normalized.is_absolute() and normalized.parts and ('..' not in normalized.parts) and (not normalized.parts[0].endswith(':')))

def _zip_entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16 & 61440
    return mode == stat.S_IFLNK
