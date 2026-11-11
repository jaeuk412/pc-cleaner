"""내용이 같은 중복 파일을 찾습니다.

모든 파일의 전체 해시를 계산하면 느리기 때문에 3단계로 후보를 줄입니다.
  1) 파일 크기가 같은 것끼리 묶기 (디스크 읽기 없음)
  2) 앞부분 64KB 해시가 같은 것끼리 묶기
  3) 전체 SHA-256 해시가 같은 것만 중복으로 확정
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .scanner import FileInfo

PARTIAL_BYTES = 64 * 1024
CHUNK = 1024 * 1024


def file_hash(path: Path, limit: int | None = None) -> str:
    """파일의 SHA-256을 계산합니다. limit이 있으면 앞부분 limit 바이트만 읽습니다."""
    h = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as fp:
        while True:
            size = CHUNK if remaining is None else min(CHUNK, remaining)
            if size == 0:
                break
            block = fp.read(size)
            if not block:
                break
            h.update(block)
            if remaining is not None:
                remaining -= len(block)
    return h.hexdigest()


@dataclass
class DuplicateGroup:
    size: int
    sha256: str
    files: list[FileInfo]  # files[0]이 남길 원본, 나머지가 정리 대상

    @property
    def keep(self) -> FileInfo:
        return self.files[0]

    @property
    def redundant(self) -> list[FileInfo]:
        return self.files[1:]

    @property
    def wasted(self) -> int:
        return self.size * len(self.redundant)


def _keep_order(f: FileInfo) -> tuple:
    # 가장 오래된 파일(원본일 가능성이 높음)을 남기고, 같으면 경로가 짧은 것을 남깁니다.
    return (f.mtime, len(str(f.path)), str(f.path).lower())


def _group_by_hash(files: Iterable[FileInfo], limit: int | None, errors: list) -> list[list[FileInfo]]:
    buckets: dict[str, list[FileInfo]] = defaultdict(list)
    for f in files:
        try:
            buckets[file_hash(f.path, limit)].append(f)
        except OSError as exc:
            errors.append((str(f.path), exc.strerror or str(exc)))
    return [g for g in buckets.values() if len(g) > 1]


def find_duplicates(
    files: Iterable[FileInfo], min_size: int = 1, errors: list | None = None
) -> list[DuplicateGroup]:
    """중복 그룹 목록을 낭비 용량이 큰 순서로 반환합니다."""
    errors = [] if errors is None else errors
    by_size: dict[int, list[FileInfo]] = defaultdict(list)
    for f in files:
        if f.size >= min_size:
            by_size[f.size].append(f)

    groups: list[DuplicateGroup] = []
    for size, same_size in by_size.items():
        if len(same_size) < 2:
            continue
        candidates = [same_size]
        if size > PARTIAL_BYTES:  # 작은 파일은 앞부분 해시 = 전체 해시이므로 생략
            candidates = _group_by_hash(same_size, PARTIAL_BYTES, errors)
        for cand in candidates:
            full_hash_groups: dict[str, list[FileInfo]] = defaultdict(list)
            for f in cand:
                try:
                    full_hash_groups[file_hash(f.path)].append(f)
                except OSError as exc:
                    errors.append((str(f.path), exc.strerror or str(exc)))
            for digest, members in full_hash_groups.items():
                if len(members) > 1:
                    groups.append(DuplicateGroup(size, digest, sorted(members, key=_keep_order)))
    return sorted(groups, key=lambda g: -g.wasted)
