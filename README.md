# 카드 명세서 자동화 — 옵션 A 구현 (Google Apps Script + Cloud Run)

BC바로카드(암호 PDF/엑셀) / 현대카드(보안 HTML) / 삼성카드(보안 HTML) / 신한카드(암호 PDF)
명세서를 Gmail에서 찾아 AI(Claude 또는 Gemini, 전환 가능)로 파싱하고, 아래 시트에
자동으로 행을 추가합니다.

- 대상 시트: https://docs.google.com/spreadsheets/d/1b1Y50n_AlJ4fTFGxVlmzXCEEvpt8HEyqEHD2N5buFv0/edit
- 헤더: `카드명 | 일자 | 가맹점 | 금액 | 분류`
- 명세서 첨부파일명에서 날짜를 뽑아 **월별 탭(`YYYYMM`)** 에 기록하고, 탭이 없으면
  헤더와 함께 자동으로 만듭니다.
- 탭마다 **카드사별 소계 + 전체 합계** 행을 자동으로 다시 계산해 붙입니다(멱등적).

> 소스 구조·함수 단위 설명은 [`SOURCE_REVIEW.md`](SOURCE_REVIEW.md)를 참고하세요. 이
> README는 "무엇을 어떻게 세팅하고 돌리는가"에, `SOURCE_REVIEW.md`는 "코드가 어떻게
> 동작하는가"에 집중합니다.

## 실제 파일로 검증 완료된 내용 (2026-08-27 기준)

네 카드사 모두 실제 명세서 파일 + 실제 비밀번호로 복호화부터 AI 파싱까지 전 과정을
검증했습니다. 아래는 그 과정에서 확인된 사실과 고친 문제들입니다.

- **BC바로카드 PDF**: 표준 PDF 사용자 암호로 잠겨 있고(`is_encrypted: True`), `pypdf`로
  비밀번호만 알면 정상적으로 복호화됩니다.
  - **중요**: 복호화된 PDF의 텍스트 레이어(`pdfplumber` 등으로 추출)는 카드사가
    복사/추출 방지를 위해 폰트 인코딩을 스크램블해놔서 **가맹점명이 깨진 문자로만
    나옵니다** (숫자·날짜는 정상). 페이지를 이미지로 렌더링해서 보면 가맹점명이
    전부 또렷하게 보이므로, `main.py`는 텍스트 추출 대신 **`pypdfium2`로 각 페이지를
    PNG 이미지로 렌더링해서 AI에 비전 입력으로 직접 넣는 방식**을 씁니다
    (`decrypt_bc_pdf()`가 문자열이 아니라 이미지 바이트 리스트를 반환함).
  - 실제 파일로 끝까지 테스트한 결과, AI가 파싱한 거래 금액 합계가 명세서에 찍힌
    실제 총액과 정확히 일치하는 것까지 확인됨.
- **BC바로카드 — PDF 또는 엑셀**: BC바로카드 명세서는 PDF뿐 아니라 엑셀(`.xlsx`/`.xls`)
  로도 올 수 있어서, 첨부파일 확장자로 자동 판별해 처리합니다(`main.py`의
  `decrypt_bc_excel()`). PDF와 달리 엑셀 셀 값은 폰트 스크램블 문제가 없어 이미지
  렌더링 없이 셀 값을 텍스트로 바로 추출해 AI에 넘깁니다. 엑셀도 PDF처럼 비밀번호로
  암호화되어 있는 것이 일반적이라(`msoffcrypto-tool`로 복호화) 같은 BC 비밀번호를
  그대로 사용하며, 복호화 후 신형(`.xlsx`, OOXML)은 `openpyxl`, 구형(`.xls`, BIFF8)은
  `xlrd`로 셀을 읽습니다. **다만 아직 실제 BC 엑셀 샘플 파일로 검증하지 못했으니**,
  실제 파일을 받으면 `python test_local.py bc 실제파일.xlsx`로 먼저 로컬 검증을
  권장합니다.
- **신한카드 PDF**: BC바로카드와 **완전히 동일한 상황**입니다 — 표준 암호 PDF이고,
  텍스트 레이어가 복사방지용으로 스크램블되어 있습니다(2026-08-26 실제 파일로 확인).
  그래서 BC와 같은 이미지 렌더링 경로(`_decrypt_pdf_to_page_images()`)를 그대로
  재사용합니다(`decrypt_shinhan_pdf()`).
