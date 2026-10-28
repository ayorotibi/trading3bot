# trading3bot

An automated trading bot for [Alpaca](https://alpaca.markets) running two strategies:

- **Congress "follow the crowd":** every trading day, read the disclosed stock trades of **all** members of Congress from [Capitol Trades](https://www.capitoltrades.com/trades), rank stocks by how many different politicians are buying them, and hold the top 10 with 30% of the account.
- **The wheel (options):** sell cash-secured puts on the top-ranked congress stocks the account can afford. If you're assigned the shares, sell covered calls on them (never below what you paid). When the shares are called away, start again with puts.

It runs on a schedule on [Render](https://render.com) (every 5 minutes during US market hours), sends every trade to Telegram and/or Discord, and has a password-protected **trade log page** listing everything it has done. It can also run on your own desktop or laptop.

> ⚠️ **Paper trading by default.** This is a tool for automating your own rules, not financial advice, and none of these strategies is guaranteed to make money. Selling options can lose far more than the premium collected if a stock falls sharply. Politicians have up to 45 days to disclose trades, so the bot always buys after they did, and research on whether following them beats the market is mixed. Run it on paper for weeks before considering real money.

---

## How it works

Every run (every 5 minutes, market hours only):

1. Checks Alpaca's market clock and exits if the market is closed (including holidays).
2. **Congress** (once a day at 15:00 UTC, or straight away on the very first run):
   - Reads every buy and sell disclosed in the last 30 days (trades made within the last 90 days).
   - Scores each stock: each different politician buying adds 1 point for a small trade, up to 4 for $1M+; each politician selling subtracts the same way. One person buying five times still counts once.
   - Keeps stocks with **at least 2 different politicians buying**, more buying than selling, tradable on Alpaca, priced above $5.
   - Holds the **top 10**, each getting an equal share of **30% of equity** (3% each). Keeps a holding while it stays in the top 15, and sells it when it drops further or politicians turn net sellers.
   - Saves the ranking to an Alpaca watchlist called **Congress Picks** (visible in the Alpaca app) and sends you a Telegram summary.
   - Only ever sells shares it bought itself; wheel shares and anything you buy by hand are left alone.
3. **Wheel**, on up to 3 stocks at a time, taken from the Congress Picks list in rank order (skipping any priced over $50, since a put needs strike × 100 in cash). A stock already mid-cycle keeps going even if it drops off the list:
   - Short put open → wait. If 50% of the premium is already captured, buy it back.
   - Own 100+ shares not yet covered → sell covered calls at ~0.30 delta, 21–45 days out, strike at or above your cost basis.
   - Nothing open → sell a cash-secured put at ~0.30 delta, 21–45 days out, if you have the cash and stay under the `max_cash_pct` cap.
   - Unfilled orders are cancelled and re-priced at the new mid after 30 minutes.

There is no database. The bot works out its state from your Alpaca positions and order history every run, so it survives restarts.

## Seeing what it's doing

| Where | What you see |
|---|---|
| **Telegram / Discord** | A daily congress summary (top 10 and trades made), a message for every wheel order and take-profit, and any error |
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

Don't run it locally **and** on Render with the same keys at the same time. It won't double-copy politician trades (order IDs prevent that), but two copies of the wheel could both try to sell puts.

---

## Settings (`config.yaml`)

Everything is commented in the file. The ones you're most likely to change:

| Setting | Default | Meaning |
|---|---|---|
| `congress.top_n` | `10` | How many congress stocks to hold |
| `congress.budget_pct` | `0.30` | Share of equity for congress stocks, split equally |
| `congress.min_buyers` | `2` | Different politicians needed before a stock counts |
| `congress.lookback_days` | `30` | How far back to count disclosures |
| `congress.exclude` | `[]` | Tickers never to buy |
| `congress.source` | `capitol_trades` | Free, read from capitoltrades.com. `quiver` uses Quiver Quantitative's paid API (~$30/month), which is more reliable |
| `wheel.max_symbols` | `3` | Stocks wheeled at once |
| `wheel.max_share_price` | `50` | Most expensive stock the wheel will start on. A $50 stock's put ties up ~$5,000 |
| `wheel.symbols_from_congress` | `true` | `false` = wheel the fixed `wheel.symbols` list instead |
| `wheel.put_target_delta` | `0.30` | Higher = more premium, more likely to be assigned |
| `wheel.take_profit_pct` | `0.50` | Buy back once half the premium is captured. `0` = hold to expiry |
| `wheel.max_cash_pct` | `0.50` | Max share of equity tied up securing puts |

Turn either strategy off with `enabled: false`. Together the defaults can use up to 80% of the account (30% congress stocks + 50% securing puts).

**See the ranking without trading** (on your own computer, no Alpaca keys needed):

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
- **Use a dedicated Alpaca account.** The congress strategy only sells shares it bought, but the wheel will sell covered calls on any 100+ shares it finds that the congress strategy didn't buy, including ones you bought yourself.
- **Capitol Trades may block Render's servers.** If the daily summary says it couldn't read Capitol Trades, switch to `source: quiver`.
- **Early assignment, earnings and dividends** aren't specially handled; the bot reacts to whatever positions it finds on the next run.

## Project layout

```
bot/main.py                     entry point (run once / loop / dry run)
bot/strategies/wheel.py         the wheel
bot/strategies/congress.py      congress follow-the-crowd
bot/sources/capitol_trades.py   capitoltrades.com scraper
bot/sources/quiver.py           Quiver Quantitative API
bot/broker.py                   Alpaca wrapper (all orders go through here)
bot/dashboard.py                trade log page
bot/notify.py                   Telegram / Discord alerts
config.yaml                     strategy settings
render.yaml                     Render setup
tests/                          tests with a fake broker (pytest)
```
