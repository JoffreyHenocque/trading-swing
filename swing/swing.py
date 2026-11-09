#!/usr/bin/env python3
"""Swing trading engine, paper only. Standard library only.

    python3 swing/swing.py run        # after the close: update paper positions, find new signals
    python3 swing/swing.py backtest   # replay the same rules on the past (default 3 years)
    python3 swing/swing.py report     # print the current state in plain words

Strategy (config.json, all values editable):
  Signal   a close above the highest close of the previous `breakout_days`
           sessions, with the close above its `trend_sma`-day average.
  Entry    at the next session's open (a pending order, filled on the next run).
  Stop     entry - `atr_mult` x ATR(14) of the signal day.
  Target   entry + `target_r` x the risk (R).
  Time     out at the close after `max_hold` sessions if neither was hit.
  Size     risk `risk_pct` of equity per trade, whole shares, capped at
           `max_position_pct` of equity and the cash available.
  Costs    broker fee per side (pct with a minimum) + French financial
           transaction tax on buys of taxed French shares + slippage.
  Same-day stop and target: the stop is assumed first (pessimistic).

Data: Yahoo Finance daily bars (free, unofficial). Offline: --offline DIR
reads DIR/<SYMBOL>.json chart files. Outputs go to data/.

Paper trading only: this script never talks to a broker. Not financial advice.
"""

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.json")
DATA = os.path.join(ROOT, "data")
UA = "Mozilla/5.0 (compatible; swing-paper/1.0)"


# ---------------------------------------------------------------- io

