"""
GCP 서비스 계정으로 Google Sheets 쓰기 테스트 (일회성 검증 스크립트).
- 서비스 계정 키로 인증
- 명세서 파일명에서 YYYYMM(년월)을 추출해 해당 이름의 탭을 대상으로 함
  (탭이 없으면 헤더 행과 함께 새로 생성)
- 테스트 행 추가 → 검증 후 추가한 행만 삭제(탭 자체는 실제 데이터가 쌓일 자리이므로 유지)
"""
import json
import re
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build

KEY_FILE = "../card-automation-506604-148c68e844bd.json"
SHEET_ID = "1b1Y50n_AlJ4fTFGxVlmzXCEEvpt8HEyqEHD2N5buFv0"
SHEET_HEADERS = ["카드명", "일자", "가맹점", "금액", "분류"]

# 실제 명세서 파일명 예시로 테스트 (둘 다 2026년 8월 → 같은 탭 "202608"로 귀결)
TEST_FILENAMES = ["BC바로카드_20260813.pdf", "hyundaicard_20260825.html"]


def month_tab_name(filename: str) -> str:
    """파일명에서 8자리 날짜(YYYYMMDD)를 찾아 앞 6자리(YYYYMM)를 탭 이름으로 사용."""
    m = re.search(r"(\d{8})", filename)
    if not m:
        raise ValueError(f"파일명에서 YYYYMMDD 형식의 날짜를 찾을 수 없음: {filename}")
    return m.group(1)[:6]


def ensure_tab(service, spreadsheet_id, tab_name, existing_titles):
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
    existing_titles.add(tab_name)
    print(f"  탭 '{tab_name}' 없어서 새로 생성 + 헤더 작성함.")


creds = service_account.Credentials.from_service_account_info(
    json.load(open(KEY_FILE, encoding="utf-8")),
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
)
service = build("sheets", "v4", credentials=creds)
print(f"서비스 계정: {creds.service_account_email}")

try:
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    titles = {s["properties"]["title"] for s in meta["sheets"]}
    print(f"접근 성공. 스프레드시트 제목: {meta['properties']['title']}")
    print(f"기존 탭 목록: {sorted(titles)}")
except Exception as e:
    print(f"접근 실패: {e}")
    print(f"\n=> 이 스프레드시트를 서비스 계정 이메일({creds.service_account_email})에 편집자로 공유했는지 확인하세요.")
    sys.exit(1)

added = []  # (tab_name, updated_range)

for fname in TEST_FILENAMES:
    tab = month_tab_name(fname)
    print(f"\n파일명 '{fname}' -> 탭 '{tab}'")
    ensure_tab(service, SHEET_ID, tab, titles)

    card_name = "BC바로카드" if fname.startswith("BC") else "현대카드"
    test_row = [f"[TEST]{card_name}", "2026-08-25", "서비스계정 쓰기 테스트", 0, "테스트"]
    resp = service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{tab}!A:E",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [test_row]},
    ).execute()
    updated_range = resp["updates"]["updatedRange"]
    print(f"  테스트 행 추가 성공: {updated_range}")
    added.append((tab, updated_range))

# 정리: 추가한 테스트 행만 삭제 (탭 자체는 실제 데이터가 쌓일 자리이므로 유지)
meta2 = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
sheet_id_by_title = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta2["sheets"]}

for tab, updated_range in sorted(added, key=lambda x: x[1], reverse=True):
    range_part = updated_range.split("!")[1]
    row_num = int(range_part.split(":")[0][1:])
    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id_by_title[tab],
                    "dimension": "ROWS",
                    "startIndex": row_num - 1,
                    "endIndex": row_num,
                }
            }
        }]},
    ).execute()
    print(f"테스트 행({tab}!{updated_range}) 삭제 완료.")

print("\n=== 전체 테스트 성공: 파일명 기반 월별 탭 자동 생성 + 쓰기 확인됨 ===")