- **현대카드 HTML**: 파일 자체에는 암호화 로직이 없고, `https://www.hyundaicard.com/.../email_new.js`
  라는 **외부 스크립트**가 실제 복호화(`doAction()`)를 수행합니다. 이 파일은 인터넷에
  연결된 실제 브라우저 안에서만 열 수 있어 **헤드리스 브라우저(Playwright)가 필수**입니다.
  - 실제 비밀번호 입력칸(`#password`)은 `display:none`으로 숨겨져 있고, 화면에는
    안내문구가 적힌 가짜 입력칸(`name="p2_temp"`)만 보입니다. `main.py`는 가짜
    입력칸을 먼저 클릭해 카드사 JS가 상태를 전환하게 한 뒤, `#password`에는 JS로
    값을 직접 주입해 우회합니다.
  - **실제 비밀번호로 확인한 결과, "조회 확인" 버튼을 누르면 팝업(새 창) 없이 같은
    페이지 안에서 내용이 바로 교체됩니다.** (예전엔 팝업이 뜨는지 불확실해서 두
    경우를 모두 처리하려다 클릭을 두 번 하는 버그가 있었는데, 첫 클릭 이후 버튼이
    이미 DOM에서 사라져 두 번째 클릭이 항상 타임아웃 나는 문제였습니다 — 지금은
    클릭 1회 + 팝업 여부만 짧게 확인하는 방식으로 고쳐져 있습니다.)
  - 실제 파일로 텍스트 추출 및 AI 파싱까지 정상 동작 확인됨(가맹점명 깨짐 없음).
  - **"결제상세내역 더 보기" 처리**: 실제 브라우저로 열어보면 최근 거래 일부만
    보이고 "결제상세내역 더 보기"를 눌러야 전체 목록이 펼쳐집니다(사용자가 직접
    확인). 이 카드사는 접기를 CSS 클리핑으로 구현해서 Playwright의 `inner_text()`가
    클릭 전에도 이미 전체 텍스트를 반환하는 것으로 실측 확인됐지만(클릭 전/후
    텍스트 길이 동일), `main.py`는 카드사가 구현을 `display:none` 방식으로 바꾸는
    경우에 대비해 `a.detailView` 토글을 방어적으로 전부 클릭한 뒤 추출합니다.
  - 현대카드는 다른 카드사와 **결제일 기준이 겹쳐 이중 계상되는 경우**가 있어,
    **전체 합계(전체 합계 행)에서는 제외**합니다(`GRAND_TOTAL_EXCLUDED_CARDS`).
    카드사별 소계(현대카드 소계)에는 그대로 반영됩니다.
- **삼성카드 HTML**: 보안 HTML이지만 현대카드와 달리 **진짜 표시형 비밀번호 입력칸**
  (`#password`, `type="password"`)과 제출 버튼(`#confirm`)을 그대로 씁니다 — 위장
  입력칸이 없습니다(2026-08-25 실제 파일로 확인). Playwright로 그대로 채워서 제출합니다.
  - 비밀번호가 틀리면 JS `alert("비밀번호 입력이 잘못되었습니다.")`가 뜨는데, `main.py`는
    이 다이얼로그를 감지해서 실패로 처리합니다.
  - 거래 목록에 **진짜 페이지네이션**("더보기 (현재페이지 X/전체페이지 Y)")이 있고,
    현대카드와 달리 **클릭 전에는 실제로 데이터가 DOM에 없습니다**(총 17건인데 첫
    클릭 전엔 10건만 존재, 실측 확인). 그래서 마지막 페이지까지 반복 클릭이 필수입니다.
  - 복호화 과정에서 `samsungcard.com` 쪽으로 실제 네트워크 요청이 발생합니다(정적
    리소스 CORS 에러가 일부 나지만 본문 복호화 자체는 성공). Cloud Run처럼 일반
    인터넷 접근이 되는 환경이면 문제없습니다.
- **파싱 엔진**: 원래 Gemini API로 구현했으나, Gemini와 Claude API를 **둘 다 코드에
  유지**하고 `PARSER_ENGINE` 환경변수(`claude` 또는 `gemini`, 기본값 `claude`)로
  전환할 수 있게 만들어뒀습니다. 자세한 내용은 아래 "1단계" 참고.
  - AI가 아주 드물게(2026-08-27 실측, 4번 중 1번꼴) 요청한 스키마(거래 = 객체 배열)를
    벗어나 **`transactions`를 문자열 하나로 반환**하는 경우가 있어, `main.py`는 형태를
    검증해서 최대 3회 자동 재시도하고, 그래도 실패하면 원인을 알 수 있는 메시지로
    에러를 냅니다(`parse_transactions_with_claude` / `_validate_transactions`).
- **시트 일자 셀 버그**: Sheets에 `USER_ENTERED`로 `"2026-01-01"` 같은 문자열을 쓰면
  "사용자가 직접 입력한 것"처럼 스마트 파싱돼 **날짜 일련번호(예: 46023)로 바뀌어**
  다시 읽으면 숫자로 돌아오는 문제가 있었습니다. 지금은 소계/합계를 다시 쓸 때
  일자 열을 `TEXT` 서식으로 명시하고, 이미 숫자로 바뀐 값은 다시 날짜 문자열로
  되돌립니다(`_normalize_date_cell_value`). 중복 판정 키도 이 정규화를 거칩니다.
- **개인 사용내역 제외**: 시트에 남기고 싶지 않은 거래(가맹점명에 특정 문자열이
  들어간 건)는 `main.py`의 `EXCLUDED_MERCHANT_SUBSTRINGS`에 넣으면 파싱은 하되 시트
  기록 단계에서 건너뜁니다.

