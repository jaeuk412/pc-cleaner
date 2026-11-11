"""공통 유틸리티."""
from __future__ import annotations

from pathlib import Path


def human_size(num_bytes: float) -> str:
    """바이트 수를 사람이 읽기 쉬운 문자열로 변환합니다. 예: 1536 -> '1.5 KB'."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover


def is_relative_to(path: Path, parent: Path) -> bool:
    """path가 parent 내부(또는 parent 자신)인지 확인합니다."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
