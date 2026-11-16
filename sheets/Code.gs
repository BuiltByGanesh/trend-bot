/**
 * Stock_Bot_Suggest – receiver for the trading bot.
 * Deploy: Extensions > Apps Script > paste this > Deploy > New deployment > Web app
 *         (Execute as: Me, Who has access: Anyone). Put the URL in SHEET_WEBHOOK_URL.
 * Optional extra lock: Project Settings > Script properties > TOKEN, and the same value as env SHEET_TOKEN.
 */
const HEADERS = {
  Signals: ['Date', 'Time', 'Symbol', 'Action', 'Qty', 'Entry', 'Stop', 'Risk ₹', 'Mode'],
  Trades: ['Date', 'Symbol', 'Entry time', 'Entry', 'Exit time', 'Exit', 'Qty', 'Reason', 'Initial SL',
           'Costs ₹', 'Net P&L ₹', 'Mode'],
  Trade_Log: ['ID', 'Buy date', 'Buy time', 'Stock', 'Qty', 'Buy price', 'Stop-loss (initial)',
              'Stop-loss (current)', 'Target (2R guide)', 'Risk ₹', 'Sell by', 'When to sell', 'Status',
              'Sell date', 'Sell time', 'Sell price', 'Exit reason', 'Costs ₹', 'Net P&L ₹', 'P&L %', 'Mode'],
  Daily: ['Date', 'Trades', 'Winners', 'Net P&L ₹', 'Return %', 'Halted', 'Mode'],
  Backtest: ['Run', 'Section', 'Period', 'Trades', 'Net P&L ₹', 'Return %', 'Win rate %', 'Profit factor',
             'Avg R', 'Max DD %', 'Costs ₹', 'Halted days'],
  Backtest_Trades: ['Run', 'Date', 'Symbol', 'Entry time', 'Entry', 'Exit time', 'Exit', 'Qty', 'Reason',
                    'Initial SL', 'Costs ₹', 'Net P&L ₹', 'R multiple', 'Entry #'],
};

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const body = JSON.parse(e.postData.contents);
    const token = PropertiesService.getScriptProperties().getProperty('TOKEN');
    if (token && body.token !== token) return out({ok: false, error: 'bad token'});
    const sh = tab(body.tab);
    if (body.upsert) return out(upsert(sh, body.upsert));
    const rows = body.rows || [];
    if (rows.length) {
      const w = Math.max(...rows.map(r => r.length));
      const padded = rows.map(r => r.concat(Array(w - r.length).fill('')));
      sh.getRange(sh.getLastRow() + 1, 1, padded.length, w).setValues(padded);
    }
    return out({ok: true, added: rows.length});
  } catch (err) {
    return out({ok: false, error: String(err)});
  } finally {
    lock.releaseLock();
  }
}

// Update the row whose first column equals rec.ID (or add it). Only the given fields change.
function upsert(sh, rec) {
  const head = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
  const n = sh.getLastRow();
  let row = -1;
  if (n > 1) {
    const ids = sh.getRange(2, 1, n - 1, 1).getValues();
    for (let i = 0; i < ids.length; i++) if (String(ids[i][0]) === String(rec.ID)) { row = i + 2; break; }
  }
  if (row < 0) { row = n + 1; sh.getRange(row, 1).setValue(rec.ID); }
  Object.keys(rec).forEach(k => {
    const c = head.indexOf(k);
    if (c >= 0) sh.getRange(row, c + 1).setValue(rec[k]);
  });
  const st = head.indexOf('Status');
  if (st >= 0) {
    const v = sh.getRange(row, st + 1).getValue();
    sh.getRange(row, 1, 1, head.length).setBackground(v === 'OPEN' ? '#fff7d6' : (Number(rec['Net P&L ₹']) > 0 ? '#e6f4ea' : (v === 'CLOSED' ? '#fce8e6' : null)));
  }
  return {ok: true, row: row};
}

function tab(name) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    const h = HEADERS[name] || ['Data'];
    sh.getRange(1, 1, 1, h.length).setValues([h]).setFontWeight('bold').setBackground('#1f2937').setFontColor('#ffffff');
    sh.setFrozenRows(1);
  }
  return sh;
}

function setup() {  // run once from the editor to create all tabs
  Object.keys(HEADERS).forEach(tab);
  const s1 = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Sheet1');
  if (s1 && s1.getLastRow() === 0 && SpreadsheetApp.getActiveSpreadsheet().getSheets().length > 1) {
    SpreadsheetApp.getActiveSpreadsheet().deleteSheet(s1);
  }
}

function out(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
