# 코드 리뷰 — RAG 코드 검색 스크립트 (`scripts/`)

RAG 코드 검색용으로 새로 추가한 파이썬 4개 파일에 대한 리뷰입니다.
동작 설명이 아니라 **결함·엣지케이스·개선점**에 초점을 둡니다.

| 항목 | 내용 |
|---|---|
| 대상 | [`scripts/chunk.py`](scripts/chunk.py) · [`scripts/index.py`](scripts/index.py) · [`scripts/search.py`](scripts/search.py) · [`scripts/query.py`](scripts/query.py) |
| 파이프라인 | `chunk.py`(ast 청킹 → `rag/chunks.jsonl`) → `index.py`(임베딩 → `rag/chroma/`) → `search.py` / `query.py`(BM25+벡터 RRF 검색) |
| 의존성 | `chunk.py`는 표준 라이브러리만. 나머지는 `chromadb`, `sentence-transformers`, `rank-bm25` ([`rag-requirements.txt`](rag-requirements.txt)) |
| 작성 시점 규모 | 8파일 → 65청크 (module=8, function=53, class=1, method=3) |

---

## 요약

전반적으로 구조는 단정합니다. `HybridSearcher` 하나로 검색 로직을 모으고 `query.py`가
이를 재사용하는 점, RRF로 점수 정규화를 피한 점, `chunk.py`를 무의존성으로 유지한 점이 좋습니다.

다만 **재색인 워크플로에 조용한 오염 경로가 두 개** 있고(아래 H-1, H-2), 필터·인코딩
관련 중간 결함이 몇 개 있습니다. 실사용 전에 H-1, H-2, M-1은 손보는 것을 권합니다.

| # | 심각도 | 파일 | 한 줄 요약 |
|---|---|---|---|
| H-1 | 상 | index.py | `--reset` 없이 재색인하면 **stale 벡터가 누적**된다 (upsert는 삭제를 안 함) |
| H-2 | 상 | index.py | `--model` 을 바꿔 재색인해도 기존 컬렉션 metadata를 chroma가 무시 → **임베딩 공간 혼재** |
| M-1 | 중 | search.py | `--file` 필터가 벡터 쪽에 push되지 않아 하이브리드가 조용히 BM25-only로 퇴화할 수 있음 |
| M-2 | 중 | chunk.py | `tokenize.open()` 실패(코딩 선언 오류 등)는 미포착 → run 전체 크래시 |
| M-3 | 중 | chunk.py | `<module>` 청크의 `start_line~end_line`이 **비연속 구간**을 span (예: `main.py:1-1071`) |
| M-4 | 중 | query.py | `:kind` 에 오타를 주면 **경고 없이 필터가 해제**됨 |
| M-5 | 중 | (전체) | `apps_script/Code.gs`가 인덱스에서 통째로 빠짐 — 검색이 Python만 커버 |
| L-1 | 하 | index/search | 기본 임베딩 모델이 검색(비대칭)용이 아니라 STS/paraphrase용 |
| L-2 | 하 | index/search | 기본 모델명이 두 파일에 중복 정의 (`DEFAULT_MODEL` vs `FALLBACK_MODEL`) |
| L-3 | 하 | index.py | 임베딩 문서에 파일/심볼 헤더가 없어 벡터 recall 손해 |
| L-4 | 하 | search.py | 단일 리트리버 모드에서 표시 점수가 RRF 값(`≈0.016`)뿐이라 비직관적 |
| L-5 | 하 | index.py | `load_chunks`의 `obj["id"], obj["text"], obj["metadata"]` 줄은 의도가 드러나지 않음 |
| L-6 | 하 | (전체) | UTF-8 `reconfigure` 블록이 3파일에 복붙 |
| N-1 | nit | chunk.py | `SKIP_DIRS` 를 `f.parts` 전체에서 검사 → 상위 경로에 `build/` 등이 있으면 오탐 |
| N-2 | nit | query.py | Ctrl-C가 현재 입력 취소가 아니라 즉시 종료 |
| N-3 | nit | query.py | 명령 목록이 docstring과 `HELP` 두 곳에 중복 |

