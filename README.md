# trading3bot

An automated options trading bot for [Alpaca](https://alpaca.markets). **The wheel is the strategy; Congress tells it which stocks to trade.**

- **Stock picking from Congress:** every trading day the bot reads a year of disclosed trades by **all** members of Congress from [Capitol Trades](https://www.capitoltrades.com/trades). It works out each politician's **track record** (how their buys did against the S&P 500) and ranks the stocks they've been buying in the last 30 days, giving more weight to the **active, successful traders**.
- **The wheel (the trading):** on the best-ranked stocks the account can afford, sell cash-secured puts. If you're assigned the shares, sell covered calls on them (never below what you paid). When the shares are called away, start again with puts.

It runs on a schedule on [Render](https://render.com) (every 5 minutes during US market hours), sends every trade to Telegram and/or Discord, and has a password-protected **trade log page** listing everything it has done. It can also run on your own desktop or laptop.

> ⚠️ **Paper trading by default.** This is a tool for automating your own rules, not financial advice, and none of these strategies is guaranteed to make money. Selling options can lose far more than the premium collected if a stock falls sharply. Politicians have up to 45 days to disclose trades, so the bot always buys after they did, and research on whether following them beats the market is mixed. Run it on paper for weeks before considering real money.

---

## How it works

Every run (every 5 minutes, market hours only):

1. Checks Alpaca's market clock and exits if the market is closed (including holidays).
2. **Congress picks** (once a day, on the first top-of-the-hour run after the market opens; retried hourly if it fails):
   - Reads every politician's disclosed buys and sells from the last year.
   - **Track records:** for each politician's buys, measures the stock's return over the 90 days after the trade was *disclosed* (the earliest day anyone could copy it), minus the S&P 500's return over the same days. Politicians who beat the market get a higher weight (up to 3×), those who lagged get less (down to 0.25×). Anyone with fewer than 5 judged buys stays neutral (1×), and short records are pulled towards neutral so one lucky trade doesn't count for much.
   - **Ranking:** for stocks disclosed in the last 30 days, each different politician buying adds trade-size points (1 for small, up to 4 for $1M+) × their weight; each politician selling subtracts the same way. One person buying five times still counts once. Only recent trades count, so only **active** politicians affect the picks.
   - Keeps stocks with **at least 2 different politicians buying**, more buying than selling, tradable on Alpaca, priced above $5.
   - Saves the ranking to an Alpaca watchlist called **Congress Picks** (visible in the Alpaca app) and sends you a Telegram summary with the top stocks and top traders.
   - It **doesn't buy anything itself**. (Set `congress.buy_stocks: true` to also hold the top 10 with 30% of the account.)
3. **Wheel** (every run), on up to **5 stocks at a time**:
   - Stocks already mid-cycle keep going, even if they drop off the picks.
   - Free slots are filled from Congress Picks, best first. A pick is skipped, and the next one tried, if one put would need more than its slice of the cash budget (equity × 50% ÷ 5, so $10,000 on a $100k account) or its options are too thinly traded.
   - Short put open → wait. If 50% of the premium is already captured, buy it back.
   - Own 100+ shares not yet covered → sell covered calls at ~0.30 delta, 21–45 days out, strike at or above your cost basis.
   - Nothing open → sell a cash-secured put at ~0.30 delta, 21–45 days out, if you have the cash and stay under the `max_cash_pct` cap.
   - Unfilled orders are cancelled and re-priced at the new mid after 30 minutes.

There is no database. The bot works out its state from your Alpaca positions and order history every run, so it survives restarts.

## Seeing what it's doing

| Where | What you see |
|---|---|
| **Telegram / Discord** | A daily summary (top 10 picks and top politicians by track record), a message for every wheel order and take-profit, and any error |
| **Trade log page** | Today's congress picks, every bot order (strategy, contract, price, filled or not), current positions with P/L, cash and equity. Auto-refreshes every minute |
| **Render → trading-bot → Logs** | The full log of every run, including why it *didn't* trade (e.g. "no put met the filters", "need $1,300, only $900 available") |

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

## 3. Deploy on Render

1. In Render: **New → Blueprint**, connect GitHub and pick this repo. Render reads `render.yaml` and creates:
   - `trading-bot`: a cron job (Starter plan; billed by the second it runs, minimum $1/month)
   - `trade-log`: the trade log page (free plan; it sleeps when idle, so the first visit takes ~30 seconds to wake)
2. Render asks for the secret values. Fill in:
   - `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` — asked for **twice** (once per service); enter the same paper keys both times
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (and/or `DISCORD_WEBHOOK_URL`)
   - `DASHBOARD_PASSWORD` — make one up, 8+ characters
   - Leave `QUIVER_API_TOKEN` blank unless you use Quiver.
3. Click **Apply**, then open the `trade-log` service URL and log in with any username plus your `DASHBOARD_PASSWORD`.

Render only asks for these values when the Blueprint is first created. To change one later, open the service → **Environment**, edit it, and save (and change the Alpaca keys on both services).

To test without waiting for the schedule: `trading-bot` → **Trigger Run**, then watch **Logs**.

Changing settings: edit `config.yaml`, commit and push. Render rebuilds automatically.

## 4. Or run it on your own computer (desktop or laptop)

Needs Python 3.10+.

```bash
git clone https://github.com/ayorotibi/trading3bot.git
cd trading3bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env  — then fill it in

python -m bot.main --dry-run --force   # preview decisions without placing orders
python -m bot.main --loop              # run every 5 minutes until you stop it (Ctrl+C)
python -m bot.dashboard                # trade log page at http://localhost:8000
```

Don't run it locally **and** on Render with the same keys at the same time: two copies of the wheel could both try to sell puts.

---

## Settings (`config.yaml`)

Everything is commented in the file. The ones you're most likely to change:

| Setting | Default | Meaning |
|---|---|---|
| `wheel.max_symbols` | `5` | Stocks wheeled at once |
| `wheel.max_cash_pct` | `0.50` | Max share of equity tied up securing puts, split equally between the stocks. Raise it to put more of the account to work |
| `wheel.put_target_delta` | `0.30` | Higher = more premium, more likely to be assigned |
| `wheel.take_profit_pct` | `0.50` | Buy back once half the premium is captured. `0` = hold to expiry |
| `wheel.symbols_from_congress` | `true` | `false` = wheel the fixed `wheel.symbols` list instead |
| `congress.min_buyers` | `2` | Different politicians needed before a stock counts |
| `congress.lookback_days` | `30` | How recent a disclosure must be to count towards the picks |
| `congress.use_track_records` | `true` | `false` = every politician counts the same |
| `congress.track_record.max_weight` | `3.0` | How much more the best traders count |
| `congress.exclude` | `[]` | Tickers never to pick |
| `congress.source` | `capitol_trades` | Free, read from capitoltrades.com. `quiver` uses Quiver Quantitative's paid API (~$30/month), which is more reliable |
| `congress.buy_stocks` | `false` | `true` = also buy the top 10 picks outright with `budget_pct` (30%) of the account |

Turn the picker off with `congress.enabled: false` and the wheel uses `wheel.symbols` instead.

**See the top traders and the ranking without trading** (on your own computer; with your Alpaca keys in `.env` it includes track records):

```bash
python -m bot.main --picks
```

## Going live (real money)

Only after a long paper trial:

1. Generate **live** API keys in Alpaca and replace the paper ones on Render.
2. Set `ALPACA_PAPER=false` on both services (each service → **Environment**).
3. Add `LIVE_TRADING_CONFIRM` = `I-UNDERSTAND-THIS-USES-REAL-MONEY` to the `trading-bot` service.

Without step 3 the bot refuses to start in live mode. The trade log page shows a red **LIVE** badge.

## Known limitations

- **Capitol Trades has no official API.** If they redesign their page, the scraper may stop finding trades; you'll see a "page layout may have changed" warning in the logs. Switch to `source: quiver` if that happens.
- **Free option data is delayed/indicative.** Prices are fine for 30-day options sold at the mid, but fills may lag. Upgrade to `options_feed: opra` if you have Alpaca's paid data plan.
- **Use a dedicated Alpaca account.** The wheel sells covered calls on any 100+ shares it finds (except ones the optional `buy_stocks` mode bought), including shares you bought yourself.
- **Track records are approximate.** They use Alpaca's free daily prices and judge each buy over a fixed 90 days, not when the politician actually sold. Past success doesn't guarantee future picks.
- **The daily refresh reads a lot of pages.** A year of trades is a few thousand rows, which takes a few minutes of the cron job's time once a day (a few cents a month on Render).
- **Capitol Trades may block Render's servers.** If the daily summary says it couldn't read Capitol Trades, switch to `source: quiver`.
- **Early assignment, earnings and dividends** aren't specially handled; the bot reacts to whatever positions it finds on the next run.

## Project layout

```
bot/main.py                     entry point (run once / loop / dry run)
bot/strategies/wheel.py         the wheel
bot/strategies/congress.py      congress stock picker
bot/strategies/politicians.py   politician track records
bot/sources/capitol_trades.py   capitoltrades.com scraper
bot/sources/quiver.py           Quiver Quantitative API
bot/broker.py                   Alpaca wrapper (all orders go through here)
bot/dashboard.py                trade log page
bot/notify.py                   Telegram / Discord alerts
config.yaml                     strategy settings
render.yaml                     Render setup
tests/                          tests with a fake broker (pytest)
```
