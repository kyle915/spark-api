/**
 * Torch Retail Schedule → Spark Event Confirmation / Cancel
 *
 * Install on:
 *   https://docs.google.com/spreadsheets/d/1kAvZhy2B9HoeSS-qjKXve8JWUV1oBxDqnhs1-7dQUYw
 *
 * Setup (one-time, Kyle or an Ignite sheet editor):
 *   1. Extensions → Apps Script → paste this file.
 *   2. Project Settings → Script properties:
 *        SPARK_CRON_SECRET = <same value as Cloud Run INTERNAL_CRON_SECRET>
 *        SPARK_API_BASE    = https://spark-api-new-490085168610.us-central1.run.app
 *          (optional; defaults to the prod Cloud Run URL above)
 *   3. Run `installTriggers` once (authorize as the installing Google user).
 *   4. Run `ensureConfirmationColumns` once. It finds columns by header
 *      name (not letter) and only appends a header that is missing.
 *      Checkbox validation is Send / Cancel / Force Resend /
 *      Resend Confirmation only.
 *      Never add a checkbox to "Event Confirmation Sent?" (column O).
 *
 * Live retail tab (gid 0), read 2026-09-17 — do not hardcode these letters;
 * onEdit and _postRow_ resolve them with _colIndex_:
 *   Y  Send Confirmation
 *   Z  Cancel Confirmation
 *   AA Force Resend
 *   AB Confirmation Status
 *   AC Confirmation Sent At
 *   AD Confirmation Error
 *   AE Spark Confirmation UUID
 * They were not appended at AT–AZ.
 *   Resend Confirmation is appended after the last real header (name lookup
 *   only — do not hardcode its letter). Checking it force-sends once, then
 *   the script clears the box so the next edit cannot double-email.
 *
 * Usage:
 *   - Check **Send Confirmation** → emails the BA (Retail Sampling), stamps Sent.
 *   - Check **Cancel Confirmation** → cancel email if previously Sent; stamps Cancelled.
 *   - Unchecking Send does nothing (never cancels).
 *   - BA didn't get mail: check **Resend Confirmation** (one-shot). Do not
 *     leave Force Resend checked.
 *   - Swap BA: Cancel (or Cancel checkbox) → update BA Name+Email → Send
 *     Confirmation again (no Force Resend). Overwriting Email on a Sent row
 *     also emails the new BA.
 *   - Menu Spark Confirmations → Send confirmations for tomorrow (bulk).
 */

var TORCH_SHEET_ID = '1kAvZhy2B9HoeSS-qjKXve8JWUV1oBxDqnhs1-7dQUYw';
var DEFAULT_API_BASE =
  'https://spark-api-new-490085168610.us-central1.run.app';
var ENDPOINT_PATH = '/internal/torch-sheet-event-confirmation';

var HEADER_SEND = 'Send Confirmation';
var HEADER_CANCEL = 'Cancel Confirmation';
var HEADER_FORCE = 'Force Resend';
var HEADER_RESEND = 'Resend Confirmation';
var HEADER_STATUS = 'Confirmation Status';
var HEADER_SENT_AT = 'Confirmation Sent At';
var HEADER_ERROR = 'Confirmation Error';
var HEADER_UUID = 'Spark Confirmation UUID';

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Spark Confirmations')
    .addItem('Send confirmations for tomorrow', 'sendConfirmationsForTomorrow')
    .addItem('Ensure confirmation columns', 'ensureConfirmationColumns')
    .addItem('Install onEdit trigger', 'installTriggers')
    .addToUi();
}

function installTriggers() {
  var ss = SpreadsheetApp.getActive();
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'onConfirmationEdit') {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger('onConfirmationEdit')
    .forSpreadsheet(ss)
    .onEdit()
    .create();
  SpreadsheetApp.getUi().alert('Installable onEdit trigger ready.');
}