---

## 1. 파일별 리뷰

### 1.1 `chunk.py`

**좋은 점**
- 표준 라이브러리(`ast`, `tokenize`)만 사용 — venv 없이도 돌아가고 CI 부담이 없음.
- `start_line()`이 데코레이터를 포함하도록 `decorator_list[*].lineno`의 최소값을 씀 →
  `@app.route(...)` 가 청크에 들어옴. (자주 빠뜨리는 부분)
- `tokenize.open()`으로 PEP 263 코딩 선언을 존중.
- `class-mode methods`에서 "클래스 헤더(첫 메서드 전까지) + 메서드별 청크"로 나눈 설계가
  큰 클래스에 적절. 실제 `HybridSearcher`(1 class + 3 method)로 경로가 검증됨.

**M-2 — `tokenize.open()` 예외 미포착 (중)**
`chunk_file()`은 `ast.parse`의 `SyntaxError`만 잡습니다. 하지만 `read_source()`의
`tokenize.open()`도 잘못된/모순된 코딩 선언에서 `SyntaxError`를, 드물게 다른 예외를
던질 수 있고, 이 경우 파일 한 개 때문에 전체 색인 작업이 중단됩니다.
→ `read_source()` 호출도 `try`로 감싸 "건너뜀" 처리에 포함.

**M-3 — `<module>` 청크의 라인 범위가 비연속 (중)**
`module_chunk()`는 최상위 non-def/class 문장들의 *조각*을 `\n`으로 이어 붙이고,
metadata의 `start_line`/`end_line`은 그 조각들의 min/max로 잡습니다. 결과적으로
`cloud_run/main.py`의 module 청크는 `1-1071`로 찍히지만 실제 담긴 코드는 109줄이며,
그 사이 900여 줄(모든 함수 본문)은 빠져 있습니다.
- 인용/점프 대상으로서 `main.py:1-1071`은 오해를 부릅니다.
- `.rstrip("\n")` + `\n` join 이라 조각 사이 빈 줄도 사라져 임포트·상수·`__main__`
  블록이 한 덩어리로 뭉칩니다.
→ 최소한 metadata에 `contiguous: false` 또는 `segments: [[lo,hi],...]`를 남기거나,
  module 청크를 "파일 첫 def/class 전까지"로 한정하는 편이 정직합니다.

**설계 노트 (결함 아님)**
- 중첩 함수/클래스는 부모 청크에 포함될 뿐 별도 청크가 안 됩니다. 이 저장소 규모엔 무해.
- `rel_path()`가 `REPO_ROOT`(= `__file__`의 조부모)에 의존 → 스크립트를 옮기면 깨짐.

**N-1 — `SKIP_DIRS` 검사 범위 (nit)**
`any(part in SKIP_DIRS for part in f.parts)` 는 드라이브·홈 등 상위 경로 세그먼트까지
포함합니다. 저장소가 우연히 `.../dist/...` 아래 체크아웃돼 있으면 전부 스킵됩니다.
→ `f.relative_to(root).parts` 기준으로 검사.

---

### 1.2 `index.py`

**좋은 점**
- chroma의 `SentenceTransformerEmbeddingFunction`을 컬렉션에 붙여 색인·검색 양쪽이
  같은 임베딩을 자동 사용 → 검색 스크립트에서 임베딩 코드를 중복 구현할 필요가 없음.
- `hnsw:space=cosine` 명시, 컬렉션 metadata에 `embed_model` 기록 → `search.py`가 되읽음.
- `--reset` / `--query` 스모크 테스트 옵션.

