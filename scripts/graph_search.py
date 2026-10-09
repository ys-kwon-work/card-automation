#!/usr/bin/env python3
"""
그래프 기반 코드 검색: 어휘 매칭으로 **씨앗(seed)** 노드를 찾고, 코드 그래프의
엣지(호출/포함/임포트)를 타고 이웃으로 **확장(expand)** 해 관련 코드를 모은다.

vector RAG(search.py)와의 차이 ─────────────────────────────────────────────
  vector    : 질의를 임베딩 → 코사인 거리로 top-k. 관계는 "의미가 가깝다" 하나뿐.
  graph     : 질의로 씨앗만 찾고, 그 다음은 **코드가 실제로 어떻게 연결됐는지**
              (A가 B를 호출한다 / 같은 파일이다 / 같은 클래스다)로 결과를 넓힌다.
  → "이 흐름에 뭐가 엮여 있나", "이 함수를 누가 부르나"류 질문에서 vector가
     놓치는 호출 상·하류를 정확히 끌어온다. 반대로 순수 의미 유사(동의어)만
     필요한 질문엔 어휘 씨앗이 약하면 확장도 약하다. → RAG_COMPARISON.md 참고.

파이프라인:
  1. seed   : rag/graph.json 의 각 노드 document(name+signature+docstring+body+file)
              에 대해 BM25(순수 파이썬 구현, 무의존성)로 상위 --seeds 개.
  2. expand : 각 씨앗에서 BFS로 --depth 홉까지. 홉마다 점수에 --decay 를 곱한다.
              엣지 방향별 가중치: 호출 하류(CALLS ↓) / 호출 상류(CALLED_BY ↑) /
              형제(SIBLING, 같은 parent) / 포함(CONTAINS).
  3. score  : node_score = max over paths( seed_score * Π decay*edge_w )
                           + --centrality * (normalized PageRank)
  4. 출력   : search.py 와 같은 모양 + "왜 떴는지"(경로) 설명.

무의존성(표준 라이브러리만). rank_bm25 / chromadb / sentence-transformers 불필요.

사용:
    python scripts/graph_search.py "BC바로카드 PDF 처리 흐름"
    python scripts/graph_search.py "소계 계산" --depth 2 --full
    python scripts/graph_search.py "parse_transactions 를 누가 부르나" --explain
    python scripts/graph_search.py "월별 탭 생성" --seeds-only     # 확장 없이 어휘만(=baseline)
    python scripts/graph_search.py "중복 거래 건너뛰기" --json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GRAPH = REPO_ROOT / "rag" / "graph.json"
KINDS = ("module", "function", "class", "method")

# 씨앗 어휘 점수에 곱하는 kind 가중치.
# module 노드는 docstring/상수가 길어 어휘 매칭이 광범위하게 걸리므로 강하게 낮춘다
# (검색이 원하는 건 보통 함수/메서드지 "파일 통째"가 아님).
KIND_SEED_WEIGHT = {"function": 1.0, "method": 1.0, "class": 0.85, "module": 0.3}

# 확장 시 엣지 종류별 전파 가중치(홉 decay 위에 곱해짐).
EDGE_WEIGHT = {
    "CALLS": 1.0,        # 씨앗이 부르는 함수(하류) — 강함
    "CALLED_BY": 0.9,    # 씨앗을 부르는 함수(상류) — 강함
    "SIBLING": 0.45,     # 같은 파일/클래스의 이웃 — 약함
    "CONTAINS": 0.6,     # 파일→심볼, 클래스→메서드
    "CONTAINED_BY": 0.5,
    "INHERITS": 0.7,
    "INHERITED_BY": 0.7,
}


# ---------------------------------------------------------------------------
# 토크나이저 (search.py와 동일 규칙 — 비교를 공정하게)
# ---------------------------------------------------------------------------
_RUN_RE = re.compile(r"[A-Za-z0-9]+|[가-힣]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z]|$)|[A-Z]?[a-z]+|[0-9]+")


def tokenize(text: str) -> list[str]:
    toks: list[str] = []
    for run in _RUN_RE.findall(text):
        if run.isascii():
            low = run.lower()
            toks.append(low)
            for sub in _CAMEL_RE.findall(run):
                s = sub.lower()
                if s and s != low:
                    toks.append(s)
        else:
            toks.append(run)
            if len(run) >= 2:
                toks.extend(run[i:i + 2] for i in range(len(run) - 1))
    return toks


# ---------------------------------------------------------------------------
# 순수 파이썬 BM25 (rank_bm25 불필요)
# ---------------------------------------------------------------------------
class BM25:
    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = corpus
        self.N = len(corpus)
        self.doc_len = [len(d) for d in corpus]
        self.avgdl = (sum(self.doc_len) / self.N) if self.N else 0.0
        df: dict[str, int] = defaultdict(int)
        self.tf: list[dict[str, int]] = []
        for d in corpus:
            seen: dict[str, int] = defaultdict(int)
            for t in d:
                seen[t] += 1
            self.tf.append(seen)
            for t in seen:
                df[t] += 1
        self.idf = {
            t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()
        }

    def scores(self, query: list[str]) -> list[float]:
        out = [0.0] * self.N
        for t in query:
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i in range(self.N):
                f = self.tf[i].get(t, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                out[i] += idf * (f * (self.k1 + 1)) / denom
        return out


# ---------------------------------------------------------------------------
# 그래프 검색기
# ---------------------------------------------------------------------------
class GraphSearcher:
    def __init__(self, graph_path: Path = DEFAULT_GRAPH, quiet: bool = False):
        if not Path(graph_path).exists():
            sys.exit(f"그래프 파일이 없습니다: {graph_path}\n"
                     f"먼저 `python scripts/graph_build.py` 를 실행하세요.")
        data = json.loads(Path(graph_path).read_text(encoding="utf-8"))
        self.meta = data.get("meta", {})

        # 내부 노드만 검색/확장 대상(external = 임포트·서드파티 호출 leaf)
        self.nodes: dict[str, dict] = {
            n["id"]: n for n in data["nodes"] if n.get("kind") != "external"
        }
        self.ids = list(self.nodes)
        self._idx = {nid: i for i, nid in enumerate(self.ids)}

        # 인접 리스트(양방향, 확장용 논리 엣지로 변환)
        self.adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
        raw_calls: list[tuple[str, str]] = []
        by_parent: dict[tuple[str, str], list[str]] = defaultdict(list)
        for e in data["edges"]:
            s, d, t = e["src"], e["dst"], e["type"]
            if t == "CALLS" and s in self.nodes and d in self.nodes:
                self.adj[s].append((d, "CALLS"))
                self.adj[d].append((s, "CALLED_BY"))
                raw_calls.append((s, d))
            elif t == "CONTAINS" and s in self.nodes and d in self.nodes:
                self.adj[s].append((d, "CONTAINS"))
                self.adj[d].append((s, "CONTAINED_BY"))
            elif t == "INHERITS" and s in self.nodes and d in self.nodes:
                self.adj[s].append((d, "INHERITS"))
                self.adj[d].append((s, "INHERITED_BY"))
        # 형제(SIBLING): 같은 **클래스**의 메서드끼리만. 같은 파일이라는 이유만으로
        # 잇는 건(1000줄짜리 모듈) 신호가 없어 노이즈만 늘린다 — 제외.
        for nid, n in self.nodes.items():
            if n["kind"] == "method" and n.get("parent"):
                by_parent[(n["file"], n["parent"])].append(nid)
        for group in by_parent.values():
            for a in group:
                for b_ in group:
                    if a != b_:
                        self.adj[a].append((b_, "SIBLING"))

        self.pagerank = self._pagerank(raw_calls)

        # BM25 코퍼스: 노드 document
        self._bm25 = BM25([tokenize(self._doc(n)) for n in self.nodes.values()])

        if not quiet:
            print(f"그래프: 노드 {len(self.nodes)} · CALLS {len(raw_calls)} "
                  f"· 생성 {self.meta.get('generated','?')}", file=sys.stderr)

    # -- document: 어휘 매칭에 쓰는 노드 텍스트 --
    @staticmethod
    def _doc(n: dict) -> str:
        return "\n".join(filter(None, [
            n.get("name", ""), n.get("qualname", ""), n.get("signature", ""),
            n.get("docstring", ""), n.get("file", ""), n.get("text", ""),
        ]))

    # -- 호출 그래프 PageRank (무가중, 표준식) --
    def _pagerank(self, calls: list[tuple[str, str]], d: float = 0.85,
                  iters: int = 40) -> dict[str, float]:
        N = len(self.ids)
        if not N:
            return {}
        out_links: dict[str, list[str]] = defaultdict(list)
        for s, t in calls:
            out_links[s].append(t)
        pr = {nid: 1.0 / N for nid in self.ids}
        for _ in range(iters):
            new = {nid: (1 - d) / N for nid in self.ids}
            dangling = 0.0
            for nid in self.ids:
                outs = out_links.get(nid)
                if not outs:
                    dangling += pr[nid]
                    continue
                share = d * pr[nid] / len(outs)
                for t in outs:
                    new[t] += share
            for nid in self.ids:
                new[nid] += d * dangling / N
            pr = new
        mx = max(pr.values()) or 1.0
        return {k: v / mx for k, v in pr.items()}

    def _keep(self, n: dict, kinds, file_sub) -> bool:
        if kinds and n.get("kind") not in kinds:
            return False
        if file_sub and file_sub.lower() not in str(n.get("file", "")).lower():
            return False
        return True

    # -----------------------------------------------------------------
    def search(self, query: str, *, k: int = 8, seeds: int = 6, depth: int = 2,
               decay: float = 0.5, centrality: float = 0.15,
               kinds=None, file_sub: str | None = None,
               seeds_only: bool = False) -> list[dict]:
        q_tok = tokenize(query)
        raw = self._bm25.scores(q_tok) if q_tok else [0.0] * len(self.ids)
        # kind 가중치 적용(module 억제)
        raw = [
            sc * KIND_SEED_WEIGHT.get(self.nodes[self.ids[i]]["kind"], 1.0)
            for i, sc in enumerate(raw)
        ]
        order = sorted(range(len(self.ids)), key=lambda i: raw[i], reverse=True)

        seed_list: list[tuple[str, float]] = []
        for i in order:
            if raw[i] <= 0.0:
                break
            seed_list.append((self.ids[i], raw[i]))
            if len(seed_list) >= seeds:
                break
        if not seed_list:
            return []
        top = seed_list[0][1] or 1.0
        seed_norm = {nid: sc / top for nid, sc in seed_list}

        # best[nid] = (score, via_type, hop, parent_seed, path_from_seed)
        best: dict[str, tuple[float, str, int, str, list[str]]] = {}
        for nid, sc in seed_norm.items():
            best[nid] = (sc, "seed", 0, nid, [nid])

        if not seeds_only:
            for seed_id, sbase in seed_norm.items():
                # (nid, score_so_far, hop, path)
                dq: deque[tuple[str, float, int, list[str]]] = deque([(seed_id, sbase, 0, [seed_id])])
                local_best: dict[str, float] = {seed_id: sbase}
                while dq:
                    cur, cur_sc, hop, path = dq.popleft()
                    if hop >= depth:
                        continue
                    for nxt, etype in self.adj.get(cur, []):
                        ew = EDGE_WEIGHT.get(etype, 0.3)
                        nsc = cur_sc * decay * ew
                        if nsc <= 1e-4:
                            continue
                        if nsc <= local_best.get(nxt, 0.0):
                            continue
                        local_best[nxt] = nsc
                        npath = path + [nxt]
                        prev = best.get(nxt)
                        if prev is None or nsc > prev[0]:
                            best[nxt] = (nsc, etype, hop + 1, seed_id, npath)
                        dq.append((nxt, nsc, hop + 1, npath))

        # centrality 보너스
        for nid in list(best):
            sc, via, hop, ps, path = best[nid]
            bonus = centrality * self.pagerank.get(nid, 0.0)
            best[nid] = (sc + bonus, via, hop, ps, path)

        ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)

        results: list[dict] = []
        for nid, (score, via, hop, ps, path) in ranked:
            n = self.nodes[nid]
            if not self._keep(n, kinds, file_sub):
                continue
            results.append({
                "rank": len(results) + 1,
                "score": score,
                "id": nid,
                "via": via,
                "hop": hop,
                "seed": ps,
                "seed_score": seed_norm.get(nid),
                "pagerank": round(self.pagerank.get(nid, 0.0), 4),
                "path": path,
                "metadata": {
                    "file": n["file"], "name": n["name"], "kind": n["kind"],
                    "parent": n.get("parent", ""), "start_line": n["start_line"],
                    "end_line": n["end_line"], "signature": n.get("signature", ""),
                    "docstring": n.get("docstring", ""),
                    "calls_out": n.get("calls_out", 0), "called_by": n.get("called_by", 0),
                },
                "text": n.get("text", ""),
            })
            if len(results) >= k:
                break
        return results


# ---------------------------------------------------------------------------
# 렌더링
# ---------------------------------------------------------------------------
_VIA_LABEL = {
    "seed": "씨앗(어휘 매칭)",
    "CALLS": "→ 호출 하류",
    "CALLED_BY": "← 호출 상류",
    "SIBLING": "· 같은 파일/클래스",
    "CONTAINS": "· 포함",
    "CONTAINED_BY": "· 상위",
    "INHERITS": "· 상속",
    "INHERITED_BY": "· 하위클래스",
}


def _short(node_id: str) -> str:
    return node_id.split("::", 1)[1] if "::" in node_id else node_id


def render_result(r: dict, *, body: bool = False, max_body_lines: int = 40,
                  explain: bool = False) -> str:
    m = r["metadata"]
    via = _VIA_LABEL.get(r["via"], r["via"])
    tag = f"{via}" if r["via"] == "seed" else f"{via} · {r['hop']}홉 · from {_short(r['seed'])}"
    lines = [
        f"[{r['rank']}] {r['score']:.4f}  {m['file']}:{m['start_line']}-{m['end_line']}  "
        f"{m['name']}  ({m['kind']})",
        f"    {tag}   calls_out={m['calls_out']} called_by={m['called_by']} pr={r['pagerank']}",
    ]
    if m.get("signature"):
        lines.append(f"    {m['signature']}")
    if m.get("docstring"):
        lines.append(f'    "{m["docstring"]}"')
    if explain and len(r["path"]) > 1:
        lines.append("    경로: " + " ─▶ ".join(_short(p) for p in r["path"]))
    if body and r["text"]:
        lines.append("    " + "-" * 68)
        text_lines = r["text"].rstrip("\n").splitlines()
        shown = text_lines[:max_body_lines]
        lines += ["    | " + ln for ln in shown]
        if len(text_lines) > max_body_lines:
            lines.append(f"    | … (+{len(text_lines) - max_body_lines}줄, --full 로 전체 보기)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="그래프 기반 코드 검색(씨앗 어휘매칭 + 엣지 확장)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("query", help="검색어(코드 식별자·한국어 자연어 모두 가능)")
    ap.add_argument("-n", "--n-results", type=int, default=8, help="출력 개수 (기본 8)")
    ap.add_argument("--seeds", type=int, default=6, help="어휘 씨앗 개수 (기본 6)")
    ap.add_argument("--depth", type=int, default=2, help="확장 BFS 홉 수 (기본 2)")
    ap.add_argument("--decay", type=float, default=0.5, help="홉당 점수 감쇠 (기본 0.5)")
    ap.add_argument("--centrality", type=float, default=0.15,
                    help="PageRank 보너스 가중치 (기본 0.15, 0이면 끔)")
    ap.add_argument("--seeds-only", action="store_true",
                    help="그래프 확장 없이 어휘 씨앗만(순수 BM25 baseline)")
    ap.add_argument("--kind", nargs="+", choices=KINDS, help="이 kind 만")
    ap.add_argument("--file", help="파일 경로에 이 문자열이 포함된 노드만")
    ap.add_argument("--explain", action="store_true", help="씨앗→결과 경로를 함께 출력")
    ap.add_argument("--full", action="store_true", help="노드 본문 전체 출력")
    ap.add_argument("--json", action="store_true", help="결과를 JSON으로")
    ap.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    args = ap.parse_args(argv)

    searcher = GraphSearcher(args.graph)
    results = searcher.search(
        args.query, k=args.n_results, seeds=args.seeds, depth=args.depth,
        decay=args.decay, centrality=args.centrality,
        kinds=set(args.kind) if args.kind else None, file_sub=args.file,
        seeds_only=args.seeds_only,
    )
    if not results:
        print("결과 없음. (어휘 씨앗이 하나도 안 잡혔습니다 — 식별자/키워드를 바꿔보세요)",
              file=sys.stderr)
        return 1

    if args.json:
        out = []
        for r in results:
            e = {kk: vv for kk, vv in r.items() if kk != "text"}
            e["score"] = round(e["score"], 6)
            if args.full:
                e["text"] = r["text"]
            out.append(e)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    mode = "씨앗만(BM25)" if args.seeds_only else f"씨앗{args.seeds}+확장 depth={args.depth} decay={args.decay}"
    print(f"질의: {args.query!r}   |  {mode}\n")
    big = 10_000
    for r in results:
        print(render_result(r, body=args.full, max_body_lines=big if args.full else 40,
                            explain=args.explain))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
