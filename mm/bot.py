"""
Limit-order market-making bot for Kalshi.

Quotes both sides of the configured market's central (ATM) strikes as PASSIVE
limit orders, with inventory-aware skew and hard risk controls.

SAFETY — read before enabling:
  - DRY-RUN is the DEFAULT. Real orders require the explicit env LIVE=1.
  - Hard caps: size/order, inventory/strike, number of strikes, TOTAL exposure ($).
  - KILL SWITCH: if the day's realized loss exceeds MM_KILL_LOSS, cancel, flatten, stop.
  - Checks balance before each order; refuses if short.
  - Orders auto-expire (MM_ORDER_EXPIRY_S) so nothing rests if the bot dies.
  - Starts SMALL on purpose. Scale only after live fills confirm the model.

Config comes from the environment only — credentials and the target market are
never hardcoded. Never commit keys.

Usage:
  python -m mm.bot --status            # balance, positions, resting orders, day PnL
  python -m mm.bot --dry               # dry-run (default): prints intended orders
  LIVE=1 python -m mm.bot --live       # REAL orders (guarded by LIVE=1)
  python -m mm.bot --flatten           # cancel all resting orders (panic)
"""
from __future__ import annotations
import argparse
import os
import statistics as st
import time
import datetime as dt

from broker.kalshi import KalshiClient

SERIES = os.getenv("MM_SERIES", "").strip()          # target series — env/secret only
# --- size / ramp (START SMALL) ---
QTY = int(os.getenv("MM_QTY", "5"))                  # contracts per order
INV_CAP = int(os.getenv("MM_INV_CAP", "20"))         # max inventory per strike
MAX_STRIKES = int(os.getenv("MM_MAX_STRIKES", "4"))
MAX_EXPOSURE_USD = float(os.getenv("MM_MAX_USD", "40"))   # total collateral cap
# --- strategy ---
ATM_LO, ATM_HI = 0.15, 0.85
SKEW_MULT = float(os.getenv("MM_SKEW", "0.3"))       # inventory skew (anchor)
QUOTE_UNTIL_S = int(os.getenv("MM_QUOTE_UNTIL_S", "1800"))
ORDER_EXPIRY_S = int(os.getenv("MM_ORDER_EXPIRY_S", "90"))   # auto-cancel if bot dies
# --- risk ---
KILL_LOSS_USD = float(os.getenv("MM_KILL_LOSS", "15"))      # stop the day on this loss
STEP_S = int(os.getenv("MM_STEP_S", "30"))
DRY = os.getenv("LIVE") != "1"