## 폴더 구성

```
cloud_run/
  main.py             복호화(BC/신한: PDF는 이미지 렌더링, BC 엑셀·현대·삼성: 텍스트 추출)
                      + AI 파싱(Claude 또는 Gemini, PARSER_ENGINE으로 전환)
                      + Sheets 기록(월별 탭 + 카드사별 소계/전체 합계)을 담당하는 Cloud Run 서비스
  requirements.txt
  Dockerfile
  test_local.py       ← 배포 전 로컬 검증용. 파일 하나로 복호화(+키 있으면 파싱까지) 확인
  test_gmail_fetch.py ← 로컬에서 실제 Gmail(IMAP)까지 연결해 검색→복호화→파싱→시트 기록 전 과정 검증
  test_sheets_write.py← 서비스 계정 Sheets 쓰기 + 파일명 기반 월별 탭 생성 검증(일회성)
  .env                ← 로컬 테스트용 실제 값 (git에 올리지 말 것, .gitignore에 포함됨)
  .env.example        ← .env 템플릿 (실제 값 없음, 이 파일은 공유해도 무방)
apps_script/          Gmail 감지 + Cloud Run 호출을 담당하는 Apps Script
  Code.gs
  appsscript.json
SOURCE_REVIEW.md      소스 코드 상세 설명 (아키텍처 / 함수 단위 워크스루 / 알려진 리스크)
```

## 테스트 방법 (0단계 — 배포 전 로컬 검증, 강력 권장)

GCP 배포 전에 `test_local.py`로 복호화 + (키가 있으면) AI 파싱까지 먼저 확인합니다.
이 스크립트는 **카드사별로 따로** 테스트하며, 두 단계로 동작합니다:

1. 항상 실행됨 — 파일을 실제 비밀번호로 복호화. BC바로카드·신한카드(PDF)는 각
   페이지를 `bc_page_1.png` / `shinhan_page_1.png` … 로 저장하고(직접 열어서
   가맹점명이 잘 보이는지 눈으로 확인 가능), BC 엑셀·현대카드·삼성카드는 추출된
   텍스트 앞부분을 화면에 출력합니다.
2. 조건부 실행 — `.env`(또는 환경변수)에 현재 `PARSER_ENGINE`에 맞는 API 키가
   있으면, 이어서 AI 파싱까지 자동 실행해서 거래 목록(일자/가맹점/금액/분류)을
   화면에 출력합니다. 키가 없으면 1번까지만 하고 안내 메시지를 보여줍니다.

### 준비

```bash
cd cloud_run
python -m pip install -r requirements.txt
python -m playwright install chromium
```

`.env` 파일(`.env.example`을 복사해서 만들면 됨)에 아래 값을 채웁니다:

```dotenv
# 파싱에 쓸 AI. claude 또는 gemini (기본값 claude). 언제든 이 값만 바꾸면 전환됨.
PARSER_ENGINE=claude

# PARSER_ENGINE=claude일 때 사용 (발급: https://console.anthropic.com/settings/keys)
ANTHROPIC_API_KEY=

# PARSER_ENGINE=gemini일 때 사용 (발급: https://aistudio.google.com/apikey)
GEMINI_API_KEY=

# 각 카드사 명세서를 열 때 쓰는 비밀번호 (생년월일 6자리 등)
BC_CARD_PASSWORD=
HYUNDAI_CARD_PASSWORD=
SAMSUNG_CARD_PASSWORD=
SHINHAN_CARD_PASSWORD=
```

> **비밀번호 환경변수 이름이 두 벌인 이유**: 로컬 테스트 스크립트(`.env`)는
> `BC_CARD_PASSWORD` / `HYUNDAI_CARD_PASSWORD` / `SAMSUNG_CARD_PASSWORD` /
> `SHINHAN_CARD_PASSWORD`를 쓰고, 운영용 Apps Script 스크립트 속성은
> `BC_PDF_PASSWORD` / `HYUNDAI_HTML_PASSWORD` / `SAMSUNG_HTML_PASSWORD` /
> `SHINHAN_PDF_PASSWORD`를 씁니다(4단계 표 참고). 값은 같고 이름만 다릅니다 — Cloud
> Run(`main.py`)은 비밀번호를 저장하지 않고 매 요청 payload로 받으므로 이 이름들과
> 무관합니다.

### 실행

```bash
# BC바로카드 (.env의 BC_CARD_PASSWORD 사용) — PDF 또는 엑셀(확장자로 자동 판별)
python test_local.py bc "실제BC명세서.pdf"
python test_local.py bc "실제BC명세서.xlsx"

# 신한카드 (.env의 SHINHAN_CARD_PASSWORD 사용)
python test_local.py shinhan "실제신한명세서.pdf"

# 현대카드 (.env의 HYUNDAI_CARD_PASSWORD 사용)
python test_local.py hyundai "실제현대명세서.html"

# 삼성카드 (.env의 SAMSUNG_CARD_PASSWORD 사용)
python test_local.py samsung "실제삼성명세서.html"

# 비밀번호를 .env 대신 그때그때 직접 넘기고 싶으면 세 번째 인자로 지정 가능
python test_local.py bc "실제BC명세서.pdf" "생년월일6자리"
```

