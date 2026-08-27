/**
 * 카드 명세서 자동 처리 — Gmail 감지 + Cloud Run 호출
 * -------------------------------------------------------
 * 1. checkNewStatements()  : 시간 트리거로 주기 실행(매일). "정산완료" 라벨이 없는
 *                            명세서 메일만 찾아 Cloud Run(/process)에 넘기고,
 *                            성공하면 라벨을 붙인다.
 * 2. forceReprocessAll()   : 필요할 때 수동으로 실행. 라벨 여부와 상관없이 최근
 *                            명세서 메일을 전부 다시 처리한다. Cloud Run이 시트에
 *                            이미 있는 거래(카드명+일자+가맹점+금액 동일)는 자동으로
 *                            건너뛰므로 중복 행 걱정 없이 실행해도 된다. 부분 실패로
 *                            누락된 거래를 다시 채우거나, 파싱/분류 로직을 고친 뒤
 *                            재확인할 때 사용.
 * 3. createTimeTrigger()   : 최초 1회 수동 실행 — 1일 1회(매일 지정 시각) 트리거 설치.
 *
 * 사전 설정 (스크립트 속성, 좌측 톱니바퀴 > 프로젝트 설정 > 스크립트 속성):
 *   CLOUD_RUN_URL          예) https://card-automation-xxxxx-an.a.run.app
 *   SHARED_SECRET          Cloud Run과 동일한 값
 *   BC_PDF_PASSWORD        BC바로카드 PDF 열기 암호 (생년월일 6자리 등)
 *   HYUNDAI_HTML_PASSWORD  현대카드 보안메일 암호
 *   SAMSUNG_HTML_PASSWORD  삼성카드 보안메일 암호
 *   SHINHAN_PDF_PASSWORD   신한카드 PDF 열기 암호
 *   PROCESSED_LABEL        기본값 "정산완료" (없으면 자동 생성)
 */

// 카드사별로 허용하는 첨부파일 확장자.
// BC바로카드는 명세서가 PDF 또는 엑셀(xlsx/xls) 둘 중 하나로 올 수 있어 둘 다 허용.
const ACCEPTED_EXTENSIONS = {
  BC: ['.pdf', '.xlsx', '.xls'],
  SHINHAN: ['.pdf'],
  HYUNDAI: ['.html', '.htm'],
  SAMSUNG: ['.html', '.htm'],
};

function getConfig_() {
  const p = PropertiesService.getScriptProperties();
  return {
    cloudRunUrl: p.getProperty('CLOUD_RUN_URL'),
    sharedSecret: p.getProperty('SHARED_SECRET'),
    bcPassword: p.getProperty('BC_PDF_PASSWORD'),
    hyundaiPassword: p.getProperty('HYUNDAI_HTML_PASSWORD'),
    samsungPassword: p.getProperty('SAMSUNG_HTML_PASSWORD'),
    shinhanPassword: p.getProperty('SHINHAN_PDF_PASSWORD'),
    labelName: p.getProperty('PROCESSED_LABEL') || '정산완료',
  };
}

function checkNewStatements() {
  runCheck_(false);
}

/**
 * 수동 강제 업데이트. "정산완료" 라벨이 이미 붙은 메일도 포함해서 다시 처리합니다.
 * Cloud Run 쪽에서 시트에 이미 있는 거래는 자동으로 걸러내므로(main.py의
 * _existing_transaction_keys), 이 함수를 실행해도 시트에 중복 행이 쌓이지 않습니다.
 * Apps Script 편집기에서 이 함수를 선택해 수동 실행하세요.
 */
function forceReprocessAll() {
  runCheck_(true);
}