def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def read_universe(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                sym, _, name = ln.partition("|")
                out.append((sym.strip().upper(), name.strip() or sym.strip()))
    return out


# ---------------------------------------------------------------- data

def fetch_chart(sym, rng, pause):
    last = "no answer"
    for attempt in range(4):
        host = ("query2", "query1")[attempt % 2]
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (
            host, urllib.parse.quote(sym), rng)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # 429s and timeouts: back off and retry
            last = str(e)
            time.sleep(pause * (attempt + 2) * 2)
    raise RuntimeError("%s: %s" % (sym, last))


def bars_from_chart(doc):
    res = (doc.get("chart") or {}).get("result") or []
    if not res:
        return []
    r = res[0]
    q = (r.get("indicators", {}).get("quote") or [{}])[0]
    off = (r.get("meta") or {}).get("gmtoffset") or 0
    bars = []
    for i, ts in enumerate(r.get("timestamp") or []):
        o, h, l, c = (q.get(k, [None])[i] if i < len(q.get(k) or []) else None
                      for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        day = (dt.datetime(1970, 1, 1) + dt.timedelta(seconds=ts + off)).date().isoformat()
        if bars and bars[-1]["d"] == day:
            bars[-1] = {"d": day, "o": o, "h": h, "l": l, "c": c}
        else:
            bars.append({"d": day, "o": o, "h": h, "l": l, "c": c})
    return bars


def load_bars(universe, cfg, offline=None, rng="5y"):
    bars, errors = {}, []
    for sym, _ in universe:
        try:
            if offline:
                doc = load_json(os.path.join(offline, sym + ".json"))
                if doc is None:
                    raise RuntimeError("no offline file")
            else:
                doc = fetch_chart(sym, rng, cfg.get("pause_s", 1.0))
                time.sleep(cfg.get("pause_s", 1.0))
            b = bars_from_chart(doc)
            if len(b) < cfg["trend_sma"] + 20:
                raise RuntimeError("only %d daily bars" % len(b))
            bars[sym] = b
        except Exception as e:
            errors.append("%s: %s" % (sym, e))
    return bars, errors


# ---------------------------------------------------------------- indicators

def atr(b, i, n=14):
    if i < n:
        return None
    trs = []
    for k in range(i - n + 1, i + 1):
        pc = b[k - 1]["c"]
        trs.append(max(b[k]["h"] - b[k]["l"], abs(b[k]["h"] - pc), abs(b[k]["l"] - pc)))
    return sum(trs) / n


def signal(b, i, cfg):
    """A breakout signal on bar i (its close), or None."""
    n, t = cfg["breakout_days"], cfg["trend_sma"]
    if i < max(n, t, 15):
        return None
    prior_high = max(x["c"] for x in b[i - n:i])
    sma = sum(x["c"] for x in b[i - t + 1:i + 1]) / t
    c = b[i]["c"]
    if c <= prior_high or c <= sma:
        return None
    a = atr(b, i)
    if not a:
        return None
    return {"date": b[i]["d"], "close": round(c, 4), "prior_high": round(prior_high, 4),
            "sma": round(sma, 4), "atr": round(a, 4), "strength": round((c - prior_high) / a, 3)}


# ---------------------------------------------------------------- costs

def fee(value, cfg):
    return max(value * cfg["fee_pct"] / 100.0, cfg["fee_min"])


def buy_tax(sym, value, cfg):
    if sym.endswith(".PA") and sym not in set(cfg.get("ttf_exempt", [])):
        return value * cfg["ttf_pct"] / 100.0
    return 0.0


# ---------------------------------------------------------------- engine

class Book(object):
    """Paper account: cash, open positions, pending orders, closed trades."""

    def __init__(self, cfg, state=None):
        s = state or {}
        self.cfg = cfg
        self.cash = s.get("cash", cfg["capital"])
        self.positions = s.get("positions", [])
        self.pending = s.get("pending", [])
        self.trades = s.get("trades", [])
        self.equity = s.get("equity", [])
        self.last_date = s.get("last_date")
        self.log = []

    def state(self):
        return {"cash": round(self.cash, 2), "positions": self.positions, "pending": self.pending,
                "trades": self.trades, "equity": self.equity, "last_date": self.last_date}

    def mark(self, bars, day):
        value = 0.0
        for p in self.positions:
            b = bar_on(bars.get(p["symbol"]), day) or {"c": p["last"]}
            p["last"] = round(b["c"], 4)
            value += p["qty"] * b["c"]
        eq = round(self.cash + value, 2)
        if self.equity and self.equity[-1]["d"] == day:
            self.equity[-1] = {"d": day, "equity": eq, "cash": round(self.cash, 2)}
        else:
            self.equity.append({"d": day, "equity": eq, "cash": round(self.cash, 2)})
        return eq

    def close(self, p, price, day, reason):
        slip = self.cfg["slippage_pct"] / 100.0
        px = price * (1 - slip)
        value = p["qty"] * px
        f = fee(value, self.cfg)
        self.cash += value - f
        pnl = value - f - p["cost"]
        r = pnl / (p["qty"] * p["risk_per_share"]) if p["risk_per_share"] else 0   # net of all costs
        t = dict(p, exit=round(px, 4), exit_date=day, reason=reason, exit_fee=round(f, 2),
                 pnl=round(pnl, 2), r=round(r, 3), pnl_pct=round(100 * pnl / p["cost"], 2))
        t.pop("last", None)
        self.trades.append(t)
        self.log.append("%s close %s %d @ %.2f (%s): %+.2f EUR, %+.2fR" % (
            day, p["symbol"], p["qty"], px, reason, pnl, r))

    def step(self, bars, day):
        """Process one session `day`: fill pending, manage positions, mark, new signals."""
        cfg = self.cfg
        # 1. fill pending orders at today's open
        still = []
        for o in self.pending:
            b = bar_on(bars.get(o["symbol"]), day)
            if not b:
                o["age"] = o.get("age", 0) + 1
                if o["age"] <= 3:
                    still.append(o)
                continue
            self.open_position(o, b, day)
        self.pending = still
        # 2. manage open positions on today's bar
        keep = []
        for p in self.positions:
            b = bar_on(bars.get(p["symbol"]), day)
            if not b:
                keep.append(p)
                continue
            if p["entry_date"] != day:
                p["held"] = p.get("held", 0) + 1
            if b["o"] <= p["stop"]:
                self.close(p, b["o"], day, "stop (gap)")
            elif b["l"] <= p["stop"]:
                self.close(p, p["stop"], day, "stop")
            elif b["o"] >= p["target"]:
                self.close(p, b["o"], day, "target (gap)")
            elif b["h"] >= p["target"]:
                self.close(p, p["target"], day, "target")
            elif p["held"] >= cfg["max_hold"]:
                self.close(p, b["c"], day, "time")
            else:
                keep.append(p)
        self.positions = keep
        eq = self.mark(bars, day)
        # 3. new signals on today's close -> pending orders for the next open
        busy = {p["symbol"] for p in self.positions} | {o["symbol"] for o in self.pending}
        slots = cfg["max_positions"] - len(self.positions) - len(self.pending)
        cands = []
        for sym, b in bars.items():
            if sym in busy or sym in cfg.get("blocked", []) or sym == cfg.get("benchmark"):
                continue
            i = index_on(b, day)
            if i is None:
                continue
            s = signal(b, i, cfg)
            if s:
                cands.append(dict(s, symbol=sym))
        cands.sort(key=lambda s: -s["strength"])
        new = []
        for s in cands:
            if slots <= 0:
                break
            order = self.size(s, eq)
            if order:
                self.pending.append(order)
                new.append(order)
                slots -= 1
        self.last_date = day
        return cands, new

    def size(self, s, equity):
        cfg = self.cfg
        risk_ps = cfg["atr_mult"] * s["atr"]
        if risk_ps <= 0:
            return None
        risk_eur = equity * cfg["risk_pct"] / 100.0
        qty = int(risk_eur // risk_ps)
        cap = min(equity * cfg["max_position_pct"] / 100.0, self.cash * 0.98)
        qty = min(qty, int(cap // (s["close"] * 1.01)))
        if qty < 1:
            self.log.append("%s skip %s: one share (%.2f) does not fit the risk or position limits"
                            % (s["date"], s["symbol"], s["close"]))
            return None
        return {"symbol": s["symbol"], "qty": qty, "signal": s, "risk_per_share": round(risk_ps, 4),
                "created": s["date"], "age": 0}

    def open_position(self, o, b, day):
        cfg = self.cfg
        px = b["o"] * (1 + cfg["slippage_pct"] / 100.0)
        qty = o["qty"]
        value = qty * px
        f = fee(value, cfg)
        tax = buy_tax(o["symbol"], value, cfg)
        if value + f + tax > self.cash:
            qty = int((self.cash - f - tax) // (px * 1.01))
            if qty < 1:
                self.log.append("%s skip %s: not enough cash at the open" % (day, o["symbol"]))
                return
            value = qty * px
            f, tax = fee(value, cfg), buy_tax(o["symbol"], value, cfg)
        rps = o["risk_per_share"]
        stop = round(px - rps, 2)
        target = round(px + cfg["target_r"] * rps, 2)
        self.cash -= value + f + tax
        p = {"symbol": o["symbol"], "qty": qty, "entry": round(px, 4), "entry_date": day, "stop": stop,
             "target": target, "risk_per_share": rps, "cost": round(value + f + tax, 2),
             "entry_fee": round(f, 2), "entry_tax": round(tax, 2), "held": 0, "last": round(b["c"], 4),
             "signal_date": o["created"]}
        self.positions.append(p)
        self.log.append("%s open %s %d @ %.2f, stop %.2f, target %.2f (fee %.2f, tax %.2f)" % (
            day, o["symbol"], qty, px, stop, target, f, tax))
        # the entry bar's own low and high are checked by step() right after (stop first)


def bar_on(b, day):
    i = index_on(b, day) if b else None
    return b[i] if i is not None else None


def index_on(b, day):
    lo, hi = 0, len(b) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if b[mid]["d"] == day:
            return mid
        if b[mid]["d"] < day:
            lo = mid + 1
        else:
            hi = mid - 1
    return None


def all_days(bars):
    return sorted({x["d"] for b in bars.values() for x in b})


# ---------------------------------------------------------------- stats

def stats(trades, equity, capital):
    closed = trades
    wins = [t for t in closed if t["pnl"] > 0]
    rs = [t["r"] for t in closed]
    peak, mdd = capital, 0.0
    for e in equity:
        peak = max(peak, e["equity"])
        mdd = min(mdd, (e["equity"] - peak) / peak)
    fees = sum(t.get("entry_fee", 0) + t.get("exit_fee", 0) + t.get("entry_tax", 0) for t in closed)
    last = equity[-1]["equity"] if equity else capital
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in closed if t["pnl"] <= 0)
    return {"trades": len(closed), "wins": len(wins),
            "win_rate": round(100.0 * len(wins) / len(closed), 1) if closed else None,
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "total_pnl": round(sum(t["pnl"] for t in closed), 2),
            "costs": round(fees, 2), "equity": round(last, 2),
            "return_pct": round(100.0 * (last - capital) / capital, 2),
            "max_drawdown_pct": round(100.0 * mdd, 2)}


def benchmark(bars, sym, start, end):
    b = bars.get(sym)
    if not b:
        return None
    i0 = next((i for i, x in enumerate(b) if x["d"] >= start), None)
    i1 = index_on(b, end) if end else len(b) - 1
    if i0 is None or i1 is None:
        return None
    return round(100.0 * (b[i1]["c"] / b[i0]["o"] - 1), 2)



# ---------------------------------------------------------------- monthly strategies (lab)

def month_ends(days):
    """Last trading day of each month in `days` (sorted ISO dates)."""
    out = []
    for i, d in enumerate(days):
        if i + 1 == len(days) or days[i + 1][:7] != d[:7]:
            out.append(d)
    return out


def month_starts(days):
    return [d for i, d in enumerate(days) if i == 0 or days[i - 1][:7] != d[:7]]


def sma_at(b, i, n):
    if i + 1 < n:
        return None
    return sum(x["c"] for x in b[i - n + 1:i + 1]) / n


def ret_at(b, i, n):
    if i < n:
        return None
    return b[i]["c"] / b[i - n]["c"] - 1


def targets_for(v, bars, day):
    """Symbols to hold after the month-end `day`, for variant v."""
    kind = v["kind"]
    if kind == "buy_hold":
        return list(v["symbols"])
    if kind == "trend":
        keep = []
        for sym in v["symbols"]:
            b = bars.get(sym)
            i = index_on(b, day) if b else None
            if i is None:
                continue
            m = sma_at(b, i, v.get("sma", 200))
            if m is not None and b[i]["c"] > m:
                keep.append(sym)
        return keep
    if kind == "momentum":
        ranked = []
        for sym in v["symbols"]:
            b = bars.get(sym)
            i = index_on(b, day) if b else None
            if i is None:
                continue
            r = ret_at(b, i, v.get("lookback", 126))
            m = sma_at(b, i, v.get("sma", 200))
            if r is None or m is None or b[i]["c"] <= m or r <= 0:
                continue
            ranked.append((r, sym))
        ranked.sort(reverse=True)
        return [s for _, s in ranked[:v.get("top", 3)]]
    raise ValueError("unknown kind %s" % kind)


def run_monthly(v, bars, days, cfg):
    """Monthly rebalancing on the next open after each month end; or monthly
    contributions (kind dca). Whole shares, costs included, cash carried."""
    c = dict(cfg, **v.get("costs", {}))
    cash = float(v.get("capital", cfg["capital"]))
    contributed = cash
    pos = {}                     # sym -> qty
    costs = 0.0
    trades = 0
    equity = []
    ends = set(month_ends(days))
    starts = set(month_starts(days))
    pending = None
    first = True

    def px(sym, d, k="o"):
        b = bar_on(bars.get(sym), d)
        return b[k] if b else None

    def buy(sym, budget, d):
        nonlocal cash, costs, trades
        p = px(sym, d)
        if not p:
            return
        p *= 1 + c["slippage_pct"] / 100.0
        tax_rate = 0 if v.get("no_ttf") else (c["ttf_pct"] / 100.0 if sym.endswith(".PA") and sym not in set(c.get("ttf_exempt", [])) else 0)
        q = int(budget // (p * (1 + tax_rate) * 1.003))
        while q > 0 and q * p * (1 + tax_rate) + fee(q * p, c) > cash:
            q -= 1
        if q < 1:
            return
        f, t = fee(q * p, c), q * p * tax_rate
        cash -= q * p + f + t
        costs += f + t
        trades += 1
        pos[sym] = pos.get(sym, 0) + q

    def sell(sym, d):
        nonlocal cash, costs, trades
        p = px(sym, d)
        if not p:
            return
        p *= 1 - c["slippage_pct"] / 100.0
        q = pos.pop(sym)
        f = fee(q * p, c)
        cash += q * p - f
        costs += f
        trades += 1

    for d in days:
        if v["kind"] == "dca":
            if d in starts:
                if not first:
                    cash += v["monthly"]
                    contributed += v["monthly"]
                first = False
                for sym in v["symbols"]:
                    buy(sym, cash / len(v["symbols"]), d)
        elif pending is not None:
            for sym in [s for s in pos if s not in pending]:
                sell(sym, d)
            new = [s for s in pending if s not in pos]
            if new:
                value = cash + sum(q * (px(s, d) or 0) for s, q in pos.items())
                slot = value / max(len(pending), 1)
                for sym in new:
                    buy(sym, min(slot, cash), d)
            pending = None
        if v["kind"] != "dca" and (d in ends or (first and v["kind"] == "buy_hold")):
            pending = targets_for(v, bars, d)
            first = False
        val = cash
        for s, q in pos.items():
            b = bar_on(bars.get(s), d)
            if b:
                val += q * b["c"]
            else:
                val += q * (equity_last_price(bars.get(s), d) or 0)
        equity.append({"d": d, "equity": round(val, 2), "contributed": round(contributed, 2)})
    peak, mdd = 0.0, 0.0
    for e in equity:
        ratio = e["equity"] / e["contributed"]
        peak = max(peak, ratio)
        mdd = min(mdd, ratio / peak - 1) if peak else mdd
    last = equity[-1]
    return {"name": v["name"], "label": v["label"], "kind": v["kind"],
            "contributed": last["contributed"], "final": last["equity"],
            "return_pct": round(100.0 * (last["equity"] / last["contributed"] - 1), 2),
            "costs": round(costs, 2), "trades": trades,
            "max_drawdown_pct": round(100.0 * mdd, 2),
            "holdings": {s: q for s, q in pos.items()},
            "equity": equity}


def equity_last_price(b, d):
    if not b:
        return None
    last = None
    for x in b:
        if x["d"] > d:
            break
        last = x["c"]
    return last


def compare(bars, days, cfg, breakout_doc, stocks):
    rows = []
    s = breakout_doc["stats"]
    rows.append({"name": "breakout", "label": "Swing cassure 20 jours (actions CAC 40)", "kind": "swing",
                 "contributed": cfg["capital"], "final": s["equity"], "return_pct": s["return_pct"],
                 "costs": s["costs"], "trades": s["trades"], "max_drawdown_pct": s["max_drawdown_pct"],
                 "equity": breakout_doc["full_equity"]})
    for v in cfg.get("variants", []):
        try:
            v2 = dict(v, symbols=stocks) if v.get("symbols") == "universe" else v
            rows.append(run_monthly(v2, bars, days, cfg))
        except Exception as e:
            rows.append({"name": v.get("name"), "label": v.get("label"), "error": str(e)})
    for r in rows:
        eq = r.pop("equity", None) or []
        step = max(1, len(eq) // 160)
        r["curve"] = [{"d": e["d"], "pct": round(100.0 * (e["equity"] / e.get("contributed", cfg["capital"]) - 1), 2)}
                      for e in eq[::step] + eq[-1:]]
    return rows

# ---------------------------------------------------------------- commands

def cmd_run(a, cfg, universe):
    bars, errors = load_bars(universe, cfg, a.offline, "1y")
    if not bars:
        print("No data at all: %s" % "; ".join(errors[:5]))
        return 1
    statep = os.path.join(DATA, "state.json")
    book = Book(cfg, load_json(statep))
    days = all_days(bars)
    if a.until:
        days = [d for d in days if d <= a.until]
    todo = [d for d in days if not book.last_date or d > book.last_date]
    if not book.last_date:
        todo = todo[-1:]     # first run: start from the latest session, no back-fill
    cands, new = [], []
    for d in todo:
        cands, new = book.step(bars, d)
    save_json(statep, book.state())
    day = book.last_date
    snap = {"as_of": day, "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "config": cfg, "stats": stats(book.trades, book.equity, cfg["capital"]),
            "positions": book.positions, "pending": book.pending, "signals": cands,
            "recent_trades": book.trades[-20:], "equity": book.equity, "log": book.log,
            "data_errors": errors}
    save_json(os.path.join(DATA, "latest.json"), snap)
    hist = load_json(os.path.join(DATA, "log.json"), [])
    hist.extend(book.log)
    save_json(os.path.join(DATA, "log.json"), hist[-500:])
    print_report(snap)
    return 0


def cmd_backtest(a, cfg, universe):
    extra = sorted({x for v in cfg.get("variants", []) if isinstance(v.get("symbols"), list)
                    for x in v["symbols"]} - {u[0] for u in universe})
    bars, errors = load_bars(universe + [(x, x) for x in extra], cfg, a.offline, "5y")
    lab = {x: bars.pop(x) for x in extra if x in bars}
    if not bars:
        print("No data at all: %s" % "; ".join(errors[:5]))
        return 1
    days = all_days(bars)
    start = (dt.date.fromisoformat(days[-1]) - dt.timedelta(days=int(365 * a.years))).isoformat()
    days = [d for d in days if d >= start]
    book = Book(cfg)
    for d in days:
        book.step(bars, d)
    for p in list(book.positions):          # close what is left at the last close
        b = bar_on(bars[p["symbol"]], days[-1])
        book.close(p, b["c"], days[-1], "end of test")
    book.positions = []
    book.mark(bars, days[-1])
    s = stats(book.trades, book.equity, cfg["capital"])
    by_year = {}
    for t in book.trades:
        y = t["exit_date"][:4]
        by_year.setdefault(y, []).append(t)
    years = {y: {"trades": len(ts), "pnl": round(sum(t["pnl"] for t in ts), 2),
                 "win_rate": round(100.0 * sum(1 for t in ts if t["pnl"] > 0) / len(ts), 1)}
             for y, ts in sorted(by_year.items())}
    doc = {"generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "from": days[0], "to": days[-1], "years": a.years, "config": cfg, "stats": s,
           "by_year": years, "benchmark": {cfg.get("benchmark", "^FCHI"): benchmark(
               bars, cfg.get("benchmark", "^FCHI"), days[0], days[-1])},
           "equity": book.equity[::max(1, len(book.equity) // 400)] + book.equity[-1:],
           "trades": book.trades, "symbols": len(bars), "data_errors": errors}
    full_equity = list(book.equity)
    save_json(os.path.join(DATA, "backtest.json"), doc)
    bars.update(lab)
    stocks = [u[0] for u in universe if u[0] != cfg.get("benchmark") and u[0] in bars]
    rows = compare(bars, days, cfg, dict(doc, full_equity=full_equity), stocks)
    save_json(os.path.join(DATA, "compare.json"), {
        "generated_utc": doc["generated_utc"], "from": days[0], "to": days[-1], "rows": rows})
    print("\nStrategy lab, %s to %s, costs included:" % (days[0], days[-1]))
    for r in rows:
        if r.get("error"):
            print("  %-34s error: %s" % (r["label"], r["error"]))
            continue
        print("  %-48s %+7.2f%%  (%d trades, costs %.2f EUR, put in %.0f EUR, worth %.2f EUR, max drawdown %.1f%%)" % (
            r["label"], r["return_pct"], r["trades"], r["costs"], r["contributed"], r["final"], r["max_drawdown_pct"]))
    print("Backtest %s to %s on %d stocks (paper, costs included)." % (days[0], days[-1], doc["symbols"]))
    print_stats(s, cfg)
    for y, v in years.items():
        print("  %s: %d trades, win rate %.1f%%, P&L %+.2f EUR" % (y, v["trades"], v["win_rate"], v["pnl"]))
    bm = list(doc["benchmark"].values())[0]
    if bm is not None:
        print("Buy and hold of the index over the same period: %+.2f%% (no dividends)." % bm)
    print(verdict(s))
    return 0


def print_stats(s, cfg):
    if not s["trades"]:
        print("No closed trade.")
        return
    print("%d trades, win rate %.1f%%, average %+.2fR, profit factor %s." % (
        s["trades"], s["win_rate"], s["avg_r"], s["profit_factor"]))
    print("P&L %+.2f EUR on %.0f EUR (%+.2f%%), costs paid %.2f EUR, max drawdown %.2f%%." % (
        s["total_pnl"], cfg["capital"], s["return_pct"], s["costs"], s["max_drawdown_pct"]))


def verdict(s):
    if s["trades"] < 30:
        return "Verdict: fewer than 30 trades, too few to judge. Keep it on paper."
    if (s["avg_r"] or 0) <= 0.1 or (s["profit_factor"] or 0) < 1.2:
        return "Verdict: no edge after costs (average R %+.2f, profit factor %s). No real money." % (
            s["avg_r"] or 0, s["profit_factor"])
    return ("Verdict: positive after costs on %d trades. Past results, not a promise: "
            "confirm on paper going forward before any real money, and start small." % s["trades"])


def print_report(snap):
    cfg = snap["config"]
    print("Paper account as of %s." % snap["as_of"])
    print_stats(snap["stats"], cfg)
    print("Equity %.2f EUR, cash %.2f EUR." % (snap["stats"]["equity"],
                                               snap["equity"][-1]["cash"] if snap["equity"] else cfg["capital"]))
    for p in snap["positions"]:
        print("  open %s x%d since %s: entry %.2f, last %.2f, stop %.2f, target %.2f" % (
            p["symbol"], p["qty"], p["entry_date"], p["entry"], p["last"], p["stop"], p["target"]))
    for o in snap["pending"]:
        print("  pending buy %s x%d at the next open (signal %s)" % (o["symbol"], o["qty"], o["created"]))
    for line in snap["log"]:
        print("  " + line)
    if snap["data_errors"]:
        print("Data errors: %s" % "; ".join(snap["data_errors"][:8]))
    print(verdict(snap["stats"]))


def cmd_report(a, cfg, universe):
    snap = load_json(os.path.join(DATA, "latest.json"))
    if not snap:
        print("No run yet.")
        return 1
    print_report(snap)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Swing trading engine, paper only.")
    ap.add_argument("cmd", choices=["run", "backtest", "report"])
    ap.add_argument("--offline", help="folder of <SYMBOL>.json Yahoo chart files")
    ap.add_argument("--years", type=float, default=3)
    ap.add_argument("--until", help="run: stop at this date (tests)")
    ap.add_argument("--config", default=CONFIG)
    a = ap.parse_args(argv)
    cfg = load_json(a.config)
    universe = read_universe(os.path.join(ROOT, cfg.get("universe", "universe.txt")))
    bm = cfg.get("benchmark")
    if bm and bm not in [u[0] for u in universe]:
        universe.append((bm, "benchmark"))
    if a.cmd == "run":
        universe = [u for u in universe if u[0] != bm] + ([(bm, "benchmark")] if bm else [])
    return {"run": cmd_run, "backtest": cmd_backtest, "report": cmd_report}[a.cmd](a, cfg, universe)


if __name__ == "__main__":
    sys.exit(main())
