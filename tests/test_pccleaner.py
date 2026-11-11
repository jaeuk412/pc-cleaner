import os
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pccleaner.backup import create_backup, list_backups  # noqa: E402
from pccleaner.cleaner import execute_clean, list_sessions, plan_clean, purge, undo  # noqa: E402
from pccleaner.cli import main  # noqa: E402
from pccleaner.duplicates import find_duplicates  # noqa: E402
from pccleaner.scanner import scan  # noqa: E402
from pccleaner.temp_finder import find_old_temp_files  # noqa: E402
from pccleaner.utils import human_size  # noqa: E402
from tools.make_sample_data import make_sample  # noqa: E402

DAY = 86400


def write(path: Path, data: bytes, age_days: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if age_days:
        t = time.time() - age_days * DAY
        os.utime(path, (t, t))
    return path


@pytest.fixture
def sample(tmp_path):
    return make_sample(tmp_path / "sample")


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(5 * 1024**3) == "5.0 GB"


def test_scan_excludes_default_dirs(tmp_path):
    write(tmp_path / "a.txt", b"hello")
    write(tmp_path / ".git/objects/x", b"git data")
    write(tmp_path / ".pccleaner/quarantine/y", b"quarantined")
    result = scan(tmp_path)
    assert [f.path.name for f in result.files] == ["a.txt"]


def test_duplicates_keep_oldest_and_ignore_same_size_different_content(tmp_path):
    data = os.urandom(200_000)  # 64KB보다 커서 앞부분 해시 단계까지 거침
    write(tmp_path / "orig.bin", data, age_days=10)
    write(tmp_path / "copy/orig.bin", data, age_days=1)
    write(tmp_path / "other.bin", os.urandom(200_000))  # 같은 크기, 다른 내용
    # 앞 64KB는 같고 뒷부분만 다른 파일 → 전체 해시 단계에서 걸러져야 함
    write(tmp_path / "tricky.bin", data[:-1] + bytes([data[-1] ^ 0xFF]))
    groups = find_duplicates(scan(tmp_path).files)
    assert len(groups) == 1
    assert groups[0].keep.path.name == "orig.bin" and groups[0].keep.path.parent == tmp_path
    assert groups[0].wasted == 200_000


def test_temp_files_by_age_and_pattern(tmp_path):
    write(tmp_path / "old.tmp", b"x", age_days=40)
    write(tmp_path / "new.tmp", b"x", age_days=1)
    write(tmp_path / "old_doc.txt", b"x", age_days=400)
    write(tmp_path / "pkg/__pycache__/m.pyc", b"x", age_days=40)
    found = {t.file.path.name for t in find_old_temp_files(scan(tmp_path).files, age_days=30, root=tmp_path)}
    assert found == {"old.tmp", "m.pyc"}


def test_temp_dir_check_uses_relative_path(tmp_path):
    """루트 자체가 Temp 폴더 아래에 있어도 일반 파일을 임시 파일로 판정하면 안 된다."""
    root = tmp_path / "Temp" / "project"
    write(root / "report.docx", b"x", age_days=100)
    assert find_old_temp_files(scan(root).files, age_days=30, root=root) == []


def test_clean_undo_roundtrip(sample, tmp_path):
    result = scan(sample)
    dups = find_duplicates(result.files)
    temps = find_old_temp_files(result.files, root=result.root)
    actions = plan_clean(dups, temps)
    before = {p for p in sample.rglob("*") if p.is_file()}

    res = execute_clean(actions, sample, tmp_path / "q")
    assert len(res.moved) == len(actions) == 12
    after = {p for p in sample.rglob("*") if p.is_file()}
    assert before - after == {a.path for a in actions}
    # 원본은 남아 있어야 함
    assert (sample / "photos/2026_trip/IMG_0001.jpg").exists()
    # 최근 임시 파일은 정리 대상이 아님
    assert (sample / "downloads/in_progress.tmp").exists()

    restored, skipped = undo(list_sessions(tmp_path / "q")[-1])
    assert len(restored) == 12 and not skipped
    assert {p for p in sample.rglob("*") if p.is_file()} == before


def test_clean_skips_changed_file_and_missing_original(tmp_path):
    root = tmp_path / "root"
    data = os.urandom(1000)
    write(root / "keep.bin", data, age_days=5)
    write(root / "dup.bin", data, age_days=1)
    write(root / "old.tmp", b"abc", age_days=60)
    result = scan(root)
    actions = plan_clean(find_duplicates(result.files), find_old_temp_files(result.files, root=root))
    (root / "keep.bin").unlink()  # 원본이 스캔 후 사라짐 → 중복본은 지우면 안 됨
    write(root / "old.tmp", b"changed!", age_days=60)  # 스캔 후 내용 변경
    res = execute_clean(actions, root, tmp_path / "q")
    assert res.moved == []
    assert (root / "dup.bin").exists() and (root / "old.tmp").exists()


def test_undo_does_not_overwrite(tmp_path):
    root = tmp_path / "root"
    write(root / "a.tmp", b"old", age_days=60)
    result = scan(root)
    execute_clean(plan_clean([], find_old_temp_files(result.files, root=root)), root, tmp_path / "q")
    write(root / "a.tmp", b"new file with same name")
    restored, skipped = undo(list_sessions(tmp_path / "q")[-1])
    assert restored == [] and len(skipped) == 1
    assert (root / "a.tmp").read_bytes() == b"new file with same name"


def test_purge_frees_space(tmp_path):
    root = tmp_path / "root"
    write(root / "a.tmp", b"x" * 500, age_days=60)
    result = scan(root)
    res = execute_clean(plan_clean([], find_old_temp_files(result.files, root=root)), root, tmp_path / "q")
    assert purge(res.session_dir) == 500
    assert not res.session_dir.exists()


def test_backup_retention_and_integrity(tmp_path):
    src = tmp_path / "work"
    write(src / "a.txt", b"hello" * 100)
    write(src / "sub/b.txt", b"world")
    write(tmp_path / "backups/unrelated.zip", b"not ours")
    base = datetime(2026, 12, 1, 9, 0, 0)
    for i in range(4):
        create_backup(src, tmp_path / "backups", keep=2, now=base + timedelta(days=i))
    backups = list_backups(tmp_path / "backups", "work")
    assert [b.name for b in backups] == ["work_2026-12-03_090000.zip", "work_2026-12-04_090000.zip"]
    assert (tmp_path / "backups/unrelated.zip").exists()  # 다른 zip은 건드리지 않음
    with zipfile.ZipFile(backups[-1]) as zf:
        assert sorted(zf.namelist()) == ["a.txt", "sub/b.txt"]
        assert zf.read("a.txt") == b"hello" * 100


def test_backup_dest_inside_source(tmp_path):
    src = tmp_path / "work"
    write(src / "a.txt", b"data")
    create_backup(src, src / "backups", now=datetime(2026, 1, 1))
    res = create_backup(src, src / "backups", now=datetime(2026, 1, 2))
    with zipfile.ZipFile(res.archive) as zf:
        assert zf.namelist() == ["a.txt"]  # 이전 백업 zip이 다시 들어가면 안 됨


def test_cli_end_to_end(sample, tmp_path, capsys):
    workdir = str(tmp_path / "wd")
    report = tmp_path / "r.html"
    assert main(["--workdir", workdir, "scan", str(sample), "--report", str(report)]) == 0
    assert "PC 용량 분석 리포트" in report.read_text(encoding="utf-8")
    assert main(["--workdir", workdir, "scan", str(sample), "--report", str(report), "--display-root", "jaeuk/sample_data"]) == 0
    html = report.read_text(encoding="utf-8")
    assert "jaeuk/sample_data" in html and str(sample) not in html
    assert main(["--workdir", workdir, "clean", str(sample)]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert (sample / "downloads/chrome_download.tmp").exists()  # 미리보기는 아무것도 안 바꿈
    assert main(["--workdir", workdir, "clean", str(sample), "--apply"]) == 0
    assert not (sample / "downloads/chrome_download.tmp").exists()
    assert main(["--workdir", workdir, "undo"]) == 0
    assert (sample / "downloads/chrome_download.tmp").exists()


def test_sample_generator_refuses_non_sample_folder(tmp_path):
    important = tmp_path / "important"
    write(important / "thesis.docx", b"do not delete")
    with pytest.raises(SystemExit):
        make_sample(important)
    assert (important / "thesis.docx").exists()