**H-1 — `--reset` 없는 재색인은 stale 벡터를 남긴다 (상)**
`get_or_create_collection(...)` 뒤 `upsert`만 합니다. `upsert`는 **추가/갱신만** 하고
사라진 id를 지우지 않습니다. 그런데 청크 id는 `경로::이름::시작줄`이라:
- 함수 하나를 지우거나 이름을 바꾸면 → 그 벡터가 인덱스에 **계속 남음**.
- 어떤 함수 본문이 늘어 아래 함수가 밀리면 → 아래 함수의 `시작줄`이 바뀌어 **새 id로
  삽입**되고, 옛 id(옛 본문)의 벡터가 그대로 남음. 개수만 보면 안 늘어난 것처럼 보임.

즉 표준 워크플로(`chunk.py` → `index.py`)를 반복할수록 조용히 오염됩니다.
`index.py`의 개수 불일치 경고는 이 케이스(같은 개수, 다른 내용)를 못 잡습니다.
→ 택1: (a) 기본 동작을 전체 재생성으로, `--incremental`을 옵트인으로. (b) 색인 전에
  `collection.get()`으로 기존 id를 받아 `현재 id - 신규 id`를 `collection.delete()`.
  (c) id를 `경로::이름::본문해시`로 바꿔 라인 이동에 불변이 되게.

**H-2 — `--model` 변경이 기존 컬렉션과 조용히 어긋난다 (상)**
컬렉션이 이미 있으면 `get_or_create_collection`에 넘긴 `metadata`(새 `embed_model`,
`hnsw:space`)는 **무시되고 기존 값이 유지**됩니다(chroma 사양). `embedding_function`은
chroma 버전에 따라 교체되거나 충돌 에러가 나는데, 전자라면 `--model other` +
`--reset` 없이 재색인 시:
- 새 문서는 `--model`로 임베딩되지만 컬렉션 기록 모델명은 옛것.
- 옛 문서 벡터(옛 모델)와 혼재 → 거리 비교가 무의미해짐. 경고 없음.
→ 색인 시작 시 `probe.metadata["embed_model"]`과 `args.model`을 비교해 다르면
  `--reset` 없이는 중단(`sys.exit`)하도록. (버전 무관하게 안전)

**L-3 — 임베딩 문서에 심볼/파일 힌트가 없음 (하)**
BM25 코퍼스는 `text + name + file`을 토큰화하지만, 벡터 임베딩에 들어가는 document는
`chunk["text"]`(원본 소스)뿐입니다. `decrypt_bc_pdf`처럼 본문이 2줄이고 docstring도
없는 청크는 벡터 표현이 빈약합니다.
→ 임베딩용 document 앞에 `f"# {file} :: {name}\n{docstring}\n\n"` 헤더를 덧붙여
  색인하면(원본 `text`는 metadata로 별도 보관) 짧은 청크의 recall이 올라갑니다.

**L-5 — `load_chunks`의 검증 트릭 (하)**
```python
obj = json.loads(line)
obj["id"], obj["text"], obj["metadata"]   # KeyError 유발용, 값은 안 씀
```
표현식 문 한 줄이 "필수 키 존재 검증"이라는 게 안 보입니다.
→ `missing = {"id","text","metadata"} - obj.keys()` 후 명시적으로 에러.

**기타**
- 첫 실행 시 HF 허브에서 모델(~470MB)을 받습니다. 오프라인/CI 환경 안내나
  `HF_HUB_OFFLINE` 처리가 없음. (배포 관심사)
- 재현성: 컬렉션 metadata에 모델 *이름*만 있고 리비전 해시가 없음.

---

### 1.3 `search.py`

**좋은 점**
- `HybridSearcher`가 BM25 코퍼스와 chroma 컬렉션을 1회 로드해 재사용 → `query.py`가
  그대로 씀. 로직 단일화가 잘 됨.
- 무거운 import(`chromadb`, `rank_bm25`)를 `__init__` 안으로 지연 → `--help`가 빠르고
  `tokenize`/`load_chunks`만 필요한 쪽에서 가볍게 import 가능.
- 토크나이저가 snake/camelCase 분해 + 한글 bigram을 함께 넣어 형태소 분석 없이도
  부분 일치를 잡음.
