/**
 * Long Drink (EVENT CALENDAR) → Spark Event Confirmation / Cancel
 *
 * Install on the Ignite EVENT CALENDAR workbook, Long Drink tab ONLY:
 *   https://docs.google.com/spreadsheets/d/1eNIuGiOqkLIwgF7al5LkGvwYtmNHrKoct4qu1UnYHMc
 *   gid 1367810944 · tab title "Long Drink"
 *
 * Emails use Mark Anthony Brands standing walk-up MAB-V696HM (do not remint)
 * plus the Long Drink BA Sampling Guide PDF.
 *
 * Setup (one-time, Kyle or an Ignite sheet editor):
 *   1. Extensions → Apps Script → paste this file (bound to EVENT CALENDAR).
 *   2. Project Settings → Script properties:
 *        SPARK_CRON_SECRET = <same value as Cloud Run INTERNAL_CRON_SECRET>
 *        SPARK_API_BASE    = https://spark-api-new-490085168610.us-central1.run.app
 *          (optional; defaults to the prod Cloud Run URL above)
 *   3. Share the sheet with spark-api-new-sa@spark-479222.iam.gserviceaccount.com
 *      as Editor (status write-back).
 *   4. Run `installTriggers` once (authorize as the installing Google user).
 *   5. Run `ensureConfirmationColumns` once — touches Long Drink only.
 *
 * Live Long Drink tab (gid 1367810944), read 2026-11-12 — headers A–Q:
 *   City, Address, State, Store Number, FSM, Distributor, Date, Time,
 *   Recaps received, Paid BA, Product Spend, Product Spend Invoiced?,
 *   BA Name, Phone number, Email, Rate, Hours
 * Email window is always Time + 3 hours (Hours is pay math, ignored).
 * After ensureConfirmationColumns (append-only), expect R–Y:
 *   R Send Confirmation
 *   S Cancel Confirmation
 *   T Force Resend
 *   U Confirmation Status
 *   V Confirmation Sent At
 *   W Confirmation Error
 *   X Spark Confirmation UUID
 *   Y Resend Confirmation
 *
 * onEdit ignores every tab except Long Drink. Do not run
 * ensureConfirmationColumns against Mark Anthony Brands or other tabs.
 *
 * Usage:
 *   - Check **Send Confirmation** → emails the BA (Retail Sampling).
 *   - Check **Cancel Confirmation** → cancel email if previously Sent.
 *   - Unchecking Send does nothing (never cancels).
 *   - BA didn't get mail: check **Resend Confirmation** (one-shot).
 *   - Menu Spark Confirmations → Send confirmations for tomorrow (bulk).
 *   - Walk-up CTAs: https://client.igniteproductions.co/checkin/MAB-V696HM
 */

var LONG_DRINK_SHEET_ID = '1eNIuGiOqkLIwgF7al5LkGvwYtmNHrKoct4qu1UnYHMc';
var LONG_DRINK_TAB_GID = 1367810944;
var DEFAULT_API_BASE =
  'https://spark-api-new-490085168610.us-central1.run.app';
var ENDPOINT_PATH = '/internal/long-drink-sheet-event-confirmation';

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
  SpreadsheetApp.getUi().alert('Installable onEdit trigger ready (Long Drink tab only).');
}

function ensureConfirmationColumns() {
  var sheet = _longDrinkSheet_();
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
  SpreadsheetApp.getActive().toast('Long Drink confirmation columns ready.');
}

/**
 * Installable onEdit — fires for Send / Cancel / Resend checkbox TRUE.
 * Unchecking Send is intentionally a no-op. Other EVENT CALENDAR tabs
 * are ignored so Mark Anthony Brands / Torch Field Events stay untouched.
 */
function onConfirmationEdit(e) {
  if (!e || !e.range) return;
  var sheet = e.range.getSheet();
  if (sheet.getParent().getId() !== LONG_DRINK_SHEET_ID) return;
  if (sheet.getSheetId() !== LONG_DRINK_TAB_GID) return;
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
    String(value).toUpperCase() === 'TRUE';
  if (!checked) return;

  var row = e.range.getRow();
  if (header === HEADER_RESEND) {
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
  var sheet = _longDrinkSheet_();
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
  SpreadsheetApp.getUi().alert('Queued ' + queued + ' tomorrow Long Drink row(s).');
}

function _postedCell_(key, cell) {
  if (Object.prototype.toString.call(cell) !== '[object Date]') {
    return cell;
  }
  var tz = Session.getScriptTimeZone();
  var name = String(key || '').trim().toLowerCase();
  // Time-only cells sit on the Sheets epoch (1899-12-30). Long Drink's
  // column is "Time" (not Start/End Time).
  if (
    name === 'start time' ||
    name === 'end time' ||
    name === 'time' ||
    cell.getFullYear() < 1905
  ) {
    return Utilities.formatDate(cell, tz, 'h:mm a');
  }
  return Utilities.formatDate(cell, tz, 'MMM d, yyyy');
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
    values[key] = _postedCell_(key, rowValues[i]);
  }

  var payload = {
    action: action,
    rowNumber: rowNumber,
    sheetId: LONG_DRINK_SHEET_ID,
    dryRun: !!dryRun,
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

function _longDrinkSheet_() {
  var ss = SpreadsheetApp.openById(LONG_DRINK_SHEET_ID);
  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    if (sheets[i].getSheetId() === LONG_DRINK_TAB_GID) return sheets[i];
  }
  throw new Error('Long Drink tab (gid ' + LONG_DRINK_TAB_GID + ') not found');
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
