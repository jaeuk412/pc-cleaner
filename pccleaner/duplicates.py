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

PARTIAL_BYTES = 64 * 1024  # 2단계 빠른 비교를 위해 읽을 앞부분 바이트 수 (64KB)
CHUNK = 1024 * 1024        # 대용량 파일 해시 계산 시 한 번에 읽을 블록 크기 (1MB)


def file_hash(path: Path, limit: int | None = None) -> str:
    """파일의 SHA-256을 계산합니다. limit이 있으면 앞부분 limit 바이트만 읽습니다."""
    h = hashlib.sha256()
    remaining = limit
    # 메모리를 과도하게 사용하지 않도록 CHUNK 단위로 나누어 읽으며 해시를 갱신합니다.
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
    """동일한 내용을 가진 중복 파일 그룹을 나타내는 데이터 클래스."""

    size: int              # 개별 파일 크기 (바이트)
    sha256: str            # 파일 전체의 SHA-256 해시값
    files: list[FileInfo]  # files[0]이 남길 원본, 나머지가 정리 대상

    @property
    def keep(self) -> FileInfo:
        """보존할 원본 파일(첫 번째 항목)을 반환합니다."""
        return self.files[0]

    @property
    def redundant(self) -> list[FileInfo]:
        """정리(격리) 대상이 되는 나머지 중복 파일 목록을 반환합니다."""
        return self.files[1:]

    @property
    def wasted(self) -> int:
        """중복 파일들로 인해 낭비되고 있는 총 용량(바이트)을 계산합니다."""
        return self.size * len(self.redundant)


def _keep_order(f: FileInfo) -> tuple:
    """중복 파일 중 어떤 파일을 원본(keep)으로 남길지 결정하는 정렬 키 함수."""
    # 가장 오래된 파일(원본일 가능성이 높음)을 남기고, 같으면 경로가 짧은 것을 남깁니다.
    return (f.mtime, len(str(f.path)), str(f.path).lower())


def _group_by_hash(files: Iterable[FileInfo], limit: int | None, errors: list) -> list[list[FileInfo]]:
    """주어진 파일들의 해시를 계산하여 해시값이 같은 파일(2개 이상)끼리 묶어 반환합니다."""
    buckets: dict[str, list[FileInfo]] = defaultdict(list)
    for f in files:
        try:
            buckets[file_hash(f.path, limit)].append(f)
        except OSError as exc:
            # 해시 계산 중 파일 읽기 권한 오류 등이 발생하면 건너뛰고 기록합니다.
            errors.append((str(f.path), exc.strerror or str(exc)))
    return [g for g in buckets.values() if len(g) > 1]


def find_duplicates(
    files: Iterable[FileInfo], min_size: int = 1, errors: list | None = None
) -> list[DuplicateGroup]:
    """중복 그룹 목록을 낭비 용량이 큰 순서로 반환합니다."""
    errors = [] if errors is None else errors
    # 1단계: 최소 크기(min_size) 이상인 파일들을 파일 크기별로 분류합니다.
    by_size: dict[int, list[FileInfo]] = defaultdict(list)
    for f in files:
        if f.size >= min_size:
            by_size[f.size].append(f)

    groups: list[DuplicateGroup] = []
    for size, same_size in by_size.items():
        # 같은 크기의 파일이 2개 미만이면 중복일 수 없으므로 건너뜁니다.
        if len(same_size) < 2:
            continue
        candidates = [same_size]
        # 2단계: 64KB보다 큰 파일은 앞부분 64KB 해시만 먼저 비교하여 후보를 좁힙니다.
        if size > PARTIAL_BYTES:  # 작은 파일은 앞부분 해시 = 전체 해시이므로 생략
            candidates = _group_by_hash(same_size, PARTIAL_BYTES, errors)
        # 3단계: 앞부분 해시까지 일치한 후보들에 대해 전체 파일 SHA-256 해시를 비교합니다.
        for cand in candidates:
            full_hash_groups: dict[str, list[FileInfo]] = defaultdict(list)
            for f in cand:
                try:
                    full_hash_groups[file_hash(f.path)].append(f)
                except OSError as exc:
                    errors.append((str(f.path), exc.strerror or str(exc)))
            for digest, members in full_hash_groups.items():
                if len(members) > 1:
                    # 보존 기준(_keep_order)으로 정렬하여 첫 번째 파일을 원본으로 지정합니다.
                    groups.append(DuplicateGroup(size, digest, sorted(members, key=_keep_order)))
    # 낭비되는 용량이 큰 그룹부터 내림차순으로 정렬하여 반환합니다.
    return sorted(groups, key=lambda g: -g.wasted)
