"""
카드 명세서 자동 처리 서비스 (Cloud Run)
--------------------------------------------------
Apps Script가 Gmail에서 찾은 첨부파일(base64)을 이 서비스로 보내면:
  1. BC바로카드  -> 비밀번호로 PDF 복호화 후 각 페이지를 이미지로 렌더링
                    (텍스트 레이어가 복사방지용으로 스크램블되어 있어 텍스트
                    추출 대신 이미지를 비전 모델 입력으로 사용)
  2. 현대카드    -> Playwright(헤드리스 브라우저)로 보안 HTML을 열고
                    비밀번호를 입력해 복호화된 내용을 텍스트로 추출
  2-B. 삼성카드  -> Playwright로 보안 HTML을 열고 비밀번호 입력 후 제출.
                    현대카드와 달리 진짜 표시형 입력칸을 쓰고, "더보기" 페이지네이션이
                    실제 데이터 누락을 유발하므로 전체 페이지를 다 볼 때까지 클릭함
                    (2026-08-25 실제 파일로 확인).
  2-C. 신한카드  -> BC바로카드와 동일하게 암호 PDF + 폰트 스크램블(안티카피)이 확인되어
                    같은 이미지 렌더링 방식을 재사용함(2026-08-26 실제 파일로 확인).
  3. AI로 (이미지 또는 텍스트를) 거래내역 JSON으로 파싱
     — Gemini API와 Claude API 둘 다 지원하며, 환경변수 PARSER_ENGINE으로 선택
       ("gemini" 또는 "claude", 기본값 "claude"). 둘 다 코드에 남겨뒀으니 필요하면
       언제든 다른 쪽으로 바꿀 수 있음.
  4. Google Sheets API로 기존 시트(카드명·일자·가맹점·금액·분류)에 행 추가
     — 단, (카드명, 일자, 가맹점, 금액)이 이미 시트에 있는 거래는 건너뜀(중복 방지).
       라벨이 붙지 않아 같은 메일이 다시 들어오거나, 수동 강제 재처리를 해도
       시트에는 중복 행이 쌓이지 않음.

2026-08-25 로컬 테스트로 확인된 사실 (실제 파일 + 실제 비밀번호로 검증):
  - BC바로카드 PDF: pypdf로 복호화 자체는 성공하지만, pdfplumber로 텍스트를
    추출하면 가맹점명 부분이 심하게 깨짐(예: "ŸœÃÒ ƒ“´¬Œ_ˆ∫"). 페이지를
    이미지로 렌더링해서 육안으로 보면 가맹점명이 전부 또렷하게 보이므로, PDF
    자체 문제가 아니라 카드사가 복사/추출 방지 목적으로 폰트 인코딩을 페이지당
    수십 개의 서브셋 폰트로 잘게 쪼개 스크램블해놓은 것으로 확인됨. 그래서
    텍스트 추출을 포기하고 페이지 이미지를 그대로 파싱 모델에 넣는 방식으로 변경함.
  - 현대카드 HTML: 비밀번호 입력 후 "조회 확인" 버튼을 누르면 팝업(새 창) 없이
    같은 페이지 안에서 내용이 바로 교체됨을 확인함(이전까지 미확인 상태였음).
    이전 코드는 팝업을 기다렸다가 없으면 같은 버튼을 한 번 더 클릭하는 방식이었는데,
    첫 클릭에서 이미 DOM이 교체되어 버튼이 사라지므로 두 번째 클릭이 항상 타임아웃
    으로 실패하는 버그가 있었음. 클릭은 1회만 하고, 그 클릭이 팝업을 띄웠는지
    여부만 짧게 확인하는 방식으로 수정함.
  - Gemini API(generativelanguage.googleapis.com)는 Claude(Cowork) 세션의 네트워크
    허용목록에 없어 세션 안에서는 호출이 403으로 막힘(로컬 테스트 불가, 사용자 PC에서는
    정상 동작함). api.anthropic.com은 세션 허용목록에 포함되어 있어 세션 안에서도
    테스트 가능. 이 차이 때문에 기본 엔진을 Claude API로 바꿨지만, 사용자 요청에 따라
    Gemini 쪽 코드/설정도 전부 남겨뒀고 PARSER_ENGINE 환경변수로 언제든 되돌릴 수 있음.
    Claude API는 Gemini와 달리 상시 무료 등급이 없는 종량제라는 점 참고(프로젝트 목표인
    "비용 0원"과는 다름 — 이 정도 사용량이면 실제 비용은 낮을 것으로 예상됨. 정확한
    가격은 https://platform.claude.com/docs/en/about-claude/pricing 참고).
"""

import base64
import io
import json
import os
import re
import tempfile
import traceback
from datetime import date, timedelta

from flask import Flask, request, jsonify

import pypdf
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# ---------------------------------------------------------------------------
# 환경변수 (Cloud Run 배포 시 --set-env-vars 또는 Secret Manager로 주입)
# 주의: 모듈 로드 시점이 아니라 실제로 필요한 함수 안에서 읽습니다 —
#       test_local.py로 복호화 로직만 단독 테스트할 때 나머지 값이
#       없어도 되도록 하기 위함입니다.
# ---------------------------------------------------------------------------
# 월별 탭(YYYYMM) 자동 생성/기입이 기본 동작입니다 — 첨부파일명에서 8자리 날짜
# (YYYYMMDD)를 찾아 앞 6자리를 탭 이름으로 씁니다(test_sheets_write.py로 검증됨,
# 2026-08-25 사용자 확정 요청). 파일명에 날짜가 없는 등 예외 상황에서만
# SHEET_TAB(기본값 "시트1")으로 폴백합니다.
SHEET_TAB = os.environ.get("SHEET_TAB", "시트1")

# 파싱에 어떤 AI를 쓸지 선택. "claude"(기본값) 또는 "gemini".
# 둘 다 코드가 남아있으니 이 값만 바꾸면(.env 또는 Cloud Run 환경변수) 언제든 전환 가능.
PARSER_ENGINE = os.environ.get("PARSER_ENGINE", "claude").strip().lower()

# 2026-08 기준 최신 모델 ID들. 필요하면 환경변수로 덮어쓸 수 있음.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
# gemini-2.5-flash는 신규 사용자에게 단종됨(2026-08-27 실제 API 호출로 확인 —
# 404 NOT_FOUND, 응답에서 gemini-3.6-flash로 교체하라고 명시함).
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

