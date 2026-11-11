"""스캔 결과를 HTML 리포트(그래프 포함)와 JSON으로 저장합니다. 외부 라이브러리 없이 SVG로 그립니다."""
from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path

from .cleaner import CleanAction, action_to_dict
from .duplicates import DuplicateGroup
from .scanner import ScanResult
from .temp_finder import TempFile
from .utils import human_size

# SVG 막대 그래프에서 항목별로 순환하며 사용할 색상 팔레트
PALETTE = ["#2563eb", "#0891b2", "#16a34a", "#ca8a04", "#ea580c", "#dc2626", "#9333ea", "#64748b"]


def _bar_chart(items: list[tuple[str, int]], title: str) -> str:
    """가로 막대 그래프 SVG."""
    if not items:
        return "<p class='muted'>데이터 없음</p>"
    # 각 행의 높이, 라벨 영역 너비, 막대 최대 너비, 값 텍스트 영역 너비 설정
    row_h, label_w, bar_w, val_w = 26, 170, 420, 90
    width = label_w + bar_w + val_w
    height = row_h * len(items) + 10
    # 가장 큰 값을 기준으로 막대 길이를 비율 계산 (0으로 나누기 방지용 or 1)
    peak = max(v for _, v in items) or 1
    rows = []
    for i, (label, value) in enumerate(items):
        y = i * row_h + 5
        w = max(2, value / peak * bar_w)
        color = PALETTE[i % len(PALETTE)]
        # 라벨이 너무 길면 말줄임표(…) 처리
        short = label if len(label) <= 22 else label[:21] + "…"
        rows.append(
            f"<text x='{label_w - 8}' y='{y + 16}' text-anchor='end' class='lbl'>{escape(short)}</text>"
            f"<rect x='{label_w}' y='{y + 3}' width='{w:.1f}' height='{row_h - 8}' rx='3' fill='{color}'>"
            f"<title>{escape(label)}: {human_size(value)}</title></rect>"
            f"<text x='{label_w + w + 6:.1f}' y='{y + 16}' class='val'>{human_size(value)}</text>"
        )
    return (
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{escape(title)}' class='chart'>"
        + "".join(rows)
        + "</svg>"
    )


def mask_home(path: Path) -> str:
    """사용자 홈 폴더(C:/Users/<이름>)를 '~'로 바꿔 표시합니다."""
    try:
        return str(Path("~") / Path(path).resolve().relative_to(Path.home().resolve()))
    except ValueError:
        return str(path)


