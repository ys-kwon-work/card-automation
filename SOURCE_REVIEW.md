# 소스 리뷰 — 카드 명세서 자동화

이 문서는 저장소의 코드가 **어떻게 동작하는지**를 파일·함수 단위로 설명합니다.
세팅·배포 절차는 [`README.md`](README.md)를 보세요.

- 대상 커밋 기준: `feac20a` (2026-08-27, "시트 일자 버그 수정, AI API 예외 처리 추가")
- 구성 요소는 두 개뿐입니다: **Apps Script**(Gmail 감지 + 호출) → **Cloud Run**(복호화 + 파싱 + 시트 기록).

---

## 1. 전체 아키텍처

```
                 매일 08:00 시간 트리거
                         │
        ┌────────────────▼─────────────────┐
        │  Apps Script  (apps_script/Code.gs)│
        │  · Gmail에서 "정산완료" 라벨 없는  │
        │    카드사별 명세서 메일 검색       │
        │  · 첨부파일을 base64로 인코딩      │
        │  · card_type/password/filename과   │
        │    함께 POST                        │
        └────────────────┬─────────────────┘
                         │  HTTPS POST /process
                         │  헤더: X-Shared-Secret
                         ▼
        ┌──────────────────────────────────────────────┐
        │  Cloud Run  (cloud_run/main.py, Flask)        │
        │                                              │
        │  1) 복호화 (card_type로 분기)                 │
        │     BC PDF / 신한 PDF → 페이지 PNG 이미지     │
        │     BC 엑셀           → 셀 텍스트             │
        │     현대 HTML / 삼성 HTML → Playwright 텍스트 │
        │                                              │
        │  2) AI 파싱 (PARSER_ENGINE)                   │
        │     claude → tool use 강제 JSON               │
        │     gemini → response_json_schema 강제 JSON   │
        │     → [{일자,가맹점,금액,분류}, ...]          │
        │                                              │
        │  3) Google Sheets 기록                        │
        │     · 파일명 → 월별 탭(YYYYMM), 없으면 생성   │
        │     · (카드명,일자,가맹점,금액) 중복 스킵     │
        │     · EXCLUDED_MERCHANT_SUBSTRINGS 제외       │
        │     · 카드사별 소계 + 전체 합계 재계산        │
        └────────────────┬─────────────────────────────┘
                         │  200 {status:"ok", rows_added, rows_skipped}
                         ▼
        Apps Script: 성공 → 스레드에 "정산완료" 라벨
                     실패 → 본인 메일로 알림(notifyFailure_)
```

핵심 설계 원칙:

1. **비밀번호는 Cloud Run에 저장하지 않는다.** 매 요청 payload로 받는다. 저장은
   Apps Script 스크립트 속성 한 곳(+ AI 키 등 서버 설정은 Cloud Run 환경변수).
2. **멱등성.** 같은 메일을 여러 번 처리해도 시트에 중복이 생기지 않는다. 라벨이
   유실되거나 수동 강제 재처리(`forceReprocessAll`)를 해도 안전.
3. **실패는 라벨을 안 붙이는 것으로 표현한다.** Cloud Run이 500을 반환하면 Apps
   Script가 라벨을 안 달고 알림 메일을 보낸다 → 다음 트리거에서 자동 재시도됨.
4. **파싱 엔진은 코드에 둘 다 유지.** `PARSER_ENGINE` 환경변수만으로 전환. 재배포 불필요.

---

## 2. `apps_script/Code.gs` — Gmail 감지 + 호출

Google Apps Script(V8). Gmail·트리거·외부 요청·메일 발송 권한을 씁니다
(`appsscript.json`의 `oauthScopes`).

### 상수 / 설정

