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

var VERSION = 3;
var APPLICATIONS = "My Applications";
var STATUSES = ["Interested", "Applied", "Interviewing", "Offer", "Rejected", "Skip"];
// New columns are only ever appended at the end, so a My Applications tab
// created by an older version keeps lining up (ensureAppColumns_ adds them).
var APP_COLUMNS = ["Status", "Apply", "Company", "Title", "Role", "Location",
                   "Posted", "First marked", "Updated", "job_id", "Listing", "Pay"];
var ROLE_COLORS = { "intern": "#E3F4E8", "new_grad": "#E3EEFA" };

// Column widths in pixels, by header. Columns are first auto-sized to their
// content, then clamped to [min, max] so a 200-character title can't push
// everything off screen. Columns listed in WRAP show long text on several
// lines instead of being cut off.
var WIDTHS = {
  "Status": [110, 120], "Apply": [60, 70], "Score": [55, 60], "Why": [220, 340],
  "Track": [110, 140], "Visa outlook": [170, 240], "Open PhD/research roles": [90, 110],
  "Postings offering sponsorship/OPT": [100, 130], "Postings ruling it out": [90, 110],
  "US citizens/clearance only": [90, 110], "Company": [120, 200], "Title": [220, 360], "Role": [70, 90],
  "Category": [75, 95], "Location": [130, 240], "Posted": [90, 100],
  "First seen": [120, 135], "First marked": [120, 150], "Updated": [120, 150],
  "Visa sponsorship": [110, 130], "US citizenship": [100, 120], "Clearance": [95, 115],
  "Grad year": [75, 100], "Applicants": [90, 170], "Repost": [85, 130],
  "Source": [80, 120], "Listing": [60, 75], "Pay": [100, 150]
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
    } else if (body.action === "list_applications") {
      result = { ok: true, ids: Object.keys(readApplications_()) };
    } else if (body.action === "listing_status") {
      result = markListings_(body.closed || []);
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
    sheet.setConditionalFormatRules(freshnessRules_(sheet, header, rows.length)
                                    .concat(roleRules_(sheet, header, rows.length)));
  }
  var idIndex = header.indexOf("job_id");
  if (idIndex >= 0) sheet.hideColumns(idIndex + 1);
  formatColumns_(sheet, header, rows.length);
  trimGrid_(sheet, values.length, header.length);
  return { ok: true, tab: name, rows: rows.length };
}

// Tabs above this many rows (All Open, ~40k) get the light treatment: the
// freshness highlight on the Posted cell only, no wrapping, fixed widths.
// Whole-row rules and wrapped text on 800k cells are what made it sluggish.
var BIG_TAB_ROWS = 5000;

/** Delete empty rows/columns left over from a bigger previous rebuild:
 *  every cell in the grid counts toward recalculation and the 10M cap. */
function trimGrid_(sheet, usedRows, usedCols) {
  var extraRows = sheet.getMaxRows() - Math.max(usedRows, 2);
  if (extraRows > 0) sheet.deleteRows(Math.max(usedRows, 2) + 1, extraRows);
  var extraCols = sheet.getMaxColumns() - usedCols;
  if (extraCols > 0) sheet.deleteColumns(usedCols + 1, extraCols);
}

// Recently posted rows. Evaluated against TODAY() by Sheets itself, so the
// highlight stays correct between rebuilds. First matching rule wins.
var FRESH = [
  { days: 2, color: "#FFE68A", bold: true },    // posted in the last 2 days
  { days: 7, color: "#FFF7D1", bold: false }    // posted in the last week
];

