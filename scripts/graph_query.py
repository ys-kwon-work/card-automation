#!/usr/bin/env python3
"""
대화형 그래프 코드 검색 REPL.

query.py(하이브리드)와 같은 사용감이되, 엔진이 graph_search.py 의 GraphSearcher
(어휘 씨앗 + 그래프 확장)다. 그래프는 시작 시 1회 로드하고 질문마다 재사용한다.

    python scripts/graph_query.py
    질문> 소계 계산 로직 어디야
    ...

종료 : 빈 줄에서 Ctrl-D(Windows는 Ctrl-Z Enter), 또는 :q / quit / exit
명령 : :help 로 표시
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_search import (  # noqa: E402
    DEFAULT_GRAPH, KINDS, GraphSearcher, render_result,
)

for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HELP = """\
명령:
  :n <숫자>       결과 개수 변경
  :depth <숫자>   확장 홉 수 (0 = 씨앗만/BM25 baseline)
  :decay <실수>   홉당 감쇠 (기본 0.5)
  :full           본문 전체 표시 토글
  :explain        씨앗→결과 경로 표시 토글
  :kind <k...>    kind 필터 (module/function/class/method), 인자 없으면 해제
  :file <문자열>  파일경로 부분일치 필터, 인자 없으면 해제
  :help           도움말
  :q / quit / exit  종료"""


def run_repl(s: GraphSearcher, *, n: int, depth: int, decay: float,
             full: bool, explain: bool) -> int:
    kinds: set[str] | None = None
    file_sub: str | None = None

    print(f"그래프 노드 {len(s.nodes)}개  (생성 {s.meta.get('generated','?')})")
    print("질문을 입력하세요.  종료: :q  /  도움말: :help\n")

    while True:
        try:
            line = input("질문> ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("  (취소 — 종료는 :q)")
            continue

        if not line:
            continue
        if line in (":q", "quit", "exit", ":quit", ":exit"):
            return 0
        if line in (":help", ":h", "help", "?"):
            print(HELP + "\n")
            continue

        if line.startswith(":"):
            cmd, _, arg = line[1:].partition(" ")
            arg = arg.strip()
            if cmd == "n" and arg.isdigit():
                n = max(1, int(arg))
                print(f"  결과 개수 = {n}\n")
            elif cmd == "depth" and arg.isdigit():
                depth = int(arg)
                print(f"  확장 depth = {depth}{'  (씨앗만)' if depth == 0 else ''}\n")
            elif cmd == "decay":
                try:
                    decay = float(arg)
                    print(f"  decay = {decay}\n")
                except ValueError:
                    print("  숫자를 주세요 (예: :decay 0.4)\n")
            elif cmd == "full":
                full = not full
                print(f"  본문 전체 표시 = {full}\n")
            elif cmd == "explain":
                explain = not explain
                print(f"  경로 표시 = {explain}\n")
            elif cmd == "kind":
                picked = [k for k in arg.split() if k in KINDS]
                bad = [k for k in arg.split() if k not in KINDS]
                if bad:
                    print(f"  알 수 없는 kind: {bad} (가능: {', '.join(KINDS)}) — 필터 유지\n")
                else:
                    kinds = set(picked) or None
                    print(f"  kind 필터 = {sorted(kinds) if kinds else '해제'}\n")
            elif cmd == "file":
                file_sub = arg or None
                print(f"  file 필터 = {file_sub or '해제'}\n")
            else:
                print("  알 수 없는 명령. :help 참고\n")
            continue

        results = s.search(
            line, k=n, depth=depth, decay=decay,
            kinds=kinds, file_sub=file_sub, seeds_only=(depth == 0),
        )
        if not results:
            print("  결과 없음.\n")
            continue
        print()
        for r in results:
            print(render_result(r, body=True, explain=explain,
                                max_body_lines=10_000 if full else 40))
            print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="대화형 그래프 코드 검색 (질문 -> 씨앗 + 그래프 확장)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-n", "--n-results", type=int, default=5, help="결과 개수 (기본 5)")
    ap.add_argument("--depth", type=int, default=2, help="확장 홉 수 (기본 2, 0=씨앗만)")
    ap.add_argument("--decay", type=float, default=0.5, help="홉당 감쇠 (기본 0.5)")
    ap.add_argument("--full", action="store_true", help="처음부터 본문 전체 표시")
    ap.add_argument("--explain", action="store_true", help="처음부터 경로 표시")
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    ap.add_argument("question", nargs="*",
                    help="주어지면 REPL 없이 이 질문 1건만 처리하고 종료")
    args = ap.parse_args(argv)

    print("그래프 로딩 중…", file=sys.stderr)
    s = GraphSearcher(args.graph)

    if args.question:
        q = " ".join(args.question)
        results = s.search(q, k=args.n_results, depth=args.depth, decay=args.decay,
                           seeds_only=(args.depth == 0))
        if not results:
            print("결과 없음.", file=sys.stderr)
            return 1
        print(f"질의: {q!r}\n")
        for r in results:
            print(render_result(r, body=True, explain=args.explain,
                                max_body_lines=10_000 if args.full else 40))
            print()
        return 0

    return run_repl(s, n=args.n_results, depth=args.depth, decay=args.decay,
                    full=args.full, explain=args.explain)


if __name__ == "__main__":
    raise SystemExit(main())
