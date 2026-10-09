#!/usr/bin/env python3
"""
소스를 ast로 파싱해 **코드 그래프**(심볼 노드 + 관계 엣지)를 만든다.

vector RAG(chunk.py + index.py)가 "청크를 독립된 점"으로 보고 임베딩 거리로만
잇는 반면, 이쪽은 코드의 **구조적 관계**(포함/호출/임포트/상속)를 명시적인
엣지로 남긴다. 검색(graph_search.py)은 이 엣지를 타고 이웃을 확장한다.

산출물: rag/graph.json  (node-link JSON, networkx 없이도 읽을 수 있는 평문)

    {
      "meta":  {"files": 5, "nodes": 71, "edges": 120, "generated": "...", ...},
      "nodes": [
        {
          "id": "cloud_run/main.py::append_rows_to_sheet",   # 라인 번호 비의존(편집에 강함)
          "file": "cloud_run/main.py",
          "name": "append_rows_to_sheet",
          "qualname": "append_rows_to_sheet",
          "kind": "function",                 # module | function | class | method
          "parent": "",                       # 메서드면 소속 클래스 qualname
          "start_line": 693, "end_line": 731,
          "def_line": 693,
          "signature": "append_rows_to_sheet(card_name, transactions, filename='')",
          "docstring": "...",                  # 첫 줄, 최대 200자
          "calls_out": 8, "called_by": 1,     # 편의용 degree 캐시
          "loc": 39
        }, ...
      ],
      "edges": [
        {"src": "...::process", "dst": "...::append_rows_to_sheet", "type": "CALLS"},
        {"src": "...::main.py", "dst": "...::process", "type": "CONTAINS"},
        {"src": "...::main.py", "dst": "flask.Flask", "type": "IMPORTS", "external": true},
        {"src": "...::Foo", "dst": "...::Bar", "type": "INHERITS"},
        ...
      ]
    }

엣지 타입:
  CONTAINS  파일→심볼, 클래스→메서드            (구조)
  CALLS     심볼→심볼 (같은 저장소 내에서 이름으로 해석된 호출)
  CALLS_EXT 심볼→외부이름 (해석 실패/서드파티. external=true)
  IMPORTS   파일→임포트 대상 (external=true)
  INHERITS  클래스→기반 클래스

표준 라이브러리만 사용한다(ast, re, json). chunk.py와 같은 무의존성 원칙.
apps_script/Code.gs(자바스크립트)는 정규식 기반으로 함수만 얕게 훑어 포함한다.

사용:
    python scripts/graph_build.py                 # 저장소 전체 -> rag/graph.json
    python scripts/graph_build.py --stats          # 만들고 통계만 출력
    python scripts/graph_build.py cloud_run -o -    # 특정 경로만, stdout으로
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    "build", "dist", ".pytest_cache", ".mypy_cache", ".ruff_cache", "rag",
}

DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


# ---------------------------------------------------------------------------
# 파일 수집
# ---------------------------------------------------------------------------
def iter_files(paths: list[Path], suffixes: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        if p.is_file():
            if p.suffix in suffixes:
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    out.append(rp)
            continue
        for f in sorted(p.rglob("*")):
            if f.suffix not in suffixes:
                continue
            rel = f.resolve().relative_to(REPO_ROOT) if _under(f, REPO_ROOT) else f
            if any(part in SKIP_DIRS for part in getattr(rel, "parts", ())):
                continue
            rp = f.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(rp)
    return out


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def rel_path(path: Path) -> str:
    rp = path.resolve()
    try:
        return rp.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return rp.as_posix()


def read_source(path: Path) -> str:
    import tokenize
    with tokenize.open(str(path)) as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 노드/엣지 컨테이너
# ---------------------------------------------------------------------------
class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_seen: set[tuple[str, str, str]] = set()

    def add_node(self, node_id: str, **attrs) -> None:
        if node_id in self.nodes:
            self.nodes[node_id].update({k: v for k, v in attrs.items() if v not in (None, "")})
        else:
            self.nodes[node_id] = {"id": node_id, **attrs}

    def add_edge(self, src: str, dst: str, etype: str, **attrs) -> None:
        key = (src, dst, etype)
        if key in self._edge_seen:
            return
        self._edge_seen.add(key)
        self.edges.append({"src": src, "dst": dst, "type": etype, **attrs})


# ---------------------------------------------------------------------------
# ast 헬퍼
# ---------------------------------------------------------------------------
def _start_line(node: ast.AST) -> int:
    decos = getattr(node, "decorator_list", None)
    if decos:
        return min(d.lineno for d in decos)
    return node.lineno  # type: ignore[attr-defined]


def _first_docline(node: ast.AST) -> str:
    try:
        doc = ast.get_docstring(node, clean=True)
    except TypeError:
        return ""
    if not doc:
        return ""
    return doc.strip().splitlines()[0][:200]


def _signature(node: ast.AST) -> str:
    if not isinstance(node, DEF_TYPES):
        return ""
    a = node.args
    parts: list[str] = []
    pos = list(a.posonlyargs) + list(a.args)
    defaults = list(a.defaults)
    first_default = len(pos) - len(defaults)
    for i, arg in enumerate(pos):
        if i >= first_default:
            d = defaults[i - first_default]
            parts.append(f"{arg.arg}={_lit(d)}")
        else:
            parts.append(arg.arg)
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        parts.append("*")
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        parts.append(f"{arg.arg}={_lit(d)}" if d is not None else arg.arg)
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    return f"{node.name}({', '.join(parts)})"


def _lit(node: ast.AST | None) -> str:
    if node is None:
        return "None"
    try:
        return repr(ast.literal_eval(node))
    except Exception:
        return "…"


def _call_name(call: ast.Call) -> tuple[str, bool]:
    """호출식에서 (이름, self호출여부)를 뽑는다.

    foo()          -> ("foo", False)
    self.bar()     -> ("bar", True)     # 같은 클래스 메서드로 해석 시도
    obj.baz()      -> ("baz", False)    # 보통 외부
    a.b.c()        -> ("c",  False)
    """
    f = call.func
    if isinstance(f, ast.Name):
        return f.id, False
    if isinstance(f, ast.Attribute):
        base = f.value
        is_self = isinstance(base, ast.Name) and base.id == "self"
        return f.attr, is_self
    return "", False


# ---------------------------------------------------------------------------
# 파이썬 파일 -> 그래프
# ---------------------------------------------------------------------------
def add_python_file(g: Graph, path: Path) -> None:
    rel = rel_path(path)
    try:
        src = read_source(path)
    except Exception as exc:  # tokenize.open 실패(코딩 선언 오류 등)도 파일만 건너뜀
        print(f"  건너뜀(읽기 실패) {rel}: {exc}", file=sys.stderr)
        return
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        print(f"  건너뜀(구문 오류) {rel}:{exc.lineno}: {exc.msg}", file=sys.stderr)
        return

    lines = src.splitlines()
    n_lines = len(lines)
    file_id = f"{rel}::<module>"
    # 모듈 노드의 text는 파일 첫 def/class 전까지(임포트·상수·앱 초기화)만 담는다 —
    # chunk.py의 <module> 청크가 비연속 구간을 span하던 문제(RAG_REVIEW M-3)를 피함.
    first_def = min(
        (_start_line(n) for n in tree.body if isinstance(n, (ast.ClassDef, *DEF_TYPES))),
        default=n_lines + 1,
    )
    g.add_node(
        file_id, file=rel, name=rel.rsplit("/", 1)[-1], qualname="<module>",
        kind="module", parent="", start_line=1, end_line=max(1, first_def - 1), def_line=1,
        signature="", docstring=_first_docline(tree), lang="python",
        loc=max(1, first_def - 1), text="\n".join(lines[:first_def - 1]),
    )

    # --- 임포트 ---
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                g.add_node(alias.name, name=alias.name, kind="external", external=True)
                g.add_edge(file_id, alias.name, "IMPORTS", external=True)
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            for alias in node.names:
                target = f"{mod}.{alias.name}" if mod else alias.name
                g.add_node(target, name=alias.name, kind="external", external=True)
                g.add_edge(file_id, target, "IMPORTS", external=True)

    # --- 심볼 테이블(같은 저장소 호출 해석용): 단순명 -> [node_id...] ---
    local_defs: dict[str, list[str]] = defaultdict(list)

    def register(node_id: str, simple: str) -> None:
        local_defs[simple].append(node_id)

    def sym_id(name: str) -> str:
        return f"{rel}::{name}"

    top_symbols: list[tuple[ast.AST, str, str, str]] = []  # (node, node_id, kind, parent)

    for node in tree.body:
        if isinstance(node, DEF_TYPES):
            nid = sym_id(node.name)
            top_symbols.append((node, nid, "function", ""))
            register(nid, node.name)
        elif isinstance(node, ast.ClassDef):
            nid = sym_id(node.name)
            top_symbols.append((node, nid, "class", ""))
            register(nid, node.name)
            for b in node.body:
                if isinstance(b, DEF_TYPES):
                    mid = f"{rel}::{node.name}.{b.name}"
                    top_symbols.append((b, mid, "method", node.name))
                    register(mid, b.name)
                    register(mid, f"{node.name}.{b.name}")

    # --- 노드 생성 + CONTAINS/INHERITS ---
    for node, nid, kind, parent in top_symbols:
        lo, hi = _start_line(node), node.end_lineno  # type: ignore[attr-defined]
        name = nid.split("::", 1)[1]
        g.add_node(
            nid, file=rel, name=name, qualname=name, kind=kind, parent=parent,
            start_line=lo, end_line=hi, def_line=node.lineno,  # type: ignore[attr-defined]
            signature=_signature(node), docstring=_first_docline(node),
            lang="python", loc=hi - lo + 1, text="\n".join(lines[lo - 1:hi]),
        )
        if parent:
            g.add_edge(f"{rel}::{parent}", nid, "CONTAINS")
        else:
            g.add_edge(file_id, nid, "CONTAINS")
        if kind == "class":
            for base in getattr(node, "bases", []):
                bname = base.id if isinstance(base, ast.Name) else (
                    base.attr if isinstance(base, ast.Attribute) else None)
                if not bname:
                    continue
                targets = local_defs.get(bname)
                if targets:
                    g.add_edge(nid, targets[0], "INHERITS")
                else:
                    g.add_node(bname, name=bname, kind="external", external=True)
                    g.add_edge(nid, bname, "INHERITS", external=True)

    # --- CALLS: 각 심볼 본문을 훑어 호출을 해석 ---
    # 중첩 def의 호출도 부모 심볼에 귀속(이 저장소엔 중첩 def가 거의 없음 — 근사).
    method_owner: dict[str, str] = {
        nid: parent for node, nid, kind, parent in top_symbols if kind == "method"
    }
    for node, nid, kind, parent in top_symbols:
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            cname, is_self = _call_name(sub)
            if not cname:
                continue
            resolved: str | None = None
            if is_self and parent:
                cand = f"{rel}::{parent}.{cname}"
                if cand in g.nodes or cand in method_owner:
                    resolved = cand
            if resolved is None:
                targets = [t for t in local_defs.get(cname, []) if t != nid]
                # 메서드 단순명 충돌 시 같은 클래스 것을 우선
                if parent:
                    same_cls = [t for t in targets if t.startswith(f"{rel}::{parent}.")]
                    if same_cls:
                        targets = same_cls
                if targets:
                    resolved = targets[0]
            if resolved:
                g.add_edge(nid, resolved, "CALLS")
            else:
                ext_id = f"?{cname}"
                g.add_node(ext_id, name=cname, kind="external", external=True)
                g.add_edge(nid, ext_id, "CALLS_EXT", external=True)


# ---------------------------------------------------------------------------
# JavaScript(.gs) -> 얕은 함수 노드 (정규식)
# ---------------------------------------------------------------------------
_JS_FUNC_RE = re.compile(
    r"^(?:\s*)(?:function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)"
    r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>)",
    re.MULTILINE,
)


def add_js_file(g: Graph, path: Path) -> None:
    rel = rel_path(path)
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"  건너뜀(읽기 실패) {rel}: {exc}", file=sys.stderr)
        return
    lines = src.splitlines()
    file_id = f"{rel}::<module>"
    g.add_node(
        file_id, file=rel, name=path.name, qualname="<module>", kind="module",
        parent="", start_line=1, end_line=len(lines), def_line=1, signature="",
        docstring=_js_leading_comment(lines), lang="javascript", loc=len(lines),
        text="\n".join(lines[:60]),
    )

    funcs: list[tuple[str, str, int]] = []  # (name, signature, start_line)
    for m in _JS_FUNC_RE.finditer(src):
        name = m.group(1) or m.group(3)
        params = (m.group(2) or m.group(4) or "").strip()
        start_line = src.count("\n", 0, m.start()) + 1
        funcs.append((name, f"{name}({params})", start_line))

    names = {n for n, _, _ in funcs}
    ordered = sorted(funcs, key=lambda t: t[2])
    for i, (name, sig, sl) in enumerate(ordered):
        end = (ordered[i + 1][2] - 1) if i + 1 < len(ordered) else len(lines)
        nid = f"{rel}::{name}"
        body = "\n".join(lines[sl - 1:end])
        g.add_node(
            nid, file=rel, name=name, qualname=name, kind="function", parent="",
            start_line=sl, end_line=end, def_line=sl, signature=sig,
            docstring=_js_doc_for(lines, sl), lang="javascript", loc=end - sl + 1,
            text=body,
        )
        g.add_edge(file_id, nid, "CONTAINS")
        # 같은 파일 안에서 다른 함수 이름을 본문에서 부르면 CALLS 근사
        for other in names:
            if other != name and re.search(rf"\b{re.escape(other)}\s*\(", body):
                g.add_edge(nid, f"{rel}::{other}", "CALLS")


def _js_leading_comment(lines: list[str]) -> str:
    for ln in lines[:5]:
        s = ln.strip().lstrip("/* ").strip()
        if s:
            return s[:200]
    return ""


def _js_doc_for(lines: list[str], start_line: int) -> str:
    for j in range(start_line - 2, max(-1, start_line - 6), -1):
        if 0 <= j < len(lines):
            s = lines[j].strip()
            if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
                return s.lstrip("/*  ").strip()[:200]
            if s:
                break
    return ""


# ---------------------------------------------------------------------------
# degree 캐시 + 직렬화
# ---------------------------------------------------------------------------
def finalize(g: Graph) -> None:
    out_c: Counter = Counter()
    in_c: Counter = Counter()
    for e in g.edges:
        if e["type"] in ("CALLS", "CALLS_EXT"):
            out_c[e["src"]] += 1
            in_c[e["dst"]] += 1
    for nid, n in g.nodes.items():
        if n.get("kind") == "external":
            continue
        n["calls_out"] = out_c.get(nid, 0)
        n["called_by"] = in_c.get(nid, 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="ast 기반 코드 그래프 빌더 -> rag/graph.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("paths", nargs="*", default=[str(REPO_ROOT)],
                    help="대상 파일/디렉터리 (기본: 저장소 루트)")
    ap.add_argument("-o", "--output", default=str(REPO_ROOT / "rag" / "graph.json"),
                    help="출력 JSON 경로 (기본: rag/graph.json). '-' 이면 stdout")
    ap.add_argument("--no-js", action="store_true", help="apps_script/*.gs 제외")
    ap.add_argument("--stats", action="store_true", help="빌드 후 통계 출력")
    args = ap.parse_args(argv)

    roots = [Path(p) for p in args.paths]
    py_files = iter_files(roots, (".py",))
    js_files = [] if args.no_js else iter_files(roots, (".gs",))
    if not py_files and not js_files:
        print("대상 파일이 없습니다.", file=sys.stderr)
        return 1

    g = Graph()
    for f in py_files:
        add_python_file(g, f)
    for f in js_files:
        add_js_file(g, f)
    finalize(g)

    internal = [n for n in g.nodes.values() if n.get("kind") != "external"]
    payload = {
        "meta": {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "repo_root": str(REPO_ROOT),
            "files": len(py_files) + len(js_files),
            "nodes": len(internal),
            "nodes_incl_external": len(g.nodes),
            "edges": len(g.edges),
            "by_kind": dict(Counter(n["kind"] for n in internal)),
            "by_edge": dict(Counter(e["type"] for e in g.edges)),
        },
        "nodes": sorted(g.nodes.values(), key=lambda n: (n.get("file", "~"), n.get("start_line", 0))),
        "edges": g.edges,
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output == "-":
        sys.stdout.write(text + "\n")
    else:
        outp = Path(args.output)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(text, encoding="utf-8")
        print(f"저장: {rel_path(Path(args.output))}", file=sys.stderr)

    m = payload["meta"]
    print(f"파일 {m['files']}개 → 노드 {m['nodes']}개 "
          f"(+외부 {m['nodes_incl_external'] - m['nodes']}) · 엣지 {m['edges']}개",
          file=sys.stderr)
    print(f"  kind: {m['by_kind']}", file=sys.stderr)
    print(f"  edge: {m['by_edge']}", file=sys.stderr)

    if args.stats:
        print("\n=== 호출 많이 받는 심볼(called_by) ===", file=sys.stderr)
        for n in sorted(internal, key=lambda n: n.get("called_by", 0), reverse=True)[:10]:
            print(f"  {n['called_by']:3}  {n['file']}:{n['start_line']:<5} {n['name']}", file=sys.stderr)
        print("\n=== 호출 많이 하는 심볼(calls_out) ===", file=sys.stderr)
        for n in sorted(internal, key=lambda n: n.get("calls_out", 0), reverse=True)[:10]:
            print(f"  {n['calls_out']:3}  {n['file']}:{n['start_line']:<5} {n['name']}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