function freshnessRules_(sheet, header, numRows) {
  var posted = header.indexOf("Posted");
  if (posted < 0 || numRows < 1) return [];
  var cell = "$" + colLetter_(posted + 1) + "2";
  var rows = sheet.getRange(2, 1, numRows, header.length);
  var postedCells = sheet.getRange(2, posted + 1, numRows, 1);
  sheet.getRange(1, posted + 1).setNote(
    "Highlighted rows: bright yellow = posted in the last 2 days, " +
    "light yellow = posted in the last 7 days. Updates daily on its own.");
  var rules = [];
  for (var i = 0; i < FRESH.length; i++) {
    // Posted may be stored as text ("2026-09-25") or as a real date.
    var f = "=AND(" + cell + "<>\"\", IFERROR(DATEVALUE(" + cell + "), " + cell + ")>=TODAY()-"
            + FRESH[i].days + ")";
    if (FRESH[i].bold) {   // bold date first: rules on the same cell stop at the first match
      rules.push(SpreadsheetApp.newConditionalFormatRule().whenFormulaSatisfied(f)
        .setBackground(FRESH[i].color).setBold(true).setRanges([postedCells]).build());
    }
    if (numRows <= BIG_TAB_ROWS) {
      rules.push(SpreadsheetApp.newConditionalFormatRule().whenFormulaSatisfied(f)
        .setBackground(FRESH[i].color).setRanges([rows]).build());
    } else if (!FRESH[i].bold) {   // big tab: tint the Posted cell only
      rules.push(SpreadsheetApp.newConditionalFormatRule().whenFormulaSatisfied(f)
        .setBackground(FRESH[i].color).setRanges([postedCells]).build());
    }
  }
  return rules;
}

/** Intern / new-grad tint on the Role cell only, so it can't hide the
 *  freshness highlight. */
function roleRules_(sheet, header, numRows) {
  var role = header.indexOf("Role");
  if (role < 0 || numRows < 1) return [];
  var cell = "$" + colLetter_(role + 1) + "2";
  var range = sheet.getRange(2, role + 1, numRows, 1);
  var rules = [];
  for (var r in ROLE_COLORS) {
    rules.push(SpreadsheetApp.newConditionalFormatRule()
      .whenFormulaSatisfied("=" + cell + "=\"" + r + "\"")
      .setBackground(ROLE_COLORS[r]).setRanges([range]).build());
  }
  return rules;
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
  var big = numRows > BIG_TAB_ROWS;
  // Auto-sizing scans every row; on a big tab, use the upper width bound.
  if (!big) sheet.autoResizeColumns(1, header.length);
  for (var c = 0; c < header.length; c++) {
    var name = header[c];
    if (name === "job_id") continue;
    var bounds = WIDTHS[name] || DEFAULT_WIDTH;
    if (big) {
      sheet.setColumnWidth(c + 1, bounds[1]);
      continue;   // no wrapping: single-line rows scroll fast
    }
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
    var n = Math.max(sheet.getLastRow() - 1, 0);
    formatColumns_(sheet, header, n);
    sheet.setConditionalFormatRules(freshnessRules_(sheet, header, n)
                                    .concat(roleRules_(sheet, header, n)));
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
  ensureAppColumns_(apps);
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

/** Mark each My Applications row open / closed: `closed` lists the job ids
 *  the tracker saw taken down (or expired out of its 60-day window). */
function markListings_(closedIds) {
  var apps = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(APPLICATIONS);
  if (!apps || apps.getLastRow() < 2) return { ok: true, marked: 0 };
  ensureAppColumns_(apps);
  var header = apps.getRange(1, 1, 1, apps.getLastColumn()).getValues()[0];
  var id = header.indexOf("job_id"), listing = header.indexOf("Listing");
  var closed = {};
  for (var i = 0; i < closedIds.length; i++) closed[closedIds[i]] = true;
  var ids = apps.getRange(2, id + 1, apps.getLastRow() - 1, 1).getValues();
  var out = ids.map(function (r) { return [r[0] ? (closed[r[0]] ? "closed" : "open") : ""]; });
  var range = apps.getRange(2, listing + 1, out.length, 1);
  range.setValues(out);
  var rules = [SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied("=$" + colLetter_(listing + 1) + "2=\"closed\"")
    .setFontColor("#999999").setStrikethrough(true)
    .setRanges([apps.getRange(2, 1, out.length, header.length)]).build()];
  apps.setConditionalFormatRules(rules);
  var n = out.filter(function (r) { return r[0] === "closed"; }).length;
  return { ok: true, marked: n };
}

/** Append any APP_COLUMNS header cells an older version didn't have. */
function ensureAppColumns_(apps) {
  var header = apps.getRange(1, 1, 1, Math.max(apps.getLastColumn(), 1)).getValues()[0];
  for (var c = 0; c < APP_COLUMNS.length; c++) {
    if (header.indexOf(APP_COLUMNS[c]) < 0) {
      apps.getRange(1, apps.getLastColumn() + 1).setValue(APP_COLUMNS[c]);
      styleHeader_(apps, apps.getLastColumn());
      header.push(APP_COLUMNS[c]);
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
