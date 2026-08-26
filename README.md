# 카드 명세서 자동화 — 옵션 A 구현 (Google Apps Script + Cloud Run)

BC바로카드(암호 PDF) / 현대카드(보안 HTML) / 삼성카드(보안 HTML) / 신한카드(암호 PDF)
명세서를 Gmail에서 찾아 AI(Claude 또는 Gemini, 전환 가능)로 파싱하고, 아래 시트에
자동으로 행을 추가합니다.

- 대상 시트: https://docs.google.com/spreadsheets/d/1b1Y50n_AlJ4fTFGxVlmzXCEEvpt8HEyqEHD2N5buFv0/edit
- 헤더: `카드명 | 일자 | 가맹점 | 금액 | 분류` (한 시트에 카드명으로 구분해 계속 추가)

## 실제 파일로 검증 완료된 내용 (2026-08-25 기준)

두 카드사 실제 명세서 파일 + 실제 비밀번호로 복호화부터 AI 파싱까지 전 과정을
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
- **파싱 엔진**: 원래 Gemini API로 구현했으나, Gemini와 Claude API를 **둘 다 코드에
  유지**하고 `PARSER_ENGINE` 환경변수(`claude` 또는 `gemini`, 기본값 `claude`)로
  전환할 수 있게 만들어뒀습니다. 자세한 내용은 아래 "1단계" 참고.

## 폴더 구성

```
cloud_run/
  main.py             복호화(BC: 이미지 렌더링 / 현대카드: 텍스트 추출)
                      + AI 파싱(Claude 또는 Gemini, PARSER_ENGINE으로 전환)
                      + Sheets 기록을 담당하는 Cloud Run 서비스
  requirements.txt
  Dockerfile
  test_local.py       ← 배포 전 로컬 검증용 (아래 "테스트 방법" 참고)
  .env                ← 로컬 테스트용 실제 값 (git에 올리지 말 것, .gitignore에 포함됨)
  .env.example        ← .env 템플릿 (실제 값 없음, 이 파일은 공유해도 무방)
apps_script/          Gmail 감지 + Cloud Run 호출을 담당하는 Apps Script
  Code.gs
  appsscript.json
```

## 테스트 방법 (0단계 — 배포 전 로컬 검증, 강력 권장)

GCP 배포 전에 `test_local.py`로 복호화 + (키가 있으면) AI 파싱까지 먼저 확인합니다.
이 스크립트는 **BC바로카드와 현대카드를 각각 따로** 테스트하며, 두 단계로 동작합니다:

1. 항상 실행됨 — 파일을 실제 비밀번호로 복호화. BC카드는 각 페이지를
   `bc_page_1.png`, `bc_page_2.png`, ... 로 저장하고(직접 열어서 가맹점명이 잘
   보이는지 눈으로 확인 가능), 현대카드는 추출된 텍스트 앞부분을 화면에 출력합니다.
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

# 두 카드사 명세서를 열 때 쓰는 비밀번호 (생년월일 6자리 등)
BC_CARD_PASSWORD=
HYUNDAI_CARD_PASSWORD=
```

### 실행

```bash
# BC바로카드 (.env의 BC_CARD_PASSWORD 사용)
python test_local.py bc "실제BC명세서.pdf"

# 현대카드 (.env의 HYUNDAI_CARD_PASSWORD 사용)
python test_local.py hyundai "실제현대명세서.html"

# 비밀번호를 .env 대신 그때그때 직접 넘기고 싶으면 세 번째 인자로 지정 가능
python test_local.py bc "실제BC명세서.pdf" "생년월일6자리"
python test_local.py hyundai "실제현대명세서.html" "비밀번호"
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
있어 이런 문제 자체가 생기지 않습니다.

```powershell
py -3.12 -m venv venv
venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

현대카드 테스트가 "복호화 결과가 비어 있습니다" 오류를 내면, `main.py`의
`decrypt_hyundai_html()`에서 버튼 선택자(`input[type="image"].w_section`)나 팝업 감지
로직을 실제 동작에 맞게 수정해야 합니다(카드사가 페이지 구조를 바꾼 경우). 실패 시
`cloud_run/` 폴더에 `hyundai_debug_failure.png` 스크린샷이 자동으로 남으니 먼저 그걸
열어보세요. 화면을 직접 보면서 디버깅하려면:

```bash
# Windows PowerShell
$env:DEBUG_HEADED="1"; python test_local.py hyundai 실제파일.html 실제비밀번호

