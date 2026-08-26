"""
"실제 메일함에서 검색해서 읽기"를 로컬에서 검증하는 스크립트.

운영 시(Apps Script)는 GmailApp의 자체 OAuth로 동작하므로 비밀번호가 필요 없지만,
이 스크립트는 이 세션(로컬 PC 터미널)에서 leftfootone@gmail.com 메일함을 직접
검색/읽기 위해 IMAP + 앱 비밀번호(.env의 GMAIL_APP_PASSWORD)를 사용합니다.

동작:
  1. IMAP으로 로그인 (imap.gmail.com)
  2. Gmail 전용 검색(X-GM-RAW)으로 Code.gs와 동일한 조건의 메일을 찾음:
       (subject:BC바로카드 subject:명세서) OR (subject:현대카드 subject:명세서)
       OR (subject:삼성카드 subject:명세서) OR (subject:신한카드 subject:명세서)
  3. 매칭된 메일에서 첨부파일(PDF/HTML)을 꺼냄
  4. main.py의 decrypt_bc_pdf / decrypt_hyundai_html / parse_transactions /
     append_rows_to_sheet를 그대로 재사용해서 복호화 -> 파싱 -> 시트 기록까지 수행

준비:
  .env에 GMAIL_ADDRESS(leftfootone@gmail.com)와 GMAIL_APP_PASSWORD(16자리 앱
  비밀번호, https://myaccount.google.com/apppasswords 에서 발급)를 채워두세요.
"""
import email
import imaplib
import os
import re
import sys

from dotenv import load_dotenv

load_dotenv()

from main import (
    decrypt_bc_pdf,
    decrypt_shinhan_pdf,
    decrypt_hyundai_html,
    decrypt_samsung_html,
    parse_transactions,
    append_rows_to_sheet,
)

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

SEARCHES = [
    {"card_type": "BC", "card_name": "BC바로카드", "raw_query": "subject:BC바로카드 subject:명세서", "ext": ".pdf"},
    {"card_type": "HYUNDAI", "card_name": "현대카드", "raw_query": "subject:현대카드 subject:명세서", "ext": (".html", ".htm")},
    {"card_type": "SAMSUNG", "card_name": "삼성카드", "raw_query": "subject:삼성카드 subject:명세서", "ext": (".html", ".htm")},
    {"card_type": "SHINHAN", "card_name": "신한카드", "raw_query": "subject:신한카드 subject:명세서", "ext": ".pdf"},
]

ENV_PASSWORD_KEY = {
    "BC": "BC_CARD_PASSWORD",
    "HYUNDAI": "HYUNDAI_CARD_PASSWORD",
    "SAMSUNG": "SAMSUNG_CARD_PASSWORD",
    "SHINHAN": "SHINHAN_CARD_PASSWORD",
}


def find_all_mail_folder(mail: imaplib.IMAP4_SSL) -> str:
    """Gmail 표시 언어에 관계없이 \\All 특수 플래그가 붙은 폴더(전체 보관함)를 찾음."""
    typ, data = mail.list()
    if typ != "OK":
        raise RuntimeError(f"메일함 목록 조회 실패: {typ} {data}")
    for line in data:
        line_str = line.decode("utf-8") if isinstance(line, bytes) else line
        if "\\All" in line_str:
            m = re.search(r'"([^"]+)"$', line_str)
            if m:
                return m.group(1)
    raise RuntimeError("전체 보관함(All Mail, \\All 플래그) 폴더를 찾지 못했습니다.")


def imap_search(mail: imaplib.IMAP4_SSL, raw_query: str) -> list[bytes]:
    # 한글이 포함된 검색어는 IMAP 프로토콜상 반드시 리터럴({len}\r\n<bytes>)로 보내야
    # 함(quoted string 안에 8bit 문자를 넣으면 Gmail 서버가 "Could not parse command"로
    # 거부함, 실제 확인됨). X-GM-RAW 키워드는 일반 인자로, 검색어 본문만 리터럴로 분리.
    mail._encoding = "utf-8"
    mail.literal = raw_query.encode("utf-8")
    typ, data = mail.uid("search", "CHARSET", "UTF-8", "X-GM-RAW")
    if typ != "OK":
        raise RuntimeError(f"IMAP 검색 실패: {typ} {data}")
    uids = data[0].split()
    return uids


