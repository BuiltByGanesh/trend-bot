# Trend-Follow Bot with Trailing Stop-Loss (NSE / Angel One)

The bot buys when a stock is trending up and sells when the trend turns bearish. The stop-loss only moves up and never comes back down.

## The rules

**BUY**, when all of these are true:
1. Close is above the 50 EMA (long-term uptrend)
2. 9 EMA is above the 21 EMA, and the 21 EMA is rising
3. There's a fresh trigger: either the 9 EMA just crossed above the 21 EMA, or price pulled back to the 21 EMA and closed above the previous high
4. RSI is between 50 and 75 (has momentum but isn't overbought)

**SELL**, on whichever happens first:
- **Stop-loss**: the stop starts at entry − 1.5×ATR (or the recent swing low)
- **Trailing stop**: highest high since entry − 2×ATR. It moves up as price rises and never drops
- **Bearish cross**: the 9 EMA closes below the 21 EMA
- *(optional)* fixed target at 1:N reward:risk (`target_rr`)

**Position size**: each trade risks 1% of capital (entry − SL), and no single stock gets more than 25% of capital.

## Setup (Windows / VPS)
```bash
pip install -r requirements.txt
copy .env.example .env      # fill in Telegram + Angel One keys
```

## Use it
```bash
python main.py backtest     # FIRST: check the rules on your watchlist (2y daily, Yahoo)
python main.py once         # one scan now, paper trades
python main.py run          # loops every 15 min during market hours
```
- Open positions are saved in `state/positions.json`, and closed trades in `state/trades.csv`
- Buys, sells and SL changes are sent to Telegram

## Going live (real orders)
1. Paper trade for at least 2–4 weeks and compare the results with the backtest
2. In `config.yaml`, set `mode: live`
3. In `.env`, set `CONFIRM_LIVE=YES` (the bot won't place real orders without it)

## Docker on Hostinger VPS
```bash
docker build -t trend-bot .
docker run -d --name trend-bot --env-file .env -v $(pwd)/state:/app/state --restart unless-stopped trend-bot
```

## Tuning (config.yaml)
| Want | Change |
|---|---|
| Fewer, higher-quality entries | raise `rsi_min` to 55, `ema_trend` to 100 |
| Hold trends longer | `trail_sl_atr: 2.5–3`, `exit_on_ema_cross: false` |
| Tighter exits | `trail_sl_atr: 1.5` |
| Intraday | `timeframe: 15m`, `product_type: INTRADAY` |

Exits are checked when a scan runs, not on every tick. For a hard SL at the exchange, place a stop-loss / GTT order in Angel One right after entry.

## Host & schedule on GitHub (free, runs while your PC is off)
1. Install the GitHub CLI: `winget install GitHub.cli`, then run `gh auth login`
2. Fill in `.env`
3. In PowerShell, from this folder, run `.\setup-github.ps1`. It:
   - creates a **private** repo `trend-bot` and pushes the code
   - uploads every `.env` value as an Actions secret (`.env` itself is never committed)
   - sends a test message to Telegram, then runs a backtest and sends the results to Telegram

`.github/workflows/trade.yml` then runs a scan **every 15 min, Mon–Fri, 09:15–15:30 IST**. After each run, positions and trades are committed back to `state/` so the next run picks up where the last one stopped.

Manual runs: `gh workflow run trade.yml -f cmd=once | backtest | test-telegram`

### What you get in Telegram
| When | Message |
|---|---|
| First scan of the day | 🔔 Market open + open positions with their SLs |
| Uptrend entry | 🟢 BUY symbol, qty, price, SL, RSI |
| Price rises | ⬆️ Trailing SL raised old → new |
| SL hit / bearish cross | 🔴 SELL with reason and P&L |
| ~15:15 IST | 📊 End-of-day summary: open positions, unrealised and realised P&L |
| Can't fetch data | ⚠️ SL not checked for that stock |
| Workflow crashes | ❌ link to the failed run |
| Backtest | 🧪 per-stock results |

Notes: GitHub's scheduler can start runs 5–15 min late at busy times. A private repo gets 2,000 free Action minutes a month, and this schedule uses roughly 700–1,000 of them.
