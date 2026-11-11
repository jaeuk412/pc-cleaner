"""오래된 임시/캐시 파일을 찾습니다."""
from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .scanner import FileInfo

# 파일 이름 패턴 (대소문자 무시)
DEFAULT_TEMP_PATTERNS = (
    "*.tmp",
    "*.temp",
    "*.bak",
    "*.old",
    "*.log",
    "*.dmp",
    "~$*",  # MS Office 잠금 파일
    "thumbs.db",
    ".ds_store",
)

# 이 이름의 폴더 안에 있는 파일은 모두 임시 파일로 간주
DEFAULT_TEMP_DIRS = ("__pycache__", ".pytest_cache", ".cache", "temp", "tmp")

DAY = 24 * 60 * 60


@dataclass(frozen=True)
class TempFile:
    file: FileInfo
    reason: str
    age_days: int


def find_old_temp_files(
    files: Iterable[FileInfo],
    age_days: int = 30,
    patterns: Iterable[str] = DEFAULT_TEMP_PATTERNS,
    temp_dirs: Iterable[str] = DEFAULT_TEMP_DIRS,
    now: float | None = None,
    root: Path | None = None,
) -> list[TempFile]:
    """마지막 수정 후 age_days일 이상 지난 임시 파일 목록(크기 내림차순).

    임시 폴더 이름 검사는 **스캔 루트 기준 상대 경로**로만 합니다.
    (절대 경로로 검사하면 루트가 C:/Users/.../AppData/Local/Temp 아래에 있을 때
    모든 파일이 'Temp 폴더 안'으로 잘못 판정됩니다.)
    """
    now = time.time() if now is None else now
    patterns = [p.lower() for p in patterns]
    temp_dirs = {d.lower() for d in temp_dirs}
    found: list[TempFile] = []
    for f in files:
        age = int((now - f.mtime) // DAY)
        if age < age_days:
            continue
        name = f.path.name.lower()
        reason = next((f"패턴 {p}" for p in patterns if fnmatch.fnmatchcase(name, p)), None)
        if reason is None:
            parent = f.path.parent
            if root is not None:
                try:
                    parent = parent.relative_to(root)
                except ValueError:
                    pass
            parent_dir = next((part for part in parent.parts if part.lower() in temp_dirs), None)
            if parent_dir is not None:
                reason = f"임시 폴더 {parent_dir}/"
        if reason:
            found.append(TempFile(file=f, reason=reason, age_days=age))
    return sorted(found, key=lambda t: -t.file.size)