| 이름 | 역할 |
|---|---|
| `ACCEPTED_EXTENSIONS` | 카드사별 허용 첨부 확장자. BC만 `.pdf`/`.xlsx`/`.xls` 셋 다, 신한 `.pdf`, 현대·삼성 `.html`/`.htm`. |
| `getConfig_()` | 스크립트 속성에서 `CLOUD_RUN_URL`, `SHARED_SECRET`, 카드사 4종 비밀번호, `PROCESSED_LABEL`(기본 `정산완료`)을 읽어 객체로 반환. |

### 진입점 3개

| 함수 | 호출 방식 | 동작 |
|---|---|---|
| `checkNewStatements()` | 시간 트리거(매일) | `runCheck_(false)` — 라벨 없는 메일만 |
| `forceReprocessAll()` | 편집기에서 수동 실행 | `runCheck_(true)` — 라벨 유무 무시하고 전부 |
| `createTimeTrigger()` | 최초 1회 수동 실행 | `checkNewStatements`용 기존 트리거 제거 후 `everyDays(1).atHour(8)` 재설치 |

### `runCheck_(includeAlreadyLabeled)`

- 라벨 객체를 준비(`getOrCreateLabel_`)하고, `includeAlreadyLabeled`가 false면
  검색어에 ` -label:정산완료`를 덧붙인다.
- 카드사 4종을 **각각 따로** 검색한다. 쿼리는 `in:inbox subject:<카드명> subject:명세서`.
  - `in:inbox` → 받은편지함만(보관/다른 라벨 제외). 스팸·휴지통은 `GmailApp.search()`가
    기본적으로 빼므로 추가 조건 불필요.
  - 카드사별로 나눈 이유: 제목 조건이 다르고, 매칭되는 순간 `card_type`을 바로 알 수 있음.
- 각 스레드마다 `processThread_`.

### `processThread_(thread, cardType, cfg, label)`

스레드의 모든 메시지 → 모든 첨부파일을 순회하며:

1. 파일명 소문자로 만들어 `ACCEPTED_EXTENSIONS[cardType]` 중 하나로 끝나는지 검사.
   아니면 건너뜀(예: 현대카드 메일에 붙은 안내 PDF).
2. `cardType`에 맞는 비밀번호를 고른다.
3. payload 구성: `{card_type, file_base64, password, filename}`.
   `filename`은 원본 이름 그대로(Cloud Run이 여기서 날짜를 뽑아 월별 탭을 정함).
4. `UrlFetchApp.fetch(cloudRunUrl + '/process', ...)` — `X-Shared-Secret` 헤더 포함,
   `muteHttpExceptions: true`(4xx/5xx에서도 예외 대신 응답 객체를 받아 직접 처리).
5. `code === 200 && body.status === 'ok'` →
   - **스레드**에 라벨을 붙인다(메시지 단위가 아님 — 스레드 전체가 처리 완료 표시됨).
   - 로그에 `추가 N건 / 중복 M건`.
   그 외 → 로그 + `notifyFailure_`(본인 메일로 파일명·오류 메시지 발송).

> **주의점**: 라벨은 스레드 단위. 한 스레드에 첨부가 여러 개인데 일부만 성공하면,
> 성공한 첨부가 하나라도 있으면 라벨이 붙는다. 다만 Cloud Run 쪽 중복 스킵이 있어
> 다음 실행에서 `forceReprocessAll`로 안전하게 메꿀 수 있다.

### 보조 함수

- `notifyFailure_(cardType, filename, message)` — `MailApp.sendEmail`로 본인에게 실패 알림.
- `getOrCreateLabel_(name)` — 라벨 조회, 없으면 생성.

---

## 3. `cloud_run/main.py` — 복호화 + 파싱 + 기록

Flask 앱 하나. 엔드포인트는 `/process`(POST)와 `/healthz`(GET) 둘뿐.

> 환경변수는 **모듈 로드 시점이 아니라 실제 사용하는 함수 안에서** 읽는다.
> `test_local.py`로 복호화 로직만 단독 테스트할 때 나머지 값(시트 자격증명 등)이
> 없어도 되게 하려는 의도.