function ensureConfirmationColumns() {
  var sheet = _retailScheduleSheet_();
  var headers = _headerRow_(sheet);
  var needed = [
    HEADER_SEND,
    HEADER_CANCEL,
    HEADER_FORCE,
    HEADER_STATUS,
    HEADER_SENT_AT,
    HEADER_ERROR,
    HEADER_UUID,
    HEADER_RESEND,
  ];
  var missing = [];
  needed.forEach(function (name) {
    if (_colIndex_(headers, name) < 0) missing.push(name);
  });
  if (missing.length) {
    // getRange's 4th arg is column count, not an end column. Append only
    // after the last real header (trailing blanks trimmed in _headerRow_).
    // Refuse if those cells already have a header — never clobber Y/AB
    // or any other existing column. Existing Send/Cancel/Status columns
    // are found by name above, wherever they sit.
    var startCol = headers.length + 1;
    var occupied = sheet.getRange(1, startCol, 1, missing.length).getValues()[0];
    var blocked = false;
    for (var i = 0; i < occupied.length; i++) {
      if (String(occupied[i] || '').trim()) {
        blocked = true;
        break;
      }
    }
    if (blocked) {
      SpreadsheetApp.getActive().toast(
        'Not appending — cells after the last header are already used. ' +
          'Confirmation columns are resolved by header name.'
      );
    } else {
      sheet.getRange(1, startCol, 1, missing.length).setValues([missing]);
      headers = headers.concat(missing);
    }
  }
  [HEADER_SEND, HEADER_CANCEL, HEADER_FORCE, HEADER_RESEND].forEach(function (name) {
    var col = _colIndex_(headers, name) + 1;
    if (col < 1) return;
    var range = sheet.getRange(2, col, Math.max(sheet.getMaxRows() - 1, 1), 1);
    var rule = SpreadsheetApp.newDataValidation()
      .requireCheckbox()
      .setAllowInvalid(false)
      .build();
    range.setDataValidation(rule);
  });
  SpreadsheetApp.getActive().toast('Confirmation columns ready.');
}

/**
 * Installable onEdit — fires for Send / Cancel / Resend checkbox TRUE.
 * Unchecking Send is intentionally a no-op.
 * Resend Confirmation clears itself before the request returns so it
 * cannot stay checked and double-fire on the next edit.
 */
function onConfirmationEdit(e) {
  if (!e || !e.range) return;
  var sheet = e.range.getSheet();
  if (sheet.getParent().getId() !== TORCH_SHEET_ID) return;
  if (e.range.getRow() < 2 || e.range.getNumRows() !== 1 || e.range.getNumColumns() !== 1) {
    return;
  }
  var headers = _headerRow_(sheet);
  var col = e.range.getColumn() - 1;
  var header = (headers[col] || '').toString().trim();
  var value = e.value;
  var checked =
    value === true ||
    value === 'TRUE' ||
    value === 'true' ||
    value === 'TRUE' ||
    String(value).toUpperCase() === 'TRUE';
  if (!checked) return;

  var row = e.range.getRow();
  if (header === HEADER_RESEND) {
    // Clear before the slow POST so a crash or a later Send edit cannot
    // see a sticky TRUE. Script writes do not re-enter onEdit.
    e.range.setValue(false);
    _postRow_(sheet, row, 'send', false, true);
    return;
  }

  var action = null;
  if (header === HEADER_SEND) action = 'send';
  if (header === HEADER_CANCEL) action = 'cancel';
  if (!action) return;

  _postRow_(sheet, row, action, false, false);
}

function sendConfirmationsForTomorrow() {
  var sheet = _retailScheduleSheet_();
  var headers = _headerRow_(sheet);
  var dateCol = _colIndex_(headers, 'Date');
  var statusCol = _colIndex_(headers, HEADER_STATUS);
  var sendCol = _colIndex_(headers, HEADER_SEND);
  if (dateCol < 0 || sendCol < 0) {
    SpreadsheetApp.getUi().alert('Missing Date or Send Confirmation column.');
    return;
  }
  var tomorrow = new Date();
  tomorrow.setHours(0, 0, 0, 0);
  tomorrow.setDate(tomorrow.getDate() + 1);
  var last = sheet.getLastRow();
  var queued = 0;
  for (var r = 2; r <= last; r++) {
    var dateVal = sheet.getRange(r, dateCol + 1).getValue();
    if (!_isSameCalendarDay_(dateVal, tomorrow)) continue;
    var status = statusCol >= 0
      ? String(sheet.getRange(r, statusCol + 1).getValue() || '')
      : '';
    if (/^sent/i.test(status.trim())) continue;
    if (/^cancelled/i.test(status.trim())) continue;
    sheet.getRange(r, sendCol + 1).setValue(true);
    _postRow_(sheet, r, 'send', false, false);
    queued++;
  }
  SpreadsheetApp.getUi().alert('Queued ' + queued + ' tomorrow row(s).');
}

