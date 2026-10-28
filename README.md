# trading3bot

An automated **options wheel** bot for [Alpaca](https://alpaca.markets). The wheel is the strategy; a daily stock picker tells it **which** stocks to trade.

```
STOCK PICKER (daily)              THE WHEEL (every 5 minutes)
  Congress  (Capitol Trades)        sell cash-secured put
  Insiders  (SEC Form 4)    ──▶       -> assigned? own 100 shares
  Market    (Alpaca prices)             -> sell covered calls
                                          -> called away? start again
                                    + take profit, roll, avoid earnings
```

It runs on [Render](https://render.com) every 5 minutes in US market hours, sends every decision to Telegram and/or Discord, and has a password-protected **trade log page**. It also runs on your own desktop or laptop.

> ⚠️ **Paper trading by default.** This automates rules; it is not financial advice and can't guarantee profit. Selling puts means buying the stock if it falls, and a sharp drop can cost far more than the premium. Congress discloses trades up to 45 days late and research on copying them is mixed; insider buying and price trends are signals, not certainties. Run it on paper for weeks before considering real money.

---

## How it works

### 1. The stock picker (once a day)

On the first top-of-the-hour run after the market opens (retried hourly if a source fails), it blends three sources:

| Source | What it measures | How fresh |
|---|---|---|
| **Congress** — [Capitol Trades](https://www.capitoltrades.com/trades) | Stocks members of Congress bought in the last 30 days. Each politician is scored on a year of their buys vs the S&P 500 (from the day each was *disclosed*); successful traders count up to 3×, poor ones as little as 0.25×. Needs 2+ different politicians buying | Up to 45 days late |
| **Insiders** — SEC Form 4 via [OpenInsider](http://openinsider.com) | Company officers and directors buying their own stock with their own money in the last 14 days (≥ $25k). Several insiders buying ("cluster buys") or the CEO/CFO buying counts 1.5× | Filed within 2 business days |
| **Market** — Alpaca prices | Trend (vs 50- and 200-day averages) and 3-month strength vs the S&P 500 for every candidate. Also adds today's most active stocks, so there are always fresh ideas even if the other sites fail | Live |

Each source is scaled 0–1 and blended (`congress 1.0 + insiders 1.0 + market 0.5`), with a **bonus when Congress and insiders agree**. Stocks below their 200-day average, under $5, or trading under $20M a day are dropped. The top 30 are saved as the Alpaca watchlist **Wheel Picks** (visible in the Alpaca app), and you get a Telegram summary.

### 2. The wheel (every run)

**Choosing a contract**
- **Expiry:** nearest to 30 days (21–45), but **always before the next earnings date** (as short as 7 days to fit). If earnings are within a week, it waits until after.
- **Strike adapts to conditions** — the delta target starts at 0.30 (≈30% chance of assignment) and moves further out of the money when the stock is below its 50-day average, when the market is **defensive** (S&P 500 below its 50-day average or very volatile), and when premiums are rich (implied volatility ≥ 1.3× recent realised volatility). Covered calls go further out in strong uptrends so the shares have room to rise.
- **Must be worth it:** puts must pay ≥ 10% a year on the cash they tie up (calls ≥ 4% on the shares), with a tight bid/ask spread.
- **Puts prefer strikes at or below support** (the 20-day low).

**Managing open contracts** — checked every 5 minutes:

| Situation | What it does |
|---|---|
| 50% of the premium captured | Buys it back (take profit) |
| Contract worth ≤ $0.05 | Buys it back to free the cash early; on expiry day just lets it expire |
| Earnings within 2 days, ≥ 25% profit | Closes it before earnings |
| Covered call in the money just before ex-dividend, time value < dividend | Early assignment is likely: rolls it if the stock is still wanted, otherwise lets the shares go |
| **Put approaching its strike** (within 2%, ≤ 7 days left) | Stock still a pick and not collapsing → **accepts assignment** (we wanted to own it). Otherwise, or if it's 5%+ below the strike → **rolls down and out** for a net credit; if no roll pays, accepts assignment |
| **Call approaching its strike** | Stock still a pick and in an uptrend → **rolls up and out** for a net credit to keep a winner. Otherwise **lets the shares be called away** (at or above the adjusted cost basis, so the cycle is profitable) |
| Out of the money near expiry | Lets it expire worthless |

A stock is rolled at most 3 times in 120 days, then the bot accepts the outcome. Rolls are two steps: buy back the old contract, then sell the new one as soon as that fills.

**Cost basis across cycles** — the bot rebuilds each stock's full history from Alpaca's records (premiums, assignments, expiries, shares called away). Its **adjusted cost basis** = what you paid for the shares minus all premium collected in the current campaign; covered calls are never sold below it. For example, a $13 put assigned after collecting $0.80 gives an adjusted basis of $12.20. It also tracks the number of completed cycles, total premium and realised profit per stock.

**Starting new cycles** — up to 3 stocks at a time (2 in a defensive market), taken from Wheel Picks best first. Each stock gets an equal slice of the cash budget (75% of the account ÷ 3 = $25,000 on $100k, so stocks up to ~$250 a share); a pick that's too expensive for its slice, in a downtrend, or with thinly traded options is skipped for the next. No new positions on `blackout_dates` (e.g. Fed decision days).

There is no database: everything is worked out from your Alpaca positions, orders and account activity each run, so it survives restarts.

## Seeing what it's doing

| Where | What you see |
|---|---|
| **Telegram / Discord** | Daily picks (with which sources agree), every trade with its reasoning (delta target and why, yield, earnings), rolls, take-profits, errors, and a **weekly report every Monday** (account change, open contracts, adjusted cost basis, premium and profit per stock) |
| **Trade log page** | Wheel picks, the wheel ledger (shares, purchase price, adjusted basis, premium collected, cycles and profit), every bot order, positions with P/L, cash and equity |
| **Render → trading-bot → Logs** | Every decision, including why it *didn't* trade ("earnings on 12 Nov are too close", "no roll pays a credit; accepting assignment") |

---

## 1. Get your Alpaca paper keys

1. Sign up at [alpaca.markets](https://alpaca.markets) and switch to your **Paper** account.
2. Go to **API Keys → Generate** and copy the key and secret.
3. Paper accounts can trade options. For a live account you'll need options approval (level 1 covers covered calls and cash-secured puts).

## 2. Set up alerts (pick one or both)

**Telegram**
1. In Telegram, message **@BotFather**, send `/newbot` and follow the prompts. Copy the token → `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser. The number at `"chat":{"id": ...}` is your `TELEGRAM_CHAT_ID`.

**Discord**
1. In a server you own: channel **Settings → Integrations → Webhooks → New Webhook → Copy URL** → `DISCORD_WEBHOOK_URL`.

## 3. Get a free Finnhub key (earnings dates)

Sign up at [finnhub.io](https://finnhub.io) → **Get free API key**, and copy it → `FINNHUB_API_KEY`. Without it the wheel can't see earnings dates and won't avoid them (it says so in the logs).

## 4. Deploy on Render

1. In Render: **New → Blueprint**, connect GitHub and pick this repo. Render reads `render.yaml` and creates:
   - `trading-bot`: a cron job (Starter plan; billed by the second it runs, minimum $1/month)
   - `trade-log`: the trade log page (free plan; it sleeps when idle, so the first visit takes ~30 seconds to wake)
2. Render asks for the secret values. Fill in:
   - `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` — asked for **twice** (once per service); enter the same paper keys both times
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (and/or `DISCORD_WEBHOOK_URL`)
   - `FINNHUB_API_KEY`
   - `DASHBOARD_PASSWORD` — make one up, 8+ characters
   - Leave `QUIVER_API_TOKEN` blank unless you use Quiver.
3. Click **Apply**, then open the `trade-log` service URL and log in with any username plus your `DASHBOARD_PASSWORD`.

Render only asks for these values when the Blueprint is first created. To change one later, open the service → **Environment**, edit it, and save (and change the Alpaca keys on both services). If you deployed before the Finnhub key existed, add `FINNHUB_API_KEY` this way on the `trading-bot` service.

To test without waiting for the schedule: `trading-bot` → **Trigger Run**, then watch **Logs**.

Changing settings: edit `config.yaml`, commit and push. Render rebuilds automatically.

## 5. Or run it on your own computer (desktop or laptop)

Needs Python 3.10+.

```bash
git clone https://github.com/ayorotibi/trading3bot.git
cd trading3bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env  — then fill it in

python -m bot.main --picks             # see today's stock picks (trades nothing)
python -m bot.main --report            # cost basis, premiums and profit per stock
python -m bot.main --dry-run --force   # preview the wheel's decisions without placing orders
python -m bot.main --loop              # run every 5 minutes until you stop it (Ctrl+C)
python -m bot.dashboard                # trade log page at http://localhost:8000
```

Don't run it locally **and** on Render with the same keys at the same time: two copies of the wheel could both try to sell puts.

---

## Settings (`config.yaml`)

Everything is explained in the file. The ones you're most likely to change:

| Setting | Default | Meaning |
|---|---|---|
| `wheel.max_symbols` | `3` | Stocks wheeled at once |
| `wheel.max_cash_pct` | `0.75` | Share of the account that can back puts, split equally between the stocks |
| `wheel.put_target_delta` | `0.30` | Starting delta before adjustments. Higher = more premium, more assignments |
| `wheel.take_profit_pct` | `0.50` | Buy back once half the premium is captured |
| `wheel.target_dte` | `30` | Days to expiry. Set `7` with `min_dte: 5`, `max_dte: 10` for weekly options |
| `wheel.call_floor` | `adjusted` | Covered calls never below the adjusted cost basis, or `purchase` for the price paid |
| `wheel.blackout_dates` | `[]` | Days with no new positions, e.g. `["2026-12-09"]` |
| `wheel.max_rolls` | `3` | Rolls per stock before accepting assignment / call-away |
| `picker.weights` | `1 / 1 / 0.5` | How much Congress, insiders and market data count |
| `picker.require_uptrend` | `true` | Drop stocks below their 200-day average |
| `picker.congress.source` | `capitol_trades` | Or `quiver` for Quiver Quantitative's paid API (~$30/month), more reliable |
| `picker.insiders.min_value` | `25000` | Smallest insider purchase that counts |
| `picker.exclude` | `[]` | Tickers never to pick |

Turn the picker off with `picker.enabled: false` and the wheel trades the fixed `wheel.symbols` list.

## Going live (real money)

Only after a long paper trial:

1. Generate **live** API keys in Alpaca and replace the paper ones on Render.
2. Set `ALPACA_PAPER=false` on both services (each service → **Environment**).
3. Add `LIVE_TRADING_CONFIRM` = `I-UNDERSTAND-THIS-USES-REAL-MONEY` to the `trading-bot` service.

Without step 3 the bot refuses to start in live mode. The trade log page shows a red **LIVE** badge.

## Known limitations

- **Capitol Trades and OpenInsider have no official APIs.** If either blocks Render or changes its layout, that source is skipped and the daily Telegram summary says so; the others keep working. For Congress, `source: quiver` is the paid, reliable alternative.
- **Free option data is indicative**, and greeks/implied volatility may sometimes be missing; the bot then falls back to 5% out of the money. `options_feed: opra` needs Alpaca's paid data plan.
- **Earnings dates need `FINNHUB_API_KEY`.** Without it, earnings aren't avoided.
- **Macro events** (Fed, CPI) aren't detected automatically; list them in `blackout_dates`.
- **Use a dedicated Alpaca account.** The wheel manages any short options and sells covered calls on any 100+ share positions it finds.
- **Track records and the ledger are approximate.** Track records judge each buy over a fixed 90 days using free daily prices. The ledger depends on Alpaca's activity records; if the adjusted basis ever looks wrong (under half the purchase price) the bot uses the purchase price instead.
- **The daily refresh reads many pages** (a year of congressional trades), taking a few minutes of the cron job's time once a day.

## Project layout

```
bot/main.py                     entry point (run once / loop / dry run / --picks / --report)
bot/strategies/picker.py        daily stock picker (blends the three sources)
bot/strategies/congress.py      Congress scoring
bot/strategies/politicians.py   politician track records
bot/strategies/wheel.py         the wheel: contract choice, management, rolls
bot/sources/capitol_trades.py   capitoltrades.com reader
bot/sources/openinsider.py      SEC Form 4 insider purchases (OpenInsider)
bot/sources/quiver.py           Quiver Quantitative API (optional)
bot/market.py                   trend, volatility, support, market regime
bot/events.py                   earnings calendar (Finnhub)
bot/ledger.py                   cost basis & profit across wheel cycles
bot/report.py                   weekly report
bot/broker.py                   Alpaca wrapper (all orders go through here)
bot/dashboard.py                trade log page
bot/notify.py                   Telegram / Discord alerts
config.yaml                     strategy settings
render.yaml                     Render setup
tests/                          tests with a fake broker (pytest)
```