### 3.1 상수 / 설정

| 이름 | 값 / 역할 |
|---|---|
| `SHEET_TAB` | 파일명에서 날짜를 못 찾을 때 쓰는 폴백 탭 이름(기본 `시트1`) |
| `PARSER_ENGINE` | `claude`(기본) 또는 `gemini`. env에서 읽어 소문자화 |
| `ANTHROPIC_MODEL` / `GEMINI_MODEL` | 기본 `claude-sonnet-5` / `gemini-3.6-flash`, env로 덮어쓰기 가능 |
| `SHEET_HEADERS` | `["카드명","일자","가맹점","금액","분류"]` |
| `CATEGORY_CHOICES` | 분류 10종(식비/카페·간식/교통/쇼핑/통신/의료/문화·여가/교육/주거·공과금/기타) |
| `CATEGORY_OVERRIDE_RULES` | `(키워드들, 분류)` 목록. 가맹점명에 키워드가 포함되면 AI 분류를 강제 교체(위에서부터 첫 매칭 1개). 편의점류(씨유·이마트24·GS25·KFC·`지에스 더프레시`·노랑냉장고)→`카페/간식`, 학원류(교육·학원·스터디·`더지니어스아라`)→`교육` |
| `EXCLUDED_MERCHANT_SUBSTRINGS` | 가맹점명에 이 문자열이 들어가면 시트에 안 씀(개인 사용내역 제외) |
| `GRAND_TOTAL_EXCLUDED_CARDS` | `{"현대카드"}` — 전체 합계에서만 제외(소계엔 포함) |
| `CHART_EXCLUDED_CARDS` | `{"현대카드"}` — 분류별 원형차트 집계에서 제외 |
| `TRANSACTION_SCHEMA` | AI가 반환해야 하는 JSON Schema. `transactions: [{일자,가맹점,금액,분류}]` |
| `_build_parse_instruction(card_name)` | 파싱 프롬프트 문자열 생성(규칙: 합계행 제외, 취소=음수, 가맹점 원문, 분류는 enum) |

### 3.2 복호화 계층

#### `_decrypt_pdf_to_page_images(pdf_bytes, password) -> list[bytes]`  (BC·신한 공용)

1. `pypdf.PdfReader`로 열고, 암호화돼 있으면 `reader.decrypt(password)`.
   반환값 `0`이면 비밀번호 오류 → `ValueError`.
2. 복호화된 페이지를 `PdfWriter`로 새 버퍼에 다시 쓴다(암호 제거된 깨끗한 PDF).
3. `pypdfium2`로 그 버퍼를 열고 **각 페이지를 `scale=2.5`로 렌더링 → PIL → PNG bytes**.
4. 이미지 리스트 반환. 하나도 없으면 `ValueError`.

> **왜 텍스트가 아니라 이미지인가**: BC·신한 PDF는 복사/추출 방지를 위해 폰트
> 인코딩을 페이지당 수십 개 서브셋 폰트로 잘게 쪼개 스크램블해 둠. `pdfplumber` 등으로
> 텍스트를 뽑으면 가맹점명이 깨진 글자(`ŸœÃÒ ƒ"´¬Œ_ˆ∫`)로 나온다(숫자·날짜는 정상).
> 페이지를 이미지로 렌더링하면 육안·비전모델 모두 정상적으로 읽힌다. 실제 파일로 확인됨.

- `decrypt_bc_pdf` / `decrypt_shinhan_pdf` 는 이 함수를 그대로 호출하는 얇은 래퍼.
  신한은 2026-08-26에 BC와 동일한 스크램블임이 확인되어 경로를 재사용.

#### `decrypt_bc_excel(file_bytes, password) -> str`  (BC가 엑셀로 올 때)