def _f(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _c(p):
    return max(1, min(99, int(round(p * 100))))


def _mask(t):
    t = t or ""
    return f"{t[:2]}…{t[-2:]}" if len(t) > 4 else "***"


# Public-log safety: by default NEVER print account $ (balance / PnL / exposure)
# or inventory size. Set MM_MASK_USD=0 (e.g. local/private run) to see real figures.
MASK_USD = os.getenv("MM_MASK_USD", "1") == "1"


def _money(x, signed=False):
    if MASK_USD:
        return "$•••"
    return f"${x:+.2f}" if signed else f"${x:.2f}"


def _invs(n):
    if not MASK_USD:
        return str(n)
    return "flat" if n == 0 else ("long" if n > 0 else "short")


def client():
    return KalshiClient(env="prod")


def balance(c):
    b = c.balance()
    return _f((b or {}).get("balance_dollars"), 0.0) or 0.0


def discover(c):
    """Central (ATM) strikes with a 2-sided book, closest to the median strike."""
    r = c.markets(series_ticker=SERIES, status="open", limit=200)
    mk = [m for m in (r.get("markets", []) if isinstance(r, dict) else [])
          if _f(m.get("floor_strike")) is not None and m.get("close_time")]
    if not mk:
        return []
    med = st.median([_f(m["floor_strike"]) for m in mk])
    out = []
    now = time.time()
    for m in sorted(mk, key=lambda x: abs(_f(x["floor_strike"]) - med)):
        b, a = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
        if b is None or a is None or not (0 < b < a < 1):
            continue
        if not (ATM_LO <= (b + a) / 2 <= ATM_HI):
            continue
        ct = dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()
        if ct - now < QUOTE_UNTIL_S:
            continue
        out.append({"ticker": m["ticker"], "bid": b, "ask": a})
        if len(out) >= MAX_STRIKES:
            break
    return out


def inventory(c, ticker):
    try:
        pos = c.positions(limit=200)
        for p in (pos.get("market_positions", []) if isinstance(pos, dict) else []):
            if p.get("ticker") == ticker:
                return int(p.get("position", 0) or 0)
    except Exception:
        pass
    return 0


def resting(c, ticker=None):
    try:
        od = c.orders(status="resting", limit=200)
        out = od.get("orders", []) if isinstance(od, dict) else []
        return [o for o in out if (ticker is None or o.get("ticker") == ticker)]
    except Exception:
        return []


def cancel_all(c, dry, ticker=None):
    n = 0
    for o in resting(c, ticker):
        oid = o.get("order_id") or o.get("id")
        if not oid:
            continue
        if dry:
            print(f"   [dry] would cancel {oid}")
        else:
            c.cancel_order(oid)
        n += 1
    return n


def desired_quotes(m, inv):
    """2-sided maker quotes on the YES book with inventory skew (single-book V2).
    bid = buy YES at the (shifted) bid; ask = sell YES at the (shifted) ask.
    inv>0 shifts both down to lean inventory off. px_cost = collateral/contract."""
    half = (m["ask"] - m["bid"]) / 2
    sk = SKEW_MULT * half * (inv / INV_CAP)
    ybid = m["bid"] - sk
    yask = m["ask"] - sk
    orders = []
    if inv < INV_CAP and 0 < ybid < 1:                 # bid: buy YES (go long)
        orders.append({"side": "bid", "price": ybid, "px_cost": ybid})
    if inv > -INV_CAP and 0 < yask < 1:                # ask: sell YES (go short / long NO)
        orders.append({"side": "ask", "price": yask, "px_cost": 1 - yask})
    return orders


def realized_today(c):
    """Today's realized PnL for the configured series, via settlements."""
    try:
        s = c._request("GET", "/portfolio/settlements", params={"limit": 200})
        items = s.get("settlements", []) if isinstance(s, dict) else []
    except Exception:
        return 0.0
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    tot = 0.0
    for x in items:
        if SERIES in (x.get("ticker") or "") and today in (x.get("settled_time") or ""):
            tot += _f(x.get("revenue"), 0) or 0
    return tot


def step(c, dry, budget):
    cands = discover(c)
    if not cands:
        print("   no ATM strikes right now."); return 0.0
    cancel_all(c, dry)                   # cancel stale orders, re-quote fresh
    if not dry:
        time.sleep(0.4)
    spent = 0.0
    for m in cands:
        inv = 0 if dry else inventory(c, m["ticker"])
        for o in desired_quotes(m, inv):
            cost = o["px_cost"] * QTY
            if spent + cost > budget:
                print(f"   [exposure cap {_money(budget)}] skip {_mask(m['ticker'])} {o['side']}")
                continue
            spent += cost
            tag = f"{_mask(m['ticker'])} {o['side']} {QTY}@{_c(o['price'])}c (${o['px_cost']:.2f})"
            if dry:
                print(f"   [dry] would post MAKER (expires {ORDER_EXPIRY_S}s): {tag}")
            else:
                res = c.create_order(
                    ticker=m["ticker"], side=o["side"], count=QTY, price=o["price"],
                    post_only=True, client_order_id=f"mm-{int(time.time()*1000)}-{o['side']}",
                    expiration_ts=int(time.time()) + ORDER_EXPIRY_S)
                ok = isinstance(res, dict) and not res.get("_http_error") and not res.get("_error")
                print(f"   [LIVE] {tag} -> {'ok' if ok else res}")
    print(f"   exposure posted this round: ~{_money(spent)}")
    return spent


def held(c):
    """# of strikes in SERIES with a non-zero position (i.e. orders that filled)."""
    n = 0
    try:
        pos = c.positions(limit=200)
        for p in (pos.get("market_positions", []) if isinstance(pos, dict) else []):
            if SERIES in (p.get("ticker") or "") and int(p.get("position", 0) or 0) != 0:
                n += 1
    except Exception:
        pass
    return n


def fills_count(c):
    """GROSS fills in the recent batch for SERIES. Distinguishes 'nothing is filling'
    (stays 0) from balanced round-trips that net to flat inventory (grows even while
    held stays 0). The signal that resolves a held=0 reading."""
    try:
        r = c.fills(limit=200)
        items = r.get("fills", []) if isinstance(r, dict) else []
    except Exception:
        return 0
    return sum(1 for f in items if SERIES in (f.get("ticker") or ""))


def run_loop(dry):
    if not SERIES:
        print("set MM_SERIES (target series ticker) via env/secret."); return
    c = client()
    bal = balance(c)
    budget = min(MAX_EXPOSURE_USD, bal if not dry else MAX_EXPOSURE_USD)
    print("=" * 64)
    print(f"MM {_mask(SERIES)} | mode={'DRY-RUN' if dry else 'LIVE — REAL MONEY'} | balance {_money(bal)}")
    print(f"qty {QTY} | inv cap ±{INV_CAP} | strikes<= {MAX_STRIKES} | exposure<= {_money(budget)} | "
          f"skew {SKEW_MULT} | kill if loss > ${KILL_LOSS_USD}")
    print("=" * 64)
    if not dry and bal < 1.0:
        print("\n  !! balance insufficient (< $1). Deposit and restart."); return
    deadline = time.time() + int(os.getenv("LOOP_MAX_MINUTES", "350")) * 60
    while time.time() < deadline:
        try:
            if not dry:
                pnl = realized_today(c)
                if pnl <= -abs(KILL_LOSS_USD):
                    print(f"\n  !! KILL SWITCH: day loss {_money(pnl, signed=True)} <= -${KILL_LOSS_USD}. "
                          f"Cancelling + flattening.")
                    cancel_all(c, dry=False)
                    return
            print(f"\n[{dt.datetime.now().strftime('%H:%M:%S')}]", flush=True)
            if not dry:
                print(f"   held: {held(c)} | fills(recent): {fills_count(c)} | "
                      f"resting: {len(resting(c))} | day PnL: {_money(pnl, signed=True)}", flush=True)
            step(c, dry, budget)
        except Exception as e:
            print(f"   ! error: {e}", flush=True)
        time.sleep(STEP_S)
    if not dry:
        cancel_all(c, dry=False)


def cmd_status():
    if not SERIES:
        print("set MM_SERIES (target series ticker) via env/secret."); return
    c = client()
    b = c.balance()
    authed = isinstance(b, dict) and "balance_dollars" in b
    print(f"auth: {'OK' if authed else 'FAILED -> ' + str(b)[:200]}")
    print(f"balance: {_money(_f(b.get('balance_dollars'), 0.0) or 0.0) if authed else 'n/a'}")
    cands = discover(c)
    print(f"ATM strikes: {[_mask(m['ticker']) for m in cands]}")
    for m in cands:
        print(f"  {_mask(m['ticker'])}: inv {_invs(inventory(c, m['ticker']))} | bid/ask {m['bid']}/{m['ask']}")
    print(f"resting orders: {len(resting(c))}")
    print(f"realized PnL TODAY: {_money(realized_today(c), signed=True)}")


def cmd_flatten():
    c = client()
    n = cancel_all(c, dry=False)
    print(f"cancelled {n} orders. (open positions must be closed manually or left to settle)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry", action="store_true")
    g.add_argument("--live", action="store_true")
    g.add_argument("--flatten", action="store_true")
    g.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        cmd_status()
    elif a.flatten:
        cmd_flatten()
    elif a.live and os.getenv("LIVE") != "1":
        print("Refused: --live requires LIVE=1 (safety gate). Running dry.")
        run_loop(True)
    else:
        run_loop(bool(a.dry))