- RRF는 이종 점수(코사인 거리 vs BM25)를 정규화 없이 합치는 견고한 선택.
- 인덱스/청크 개수 불일치 경고.

**M-1 — `--file` 필터가 벡터 검색에 반영되지 않음 (중)**
`kind` 필터는 chroma `where`로 push되지만 `file_sub`는 substring이라 push가 안 되고
결과에서 후처리로 걸러냅니다. `collection.query(n_results=pool)`가 돌려준 `pool`개가
전부 `--file` 조건에 안 맞으면 벡터 후보가 크게 줄거나 0이 되어, 하이브리드가 사실상
BM25-only로 퇴화합니다(사용자는 모름).
→ `file_sub`가 있으면 벡터 `n_results`를 크게(예: `min(count, pool*8)` 또는 전체) 잡고
  후처리 필터 후 앞의 `pool`개만 사용.

**L-1 — 모델 선택 (하)**
`paraphrase-multilingual-MiniLM-L12-v2`는 문장 유사도(STS/paraphrase) 목적 모델입니다.
"짧은 질의 → 코드 청크"는 비대칭 검색이라 `intfloat/multilingual-e5-*`,
`BAAI/bge-m3`, `Alibaba-NLP/gte-multilingual-base` 같은 검색 특화 모델이 보통 더
좋습니다(e5 계열은 `query:` / `passage:` 프리픽스 규약이 붙는 대신). 현재도 데모
품질은 괜찮지만 기본값으로는 아쉬움.

**L-2 — 모델명 중복 정의 (하)**
`index.py:DEFAULT_MODEL` 와 `search.py:FALLBACK_MODEL`이 같은 문자열을 각자 들고
있습니다. 실사용 경로에선 컬렉션 metadata의 `embed_model`을 읽으므로 문제는 안
되지만, 한쪽만 바꾸면 어긋납니다.
→ 공용 상수 모듈(`scripts/_rag_common.py`) 하나로.

**L-4 — 단일 리트리버 모드의 표시 점수 (하)**
`--bm25-only`일 때 출력의 첫 숫자는 `w/(rrf_k+rank)` 값이라 `0.0164, 0.0161…`처럼
거의 붙어 있고 의미가 약합니다.
→ 리트리버가 하나면 그쪽 native 점수(BM25 점수 / `1-거리`)를 주 지표로 출력.

**엣지**
- `n_results=min(pool, self.count)` 에서 `self.count == 0`이면 `query(n_results=0)`.
  (인덱스가 존재하면 0은 거의 없지만 방어 없음)
- BM25 코퍼스에 `file` 경로 토큰이 매 문서에 들어가 `main`, `py`, `cloud`, `run`의
  IDF가 낮아지고 노이즈가 됩니다. 파일명은 별도 필드 매칭으로 두는 편이 깔끔.

---

### 1.4 `query.py`

**좋은 점**
- 인덱스·모델을 시작 시 1회 로드하고 질문마다 재사용 — 대화형의 핵심을 지킴.
- `search.py`의 `HybridSearcher` / `render_result`를 재사용(로직 0 중복).
- `sys.stdin`까지 UTF-8 `reconfigure` — 파이프로 한글 질문 시 cp949 오디코딩으로 생긴
  서러게이트 문자가 Rust 토크나이저를 깨뜨리던 문제를 실제로 잡음.
- 원샷 모드(`query.py "질문"`)와 REPL을 한 파일에서 지원.

**M-4 — `:kind` 오타가 조용히 필터 해제 (중)**
```python
picked = [k for k in arg.split() if k in KINDS]
kinds = set(picked) or None
```
`:kind func`(오타)를 주면 `picked == []` → `kinds = None` → "필터 = 해제"로 출력됩니다.
사용자는 필터를 *걸었다고* 생각하는데 정반대로 동작.
→ 인식 못한 토큰이 있으면 경고하고 필터를 바꾸지 않기.

