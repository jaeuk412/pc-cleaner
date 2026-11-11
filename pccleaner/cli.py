"""명령행 인터페이스.

    python main.py scan    <폴더> [--report r.html] [--json r.json]
    python main.py clean   <폴더> [--apply]            # 기본은 미리보기(dry-run)
    python main.py undo    [--session 경로]             # 기본은 가장 최근 정리
    python main.py sessions
    python main.py purge   (--session 경로 | --older-than 일수) --yes
    python main.py backup  <폴더> --dest <백업폴더> [--keep 7]
    python main.py schedule --source <폴더> --dest <백업폴더> [--time 09:00]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .backup import create_backup
from .cleaner import (
    execute_clean,
    list_sessions,
    load_manifest,
    plan_clean,
    purge,
    sessions_older_than,
    undo,
)
from .duplicates import find_duplicates
from .report import build_html, build_json
from .scanner import scan
from .temp_finder import find_old_temp_files
from .utils import human_size

DEFAULT_WORKDIR = Path(".pccleaner")


def _quarantine_root(args) -> Path:
    return Path(args.workdir) / "quarantine"


def _analyze(args):
    workdir = Path(args.workdir).resolve()
    result = scan(args.root, exclude_paths=[workdir])
    print(f"[스캔] {result.root}  파일 {len(result.files):,}개, {human_size(result.total_size)}")
    if result.errors:
        print(f"  접근 불가 {len(result.errors)}개 건너뜀")
    dups = [] if getattr(args, "no_duplicates", False) else find_duplicates(result.files, args.min_size, result.errors)
    temps = (
        []
        if getattr(args, "no_temp", False)
        else find_old_temp_files(result.files, age_days=args.age_days, root=result.root)
    )
    actions = plan_clean(dups, temps)
    return result, dups, temps, actions


def _print_summary(result, dups, temps, actions, top: int) -> None:
    root = result.root
    rel = lambda p: str(Path(p).relative_to(root)) if str(p).startswith(str(root)) else str(p)  # noqa: E731
    print(f"\n[중복 파일] {len(dups)}개 그룹, 낭비 {human_size(sum(g.wasted for g in dups))}")
    for g in dups[:top]:
        print(f"  남김  {rel(g.keep.path)}  ({human_size(g.size)})")
        for r in g.redundant:
            print(f"   └ 정리 {rel(r.path)}")
    print(f"\n[오래된 임시 파일] {len(temps)}개, {human_size(sum(t.file.size for t in temps))}")
    for t in temps[:top]:
        print(f"  {rel(t.file.path)}  ({human_size(t.file.size)}, {t.reason}, {t.age_days}일)")
    print(f"\n[확보 가능 용량] {human_size(sum(a.size for a in actions))} (파일 {len(actions)}개)")


def cmd_scan(args) -> int:
    result, dups, temps, actions = _analyze(args)
    _print_summary(result, dups, temps, actions, args.top)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        html = build_html(result, dups, temps, actions, args.age_days, args.top, args.display_root)
        Path(args.report).write_text(html, encoding="utf-8")
        print(f"\nHTML 리포트 저장: {Path(args.report).resolve()}")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(build_json(result, dups, temps, actions), encoding="utf-8")
        print(f"JSON 저장: {Path(args.json).resolve()}")
    return 0


def cmd_clean(args) -> int:
    result, dups, temps, actions = _analyze(args)
    _print_summary(result, dups, temps, actions, args.top)
    if not actions:
        print("\n정리할 파일이 없습니다.")
        return 0
    if not args.apply:
        print("\n※ 미리보기(dry-run)입니다. 실제로 정리하려면 --apply 를 붙이세요.")
        return 0
    res = execute_clean(actions, result.root, _quarantine_root(args))
    print(f"\n[완료] {len(res.moved)}개 파일({human_size(res.moved_size)})을 격리 폴더로 이동했습니다.")
    print(f"  격리 위치: {res.session_dir}")
    for path, reason in res.skipped:
        print(f"  건너뜀: {path} ({reason})")
    print("  되돌리기: python main.py undo")
    print("  영구 삭제(실제 용량 확보): python main.py purge --session \"%s\" --yes" % res.session_dir)
    return 0


def _resolve_session(args) -> Path | None:
    if args.session:
        return Path(args.session)
    sessions = list_sessions(_quarantine_root(args))
    return sessions[-1] if sessions else None


def cmd_undo(args) -> int:
    session = _resolve_session(args)
    if session is None:
        print("되돌릴 정리 기록이 없습니다.")
        return 1
    restored, skipped = undo(session)
    print(f"[복구] {len(restored)}개 파일을 원래 위치로 되돌렸습니다. ({session.name})")
    for path, reason in skipped:
        print(f"  건너뜀: {path} ({reason})")
    return 0 if not skipped else 2


def cmd_sessions(args) -> int:
    sessions = list_sessions(_quarantine_root(args))
    if not sessions:
        print("격리된 정리 기록이 없습니다.")
        return 0
    print(f"{'세션':<20} {'생성 시각':<20} {'파일 수':>6} {'용량':>10}  상태")
    for s in sessions:
        m = load_manifest(s)
        state = "복구됨" if m.get("restored") else "격리 중"
        print(f"{s.name:<20} {m['created']:<20} {len(m['items']):>6} {human_size(m['total_size']):>10}  {state}")
    return 0


def cmd_purge(args) -> int:
    if args.session:
        targets = [Path(args.session)]
    elif args.older_than is not None:
        targets = sessions_older_than(_quarantine_root(args), args.older_than)
    else:
        print("--session 또는 --older-than 중 하나를 지정하세요.")
        return 1
    if not targets:
        print("삭제할 격리 세션이 없습니다.")
        return 0
    if not args.yes:
        print("다음 격리 세션을 영구 삭제합니다(복구 불가). 실행하려면 --yes 를 붙이세요.")
        for t in targets:
            print(f"  {t}")
        return 0
    total = 0
    for t in targets:
        freed = purge(t)
        total += freed
        print(f"  삭제: {t.name} ({human_size(freed)})")
    print(f"[완료] {human_size(total)} 확보")
    return 0


def cmd_backup(args) -> int:
    res = create_backup(args.source, args.dest, keep=args.keep)
    ratio = res.archive_size / res.original_size * 100 if res.original_size else 0
    print(f"[백업] {res.archive}")
    print(f"  파일 {res.file_count}개, {human_size(res.original_size)} → {human_size(res.archive_size)} ({ratio:.0f}%)")
    for old in res.removed_old:
        print(f"  보관 개수({args.keep}개) 초과로 삭제: {old.name}")
    for path, err in res.errors:
        print(f"  실패: {path} ({err})")
    return 0 if not res.errors else 2


def cmd_schedule(args) -> int:
    """Windows 작업 스케줄러 등록 명령을 출력합니다(직접 등록하지는 않음)."""
    python = Path(sys.executable).resolve()
    main_py = (Path(__file__).resolve().parent.parent / "main.py").resolve()
    source, dest = Path(args.source).resolve(), Path(args.dest).resolve()
    task_cmd = f'\\"{python}\\" \\"{main_py}\\" backup \\"{source}\\" --dest \\"{dest}\\" --keep {args.keep}'
    print("아래 명령을 PowerShell 또는 cmd에서 실행하면 매일 자동 백업이 등록됩니다:\n")
    print(f'schtasks /Create /SC DAILY /ST {args.time} /TN "{args.name}" /TR "{task_cmd}" /F\n')
    print(f'확인: schtasks /Query /TN "{args.name}"')
    print(f'해제: schtasks /Delete /TN "{args.name}" /F')
    print("\n(macOS/Linux crontab 예시)")
    print(f'{args.time[3:5]} {args.time[0:2]} * * * "{python}" "{main_py}" backup "{source}" --dest "{dest}" --keep {args.keep}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pccleaner", description="PC 용량 확보 및 백업 자동화 도구")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--workdir", default=str(DEFAULT_WORKDIR), help="격리 폴더/기록 저장 위치 (기본: ./.pccleaner)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_analysis_opts(sp):
        sp.add_argument("root", help="분석할 폴더")
        sp.add_argument("--age-days", type=int, default=30, help="임시 파일 판정 기준 경과 일수 (기본 30)")
        sp.add_argument("--min-size", type=int, default=1, help="중복 검사 최소 파일 크기(바이트)")
        sp.add_argument("--top", type=int, default=10, help="화면/리포트에 표시할 항목 수")

    sp = sub.add_parser("scan", help="용량 분석 및 리포트 생성")
    add_analysis_opts(sp)
    sp.add_argument("--report", help="HTML 리포트 저장 경로")
    sp.add_argument("--json", help="JSON 결과 저장 경로")
    sp.add_argument("--display-root", help="리포트에 표시할 대상 경로 (기본: 홈 폴더를 ~로 가림)")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("clean", help="중복/임시 파일 정리 (기본은 미리보기)")
    add_analysis_opts(sp)
    sp.add_argument("--apply", action="store_true", help="실제로 격리 폴더로 이동")
    sp.add_argument("--no-duplicates", action="store_true", help="중복 파일은 정리하지 않음")
    sp.add_argument("--no-temp", action="store_true", help="임시 파일은 정리하지 않음")
    sp.set_defaults(func=cmd_clean)

    sp = sub.add_parser("undo", help="정리한 파일을 원래 위치로 복구")
    sp.add_argument("--session", help="격리 세션 폴더 (기본: 가장 최근)")
    sp.set_defaults(func=cmd_undo)

    sp = sub.add_parser("sessions", help="격리 세션 목록")
    sp.set_defaults(func=cmd_sessions)

    sp = sub.add_parser("purge", help="격리 세션 영구 삭제 (실제 용량 확보)")
    sp.add_argument("--session", help="삭제할 세션 폴더")
    sp.add_argument("--older-than", type=int, help="N일 이상 지난 세션 모두 삭제")
    sp.add_argument("--yes", action="store_true", help="확인 없이 삭제")
    sp.set_defaults(func=cmd_purge)

    sp = sub.add_parser("backup", help="폴더를 날짜별 zip으로 백업")
    sp.add_argument("source", help="백업할 폴더")
    sp.add_argument("--dest", required=True, help="백업 저장 폴더 (클라우드 동기화 폴더 가능)")
    sp.add_argument("--keep", type=int, default=7, help="보관할 최근 백업 개수 (기본 7)")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("schedule", help="자동 백업 예약 명령 출력")
    sp.add_argument("--source", required=True)
    sp.add_argument("--dest", required=True)
    sp.add_argument("--keep", type=int, default=7)
    sp.add_argument("--time", default="09:00", help="실행 시각 HH:MM (기본 09:00)")
    sp.add_argument("--name", default="PCCleanerBackup", help="작업 이름")
    sp.set_defaults(func=cmd_schedule)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # 콘솔 인코딩 문제로 죽지 않도록
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (NotADirectoryError, FileNotFoundError, ValueError) as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 1
