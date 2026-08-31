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

// Apps Script는 실행 1회당 총 실행 시간 상한이 있습니다(무료 Gmail 계정 약 6분,
// Workspace 약 30분). Cloud Run 왕복은 첨부 1건당 수십 초~수 분이 걸릴 수 있어
// (복호화 + Playwright/PDF 렌더 + AI 파싱 재시도 + 시트 기록 + 소계/차트 갱신),
// 처리할 메일이 쌓여 있으면 — 특히 forceReprocessAll — 이 상한에 걸려
// "Exceeded maximum execution time"으로 강제 중단됩니다.
//
// 그래서 5분이 지나면 남은 메일은 처리하지 않고 깔끔히 멈춥니다. 남은 메일은
// 라벨이 안 붙으므로 다음 트리거 실행(또는 forceReprocessAll 재실행)에서
// 그대로 이어서 처리됩니다. Cloud Run이 (카드명,일자,가맹점,금액) 중복을 자동으로
// 건너뛰고, 라벨도 성공했을 때만 붙으므로 중간에 멈춰도 시트가 깨지지 않습니다.
const MAX_RUNTIME_MS = 5 * 60 * 1000;

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
  const startTime = Date.now();
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

  for (var i = 0; i < searches.length; i++) {
    const s = searches[i];
    const threads = GmailApp.search(s.query, 0, 20);
    for (var j = 0; j < threads.length; j++) {
      if (Date.now() - startTime > MAX_RUNTIME_MS) {
        Logger.log(
          '시간 예산(%s분) 초과 — 남은 메일은 다음 실행에서 이어서 처리합니다.',
          MAX_RUNTIME_MS / 60000
        );
        return;
      }
      processThread_(threads[j], s.cardType, cfg, label);
    }
  }
}

function processThread_(thread, cardType, cfg, label) {
  const messages = thread.getMessages();

  for (var mi = 0; mi < messages.length; mi++) {
    const message = messages[mi];
    const attachments = message.getAttachments();
    const password =
      cardType === 'BC' ? cfg.bcPassword :
      cardType === 'HYUNDAI' ? cfg.hyundaiPassword :
      cardType === 'SAMSUNG' ? cfg.samsungPassword :
      cfg.shinhanPassword;

    // 주의: 시간 예산(MAX_RUNTIME_MS) 체크는 여기(첨부 단위)가 아니라 runCheck_의
    // 스레드 단위 루프에서만 합니다. 예전엔 여기서도 첨부마다 체크했는데, 한
    // 스레드에 첨부가 2개 이상 있을 때 1번째가 성공해서 thread.addLabel()이 이미
    // 실행된 뒤 2번째 처리 전에 시간 예산이 초과되면 함수가 그냥 반환되어 버리고,
    // 스레드는 이미 라벨이 붙어 있어 다음 실행에서 검색 조건(-label:라벨)에 걸려
    // 영구히 제외되는 문제가 있었습니다(2026-08-28 코드리뷰로 확인) — 즉 2번째
    // 첨부의 거래가 수동 forceReprocessAll 전까지 영원히 시트에 안 들어감. 한
    // 스레드를 시작하면 끝까지(그 스레드의 첨부를 전부) 처리하는 것으로 바꿔서
    // 이 문제를 없앴습니다(스레드당 첨부가 보통 1~2개라 예산을 살짝 넘기는 정도의
    // 비용은 감수할 만함).
    for (var ai = 0; ai < attachments.length; ai++) {
      const attachment = attachments[ai];
      const name = attachment.getName().toLowerCase();
      const accepted = ACCEPTED_EXTENSIONS[cardType] || [];
      const matches = accepted.some(function (ext) { return name.endsWith(ext); });

      if (!matches) {
        continue; // 이 카드사에서 기대하는 첨부 형식이 아니면 건너뜀
      }

      const payload = {
        card_type: cardType,
        file_base64: Utilities.base64Encode(attachment.getBytes()),
        password: password,
        filename: attachment.getName(),
      };

      try {
        const response = UrlFetchApp.fetch(cfg.cloudRunUrl + '/process', {
          method: 'post',
          contentType: 'application/json',
          headers: { 'X-Shared-Secret': cfg.sharedSecret },
          payload: JSON.stringify(payload),
          muteHttpExceptions: true,
        });

        const code = response.getResponseCode();
        var body;
        try {
          body = JSON.parse(response.getContentText());
        } catch (parseErr) {
          // Cloud Run 앞단 인프라(로드밸런서 타임아웃, 502/503/504 등)가 Flask
          // 앱까지 요청을 못 넘기고 JSON이 아닌 응답(HTML/빈 본문)을 주는 경우 —
          // 이 첨부 하나만 실패로 기록하고 나머지 첨부/스레드는 계속 처리합니다
          // (2026-08-28 코드리뷰로 지적됨: 예전엔 여기서 잡히지 않은 예외가 그대로
          // 튀어나가 runCheck_ 전체가 조용히 중단됐었음 — 실패 메일도 안 가고
          // 남은 모든 카드사/스레드가 그냥 건너뛰어짐).
          Logger.log(
            '처리 실패 [%s / %s]: 응답이 JSON이 아님 (HTTP %s): %s',
            cardType, attachment.getName(), code, response.getContentText().substring(0, 200)
          );
          notifyFailure_(cardType, attachment.getName(), 'HTTP ' + code + ' (JSON이 아닌 응답 — 인프라 오류로 추정)');
          continue;
        }

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
      } catch (fetchErr) {
        // UrlFetchApp.fetch 자체가 예외를 던지는 경우(네트워크 오류 등,
        // muteHttpExceptions로도 못 막는 케이스) — 마찬가지로 이 첨부만 실패
        // 처리하고 나머지는 계속 진행합니다.
        Logger.log('처리 실패 [%s / %s]: %s', cardType, attachment.getName(), fetchErr);
        notifyFailure_(cardType, attachment.getName(), String(fetchErr));
      }
    }
  }
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
 * 최초 1회만 수동 실행하세요. checkNewStatements를 주기 실행하는 시간 트리거를
 * 설치합니다.
 *
 * 기본은 4시간마다입니다(하루 6회). 명세서는 한 달에 한 번씩만 오지만, 한 번에
 * 여러 통이 쌓였을 때 runCheck_가 5분 시간 예산에서 멈추고 남은 건 다음 실행으로
 * 넘기므로, 자주 돌려야 밀린 메일이 하루 안에 다 빠집니다. 하루 1회로 되돌리려면
 * 아래 줄을 `.everyDays(1).atHour(8)`로 바꾸고 이 함수를 다시 실행하세요.
 */
function createTimeTrigger() {
  // 기존에 등록된 동일 함수의 트리거가 있으면 중복 생성 방지
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'checkNewStatements') {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger('checkNewStatements').timeBased().everyHours(4).create();
  Logger.log('4시간마다 실행되는 트리거가 설치되었습니다.');
}
