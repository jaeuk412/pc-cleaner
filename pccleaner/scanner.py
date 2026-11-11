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
    path: Path
    size: int
    mtime: float

    @property
    def extension(self) -> str:
        ext = self.path.suffix.lower()
        return ext if ext else "(없음)"


@dataclass
class ScanResult:
    root: Path
    files: list[FileInfo] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    def by_extension(self) -> dict[str, tuple[int, int]]:
        """확장자별 (파일 수, 총 크기). 크기 내림차순."""
        stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for f in self.files:
            stats[f.extension][0] += 1
            stats[f.extension][1] += f.size
        return dict(sorted(((k, (v[0], v[1])) for k, v in stats.items()), key=lambda kv: -kv[1][1]))

    def by_top_dir(self) -> dict[str, int]:
        """root 바로 아래 폴더별 총 크기. root에 있는 파일은 '.'으로 묶습니다."""
        stats: dict[str, int] = defaultdict(int)
        for f in self.files:
            rel = f.path.relative_to(self.root)
            key = rel.parts[0] if len(rel.parts) > 1 else "."
            stats[key] += f.size
        return dict(sorted(stats.items(), key=lambda kv: -kv[1]))

    def largest(self, n: int = 20) -> list[FileInfo]:
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

    excluded_names = {n.lower() for n in (DEFAULT_EXCLUDE_DIRS if exclude_dirs is None else exclude_dirs)}
    excluded_paths = [Path(p).resolve() for p in exclude_paths]
    result = ScanResult(root=root_path)

    def on_error(err: OSError) -> None:
        result.errors.append((str(err.filename), err.strerror or str(err)))

    for dirpath, dirnames, filenames in os.walk(root_path, onerror=on_error, followlinks=False):
        current = Path(dirpath)
        # dirnames를 제자리에서 수정하면 os.walk가 해당 폴더로 내려가지 않습니다.
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
                st = path.lstat()
            except OSError as exc:
                result.errors.append((str(path), exc.strerror or str(exc)))
                continue
            if path.is_symlink():
                continue
            result.files.append(FileInfo(path=path, size=st.st_size, mtime=st.st_mtime))
    return result