1. `msoffcrypto.OfficeFile`로 암호화 여부 판별. 인식 못하는 포맷이면 비암호로 간주.
2. 암호화돼 있으면 `load_key(password=...)` → `decrypt()`. 실패 시 `ValueError`.
3. 셀 읽기: 먼저 `openpyxl`(신형 `.xlsx`/OOXML)로 시도. 실패하면 `xlrd`(구형 `.xls`/BIFF8)로 재시도.
   각 시트를 `[시트: 이름]` 헤더 + 탭 구분 텍스트로 직렬화. 전부 비어 있으면 `ValueError`.
4. 텍스트 반환 → `parse_transactions(raw_text=...)` 경로로.

> 엑셀은 폰트 스크램블 문제가 없어 이미지 렌더링이 불필요. **단 실제 BC 엑셀 샘플로
> 아직 미검증** — 셀 레이아웃에 따라 프롬프트 조정이 필요할 수 있음.

#### `decrypt_hyundai_html(html_bytes, password) -> str`  (Playwright)

파일 자체엔 복호화 로직이 없고, 외부 스크립트(`hyundaicard.com/.../email_new.js`)가
브라우저 안에서 `doAction()`으로 복호화 → **헤드리스 브라우저 필수**.

1. HTML을 임시파일로 저장, `file://`로 로드. `DEBUG_HEADED=1`이면 창을 띄운다.
2. **가짜/진짜 입력칸 우회**:
   - 진짜 입력칸 `#password`는 `display:none`, 화면엔 안내문구 달린 가짜 `name="p2_temp"`만 보임.
   - 가짜 입력칸을 먼저 `click()`(실패해도 무시) → 카드사 JS `onfocus="changeText(this)"`가 상태 전환.
   - 진짜 값은 `eval_on_selector`로 `#password.value`에 직접 주입 + `input/keyup/change` 이벤트 디스패치.
3. **"조회 확인" 버튼(`input[type="image"].w_section`)은 정확히 1회만 클릭**.
   `context.expect_page(timeout=5000)`로 감싸 팝업(새 창)이 뜨는지 확인.
   - 팝업 O → 그 페이지에서 추출.
   - 팝업 X(실측된 정상 케이스) → 같은 페이지에서 3초 대기 후 추출.

   > 과거 버그: 팝업을 기다렸다 없으면 같은 버튼을 한 번 더 클릭 → 첫 클릭에서 이미
   > 버튼이 DOM에서 사라져 두 번째 클릭이 항상 30초 타임아웃. **클릭 1회 규칙**으로 수정.
4. **"결제상세내역 더 보기"(`a.detailView`)** 토글을 방어적으로 전부 클릭.
   실측상 이 카드사는 CSS 클리핑이라 클릭 전에도 `inner_text()`가 전체를 반환하지만,
   `display:none` 방식으로 바뀔 경우를 대비.
5. `target_page.inner_text("body")` 추출. 비어 있으면 `hyundai_debug_failure.png`
   스크린샷을 남기고 `ValueError`.

#### `decrypt_samsung_html(html_bytes, password) -> str`  (Playwright)

현대카드와 **다르게** 진짜 표시형 입력칸을 씀:

1. 임시파일 → `file://` 로드. `page.on("dialog", ...)`로 JS `alert` 메시지를 모으고 `accept()`.
2. `#password`에 그냥 `fill()`, `#confirm` 클릭, 6초 대기.
3. `dialog_messages`가 있으면(비번 오류 `alert("비밀번호 입력이 잘못되었습니다.")` 등)
   즉시 `ValueError`.
4. **진짜 페이지네이션 처리** — 최대 20회 루프:
   - `text=더보기` 요소가 없으면 종료.
   - 텍스트에서 `현재페이지 X / 전체페이지 Y`를 정규식으로 파싱, `X == Y`면 종료.
   - 아니면 `더보기` 클릭 + 2초 대기.

   > 현대카드의 "더보기"와 달리 **클릭 전에는 데이터가 실제로 DOM에 없음**(총 17건 중
   > 첫 클릭 전 10건만 존재, 실측). 끝까지 클릭하지 않으면 거래가 조용히 누락됨.