SHEET_HEADERS = ["카드명", "일자", "가맹점", "금액", "분류"]
CATEGORY_CHOICES = ["식비", "카페/간식", "교통", "쇼핑", "통신", "의료", "문화/여가", "교육", "주거/공과금", "기타"]

# AI가 매긴 분류를 가맹점명 기준으로 후보정하는 규칙(위에서부터 첫 매칭 1개만 적용).
# 가맹점명에 keyword 중 하나라도 포함되면 분류를 category로 강제 교체한다(사용자 요청).
CATEGORY_OVERRIDE_RULES = [
    # 편의점류 상호 → 카페/간식
    (["씨유", "이마트24", "GS25", "KFC", "지에스 더프레시", "노랑냉장고"], "카페/간식"),
    # 학원/교육 관련 → 교육
    (["교육", "학원", "스터디", "더지니어스아라"], "교육"),
    # 대형마트/식자재 → 식비 (편의점 규칙보다 아래 — "(주)이마트"는 "이마트24"와 다름)
    (["홈플러스", "하나로마트", "트레이더스", "농협성남", "(주)이마트", "롯데마트"], "식비"),
]

# 가맹점명에 아래 문자열이 포함된 거래는 시트에 아예 기록하지 않음(사용자 요청).
EXCLUDED_MERCHANT_SUBSTRINGS = [
    "후불무승인_", "KT통신요금자동납부-991613", "KCP-성남시청",
    "KT 통신요금 기본할인", "KT 통신요금 추가할인",
]

# 전체 합계(GRAND_TOTAL_LABEL)에서 제외할 카드명. 카드사별 소계에는 그대로 반영됨.
GRAND_TOTAL_EXCLUDED_CARDS = {"현대카드"}

# 분류별 지출 비중 원형차트에서 제외할 카드명(사용자 요청 — 현대카드 제외).
CHART_EXCLUDED_CARDS = {"현대카드"}

TRANSACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "일자": {"type": "string", "description": "YYYY-MM-DD 형식"},
                    "가맹점": {"type": "string"},
                    "금액": {"type": "integer", "description": "원 단위, 취소/환불은 음수"},
                    "분류": {"type": "string", "enum": CATEGORY_CHOICES},
                },
                "required": ["일자", "가맹점", "금액", "분류"],
            },
        }
    },
    "required": ["transactions"],
}


def _build_parse_instruction(card_name: str) -> str:
    return f"""아래는 {card_name} 신용카드 명세서입니다.
이 안에서 실제 결제 거래 내역만 골라 JSON으로 반환하세요.

규칙:
- 합계/소계/카드번호/유효기간 등 거래가 아닌 줄은 제외합니다.
- 취소·환불 거래는 금액을 음수로 표기합니다.
- 가맹점명은 원문 표기를 그대로 사용합니다 (임의로 축약하지 않음).
- 분류는 반드시 다음 중 하나로 선택합니다: {", ".join(CATEGORY_CHOICES)}
"""


# ---------------------------------------------------------------------------
# 1. BC바로카드 / 신한카드 — 암호 PDF 복호화 → 페이지 이미지 렌더링
# ---------------------------------------------------------------------------
def _decrypt_pdf_to_page_images(pdf_bytes: bytes, password: str) -> list[bytes]:
    """비밀번호로 PDF를 복호화한 뒤 각 페이지를 PNG 이미지 바이트로 렌더링해서 반환합니다.

    주의: BC바로카드/신한카드 PDF는 둘 다 복사/추출 방지를 위해 폰트 인코딩이
    스크램블되어 있어 pdfplumber 같은 텍스트 레이어 추출 방식으로는 가맹점명을
    절대 정상적으로 뽑을 수 없습니다(둘 다 실제 확인됨). 그래서 텍스트 대신 페이지
    이미지를 그대로 반환하고, 호출하는 쪽(parse_transactions)에서 비전 모델
    입력으로 사용합니다.
    """
    import pypdfium2 as pdfium

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted:
        result = reader.decrypt(password)
        if result == 0:
            raise ValueError("PDF 비밀번호가 올바르지 않습니다.")

    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)

    doc = pdfium.PdfDocument(buf.read())
    images: list[bytes] = []
    for page in doc:
        # scale=2.5 정도면 작은 글씨(가맹점명 등)도 비전 모델이 읽기에 충분히 선명함
        bitmap = page.render(scale=2.5)
        pil_image = bitmap.to_pil()
        img_buf = io.BytesIO()
        pil_image.save(img_buf, format="PNG")
        images.append(img_buf.getvalue())

    if not images:
        raise ValueError("PDF에서 렌더링된 페이지가 없습니다.")
    return images


def decrypt_bc_pdf(pdf_bytes: bytes, password: str) -> list[bytes]:
    return _decrypt_pdf_to_page_images(pdf_bytes, password)


