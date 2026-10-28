"""Read-only trade log page.

Shows every order the bot placed (read from Alpaca's own order history, so
nothing is lost between cron runs), which strategy placed it, whether it
filled, plus your current positions and account balance.

  python -m bot.dashboard     then open http://localhost:8000

Protected by HTTP basic auth: username anything, password = DASHBOARD_PASSWORD.
It never places or cancels orders.
"""
from __future__ import annotations

import base64
import hmac
import html
import logging
import os
from datetime import timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from . import occ

log = logging.getLogger("dashboard")

LABELS = {
    "wh-put": "Wheel · sell put",
    "wh-call": "Wheel · covered call",
    "wh-tp": "Wheel · take profit",
    "cr-buy": "Congress · buy",
    "cr-sell": "Congress · sell",
}


def strategy_label(coid: str) -> str:
    for prefix, label in LABELS.items():
        if (coid or "").startswith(prefix + "-"):
            return label
    return "Other / manual"


def _v(x):
    return getattr(x, "value", x)


def _money(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _num(x) -> str:
    try:
        return f"{float(x):g}"
    except (TypeError, ValueError):
        return "—"


def _describe(symbol: str) -> str:
    if occ.is_option_symbol(symbol):
        o = occ.parse(symbol)
        return f"{o.underlying} ${o.strike:g} {o.type} · {o.expiry:%d %b %Y}"
    return symbol


class Data:
    def __init__(self):
        paper = os.getenv("ALPACA_PAPER", "true").strip().lower() in ("1", "true", "yes", "on")
        self.paper = paper
        self.client = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=paper)
        self.tz = ZoneInfo(os.getenv("DASHBOARD_TIMEZONE", "Europe/London"))

    def render(self, show_all: bool) -> str:
        acct = self.client.get_account()
        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=300, direction="desc"))
        if not show_all:
            orders = [o for o in orders if strategy_label(o.client_order_id) != "Other / manual"]
        positions = self.client.get_all_positions()
        clock = self.client.get_clock()
        picks = []
        for w in self.client.get_watchlists():
            if w.name == "Congress Picks":
                picks = [a.symbol for a in (self.client.get_watchlist_by_id(w.id).assets or [])]

        rows = []
        for o in orders:
            when = (o.submitted_at or o.created_at)
            when = when.astimezone(self.tz).strftime("%d %b %H:%M") if when else "—"
            status = _v(o.status)
            qty = _num(o.qty) if o.qty else _money(o.notional)
            price = _money(o.filled_avg_price) if o.filled_avg_price else (
                f"limit {_money(o.limit_price)}" if o.limit_price else "market")
            rows.append(
                f"<tr><td>{when}</td><td>{html.escape(strategy_label(o.client_order_id))}</td>"
                f"<td><b>{html.escape(_describe(o.symbol))}</b></td><td>{html.escape(_v(o.side))}</td>"
                f"<td>{qty}</td><td>{price}</td>"
                f"<td><span class='st st-{html.escape(status)}'>{html.escape(status.replace('_', ' '))}</span></td></tr>"
            )
        pos_rows = []
        for p in positions:
            pl = float(p.unrealized_pl or 0)
            pos_rows.append(
                f"<tr><td><b>{html.escape(_describe(p.symbol))}</b></td><td>{_num(p.qty)}</td>"
                f"<td>{_money(p.avg_entry_price)}</td><td>{_money(p.current_price)}</td>"
                f"<td class='{'up' if pl >= 0 else 'down'}'>{_money(pl)}</td></tr>"
            )

        mode = "PAPER" if self.paper else "LIVE"
        toggle = "<a href='/'>Bot orders only</a>" if show_all else "<a href='/?all=1'>Show all orders</a>"
        empty = "<tr><td colspan='7' class='empty'>No orders yet</td></tr>"
        no_pos = "<tr><td colspan='5' class='empty'>No open positions</td></tr>"
        return PAGE.format(
            mode=mode,
            mode_cls=mode.lower(),
            market="open" if clock.is_open else "closed",
            equity=_money(acct.equity),
            cash=_money(acct.cash),
            obp=_money(getattr(acct, "options_buying_power", None)),
            orders="".join(rows) or empty,
            positions="".join(pos_rows) or no_pos,
            toggle=toggle,
            picks=" ".join(f"<span class='pick'><small>{i}</small> {html.escape(sym)}</span>"
                           for i, sym in enumerate(picks[:15], 1))
            or "<span class='muted'>No ranking yet — it is created on the first run during market hours</span>",
        )


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60"><title>Trade Log</title>
<style>
:root{{--bg:#f6f5f2;--card:#fff;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e2dc;--up:#1f7a4d;--down:#b3372c;--accent:#2f5bd3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--card:#1f1f1d;--fg:#ecebe6;--muted:#9a988f;--line:#33322f;--up:#4cc38a;--down:#ef7a6e;--accent:#7d9cf0}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1000px;margin:0 auto;padding:20px 16px 48px}}
header{{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;justify-content:space-between}}
h1{{font-size:22px;margin:0}} h2{{font-size:16px;margin:28px 0 8px}}
.badge{{font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}}
.paper{{color:var(--accent)}} .live{{color:var(--down);border-color:var(--down)}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:16px}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}}
.stat small{{color:var(--muted);display:block}} .stat b{{font-size:18px;font-variant-numeric:tabular-nums}}
.wrap{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{color:var(--muted);font-weight:500;font-size:13px}} tr:last-child td{{border-bottom:0}}
.st{{font-size:12px;padding:2px 8px;border-radius:99px;background:var(--line)}}
.st-filled{{background:color-mix(in srgb,var(--up) 18%,transparent);color:var(--up)}}
.st-canceled,.st-expired,.st-rejected{{color:var(--muted)}}
.up{{color:var(--up)}} .down{{color:var(--down)}} .empty{{color:var(--muted);text-align:center;padding:20px}}
.picks{{display:flex;flex-wrap:wrap;gap:6px}}
.pick{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:4px 10px;font-weight:600}}
.pick small{{color:var(--muted);font-weight:400}}
a{{color:var(--accent)}} .muted{{color:var(--muted);font-size:13px}}
</style></head><body><main>
<header><h1>Trade Log <span class="badge {mode_cls}">{mode}</span></h1>
<span class="muted">Market {market} · refreshes every minute</span></header>
<div class="stats">
<div class="stat"><small>Equity</small><b>{equity}</b></div>
<div class="stat"><small>Cash</small><b>{cash}</b></div>
<div class="stat"><small>Options buying power</small><b>{obp}</b></div>
</div>
<h2>Congress picks <span class="muted">· stocks politicians are buying most, best first</span></h2>
<div class="picks">{picks}</div>
<h2>Positions</h2>
<div class="wrap"><table><tr><th>Holding</th><th>Qty</th><th>Avg price</th><th>Now</th><th>Unrealised P/L</th></tr>{positions}</table></div>
<h2>Orders <span class="muted">· {toggle}</span></h2>
<div class="wrap"><table><tr><th>Time</th><th>Strategy</th><th>Contract / stock</th><th>Side</th><th>Qty</th><th>Price</th><th>Status</th></tr>{orders}</table></div>
<p class="muted">Negative quantities are options you sold. Full run-by-run logs are in the Render dashboard → cron job → Logs.</p>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    data: Data = None
    password: str = ""

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(pw, self.password)

    def do_GET(self):
        if self.path.startswith("/healthz"):
            self._send(200, "ok", "text/plain")
            return
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Trade Log"')
            self.end_headers()
            return
        try:
            body = self.data.render(show_all="all=1" in self.path)
            self._send(200, body, "text/html; charset=utf-8")
        except Exception as exc:
            log.exception("Render failed")
            self._send(500, f"Error loading data from Alpaca: {html.escape(str(exc))}", "text/plain")

    def _send(self, code, body, ctype):
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    password = os.getenv("DASHBOARD_PASSWORD", "")
    if len(password) < 8:
        raise SystemExit("Set DASHBOARD_PASSWORD (8+ characters) before starting the trade log page")
    Handler.data = Data()
    Handler.password = password
    port = int(os.getenv("PORT", "8000"))
    log.info("Trade log page on http://0.0.0.0:%d", port)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
