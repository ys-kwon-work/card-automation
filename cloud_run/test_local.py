"""
배포 전 로컬 검증용 스크립트.
GCP/Apps Script 세팅 없이, 실제 파일 + 실제 비밀번호로 복호화(+ 가능하면 AI 파싱까지)가
되는지 먼저 확인합니다.

준비:
  cd cloud_run
  pip install -r requirements.txt
  playwright install chromium
  .env 파일에 아래 값을 채워두면 매번 커맨드라인에 비밀번호를 입력하지 않아도 되고,
  해당 파싱 엔진의 API 키가 채워져 있으면 복호화 후 자동으로 파싱까지 실행해서
  결과를 보여줍니다.
    PARSER_ENGINE=claude 또는 gemini (기본값 claude, main.py와 동일하게 언제든 전환 가능)
    ANTHROPIC_API_KEY=...   (PARSER_ENGINE=claude일 때, 발급: https://console.anthropic.com/settings/keys)
    GEMINI_API_KEY=...      (PARSER_ENGINE=gemini일 때, 발급: https://aistudio.google.com/apikey
                              — 단, Claude 세션 안에서는 네트워크 제약으로 이 엔진 테스트가
                              막혀 있을 수 있음. 사용자 자신의 PC에서 실행하면 정상 동작함.)
    BC_CARD_PASSWORD=...
    HYUNDAI_CARD_PASSWORD=...

사용법:
  python3 test_local.py bc "실제BC명세서.pdf"                 # .env의 비밀번호 사용
  python3 test_local.py bc "실제BC명세서.pdf" "생년월일6자리"    # 비밀번호 직접 지정
  python3 test_local.py hyundai "실제현대명세서.html"
  python3 test_local.py hyundai "실제현대명세서.html" "비밀번호"
  python3 test_local.py samsung "실제삼성명세서.html"
  python3 test_local.py shinhan "실제신한명세서.pdf"

BC바로카드/신한카드는 텍스트 대신 페이지 이미지(PNG)를 생성합니다 — 이 두 카드사
PDF는 복사/추출 방지를 위해 텍스트 레이어가 스크램블되어 있어(둘 다 실제 확인됨)
이미지로 렌더링해서 비전 모델 입력으로 사용해야 하기 때문입니다. 생성된
{bc,shinhan}_page_N.png 파일을 직접 열어서 가맹점명이 잘 보이는지 확인하세요.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

from main import (
    decrypt_bc_pdf,
    decrypt_shinhan_pdf,
    decrypt_hyundai_html,
    decrypt_samsung_html,
    parse_transactions,
    PARSER_ENGINE,
)

CARD_NAME_MAP = {"bc": "BC바로카드", "hyundai": "현대카드", "samsung": "삼성카드", "shinhan": "신한카드"}
ENV_PASSWORD_KEY = {
    "bc": "BC_CARD_PASSWORD",
    "hyundai": "HYUNDAI_CARD_PASSWORD",
    "samsung": "SAMSUNG_CARD_PASSWORD",
    "shinhan": "SHINHAN_CARD_PASSWORD",
}
ENGINE_API_KEY = {"claude": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
PDF_CARD_TYPES = ("bc", "shinhan")


def print_transactions(transactions: list[dict]) -> None:
    print(f"거래 {len(transactions)}건 파싱됨:")
    for t in transactions:
        amount = t.get("금액")
        amount_str = f"{amount:,}" if isinstance(amount, int) else str(amount)
        print(f"  {t.get('일자')}\t{t.get('가맹점')}\t{amount_str:>10}\t{t.get('분류')}")


def main():
    if len(sys.argv) not in (3, 4):
        print(__doc__)
        sys.exit(1)

    card_type = sys.argv[1]
    filepath = sys.argv[2]

    if card_type not in CARD_NAME_MAP:
        print("첫 번째 인자는 bc, hyundai, samsung, shinhan 중 하나여야 합니다.")
        sys.exit(1)

    password = sys.argv[3] if len(sys.argv) == 4 else os.environ.get(ENV_PASSWORD_KEY[card_type])
    if not password:
        print(f"비밀번호가 없습니다. 커맨드라인 인자로 넘기거나 .env의 {ENV_PASSWORD_KEY[card_type]}를 채워주세요.")
        sys.exit(1)

    with open(filepath, "rb") as f:
        file_bytes = f.read()

    card_name = CARD_NAME_MAP[card_type]
    engine_key_name = ENGINE_API_KEY.get(PARSER_ENGINE)
    parser_ready = bool(engine_key_name and os.environ.get(engine_key_name))

    if card_type in PDF_CARD_TYPES:
        decrypt_fn = decrypt_bc_pdf if card_type == "bc" else decrypt_shinhan_pdf
        images = decrypt_fn(file_bytes, password)
        print(f"=== {card_name}: {len(images)}개 페이지를 이미지로 렌더링 완료 ===")
        for i, img_bytes in enumerate(images, start=1):
            out_path = f"{card_type}_page_{i}.png"
            with open(out_path, "wb") as out_f:
                out_f.write(img_bytes)
            print(f"  page {i}: {len(img_bytes):,} bytes -> {out_path}")
        print("\n생성된 PNG 파일들을 직접 열어 가맹점명이 잘 보이는지 확인하세요.")

        if parser_ready:
            print(f"\n=== PARSER_ENGINE={PARSER_ENGINE} ({engine_key_name} 감지됨) → 파싱 실행 ===")
            transactions = parse_transactions(card_name, page_images=images)
            print_transactions(transactions)
        else:
            print(f"\n(.env에 PARSER_ENGINE={PARSER_ENGINE}에 맞는 {engine_key_name}를 채우면 이어서 파싱까지 자동 실행됩니다.)")

    else:  # hyundai, samsung
        decrypt_fn = decrypt_hyundai_html if card_type == "hyundai" else decrypt_samsung_html
        text = decrypt_fn(file_bytes, password)
        print("=== 추출된 텍스트 (앞 1000자) ===")
        print(text[:1000])
        print("\n=== 총 길이:", len(text), "자 ===")

        if parser_ready:
            print(f"\n=== PARSER_ENGINE={PARSER_ENGINE} ({engine_key_name} 감지됨) → 파싱 실행 ===")
            transactions = parse_transactions(card_name, raw_text=text)
            print_transactions(transactions)
        else:
            print(f"\n(.env에 PARSER_ENGINE={PARSER_ENGINE}에 맞는 {engine_key_name}를 채우면 이어서 파싱까지 자동 실행됩니다.)")


if __name__ == "__main__":
    main()
