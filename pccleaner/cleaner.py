"""정리 계획 수립, 격리 폴더 이동, 되돌리기(undo), 영구 삭제(purge).

안전 설계:
  - 파일을 바로 지우지 않고 격리 폴더(quarantine)로 '이동'합니다.
  - 이동한 목록은 manifest.json에 기록되어 `undo`로 원위치 복구할 수 있습니다.
  - 실제 용량은 `purge`로 격리 폴더를 비울 때 확보됩니다.
  - 실행 직전에 파일 크기·수정 시간이 스캔 때와 같은지, 중복 원본이 아직 있는지 다시 확인합니다.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .duplicates import DuplicateGroup
from .temp_finder import TempFile
from .utils import is_relative_to

MANIFEST = "manifest.json"  # 각 격리 세션 폴더 안에 저장되는 작업 내역 메타데이터 파일명


@dataclass(frozen=True)
class CleanAction:
    """개별 파일에 대한 정리(격리 이동) 계획 항목을 나타내는 데이터 클래스."""

    path: Path                     # 정리 대상 파일 경로
    size: int                      # 스캔 당시 파일 크기 (바이트)
    mtime: float                   # 스캔 당시 마지막 수정 시각
    category: str                  # 정리 분류: "duplicate" (중복 파일) | "temp" (임시 파일)
    reason: str                    # 정리 사유 설명 문자열
    keep_path: Path | None = None  # 중복일 때 남겨 두는 원본


def plan_clean(
    duplicate_groups: Iterable[DuplicateGroup] = (),
    temp_files: Iterable[TempFile] = (),
) -> list[CleanAction]:
    """정리할 파일 목록을 만듭니다. 같은 파일이 두 번 들어가지 않도록 합니다."""
    actions: dict[Path, CleanAction] = {}
    # 1) 중복 파일 그룹에서 원본(keep)을 제외한 나머지(redundant) 파일들을 계획에 추가
    for group in duplicate_groups:
        for f in group.redundant:
            actions.setdefault(
                f.path,
                CleanAction(f.path, f.size, f.mtime, "duplicate", f"중복 (원본: {group.keep.path.name})", group.keep.path),
            )
    # 2) 오래된 임시 파일들을 계획에 추가 (이미 중복 파일로 등록된 경우 setdefault로 중복 등록 방지)
    for t in temp_files:
        actions.setdefault(
            t.file.path,
            CleanAction(t.file.path, t.file.size, t.file.mtime, "temp", f"{t.reason}, {t.age_days}일 경과"),
        )
    # 파일 크기가 큰 순서대로 정렬하여 반환
    return sorted(actions.values(), key=lambda a: -a.size)


@dataclass
class ExecuteResult:
    """정리 실행(격리 폴더 이동) 결과를 담는 데이터 클래스."""

    session_dir: Path                # 생성된 격리 세션 폴더 경로
    moved: list[dict]                # 격리 폴더로 이동 성공한 파일 기록 목록
    skipped: list[tuple[str, str]]   # 이동하지 않고 건너뛴 (파일 경로, 사유) 목록

    @property
    def moved_size(self) -> int:
        """실제로 격리 폴더로 이동된 파일들의 총 크기(바이트)를 계산합니다."""
        return sum(item["size"] for item in self.moved)


def _session_dir(quarantine_root: Path) -> Path:
    """현재 시각(YYYYMMDD_HHMMSS)을 이름으로 하는 고유한 격리 세션 폴더 경로를 생성합니다."""
    base = quarantine_root / datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate, n = base, 1
    # 같은 초에 여러 번 실행되어 폴더가 이미 존재하면 뒤에 _1, _2 번호를 붙입니다.
    while candidate.exists():
        candidate = base.with_name(f"{base.name}_{n}")
        n += 1
    return candidate


def _stored_path(session: Path, original: Path, root: Path) -> Path:
    """원본 파일이 격리 세션 폴더 내부(`session/files/...`)에 저장될 경로를 계산합니다."""
    try:
        # 스캔 루트 기준 상대 구조를 그대로 유지하여 격리 폴더 안에서도 폴더 구조를 보존합니다.
        rel = original.resolve().relative_to(root.resolve())
    except ValueError:
        # 루트 밖 파일은 계획 단계에서 걸러지지만, 만약을 위해 드라이브 문자를 폴더명으로 변환
        rel = Path(*(p.replace(":", "").strip("\\/") or "_" for p in original.resolve().parts))
    return session / "files" / rel


def execute_clean(actions: Iterable[CleanAction], root: Path, quarantine_root: Path) -> ExecuteResult:
    """정리 계획(actions)에 따라 파일들을 안전 검증 후 격리 세션 폴더로 이동합니다."""
    root = Path(root).resolve()
    quarantine_root = Path(quarantine_root).resolve()
    session = _session_dir(quarantine_root)
    session.mkdir(parents=True)
    result = ExecuteResult(session_dir=session, moved=[], skipped=[])

    try:
        for action in actions:
            src = Path(action.path)
            # 안전 검사 1: 스캔 루트 밖의 파일은 건드리지 않음
            if not is_relative_to(src, root):
                result.skipped.append((str(src), "스캔 루트 밖의 파일"))
                continue
            # 안전 검사 2: 파일이 여전히 존재하는지 확인
            if not src.is_file():
                result.skipped.append((str(src), "파일이 이미 없음"))
                continue
            # 안전 검사 3: 스캔 시점 이후에 파일 크기나 수정 시각이 바뀌었는지 확인 (TOCTOU 방지)
            st = src.stat()
            if st.st_size != action.size or abs(st.st_mtime - action.mtime) > 1:
                result.skipped.append((str(src), "스캔 이후 파일이 변경됨"))
                continue
            # 안전 검사 4: 중복 파일 정리는 보존할 원본(keep_path)이 실제로 남아 있을 때만 수행
            if action.keep_path is not None and not Path(action.keep_path).is_file():
                result.skipped.append((str(src), "중복 원본이 사라져서 보존"))
                continue
            # 격리 폴더 내 저장 경로를 만들고 파일 이동
            dst = _stored_path(session, src, root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(dst))
            except OSError as exc:
                result.skipped.append((str(src), f"이동 실패: {exc.strerror or exc}"))
                continue
            result.moved.append(
                {
                    "original": str(src),
                    "stored": str(dst.relative_to(session)),
                    "size": action.size,
                    "category": action.category,
                    "reason": action.reason,
                }
            )
    finally:
        # 중간에 예외가 나도 지금까지 옮긴 파일은 반드시 기록해야 복구할 수 있습니다.
        _write_manifest(session, root, result)
    return result


def _write_manifest(session: Path, root: Path, result: ExecuteResult) -> None:
    """격리 세션 폴더에 이동 내역과 건너뛴 내역을 담은 manifest.json 파일을 작성합니다."""
    data = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "restored": False,
        "total_size": result.moved_size,
        "items": result.moved,
        "skipped": [{"path": p, "reason": r} for p, r in result.skipped],
    }
    (session / MANIFEST).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_manifest(session: Path) -> dict:
    """지정한 격리 세션 폴더의 manifest.json 파일을 읽어 딕셔너리로 반환합니다."""
    return json.loads((Path(session) / MANIFEST).read_text(encoding="utf-8"))


def list_sessions(quarantine_root: Path) -> list[Path]:
    """격리 루트 폴더 아래의 유효한 격리 세션 폴더들을 이름(시간) 순으로 반환합니다."""
    root = Path(quarantine_root)
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if (p / MANIFEST).is_file()), key=lambda p: p.name)


def undo(session: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """격리된 파일을 원래 위치로 되돌립니다. (복구된 목록, 건너뛴 목록)"""
    session = Path(session)
    manifest = load_manifest(session)
    restored: list[str] = []
    skipped: list[tuple[str, str]] = []
    remaining: list[dict] = []
    for item in manifest["items"]:
        src = session / item["stored"]
        dst = Path(item["original"])
        if not src.exists():
            skipped.append((str(dst), "격리 폴더에 파일이 없음(이미 복구 또는 삭제됨)"))
            continue
        # 원래 위치에 새 파일이 생겼다면 데이터 손실을 막기 위해 덮어쓰지 않고 보류합니다.
        if dst.exists():
            skipped.append((str(dst), "원래 위치에 같은 이름의 파일이 있어 덮어쓰지 않음"))
            remaining.append(item)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        restored.append(str(dst))
    # 복구 후 남은 항목과 복구 완료 여부를 manifest.json에 갱신합니다.
    manifest["items"] = remaining
    manifest["restored"] = not remaining
    manifest["total_size"] = sum(i["size"] for i in remaining)
    (session / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # 파일이 빠져나간 빈 하위 폴더들을 정리합니다.
    _remove_empty_dirs(session / "files")
    return restored, skipped


def purge(session: Path) -> int:
    """격리 세션을 영구 삭제하고 확보된 바이트 수를 반환합니다."""
    session = Path(session)
    if not (session / MANIFEST).is_file():
        raise FileNotFoundError(f"격리 세션이 아닙니다(manifest 없음): {session}")
    # 삭제 전 세션 내부에 남아 있는 파일들의 총 크기를 계산합니다.
    freed = sum(p.stat().st_size for p in (session / "files").rglob("*") if p.is_file()) if (session / "files").exists() else 0
    shutil.rmtree(session)
    return freed


def sessions_older_than(quarantine_root: Path, days: int, now: float | None = None) -> list[Path]:
    """생성된 지 days일 이상 지난 격리 세션 폴더 목록을 반환합니다."""
    now = time.time() if now is None else now
    out = []
    for s in list_sessions(quarantine_root):
        created = datetime.fromisoformat(load_manifest(s)["created"]).timestamp()
        if now - created >= days * 86400:
            out.append(s)
    return out


def _remove_empty_dirs(path: Path) -> None:
    """하위 폴더부터 역순(깊은 경로 우선)으로 순회하며 비어 있는 폴더들을 삭제합니다."""
    if not path.is_dir():
        return
    for d in sorted((p for p in path.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError:
        pass


def action_to_dict(a: CleanAction) -> dict:
    """CleanAction 객체를 JSON 직렬화 가능한 딕셔너리로 변환합니다."""
    d = asdict(a)
    d["path"] = str(a.path)
    d["keep_path"] = str(a.keep_path) if a.keep_path else None
    return d
