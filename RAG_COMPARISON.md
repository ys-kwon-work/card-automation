# Vector RAG vs Graph RAG — 코드 검색 비교/학습 노트

이 저장소의 파이썬 코드를 대상으로 **두 가지 검색 방식**을 나란히 구현해 두고,
같은 질문을 던져 결과가 어떻게 달라지는지 관찰하기 위한 문서입니다.

| 방식 | 스크립트 | 인덱스 | 한 줄 요약 |
|---|---|---|---|
| **Vector / Hybrid RAG** | [`scripts/chunk.py`](scripts/chunk.py) · [`index.py`](scripts/index.py) · [`search.py`](scripts/search.py) · [`query.py`](scripts/query.py) | `rag/chunks.jsonl` + `rag/chroma/` | 청크를 임베딩해 **의미 거리**로 top-k. BM25와 RRF로 융합 |
| **Graph RAG** | [`scripts/graph_build.py`](scripts/graph_build.py) · [`graph_search.py`](scripts/graph_search.py) · [`graph_query.py`](scripts/graph_query.py) | `rag/graph.json` | 어휘로 **씨앗**만 찾고 **호출/포함 엣지**를 타고 이웃으로 확장 |

Vector 쪽 상세는 [`scripts/README.md`](scripts/README.md), 그쪽 결함/개선점은
[`RAG_REVIEW.md`](RAG_REVIEW.md)를 보세요. 이 문서는 **두 방식의 차이**에만 집중합니다.

---

## 1. 근본 차이 한 장

```
Vector RAG                              Graph RAG
──────────                             ─────────
소스 → 청크 → 임베딩(384차원 벡터)       소스 → ast → 심볼 노드 + 관계 엣지
        ↓                                      ↓
   chroma 벡터 저장소                     graph.json (노드 + CALLS/CONTAINS/IMPORTS)
        ↓                                      ↓
질의 임베딩 → 코사인 최근접 k개           질의 → BM25로 "씨앗" 노드 → 엣지 BFS 확장
        ↓                                      ↓
"뜻이 가까운 조각"                        "씨앗 + 그 씨앗이 부르거나/부르는 코드"
```

- Vector는 **문서 사이의 유일한 관계가 "임베딩 거리"** 다. 두 함수가 서로 호출하든
  전혀 무관하든, 오직 텍스트 의미가 비슷하면 가깝다고 본다.
- Graph는 **관계가 코드 구조에서 온다.** `A`가 `B`를 호출하면 엣지가 있고, 검색은
  씨앗에서 그 엣지를 타고 퍼진다. "이 함수를 누가 부르나", "이 흐름에 뭐가 엮이나"가
  1급 질의가 된다.

---

## 2. 인덱스 산출물 비교 (이 저장소, 2026-09 기준)

| | Vector / Hybrid | Graph |
|---|---|---|
| 빌드 명령 | `chunk.py` → `index.py --reset` | `graph_build.py` |
| 산출물 크기 | `chunks.jsonl` 120 KB + `chroma/` **2.4 MB** | `graph.json` **~400 KB** (본문 포함) |
| 최초 빌드 비용 | 임베딩 모델 **~470 MB 다운로드** + 65청크 인코딩(GPU 없으면 수십 초) | **~4초, 다운로드 0** |
| 런타임 의존성 | `chromadb`, `sentence-transformers`(torch 포함), `rank-bm25` | **없음** (표준 라이브러리만) |
| 대상 파일 | `cloud_run/*.py` + `scripts/*.py` (8파일·65청크) | 위 + `apps_script/Code.gs`(JS 얕은 파싱) — 노드 ~110, CALLS ~110 |
| 재색인 함정 | `--reset` 안 하면 stale 벡터 누적([RAG_REVIEW](RAG_REVIEW.md) H-1) | 매번 전량 재생성이라 stale 없음. 노드 id가 **라인 비의존**(`file::qualname`) |

> torch까지 들어가는 vector 스택과 달리 graph 쪽은 `python scripts/graph_build.py` 하나로
> 끝난다. "무의존성"은 [`chunk.py`](scripts/chunk.py)의 설계 원칙을 그래프까지 밀고 간 것.

---

## 3. 같은 질문, 다른 결과

### 3-1. "카드사별 소계 계산"  — **Graph 압승**