5. `page.inner_text("body")` 추출. 비어 있으면 `ValueError`.

### 3.3 AI 파싱 계층

#### `parse_transactions(card_name, raw_text=None, page_images=None)`

`PARSER_ENGINE` 값으로 `_with_gemini` / `_with_claude` 중 하나로 위임. 그 외 값이면 `ValueError`.

#### `parse_transactions_with_claude(...)`

- `_build_parse_instruction`을 살짝 바꿔("JSON으로 반환" → "record_transactions 도구를 호출").
- content 구성:
  - `page_images` 있으면 → 각 PNG를 base64 이미지 블록으로 추가(비전 입력).
  - 없으면 → `raw_text[:15000]`를 프롬프트 뒤에 붙임.
- **tool use 강제**: `record_transactions`라는 가상 도구(`input_schema = TRANSACTION_SCHEMA`)를
  정의하고 `tool_choice={"type":"tool","name":"record_transactions"}` → 모델이 자유
  텍스트 대신 스키마에 맞는 도구 입력값(JSON)만 반환.
- **재시도 로직(최대 3회)**: 응답의 `tool_use` 블록에서 `transactions`를 꺼내
  `list[dict]`인지 검사. 아니면(드물게 JSON 전체가 문자열 하나로 옴 — 2026-08-27,
  4번 중 1번꼴 실측) 같은 요청을 다시 보냄. 3회 다 실패하면 마지막 값의 타입·앞부분을
  담은 `ValueError`.

  > `temperature`로 결정성을 높이려 했으나 설치된 `anthropic==1.0.0`의
  > `messages.create()`가 파라미터 자체를 안 받음(`TypeError`). 그래서 재시도로 대응.

#### `parse_transactions_with_gemini(...)`

- `google-genai` 사용. `contents`는 Claude와 같은 원리(이미지 or 텍스트).
- `GenerateContentConfig(response_mime_type="application/json",
  response_json_schema=TRANSACTION_SCHEMA, temperature=0)` — 서버가 스키마를 강제.
- Claude와 동일한 재시도 + 형태 검증(Gemini는 서버 강제라 확률이 낮지만 대비).

  > `generativelanguage.googleapis.com`은 Claude Code(Cowork) 세션의 네트워크
  > 허용목록에 없어 세션 안에서는 403. 사용자 PC / Cloud Run에선 정상.

### 3.4 Google Sheets 기록 계층

#### `month_tab_name(filename) -> str`

정규식 `(20\d{2})(0[1-9]|1[0-2])(?:\d{2})?` 로 파일명에서 `YYYYMM` 또는 `YYYYMMDD`를
찾아 앞 6자리를 탭 이름으로. 못 찾으면 `SHEET_TAB`.

> 카드사/형식마다 파일명 날짜 길이가 다름(BC `..._20260813.pdf` 8자리, 현대
> `hyundaicard_202606.html` 6자리) — 둘 다 지원.

#### `ensure_tab(service, spreadsheet_id, tab_name)`

스프레드시트 메타를 읽어 탭이 있으면 return. 없으면 `addSheet` → `A1:E1`에 헤더 기입.

#### `_existing_transaction_keys(service, ...) -> set[tuple]`

탭의 `A2:E100000`을 `UNFORMATTED_VALUE`로 읽어 `(카드명, 정규화된 일자, 가맹점, 금액)`
튜플 집합 반환. 소계(`" 소계"`로 끝남)·전체 합계 행은 제외. **중복 판정 키**.

#### `_validate_transactions(transactions)`

