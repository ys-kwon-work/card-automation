#!/usr/bin/env python3
"""
하이브리드 코드 검색: BM25(어휘) + 벡터(의미)를 RRF로 융합한다.

- 벡터:  rag/chroma/ 의 chromadb 컬렉션(code_chunks) — scripts/index.py 산출물.
- BM25:  rag/chunks.jsonl 을 매 실행 시 읽어 메모리에서 rank_bm25로 랭킹.
- 융합:  Reciprocal Rank Fusion
             score(d) = Σ_retriever  w / (rrf_k + rank_retriever(d))
         점수 정규화가 필요 없어 이종 점수(코사인 거리 vs BM25 점수)를
         안전하게 합칠 수 있다.

토크나이저는 코드/한글 혼용을 노린다:
  - 식별자는 snake_case / camelCase 를 부분 토큰으로도 쪼갠다
    (parseTransactionsWithClaude -> parse, transactions, with, claude)
  - 한글 런은 원형 + 문자 bigram 을 함께 넣어 형태소 분석 없이도 부분 일치를 잡는다

핵심 로직은 HybridSearcher 클래스에 있고, 대화형 래퍼(scripts/query.py)가
이걸 그대로 재사용한다.

사용:
    python scripts/search.py "PDF 비밀번호 복호화"
    python scripts/search.py "월별 시트 탭 생성" -n 5 --full
    python scripts/search.py "소계 계산" --kind function --file main.py
    python scripts/search.py "삼성카드 더보기" --bm25-only     # 한쪽만 (비교용)
    python scripts/search.py "중복 거래 건너뛰기" --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 한국어 Windows 콘솔(cp949)에서도 한글/기호가 깨지지 않도록 UTF-8로 고정
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHUNKS = REPO_ROOT / "rag" / "chunks.jsonl"
DEFAULT_DB = REPO_ROOT / "rag" / "chroma"
DEFAULT_COLLECTION = "code_chunks"
# index.py와 동일한 기본 모델(컬렉션 metadata에 저장된 값이 있으면 그쪽을 우선 사용)
FALLBACK_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

KINDS = ("module", "function", "class", "method")


# ---------------------------------------------------------------------------
# 토크나이저 (BM25 전용)
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
# 입력 로드
# ---------------------------------------------------------------------------
def load_chunks(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"청크 파일이 없습니다: {path}\n먼저 `python scripts/chunk.py` 를 실행하세요.")
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                obj["id"], obj["text"], obj["metadata"]
            except (json.JSONDecodeError, KeyError) as exc:
                sys.exit(f"{path}:{i} 파싱 실패: {exc}")
            rows.append(obj)
    if not rows:
        sys.exit(f"{path} 에 청크가 없습니다.")
    return rows


# ---------------------------------------------------------------------------
# RRF 융합
# ---------------------------------------------------------------------------
def rrf_fuse(ranked_lists: dict[str, list[str]],
             weights: dict[str, float], rrf_k: int) -> dict[str, float]:
    scores: dict[str, float] = {}
    for name, ids in ranked_lists.items():
        w = weights.get(name, 1.0)
        for rank, cid in enumerate(ids, 1):
            scores[cid] = scores.get(cid, 0.0) + w / (rrf_k + rank)
    return scores


# ---------------------------------------------------------------------------
# 하이브리드 검색기 (BM25 코퍼스 + chroma 컬렉션을 1회 로드해 재사용)
# ---------------------------------------------------------------------------
class HybridSearcher:
    def __init__(self,
                 chunks_path: Path = DEFAULT_CHUNKS,
                 db_path: Path = DEFAULT_DB,
                 collection_name: str = DEFAULT_COLLECTION,
                 quiet: bool = False):
        self.chunks = load_chunks(chunks_path)
        self.by_id = {c["id"]: c for c in self.chunks}
        self._ids = [c["id"] for c in self.chunks]

        from rank_bm25 import BM25Okapi

        self._bm25 = BM25Okapi([
            tokenize(c["text"] + "\n" + c["metadata"]["name"] + "\n" + c["metadata"]["file"])
            for c in self.chunks
        ])

        if not Path(db_path).exists():
            sys.exit(f"chroma 저장소가 없습니다: {db_path}\n먼저 `python scripts/index.py` 를 실행하세요.")

        import chromadb
        from chromadb.utils import embedding_functions

        client = chromadb.PersistentClient(path=str(db_path))
        try:
            probe = client.get_collection(collection_name)
        except Exception as exc:
            sys.exit(f"컬렉션 '{collection_name}' 을 열 수 없습니다: {exc}")

        self.embed_model = (probe.metadata or {}).get("embed_model", FALLBACK_MODEL)
        ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=self.embed_model)
        self.collection = client.get_collection(collection_name, embedding_function=ef)
        self.count = self.collection.count()

        if not quiet and self.count != len(self.chunks):
            print(f"주의: 인덱스({self.count})와 청크 파일({len(self.chunks)})의 개수가 "
                  f"다릅니다 — `python scripts/index.py --reset` 로 재색인을 권장합니다.",
                  file=sys.stderr)

    def _keep(self, meta: dict, kinds, file_sub) -> bool:
        if kinds and meta.get("kind") not in kinds:
            return False
        if file_sub and file_sub.lower() not in str(meta.get("file", "")).lower():
            return False
        return True

    def search(self, query: str, *, k: int = 5, pool: int = 40, rrf_k: int = 60,
               w_bm25: float = 1.0, w_vec: float = 1.0,
               kinds=None, file_sub: str | None = None,
               bm25_only: bool = False, vector_only: bool = False) -> list[dict]:
        ranked: dict[str, list[str]] = {}
        bm_score: dict[str, float] = {}
        vec_dist: dict[str, float] = {}

        # ---- BM25 ----
        if not vector_only:
            q_tok = tokenize(query)
            if q_tok:
                scores = self._bm25.get_scores(q_tok)
                pairs = sorted(zip(self._ids, map(float, scores)),
                               key=lambda t: t[1], reverse=True)
                out: list[str] = []
                for cid, sc in pairs:
                    if sc <= 0.0:
                        break
                    if not self._keep(self.by_id[cid]["metadata"], kinds, file_sub):
                        continue
                    out.append(cid)
                    bm_score[cid] = sc
                    if len(out) >= pool:
                        break
                ranked["bm25"] = out

        # ---- 벡터 (chromadb) ----
        if not bm25_only:
            where = {"kind": {"$in": list(kinds)}} if kinds else None
            res = self.collection.query(
                query_texts=[query],
                n_results=min(pool, self.count),
                where=where,
            )
            out = []
            for cid, dist in zip(res["ids"][0], res["distances"][0]):
                if cid not in self.by_id or not self._keep(self.by_id[cid]["metadata"], kinds, file_sub):
                    continue
                out.append(cid)
                vec_dist[cid] = float(dist)
            ranked["vector"] = out

        # ---- 융합 ----
        fused = rrf_fuse(ranked, {"bm25": w_bm25, "vector": w_vec}, rrf_k)
        bm_rank = {cid: i for i, cid in enumerate(ranked.get("bm25", []), 1)}
        vec_rank = {cid: i for i, cid in enumerate(ranked.get("vector", []), 1)}

        results: list[dict] = []
        for rank, (cid, score) in enumerate(
            sorted(fused.items(), key=lambda t: t[1], reverse=True)[:k], 1
        ):
            c = self.by_id[cid]
            results.append({
                "rank": rank,
                "score": score,
                "id": cid,
                "bm25_rank": bm_rank.get(cid),
                "bm25_score": bm_score.get(cid),
                "vector_rank": vec_rank.get(cid),
                "distance": vec_dist.get(cid),
                "retrievers": list(ranked),
                "metadata": c["metadata"],
                "text": c["text"],
            })
        return results


# ---------------------------------------------------------------------------
# 결과 렌더링 (search.py / query.py 공용)
# ---------------------------------------------------------------------------
def render_result(r: dict, *, body: bool = False, max_body_lines: int = 40) -> str:
    m = r["metadata"]
    parts = [f"bm25#{r['bm25_rank'] if r['bm25_rank'] else '-'}",
             f"vec#{r['vector_rank'] if r['vector_rank'] else '-'}"]
    if r["distance"] is not None:
        parts.append(f"dist={r['distance']:.3f}")
    if r["bm25_score"] is not None:
        parts.append(f"bm25={r['bm25_score']:.2f}")

    lines = [
        f"[{r['rank']}] {r['score']:.4f}  {m['file']}:{m['start_line']}-{m['end_line']}  "
        f"{m['name']}  ({m['kind']})",
        f"    {'  '.join(parts)}",
    ]
    if m.get("docstring"):
        lines.append(f'    "{m["docstring"]}"')
    if body:
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
        description="BM25 + 벡터 하이브리드 코드 검색",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("query", help="검색어(코드 식별자·한국어 자연어 모두 가능)")
    ap.add_argument("-n", "--n-results", type=int, default=8, help="출력 개수 (기본 8)")
    ap.add_argument("--pool", type=int, default=40,
                    help="각 리트리버가 융합에 넘길 후보 수 (기본 40)")
    ap.add_argument("--rrf-k", type=int, default=60, help="RRF 상수 k (기본 60)")
    ap.add_argument("--w-bm25", type=float, default=1.0, help="BM25 가중치 (기본 1.0)")
    ap.add_argument("--w-vec", type=float, default=1.0, help="벡터 가중치 (기본 1.0)")
    ap.add_argument("--bm25-only", action="store_true", help="BM25 결과만")
    ap.add_argument("--vector-only", action="store_true", help="벡터 결과만")
    ap.add_argument("--kind", nargs="+", choices=KINDS, help="이 kind 만 검색")
    ap.add_argument("--file", help="파일 경로에 이 문자열이 포함된 청크만")
    ap.add_argument("--full", action="store_true", help="청크 본문 전체를 함께 출력")
    ap.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    ap.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--collection", default=DEFAULT_COLLECTION)
    args = ap.parse_args(argv)

    if args.bm25_only and args.vector_only:
        sys.exit("--bm25-only 와 --vector-only 는 함께 쓸 수 없습니다.")

    searcher = HybridSearcher(args.chunks, args.db, args.collection)
    results = searcher.search(
        args.query, k=args.n_results, pool=args.pool, rrf_k=args.rrf_k,
        w_bm25=args.w_bm25, w_vec=args.w_vec,
        kinds=set(args.kind) if args.kind else None, file_sub=args.file,
        bm25_only=args.bm25_only, vector_only=args.vector_only,
    )
    if not results:
        print("결과 없음.", file=sys.stderr)
        return 1

    if args.json:
        out = []
        for r in results:
            e = {k: v for k, v in r.items() if k != "text"}
            e["score"] = round(e["score"], 6)
            if e["bm25_score"] is not None:
                e["bm25_score"] = round(e["bm25_score"], 4)
            if e["distance"] is not None:
                e["distance"] = round(e["distance"], 4)
            if args.full:
                e["text"] = r["text"]
            out.append(e)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    mode = "+".join(results[0]["retrievers"])
    print(f"질의: {args.query!r}   |  리트리버: {mode}  |  후보풀: {args.pool}\n")
    big = 10_000
    for r in results:
        print(render_result(r, body=args.full, max_body_lines=big if args.full else 40))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
