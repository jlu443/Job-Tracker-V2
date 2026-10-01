/**
 * Google Apps Script webhook for Job-Tracker-V2 (v2: rebuilt tabs).
 *
 * Tabs (created automatically):
 *   Today            announceable jobs first seen in the last 24 hours
 *   This Week        the same, last 7 days
 *   All Open         every tracked intern/new-grad job in the 60-day window
 *   My Applications  every job you give a Status; never trimmed
 *
 * Today / This Week / All Open are rebuilt from the tracker's database on
 * each run. Set a job's Status (dropdown, first column) on any tab: the row
 * is copied to My Applications immediately, and your Status is re-applied
 * whenever the tabs are rebuilt. Clearing a Status removes it from My
 * Applications. My Applications itself is never rebuilt or trimmed.
 *
 * Setup / upgrade:
 *   1. Open your Google Sheet -> Extensions -> Apps Script.
 *   2. Replace everything with this file and save.
 *   3. Deploy -> Manage deployments -> edit (pencil) -> Version: "New version"
 *      -> Deploy. (Editing the existing deployment keeps the same /exec URL,
 *      so GOOGLE_SHEETS_WEBHOOK_URL doesn't change.)
 *      First-time setup instead: Deploy -> New deployment -> "Web app",
 *      Execute as: Me, Who has access: Anyone; copy the /exec URL into the
 *      GOOGLE_SHEETS_WEBHOOK_URL secret.
 */

var VERSION = 2;
var APPLICATIONS = "My Applications";
var STATUSES = ["Interested", "Applied", "Interviewing", "Offer", "Rejected", "Skip"];
var APP_COLUMNS = ["Status", "Apply", "Company", "Title", "Role", "Location",
                   "Posted", "First marked", "Updated", "job_id"];
var ROLE_COLORS = { "intern": "#E3F4E8", "new_grad": "#E3EEFA" };

// Column widths in pixels, by header. Columns are first auto-sized to their
// content, then clamped to [min, max] so a 200-character title can't push
// everything off screen. Columns listed in WRAP show long text on several
// lines instead of being cut off.
var WIDTHS = {
  "Status": [110, 120], "Apply": [60, 70], "Score": [55, 60], "Why": [220, 340],
  "Track": [110, 140], "Company": [120, 200], "Title": [220, 360], "Role": [70, 90],
  "Category": [75, 95], "Location": [130, 240], "Posted": [90, 100],
  "First seen": [120, 135], "First marked": [120, 150], "Updated": [120, 150],
  "Visa sponsorship": [110, 130], "US citizenship": [100, 120], "Clearance": [95, 115],
  "Grad year": [75, 100], "Applicants": [90, 170], "Repost": [85, 130],
  "Source": [80, 120], "Listing": [60, 75]
};
var DEFAULT_WIDTH = [70, 220];
var WRAP = { "Title": true, "Why": true, "Location": true, "Company": true };

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var body = JSON.parse(e.postData.contents);
    var result;
    if (body.action === "ping") {
      result = { ok: true, version: VERSION };
    } else if (body.action === "replace_tab") {
      result = replaceTab_(body.tab, body.columns || [], body.rows || []);
    } else {
      result = { ok: false, error: "unknown action; expected ping or replace_tab" };
    }
    return json_(result);
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

/** Rewrite one tab in a single write, keeping every Status the user set. */
function replaceTab_(name, columns, rows) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(name) || ss.insertSheet(name);
  var idCol = columns.indexOf("job_id");
  var roleCol = columns.indexOf("Role");

  // Status comes from My Applications first, then whatever is on this tab
  // (covers an edit whose onEdit copy failed).
  var statuses = readStatuses_(sheet);
  var fromApps = readApplications_();
  for (var id in fromApps) statuses[id] = fromApps[id].status;

  var header = ["Status"].concat(columns);
  var values = [header];
  for (var i = 0; i < rows.length; i++) {
    var id2 = idCol >= 0 ? rows[i][idCol] : "";
    values.push([statuses[id2] || ""].concat(rows[i]));
  }

  var filter = sheet.getFilter();
  if (filter) filter.remove();
  sheet.clear();
  sheet.clearConditionalFormatRules();
  sheet.getRange(1, 1, values.length, header.length).setValues(values);

  styleHeader_(sheet, header.length);
  sheet.setFrozenRows(1);
  sheet.setFrozenColumns(Math.min(4, header.length));
  if (rows.length > 0) {
    var statusRange = sheet.getRange(2, 1, rows.length, 1);
    statusRange.setDataValidation(statusRule_());
    sheet.getRange(1, 1, rows.length + 1, header.length).createFilter();
    if (roleCol >= 0) {
      var roleLetter = colLetter_(roleCol + 2);   // +1 for Status, +1 for 1-based
      var rules = [];
      for (var role in ROLE_COLORS) {
        rules.push(SpreadsheetApp.newConditionalFormatRule()
          .whenFormulaSatisfied("=$" + roleLetter + "2=\"" + role + "\"")
          .setBackground(ROLE_COLORS[role])
          .setRanges([sheet.getRange(2, 1, rows.length, header.length)])
          .build());
      }
      sheet.setConditionalFormatRules(rules);
    }
  }
  var idIndex = header.indexOf("job_id");
  if (idIndex >= 0) sheet.hideColumns(idIndex + 1);
  formatColumns_(sheet, header, rows.length);
  return { ok: true, tab: name, rows: rows.length };
}