AI 반환값이 `list[dict]`이고 각 항목에 `{일자,가맹점,금액,분류}`가 다 있는지 사전
검증. 아니면 "잠시 후 `forceReprocessAll` 재시도" 안내가 담긴 `ValueError`.
(안 하면 뒤에서 `string indices must be integers` 같은 원인 불명 에러로 터짐.)

#### `append_rows_to_sheet(card_name, transactions, filename="") -> {added, skipped}`

1. `_validate_transactions`.
1-B. `_apply_category_overrides` — 가맹점명이 `CATEGORY_OVERRIDE_RULES`에 걸리면 `분류`를 제자리에서 교체(AI 결과보다 우선). `/process` 응답의 `transactions`에도 반영됨.
2. `SHEETS_SERVICE_ACCOUNT_JSON`으로 서비스 계정 자격증명 → `sheets v4` 클라이언트.
3. `month_tab_name(filename)` → `ensure_tab`.
4. `_existing_transaction_keys`로 기존 키 집합 확보.
5. 각 거래에 대해:
   - 가맹점명이 `EXCLUDED_MERCHANT_SUBSTRINGS`에 걸리면 skip(카운트 안 함).
   - `(카드명, 일자, 가맹점, 금액)` 키가 이미 있으면 `skipped += 1`.
   - 아니면 키 집합에 추가(같은 요청 내 중복도 방지) 후 행 목록에 추가.
6. 새 행이 있으면 `values().append(...INSERT_ROWS)` → **`apply_card_totals` 호출**.
7. `{"added": len(rows), "skipped": skipped}` 반환.

#### `apply_card_totals(service, spreadsheet_id, tab_name)`  — 소계/합계 재계산 (멱등)

1. 탭의 `sheetId`를 찾는다(없으면 return).
2. `A2:E100000`을 읽어 **소계/합계 행을 걷어낸 순수 거래 행**만 남김. 없으면 return.
3. 등장 순서를 유지하며 카드명으로 그룹핑.
4. `new_rows` 구성: 각 그룹의 거래 행들 → `<카드명> 소계` 행(금액=그룹 합) → …
   → 마지막에 `전체 합계` 행(`GRAND_TOTAL_EXCLUDED_CARDS` 제외 합).
5. **값 + 서식을 한 번의 `updateCells`로 원자적으로 기입**:
   - 금액 열(col 3): `numberValue` + 통화 서식 `#,##0"원";-#,##0"원"`.
   - 나머지 열: `stringValue` + `TEXT` 서식(스마트 파싱 차단).
   - 일자 열(col 1): `_normalize_date_cell_value`로 숫자화된 값을 날짜 문자열로 복원.
   - 소계 행: 연한 파랑 배경 + 굵게 / 전체 합계 행: 금색 배경 + 굵게.
6. 재계산 결과가 기존보다 짧으면(소계 정리로 행이 줄면) 꼬리 행을 값·서식 모두 비운다.
7. `batchUpdate` 실행.

> **왜 값·서식을 한 요청에 묶나**: 값을 먼저 쓰고 서식을 나중에 별도 요청으로 입히면,
> Sheets가 값 입력 시점에 열 서식을 "자동 감지"로 되돌려 통화 서식이 사라지는
> 경우가 있었음(2026-08-26 확인). 원자적으로 적용하면 사라지지 않음.

8. **`_upsert_category_pie_chart` 호출** — 소계/합계가 최신이 된 직후 분류별 원형차트 갱신.

#### `_upsert_category_pie_chart(service, spreadsheet_id, tab_name, sheet_id, anchor_row_index)`  — 분류별 지출 비중 원형차트 (멱등)

1. 거래표(A:E)와 겹치지 않게 **G:H에 "분류 / 금액" 2열 집계표**를 `updateCells`로 씀.
   각 분류 행의 금액은 고정값이 아니라 수식 `=SUMIFS($D:$D,$E:$E,$G행,$A:$A,"<>"&"현대카드")`.
   → 사람이 나중에 금액 셀을 고치면 표·차트가 자동 재계산됨(소계/합계와 같은 설계).
   `CHART_EXCLUDED_CARDS`(현대카드)는 SUMIFS 조건에서 빠지므로 차트에 안 잡힘.
