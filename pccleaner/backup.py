"""중요 폴더를 날짜별 zip으로 압축 백업하고, 오래된 백업은 보관 개수만큼만 남깁니다.

대상 폴더(dest)를 OneDrive / Google Drive / Dropbox 동기화 폴더로 지정하면
별도 API 인증 없이 클라우드 백업이 됩니다.
"""
from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .scanner import DEFAULT_EXCLUDE_DIRS
from .utils import is_relative_to

STAMP_FORMAT = "%Y-%m-%d_%H%M%S"


@dataclass
class BackupResult:
    archive: Path
    file_count: int
    original_size: int
    archive_size: int
    removed_old: list[Path] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


def _archive_pattern(prefix: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(prefix)}_\d{{4}}-\d{{2}}-\d{{2}}_\d{{6}}(_\d+)?\.zip$")


def list_backups(dest: Path, prefix: str) -> list[Path]:
    """이 프로그램이 만든 백업 파일만 오래된 순서로 반환합니다(다른 zip은 건드리지 않음)."""
    dest = Path(dest)
    if not dest.is_dir():
        return []
    pat = _archive_pattern(prefix)
    return sorted((p for p in dest.iterdir() if p.is_file() and pat.match(p.name)), key=lambda p: p.name)


def create_backup(
    source: str | os.PathLike,
    dest: str | os.PathLike,
    keep: int = 7,
    exclude_dirs: Iterable[str] | None = None,
    now: datetime | None = None,
) -> BackupResult:
    source = Path(source).resolve()
    dest = Path(dest).resolve()
    if not source.is_dir():
        raise NotADirectoryError(f"백업할 폴더가 없습니다: {source}")
    if keep < 1:
        raise ValueError("keep은 1 이상이어야 합니다.")
    dest.mkdir(parents=True, exist_ok=True)

    prefix = source.name or "drive"
    stamp = (now or datetime.now()).strftime(STAMP_FORMAT)
    archive = dest / f"{prefix}_{stamp}.zip"
    n = 1
    while archive.exists():
        archive = dest / f"{prefix}_{stamp}_{n}.zip"
        n += 1
    partial = archive.with_name(archive.name + ".partial")

    excluded = {d.lower() for d in (DEFAULT_EXCLUDE_DIRS if exclude_dirs is None else exclude_dirs)}
    result = BackupResult(archive=archive, file_count=0, original_size=0, archive_size=0)

    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for dirpath, dirnames, filenames in os.walk(source):
                current = Path(dirpath)
                # 백업 대상 폴더가 원본 안에 있으면 자기 자신을 계속 압축하게 되므로 제외
                dirnames[:] = [
                    d for d in dirnames if d.lower() not in excluded and not is_relative_to(current / d, dest)
                ]
                for name in filenames:
                    path = current / name
                    if path == partial:
                        continue
                    try:
                        zf.write(path, path.relative_to(source).as_posix())
                        result.file_count += 1
                        result.original_size += path.stat().st_size
                    except (OSError, ValueError) as exc:
                        result.errors.append((str(path), str(exc)))
        # 무결성 검사: 압축 파일을 다시 읽어 CRC를 확인
        with zipfile.ZipFile(partial) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise zipfile.BadZipFile(f"손상된 항목: {bad}")
        os.replace(partial, archive)  # 완성된 뒤에만 정식 이름으로 변경
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    result.archive_size = archive.stat().st_size

    backups = list_backups(dest, prefix)
    for old in backups[: max(0, len(backups) - keep)]:
        old.unlink()
        result.removed_old.append(old)
    return result