# ---------------------------------------------------------------------------
# 1-B. BC바로카드 — 암호 엑셀(xlsx/xls) 복호화 → 텍스트 추출
# ---------------------------------------------------------------------------
def decrypt_bc_excel(file_bytes: bytes, password: str) -> str:
    """BC바로카드 명세서가 PDF 대신 엑셀로 오는 경우. PDF와 마찬가지로 생년월일 등
    비밀번호로 암호화된 MS Office 파일인 것이 일반적이라 msoffcrypto-tool로 먼저
    복호화를 시도하고, 암호화되어 있지 않으면 원본을 그대로 읽습니다.

    PDF와 달리 엑셀 셀 값은 폰트 스크램블(복사방지) 문제가 없으므로 이미지 렌더링
    없이 셀 값을 그대로 텍스트로 뽑아 AI 텍스트 파싱 경로(raw_text)로 넘깁니다.

    주의: 아직 실제 BC바로카드 엑셀 샘플 파일로 검증되지 않았습니다. 실제 파일을
    받으면 `python test_local.py bc 실제파일.xlsx`로 먼저 로컬 검증을 권장합니다
    (README "테스트 방법" 참고). 신형(.xlsx, OOXML)과 구형(.xls, BIFF8) 포맷을 모두
    시도하도록 만들어뒀지만, 실제 파일의 셀 레이아웃(표 구조)에 따라
    `_build_parse_instruction()`의 프롬프트를 다듬어야 할 수 있습니다.
    """
    import msoffcrypto

    src = io.BytesIO(file_bytes)
    decrypted = io.BytesIO()

    try:
        office_file = msoffcrypto.OfficeFile(src)
        is_encrypted = office_file.is_encrypted()
    except Exception:
        # msoffcrypto가 인식 못하는 포맷이면 암호화되지 않은 파일로 간주하고 원본 사용
        is_encrypted = False

    if is_encrypted:
        try:
            office_file.load_key(password=password)
            office_file.decrypt(decrypted)
        except Exception as exc:
            raise ValueError(f"엑셀 비밀번호가 올바르지 않거나 복호화에 실패했습니다: {exc}") from exc
        decrypted.seek(0)
    else:
        decrypted = io.BytesIO(file_bytes)

    lines: list[str] = []
    try:
        import openpyxl

        wb = openpyxl.load_workbook(decrypted, data_only=True)
        for ws in wb.worksheets:
            lines.append(f"[시트: {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                if all(cell is None for cell in row):
                    continue
                lines.append("\t".join("" if c is None else str(c) for c in row))
    except Exception:
        # 신형(.xlsx) 파서로 못 열면 구형(.xls, BIFF8) 포맷일 수 있으니 xlrd로 재시도
        decrypted.seek(0)
        import xlrd

        wb = xlrd.open_workbook(file_contents=decrypted.read())
        for sheet in wb.sheets():
            lines.append(f"[시트: {sheet.name}]")
            for row_idx in range(sheet.nrows):
                row = sheet.row_values(row_idx)
                if all(c == "" for c in row):
                    continue
                lines.append("\t".join(str(c) for c in row))

    text = "\n".join(lines)
    if not text.strip():
        raise ValueError("엑셀에서 읽은 내용이 비어 있습니다.")
    return text


def decrypt_shinhan_pdf(pdf_bytes: bytes, password: str) -> list[bytes]:
    """신한카드 명세서 PDF. BC바로카드와 동일하게 폰트 스크램블이 확인되어(2026-08-26)
    같은 이미지 렌더링 방식을 그대로 사용합니다."""
    return _decrypt_pdf_to_page_images(pdf_bytes, password)


# ---------------------------------------------------------------------------
# 2. 현대카드 — 보안 HTML 복호화 (Playwright)
# ---------------------------------------------------------------------------
def decrypt_hyundai_html(html_bytes: bytes, password: str) -> str:
    from playwright.sync_api import sync_playwright

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        f.write(html_bytes)
        html_path = f.name

    # 로컬에서 눈으로 보면서 디버깅하고 싶으면 환경변수 DEBUG_HEADED=1 로 실행
    headless = os.environ.get("DEBUG_HEADED") != "1"

    extracted_text = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"file://{html_path}")

        # 실제 비밀번호 입력칸(#password)은 CSS로 숨겨져 있고(style="display:none"),
        # 화면에는 안내문구("비밀번호(6자리)")가 적힌 가짜 입력칸(name="p2_temp")만 보입니다.
        # Playwright의 fill()은 화면에 보이지 않는 요소를 거부하므로:
        #   1) 먼저 가짜 입력칸을 클릭해 카드사 JS(onfocus="changeText(this)")가
        #      자체적으로 화면 상태를 전환하도록 유도하고,
        #   2) 실제 값은 JS로 직접 주입해 visibility 체크를 우회합니다.
        try:
            page.click('input[name="p2_temp"]', timeout=3000)
        except Exception:
            pass

        page.eval_on_selector(
            "#password",
            """(el, val) => {
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('keyup', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            password,
        )

        # 실제 비밀번호로 확인한 결과, "조회 확인" 버튼을 누르면 팝업(새 창) 없이
        # 같은 페이지 안에서 내용이 바로 교체됩니다. 다만 다른 계정/카드 조합에서
        # 팝업으로 뜨는 경우도 있을 수 있으니 대비는 해두되, 클릭은 반드시 1회만
        # 해야 합니다(이전 코드는 팝업을 기다렸다가 실패하면 같은 버튼을 한 번 더
        # 클릭했는데, 첫 클릭에서 이미 버튼이 DOM에서 사라져 두 번째 클릭이 항상
        # 30초 타임아웃으로 실패하는 버그가 있었습니다).
        popup = None
        try:
            with context.expect_page(timeout=5000) as new_page_info:
                page.click('input[type="image"].w_section')  # "조회 확인" 버튼 (클릭은 이 1회뿐)
            popup = new_page_info.value
        except Exception:
            # 5초 안에 새 창이 안 뜨면 같은 페이지 안에서 바뀐 것 (실제 관찰된 정상 케이스)
            popup = None

        if popup is not None:
            popup.wait_for_load_state("networkidle", timeout=8000)
            target_page = popup
        else:
            page.wait_for_timeout(3000)
            target_page = page

        # 실제로 브라우저에서 열어보면 "결제상세내역 더 보기"를 눌러야 전체 거래내역이
        # 화면에 펼쳐짐(사용자가 직접 확인해서 알려준 내용). 이 카드사는 접기 방식이
        # CSS 클리핑이라 Playwright의 inner_text()가 클릭 전에도 이미 전체 텍스트를
        # 반환하는 것으로 확인됐지만(클릭 전/후 텍스트 길이 동일), 카드사가 구현을
        # display:none 방식으로 바꾸는 경우에 대비해 방어적으로 이 토글을 전부 펼치고
        # 나서 추출합니다.
        try:
            more_links = target_page.locator("a.detailView")
            more_count = more_links.count()
            for i in range(more_count):
                try:
                    more_links.nth(i).click(timeout=3000)
                except Exception:
                    pass
            if more_count > 0:
                target_page.wait_for_timeout(500)
        except Exception:
            pass

        extracted_text = target_page.inner_text("body")

        if not extracted_text.strip():
            # 디버깅을 돕기 위해 실패 시점 스크린샷을 남깁니다.
            page.screenshot(path="hyundai_debug_failure.png", full_page=True)

        browser.close()

    os.unlink(html_path)

    if not extracted_text.strip():
        raise ValueError(
            "현대카드 HTML 복호화 결과가 비어 있습니다 — "
            "hyundai_debug_failure.png 스크린샷과 DEBUG_HEADED=1 실행으로 화면을 직접 확인하세요."
        )
    return extracted_text


# ---------------------------------------------------------------------------
# 2-B. 삼성카드 — 보안 HTML 복호화 (Playwright)
# ---------------------------------------------------------------------------
def decrypt_samsung_html(html_bytes: bytes, password: str) -> str:
    """삼성카드 명세서 HTML은 현대카드와 달리 진짜 표시형 비밀번호 입력칸(#password,
    type="password")과 제출 버튼(#confirm)을 그대로 씀 — 위장 입력칸이 없음(실제 확인됨).

    2026-08-25 실제 파일로 확인된 사실:
    - 비밀번호가 틀리면 JS alert("비밀번호 입력이 잘못되었습니다.")가 뜸 — 이 다이얼로그를
      감지해서 실패로 처리함.
    - 거래 목록에 실제 페이지네이션이 있음("더보기 (현재페이지 X/전체페이지 Y)") —
      현대카드의 "더보기"와 달리 이건 CSS 클리핑이 아니라 진짜 데이터 누락이 발생함
      (총 17건인데 첫 클릭 전엔 10건만 DOM에 있었음, 실측 확인). 그래서 전체페이지를
      다 볼 때까지 반복 클릭이 반드시 필요함.
    - 복호화 과정에서 samsungcard.com 쪽으로 실제 네트워크 요청이 발생함(정적 리소스
      CORS 에러가 일부 나지만 본문 복호화 자체는 성공함) — Cloud Run처럼 일반 인터넷
      접근이 되는 환경이면 문제없음.
    """
    from playwright.sync_api import sync_playwright

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        f.write(html_bytes)
        html_path = f.name

    headless = os.environ.get("DEBUG_HEADED") != "1"

    dialog_messages: list[str] = []
    extracted_text = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=["--no-sandbox"])
        page = browser.new_page()
        page.on("dialog", lambda d: (dialog_messages.append(d.message), d.accept()))
        page.goto(f"file://{html_path}")

        page.fill("#password", password)
        page.click("#confirm")
        page.wait_for_timeout(6000)

        if dialog_messages:
            browser.close()
            os.unlink(html_path)
            raise ValueError(f"삼성카드 HTML 복호화 실패: {dialog_messages[0]}")

        # "더보기 (현재페이지 X/전체페이지 Y)" 페이지네이션을 끝까지 클릭.
        for _ in range(20):
            more = page.locator("text=더보기")
            if more.count() == 0:
                break
            pagination_text = more.first.inner_text()
            m = re.search(r"현재페이지\s*(\d+)\s*/\s*전체페이지\s*(\d+)", pagination_text)
            if m and m.group(1) == m.group(2):
                break
            try:
                more.first.click(timeout=3000)
            except Exception:
                break
            page.wait_for_timeout(2000)

        extracted_text = page.inner_text("body")
        browser.close()

    os.unlink(html_path)

    if not extracted_text.strip():
        raise ValueError("삼성카드 HTML 복호화 결과가 비어 있습니다.")
    return extracted_text


# ---------------------------------------------------------------------------
# 3-A. Claude API로 거래내역 파싱
# ---------------------------------------------------------------------------
def parse_transactions_with_claude(
    card_name: str,
    raw_text: str | None = None,
    page_images: list[bytes] | None = None,
) -> list[dict]:
    """Claude의 tool use(도구 사용) 기능으로 구조화된 JSON 출력을 강제합니다 —
    "record_transactions"라는 가상의 도구를 정의하고 그 도구를 무조건 호출하도록
    tool_choice를 지정하면, 모델이 자유 텍스트 대신 TRANSACTION_SCHEMA에 맞는
    JSON(도구 입력값)만 반환합니다.
    """
    import anthropic

    if not raw_text and not page_images:
        raise ValueError("raw_text 또는 page_images 중 하나는 반드시 있어야 합니다.")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    instruction = _build_parse_instruction(card_name).replace(
        "JSON으로 반환하세요.", "record_transactions 도구를 호출해서 반환하세요."
    )

    content: list[dict] = [{"type": "text", "text": instruction}]
    if page_images:
        # BC바로카드: 텍스트 레이어가 스크램블되어 있어 페이지 이미지를 그대로 전달
        for img_bytes in page_images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(img_bytes).decode("utf-8"),
                    },
                }
            )
    else:
        content[0]["text"] += f"\n\n원문:\n---\n{raw_text[:15000]}\n---\n"

    tool = {
        "name": "record_transactions",
        "description": "명세서에서 추출한 신용카드 거래 내역을 기록합니다.",
        "input_schema": TRANSACTION_SCHEMA,
    }

    # 참고: temperature로 응답을 더 결정적으로 만들어보려 했으나, 현재 설치된
    # anthropic SDK(1.0.0)의 messages.create()가 temperature 파라미터 자체를 받지
    # 않음(실제 확인됨 — 전달 시 "unexpected keyword argument" TypeError 발생).
    #
    # 대신 재시도로 대응함: 아주 드물게(2026-08-27 실제 관찰, 4번 중 1번꼴)
    # record_transactions의 "transactions" 입력값이 스키마(객체 배열)를 벗어나
    # JSON 전체가 문자열 하나로 오는 경우가 있음 — 그대로 두면 호출부에서
    # "string indices must be integers, not 'str'"처럼 원인을 알기 어려운 에러로
    # 나타남. 같은 요청을 다시 보내면 대부분 정상으로 돌아오는 것으로 확인되어
    # 최대 3회까지 자동 재시도한다.
    max_attempts = 3
    last_bad_value = None
    for attempt in range(1, max_attempts + 1):
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_transactions"},
            messages=[{"role": "user", "content": content}],
        )

        transactions = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_transactions":
                transactions = block.input.get("transactions", [])
                break

        if isinstance(transactions, list) and all(isinstance(t, dict) for t in transactions):
            return transactions

        last_bad_value = transactions
        # 다음 시도로 넘어감(마지막 시도였으면 루프 종료 후 아래에서 에러 발생)

    raise ValueError(
        f"Claude가 {max_attempts}회 시도에도 계속 스키마를 벗어난 응답을 반환했습니다 "
        f"(마지막 값 타입: {type(last_bad_value).__name__}, 앞부분: {str(last_bad_value)[:200]!r}). "
        "일시적인 문제일 가능성이 높으니 잠시 후 다시 시도해보세요."
    )


# ---------------------------------------------------------------------------
# 3-B. Gemini API로 거래내역 파싱
# ---------------------------------------------------------------------------
def parse_transactions_with_gemini(
    card_name: str,
    raw_text: str | None = None,
    page_images: list[bytes] | None = None,
) -> list[dict]:
    """raw_text(현대카드) 또는 page_images(BC바로카드) 중 하나를 받아 거래내역을 파싱합니다.

    참고: generativelanguage.googleapis.com은 Claude(Cowork) 세션의 네트워크
    허용목록에 없어 세션 안에서는 이 함수 호출이 403으로 실패합니다(사용자 자신의
    PC에서 실행하면 정상 동작). 세션 안에서 바로 테스트하고 싶으면 PARSER_ENGINE을
    "claude"로 두세요.
    """
    from google import genai
    from google.genai import types as genai_types

    if not raw_text and not page_images:
        raise ValueError("raw_text 또는 page_images 중 하나는 반드시 있어야 합니다.")

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    instruction = _build_parse_instruction(card_name)

    if page_images:
        # BC바로카드: 텍스트 레이어가 스크램블되어 있어 페이지 이미지를 그대로 전달
        contents = [instruction]
        for img_bytes in page_images:
            contents.append(
                genai_types.Part.from_bytes(data=img_bytes, mime_type="image/png")
            )
    else:
        contents = [f"{instruction}\n\n원문:\n---\n{raw_text[:15000]}\n---\n"]

    # Claude 쪽에서 실제로 관찰된 "transactions가 배열 대신 문자열로 오는" 문제에
    # 대한 방어로 여기도 동일하게 재시도 + 형태 검증을 적용함(Gemini는 서버가
    # response_schema를 강제하므로 발생 확률은 낮지만, 만일을 대비).
    max_attempts = 3
    last_bad_value = None
    for attempt in range(1, max_attempts + 1):
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                # 주의: 필드명이 response_json_schema가 아니라 response_schema임
                # (2026-08-27 실제 테스트로 확인 — google-genai==1.15.0 기준
                # response_json_schema는 존재하지 않는 필드라 즉시 pydantic
                # ValidationError가 남). response_schema는 우리가 쓰는 표준 JSON
                # Schema 형태(dict)를 그대로 받아들이는 것까지 로컬 검증함.
                response_schema=TRANSACTION_SCHEMA,
                temperature=0,  # 표를 그대로 옮기는 작업이라 창의성이 불필요하고,
                # 낮출수록 스키마를 벗어난 응답 확률이 줄어듦.
            ),
        )
        data = json.loads(response.text)
        transactions = data.get("transactions", [])

        if isinstance(transactions, list) and all(isinstance(t, dict) for t in transactions):
            return transactions

        last_bad_value = transactions

    raise ValueError(
        f"Gemini가 {max_attempts}회 시도에도 계속 스키마를 벗어난 응답을 반환했습니다 "
        f"(마지막 값 타입: {type(last_bad_value).__name__}, 앞부분: {str(last_bad_value)[:200]!r}). "
        "일시적인 문제일 가능성이 높으니 잠시 후 다시 시도해보세요."
    )


# ---------------------------------------------------------------------------
# 3. 파싱 엔진 선택 (PARSER_ENGINE 환경변수: "claude"(기본) 또는 "gemini")
# ---------------------------------------------------------------------------
def parse_transactions(
    card_name: str,
    raw_text: str | None = None,
    page_images: list[bytes] | None = None,
) -> list[dict]:
    if PARSER_ENGINE == "gemini":
        return parse_transactions_with_gemini(card_name, raw_text=raw_text, page_images=page_images)
    if PARSER_ENGINE == "claude":
        return parse_transactions_with_claude(card_name, raw_text=raw_text, page_images=page_images)
    raise ValueError(f"알 수 없는 PARSER_ENGINE 값: '{PARSER_ENGINE}' (claude 또는 gemini만 가능)")


# ---------------------------------------------------------------------------
# 4. Google Sheets에 행 추가 — 첨부파일명 기반 월별(YYYYMM) 탭에 씀
# ---------------------------------------------------------------------------
def month_tab_name(filename: str) -> str:
    """파일명에서 YYYYMMDD(8자리, 예: BC바로카드_20260813.pdf) 또는 YYYYMM(6자리,
    예: hyundaicard_202606.html) 형식의 날짜를 찾아 YYYYMM 형식의 탭 이름으로
    반환합니다. 카드사/형식마다 파일명에 붙는 날짜 길이가 달라서(실제 확인됨)
    둘 다 지원합니다. 날짜를 못 찾으면 SHEET_TAB(기본 "시트1")으로 폴백."""
    m = re.search(r"(20\d{2})(0[1-9]|1[0-2])(?:\d{2})?", filename or "")
    if not m:
        return SHEET_TAB
    return m.group(1) + m.group(2)


def ensure_tab(service, spreadsheet_id: str, tab_name: str) -> None:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing_titles = {s["properties"]["title"] for s in meta["sheets"]}
    if tab_name in existing_titles:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A1:E1",
        valueInputOption="USER_ENTERED",
        body={"values": [SHEET_HEADERS]},
    ).execute()


def _existing_transaction_keys(service, spreadsheet_id: str, tab_name: str) -> set[tuple]:
    """탭에 이미 기록된 거래를 (카드명, 일자, 가맹점, 금액) 키 집합으로 반환합니다.
    소계/합계 행은 제외합니다. 같은 명세서를 다시 처리하거나(라벨 유실, 수동 강제
    재처리 등) 같은 거래가 중복 삽입되는 것을 막는 데 씁니다."""
    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A2:E100000",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    keys: set[tuple] = set()
    for r in resp.get("values", []):
        if not r or r[0] == GRAND_TOTAL_LABEL or str(r[0]).endswith(SUBTOTAL_SUFFIX):
            continue
        card, day, merchant, amount, _category = (r + ["", "", "", "", ""])[:5]
        keys.add((card, _normalize_date_cell_value(day), str(merchant), _to_amount(amount)))
    return keys


def _validate_transactions(transactions: list) -> None:
    """AI가 반환한 거래 목록이 예상한 형식(딕셔너리 목록, 필수 키 포함)인지 미리
    검증합니다. Claude/Gemini가 아주 드물게 요청한 스키마(거래 = 객체 배열)를
    벗어난 값(예: 문자열 하나)을 반환하는 경우가 실제로 관찰되어(2026-08-27,
    신한카드/현대카드 처리 중 확인), 그냥 두면 다음 단계에서
    "string indices must be integers, not 'str'" 같은 원인을 알기 어려운 에러로
    나타납니다. 여기서 미리 걸러 어떤 항목이 왜 문제인지 명확히 알려줍니다."""
    required_keys = {"일자", "가맹점", "금액", "분류"}
    for i, t in enumerate(transactions):
        if not isinstance(t, dict):
            raise ValueError(
                f"AI가 반환한 {i}번째 거래 항목이 예상한 객체 형식이 아닙니다 "
                f"(타입: {type(t).__name__}, 값: {str(t)[:200]!r}). AI 응답이 "
                "일시적으로 스키마를 벗어난 것으로 보이니, 잠시 후 재처리"
                "(Apps Script의 forceReprocessAll)를 시도해보세요."
            )
        missing = required_keys - t.keys()
        if missing:
            raise ValueError(
                f"AI가 반환한 {i}번째 거래 항목에 필수 필드가 없습니다: {missing} "
                f"(값: {str(t)[:200]!r})"
            )


def _apply_category_overrides(transactions: list[dict]) -> None:
    """가맹점명에 특정 문구가 포함되면 AI가 매긴 '분류'를 규칙 기준으로 덮어씁니다.
    (예: 편의점 상호 → '카페/간식', 학원/교육 관련 → '교육'). CATEGORY_OVERRIDE_RULES를
    위에서부터 검사해 첫 매칭 1개만 적용합니다. transactions 딕셔너리를 제자리에서 수정."""
    for t in transactions:
        merchant = str(t.get("가맹점", ""))
        for keywords, category in CATEGORY_OVERRIDE_RULES:
            if any(kw in merchant for kw in keywords):
                t["분류"] = category
                break


def append_rows_to_sheet(card_name: str, transactions: list[dict], filename: str = "") -> dict:
    _validate_transactions(transactions)
    _apply_category_overrides(transactions)
    creds_info = json.loads(os.environ["SHEETS_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)
    spreadsheet_id = os.environ["SHEET_ID"]

    tab_name = month_tab_name(filename)
    ensure_tab(service, spreadsheet_id, tab_name)

    existing_keys = _existing_transaction_keys(service, spreadsheet_id, tab_name)

    rows: list[list] = []
    skipped = 0
    for t in transactions:
        if any(sub in t["가맹점"] for sub in EXCLUDED_MERCHANT_SUBSTRINGS):
            continue
        key = (card_name, t["일자"], t["가맹점"], _to_amount(t["금액"]))
        if key in existing_keys:
            skipped += 1
            continue
        existing_keys.add(key)  # 같은 요청 안의 거래끼리도 중복 방지
        rows.append([card_name, t["일자"], t["가맹점"], t["금액"], t["분류"]])

    if rows:
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_name}!A:E",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        apply_card_totals(service, spreadsheet_id, tab_name)

    return {"added": len(rows), "skipped": skipped}


# ---------------------------------------------------------------------------
# 5. 카드사별 소계 + 전체 합계 자동 계산 (가독성용 배경색 포함)
# ---------------------------------------------------------------------------
SUBTOTAL_SUFFIX = " 소계"
GRAND_TOTAL_LABEL = "전체 합계"
SUBTOTAL_COLOR = {"red": 0.85, "green": 0.92, "blue": 1.0}  # 연한 파랑
GRAND_TOTAL_COLOR = {"red": 1.0, "green": 0.84, "blue": 0.4}  # 진한 금색
AMOUNT_NUMBER_FORMAT = {"type": "CURRENCY", "pattern": '#,##0"원";-#,##0"원"'}

# 분류별 지출 비중 원형차트 관련.
# 거래표(A:E)와 겹치지 않도록 G열부터 "분류 / 금액" 2열짜리 집계표를 만들고,
# 그 표를 데이터 소스로 하는 원형차트를 시트 하단에 항상 1개만 유지한다.
CATEGORY_SUMMARY_START_COL = 6  # G열 (0-based)
CATEGORY_CHART_TITLE = "분류별 지출 비중"


def _to_amount(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        return int(cleaned) if cleaned else 0
    return 0


SHEETS_EPOCH = date(1899, 12, 30)


def _normalize_date_cell_value(value) -> str:
    """일자(예: "2026-01-01") 컬럼 전용 정규화. Sheets에 USER_ENTERED로 쓰면 사용자가
    직접 입력한 것처럼 스마트 파싱해서 날짜 일련번호로 바꿔버리는 경우가 있어(실제
    확인됨, 예: 46023) 다시 읽으면 숫자로 돌아옴 — 그런 값은 실제 날짜 문자열로
    되돌려 놓음."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (SHEETS_EPOCH + timedelta(days=int(value))).isoformat()
    return "" if value is None else str(value)


def apply_card_totals(service, spreadsheet_id: str, tab_name: str) -> None:
    """탭의 카드사별 소계 + 전체 합계 행을 최신 데이터 기준으로 다시 계산해서 씀.

    기존에 이 함수가 만들어둔 소계/합계 행(" 소계"로 끝나는 카드명, "전체 합계")은
    먼저 걸러내고 순수 거래 행만으로 다시 계산하므로, append_rows_to_sheet()가 호출될
    때마다(즉 명세서 메일이 새로 처리될 때마다) 반복 실행해도 항상 정확한 값을
    유지함(멱등적). 가독성을 위해 소계 행엔 연한 파랑, 전체 합계 행엔 진한 금색
    배경 + 굵은 글씨를 입힘.
    """
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheet_id = next(
        (s["properties"]["sheetId"] for s in meta["sheets"] if s["properties"]["title"] == tab_name),
        None,
    )
    if sheet_id is None:
        return

    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A2:E100000",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    rows = resp.get("values", [])

    transactions = [
        r for r in rows
        if r and r[0] != GRAND_TOTAL_LABEL and not str(r[0]).endswith(SUBTOTAL_SUFFIX)
    ]
    if not transactions:
        return

    groups: dict[str, list[list]] = {}
    order: list[str] = []
    for r in transactions:
        card = r[0] if r else ""
        if card not in groups:
            groups[card] = []
            order.append(card)
        groups[card].append(r)

    # 금액(D열) 합계는 파이썬에서 계산한 고정값 대신 시트 수식(SUM)으로 써서,
    # 나중에 사람이 시트에서 금액 셀을 직접 고치면 소계/전체 합계가 자동으로
    # 다시 계산되도록 함. new_rows의 0번째 항목이 실제 시트에서는 2행(헤더 다음)
    # 이므로, new_rows 인덱스 i는 항상 시트 행 번호 i+2에 대응함.
    AMOUNT_COL = "D"

    def _sheet_row(new_rows_index: int) -> int:
        return new_rows_index + 2

    new_rows: list[list] = []
    subtotal_row_indices: list[int] = []
    subtotal_cell_by_card: dict[str, str] = {}
    for card in order:
        start_row = _sheet_row(len(new_rows))
        for r in groups[card]:
            padded = (r + ["", "", "", "", ""])[:5]
            new_rows.append(padded)
        end_row = _sheet_row(len(new_rows) - 1)
        subtotal_formula = f"=SUM({AMOUNT_COL}{start_row}:{AMOUNT_COL}{end_row})"
        new_rows.append([f"{card}{SUBTOTAL_SUFFIX}", "", "", subtotal_formula, ""])
        subtotal_row_indices.append(len(new_rows) - 1)
        subtotal_cell_by_card[card] = f"{AMOUNT_COL}{_sheet_row(len(new_rows) - 1)}"

    # 전체 합계는 개인 사용내역 카드사(GRAND_TOTAL_EXCLUDED_CARDS)를 뺀 나머지
    # 카드사 소계 셀들만 더하는 수식으로 만듦(소계 자체가 이미 수식이라 개별 거래
    # 금액이 바뀌면 소계 → 전체 합계까지 자동으로 반영됨).
    included_refs = [
        subtotal_cell_by_card[card] for card in order if card not in GRAND_TOTAL_EXCLUDED_CARDS
    ]
    grand_total_formula = "=SUM(" + ",".join(included_refs) + ")" if included_refs else "=0"
    new_rows.append([GRAND_TOTAL_LABEL, "", "", grand_total_formula, ""])
    grand_total_row_index = len(new_rows) - 1

    # 값(userEnteredValue)과 서식(numberFormat/배경색/굵게)을 한 번의 updateCells
    # 요청으로 같이 씀 — value만 먼저 쓰고 서식을 나중에 별도 요청으로 입히면
    # Sheets가 값 입력 시점에 열 서식을 "자동 감지"로 되돌리는 경우가 있어 통화
    # 서식이 반영되지 않는 문제가 실제로 있었음(2026-08-26 확인). 값+서식을
    # 같은 요청에 묶어서 원자적으로 적용하면 이 문제가 사라짐.
    summary_row_extra_format = {
        i: {"backgroundColor": SUBTOTAL_COLOR, "textFormat": {"bold": True}} for i in subtotal_row_indices
    }
    summary_row_extra_format[grand_total_row_index] = {
        "backgroundColor": GRAND_TOTAL_COLOR,
        "textFormat": {"bold": True},
    }

    def _cell_data(value, col: int, row_extra: dict | None) -> dict:
        if col == 3:  # 금액
            cell_format = {"numberFormat": AMOUNT_NUMBER_FORMAT}
            if isinstance(value, str) and value.startswith("="):
                # 소계/전체 합계 행 — SUM 수식을 그대로 셀에 입력(고정값이 아님)
                user_value = {"formulaValue": value}
            else:
                user_value = {"numberValue": _to_amount(value)}
        else:
            # numberFormat을 TEXT로 명시해서 항상 원문 그대로 표시되게 함(안 그러면
            # Sheets가 "사용자가 입력한 값"으로 스마트 파싱해서 예를 들어 일자를 날짜
            # 일련번호로 바꿔버림 — 실제 확인됨).
            cell_format = {"numberFormat": {"type": "TEXT"}}
            text = _normalize_date_cell_value(value) if col == 1 else ("" if value is None else str(value))
            user_value = {"stringValue": text}
        if row_extra:
            cell_format.update(row_extra)
        return {"userEnteredValue": user_value, "userEnteredFormat": cell_format}

    sheet_rows = []
    for i, row in enumerate(new_rows):
        row_extra = summary_row_extra_format.get(i)
        sheet_rows.append({
            "values": [_cell_data(row[col], col, row_extra) for col in range(5)]
        })

    requests = [{
        "updateCells": {
            "rows": sheet_rows,
            "fields": "userEnteredValue,userEnteredFormat.numberFormat,userEnteredFormat.backgroundColor,userEnteredFormat.textFormat",
            "start": {"sheetId": sheet_id, "rowIndex": 1, "columnIndex": 0},
        }
    }]

    old_row_count = len(rows)
    if old_row_count > len(new_rows):
        # 새로 계산한 데이터가 기존보다 짧아졌으면(소계/합계 행 정리로 줄어든 경우)
        # 남는 꼬리 행의 값+서식을 완전히 비움
        requests.append({
            "updateCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1 + len(new_rows),
                    "endRowIndex": 1 + old_row_count,
                    "startColumnIndex": 0,
                    "endColumnIndex": 5,
                },
                "fields": "userEnteredValue,userEnteredFormat",
            }
        })

    service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()

    # 소계/합계가 최신 상태가 된 직후, 분류별 지출 비중 원형차트를 시트 하단에 갱신.
    # (전체 합계 행 = new_rows[grand_total_row_index] → 실제 시트에서 0-based 행 번호는
    #  grand_total_row_index + 1. 그 아래로 두 줄 띄운 자리에 차트를 앵커함.)
    _upsert_category_pie_chart(
        service, spreadsheet_id, tab_name, sheet_id,
        anchor_row_index=grand_total_row_index + 3,
    )