2. 스프레드시트 메타에서 이 탭에 이미 있는 제목 `분류별 지출 비중` 차트를 찾아
   `deleteEmbeddedObject`로 지운 뒤, 같은 `batchUpdate`에서 `addChart`로 다시 그림.
   → 매 처리마다 차트가 1개만 유지되고, 위치(전체 합계 두 줄 아래)·데이터가 최신으로 갱신됨.
   사용자가 직접 만든 다른 차트는 제목이 안 맞으므로 건드리지 않음.
3. 차트는 `overlayPosition`으로 전체 합계 행 아래(`anchor_row_index`, A열)에 앵커.
   도메인=G1:G10, 계열=H1:H10, 범례는 오른쪽.

#### 값 정규화 헬퍼

- `_to_amount(value) -> int` — int/float는 그대로, 문자열은 `,` 제거 후 int, 그 외 0.
- `_normalize_date_cell_value(value) -> str` — 숫자(날짜 일련번호)면
  `date(1899,12,30) + timedelta(days=value)`로 `YYYY-MM-DD` 복원. 아니면 문자열화.

  > **일자 버그**: `USER_ENTERED`로 `"2026-01-01"`을 쓰면 Sheets가 스마트 파싱해서
  > 일련번호 `46023`으로 바꿔 저장 → 다시 읽으면 숫자. 중복 판정과 소계 재계산이
  > 깨졌음. 이 함수 + `TEXT` 서식 고정으로 해결.

### 3.5 HTTP 엔드포인트

#### `POST /process`

1. `X-Shared-Secret` 헤더가 env와 다르면 `401`.
2. payload에서 `card_type`, `file_base64`(→ decode), `password`, `filename` 추출.
3. `CARD_NAME_MAP`(`BC/HYUNDAI/SAMSUNG/SHINHAN` → 한글 카드명)에 없으면 `400`.
4. `card_type`별 분기:
   | card_type | 복호화 | 파싱 입력 |
   |---|---|---|
   | `BC` + 확장자 `.xlsx/.xls` | `decrypt_bc_excel` | `raw_text` |
   | `BC` (그 외 = PDF) | `decrypt_bc_pdf` | `page_images` |
   | `SHINHAN` | `decrypt_shinhan_pdf` | `page_images` |
   | `HYUNDAI` | `decrypt_hyundai_html` | `raw_text` |
   | `SAMSUNG` | `decrypt_samsung_html` | `raw_text` |
5. `append_rows_to_sheet` → `200 {status:"ok", rows_added, rows_skipped, transactions}`.
6. 어떤 예외든 `traceback.print_exc()`(Cloud Run 로그) 후 `500 {status:"error", message}`.
   → Apps Script가 라벨을 안 붙임 → 다음 트리거에서 재시도.

#### `GET /healthz`

`{"status":"ok","parser_engine": PARSER_ENGINE}`. 배포 상태 + 현재 엔진 즉시 확인용.

---

## 4. `cloud_run/` 테스트 스크립트

| 파일 | 범위 | 요약 |
|---|---|---|
| `test_local.py` | 파일 1개 | `python test_local.py <bc\|hyundai\|samsung\|shinhan> <파일> [비번]`. 항상 복호화 실행(PDF는 `*_page_N.png` 저장), `.env`에 해당 엔진 API 키가 있으면 파싱까지 실행해 거래 목록 출력. 시트 기록은 안 함. |
| `test_gmail_fetch.py` | 실제 Gmail → 시트 | IMAP + 앱 비밀번호로 `All Mail` 접속, `X-GM-RAW`로 Code.gs와 같은 조건 검색 → 첨부 추출(MIME 파일명 디코딩 포함) → `main.py`의 복호화/파싱/`append_rows_to_sheet` 재사용. 운영 흐름 리허설. |
| `test_sheets_write.py` | 시트 쓰기만 | 루트의 서비스 계정 키로 인증, 파일명 기반 월별 탭 생성 + 테스트 행 추가 → 검증 후 그 행만 삭제(탭은 유지). 일회성. |

