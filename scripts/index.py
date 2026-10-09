#!/usr/bin/env python3
"""
scripts/chunk.py가 만든 rag/chunks.jsonl을 chromadb에 색인한다.

- 영구 저장소: rag/chroma/  (PersistentClient)
- 컬렉션:      code_chunks
- 임베딩:      sentence-transformers 다국어 모델(한글 주석/문서 대응).
               chromadb의 SentenceTransformerEmbeddingFunction으로 감싸므로
               색인·검색 양쪽에서 chroma가 자동으로 같은 방식으로 임베딩한다.
- 거리:        cosine (문장 임베딩에 적합)

각 청크는 id / document(text) / metadata 그대로 upsert되며, 재실행하면 같은
id를 덮어쓴다(--reset 주면 컬렉션을 통째로 지우고 다시 만든다).

사용:
    python scripts/index.py                 # rag/chunks.jsonl -> rag/chroma/
    python scripts/index.py --reset         # 컬렉션 초기화 후 재색인
    python scripts/index.py --query "PDF 복호화"   # 색인 후 간단 검색 테스트
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 한국어 Windows 콘솔(cp949)에서도 한글이 깨지지 않도록 UTF-8로 고정
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CHUNKS = REPO_ROOT / "rag" / "chunks.jsonl"
DEFAULT_DB = REPO_ROOT / "rag" / "chroma"
DEFAULT_COLLECTION = "code_chunks"
# 다국어 검색 모델. 한글 주석/docstring 비중이 커서 영어 전용 모델은 피한다.
# prefix 규칙이 없어 색인/검색 코드가 단순하다(e5 계열은 passage:/query: 필요).
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

BATCH = 128


def load_chunks(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"청크 파일이 없습니다: {path}\n먼저 `python scripts/chunk.py`를 실행하세요.")
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                # 필수 키 확인
                obj["id"], obj["text"], obj["metadata"]
            except (json.JSONDecodeError, KeyError) as exc:
                sys.exit(f"{path}:{i} 파싱 실패: {exc}")
            rows.append(obj)
    if not rows:
        sys.exit(f"{path}에 청크가 없습니다.")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="rag/chunks.jsonl -> chromadb 색인",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS,
                    help=f"입력 JSONL (기본: {DEFAULT_CHUNKS.relative_to(REPO_ROOT)})")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB,
                    help=f"chroma 영구 저장 경로 (기본: {DEFAULT_DB.relative_to(REPO_ROOT)})")
    ap.add_argument("--collection", default=DEFAULT_COLLECTION,
                    help=f"컬렉션 이름 (기본: {DEFAULT_COLLECTION})")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"sentence-transformers 모델 (기본: {DEFAULT_MODEL})")
    ap.add_argument("--reset", action="store_true",
                    help="색인 전 컬렉션을 삭제하고 새로 만든다")
    ap.add_argument("--query", metavar="TEXT",
                    help="색인 후 이 문장으로 상위 5개 검색 결과를 출력(동작 확인용)")
    args = ap.parse_args(argv)

    chunks = load_chunks(args.chunks)
    print(f"청크 {len(chunks)}개 로드: {args.chunks}", file=sys.stderr)

    import chromadb
    from chromadb.utils import embedding_functions

    print(f"임베딩 모델 로딩: {args.model} (최초 실행 시 다운로드)", file=sys.stderr)
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=args.model)

    args.db.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(args.db))

    if args.reset:
        try:
            client.delete_collection(args.collection)
            print(f"기존 컬렉션 삭제: {args.collection}", file=sys.stderr)
        except Exception:
            pass

    collection = client.get_or_create_collection(
        name=args.collection,
        embedding_function=ef,
        metadata={
            "hnsw:space": "cosine",
            "embed_model": args.model,
            "source": "scripts/chunk.py + scripts/index.py",
        },
    )

    ids = [c["id"] for c in chunks]
    docs = [c["text"] for c in chunks]
    metas = [c["metadata"] for c in chunks]

    for i in range(0, len(ids), BATCH):
        sl = slice(i, i + BATCH)
        collection.upsert(ids=ids[sl], documents=docs[sl], metadatas=metas[sl])
        print(f"  upsert {min(i + BATCH, len(ids))}/{len(ids)}", file=sys.stderr)

    total = collection.count()
    print(f"완료: 컬렉션 '{args.collection}'에 {total}개 문서 "
          f"(저장 경로: {args.db})", file=sys.stderr)

    if args.query:
        res = collection.query(query_texts=[args.query], n_results=5)
        print(f"\n검색 테스트: {args.query!r}", file=sys.stderr)
        for rank, (doc_id, meta, dist) in enumerate(
            zip(res["ids"][0], res["metadatas"][0], res["distances"][0]), 1
        ):
            print(f"  {rank}. [{dist:.3f}] {meta['kind']:8} "
                  f"{meta['file']}:{meta['start_line']}  {meta['name']}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
