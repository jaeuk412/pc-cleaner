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

MANIFEST = "manifest.json"


@dataclass(frozen=True)
class CleanAction:
    path: Path
    size: int
    mtime: float
    category: str  # "duplicate" | "temp"
    reason: str
    keep_path: Path | None = None  # 중복일 때 남겨 두는 원본


def plan_clean(
    duplicate_groups: Iterable[DuplicateGroup] = (),
    temp_files: Iterable[TempFile] = (),
) -> list[CleanAction]:
    """정리할 파일 목록을 만듭니다. 같은 파일이 두 번 들어가지 않도록 합니다."""
    actions: dict[Path, CleanAction] = {}
    for group in duplicate_groups:
        for f in group.redundant:
            actions.setdefault(
                f.path,
                CleanAction(f.path, f.size, f.mtime, "duplicate", f"중복 (원본: {group.keep.path.name})", group.keep.path),
            )
    for t in temp_files:
        actions.setdefault(
            t.file.path,
            CleanAction(t.file.path, t.file.size, t.file.mtime, "temp", f"{t.reason}, {t.age_days}일 경과"),
        )
    return sorted(actions.values(), key=lambda a: -a.size)


@dataclass
class ExecuteResult:
    session_dir: Path
    moved: list[dict]
    skipped: list[tuple[str, str]]

    @property
    def moved_size(self) -> int:
        return sum(item["size"] for item in self.moved)


def _session_dir(quarantine_root: Path) -> Path:
    base = quarantine_root / datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate, n = base, 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}_{n}")
        n += 1
    return candidate


def _stored_path(session: Path, original: Path, root: Path) -> Path:
    try:
        rel = original.resolve().relative_to(root.resolve())
    except ValueError:
        # 루트 밖 파일은 계획 단계에서 걸러지지만, 만약을 위해 드라이브 문자를 폴더명으로 변환
        rel = Path(*(p.replace(":", "").strip("\\/") or "_" for p in original.resolve().parts))
    return session / "files" / rel


def execute_clean(actions: Iterable[CleanAction], root: Path, quarantine_root: Path) -> ExecuteResult:
    root = Path(root).resolve()
    quarantine_root = Path(quarantine_root).resolve()
    session = _session_dir(quarantine_root)
    session.mkdir(parents=True)
    result = ExecuteResult(session_dir=session, moved=[], skipped=[])

    try:
        for action in actions:
            src = Path(action.path)
            if not is_relative_to(src, root):
                result.skipped.append((str(src), "스캔 루트 밖의 파일"))
                continue
            if not src.is_file():
                result.skipped.append((str(src), "파일이 이미 없음"))
                continue
            st = src.stat()
            if st.st_size != action.size or abs(st.st_mtime - action.mtime) > 1:
                result.skipped.append((str(src), "스캔 이후 파일이 변경됨"))
                continue
            if action.keep_path is not None and not Path(action.keep_path).is_file():
                result.skipped.append((str(src), "중복 원본이 사라져서 보존"))
                continue
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
    return json.loads((Path(session) / MANIFEST).read_text(encoding="utf-8"))


def list_sessions(quarantine_root: Path) -> list[Path]:
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
        if dst.exists():
            skipped.append((str(dst), "원래 위치에 같은 이름의 파일이 있어 덮어쓰지 않음"))
            remaining.append(item)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        restored.append(str(dst))
    manifest["items"] = remaining
    manifest["restored"] = not remaining
    manifest["total_size"] = sum(i["size"] for i in remaining)
    (session / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _remove_empty_dirs(session / "files")
    return restored, skipped


def purge(session: Path) -> int:
    """격리 세션을 영구 삭제하고 확보된 바이트 수를 반환합니다."""
    session = Path(session)
    if not (session / MANIFEST).is_file():
        raise FileNotFoundError(f"격리 세션이 아닙니다(manifest 없음): {session}")
    freed = sum(p.stat().st_size for p in (session / "files").rglob("*") if p.is_file()) if (session / "files").exists() else 0
    shutil.rmtree(session)
    return freed


def sessions_older_than(quarantine_root: Path, days: int, now: float | None = None) -> list[Path]:
    now = time.time() if now is None else now
    out = []
    for s in list_sessions(quarantine_root):
        created = datetime.fromisoformat(load_manifest(s)["created"]).timestamp()
        if now - created >= days * 86400:
            out.append(s)
    return out


def _remove_empty_dirs(path: Path) -> None:
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
    d = asdict(a)
    d["path"] = str(a.path)
    d["keep_path"] = str(a.keep_path) if a.keep_path else None
    return d