def _upsert_category_pie_chart(
    service, spreadsheet_id: str, tab_name: str, sheet_id: int, anchor_row_index: int
) -> None:
    """분류(E열)별 지출 합계표를 G:H에 만들고, 그 표를 소스로 하는 원형차트를 시트
    하단에 항상 1개만 유지한다(있으면 지우고 새로 그림 → 위치·데이터가 매 처리마다
    최신으로 갱신됨).

    - 집계는 시트 수식(SUMIFS)으로 넣어, 나중에 사람이 금액 셀을 직접 고치면 표와
      차트가 자동으로 다시 계산되게 한다(소계/전체 합계와 동일한 설계).
    - CHART_EXCLUDED_CARDS(현대카드)는 SUMIFS 조건에서 제외하므로 차트에 반영되지 않는다.
    """
    start_col = CATEGORY_SUMMARY_START_COL

    # 차트에서 제외할 카드사(현대카드 등)를 SUMIFS 조건으로도 그대로 제외
    excl = "".join(f',$A:$A,"<>"&"{c}"' for c in sorted(CHART_EXCLUDED_CARDS))

    def _text_cell(value: str, bold: bool = False) -> dict:
        fmt = {"numberFormat": {"type": "TEXT"}}
        if bold:
            fmt["textFormat"] = {"bold": True}
        return {"userEnteredValue": {"stringValue": value}, "userEnteredFormat": fmt}

    table_rows = [{"values": [_text_cell("분류", bold=True), _text_cell("금액", bold=True)]}]
    for i, category in enumerate(CATEGORY_CHOICES):
        sheet_row = i + 2  # G2, G3, ... (헤더가 1행)
        formula = f"=SUMIFS($D:$D,$E:$E,$G{sheet_row}{excl})"
        table_rows.append({"values": [
            _text_cell(category),
            {
                "userEnteredValue": {"formulaValue": formula},
                "userEnteredFormat": {"numberFormat": AMOUNT_NUMBER_FORMAT},
            },
        ]})

    # 이 탭에 이미 있는 '분류별 지출 비중' 차트(이전 처리에서 만든 것)를 찾아 삭제 대상에 넣음
    chart_meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title),charts(chartId,spec(title)))",
    ).execute()
    stale_chart_ids: list[int] = []
    for s in chart_meta.get("sheets", []):
        if s.get("properties", {}).get("title") != tab_name:
            continue
        for chart in s.get("charts", []):
            if chart.get("spec", {}).get("title") == CATEGORY_CHART_TITLE:
                stale_chart_ids.append(chart["chartId"])

    data_row_count = 1 + len(CATEGORY_CHOICES)  # 헤더 + 분류 9종
    requests: list[dict] = [
        {"deleteEmbeddedObject": {"objectId": cid}} for cid in stale_chart_ids
    ]
    requests.append({
        "updateCells": {
            "rows": table_rows,
            "fields": "userEnteredValue,userEnteredFormat.numberFormat,userEnteredFormat.textFormat",
            "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": start_col},
        }
    })
    requests.append({
        "addChart": {
            "chart": {
                "spec": {
                    "title": CATEGORY_CHART_TITLE,
                    "pieChart": {
                        "legendPosition": "RIGHT_LEGEND",
                        "domain": {"sourceRange": {"sources": [{
                            "sheetId": sheet_id,
                            "startRowIndex": 0, "endRowIndex": data_row_count,
                            "startColumnIndex": start_col, "endColumnIndex": start_col + 1,
                        }]}},
                        "series": {"sourceRange": {"sources": [{
                            "sheetId": sheet_id,
                            "startRowIndex": 0, "endRowIndex": data_row_count,
                            "startColumnIndex": start_col + 1, "endColumnIndex": start_col + 2,
                        }]}},
                        "pieHole": 0,
                    },
                },
                "position": {"overlayPosition": {
                    "anchorCell": {"sheetId": sheet_id, "rowIndex": max(anchor_row_index, 0), "columnIndex": 0},
                    "offsetXPixels": 0, "offsetYPixels": 0,
                    "widthPixels": 600, "heightPixels": 371,
                }},
            }
        }
    })

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()


