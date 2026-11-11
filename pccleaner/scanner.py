"""폴더를 순회하며 파일 정보를 수집하고 용량을 집계합니다."""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .utils import is_relative_to

# 스캔에서 항상 제외할 폴더 이름 (시스템/개발 도구/본 프로그램 작업 폴더)
DEFAULT_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".pccleaner",
        "$RECYCLE.BIN",
        "System Volume Information",
    }
)


@dataclass(frozen=True)
class FileInfo:
    """스캔된 개별 파일의 메타데이터(경로, 크기, 수정 시각)를 담는 불변 데이터 클래스."""

    path: Path    # 파일의 절대 경로
    size: int     # 파일 크기 (바이트 단위)
    mtime: float  # 마지막 수정 시각 (Unix 타임스탬프)

    @property
    def extension(self) -> str:
        """파일의 소문자 확장자를 반환합니다. 확장자가 없으면 '(없음)'을 반환합니다."""
        ext = self.path.suffix.lower()
        return ext if ext else "(없음)"


@dataclass
class ScanResult:
    """폴더 스캔 결과 전체를 담는 데이터 클래스 (통계 집계 메서드 포함)."""

    root: Path                                                # 스캔 기준 루트 폴더 경로
    files: list[FileInfo] = field(default_factory=list)       # 수집된 파일 목록
    errors: list[tuple[str, str]] = field(default_factory=list)  # 접근 실패한 (경로, 오류 메시지) 목록

    @property
    def total_size(self) -> int:
        """수집된 모든 파일의 총 용량(바이트)을 계산합니다."""
        return sum(f.size for f in self.files)

    def by_extension(self) -> dict[str, tuple[int, int]]:
        """확장자별 (파일 수, 총 크기). 크기 내림차순."""
        # 기본값으로 [파일 개수, 총 바이트 수] 리스트를 가지는 딕셔너리 생성
        stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for f in self.files:
            stats[f.extension][0] += 1
            stats[f.extension][1] += f.size
        # 총 크기(v[1]) 기준 내림차순으로 정렬하여 반환
        return dict(sorted(((k, (v[0], v[1])) for k, v in stats.items()), key=lambda kv: -kv[1][1]))

    def by_top_dir(self) -> dict[str, int]:
        """root 바로 아래 폴더별 총 크기. root에 있는 파일은 '.'으로 묶습니다."""
        stats: dict[str, int] = defaultdict(int)
        for f in self.files:
            rel = f.path.relative_to(self.root)
            # 하위 폴더에 있으면 첫 번째 폴더 이름을 키로, 루트 직속 파일이면 '.'을 키로 사용
            key = rel.parts[0] if len(rel.parts) > 1 else "."
            stats[key] += f.size
        # 폴더별 총 크기 기준 내림차순으로 정렬하여 반환
        return dict(sorted(stats.items(), key=lambda kv: -kv[1]))

    def largest(self, n: int = 20) -> list[FileInfo]:
        """가장 용량이 큰 상위 n개 파일 목록을 크기 내림차순으로 반환합니다."""
        return sorted(self.files, key=lambda f: -f.size)[:n]


def scan(
    root: str | os.PathLike,
    exclude_dirs: Iterable[str] | None = None,
    exclude_paths: Iterable[str | os.PathLike] = (),
) -> ScanResult:
    """root 아래 모든 파일을 수집합니다.

    - 심볼릭 링크는 따라가지 않습니다(같은 파일을 두 번 세거나 순환하는 것 방지).
    - 접근 권한이 없는 폴더/파일은 건너뛰고 errors에 기록합니다.
    """
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise NotADirectoryError(f"폴더가 아닙니다: {root_path}")

    # 제외할 폴더 이름 집합(대소문자 구분 없음)과 제외할 절대 경로 목록 준비
    excluded_names = {n.lower() for n in (DEFAULT_EXCLUDE_DIRS if exclude_dirs is None else exclude_dirs)}
    excluded_paths = [Path(p).resolve() for p in exclude_paths]
    result = ScanResult(root=root_path)

    # os.walk 중 폴더 접근 권한 오류 등이 발생했을 때 호출할 콜백 함수
    def on_error(err: OSError) -> None:
        result.errors.append((str(err.filename), err.strerror or str(err)))

    for dirpath, dirnames, filenames in os.walk(root_path, onerror=on_error, followlinks=False):
        current = Path(dirpath)
        # dirnames를 제자리에서 수정하면 os.walk가 해당 폴더로 내려가지 않습니다.
        # 제외 대상 이름, 제외 경로 하위 폴더, 심볼릭 링크 폴더를 탐색에서 제외합니다.
        dirnames[:] = [
            d
            for d in dirnames
            if d.lower() not in excluded_names
            and not any(is_relative_to(current / d, p) for p in excluded_paths)
            and not (current / d).is_symlink()
        ]
        for name in filenames:
            path = current / name
            try:
                # 심볼릭 링크 자체의 정보를 확인하기 위해 lstat() 사용
                st = path.lstat()
            except OSError as exc:
                result.errors.append((str(path), exc.strerror or str(exc)))
                continue
            # 심볼릭 링크 파일은 중복 계산이나 원본 손상을 막기 위해 건너뜁니다.
            if path.is_symlink():
                continue
            result.files.append(FileInfo(path=path, size=st.st_size, mtime=st.st_mtime))
    return result
