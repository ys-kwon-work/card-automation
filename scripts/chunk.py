#!/usr/bin/env python3
"""
Python 소스를 ast로 파싱해 함수 / 클래스 / 메서드 단위로 청킹한다.

각 청크는 JSONL 한 줄로 출력되며 형태는 다음과 같다:

    {
      "id":   "cloud_run/main.py::decrypt_bc_pdf::188",
      "text": "<청크 원본 소스>",
      "metadata": {
        "file":       "cloud_run/main.py",   # 저장소 루트 기준 상대경로(POSIX 슬래시)
        "name":       "decrypt_bc_pdf",       # 메서드는 "ClassName.method"
        "parent":     "",                     # 메서드면 소속 클래스명, 아니면 ""
        "kind":       "function",             # module | function | class | method
        "start_line": 188,                    # 데코레이터 포함, 1-기준, 양끝 포함
        "end_line":   189,
        "def_line":   188,                    # def / class 키워드가 있는 줄
        "n_lines":    2,
        "docstring":  ""                      # 있으면 docstring 첫 줄(최대 200자)
      }
    }

메타데이터 값은 chromadb가 그대로 받도록 전부 str / int 원시값이다.
표준 라이브러리만 사용하므로 가상환경 없이도 실행된다(Python 3.8+).

사용:
    python scripts/chunk.py                        # 저장소 전체 -> rag/chunks.jsonl
    python scripts/chunk.py cloud_run -o out.jsonl # 특정 경로만
    python scripts/chunk.py --class-mode full      # 클래스를 통짜 한 청크로
    python scripts/chunk.py -o -                   # 표준출력으로
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
import tokenize
from collections import Counter
from pathlib import Path

# 한국어 Windows 콘솔(cp949)에서도 한글이 깨지지 않도록 UTF-8로 고정
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 저장소 루트 = 이 스크립트 상위(scripts/)의 상위
REPO_ROOT = Path(__file__).resolve().parent.parent

# 재귀 탐색에서 건너뛸 디렉터리 이름
SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    "build", "dist", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


# ---------------------------------------------------------------------------
# 파일 수집 / 읽기
# ---------------------------------------------------------------------------
def iter_py_files(paths: list[Path]) -> list[Path]:
    """주어진 파일/디렉터리에서 .py 파일을 중복 없이 모아 정렬해 반환."""
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        if p.is_file():
            if p.suffix == ".py":
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    out.append(rp)
            continue
        for f in sorted(p.rglob("*.py")):
            if any(part in SKIP_DIRS for part in f.parts):
                continue
            rp = f.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(rp)
    return out


def read_source(path: Path) -> str:
    """coding 선언을 존중해 소스를 읽는다(대부분 utf-8)."""
    with tokenize.open(str(path)) as fh:
        return fh.read()


def rel_path(path: Path) -> str:
    """저장소 루트 기준 상대경로(POSIX). 루트 밖이면 절대경로."""
    rp = path.resolve()
    try:
        return rp.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return rp.as_posix()


# ---------------------------------------------------------------------------
# ast 노드 -> 청크
# ---------------------------------------------------------------------------
def start_line(node: ast.AST) -> int:
    """데코레이터까지 포함한 시작 줄 번호."""
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        return min(d.lineno for d in decorators)
    return node.lineno  # type: ignore[attr-defined]


def slice_lines(lines: list[str], lo: int, hi: int) -> str:
    """1-기준, 양끝 포함 범위의 원본 텍스트."""
    return "".join(lines[lo - 1:hi])


def first_docline(node: ast.AST) -> str:
    try:
        doc = ast.get_docstring(node, clean=True)
    except TypeError:
        return ""
    if not doc:
        return ""
    return doc.strip().splitlines()[0][:200]


def _meta(rel: str, name: str, parent: str, kind: str,
          lo: int, hi: int, def_line: int, doc: str) -> dict:
    return {
        "file": rel,
        "name": name,
        "parent": parent,
        "kind": kind,
        "start_line": lo,
        "end_line": hi,
        "def_line": def_line,
        "n_lines": hi - lo + 1,
        "docstring": doc,
    }


def make_chunk(rel: str, name: str, parent: str, kind: str,
               node: ast.AST, lines: list[str]) -> dict:
    lo = start_line(node)
    hi = node.end_lineno  # type: ignore[attr-defined]
    return {
        "id": f"{rel}::{name}::{lo}",
        "text": slice_lines(lines, lo, hi),
        "metadata": _meta(rel, name, parent, kind, lo, hi,
                          node.lineno, first_docline(node)),  # type: ignore[attr-defined]
    }


def module_chunk(rel: str, tree: ast.Module, lines: list[str]) -> dict | None:
    """함수/클래스 정의가 아닌 최상위 문장(임포트·상수·__main__ 블록 등)을 한 청크로."""
    segs: list[tuple[int, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, *DEF_TYPES)):
            continue
        segs.append((start_line(node), node.end_lineno))  # type: ignore[attr-defined]
    if not segs:
        return None
    text = "\n".join(slice_lines(lines, lo, hi).rstrip("\n") for lo, hi in segs) + "\n"
    lo = min(s[0] for s in segs)
    hi = max(s[1] for s in segs)
    meta = _meta(rel, "<module>", "", "module", lo, hi, lo, first_docline(tree))
    # 조각들이 흩어져 있을 수 있으므로 n_lines는 실제 담긴 줄 수로 보정
    meta["n_lines"] = sum(h - l + 1 for l, h in segs)
    return {"id": f"{rel}::<module>::{lo}", "text": text, "metadata": meta}


def chunk_file(path: Path, class_mode: str) -> list[dict]:
    rel = rel_path(path)
    src = read_source(path)
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        print(f"  건너뜀(구문 오류) {rel}:{exc.lineno}: {exc.msg}", file=sys.stderr)
        return []
    lines = src.splitlines(keepends=True)

    chunks: list[dict] = []
    mc = module_chunk(rel, tree, lines)
    if mc:
        chunks.append(mc)

    for node in tree.body:
        if isinstance(node, DEF_TYPES):
            chunks.append(make_chunk(rel, node.name, "", "function", node, lines))

        elif isinstance(node, ast.ClassDef):
            if class_mode == "full":
                chunks.append(make_chunk(rel, node.name, "", "class", node, lines))
                continue
            # class_mode == "methods": 클래스 헤더 + 메서드별 청크
            methods = [b for b in node.body if isinstance(b, DEF_TYPES)]
            cls_lo = start_line(node)
            header_hi = (start_line(methods[0]) - 1) if methods else node.end_lineno
            header_hi = max(header_hi, node.lineno)  # 헤더가 최소 class 줄은 포함
            chunks.append({
                "id": f"{rel}::{node.name}::{cls_lo}",
                "text": slice_lines(lines, cls_lo, header_hi),
                "metadata": _meta(rel, node.name, "", "class",
                                  cls_lo, header_hi, node.lineno, first_docline(node)),
            })
            for m in methods:
                chunks.append(
                    make_chunk(rel, f"{node.name}.{m.name}", node.name, "method", m, lines)
                )

    return chunks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="ast 기반 Python 함수/클래스 청킹 → JSONL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "paths", nargs="*", default=[str(REPO_ROOT)],
        help="청킹할 파일 또는 디렉터리 (기본: 저장소 루트 전체)",
    )
    ap.add_argument(
        "-o", "--output", default=str(REPO_ROOT / "rag" / "chunks.jsonl"),
        help="출력 JSONL 경로 (기본: rag/chunks.jsonl). '-' 이면 표준출력",
    )
    ap.add_argument(
        "--class-mode", choices=["methods", "full"], default="methods",
        help="methods: 클래스 헤더 + 메서드별 청크(기본) / full: 클래스를 통짜 1청크로",
    )
    ap.add_argument(
        "--min-lines", type=int, default=0,
        help="이 줄 수 미만인 청크는 제외 (기본: 0 = 제외 안 함)",
    )
    args = ap.parse_args(argv)

    files = iter_py_files([Path(p) for p in args.paths])
    if not files:
        print("대상 .py 파일이 없습니다.", file=sys.stderr)
        return 1

    all_chunks: list[dict] = []
    for f in files:
        for c in chunk_file(f, args.class_mode):
            if c["metadata"]["n_lines"] >= args.min_lines:
                all_chunks.append(c)

    if args.output == "-":
        for c in all_chunks:
            sys.stdout.write(json.dumps(c, ensure_ascii=False) + "\n")
    else:
        outp = Path(args.output)
        outp.parent.mkdir(parents=True, exist_ok=True)
        with outp.open("w", encoding="utf-8") as fh:
            for c in all_chunks:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    by_kind = Counter(c["metadata"]["kind"] for c in all_chunks)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))
    print(f"파일 {len(files)}개 → 청크 {len(all_chunks)}개  ({summary})", file=sys.stderr)
    if args.output != "-":
        print(f"저장: {rel_path(Path(args.output))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