function _postRow_(sheet, rowNumber, action, dryRun, resend) {
  var props = PropertiesService.getScriptProperties();
  var secret = props.getProperty('SPARK_CRON_SECRET');
  if (!secret) {
    SpreadsheetApp.getActive().toast('Missing SPARK_CRON_SECRET script property');
    return;
  }
  var base = props.getProperty('SPARK_API_BASE') || DEFAULT_API_BASE;
  var headers = _headerRow_(sheet);
  var rowValues = sheet
    .getRange(rowNumber, 1, 1, headers.length)
    .getValues()[0];
  var values = {};
  for (var i = 0; i < headers.length; i++) {
    var key = (headers[i] || '').toString().trim();
    if (!key) continue;
    var cell = rowValues[i];
    if (Object.prototype.toString.call(cell) === '[object Date]') {
      values[key] = Utilities.formatDate(
        cell,
        Session.getScriptTimeZone(),
        'MMM d, yyyy'
      );
    } else {
      values[key] = cell;
    }
  }

  var payload = {
    action: action,
    rowNumber: rowNumber,
    sheetId: TORCH_SHEET_ID,
    dryRun: !!dryRun,
    // One-shot flag. Not the Force Resend column, and not the checkbox
    // cell (that is cleared before this POST).
    resend: !!resend,
    values: values,
  };

  var resp = UrlFetchApp.fetch(base.replace(/\/$/, '') + ENDPOINT_PATH, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Cron-Secret': secret },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  var code = resp.getResponseCode();
  var body = resp.getContentText();
  Logger.log('row %s action=%s resend=%s → %s %s', rowNumber, action, !!resend, code, body);
  var parsed = null;
  try {
    parsed = JSON.parse(body);
  } catch (err) {
    parsed = null;
  }
  var apiMessage =
    parsed && parsed.message ? String(parsed.message) : '';
  if (code >= 400 || (parsed && parsed.ok === false)) {
    var toast =
      apiMessage ||
      'Spark ' + action + ' failed (HTTP ' + code + ') — see Apps Script logs';
    if (resend) toast = 'Resend failed — ' + toast;
    // Keep toast readable; full body is in Logger.
    if (toast.length > 160) toast = toast.substring(0, 157) + '…';
    SpreadsheetApp.getActive().toast(toast);
    return;
  }
  if (resend) {
    var who = values['Email'] || values['BA Name'] || 'the BA';
    SpreadsheetApp.getActive().toast('Resent to ' + who);
    return;
  }
  if (
    parsed &&
    (parsed.alreadySent === true ||
      (parsed.details && parsed.details.already_sent === true))
  ) {
    SpreadsheetApp.getActive().toast(
      apiMessage || 'Already sent — stamped Sent (no new email)'
    );
  }
}

function _retailScheduleSheet_() {
  var ss = SpreadsheetApp.openById(TORCH_SHEET_ID);
  return ss.getSheets()[0];
}

function _headerRow_(sheet) {
  var width = Math.max(sheet.getLastColumn(), 40);
  var headers = sheet
    .getRange(1, 1, 1, width)
    .getValues()[0]
    .map(function (h) {
      return (h || '').toString();
    });
  while (headers.length && !String(headers[headers.length - 1] || '').trim()) {
    headers.pop();
  }
  return headers;
}

function _colIndex_(headers, name) {
  var target = String(name || '')
    .trim()
    .toLowerCase();
  for (var i = 0; i < headers.length; i++) {
    if (String(headers[i] || '').trim().toLowerCase() === target) return i;
  }
  return -1;
}

function _isSameCalendarDay_(value, day) {
  if (!value) return false;
  var d = value;
  if (Object.prototype.toString.call(value) !== '[object Date]') {
    d = new Date(value);
  }
  if (isNaN(d.getTime())) return false;
  return (
    d.getFullYear() === day.getFullYear() &&
    d.getMonth() === day.getMonth() &&
    d.getDate() === day.getDate()
  );
}
