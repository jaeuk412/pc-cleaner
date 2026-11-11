"""공통 유틸리티."""
from __future__ import annotations

from pathlib import Path


def human_size(num_bytes: float) -> str:
    """바이트 수를 사람이 읽기 쉬운 문자열로 변환합니다. 예: 1536 -> '1.5 KB'."""
    size = float(num_bytes)
    # B부터 TB까지 1024 단위로 나누어가며 적절한 단위를 찾습니다.
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            if unit == "B":
                # 바이트 단위는 소수점 없이 정수로 표시합니다.
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover


def is_relative_to(path: Path, parent: Path) -> bool:
    """path가 parent 내부(또는 parent 자신)인지 확인합니다."""
    try:
        # 두 경로를 모두 절대 경로(resolve)로 변환한 뒤 상대 경로 계산이 가능한지 확인합니다.
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        # parent 하위에 속하지 않으면 ValueError가 발생하므로 False를 반환합니다.
        return False