**N-2 — Ctrl-C 동작 (nit)**
`except (EOFError, KeyboardInterrupt): return 0` 이라 Ctrl-C가 현재 줄 취소가 아니라
즉시 종료입니다. 통상 REPL은 Ctrl-C=줄 취소, Ctrl-D=종료.
→ `KeyboardInterrupt`는 `continue`, `EOFError`만 `return 0`.

**N-3 — 명령 목록 중복 (nit)**
모듈 docstring과 `HELP` 문자열에 명령 설명이 두 벌 있습니다. `HELP` 하나만 두고
docstring에선 "`:help` 참고"로.

**비대칭 (설계 판단)**
원샷 모드는 `-n`, `--full`만 받고 `--kind`/`--file`/리트리버 선택이 없습니다.
필터가 필요한 일회성 질의는 `search.py`를 쓰라는 의도지만, 사용자가 헷갈릴 수 있음.

**enhancement**
- REPL에 `:reload`(청크/인덱스 다시 읽기)가 있으면 `chunk.py`/`index.py` 재실행 후
  프로세스를 안 죽여도 됨.

---

## 2. 교차 관심사

- **테스트 없음.** `cloud_run/`엔 `test_*.py`가 있지만 RAG 스크립트엔 스모크 테스트도
  없습니다. 최소한 `chunk.py`의 `tokenize`/`start_line`(데코레이터 포함), `search.py`의
  `rrf_fuse`는 순수 함수라 단위 테스트가 쉽습니다.
- **직접 의존성이 명문화 안 됨.** `rag-requirements.txt`는 `pip freeze` 전체 덤프라
  "이 도구가 직접 쓰는 것"이 안 보입니다. `rag-requirements.in`(3줄: chromadb,
  sentence-transformers, rank-bm25)을 두고 그걸로 컴파일하는 편이 유지보수에 유리.
- **`apps_script/Code.gs` 미포함(M-5).** Gmail 감지·트리거 로직 전체가 검색 대상에서
  빠집니다. `.gs`는 JS라 `ast`로는 못 파싱하지만, 파일 통짜 또는 정규식 기반 함수
  분할로라도 인덱스에 넣을 가치가 있습니다.
- **증분 색인 없음.** `index.py`는 항상 전량 upsert. 65청크에선 무해하나 커지면
  변경 파일만 재색인하는 경로가 필요.
- **id 안정성.** 위 H-1과 연결 — id가 라인 번호에 묶여 있어 편집에 취약. 콘텐츠 해시
  기반 id로 가면 stale 문제 상당 부분이 사라짐.

---

## 3. 권장 조치 (우선순위)

1. **H-1**: `index.py` 기본을 전량 재생성으로 바꾸거나, 색인 후 `현재 id ∉ 신규 id`를
   `collection.delete()`. (조용한 오염 제거)
2. **H-2**: `index.py`에서 `args.model` vs 컬렉션 `embed_model` 불일치 시 `--reset`
   없이는 `sys.exit`.
3. **M-1**: `search.py`에서 `file_sub` 지정 시 벡터 `n_results`를 크게 잡고 후처리.
4. **M-2**: `chunk.py`에서 `read_source()` 예외도 "파일 건너뜀"에 포함.
5. **M-3 / M-4**: module 청크 라인 범위 표기 정직화, `:kind` 오타 경고.
6. **정리류**: 공용 상수 모듈(L-2), 임베딩 헤더(L-3), `rag-requirements.in`,
   순수 함수 단위 테스트.

---

## 4. 잘한 점 (유지)

- `chunk.py` 무의존성 + 데코레이터 인지 `start_line` + `tokenize.open` 코딩 존중.
- `HybridSearcher` 단일화, `query.py`의 완전 재사용.
- RRF 채택 — 점수 정규화 회피.
- 지연 import로 `--help` 응답성 확보.
- cp949 콘솔 대응(3파일 stdout/stderr, `query.py`는 stdin까지).
- 산출물(`rag/chunks.jsonl`, `rag/chroma/`) `.gitignore` 처리.