**Vector (`search.py`, bm25+vector RRF):**
```
[1] apply_card_totals            bm25#1 vec#2   ← 정답 함수
[2] _build_parse_instruction     bm25#4 vec#3   ← "계산"/"카드" 어휘로 오탐
[3] _validate_transactions       bm25#14 vec#1  ← 벡터가 끌어온 무관한 함수
[4] month_tab_name               bm25#7 vec#8
[5] _upsert_category_pie_chart   bm25#3 vec#17
[6] scripts/search.py <module>   ← 자기 자신(모듈 청크) 오탐
```

**Graph (`graph_search.py`, 씨앗+depth2):**
```
[1] apply_card_totals                    씨앗
[2] _to_amount                 → 호출 하류 1홉   경로: apply_card_totals ─▶ _to_amount
[3] _normalize_date_cell_value → 호출 하류 1홉   경로: apply_card_totals ─▶ _normalize_date_cell_value
[4] _upsert_category_pie_chart → 호출 하류 1홉   경로: apply_card_totals ─▶ _upsert_category_pie_chart
[5] _build_parse_instruction              씨앗(오탐, PageRank 스파이크)
[6] append_rows_to_sheet       ← 호출 상류 1홉   경로: apply_card_totals ─▶ append_rows_to_sheet
```

`_to_amount`(금액 문자열 → int)와 `_normalize_date_cell_value`(일자 정규화)는 소계 계산의
**핵심 헬퍼**인데 질의와 공유하는 단어가 거의 없다. Vector는 둘 다 top-6에 못 넣는다.
Graph는 `apply_card_totals`를 씨앗으로 잡은 뒤 `CALLS` 엣지로 정확히 끌어오고, **왜 떴는지
경로까지** 보여준다. `append_rows_to_sheet`(이 함수를 부르는 쪽)도 상류로 딸려온다.

### 3-2. "분류별 지출 원형차트"

Graph는 `_upsert_category_pie_chart`를 씨앗으로 잡고 → `apply_card_totals`(상류) →
`_to_amount`/`_normalize_date_cell_value`(2홉 하류)까지 **차트 갱신의 실제 호출 사슬**을
복원한다. Vector는 `_upsert_category_pie_chart` 하나만 정확히 집고 나머지는 "차트/분류"
어휘가 있는 조각들로 채운다.

### 3-3. "중복을 어떻게 막나"  — **어휘가 안 겹치면 둘 다 약하다 (양상은 다름)**

- Vector `--vector-only`: `_meta`, `slice_lines`, `index.py <module>` … `chunk.py` 노이즈로
  도배되고 정답 `_existing_transaction_keys`는 top-5 밖. (의미 임베딩이 "중복 방지"를
  코드 헬퍼와 연결하지 못함)
- Graph: 질의를 **"중복 거래 건너뛰기"** 로 바꾸면(코드 주석에 있는 표현) `_existing_transaction_keys`가
  즉시 #1. 씨앗 어휘가 잡히면 그 다음은 강하지만, **씨앗이 0이면 확장도 0**이다.

### 3-4. "BC바로카드 PDF 처리 흐름"  — **Graph가 이기지 못하는 예 (정직하게)**

씨앗이 `month_tab_name`, `decrypt_shinhan_pdf`, `decrypt_bc_excel`, (JS)`runCheck_` 등으로
**흩어지고**, 진짜 진입점 `process()`와 `decrypt_bc_pdf()`는 docstring이 비어 어휘 점수가
낮아 상위 씨앗에 못 든다. Vector도 `_decrypt_pdf_to_page_images`는 잘 잡지만 `process`는
놓친다. → **"흐름" 같은 추상 질의는 두 방식 모두 약하다.** Graph에서는 식별자를 하나라도
끼워 주면(`process BC pdf`) 씨앗이 고정되고 확장이 흐름을 복원한다.

---

## 4. 어느 쪽이 어떤 질문에 강한가

