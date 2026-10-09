# RAG 코드 검색 스크립트 설명서

`scripts/` 아래 4개 파이썬 파일은 **이 저장소의 파이썬 코드를 대상으로 한 검색 도구**입니다.
"카드사별 소계는 어디서 계산하지?" 같은 질문을 자연어나 식별자로 던지면, 관련 있는
함수/클래스 조각을 찾아 줍니다.

이 문서는 처음 보는 사람이 **전체 구조와 각 파일의 역할, 데이터가 어떻게 흐르는지**를
파악할 수 있게 쓴 설명서입니다. 개선점/결함 지적은 [`RAG_REVIEW.md`](../RAG_REVIEW.md)를,
본체(카드 자동화) 설명은 [`SOURCE_REVIEW.md`](../SOURCE_REVIEW.md)를 보세요.

> **또 다른 방식 — Graph RAG.** 같은 코드를 임베딩 대신 **호출/포함 그래프**로 색인하는
> [`graph_build.py`](graph_build.py) · [`graph_search.py`](graph_search.py) ·
> [`graph_query.py`](graph_query.py)도 있습니다(표준 라이브러리만, 모델 다운로드 없음).
> 이 아래에서 설명하는 vector/hybrid 방식과 **무엇이 어떻게 다른지**는
> [`RAG_COMPARISON.md`](../RAG_COMPARISON.md)에 같은 질의를 나란히 놓고 정리했습니다.

---

## 1. 한눈에 보기

```
 소스 코드 (*.py)
      │
      │  ① scripts/chunk.py   —  ast로 함수/클래스 단위로 자름
      ▼
 rag/chunks.jsonl            —  청크 1개 = JSON 1줄  {id, text, metadata}
      │
      │  ② scripts/index.py   —  각 청크를 임베딩(숫자 벡터)해서 저장
      ▼
 rag/chroma/                 —  chromadb 영구 벡터 저장소 (컬렉션 'code_chunks')
      │
      │  ③ 검색
      ├─────────────  scripts/search.py   —  1회성 질의 (CLI, 파이프/JSON)
      └─────────────  scripts/query.py    —  대화형 REPL (질문 반복 입력)
```

- **①번은 표준 라이브러리만** 씁니다(`ast`, `tokenize`). 가상환경 없이도 실행됩니다.
- **②③번은** `chromadb`, `sentence-transformers`, `rank-bm25` 가 필요합니다
  (`rag-requirements.txt`).
- 산출물 `rag/chunks.jsonl` 과 `rag/chroma/` 는 재생성 가능한 파일이라
  `.gitignore` 되어 있습니다.

---

## 2. 왜 이렇게 나눴나 (설계 의도)

| 단계 | 하는 일 | 분리한 이유 |
|---|---|---|
| chunk | 소스를 "검색 단위"로 자르고 위치정보를 붙임 | 파싱은 무의존성으로 빠르게. 청킹 규칙만 바꿔 실험 가능 |
| index | 청크를 의미 벡터로 바꿔 DB에 적재 | 임베딩 모델 교체·재색인을 검색과 분리 |
| search / query | 질문을 받아 순위를 매김 | 검색 로직은 한 곳(`HybridSearcher`)에 두고 CLI와 REPL이 공유 |

검색은 **두 방식을 합칩니다**:

- **BM25 (어휘 검색)** — 질문에 나온 *단어가 그대로* 들어있는 청크를 찾음.
  `ensure_tab`, `SUBTOTAL` 같은 식별자·에러 문자열에 강함.
- **벡터 검색 (의미 검색)** — 단어가 달라도 *뜻이 비슷한* 청크를 찾음.
  "중복을 어떻게 막나" → `_existing_transaction_keys` 처럼 표현이 달라도 잡음.

둘의 순위를 **RRF(Reciprocal Rank Fusion)** 로 합쳐 최종 순위를 냅니다(§6).

---

## 3. 준비 & 빠른 시작

