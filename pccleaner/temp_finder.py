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

DAY = 24 * 60 * 60  # 하루(1일)를 초 단위로 환산한 상수 (86,400초)


@dataclass(frozen=True)
class TempFile:
    """탐지된 임시 파일 정보와 판정 사유, 경과 일수를 담는 불변 데이터 클래스."""

    file: FileInfo  # 대상 파일 메타데이터
    reason: str     # 임시 파일로 판정된 이유 (예: '패턴 *.tmp', '임시 폴더 temp/')
    age_days: int   # 마지막 수정 이후 경과한 일수


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
    # 대소문자 구분 없이 비교하기 위해 패턴과 임시 폴더 이름을 모두 소문자로 변환
    patterns = [p.lower() for p in patterns]
    temp_dirs = {d.lower() for d in temp_dirs}
    found: list[TempFile] = []
    for f in files:
        # 현재 시각과 파일 마지막 수정 시각의 차이를 일(day) 단위로 계산
        age = int((now - f.mtime) // DAY)
        if age < age_days:
            # 기준 일수(age_days)가 지나지 않은 최근 파일은 건너뜀
            continue
        name = f.path.name.lower()
        # 1) 파일 이름이 임시 파일 와일드카드 패턴에 일치하는지 확인
        reason = next((f"패턴 {p}" for p in patterns if fnmatch.fnmatchcase(name, p)), None)
        if reason is None:
            # 2) 파일 이름 패턴에 해당하지 않으면 임시 폴더 하위에 위치한 파일인지 확인
            parent = f.path.parent
            if root is not None:
                try:
                    # 루트 기준 상대 경로만 추출하여 상위 시스템 경로의 폴더명 오탐을 방지
                    parent = parent.relative_to(root)
                except ValueError:
                    pass
            parent_dir = next((part for part in parent.parts if part.lower() in temp_dirs), None)
            if parent_dir is not None:
                reason = f"임시 폴더 {parent_dir}/"
        if reason:
            found.append(TempFile(file=f, reason=reason, age_days=age))
    # 파일 크기가 큰 순서대로 정렬하여 반환
    return sorted(found, key=lambda t: -t.file.size)
