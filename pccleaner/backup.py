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

STAMP_FORMAT = "%Y-%m-%d_%H%M%S"  # 백업 파일명에 사용할 날짜/시각 타임스탬프 형식


@dataclass
class BackupResult:
    """백업 실행 결과(생성된 압축 파일 정보, 삭제된 오래된 백업, 오류 목록)를 담는 데이터 클래스."""

    archive: Path                                             # 최종 생성된 백업 zip 파일 경로
    file_count: int                                           # 백업에 포함된 총 파일 수
    original_size: int                                        # 압축 전 원본 파일들의 총 크기 (바이트)
    archive_size: int                                         # 압축 후 생성된 zip 파일 크기 (바이트)
    removed_old: list[Path] = field(default_factory=list)     # 보관 개수(keep) 초과로 삭제된 이전 백업 목록
    errors: list[tuple[str, str]] = field(default_factory=list)  # 백업 중 발생한 (파일 경로, 오류 메시지) 목록


def _archive_pattern(prefix: str) -> re.Pattern:
    """`<폴더이름>_YYYY-MM-DD_HHMMSS(_번호).zip` 형식의 백업 파일명과 매칭되는 정규식을 생성합니다."""
    return re.compile(rf"^{re.escape(prefix)}_\d{{4}}-\d{{2}}-\d{{2}}_\d{{6}}(_\d+)?\.zip$")


def list_backups(dest: Path, prefix: str) -> list[Path]:
    """이 프로그램이 만든 백업 파일만 오래된 순서로 반환합니다(다른 zip은 건드리지 않음)."""
    dest = Path(dest)
    if not dest.is_dir():
        return []
    pat = _archive_pattern(prefix)
    # 타임스탬프가 파일명에 포함되어 있으므로 파일명 기준 정렬이 곧 생성 시각 순 정렬이 됩니다.
    return sorted((p for p in dest.iterdir() if p.is_file() and pat.match(p.name)), key=lambda p: p.name)


def create_backup(
    source: str | os.PathLike,
    dest: str | os.PathLike,
    keep: int = 7,
    exclude_dirs: Iterable[str] | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """원본 폴더(source)를 dest 폴더에 날짜별 zip으로 압축 백업하고 최근 keep개만 유지합니다."""
    source = Path(source).resolve()
    dest = Path(dest).resolve()
    if not source.is_dir():
        raise NotADirectoryError(f"백업할 폴더가 없습니다: {source}")
    if keep < 1:
        raise ValueError("keep은 1 이상이어야 합니다.")
    dest.mkdir(parents=True, exist_ok=True)

    # 백업 파일명 결정 (동일 시각에 이미 파일이 있으면 _1, _2 접미사 추가)
    prefix = source.name or "drive"
    stamp = (now or datetime.now()).strftime(STAMP_FORMAT)
    archive = dest / f"{prefix}_{stamp}.zip"
    n = 1
    while archive.exists():
        archive = dest / f"{prefix}_{stamp}_{n}.zip"
        n += 1
    # 압축 도중 중단되거나 실패했을 때 불완전한 zip이 남지 않도록 임시(.partial) 파일에 먼저 작성
    partial = archive.with_name(archive.name + ".partial")

    excluded = {d.lower() for d in (DEFAULT_EXCLUDE_DIRS if exclude_dirs is None else exclude_dirs)}
    result = BackupResult(archive=archive, file_count=0, original_size=0, archive_size=0)

    try:
        # DEFLATE 알고리즘(압축 레벨 6)으로 zip 아카이브 생성
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
                        # 아카이브 내부 경로는 원본 폴더 기준 상대 경로(POSIX 슬래시 형식)로 저장
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
        # 압축 또는 무결성 검사 중 예외가 발생하면 임시 파일을 정리하고 예외를 다시 던집니다.
        partial.unlink(missing_ok=True)
        raise

    result.archive_size = archive.stat().st_size

    # 보관 개수(keep)를 초과한 오래된 백업 파일을 찾아 오래된 순서대로 삭제합니다.
    backups = list_backups(dest, prefix)
    for old in backups[: max(0, len(backups) - keep)]:
        old.unlink()
        result.removed_old.append(old)
    return result