def _rel(path: Path, root: Path) -> str:
    """스캔 루트 기준 상대 경로 문자열을 반환합니다 (루트 밖이면 전체 경로 반환)."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def build_html(
    scan: ScanResult,
    duplicates: list[DuplicateGroup],
    temps: list[TempFile],
    actions: list[CleanAction],
    age_days: int,
    top: int = 15,
    display_root: str | None = None,
) -> str:
    """스캔 및 분석 결과를 시각화된 단일 HTML 문서 문자열로 생성합니다."""
    root = scan.root
    # 공유용 리포트에 PC 사용자 이름이 드러나지 않도록 표시 경로를 바꿀 수 있습니다.
    root_label = display_root or mask_home(root)
    dup_waste = sum(g.wasted for g in duplicates)
    temp_size = sum(t.file.size for t in temps)
    reclaim = sum(a.size for a in actions)
    total = scan.total_size or 1

    # 확장자별 용량 상위 8개를 추리고 나머지는 '기타'로 합산
    ext_items = list(scan.by_extension().items())
    ext_chart = [(k, v[1]) for k, v in ext_items[:8]]
    if len(ext_items) > 8:
        ext_chart.append(("기타", sum(v[1] for _, v in ext_items[8:])))
    # 상위 폴더별 용량 상위 10개 추출
    dir_chart = list(scan.by_top_dir().items())[:10]

    # 상단 요약 카드 4종 구성
    cards = [
        ("전체 용량", human_size(scan.total_size), f"파일 {len(scan.files):,}개"),
        ("중복 파일 낭비", human_size(dup_waste), f"{len(duplicates)}개 그룹"),
        (f"{age_days}일 지난 임시 파일", human_size(temp_size), f"{len(temps)}개"),
        ("확보 가능 용량", human_size(reclaim), f"전체의 {reclaim / total * 100:.1f}%"),
    ]
    cards_html = "".join(
        f"<div class='card'><div class='k'>{escape(k)}</div><div class='v'>{escape(v)}</div><div class='s'>{escape(s)}</div></div>"
        for k, v, s in cards
    )

    # 대용량 파일, 중복 파일 그룹, 오래된 임시 파일 테이블 행(row) HTML 생성
    large_rows = "".join(
        f"<tr><td>{escape(_rel(f.path, root))}</td><td class='num'>{human_size(f.size)}</td>"
        f"<td>{datetime.fromtimestamp(f.mtime):%Y-%m-%d}</td></tr>"
        for f in scan.largest(top)
    )
    dup_rows = "".join(
        f"<tr><td><b>남김</b> {escape(_rel(g.keep.path, root))}"
        + "".join(f"<br><span class='del'>정리</span> {escape(_rel(r.path, root))}" for r in g.redundant)
        + f"</td><td class='num'>{human_size(g.size)} × {len(g.files)}</td><td class='num'>{human_size(g.wasted)}</td></tr>"
        for g in duplicates[:top]
    )
    temp_rows = "".join(
        f"<tr><td>{escape(_rel(t.file.path, root))}</td><td>{escape(t.reason)}</td>"
        f"<td class='num'>{t.age_days}일</td><td class='num'>{human_size(t.file.size)}</td></tr>"
        for t in temps[:top]
    )
    err_html = ""
    if scan.errors:
        err_html = f"<p class='muted'>접근하지 못한 경로 {len(scan.errors)}개는 건너뛰었습니다.</p>"

    # 다크 모드를 지원하는 반응형 스타일시트와 함께 전체 HTML 조립
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PC 용량 분석 리포트</title>
<style>
:root {{ --bg:#f8fafc; --fg:#0f172a; --muted:#64748b; --card:#fff; --line:#e2e8f0; --accent:#2563eb; --del:#dc2626; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f172a; --fg:#e2e8f0; --muted:#94a3b8; --card:#1e293b; --line:#334155; --accent:#60a5fa; --del:#f87171; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 "Malgun Gothic","Apple SD Gothic Neo",system-ui,sans-serif; }}
main {{ max-width:1080px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:24px; margin:0 0 4px; }} h2 {{ font-size:18px; margin:32px 0 12px; }}
.muted {{ color:var(--muted); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; margin-top:20px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
.card .k {{ color:var(--muted); font-size:13px; }} .card .v {{ font-size:24px; font-weight:700; }} .card .s {{ color:var(--muted); font-size:13px; }}
.card:last-child .v {{ color:var(--accent); }}
.grid2 {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); gap:16px; }}
.panel {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; overflow-x:auto; }}
.chart {{ width:100%; height:auto; }} .chart .lbl {{ font-size:12px; fill:var(--fg); }} .chart .val {{ font-size:12px; fill:var(--muted); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; word-break:break-all; }}
th {{ color:var(--muted); font-weight:600; }} td.num {{ text-align:right; white-space:nowrap; }}
.del {{ color:var(--del); font-weight:600; }}
code {{ background:var(--line); padding:1px 5px; border-radius:4px; }}
@media (max-width:520px) {{ .grid2 {{ grid-template-columns:1fr; }} }}
</style></head><body><main>
<h1>PC 용량 분석 리포트</h1>
<div class="muted">대상: <code>{escape(root_label)}</code> · 생성: {datetime.now():%Y-%m-%d %H:%M}</div>
<div class="cards">{cards_html}</div>
{err_html}
<div class="grid2">
  <div><h2>확장자별 용량</h2><div class="panel">{_bar_chart(ext_chart, "확장자별 용량")}</div></div>
  <div><h2>폴더별 용량</h2><div class="panel">{_bar_chart(dir_chart, "폴더별 용량")}</div></div>
</div>
<h2>중복 파일 (낭비 용량 순)</h2>
<div class="panel"><table><tr><th>파일</th><th>크기 × 개수</th><th>낭비</th></tr>{dup_rows or "<tr><td colspan=3 class='muted'>없음</td></tr>"}</table></div>
<h2>{age_days}일 이상 지난 임시 파일</h2>
<div class="panel"><table><tr><th>파일</th><th>판정 이유</th><th>경과</th><th>크기</th></tr>{temp_rows or "<tr><td colspan=4 class='muted'>없음</td></tr>"}</table></div>
<h2>대용량 파일 Top {top}</h2>
<div class="panel"><table><tr><th>파일</th><th>크기</th><th>수정일</th></tr>{large_rows}</table></div>
<p class="muted">정리 실행: <code>python main.py clean "{escape(root_label)}" --apply</code> (격리 폴더로 이동되며 <code>undo</code>로 복구 가능)</p>
</main></body></html>
"""


def build_json(
    scan: ScanResult, duplicates: list[DuplicateGroup], temps: list[TempFile], actions: list[CleanAction]
) -> str:
    """스캔 결과, 중복/임시 파일 목록, 정리 계획을 JSON 문자열로 직렬화하여 반환합니다."""
    data = {
        "root": str(scan.root),
        "generated": datetime.now().isoformat(timespec="seconds"),
        "total_files": len(scan.files),
        "total_size": scan.total_size,
        "by_extension": {k: {"count": c, "size": s} for k, (c, s) in scan.by_extension().items()},
        "duplicates": [
            {"size": g.size, "sha256": g.sha256, "keep": str(g.keep.path), "redundant": [str(f.path) for f in g.redundant]}
            for g in duplicates
        ],
        "temp_files": [{"path": str(t.file.path), "size": t.file.size, "reason": t.reason, "age_days": t.age_days} for t in temps],
        "planned_actions": [action_to_dict(a) for a in actions],
        "reclaimable_size": sum(a.size for a in actions),
        "errors": [{"path": p, "error": e} for p, e in scan.errors],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)