| 질문 유형 | 예 | 유리한 쪽 |
|---|---|---|
| 정확한 식별자·에러 문자열 | `_existing_transaction_keys`, `SUBTOTAL_SUFFIX` | 둘 다(BM25). Graph는 이후 이웃까지 |
| **"누가 이 함수를 부르나 / 이게 뭘 부르나"** | `parse_transactions` 호출자 | **Graph** (`CALLED_BY`/`CALLS` 확장) |
| **"이 기능에 엮인 코드 전부"** | 소계 계산 사슬, 차트 갱신 경로 | **Graph** (씨앗 → BFS) |
| 동의어·의도(단어가 완전히 다름) | "중복을 어떻게 막나" → `_existing_transaction_keys` | **Vector** (임베딩) — 단 이 저장소에선 그마저 불안정 |
| 추상적 흐름("전체 처리 흐름") | "BC 카드 처리 흐름" | 둘 다 약함. 식별자 힌트 필요 |
| 자연어 개념 설명 요청 | "왜 이미지로 렌더링하나" | **Vector**(docstring 의미) |
| 크로스 언어(JS 트리거) | Apps Script `runCheck_` | **Graph**(vector 인덱스는 `.gs` 제외) |

---

## 5. Graph RAG 구현 노트

### 5-1. 빌더 (`graph_build.py`)

- `ast`로 파일을 파싱해 **노드**(module/function/class/method — vector 청크와 같은 입자)와
  **엣지**를 뽑는다:
  - `CONTAINS` 파일→심볼, 클래스→메서드
  - `CALLS` 저장소 안에서 **이름으로 해석된** 호출. `self.method()`는 같은 클래스 메서드로,
    bare `foo()`는 top-level 심볼 테이블로 해석. 못 풀면 `CALLS_EXT`(외부, leaf).
  - `IMPORTS` 파일→임포트 대상, `INHERITS` 클래스→기반 클래스
- 노드 id = `경로::qualname` (라인 번호 안 씀) → 코드가 밀려도 id 불변. vector 인덱스의
  `경로::name::시작줄` 방식이 겪는 stale 문제([RAG_REVIEW](RAG_REVIEW.md) H-1)가 구조적으로 없음.
- `<module>` 노드 본문은 **첫 def/class 전까지**만(임포트·상수·앱 초기화). vector의 module
  청크가 `main.py:1-1071`처럼 비연속 구간을 span하던 문제([RAG_REVIEW](RAG_REVIEW.md) M-3) 회피.
- `apps_script/Code.gs`는 정규식으로 함수 시그니처만 얕게 훑어 노드로 넣는다(호출은 같은
  파일 내 이름 매칭 근사). 완벽하진 않지만 Gmail 트리거 레이어가 검색에 **들어오긴 한다**.

### 5-2. 검색기 (`graph_search.py`)

```
1. seed    각 노드 document(name+signature+docstring+본문+경로)에 대해
           BM25(순수 파이썬 구현, rank-bm25 불필요) → 상위 --seeds 개.
           module 노드는 KIND_SEED_WEIGHT로 ×0.3 (docstring이 길어 광범위 오탐)
2. expand  씨앗마다 BFS --depth 홉. 점수 = seed_score × (--decay)^홉 × EDGE_WEIGHT[엣지]
             CALLS 1.0 / CALLED_BY 0.9 / CONTAINS 0.6 / SIBLING(같은 클래스) 0.45 …
3. score   node_score = max(경로별 점수) + --centrality × PageRank(호출그래프)
4. render  search.py와 같은 줄 포맷 + "씨앗 / →호출 하류 / ←호출 상류 / N홉 / 경로"
```

- `--seeds-only` = 확장 끄고 순수 BM25 (= 이 저장소의 어휘 baseline). Graph 확장이
  실제로 뭘 더하는지 비교용.
- `--explain` = 씨앗에서 결과까지 엣지 경로 출력.
- PageRank는 호출 그래프가 작아(111 엣지) 스파이크가 있다(`_build_parse_instruction`이
  pr=1.0). `--centrality 0`으로 끌 수 있고 기본 가중치는 0.15로 낮게 뒀다.

### 5-3. 알려진 한계

| 한계 | 영향 | 완화 |
|---|---|---|
| 이름만으로 호출 해석 | 동명이인 함수(테스트 파일의 mock `ensure_tab` 등)가 섞임 | 같은 클래스/파일 우선 규칙은 있으나 완전하진 않음 |
| 중첩 def의 호출을 부모에 귀속 | 이 저장소엔 거의 무해 | 필요 시 스코프 추적 추가 |
| `obj.method()` 대부분 `CALLS_EXT` | 서드파티 호출(`service.spreadsheets()...`)은 구조에 안 들어옴 | 의도된 것(외부는 leaf) |
| 씨앗이 0이면 결과 0 | 순수 동의어 질의에 취약 | 아래 하이브리드 |
| `.gs` 호출 그래프가 근사 | JS 쪽 확장은 신뢰도 낮음 | 정식 JS 파서 필요 |