# macOS/Linux
DEBUG_HEADED=1 python3 test_local.py hyundai 실제파일.html 실제비밀번호
```

**알려진 이슈 — "element is not visible" 오류**: 이 사이트는 실제 비밀번호 입력칸
(`#password`)을 `display:none`으로 숨겨두고, 화면에는 안내문구가 적힌 가짜 입력칸
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

> **참고**: "비용 0원" 목표를 그대로 지키고 싶다면 이 옵션을 쓰세요.

## 2단계 — Google Cloud 프로젝트 + 서비스 계정

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

`sa-key.json`에 적힌 `client_email` 값을 확인한 뒤, 대상 스프레드시트를 **편집자로 공유**하세요
(시트 우측 상단 "공유" → 해당 이메일 추가). 이 단계를 빼먹으면 Sheets API가 403을 반환합니다.

## 3단계 — Cloud Run 배포

```bash
cd cloud_run

# 앱-스크립트와 공유할 임의의 긴 문자열 생성
SHARED_SECRET=$(openssl rand -hex 24)
echo "SHARED_SECRET=$SHARED_SECRET"   # 이 값을 Apps Script 스크립트 속성에도 넣어야 함

gcloud run deploy card-automation \
  --source . \
  --region asia-northeast3 \
  --allow-unauthenticated \
  --memory 1Gi \
  --timeout 180 \
  --set-env-vars "SHARED_SECRET=$SHARED_SECRET" \
  --set-env-vars "PARSER_ENGINE=claude" \
  --set-env-vars "ANTHROPIC_API_KEY=여기에_Claude_API_키" \
  --set-env-vars "SHEET_ID=1b1Y50n_AlJ4fTFGxVlmzXCEEvpt8HEyqEHD2N5buFv0" \
  --set-env-vars "SHEETS_SERVICE_ACCOUNT_JSON=$(cat sa-key.json | tr -d '\n')"

# Gemini로 전환하고 싶다면 위 두 줄 대신 아래처럼 배포(또는 `gcloud run services update`로 변경):
#   --set-env-vars "PARSER_ENGINE=gemini" \
#   --set-env-vars "GEMINI_API_KEY=여기에_Gemini_API_키" \
```

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
   | `BC_PDF_PASSWORD` | BC바로카드 PDF 비밀번호 |
   | `HYUNDAI_HTML_PASSWORD` | 현대카드 보안메일 비밀번호 |
   | `SAMSUNG_HTML_PASSWORD` | 삼성카드 보안메일 비밀번호 |
   | `SHINHAN_PDF_PASSWORD` | 신한카드 PDF 비밀번호 |

4. 함수 목록에서 `createTimeTrigger`를 선택해 **1회 수동 실행** → Gmail 권한 승인 팝업이
   뜨면 허용 (이때 10분 주기 트리거가 설치됩니다)
5. 실행 > 로그에서 정상 동작 확인, 또는 `checkNewStatements`를 수동 실행해 즉시 테스트

## 알아둘 점 / 남은 리스크

- **현대카드 자동화의 안정성**: 카드사가 보안메일 뷰어 페이지 구조를 바꾸면 조용히
  실패할 수 있습니다. `notifyFailure_()`가 실패 시 본인 메일로 알림을 보내도록 이미
  넣어뒀지만, 가끔 한 번씩 Apps Script 실행 로그를 확인하는 습관을 권장합니다.
- **비밀번호 저장 위치**: 두 카드 비밀번호는 Apps Script 스크립트 속성과 Cloud Run
  환경변수, 두 곳에 평문으로 저장됩니다. 개인 계정 안에서만 접근 가능한 저장소이긴
  하지만, 더 엄격하게 하려면 Cloud Run 쪽 값은 Secret Manager로 옮길 수 있습니다.
- **분류 정확도**: `분류` 컬럼은 가맹점명만 보고 AI(현재 설정된 `PARSER_ENGINE`)가
  추론합니다. 처음 몇 번은 결과를 검토하고, 필요하면 `main.py`의 `CATEGORY_CHOICES`나
  프롬프트(`_build_parse_instruction`)를 다듬으세요.
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
