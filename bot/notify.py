import logging
import os

import requests

log = logging.getLogger(__name__)


def telegram(text: str, enabled: bool = True):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (enabled and tok and chat):
        log.info("ALERT: %s", text)
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=10)
        if r.ok:
            log.info("Telegram sent OK")
        else:
            log.error("Telegram rejected message: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


def sheet(tab: str, rows: list, enabled: bool = True):
    """Append rows to the Google Sheet via the Apps Script web app (see sheets/Code.gs)."""
    url, token = os.getenv("SHEET_WEBHOOK_URL"), os.getenv("SHEET_TOKEN", "")
    if not (enabled and url and rows):
        return
    try:
        import json
        body = json.dumps({"token": token, "tab": tab, "rows": rows},
                          default=lambda o: o.item() if hasattr(o, "item") else str(o))
        r = requests.post(url, data=body, headers={"Content-Type": "application/json"}, timeout=30)
        if not r.ok or '"ok":true' not in r.text.replace(" ", ""):
            log.error("Sheet write to %s failed: %s %s", tab, r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Sheet write failed: %s", e)
