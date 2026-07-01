/**
 * Google Apps Script webhook for Job-Tracker-V2.
 *
 * Setup:
 *   1. Open your Google Sheet.
 *   2. Extensions -> Apps Script. Delete any boilerplate, paste this whole file.
 *   3. Deploy -> New deployment -> gear icon -> "Web app".
 *        - Description: job-tracker
 *        - Execute as: Me
 *        - Who has access: Anyone
 *   4. Authorize when prompted, then copy the Web app URL (ends in /exec).
 *   5. Set that URL as GOOGLE_SHEETS_WEBHOOK_URL (GitHub secret and/or local .env).
 *
 * The sheet is kept sorted by:
 *   1. Role type:  intern → new_grad → mid → senior
 *   2. Date:       most recent first (by first_seen)
 *
 * De-duplication on job_id prevents duplicate rows across re-runs.
 */

var ROLE_ORDER = { "intern": 0, "new_grad": 1, "mid": 2, "senior": 3 };

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    var body    = JSON.parse(e.postData.contents);
    var columns = body.columns || [];
    var rows    = body.rows    || [];

    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];

    // Write header row once.
    if (sheet.getLastRow() === 0 && columns.length > 0) {
      sheet.appendRow(columns);
      sheet.setFrozenRows(1);
      _styleHeader(sheet, columns.length);
    }

    // Build set of existing job_ids.
    var jobIdCol = columns.indexOf("job_id");
    var existingIds = {};
    if (jobIdCol >= 0 && sheet.getLastRow() > 1) {
      var idValues = sheet.getRange(2, jobIdCol + 1, sheet.getLastRow() - 1, 1).getValues();
      for (var i = 0; i < idValues.length; i++) {
        existingIds[idValues[i][0]] = true;
      }
    }

    var appended = 0;
    for (var r = 0; r < rows.length; r++) {
      var row = rows[r];
      var id  = jobIdCol >= 0 ? row[jobIdCol] : null;
      if (id && existingIds[id]) continue;
      sheet.appendRow(row);
      appended++;
    }

    // Re-sort after appending.
    if (appended > 0 && sheet.getLastRow() > 2) {
      _sortSheet(sheet, columns);
    }

    return ContentService
      .createTextOutput(JSON.stringify({ ok: true, appended: appended }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ ok: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  } finally {
    lock.releaseLock();
  }
}

/**
 * Sort data rows:
 *  1. role_type ascending (intern → new_grad → mid → senior → unknown)
 *  2. first_seen descending (most recent first)
 */
function _sortSheet(sheet, columns) {
  var roleCol  = columns.indexOf("role_type");   // 0-based index in row array
  var dateCol  = columns.indexOf("first_seen");

  var lastRow  = sheet.getLastRow();
  if (lastRow < 3) return; // nothing to sort (just header + 1 row)

  var dataRange = sheet.getRange(2, 1, lastRow - 1, columns.length);
  var data      = dataRange.getValues();

  data.sort(function(a, b) {
    // Primary: role type order
    var ra = (roleCol >= 0 && ROLE_ORDER[a[roleCol]] !== undefined)
             ? ROLE_ORDER[a[roleCol]] : 99;
    var rb = (roleCol >= 0 && ROLE_ORDER[b[roleCol]] !== undefined)
             ? ROLE_ORDER[b[roleCol]] : 99;
    if (ra !== rb) return ra - rb;

    // Secondary: date descending (most recent first)
    var da = dateCol >= 0 ? String(a[dateCol]) : "";
    var db_ = dateCol >= 0 ? String(b[dateCol]) : "";
    if (db_ > da) return 1;
    if (db_ < da) return -1;
    return 0;
  });

  dataRange.setValues(data);
}

/** Bold + background colour for the header row. */
function _styleHeader(sheet, numCols) {
  var headerRange = sheet.getRange(1, 1, 1, numCols);
  headerRange.setFontWeight("bold");
  headerRange.setBackground("#4A90D9");
  headerRange.setFontColor("#FFFFFF");
}
