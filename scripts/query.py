#!/usr/bin/env python3
"""
대화형 코드 검색 REPL.

터미널에 질문을 입력하면 하이브리드 검색(BM25 + 벡터)으로 가장 관련 있는
코드 청크 top-5를 본문과 함께 보여준다. 인덱스와 임베딩 모델은 시작할 때
한 번만 로드하고 이후 질문마다 재사용한다.

    python scripts/query.py
    질문> BC바로카드 PDF 비밀번호는 어떻게 푸나요?
    ...

종료 : 빈 줄에서 Ctrl-D(Windows는 Ctrl-Z Enter), 또는  :q / quit / exit
명령 :
    :n <숫자>       결과 개수 변경 (기본 5)
    :full           본문 전체 표시 토글 (기본은 40줄까지)
    :kind <k...>    kind 필터 (module/function/class/method), 인자 없으면 해제
    :file <문자열>  파일경로 부분일치 필터, 인자 없으면 해제
    :bm25 / :vec / :both   리트리버 선택
    :help           이 도움말

검색 로직은 scripts/search.py 의 HybridSearcher 를 그대로 재사용한다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from search import (  # noqa: E402
    DEFAULT_CHUNKS, DEFAULT_DB, DEFAULT_COLLECTION, KINDS,
    HybridSearcher, render_result,
)

# stdin 도 UTF-8로 — 파이프로 한글 질문을 넣을 때 cp949 오디코딩으로 생긴
# 서러게이트 문자가 임베딩 토크나이저(Rust)를 깨뜨리는 것을 막는다.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


HELP = """\
명령:
  :n <숫자>      결과 개수 변경
  :full          본문 전체 표시 토글
  :kind <k...>   kind 필터 (module/function/class/method), 인자 없으면 해제
  :file <문자열> 파일경로 부분일치 필터, 인자 없으면 해제
  :bm25 / :vec / :both   리트리버 선택
  :help          도움말
  :q / quit / exit       종료"""


def run_repl(searcher: HybridSearcher, *, n: int, full: bool) -> int:
    kinds: set[str] | None = None
    file_sub: str | None = None
    mode = "both"  # both | bm25 | vec

    print(f"컬렉션 '{searcher.collection.name}'  문서 {searcher.count}개  "
          f"모델 {searcher.embed_model}")
    print("질문을 입력하세요.  종료: :q  /  도움말: :help\n")

    while True:
        try:
            line = input("질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

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
            elif cmd == "full":
                full = not full
                print(f"  본문 전체 표시 = {full}\n")
            elif cmd == "kind":
                picked = [k for k in arg.split() if k in KINDS]
                kinds = set(picked) or None
                print(f"  kind 필터 = {sorted(kinds) if kinds else '해제'}\n")
            elif cmd == "file":
                file_sub = arg or None
                print(f"  file 필터 = {file_sub or '해제'}\n")
            elif cmd in ("bm25", "vec", "vector", "both"):
                mode = "vec" if cmd == "vector" else cmd
                print(f"  리트리버 = {mode}\n")
            else:
                print("  알 수 없는 명령. :help 참고\n")
            continue

        results = searcher.search(
            line, k=n,
            kinds=kinds, file_sub=file_sub,
            bm25_only=(mode == "bm25"), vector_only=(mode == "vec"),
        )
        if not results:
            print("  결과 없음.\n")
            continue

        print()
        for r in results:
            print(render_result(r, body=True,
                                max_body_lines=10_000 if full else 40))
            print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="대화형 하이브리드 코드 검색 (질문 -> 관련 청크 top-N)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-n", "--n-results", type=int, default=5, help="결과 개수 (기본 5)")
    ap.add_argument("--full", action="store_true", help="처음부터 본문 전체 표시")
    ap.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--collection", default=DEFAULT_COLLECTION)
    ap.add_argument("question", nargs="*",
                    help="주어지면 REPL 없이 이 질문 1건만 처리하고 종료")
    args = ap.parse_args(argv)

    print("인덱스/모델 로딩 중…", file=sys.stderr)
    searcher = HybridSearcher(args.chunks, args.db, args.collection)

    if args.question:
        q = " ".join(args.question)
        results = searcher.search(q, k=args.n_results)
        if not results:
            print("결과 없음.", file=sys.stderr)
            return 1
        print(f"질의: {q!r}\n")
        for r in results:
            print(render_result(r, body=True,
                                max_body_lines=10_000 if args.full else 40))
            print()
        return 0

    return run_repl(searcher, n=args.n_results, full=args.full)


if __name__ == "__main__":
    raise SystemExit(main())