`test_gmail_fetch.py` 세부:
- `find_all_mail_folder` — Gmail 표시 언어와 무관하게 `\All` 플래그 폴더를 찾음.
- `imap_search` — 한글 검색어는 IMAP 리터럴(`{len}\r\n<bytes>`)로 보내야 함
  (quoted string에 8bit 넣으면 Gmail이 "Could not parse command"로 거부, 실측).
- `decode_mime_filename` — `=?UTF-8?B?...?=` 다중 encoded-word 파일명 디코딩(Daum 전달 메일 대응).

---

## 5. 의존성 (`requirements.txt`)

| 패키지 | 용도 |
|---|---|
| `flask`, `gunicorn` | 웹 서버 |
| `pypdf` | PDF 복호화(암호 해제) |
| `pypdfium2`, `Pillow` | PDF 페이지 → PNG 렌더링 |
| `msoffcrypto-tool`, `openpyxl`, `xlrd` | 암호 엑셀 복호화 + 셀 읽기(신형/구형) |
| `playwright`, `greenlet` | 현대·삼성 보안 HTML을 브라우저로 열어 복호화 |
| `anthropic` | Claude 파싱 엔진 |
| `google-genai` | Gemini 파싱 엔진 |
| `google-api-python-client`, `google-auth` | Sheets API |
| `python-dotenv` | 로컬 테스트에서 `.env` 로드 |

`Dockerfile`: `python:3.12-slim` → `pip install` + `playwright install --with-deps chromium`
→ `gunicorn --timeout 120 main:app` (포트 8080).

---

## 6. 알려진 리스크 / 개선 여지

| 항목 | 내용 |
|---|---|
| **보안 HTML 뷰어 취약성** | 현대·삼성이 페이지 구조(선택자, 팝업/다이얼로그, 페이지네이션 문구)를 바꾸면 조용히 실패. 선택자가 하드코딩돼 있음. `notifyFailure_` 알림 + 로그 확인 습관으로 커버. |
| **BC 엑셀 경로 미검증** | `decrypt_bc_excel`은 실제 샘플로 테스트되지 않음. 셀 레이아웃에 따라 프롬프트 조정 필요 가능. |
| **삼성 페이지네이션 누락 위험** | 20회 루프 상한 + 클릭 실패 시 `break`. 거래가 그보다 많거나 클릭이 느리면 일부 누락 가능 → 시트 건수 대조 권장. |
| **라벨은 스레드 단위** | 한 스레드 내 첨부 일부만 성공해도 라벨이 붙음. Cloud Run 중복 스킵 + `forceReprocessAll`로 보완. |
| **AI 스키마 이탈** | `transactions`가 문자열로 오는 사례 실측. 최대 3회 재시도 후 명확한 에러. 근본 해결책 아님. |
| **비밀번호 평문 저장** | Apps Script 스크립트 속성 + Cloud Run 환경변수 두 곳. Secret Manager로 이동 가능. |
| **`SHARED_SECRET` 단일 방어** | `--allow-unauthenticated` + 헤더 검사. 더 엄격히 하려면 IAM(ID 토큰) 인증으로 전환 가능. |
| **`raw_text[:15000]` 절단** | 거래가 아주 많은 텍스트 명세서는 뒷부분이 잘릴 수 있음(현재 규모에선 여유). |
| **환경변수 이름 2벌** | 로컬 `.env`(`*_CARD_PASSWORD`)와 Apps Script 속성(`*_PDF/HTML_PASSWORD`) 이름이 다름. 값은 동일. README에 명시됨. |