def fetch_message(mail: imaplib.IMAP4_SSL, uid: bytes) -> email.message.Message:
    typ, data = mail.uid("fetch", uid, "(RFC822)")
    if typ != "OK":
        raise RuntimeError(f"IMAP fetch 실패: {typ} {data}")
    raw = data[0][1]
    return email.message_from_bytes(raw)


def decode_mime_filename(raw_filename: str) -> str:
    """MIME 인코딩된 파일명(=?UTF-8?B?...?=)을 사람이 읽을 수 있는 문자열로 디코딩.
    Daum 등에서 전달된 메일은 파일명이 여러 encoded-word로 쪼개져 옴(실제 확인됨) —
    get_filename()이 디코딩 없이 원본 그대로 반환하므로 반드시 이 변환이 필요함."""
    return str(email.header.make_header(email.header.decode_header(raw_filename)))


def extract_attachments(msg: email.message.Message, ext) -> list[tuple[str, bytes]]:
    found = []
    for part in msg.walk():
        raw_filename = part.get_filename()
        if not raw_filename:
            continue
        filename = decode_mime_filename(raw_filename)
        if not filename.lower().endswith(ext):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            found.append((filename, payload))
    return found


def main():
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD가 .env에 없습니다.")
        print("https://myaccount.google.com/apppasswords 에서 leftfootone@gmail.com용 앱 비밀번호를 발급해 .env에 채워주세요.")
        sys.exit(1)

    print(f"IMAP 로그인 시도: {GMAIL_ADDRESS}")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

    all_mail_folder = find_all_mail_folder(mail)
    typ, _ = mail.select(f'"{all_mail_folder}"', readonly=True)
    if typ != "OK":
        raise RuntimeError(f"메일함 선택 실패: {all_mail_folder}")
    print(f"로그인 성공, 전체 메일함({all_mail_folder}) 검색 시작\n")

    total_rows = 0
    for s in SEARCHES:
        print(f"=== {s['card_name']} 검색: {s['raw_query']!r} ===")
        uids = imap_search(mail, s["raw_query"])
        print(f"매칭된 메일 {len(uids)}건")

        for uid in uids:
            msg = fetch_message(mail, uid)
            subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
            print(f"  - {subject}")

            attachments = extract_attachments(msg, s["ext"])
            if not attachments:
                print("    (조건에 맞는 첨부파일 없음, 건너뜀)")
                continue

            password = os.environ.get(ENV_PASSWORD_KEY[s["card_type"]])
            if not password:
                print(f"    {ENV_PASSWORD_KEY[s['card_type']]}가 .env에 없어 건너뜀")
                continue

            for filename, file_bytes in attachments:
                print(f"    첨부파일: {filename} ({len(file_bytes):,} bytes)")
                try:
                    if s["card_type"] == "BC":
                        images = decrypt_bc_pdf(file_bytes, password)
                        transactions = parse_transactions(s["card_name"], page_images=images)
                    elif s["card_type"] == "SHINHAN":
                        images = decrypt_shinhan_pdf(file_bytes, password)
                        transactions = parse_transactions(s["card_name"], page_images=images)
                    elif s["card_type"] == "HYUNDAI":
                        text = decrypt_hyundai_html(file_bytes, password)
                        transactions = parse_transactions(s["card_name"], raw_text=text)
                    else:  # SAMSUNG
                        text = decrypt_samsung_html(file_bytes, password)
                        transactions = parse_transactions(s["card_name"], raw_text=text)
                except Exception as exc:
                    print(f"    처리 실패: {exc}")
                    continue

                print(f"    파싱 {len(transactions)}건")
                added = append_rows_to_sheet(s["card_name"], transactions, filename=filename)
                print(f"    시트에 {added}행 추가 완료")
                total_rows += added
        print()

    mail.logout()
    print(f"=== 전체 완료: 총 {total_rows}행 시트에 추가됨 ===")


if __name__ == "__main__":
    main()