```bash
# (최초 1회) 가상환경 + 의존성
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r rag-requirements.txt

# ① 청킹      : *.py  ->  rag/chunks.jsonl
.venv/Scripts/python.exe scripts/chunk.py

# ② 색인      : rag/chunks.jsonl  ->  rag/chroma/   (첫 실행 시 임베딩 모델 ~470MB 다운로드)
.venv/Scripts/python.exe scripts/index.py --reset

# ③ 검색
.venv/Scripts/python.exe scripts/search.py "카드사별 소계 계산"
.venv/Scripts/python.exe scripts/query.py            # 대화형
```

> Windows 한국어 콘솔(cp949)에서도 한글이 깨지지 않도록 세 스크립트 모두 시작 시
> stdout/stderr(그리고 `query.py`는 stdin까지) 를 UTF-8로 전환합니다.

---

## 4. `chunk.py` — 소스를 청크로 자르기

### 4.1 무엇을 하나

프로젝트의 모든 `*.py` 파일을 `ast`(파이썬 표준 구문 분석기)로 파싱해, **함수·클래스·
메서드 단위**로 잘라 [JSON Lines](https://jsonlines.org/) 파일로 저장합니다.
자를 때 각 조각의 **파일 경로·심볼 이름·라인 번호**를 metadata로 함께 기록합니다.

### 4.2 청크 종류 (`kind`)

| kind | 대상 | `name` 예 |
|---|---|---|
| `module` | 함수/클래스 밖의 최상위 코드(임포트, 상수, `if __name__ == "__main__"`) | `<module>` |
| `function` | 최상위 `def` / `async def` | `append_rows_to_sheet` |
| `class` | 클래스 정의부(첫 메서드 전까지: `class` 줄 + docstring + 클래스 변수) | `HybridSearcher` |
| `method` | 클래스 안의 `def` / `async def` | `HybridSearcher.search` |

- 데코레이터(`@app.route(...)` 등)는 청크에 **포함**됩니다(`start_line`이 데코레이터
  첫 줄을 가리킴).
- `--class-mode full` 을 주면 클래스를 통째로 한 청크로 만들고 메서드를 따로 쪼개지
  않습니다. 기본값은 `methods`(위 표대로 분리).
- `.venv`, `.git`, `__pycache__`, `node_modules`, `build`, `dist` 등은 건너뜁니다.
- 구문 오류가 있는 파일은 경고를 찍고 건너뜁니다.
- `apps_script/Code.gs` 는 파이썬이 아니라 **대상에서 제외**됩니다.

### 4.3 출력 포맷 (`rag/chunks.jsonl`)

한 줄이 청크 하나입니다.

```json
{
  "id": "cloud_run/main.py::month_tab_name::611",
  "text": "def month_tab_name(filename: str) -> str:\n    \"\"\"파일명에서 ...\"\"\"\n    ...",
  "metadata": {
    "file": "cloud_run/main.py",   // 저장소 루트 기준 상대경로 (POSIX 슬래시)
    "name": "month_tab_name",       // 메서드는 "클래스명.메서드명"
    "parent": "",                   // 메서드면 소속 클래스명, 아니면 ""
    "kind": "function",             // module | function | class | method
    "start_line": 611,              // 데코레이터 포함, 1-기준, 양끝 포함
    "end_line": 619,
    "def_line": 611,                // def/class 키워드가 있는 줄
    "n_lines": 9,
    "docstring": "파일명에서 YYYYMMDD..."   // docstring 첫 줄(최대 200자), 없으면 ""
  }
}
```

- **`id` 규칙**: `{상대경로}::{name}::{start_line}` — 저장소 안에서 유일.
- metadata 값은 전부 문자열/정수 — chromadb에 그대로 넣을 수 있는 형태입니다.

### 4.4 CLI

```
python scripts/chunk.py [경로 ...] [옵션]
```

| 옵션 | 기본 | 설명 |
|---|---|---|
| `경로 ...` | 저장소 루트 | 청킹할 파일 또는 디렉터리 |
| `-o, --output` | `rag/chunks.jsonl` | 출력 경로. `-` 이면 표준출력 |
| `--class-mode` | `methods` | `methods`(헤더+메서드) 또는 `full`(통짜) |
| `--min-lines` | `0` | 이 줄 수 미만 청크 제외 |

---

## 5. `index.py` — 청크를 벡터로 색인

### 5.1 핵심 개념 3가지

- **임베딩(embedding)**: 텍스트를 고정 길이 숫자 배열(벡터)로 바꾸는 것. 뜻이 비슷한
  텍스트는 벡터도 가깝습니다. 여기서는 `sentence-transformers`의 다국어 모델
  `paraphrase-multilingual-MiniLM-L12-v2` 를 씁니다(한글 주석 비중이 커서 다국어 모델).
- **벡터 저장소(vector store)**: 벡터들을 넣어 두고 "이 벡터와 가까운 것 N개"를 빠르게
  찾아 주는 DB. 여기서는 **chromadb**(`PersistentClient`, 로컬 디렉터리에 저장).
- **컬렉션(collection)**: chromadb 안의 테이블 같은 단위. 이름은 `code_chunks`.

### 5.2 하는 일

1. `rag/chunks.jsonl` 을 읽는다.
2. chromadb의 `SentenceTransformerEmbeddingFunction` 을 컬렉션에 붙인다
   → 이후 색인·검색 양쪽에서 chroma가 **자동으로 같은 방식**으로 임베딩한다
   (검색 스크립트에서 임베딩 코드를 다시 짤 필요가 없음).
3. 각 청크를 `id` / `document(=text)` / `metadata` 로 `upsert` 한다(128개씩 배치).
4. 컬렉션 metadata에 `embed_model`(모델명)과 `hnsw:space: cosine`(거리 방식)을 남긴다
   → `search.py` 가 이 값을 읽어 같은 모델로 질의를 임베딩한다.

### 5.3 CLI

```
python scripts/index.py [옵션]
```

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--chunks` | `rag/chunks.jsonl` | 입력 JSONL |
| `--db` | `rag/chroma` | chroma 영구 저장 경로 |
| `--collection` | `code_chunks` | 컬렉션 이름 |
| `--model` | `paraphrase-multilingual-MiniLM-L12-v2` | 임베딩 모델 |
| `--reset` | (꺼짐) | 색인 전에 컬렉션을 지우고 새로 만듦 |
| `--query TEXT` | — | 색인 직후 이 문장으로 상위 5개를 뽑아 동작 확인 |

> **중요**: 코드를 수정한 뒤 재색인할 때는 `--reset` 을 붙이세요. `upsert` 는 추가/갱신만
> 하고 사라진 청크의 벡터를 지우지 않아, `--reset` 없이 반복하면 옛 벡터가 쌓입니다
> (자세한 내용은 [`RAG_REVIEW.md`](../RAG_REVIEW.md) H-1).

---

## 6. `search.py` — 하이브리드 검색 (1회성)

### 6.1 흐름

```
질의 문자열
  ├─ BM25   : rag/chunks.jsonl 을 메모리에 올려 rank_bm25로 점수 → 상위 pool개
  └─ 벡터   : chroma 컬렉션에 query_texts=[질의] → 코사인 거리 낮은 순 pool개
        │
        ▼
   RRF 융합 → 상위 N개 출력
```

### 6.2 토크나이저 (BM25용)

코드와 한글이 섞여 있어 전용 토크나이저를 씁니다.

- 식별자를 `snake_case` / `camelCase` 로 쪼개 **부분 토큰**도 추가
  `parseTransactionsWithClaude` → `parse`, `transactions`, `with`, `claude`
- 한글 덩어리는 원형 + **글자 2개씩(bigram)** 을 함께 넣어, 형태소 분석 없이도
  부분 일치를 잡음: `비밀번호` → `비밀번호`, `비밀`, `밀번`, `번호`

### 6.3 RRF (Reciprocal Rank Fusion)

두 검색의 **순위**만 가지고 합칩니다(점수 크기·단위가 달라도 안전).

```
최종점수(문서 d) = Σ  w / (rrf_k + rank_리트리버(d))       (rrf_k = 60 기본)
              리트리버
```

예) `rrf_k=60`, 가중치 1일 때
- BM25 순위:  `[A, B, C]`
- 벡터 순위:  `[B, D, A]`

| 문서 | BM25 기여 | 벡터 기여 | 합계 |
|---|---|---|---|
| A | 1/61 = 0.0164 | 1/63 = 0.0159 | **0.0323** |
| B | 1/62 = 0.0161 | 1/61 = 0.0164 | **0.0325** |
| D | — | 1/62 = 0.0161 | 0.0161 |
| C | 1/63 = 0.0159 | — | 0.0159 |

→ 최종 순위: **B, A, D, C**. 양쪽에서 고루 상위인 문서가 위로 올라갑니다.

### 6.4 출력 읽는 법

```
[2] 0.0305  cloud_run/main.py:639-654  _existing_transaction_keys  (function)
    bm25#1  vec#11  dist=0.863  bm25=18.40
    "탭에 이미 기록된 거래를 (카드명, 일자, 가맹점, 금액) 키 집합으로 반환합니다."
```

| 표기 | 뜻 |
|---|---|
| `[2]` | 최종 순위 |
| `0.0305` | RRF 최종 점수 |
| `cloud_run/main.py:639-654` | 파일 및 라인 범위 (클릭해서 열 수 있음) |
| `_existing_transaction_keys (function)` | 심볼 이름과 kind |
| `bm25#1` | BM25 단독 순위 (`#-` = BM25가 못 올림) |
| `vec#11` | 벡터 단독 순위 |
| `dist=0.863` | 코사인 거리 (0에 가까울수록 유사, 범위 0~2) |
| `bm25=18.40` | BM25 원점수 |
| `"..."` | docstring 첫 줄 |

### 6.5 CLI

```
python scripts/search.py "<질의>" [옵션]
```

| 옵션 | 기본 | 설명 |
|---|---|---|
| `-n, --n-results` | `8` | 출력 개수 |
| `--pool` | `40` | 각 리트리버가 융합에 넘길 후보 수 |
| `--rrf-k` | `60` | RRF 상수 (작을수록 상위권 가중↑) |
| `--w-bm25` / `--w-vec` | `1.0` | 리트리버별 가중치 |
| `--bm25-only` / `--vector-only` | — | 한쪽만 사용(비교·디버깅) |
| `--kind K [K ...]` | — | `module`/`function`/`class`/`method` 만 검색 |
| `--file SUBSTR` | — | 파일 경로에 이 문자열이 든 청크만 |
| `--full` | — | 청크 본문 전체 출력 |
| `--json` | — | 결과를 JSON으로 |
| `--chunks` / `--db` / `--collection` | 기본 경로 | 입력·저장소 지정 |

### 6.6 `HybridSearcher` 클래스

검색 로직의 본체입니다. `search.py` 와 `query.py` 가 **둘 다 이걸 import 해서 씁니다**
(로직 중복 없음).

```python
from search import HybridSearcher

s = HybridSearcher()                 # chunks.jsonl + chroma 컬렉션을 1회 로드
hits = s.search("월별 시트 탭 생성", k=5, kinds={"function"}, file_sub="main.py")
for h in hits:
    print(h["metadata"]["file"], h["metadata"]["start_line"], h["metadata"]["name"])
    print(h["text"])
```

`search()` 가 돌려주는 dict 항목: `rank, score, id, bm25_rank, bm25_score,
vector_rank, distance, retrievers, metadata, text`.

---

## 7. `query.py` — 대화형 검색 (REPL)

`search.py` 와 같은 엔진을 쓰되, **인덱스와 임베딩 모델을 시작할 때 한 번만 로드**하고
질문을 반복해서 받습니다. 결과는 기본 top-5이며 **코드 본문까지** 보여 줍니다
(40줄 초과 시 잘라서 `… (+N줄)`).

```
$ python scripts/query.py
컬렉션 'code_chunks'  문서 65개  모델 ...MiniLM-L12-v2
질문> BC바로카드 PDF 비밀번호는 어떻게 푸나요?

[1] 0.0325  cloud_run/main.py:149-185  _decrypt_pdf_to_page_images  (function)
    bm25#2  vec#1  dist=0.468  bm25=14.39
    "비밀번호로 PDF를 복호화한 뒤 각 페이지를 PNG 이미지 바이트로..."
    --------------------------------------------------------------------
    | def _decrypt_pdf_to_page_images(pdf_bytes: bytes, password: str) -> list[bytes]:
    |     ...
```

### 7.1 REPL 명령

| 입력 | 동작 |
|---|---|
| `:n <숫자>` | 결과 개수 변경 |
| `:full` | 본문 전체 표시 토글 |
| `:kind <k...>` | kind 필터 (`:kind function method`), 인자 없으면 해제 |
| `:file <문자열>` | 파일경로 부분일치 필터, 인자 없으면 해제 |
| `:bm25` / `:vec` / `:both` | 리트리버 선택 |
| `:help` | 도움말 |
| `:q` / `quit` / `exit` / Ctrl-D | 종료 |
| 그 외 | 검색 질의로 처리 |

### 7.2 원샷 모드

질문을 인자로 주면 REPL 없이 1건만 처리하고 끝냅니다.

```bash
python scripts/query.py -n 3 "카드사별 소계 계산 로직"
python scripts/query.py --full "원형차트 갱신"
```

---

## 8. 전형적인 사용 흐름

**코드를 고친 뒤 검색 결과에 반영하고 싶을 때**

```bash
.venv/Scripts/python.exe scripts/chunk.py          # 청크 다시 생성
.venv/Scripts/python.exe scripts/index.py --reset  # 인덱스 새로 만들기 (--reset 중요)
```

**특정 계층만 뒤질 때**

```bash
# main.py의 함수 중에서만
scripts/search.py "중복 거래 스킵" --kind function --file main.py

# 테스트 스크립트만
scripts/search.py "gmail 첨부파일" --file test_
```

**정확한 식별자/문자열을 찾을 때** → `--bm25-only`
**개념/의도로 찾을 때** → `--vector-only` 로 비교해 보면 감이 옵니다.

---

## 9. 커스터마이즈 포인트

| 하고 싶은 것 | 방법 |
|---|---|
| 다른 임베딩 모델 사용 | `index.py --model <이름> --reset` (검색 특화 모델 예: `intfloat/multilingual-e5-base`). `search.py`는 컬렉션 metadata에서 모델명을 읽으므로 자동으로 따라감 |
| 다른 폴더/저장소 색인 | `chunk.py <경로> -o other.jsonl` → `index.py --chunks other.jsonl --db other/chroma --collection foo` |
| BM25 vs 벡터 비중 조절 | `search.py --w-bm25 0.5 --w-vec 2` |
| 클래스를 통짜로 색인 | `chunk.py --class-mode full` |

---

## 10. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `청크 파일이 없습니다` | `scripts/chunk.py` 를 먼저 실행 |
| `chroma 저장소가 없습니다` | `scripts/index.py` 를 먼저 실행 |
| `주의: 인덱스(N)와 청크 파일(M)의 개수가 다릅니다` | 코드가 바뀌었는데 재색인을 안 함 → `index.py --reset` |
| 첫 `index.py` 가 오래 걸림 | 임베딩 모델(~470MB) 다운로드 중. 이후엔 캐시(`~/.cache/huggingface`) 사용 |
| 콘솔에 한글이 깨짐 | 세 스크립트가 UTF-8로 전환하므로 보통 정상. 여전하면 `PYTHONUTF8=1` 환경변수 |
| 검색 결과가 낡은 코드 | `--reset` 없이 재색인해 stale 벡터가 남은 것 → `index.py --reset` |

---

## 11. 용어집

| 용어 | 뜻 |
|---|---|
| **청크(chunk)** | 검색의 최소 단위. 여기서는 함수/클래스/메서드/모듈 조각 하나 |
| **임베딩(embedding)** | 텍스트를 의미를 담은 숫자 벡터로 변환한 것 |
| **벡터 저장소** | 벡터를 넣고 "가까운 것 N개"를 빠르게 찾는 DB (chromadb) |
| **BM25** | 단어 빈도 기반 고전 어휘 검색 랭킹. 같은 단어가 들어있으면 점수↑ |
| **코사인 거리** | 두 벡터의 방향 차이. 0이면 같은 방향(유사), 클수록 다름 |
| **RRF** | 여러 검색의 *순위*를 `1/(k+순위)` 로 더해 합치는 융합 기법 |
| **리트리버(retriever)** | 후보를 뽑아 오는 한 가지 검색 방식(BM25 또는 벡터) |
| **JSONL** | 한 줄에 JSON 객체 하나씩 있는 파일 형식 |