/** Readable widths, wrapping for long text, top-aligned rows. */
function formatColumns_(sheet, header, numRows) {
  var lastRow = Math.max(numRows + 1, 1);
  sheet.getRange(1, 1, 1, header.length)
       .setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP)
       .setVerticalAlignment("middle");
  if (numRows > 0) {
    sheet.getRange(2, 1, numRows, header.length)
         .setVerticalAlignment("top")
         .setWrapStrategy(SpreadsheetApp.WrapStrategy.CLIP);
  }
  sheet.autoResizeColumns(1, header.length);
  for (var c = 0; c < header.length; c++) {
    var name = header[c];
    if (name === "job_id") continue;
    var bounds = WIDTHS[name] || DEFAULT_WIDTH;
    var width = sheet.getColumnWidth(c + 1);
    if (width < bounds[0]) sheet.setColumnWidth(c + 1, bounds[0]);
    if (width > bounds[1]) sheet.setColumnWidth(c + 1, bounds[1]);
    if (WRAP[name] && numRows > 0) {
      sheet.getRange(2, c + 1, numRows, 1)
           .setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);
    }
  }
  sheet.setRowHeight(1, 36);
}

/** Run once by hand (Run ▶ in the Apps Script editor) to format existing tabs
 *  without waiting for the next tracker run. */
function formatAllTabs() {
  var sheets = SpreadsheetApp.getActiveSpreadsheet().getSheets();
  for (var i = 0; i < sheets.length; i++) {
    var sheet = sheets[i];
    if (sheet.getLastColumn() < 1) continue;
    var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    formatColumns_(sheet, header, Math.max(sheet.getLastRow() - 1, 0));
  }
}

/** Simple trigger: a Status edit on a rebuilt tab is copied to My Applications. */
function onEdit(e) {
  var sheet = e.range.getSheet();
  if (sheet.getName() === APPLICATIONS || e.range.getRow() < 2) return;
  var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  if (header[e.range.getColumn() - 1] !== "Status") return;

  var row = e.range.getRow();
  var values = sheet.getRange(row, 1, 1, header.length).getValues()[0];
  var formulas = sheet.getRange(row, 1, 1, header.length).getFormulas()[0];
  var rec = {};
  for (var i = 0; i < header.length; i++) {
    rec[header[i]] = formulas[i] || values[i];
  }
  if (!rec["job_id"]) return;
  upsertApplication_(rec, String(e.value || ""));
}

function upsertApplication_(rec, status) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var apps = ss.getSheetByName(APPLICATIONS);
  if (!apps) {
    apps = ss.insertSheet(APPLICATIONS);
    apps.getRange(1, 1, 1, APP_COLUMNS.length).setValues([APP_COLUMNS]);
    styleHeader_(apps, APP_COLUMNS.length);
    apps.setFrozenRows(1);
    formatColumns_(apps, APP_COLUMNS, 0);
  }
  var existing = readApplications_();
  var now = new Date();
  var hit = existing[rec["job_id"]];
  if (hit) {
    if (!status) {
      apps.deleteRow(hit.row);
    } else {
      apps.getRange(hit.row, 1).setValue(status);
      apps.getRange(hit.row, APP_COLUMNS.indexOf("Updated") + 1).setValue(now);
    }
    return;
  }
  if (!status) return;
  var out = APP_COLUMNS.map(function (c) {
    if (c === "Status") return status;
    if (c === "First marked" || c === "Updated") return now;
    return rec[c] !== undefined ? rec[c] : "";
  });
  apps.appendRow(out);
  var last = apps.getLastRow();
  apps.getRange(last, 1).setDataValidation(statusRule_());
  apps.getRange(last, 1, 1, APP_COLUMNS.length).setVerticalAlignment("top");
  for (var c = 0; c < APP_COLUMNS.length; c++) {
    if (WRAP[APP_COLUMNS[c]]) {
      apps.getRange(last, c + 1).setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);
    }
  }
}

/** job_id -> {status, row} from My Applications. */
function readApplications_() {
  var apps = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(APPLICATIONS);
  var out = {};
  if (!apps || apps.getLastRow() < 2) return out;
  var header = apps.getRange(1, 1, 1, apps.getLastColumn()).getValues()[0];
  var s = header.indexOf("Status"), id = header.indexOf("job_id");
  if (s < 0 || id < 0) return out;
  var data = apps.getRange(2, 1, apps.getLastRow() - 1, header.length).getValues();
  for (var i = 0; i < data.length; i++) {
    if (data[i][id]) out[data[i][id]] = { status: data[i][s], row: i + 2 };
  }
  return out;
}

/** job_id -> Status currently shown on a tab. */
function readStatuses_(sheet) {
  var out = {};
  if (sheet.getLastRow() < 2) return out;
  var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  var s = header.indexOf("Status"), id = header.indexOf("job_id");
  if (s < 0 || id < 0) return out;
  var data = sheet.getRange(2, 1, sheet.getLastRow() - 1, header.length).getValues();
  for (var i = 0; i < data.length; i++) {
    if (data[i][id] && data[i][s]) out[data[i][id]] = data[i][s];
  }
  return out;
}

function statusRule_() {
  return SpreadsheetApp.newDataValidation()
    .requireValueInList(STATUSES, true).setAllowInvalid(false).build();
}

function styleHeader_(sheet, numCols) {
  var r = sheet.getRange(1, 1, 1, numCols);
  r.setFontWeight("bold").setBackground("#4A90D9").setFontColor("#FFFFFF");
}

function colLetter_(n) {
  var s = "";
  while (n > 0) { var m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = (n - m - 1) / 26; }
  return s;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