`ANTHROPIC_API_KEY`(또는 `PARSER_ENGINE=gemini`일 땐 `GEMINI_API_KEY`)가 채워져
있으면 복호화 직후 자동으로 파싱까지 실행되어 아래처럼 거래 목록이 출력됩니다
(직접 확인 시 실제로 나오는 형식입니다):

```
거래 N건 파싱됨:
  2026-07-16    (주)이니시스-분당판교청소년수련관        27,000  문화/여가
  2026-07-17    메가엠지씨커피판교역로점                  2,000  카페/간식
  ...
```

이 단계에서 확인할 것: (1) 가맹점명이 깨지지 않고 원문 그대로 나오는지, (2) 금액
합계가 실제 명세서 총액과 맞는지, (3) `분류` 컬럼이 대체로 합리적인지. 분류가
자꾸 이상하게 나오면 `main.py`의 `CATEGORY_CHOICES`나 `_build_parse_instruction()`의
프롬프트를 다듬으세요.

### (선택) 실제 Gmail까지 연결해 통합 검증 — `test_gmail_fetch.py`

`test_local.py`가 "파일 하나"를 검증한다면, `test_gmail_fetch.py`는 **실제 메일함에서
Code.gs와 같은 조건으로 검색 → 첨부 추출 → 복호화 → 파싱 → 시트 기록**까지 로컬에서
그대로 돌려봅니다(운영 흐름의 리허설). Apps Script의 GmailApp 대신 IMAP + 앱
비밀번호를 쓰므로 `.env`에 아래 값이 추가로 필요합니다.

```dotenv
GMAIL_ADDRESS=명세서를_받는_Gmail주소
# 로그인 비밀번호 아님. 2단계 인증을 켠 뒤 https://myaccount.google.com/apppasswords 에서 발급하는 16자리
GMAIL_APP_PASSWORD=
# 시트 기록까지 확인하려면 아래 두 개도 필요 (3단계에서 만드는 값과 동일)
SHEETS_SERVICE_ACCOUNT_JSON=
SHEET_ID=
```

```bash
python test_gmail_fetch.py
```

> Windows에서는 `pip`/`playwright`를 명령어로 바로 치는 대신 위처럼
> `python -m pip ...` / `python -m playwright ...` 형태로 실행하는 것을 권장합니다.
> pip가 설치한 실행 파일이 PATH에 없어도 `-m`은 항상 동작합니다.

### Windows에서 `Failed building wheel for greenlet` 오류가 날 때

Playwright는 내부적으로 `greenlet`이라는 C 확장 패키지가 필요합니다. 아주 최신
Python(3.14 등)에는 아직 사전 컴파일된 배포판(wheel)이 없는 하위 버전의 greenlet이
설치되려고 하면, Windows에는 컴파일러가 없어서 이 오류가 납니다.

이 저장소의 `requirements.txt`는 이미 최신 Playwright(1.62.0, greenlet 3.5.5 이상 요구)로
맞춰뒀습니다 — 혹시 이전에 받은 파일을 쓰고 계시다면 최신 `requirements.txt`로 교체 후
아래처럼 다시 설치해보세요.

```bash
python -m pip install --upgrade pip
python -m pip cache purge
python -m pip install -r requirements.txt
```

그래도 같은 오류가 나면 아래 순서로 원인을 좁혀보세요.

**1) 정말 최신 wheel을 쓰려고 하는지 강제로 확인**

```bash
python -m pip cache purge
python -m pip install --only-binary=:all: -r requirements.txt
```

`--only-binary=:all:`을 붙이면 pip가 소스 빌드로 절대 넘어가지 않고, wheel이 없으면
"No matching distribution found" 같은 명확한 오류를 냅니다. 이 오류가 나면 2번으로,
설치가 되면 원래 오류는 이전 시도의 캐시나 예전 `requirements.txt`가 남아있었던 것입니다
(zip을 새로 풀었는지 다시 확인해주세요).

**2) 내 파이썬이 정확히 어떤 빌드인지 확인**

```bash
python -c "import sys, platform; print(sys.version); print(sys.implementation.cache_tag); print(platform.machine())"
```

`cp314`가 아니라 `cp314t`(free-threaded 빌드)이거나 `platform.machine()`이 `ARM64`로
나온다면, 그 조합용 wheel이 아직 없을 수 있습니다. 이 출력 결과를 알려주시면 정확히
확인해 드리겠습니다.

**3) 그래도 안 풀리면 — 가장 확실한 우회로**

