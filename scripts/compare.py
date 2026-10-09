#!/usr/bin/env python3
"""
같은 질의를 **vector/hybrid RAG** 와 **graph RAG** 양쪽에 넣고 결과를 나란히 출력한다.

- vector : scripts/search.py 의 HybridSearcher (BM25 + 벡터 RRF)
- graph  : scripts/graph_search.py 의 GraphSearcher (어휘 씨앗 + 엣지 확장)

두 엔진을 1회씩만 로드하고 질의 목록을 돌린다. 질의마다:
  1) 좌(vector) / 우(graph) 2단 표
  2) 공통 / 각자 고유 히트 요약  (파일::심볼 기준)

사용:
    python scripts/compare.py                                  # 기본 질의 세트
    python scripts/compare.py "카드사별 소계 계산" "PDF 복호화"   # 직접 지정
    python scripts/compare.py -f queries.txt -n 6              # 파일에서(줄당 1개)
    python scripts/compare.py "소계 계산" --stacked --full      # 본문까지 세로로
    python scripts/compare.py "소계 계산" --json                # 기계 판독용

전제: 먼저 인덱스를 만들어 둘 것.
    python scripts/chunk.py && python scripts/index.py --reset      # vector
    python scripts/graph_build.py                                   # graph
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from search import HybridSearcher                    # noqa: E402
from graph_search import GraphSearcher               # noqa: E402

DEFAULT_QUERIES = [
    "카드사별 소계 계산",
    "분류별 지출 원형차트",
    "중복 거래 건너뛰기",
    "BC바로카드 PDF 처리 흐름",
    "PDF 비밀번호 복호화",
    "월별 시트 탭 생성",
]


# ---------------------------------------------------------------------------
# 한글(전각) 폭을 고려한 컬럼 정렬
# ---------------------------------------------------------------------------
def _dw(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _clip(s: str, w: int) -> str:
    out, cur = "", 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if cur + cw > w:
            return out + "…" if cur + 1 <= w else out
        out += c
        cur += cw
    return out


def _pad(s: str, w: int) -> str:
    s = _clip(s, w)
    return s + " " * max(0, w - _dw(s))


# ---------------------------------------------------------------------------
# 결과 -> 한 줄 요약
# ---------------------------------------------------------------------------
def _key(r: dict) -> str:
    m = r["metadata"]
    return f"{m['file']}::{m['name']}"


def _base(f: str) -> str:
    return f.rsplit("/", 1)[-1]


def _vec_line(r: dict) -> str:
    m = r["metadata"]
    br = f"b{r['bm25_rank']}" if r["bm25_rank"] else "b-"
    vr = f"v{r['vector_rank']}" if r["vector_rank"] else "v-"
    return (f"{r['rank']}. {r['score']:.4f} {m['name']}  "
            f"{_base(m['file'])}:{m['start_line']}  [{br} {vr}]")


_VIA = {"seed": "씨앗", "CALLS": "→호출", "CALLED_BY": "←피호출", "SIBLING": "·형제",
        "CONTAINS": "·포함", "CONTAINED_BY": "·상위", "INHERITS": "·상속",
        "INHERITED_BY": "·하위"}


def _graph_line(r: dict) -> str:
    m = r["metadata"]
    via = _VIA.get(r["via"], r["via"])
    tag = via if r["via"] == "seed" else f"{via}{r['hop']}"
    return (f"{r['rank']}. {r['score']:.4f} {m['name']}  "
            f"{_base(m['file'])}:{m['start_line']}  [{tag}]")


# ---------------------------------------------------------------------------
# 렌더링
# ---------------------------------------------------------------------------
def render_side_by_side(q: str, vec: list[dict], gr: list[dict], col: int) -> str:
    lines = [f"\n{'='*(col*2+3)}", f"질의: {q!r}", "-" * (col * 2 + 3)]
    lines.append(_pad("▎VECTOR  (BM25 + 벡터 RRF)", col) + " | " + "▎GRAPH  (씨앗 + 엣지 확장)")
    lines.append(_pad("", col) + " | ")
    for i in range(max(len(vec), len(gr))):
        left = _vec_line(vec[i]) if i < len(vec) else ""
        right = _graph_line(gr[i]) if i < len(gr) else ""
        lines.append(_pad(left, col) + " | " + right)
    lines.append(_overlap_summary(vec, gr))
    return "\n".join(lines)


def render_stacked(q: str, vec: list[dict], gr: list[dict], *, full: bool) -> str:
    from search import render_result as vrender
    from graph_search import render_result as grender
    out = [f"\n{'='*78}", f"질의: {q!r}", "=" * 78, "\n── VECTOR (BM25 + 벡터 RRF) " + "─" * 40]
    for r in vec:
        out.append(vrender(r, body=full, max_body_lines=10_000 if full else 12))
        out.append("")
    out.append("── GRAPH (씨앗 + 엣지 확장) " + "─" * 40)
    for r in gr:
        out.append(grender(r, body=full, explain=True, max_body_lines=10_000 if full else 12))
        out.append("")
    out.append(_overlap_summary(vec, gr))
    return "\n".join(out)


def _overlap_summary(vec: list[dict], gr: list[dict]) -> str:
    vk = {_key(r): r["rank"] for r in vec}
    gk = {_key(r): r["rank"] for r in gr}
    both = sorted(set(vk) & set(gk), key=lambda k: vk[k] + gk[k])
    only_v = [k for k in vk if k not in gk]
    only_g = [k for k in gk if k not in vk]

    def _short(k: str) -> str:
        return k.split("::", 1)[1]

    parts = [
        f"\n공통 {len(both)}: " + (", ".join(f"{_short(k)}(v{vk[k]}/g{gk[k]})" for k in both) or "—"),
        f"vector 고유: " + (", ".join(_short(k) for k in only_v) or "—"),
        f"graph  고유: " + (", ".join(_short(k) for k in only_g) or "—"),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="vector RAG vs graph RAG 결과를 같은 질의로 나란히 출력",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("queries", nargs="*", help="질의(없으면 기본 세트)")
    ap.add_argument("-f", "--queries-file", type=Path,
                    help="질의 목록 파일 (줄당 1개, # 주석 무시)")
    ap.add_argument("-n", "--n-results", type=int, default=6, help="각 엔진 출력 개수 (기본 6)")
    ap.add_argument("--pool", type=int, default=40, help="vector 후보풀 (기본 40)")
    ap.add_argument("--seeds", type=int, default=6, help="graph 씨앗 수 (기본 6)")
    ap.add_argument("--depth", type=int, default=2, help="graph 확장 홉 (기본 2)")
    ap.add_argument("--decay", type=float, default=0.5, help="graph 홉 감쇠 (기본 0.5)")
    ap.add_argument("--seeds-only", action="store_true", help="graph를 씨앗만(BM25 baseline)으로")
    ap.add_argument("--kind", nargs="+",
                    choices=("module", "function", "class", "method"), help="양쪽 kind 필터")
    ap.add_argument("--file", help="양쪽 파일경로 부분일치 필터")
    ap.add_argument("--stacked", action="store_true", help="2단 표 대신 세로로(본문 포함 가능)")
    ap.add_argument("--full", action="store_true", help="본문 전체 출력 (--stacked 강제)")
    ap.add_argument("--col", type=int, default=58, help="2단 표 컬럼 폭 (기본 58)")
    ap.add_argument("--json", action="store_true", help="결과를 JSON으로")
    ap.add_argument("--chunks", type=Path)
    ap.add_argument("--db", type=Path)
    ap.add_argument("--graph", type=Path)
    args = ap.parse_args(argv)

    queries = list(args.queries)
    if args.queries_file:
        for ln in args.queries_file.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                queries.append(ln)
    if not queries:
        queries = DEFAULT_QUERIES

    print("인덱스 로딩 중… (vector: 임베딩 모델, graph: json)", file=sys.stderr)
    hs_kw = {}
    if args.chunks:
        hs_kw["chunks_path"] = args.chunks
    if args.db:
        hs_kw["db_path"] = args.db
    vec_engine = HybridSearcher(quiet=True, **hs_kw)
    gr_engine = GraphSearcher(args.graph) if args.graph else GraphSearcher()

    kinds = set(args.kind) if args.kind else None
    stacked = args.stacked or args.full

    payload = []
    for q in queries:
        vec = vec_engine.search(q, k=args.n_results, pool=args.pool,
                                kinds=kinds, file_sub=args.file)
        gr = gr_engine.search(q, k=args.n_results, seeds=args.seeds, depth=args.depth,
                              decay=args.decay, kinds=kinds, file_sub=args.file,
                              seeds_only=args.seeds_only)
        if args.json:
            payload.append({
                "query": q,
                "vector": [{kk: vv for kk, vv in r.items() if kk != "text"} for r in vec],
                "graph": [{kk: vv for kk, vv in r.items() if kk != "text"} for r in gr],
            })
        elif stacked:
            print(render_stacked(q, vec, gr, full=args.full))
        else:
            print(render_side_by_side(q, vec, gr, args.col))

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