---

## 6. 둘을 합치면 (하이브리드 제안 — 미구현)

이 저장소가 보여주는 자연스러운 결론: **vector로 씨앗을 뽑고 graph로 확장**한다.

```
질의 → (BM25 + 벡터 RRF)로 씨앗 top-k        ← search.py의 강점: 동의어에 강한 씨앗
     → graph.json 엣지로 BFS 확장 + 경로 설명  ← graph_search.py의 강점: 구조적 이웃
     → RRF로 최종 융합
```

- 3-3("중복을 어떻게 막나")처럼 어휘가 안 겹치는 질의에서 벡터가 씨앗을 구제하고,
- 3-1("소계 계산")처럼 헬퍼가 딸려와야 하는 질의에서 그래프가 recall을 올린다.
- 구현은 `graph_search.GraphSearcher`의 씨앗 선정부만 `search.HybridSearcher` 결과로
  교체하면 된다(엣지·확장 로직은 그대로 재사용).

---

## 7. 사용법 빠른 참조

```bash
# --- Graph RAG (무의존성, 먼저 빌드) ---
.venv/Scripts/python.exe scripts/graph_build.py --stats          # rag/graph.json 생성
.venv/Scripts/python.exe scripts/graph_search.py "카드사별 소계 계산" --explain
.venv/Scripts/python.exe scripts/graph_search.py "월별 탭 생성" --seeds-only   # BM25 baseline
.venv/Scripts/python.exe scripts/graph_query.py                  # 대화형 (:depth 0 으로 씨앗만)

# --- Vector / Hybrid RAG (기존) ---
.venv/Scripts/python.exe scripts/chunk.py
.venv/Scripts/python.exe scripts/index.py --reset
.venv/Scripts/python.exe scripts/search.py "카드사별 소계 계산"
.venv/Scripts/python.exe scripts/query.py

# --- 같은 질의를 양쪽에 넣고 나란히(2단 표 + 공통/고유 요약) ---
.venv/Scripts/python.exe scripts/compare.py                       # 기본 질의 세트 6개
.venv/Scripts/python.exe scripts/compare.py "분류별 지출 원형차트" -n 6
.venv/Scripts/python.exe scripts/compare.py "소계 계산" --stacked --full   # 본문까지 세로로
.venv/Scripts/python.exe scripts/compare.py -f queries.txt --json          # 기계 판독
```

[`scripts/compare.py`](scripts/compare.py)가 `HybridSearcher`와 `GraphSearcher`를 한 번씩만
로드해 질의 목록을 돌리고, 질의마다 좌(vector)/우(graph) 표와 **공통 / vector 고유 /
graph 고유** 히트를 요약한다. 위 §3의 비교표가 이 스크립트 출력이다.

`graph_search.py` 주요 옵션: `--depth`(홉, 기본 2) · `--decay`(감쇠, 0.5) ·
`--centrality`(PageRank 가중, 0.15) · `--seeds`(씨앗 수, 6) · `--seeds-only` ·
`--kind` · `--file` · `--explain` · `--full` · `--json`.

---

## 8. 이 저장소에서 배운 것 (요약)

1. **Vector RAG는 "의미가 비슷한 조각"까지만 준다.** 함수 사이의 호출 관계를 모르므로
   "이 기능에 엮인 코드 전부"를 물으면 어휘가 겹치는 것만 모은다.
2. **Graph RAG는 씨앗의 질에 전적으로 의존한다.** 씨앗이 좋으면(`apply_card_totals`)
   호출 사슬을 정확히 복원하고 근거 경로까지 준다. 씨앗이 흩어지면(추상적 "흐름" 질의)
   vector보다도 나쁠 수 있다.
3. **비용 구조가 정반대다.** Vector는 470 MB 모델 + 벡터 DB, Graph는 표준 라이브러리 4초.
   작은 코드베이스에서 "구조 질의"가 잦다면 graph의 가성비가 압도적이다.
4. **정답은 대개 하이브리드**(§6): 벡터로 씨앗, 그래프로 확장.
5. 부수 효과로 graph 빌더가 vector 인덱스의 알려진 결함 두 개(라인 기반 id의 stale,
   비연속 module 청크)를 설계로 우회했고, `.gs`까지 인덱스에 포함시켰다.