# ---------------------------------------------------------------------------
# HTTP 엔드포인트
# ---------------------------------------------------------------------------
CARD_NAME_MAP = {"BC": "BC바로카드", "HYUNDAI": "현대카드", "SAMSUNG": "삼성카드", "SHINHAN": "신한카드"}


@app.route("/process", methods=["POST"])
def process():
    if request.headers.get("X-Shared-Secret") != os.environ["SHARED_SECRET"]:
        return jsonify({"status": "error", "message": "unauthorized"}), 401

    payload = request.get_json(force=True)
    card_type = payload["card_type"]  # "BC" | "HYUNDAI"
    file_bytes = base64.b64decode(payload["file_base64"])
    password = payload["password"]
    filename = payload.get("filename", "")

    if card_type not in CARD_NAME_MAP:
        return jsonify({"status": "error", "message": f"unknown card_type: {card_type}"}), 400
    card_name = CARD_NAME_MAP[card_type]

    try:
        if card_type == "BC":
            ext = os.path.splitext(filename)[1].lower()
            if ext in (".xlsx", ".xls"):
                raw_text = decrypt_bc_excel(file_bytes, password)
                transactions = parse_transactions(card_name, raw_text=raw_text)
            else:  # .pdf (기본값)
                page_images = decrypt_bc_pdf(file_bytes, password)
                transactions = parse_transactions(card_name, page_images=page_images)
        elif card_type == "SHINHAN":
            page_images = decrypt_shinhan_pdf(file_bytes, password)
            transactions = parse_transactions(card_name, page_images=page_images)
        elif card_type == "HYUNDAI":
            raw_text = decrypt_hyundai_html(file_bytes, password)
            transactions = parse_transactions(card_name, raw_text=raw_text)
        else:  # SAMSUNG
            raw_text = decrypt_samsung_html(file_bytes, password)
            transactions = parse_transactions(card_name, raw_text=raw_text)

        result = append_rows_to_sheet(card_name, transactions, filename=filename)

        return jsonify({
            "status": "ok",
            "rows_added": result["added"],
            "rows_skipped": result["skipped"],
            "transactions": transactions,
        })

    except Exception as exc:  # noqa: BLE001 — Apps Script가 실패를 보고 라벨을 걸지 않도록 전달
        traceback.print_exc()  # Cloud Run 로그(Logs Explorer)에 전체 스택트레이스를 남김
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok", "parser_engine": PARSER_ENGINE})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