function runCheck_(includeAlreadyLabeled) {
  const cfg = getConfig_();
  const label = getOrCreateLabel_(cfg.labelName);
  const exclude = includeAlreadyLabeled ? '' : (' -label:' + cfg.labelName);

  // 카드사별로 따로 검색 — 제목 조건이 다르고, card_type을 바로 알 수 있음.
  // in:inbox — 받은편지함에 있는 메일만 대상으로 함(보관된 메일이나 다른 라벨의
  // 메일은 제외). 스팸함/휴지통은 in: 조건과 무관하게 GmailApp.search()가 기본적으로
  // 검색 대상에서 제외하므로 별도 조건 없이도 이미 안전함.
  const searches = [
    { cardType: 'BC', query: 'in:inbox subject:BC바로카드 subject:명세서' + exclude },
    { cardType: 'HYUNDAI', query: 'in:inbox subject:현대카드 subject:명세서' + exclude },
    { cardType: 'SAMSUNG', query: 'in:inbox subject:삼성카드 subject:명세서' + exclude },
    { cardType: 'SHINHAN', query: 'in:inbox subject:신한카드 subject:명세서' + exclude },
  ];

  searches.forEach(function (s) {
    const threads = GmailApp.search(s.query, 0, 20);
    threads.forEach(function (thread) {
      processThread_(thread, s.cardType, cfg, label);
    });
  });
}

function processThread_(thread, cardType, cfg, label) {
  const messages = thread.getMessages();

  messages.forEach(function (message) {
    const attachments = message.getAttachments();
    const password =
      cardType === 'BC' ? cfg.bcPassword :
      cardType === 'HYUNDAI' ? cfg.hyundaiPassword :
      cardType === 'SAMSUNG' ? cfg.samsungPassword :
      cfg.shinhanPassword;

    attachments.forEach(function (attachment) {
      const name = attachment.getName().toLowerCase();
      const accepted = ACCEPTED_EXTENSIONS[cardType] || [];
      const matches = accepted.some(function (ext) { return name.endsWith(ext); });

      if (!matches) {
        return; // 이 카드사에서 기대하는 첨부 형식이 아니면 건너뜀
      }

      const payload = {
        card_type: cardType,
        file_base64: Utilities.base64Encode(attachment.getBytes()),
        password: password,
        filename: attachment.getName(),
      };

      const response = UrlFetchApp.fetch(cfg.cloudRunUrl + '/process', {
        method: 'post',
        contentType: 'application/json',
        headers: { 'X-Shared-Secret': cfg.sharedSecret },
        payload: JSON.stringify(payload),
        muteHttpExceptions: true,
      });

      const code = response.getResponseCode();
      const body = JSON.parse(response.getContentText());

      if (code === 200 && body.status === 'ok') {
        thread.addLabel(label);
        Logger.log(
          '%s 처리 완료: %s건 추가, %s건 중복 건너뜀 (%s)',
          cardType, body.rows_added, body.rows_skipped || 0, attachment.getName()
        );
      } else {
        Logger.log('처리 실패 [%s / %s]: %s', cardType, attachment.getName(), body.message);
        notifyFailure_(cardType, attachment.getName(), body.message || ('HTTP ' + code));
      }
    });
  });
}

function notifyFailure_(cardType, filename, message) {
  MailApp.sendEmail(
    Session.getActiveUser().getEmail(),
    '[카드 명세서 자동화] 처리 실패: ' + cardType,
    '파일: ' + filename + '\n오류: ' + message +
      '\n\nApps Script 실행 기록(실행 > 로그)에서 자세한 내용을 확인하세요.'
  );
}

function getOrCreateLabel_(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}

/**
 * 최초 1회만 수동 실행하세요. 매일 지정 시각(기본 오전 8시)에 실행되는
 * 시간 트리거를 설치합니다. 시각을 바꾸려면 atHour() 숫자만 수정하세요.
 */
function createTimeTrigger() {
  // 기존에 등록된 동일 함수의 트리거가 있으면 중복 생성 방지
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'checkNewStatements') {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger('checkNewStatements').timeBased().everyDays(1).atHour(8).create();
  Logger.log('매일 1회(오전 8시경) 트리거가 설치되었습니다.');
}
