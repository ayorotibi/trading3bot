# trading3bot

An automated trading bot for [Alpaca](https://alpaca.markets) running two strategies:

- **The wheel (options):** sell cash-secured puts on stocks you'd be happy to own. If you're assigned the shares, sell covered calls on them (never below what you paid). When the shares are called away, start again with puts.
- **Politician copy trading:** read public congressional trade disclosures (Michael McCaul by default) and mirror them with a fixed dollar amount per trade.

It runs on a schedule on [Render](https://render.com) (every 5 minutes during US market hours), sends every trade to Telegram and/or Discord, and has a password-protected **trade log page** listing everything it has done. It can also run on your own desktop or laptop.

> ⚠️ **Paper trading by default.** This is a tool for automating your own rules, not financial advice, and none of these strategies is guaranteed to make money. Selling options can lose far more than the premium collected if a stock falls sharply. Politicians have up to 45 days to disclose trades, so copied trades always arrive late. Run it on paper for weeks before considering real money.

---

## How it works

Every run (every 5 minutes, market hours only):

1. Checks Alpaca's market clock and exits if the market is closed (including holidays).
2. **Wheel**, for each symbol in `config.yaml`:
   - Short put open → wait. If 50% of the premium is already captured, buy it back.
   - Own 100+ shares not yet covered → sell covered calls at ~0.30 delta, 21–45 days out, strike at or above your cost basis.
   - Nothing open → sell a cash-secured put at ~0.30 delta, 21–45 days out, if you have the cash and stay under the `max_cash_pct` cap.
   - Unfilled orders are cancelled and re-priced at the new mid after 30 minutes.
3. **Copy trading** (every 30 minutes): fetches recent disclosures. New buys published in the last 7 days get a $500 market buy; disclosed sales close your position in that stock. Each disclosure is copied once only.

There is no database. The bot works out its state from your Alpaca positions and order history every run, so it survives restarts.

## Seeing what it's doing

| Where | What you see |
|---|---|
| **Telegram / Discord** | A message for every order placed, every take-profit, and any error |
| **Trade log page** | Every bot order (strategy, contract, price, filled or not), current positions with P/L, cash and equity. Auto-refreshes every minute |
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
2. Render asks for the secret values. Fill in `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, your Telegram/Discord values, and a strong `DASHBOARD_PASSWORD`. Leave `QUIVER_API_TOKEN` blank unless you use Quiver.
3. Click **Apply**. Open the `trade-log` service URL, enter any username plus your `DASHBOARD_PASSWORD`.

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
| `wheel.symbols` | `[F, SOFI, BAC]` | Stocks to wheel. Each put needs strike × 100 in cash (a $12 put ties up $1,200) |
| `wheel.put_target_delta` | `0.30` | Higher = more premium, more likely to be assigned |
| `wheel.take_profit_pct` | `0.50` | Buy back once half the premium is captured. `0` = hold to expiry |
| `wheel.max_cash_pct` | `0.50` | Max share of equity tied up securing puts |
| `copy_trading.politicians` | McCaul | Add more using the ID in their capitoltrades.com URL |
| `copy_trading.amount_per_trade_usd` | `500` | Dollars per copied buy |
| `copy_trading.source` | `capitol_trades` | Free, scraped from capitoltrades.com. `quiver` uses Quiver Quantitative's paid API (~$30/month), which is more reliable |

Turn either strategy off with `enabled: false`.

**Michael McCaul** announced he isn't running for re-election in 2026, so his disclosures will stop once he leaves office in January 2027. Add other politicians before then.

## Going live (real money)

Only after a long paper trial:

1. Generate **live** API keys in Alpaca and replace the paper ones on Render.
2. Set `ALPACA_PAPER=false` in the `trading-secrets` group.
3. Add `LIVE_TRADING_CONFIRM` = `I-UNDERSTAND-THIS-USES-REAL-MONEY` to the `trading-bot` service.

Without step 3 the bot refuses to start in live mode. The trade log page shows a red **LIVE** badge.

## Known limitations

- **Capitol Trades has no official API.** If they redesign their page, the scraper may stop finding trades; you'll see a "page layout may have changed" warning in the logs. Switch to `source: quiver` if that happens.
- **Free option data is delayed/indicative.** Prices are fine for 30-day options sold at the mid, but fills may lag. Upgrade to `options_feed: opra` if you have Alpaca's paid data plan.
- **Mirrored sales close your whole position** in that stock, including shares you bought yourself, so use a dedicated Alpaca account. Wheel symbols are never touched by copy trading.
- **Early assignment, earnings and dividends** aren't specially handled; the bot reacts to whatever positions it finds on the next run.

## Project layout

```
bot/main.py                     entry point (run once / loop / dry run)
bot/strategies/wheel.py         the wheel
bot/strategies/copy_trading.py  politician copy trading
bot/sources/capitol_trades.py   capitoltrades.com scraper
bot/sources/quiver.py           Quiver Quantitative API
bot/broker.py                   Alpaca wrapper (all orders go through here)
bot/dashboard.py                trade log page
bot/notify.py                   Telegram / Discord alerts
config.yaml                     strategy settings
render.yaml                     Render setup
tests/                          tests with a fake broker (pytest)
```