[python.org](https://www.python.org/downloads/)에서 Python 3.12를 추가 설치한 뒤, 이
프로젝트 전용 가상환경을 3.12로 만드세요. 3.12는 사실상 모든 패키지의 wheel이 갖춰져
있어 이런 문제 자체가 생기지 않습니다(Cloud Run 컨테이너도 `python:3.12-slim` 기준).

```powershell
py -3.12 -m venv venv
venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

현대카드/삼성카드 테스트가 "복호화 결과가 비어 있습니다" 오류를 내면, `main.py`의
`decrypt_hyundai_html()` / `decrypt_samsung_html()`에서 버튼 선택자나 팝업/다이얼로그
감지 로직을 실제 동작에 맞게 수정해야 합니다(카드사가 페이지 구조를 바꾼 경우).
현대카드는 실패 시 `cloud_run/` 폴더에 `hyundai_debug_failure.png` 스크린샷이 자동으로
남으니 먼저 그걸 열어보세요. 화면을 직접 보면서 디버깅하려면:

```bash
# Windows PowerShell
$env:DEBUG_HEADED="1"; python test_local.py hyundai 실제파일.html 실제비밀번호

# macOS/Linux
DEBUG_HEADED=1 python3 test_local.py hyundai 실제파일.html 실제비밀번호
```

**알려진 이슈 — 현대카드 "element is not visible" 오류**: 이 사이트는 실제 비밀번호
입력칸(`#password`)을 `display:none`으로 숨겨두고, 화면에는 안내문구가 적힌 가짜 입력칸
(`name="p2_temp"`)만 보여줍니다. `main.py`는 이를 감안해 가짜 입력칸을 먼저 클릭한 뒤
JS로 값을 직접 주입하도록 되어 있습니다 — 그래도 같은 오류가 나면 카드사가 페이지
구조를 바꾼 것이니, `DEBUG_HEADED=1`로 화면을 보면서 실제 입력 요소를 다시 찾아
선택자를 맞춰야 합니다.

## 1단계 — 파싱에 쓸 AI 키 발급 (Claude 또는 Gemini)

`main.py`는 파싱 엔진으로 **Claude API와 Gemini API를 둘 다 지원**하며, `.env`(로컬) 또는
Cloud Run 환경변수의 `PARSER_ENGINE` 값(`claude` 또는 `gemini`, 기본값 `claude`)으로
언제든 전환할 수 있습니다. 둘 중 하나만 키를 발급받아도 되고, 나중에 바꾸고 싶으면
`PARSER_ENGINE`만 바꾸면 됩니다(코드 수정 불필요).

### 옵션 1: Claude API (기본값, 실제 파싱 테스트 완료)

1. https://console.anthropic.com/settings/keys 접속 (Anthropic 계정 필요, 결제 정보 등록 필요)
2. API 키 생성 → 아래 `ANTHROPIC_API_KEY`로 사용

> **참고**: Gemini와 달리 Claude API는 상시 무료 등급이 없는 종량제입니다(신규 계정에
> 소액 무료 크레딧이 있을 수 있음). 이 프로젝트 규모의 사용량(월 몇 건, 텍스트/이미지
> 소량)이면 실제 비용은 매우 낮을 것으로 예상되지만, 애초 목표였던 "비용 0원"과는
> 다르다는 점을 참고하세요. 정확한 가격: https://platform.claude.com/docs/en/about-claude/pricing

### 옵션 2: Gemini API (완전 무료 등급 있음, 원래 계획했던 방식)

1. https://aistudio.google.com/apikey 접속 (Google 계정 로그인, 카드 등록 불필요)
2. API 키 생성 → 아래 `GEMINI_API_KEY`로 사용, `.env`/Cloud Run에 `PARSER_ENGINE=gemini` 설정

> **참고**: "비용 0원" 목표를 그대로 지키고 싶다면 이 옵션을 쓰세요. 단
> `generativelanguage.googleapis.com`은 Claude Code(Cowork) 세션의 네트워크 허용목록에
> 없어 **세션 안에서는 Gemini 파싱 테스트가 403으로 막힙니다** — 사용자 본인 PC나
> Cloud Run에서는 정상 동작합니다.

## 2단계 — Google Cloud 프로젝트 + 서비스 계정

> **이 저장소는 이미 완료된 상태**: 프로젝트 `card-automation-506604` +
> 서비스 계정 `card-automation@card-automation-506604.iam.gserviceaccount.com`이
> 이미 만들어져 있고, 그 키 파일이 저장소 루트의
> `card-automation-506604-148c68e844bd.json`입니다(`test_sheets_write.py`가 이
> 파일로 시트 쓰기 테스트를 이미 통과함). 새로 만들 필요 없이 3단계로 넘어가면
> 됩니다. 아래는 처음부터 다시 만들 때(다른 계정/프로젝트로 복제할 때) 참고용입니다.

```bash
gcloud auth login
gcloud projects create card-automation-YOURNAME   # 프로젝트 ID는 전역 고유해야 함
gcloud config set project card-automation-YOURNAME
gcloud services enable run.googleapis.com sheets.googleapis.com

# 시트 쓰기 전용 서비스 계정 생성
gcloud iam service-accounts create sheet-writer --display-name "Sheet Writer"
gcloud iam service-accounts keys create sa-key.json \
  --iam-account sheet-writer@card-automation-YOURNAME.iam.gserviceaccount.com
```

`sa-key.json`(또는 콘솔에서 다운받은 서비스 계정 키 파일)에 적힌 `client_email` 값을 확인한 뒤,
대상 스프레드시트를 **편집자로 공유**하세요(시트 우측 상단 "공유" → 해당 이메일 추가).
이 단계를 빼먹으면 Sheets API가 403을 반환합니다.

> **로컬에 `gcloud` CLI가 없다면**: 설치([cloud.google.com/sdk/docs/install](https://cloud.google.com/sdk/docs/install))
> 하거나, [console.cloud.google.com](https://console.cloud.google.com) 우측 상단 터미널
> 아이콘(Cloud Shell)을 쓰면 설치 없이 브라우저에서 바로 `gcloud` 명령을 실행할 수 있습니다.

## 3단계 — Cloud Run 배포

> **주의**: `--set-env-vars`는 값을 콤마(`,`)로 구분하는데, 서비스 계정 JSON은 필드마다
> 콤마가 들어있어서 `--set-env-vars`에 JSON을 통째로 넣으면 `Bad syntax for dict arg`
> 오류가 납니다. JSON처럼 콤마·따옴표가 많은 값은 아래처럼 **`env.yaml` 파일로 넘겨야**
> 안전합니다.

```bash
cd cloud_run

# 앱-스크립트와 공유할 임의의 긴 문자열 생성
SHARED_SECRET=$(openssl rand -hex 24)
echo "SHARED_SECRET=$SHARED_SECRET"   # 이 값을 Apps Script 스크립트 속성에도 넣어야 함

cat > env.yaml <<EOF
SHARED_SECRET: "$SHARED_SECRET"
PARSER_ENGINE: "claude"
ANTHROPIC_API_KEY: "여기에_Claude_API_키"
SHEET_ID: "1b1Y50n_AlJ4fTFGxVlmzXCEEvpt8HEyqEHD2N5buFv0"
SHEETS_SERVICE_ACCOUNT_JSON: '$(cat ../card-automation-506604-148c68e844bd.json | tr -d '\n')'
EOF

gcloud run deploy card-automation \
  --source . \
  --region asia-northeast3 \
  --allow-unauthenticated \
  --memory 1Gi \
  --timeout 180 \
  --env-vars-file=env.yaml
```

- `SHEETS_SERVICE_ACCOUNT_JSON` 값은 **작은따옴표(`'...'`)로 감싸서** YAML이 내부의
  큰따옴표·콤마·콜론을 전부 리터럴 문자열로 처리하게 합니다(private key는 base64라
  작은따옴표가 나올 일이 없어 안전함).
- Gemini로 전환하고 싶다면 `env.yaml`의 `PARSER_ENGINE`을 `gemini`로, `ANTHROPIC_API_KEY`
  줄을 `GEMINI_API_KEY: "여기에_Gemini_API_키"`로 바꾼 뒤 다시 배포(또는
  `gcloud run services update card-automation --env-vars-file=env.yaml`)하면 됩니다.
- 배포가 끝나면 **`env.yaml`을 삭제하거나 최소한 git에 올리지 마세요** — API 키와 서비스
  계정 private key가 평문으로 들어있습니다(`cloud_run/.gitignore`에 이미 포함되어 있음).

> `--allow-unauthenticated`로 배포하되, `main.py`가 매 요청마다 `X-Shared-Secret` 헤더를
> 검사하므로 실질적으로는 이 값을 아는 Apps Script만 호출할 수 있습니다. 더 엄격하게
> 하고 싶다면 `--allow-unauthenticated`를 빼고 Apps Script에서 `ScriptApp.getIdentityToken()`으로
> 발급받은 ID 토큰을 `Authorization: Bearer` 헤더로 보내는 방식(IAM 인증)으로 바꿀 수 있습니다.

배포가 끝나면 출력되는 서비스 URL(`https://card-automation-xxxxx-an.a.run.app`)을 적어두세요.
`curl https://.../healthz`로 `{"status": "ok", "parser_engine": "claude"}` 같은 응답이
오는지 확인하면 배포 상태와 현재 파싱 엔진을 바로 알 수 있습니다.

## 4단계 — Apps Script 설정

1. https://script.google.com → 새 프로젝트
2. `Code.gs`, `appsscript.json` 내용을 그대로 붙여넣기 (프로젝트 설정에서 "appsscript.json
   매니페스트 파일을 편집기에 표시" 체크 필요)
3. 왼쪽 톱니바퀴(프로젝트 설정) → **스크립트 속성**에 아래 값 추가:

   | 속성 | 값 |
   |---|---|
   | `CLOUD_RUN_URL` | 3단계에서 받은 Cloud Run URL |
   | `SHARED_SECRET` | 3단계에서 생성한 값과 동일하게 |
   | `BC_PDF_PASSWORD` | BC바로카드 PDF/엑셀 비밀번호 |
   | `HYUNDAI_HTML_PASSWORD` | 현대카드 보안메일 비밀번호 |
   | `SAMSUNG_HTML_PASSWORD` | 삼성카드 보안메일 비밀번호 |
   | `SHINHAN_PDF_PASSWORD` | 신한카드 PDF 비밀번호 |
   | `PROCESSED_LABEL` | (선택) 기본값 `정산완료` — 없으면 자동 생성 |

4. 함수 목록에서 `createTimeTrigger`를 선택해 **1회 수동 실행** → Gmail 권한 승인 팝업이
   뜨면 허용 (이때 **4시간마다(하루 6회) 트리거**가 설치됩니다. 한 번에 여러 통이 쌓이면
   `runCheck_`가 5분 시간 예산에서 멈추고 남은 건 다음 실행으로 넘기므로, 자주 돌려야
   밀린 메일이 하루 안에 다 빠집니다. 하루 1회로 되돌리려면 `Code.gs`의
   `createTimeTrigger()` 안 `.everyHours(4)`를 `.everyDays(1).atHour(8)`로 바꾼 뒤
   `createTimeTrigger`를 재실행하면 됩니다 — 재실행 시 기존 트리거는 자동으로 지우고
   새로 설치합니다)
5. 실행 > 로그에서 정상 동작 확인, 또는 `checkNewStatements`를 수동 실행해 즉시 테스트

### Apps Script가 메일을 찾는 조건

카드사별로 Gmail 검색을 따로 돌립니다(`Code.gs`의 `runCheck_`):

| 카드 | 검색 쿼리 | 허용 첨부 확장자 |
|---|---|---|
| BC바로카드 | `in:inbox subject:BC바로카드 subject:명세서` | `.pdf` `.xlsx` `.xls` |
| 현대카드 | `in:inbox subject:현대카드 subject:명세서` | `.html` `.htm` |
| 삼성카드 | `in:inbox subject:삼성카드 subject:명세서` | `.html` `.htm` |
| 신한카드 | `in:inbox subject:신한카드 subject:명세서` | `.pdf` |

`in:inbox`라서 받은편지함에 있는 메일만 대상입니다(보관/스팸/휴지통 제외). 평소
실행(`checkNewStatements`)은 여기에 ` -label:정산완료`가 더 붙어 이미 처리한 메일을
건너뜁니다.

## 중복 방지 + 수동 강제 업데이트

카드사 명세서는 보통 한 달에 한 번만 오지만, 트리거는 4시간마다 실행됩니다. 정상적인
경우 `checkNewStatements()`가 "정산완료" 라벨이 없는 메일만 찾으므로 이미 처리한
명세서를 또 처리하지 않지만, 라벨이 무슨 이유로든 안 붙는 경우(부분 실패 등)를
대비해 Cloud Run(`main.py`) 쪽에도 안전장치를 넣었습니다: 시트에 **(카드명, 일자,
가맹점, 금액)이 이미 존재하는 거래는 자동으로 건너뜁니다.** 그래서 같은 메일이
두 번 처리되어도 시트에 중복 행이 쌓이지 않습니다(같은 요청 안의 중복도 함께 막습니다).

이 덕분에 **수동 강제 업데이트**도 안전하게 할 수 있습니다 — Apps Script 편집기에서
`forceReprocessAll` 함수를 선택해 수동 실행하면, 라벨이 이미 붙은 메일까지 포함해
최근 명세서를 전부 다시 처리합니다. 이미 시트에 기록된 거래는 자동으로 건너뛰고
누락되었던 거래만 새로 추가되므로, 아래와 같은 상황에서 쓰면 됩니다.

- 현대카드/삼성카드 뷰어 구조 변경 등으로 일부 첨부파일만 처리 실패했을 때, 원인을 고친 뒤 재확인
- `main.py`의 파싱/분류 프롬프트를 수정한 뒤 최근 명세서로 다시 검증하고 싶을 때
- 매일 트리거를 기다리지 않고 지금 바로 반영하고 싶을 때

## 시트에 기록되는 방식

- **월별 탭**: 첨부파일명에서 `YYYYMMDD`(8자리, 예: `BC바로카드_20260813.pdf`) 또는
  `YYYYMM`(6자리, 예: `hyundaicard_202606.html`) 형식의 날짜를 찾아 `YYYYMM` 탭에
  기록합니다. 탭이 없으면 헤더(`카드명 | 일자 | 가맹점 | 금액 | 분류`)와 함께 자동
  생성합니다. 파일명에 날짜가 없으면 `SHEET_TAB`(기본 `시트1`)로 폴백합니다.
- **카드사별 소계 + 전체 합계**: 명세서가 새로 처리될 때마다 순수 거래 행만 다시
  모아 카드사별로 그룹핑하고, 각 그룹 끝에 `<카드명> 소계` 행(연한 파랑), 맨 끝에
  `전체 합계` 행(금색 + 굵게)을 다시 씁니다. 여러 번 실행해도 항상 같은 결과가
  나오도록(멱등적) 기존 소계/합계 행은 먼저 걷어내고 재계산합니다. `현대카드`는
  전체 합계에서만 제외됩니다(`GRAND_TOTAL_EXCLUDED_CARDS`).
- **금액 서식**: 금액 열은 통화 서식(`#,##0"원"`), 나머지 열은 `TEXT` 서식으로
  고정해서 Sheets의 스마트 파싱이 일자를 날짜 일련번호로 바꾸지 못하게 합니다.
- **개인 사용내역 제외**: `EXCLUDED_MERCHANT_SUBSTRINGS`에 포함된 문자열이 가맹점명에
  들어간 거래는 시트에 기록하지 않습니다.

## 알아둘 점 / 남은 리스크

- **현대카드·삼성카드 자동화의 안정성**: 카드사가 보안메일 뷰어 페이지 구조를 바꾸면
  조용히 실패할 수 있습니다. `notifyFailure_()`가 실패 시 본인 메일로 알림을 보내도록
  이미 넣어뒀지만, 가끔 한 번씩 Apps Script 실행 로그를 확인하는 습관을 권장합니다.
  삼성카드는 특히 페이지네이션(더보기)을 끝까지 못 누르면 거래가 조용히 누락될 수
  있으니, 처음 몇 번은 시트 건수와 실제 명세서 건수를 대조하세요.
- **BC 엑셀 경로는 미검증**: `decrypt_bc_excel()`은 실제 BC바로카드 엑셀 샘플로 아직
  검증되지 않았습니다. 실제 파일을 받으면 `python test_local.py bc 실제파일.xlsx`로
  먼저 확인하고, 셀 레이아웃에 따라 프롬프트를 다듬어야 할 수 있습니다.
- **비밀번호 저장 위치**: 카드 비밀번호는 Apps Script 스크립트 속성과 Cloud Run
  환경변수, 두 곳에 평문으로 저장됩니다. 개인 계정 안에서만 접근 가능한 저장소이긴
  하지만, 더 엄격하게 하려면 Cloud Run 쪽 값은 Secret Manager로 옮길 수 있습니다.
- **분류 정확도**: `분류` 컬럼은 가맹점명만 보고 AI(현재 설정된 `PARSER_ENGINE`)가
  추론합니다. 처음 몇 번은 결과를 검토하고, 필요하면 `main.py`의 `CATEGORY_CHOICES`나
  프롬프트(`_build_parse_instruction`)를 다듬으세요.
- **AI 응답이 스키마를 벗어나는 경우**: 드물게 `transactions`가 객체 배열이 아니라
  문자열로 오는 것이 실측되어, `main.py`는 최대 3회 재시도 후 명확한 에러를 냅니다.
  이 에러가 나면 잠시 후 `forceReprocessAll`로 재시도하면 대부분 해결됩니다.
- **"Exceeded maximum execution time" (Apps Script)**: Apps Script는 실행 1회당 총
  시간 상한이 있습니다(무료 Gmail 약 6분). `/process` 왕복은 첨부 1건당 수십 초~수 분이
  걸려서, 한 번에 여러 통(특히 `forceReprocessAll` 직후)이면 상한에 걸립니다. `runCheck_`가
  **5분(`MAX_RUNTIME_MS`)이 지나면 남은 메일을 다음 실행으로 넘기고 멈추도록** 되어 있고,
  트리거도 4시간마다 돌므로 밀린 메일은 자동으로 빠집니다. `forceReprocessAll`은 수동
  함수라 5분에서 멈추면 로그를 확인하고 한두 번 더 실행하면 됩니다(멱등적이라 안전).
  Cloud Run 쪽은 `Dockerfile`의 `gunicorn --timeout`을 배포 시 `gcloud run --timeout`
  이상으로 맞춰 느린 한 건이 502로 죽지 않게 해두었습니다.
- **파싱 엔진(Claude/Gemini) 전환**: `main.py`에 두 엔진 코드가 모두 남아있으니,
  `.env`나 Cloud Run 환경변수의 `PARSER_ENGINE`만 `claude` ↔ `gemini`로 바꾸면 됩니다.
  코드를 다시 배포할 필요는 없고(Cloud Run은 `gcloud run services update --update-env-vars`),
  해당 엔진의 API 키만 준비되어 있으면 됩니다.
- **비용**: Cloud Run은 개인 사용량에서는 무료 한도 안입니다. `PARSER_ENGINE=gemini`면
  Gemini도 개인 사용량에서 무료 한도 안이라 "비용 0원" 목표를 그대로 지킬 수 있습니다.
  반면 `PARSER_ENGINE=claude`(기본값)는 **상시 무료 등급이 없는 종량제**입니다 — 이
  프로젝트 규모(월 몇 건)면 매우 저렴할 것으로 예상되지만 완전한 무료는 아닙니다.
  GCP 예산 알림(₩0 근처)과 별개로, https://console.anthropic.com 에서도 사용량/한도
  알림을 설정해두는 것을 권장합니다.
