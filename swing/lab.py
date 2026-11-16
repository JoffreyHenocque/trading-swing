#!/usr/bin/env python3
"""Strategy lab v2: many strategies, parameter grids, walk-forward, real budget.

    python3 swing/lab.py backtest     # every strategy x every setting, in and out of sample
    python3 swing/lab.py signals      # what each family's chosen setting would hold today

The question every run answers: with the real budget (lab.json "monthly", e.g.
75 EUR a month) and Interactive Brokers' real costs, does a strategy beat the
same money put into the world ETF every month?

Method
  - Money: the budget arrives on the first trading day of each month. Cash
    waits until an order is worth at least "min_order_eur".
  - Costs: per market (Euronext or US), slippage, the French financial
    transaction tax on taxed French shares, a currency-conversion cost on money
    sent to the US market. US shares can be bought in fractions; others whole.
  - Decisions are taken on a close and executed at the next open.
  - Walk-forward: every setting runs on the first part of the history
    ("in sample") and on the rest ("out of sample") separately. For each family
    the setting that did best in sample is kept, and only its out-of-sample
    result is used to judge the family. This guards against a setting that just
    got lucky.
  - Known bias: stock universes are today's lists, so firms that dropped out
    are missing from the past. It flatters stock strategies.

Outputs: data/lab.json (backtest), data/lab_winners.json, data/lab_signals.json.
Paper only: nothing here talks to a broker. Not financial advice.
"""

import argparse
import datetime as dt
import itertools
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swing as S  # noqa: E402  (reuse data loading and json helpers)

ROOT = S.ROOT
DATA = S.DATA
LAB = os.path.join(ROOT, "lab.json")
FX = "EURUSD=X"


# ---------------------------------------------------------------- data

def load_all(cfg, offline=None, rng="10y"):
    syms = set(cfg["universes"]["etf"]) | set(cfg["universes"]["us"]) | {cfg["benchmark_etf"], FX}
    syms |= {u[0] for u in S.read_universe(os.path.join(ROOT, cfg["universes"]["cac"]))}
    bars, errors = {}, []
    for sym in sorted(syms):
        try:
            if offline:
                doc = S.load_json(os.path.join(offline, sym + ".json"))
                if doc is None:
                    raise RuntimeError("no offline file")
            else:
                doc = S.fetch_chart(sym, rng, cfg.get("pause_s", 1.0))
                time.sleep(cfg.get("pause_s", 1.0))
            b = S.bars_from_chart(doc)
            if len(b) < 260:
                raise RuntimeError("only %d daily bars" % len(b))
            bars[sym] = b
        except Exception as e:
            errors.append("%s: %s" % (sym, e))
    return bars, errors


class Series(object):
    """One symbol's bars with cached indicators, looked up by date."""

    def __init__(self, bars):
        self.b = bars
        self.idx = {x["d"]: i for i, x in enumerate(bars)}
        self.c = [x["c"] for x in bars]
        self._sma = {}
        self._rsi2 = None
        self._hi = {}

    def i(self, d):
        return self.idx.get(d)

    def at_or_before(self, d):
        """Index of the last bar on or before d (binary search)."""
        lo, hi, best = 0, len(self.b) - 1, None
        while lo <= hi:
            m = (lo + hi) // 2
            if self.b[m]["d"] <= d:
                best, lo = m, m + 1
            else:
                hi = m - 1
        return best

    def sma(self, i, n):
        arr = self._sma.get(n)
        if arr is None:
            arr, s = [None] * len(self.c), 0.0
            for k, v in enumerate(self.c):
                s += v
                if k >= n:
                    s -= self.c[k - n]
                if k >= n - 1:
                    arr[k] = s / n
            self._sma[n] = arr
        return arr[i]

    def ret(self, i, n):
        return self.c[i] / self.c[i - n] - 1 if i >= n else None

    def rsi2(self, i):
        if self._rsi2 is None:
            out, ag, al = [None] * len(self.c), None, None
            for k in range(1, len(self.c)):
                ch = self.c[k] - self.c[k - 1]
                g, l = max(ch, 0.0), max(-ch, 0.0)
                if k < 2:
                    ag, al = g, l
                    continue
                ag = (ag * 1 + g) / 2
                al = (al * 1 + l) / 2
                out[k] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
            self._rsi2 = out
        return self._rsi2[i]

    def high(self, i, n):
        arr = self._hi.get(n)
        if arr is None:
            from collections import deque
            arr, dq = [None] * len(self.c), deque()
            for k, v in enumerate(self.c):
                while dq and self.c[dq[-1]] <= v:
                    dq.pop()
                dq.append(k)
                if dq[0] <= k - n:
                    dq.popleft()
                if k >= n - 1:
                    arr[k] = self.c[dq[0]]
            self._hi[n] = arr
        return arr[i]


# ---------------------------------------------------------------- costs

def market_of(sym):
    return "us" if "." not in sym and not sym.startswith("^") and "=" not in sym else "euronext"


def order_cost(sym, value_eur, qty, price_local, cfg, fxr, kind_costs):
    """Broker fee in EUR for one order."""
    if kind_costs == "free":
        return 0.0
    if market_of(sym) == "us":
        c = cfg["costs"]["us"]
        usd = max(c["min_usd"], qty * c["usd_per_share"])
        usd = min(usd, value_eur * fxr * c["max_pct"] / 100.0)
        return usd / fxr
    c = cfg["costs"]["euronext"]
    return max(value_eur * c["pct"] / 100.0, c["min"])


def buy_tax(sym, value, cfg):
    if sym.endswith(".PA") and sym not in set(cfg.get("ttf_exempt", [])) and sym not in set(cfg["universes"]["etf"]):
        return value * cfg["ttf_pct"] / 100.0
    return 0.0


# ---------------------------------------------------------------- strategies

def universe_syms(name, cfg, series):
    if name == "cac":
        lst = [u[0] for u in S.read_universe(os.path.join(ROOT, cfg["universes"]["cac"]))]
    else:
        lst = list(cfg["universes"][name])
    return [s for s in lst if s in series]


def is_month_end(cal, k):
    return k + 1 == len(cal) or cal[k + 1][:7] != cal[k][:7]


def is_week_end(cal, k):
    if k + 1 == len(cal):
        return True
    a = dt.date.fromisoformat(cal[k])
    b = dt.date.fromisoformat(cal[k + 1])
    return b.isocalendar()[1] != a.isocalendar()[1]


def decide(v, series, syms, d, k, cal, held):
    """Target holdings after the close of day d (None = no decision today)."""
    kind, p = v["kind"], v["params"]
    if kind in ("dca", "trend", "dual_momentum", "momentum") and not is_month_end(cal, k):
        return None
    if kind == "high52" and not is_week_end(cal, k):
        return None
    if kind == "dca":
        return list(syms)
    if kind == "trend":
        s = series[syms[0]]
        i = s.at_or_before(d)
        m = s.sma(i, p["sma"]) if i is not None else None
        return [syms[0]] if m is not None and s.c[i] > m else []
    if kind == "dual_momentum":
        best = None
        for sym in syms:
            s = series[sym]
            i = s.at_or_before(d)
            r = s.ret(i, p["lookback"] * 21) if i is not None else None
            if r is not None and (best is None or r > best[0]):
                best = (r, sym)
        return [best[1]] if best and best[0] > 0 else []
    if kind == "momentum":
        ranked = []
        for sym in syms:
            s = series[sym]
            i = s.i(d)
            if i is None:
                continue
            r, m = s.ret(i, p["lookback"] * 21), s.sma(i, 200)
            if r is None or m is None or s.c[i] <= m or r <= 0:
                continue
            ranked.append((r, sym))
        ranked.sort(reverse=True)
        return [x for _, x in ranked[:p["top"]]]
    if kind == "high52":
        keep = []
        for sym in held:
            s = series[sym]
            i = s.i(d)
            m = s.sma(i, p["exit_sma"]) if i is not None else None
            if i is None or m is None or s.c[i] >= m:
                keep.append(sym)
        cands = []
        for sym in syms:
            if sym in keep:
                continue
            s = series[sym]
            i = s.i(d)
            if i is None or i < 252:
                continue
            h, m = s.high(i - 1, 252), s.sma(i, 200)
            if h and m and s.c[i] > h and s.c[i] > m:
                cands.append((s.c[i] / h, sym))
        cands.sort(reverse=True)
        return keep + [x for _, x in cands[:max(0, p["top"] - len(keep))]]
    if kind == "rsi2":
        keep = []
        for sym in held:
            s = series[sym]
            i = s.i(d)
            m5 = s.sma(i, 5) if i is not None else None
            if i is None or m5 is None or s.c[i] <= m5:
                keep.append(sym)
        cands = []
        for sym in syms:
            if sym in keep:
                continue
            s = series[sym]
            i = s.i(d)
            if i is None:
                continue
            r, m = s.rsi2(i), s.sma(i, 200)
            if r is not None and m is not None and r < p["entry"] and s.c[i] > m:
                cands.append((r, sym))
        cands.sort()
        return keep + [x for _, x in cands[:max(0, p["top"] - len(keep))]]
    raise ValueError("unknown kind %s" % kind)


# ---------------------------------------------------------------- simulation

def simulate(v, series, cfg, start, end, keep_curve=False):
    syms = universe_syms(v["universe"], cfg, series)
    if not syms:
        raise ValueError("no data for universe %s" % v["universe"])
    cal_sym = "AAPL" if v["universe"] == "us" and "AAPL" in series else cfg["benchmark_etf"]
    cal = [x["d"] for x in series[cal_sym].b if start <= x["d"] <= end]
    if len(cal) < 60:
        raise ValueError("not enough history")
    fx = series.get(FX)
    us = v["universe"] == "us"
    costs_kind = v.get("costs", "broker")
    monthly = float(cfg["monthly"])
    cash, contributed, fees, taxes, orders = 0.0, 0.0, 0.0, 0.0, 0
    pos = {}                         # sym -> qty
    pending = None
    flows = []                       # (date, -amount)
    curve = []
    peak_ratio, mdd = 0.0, 0.0
    slip = cfg["slippage_pct"] / 100.0
    min_order = float(cfg["min_order_eur"]) if costs_kind != "free" else 1.0
    fractional = us

    def fxr(d):
        if not fx:
            return 1.0
        i = fx.at_or_before(d)
        return fx.c[i] if i is not None else fx.c[0]

    def price(sym, d, field="c"):
        s = series[sym]
        i = s.at_or_before(d)
        return s.b[i][field] if i is not None else None

    def eur(sym, p, d):
        return p / fxr(d) if market_of(sym) == "us" else p

    def buy(sym, budget, d):
        nonlocal cash, fees, taxes, orders
        p = price(sym, d, "o")
        if not p or budget < min_order:
            return
        pe = eur(sym, p, d) * (1 + slip)
        tax_rate = buy_tax(sym, 1.0, cfg)
        est_fee = order_cost(sym, budget, budget / pe, p, cfg, fxr(d), costs_kind)
        spend = min(budget, cash) - est_fee
        q = spend / (pe * (1 + tax_rate))
        if not fractional:
            q = math.floor(q)
        if q <= 0 or q * pe < min_order * 0.5:
            return
        val = q * pe
        f = order_cost(sym, val, q, p, cfg, fxr(d), costs_kind)
        t = val * tax_rate
        if val + f + t > cash + 1e-9:
            return
        cash -= val + f + t
        fees += f
        taxes += t
        orders += 1
        pos[sym] = pos.get(sym, 0) + q

    def sell(sym, d):
        nonlocal cash, fees, orders
        p = price(sym, d, "o")
        if not p:
            return
        q = pos.pop(sym)
        val = q * eur(sym, p, d) * (1 - slip)
        f = order_cost(sym, val, q, p, cfg, fxr(d), costs_kind)
        cash += val - f
        fees += f
        orders += 1

    for k, d in enumerate(cal):
        if k == 0 or cal[k - 1][:7] != d[:7]:
            amt = monthly
            if us and costs_kind != "free":
                fxc = amt * cfg["fx_pct"] / 100.0
                fees += fxc
                amt -= fxc
            cash += amt
            contributed += monthly
            flows.append((d, -monthly))
        if pending is not None:
            for sym in [x for x in pos if x not in pending]:
                sell(sym, d)
            targets = list(pending)
            if targets:
                value = cash + sum(q * eur(s, price(s, d, "o") or 0, d) for s, q in pos.items())
                slot = value / (v["params"].get("top") or len(targets))
                for sym in [x for x in targets if x not in pos]:
                    buy(sym, min(slot, cash), d)
                if v["kind"] in ("dca", "trend", "dual_momentum", "momentum"):
                    held = [x for x in targets if x in pos]
                    if held and cash >= min_order:
                        share = cash / len(held)
                        for sym in held:
                            buy(sym, share, d)
            pending = None
        if k == 0:   # start investing from the first day, as if a month had just ended
            t = decide(v, series, syms, d, 0, [d, "9999-12-31"], list(pos))
        else:
            t = decide(v, series, syms, d, k, cal, list(pos))
        if t is not None and (set(t) != set(pos) or v["kind"] in ("dca", "trend", "dual_momentum", "momentum")):
            pending = t
        val = cash + sum(q * eur(s, price(s, d) or 0, d) for s, q in pos.items())
        ratio = val / contributed if contributed else 1.0
        peak_ratio = max(peak_ratio, ratio)
        mdd = min(mdd, ratio / peak_ratio - 1 if peak_ratio else 0)
        if keep_curve:
            curve.append({"d": d, "value": round(val, 2), "contributed": round(contributed, 2)})
    final = cash + sum(q * eur(s, price(s, cal[-1]) or 0, cal[-1]) for s, q in pos.items())
    years = (dt.date.fromisoformat(cal[-1]) - dt.date.fromisoformat(cal[0])).days / 365.25
    res = {"from": cal[0], "to": cal[-1], "years": round(years, 2), "contributed": round(contributed, 2),
           "final": round(final, 2), "gain_pct": round(100 * (final / contributed - 1), 2),
           "irr_pct": irr(flows, cal[-1], final), "max_drawdown_pct": round(100 * mdd, 2),
           "orders": orders, "fees": round(fees, 2), "taxes": round(taxes, 2),
           "cost_share_pct": round(100 * (fees + taxes) / contributed, 2) if contributed else 0,
           "holdings": {s: round(q, 4) for s, q in pos.items()}, "cash": round(cash, 2)}
    if keep_curve:
        res["curve"] = curve
    return res


def irr(flows, end, final):
    """Annual money-weighted return (XIRR) in %, by bisection."""
    t_end = dt.date.fromisoformat(end)
    cfs = [(dt.date.fromisoformat(d), a) for d, a in flows] + [(t_end, final)]
    t0 = cfs[0][0]

    def npv(r):
        return sum(a / (1 + r) ** ((t - t0).days / 365.25) for t, a in cfs)
    lo, hi = -0.99, 5.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return round(100 * (lo + hi) / 2, 2)


# ---------------------------------------------------------------- grid

def expand(cfg):
    out = []
    for fam in cfg["families"]:
        grid = fam.get("grid", {})
        keys = sorted(grid)
        for combo in itertools.product(*[grid[k] for k in keys]) if keys else [()]:
            params = dict(zip(keys, combo))
            params.update(fam.get("fixed", {}))
            name = fam["family"] + ("_" + "_".join("%s%s" % (k[:2], params[k]) for k in keys) if keys else "")
            out.append({"id": name, "family": fam["family"], "label": fam["label"], "kind": fam["kind"],
                        "universe": fam["universe"], "costs": fam.get("costs", "broker"), "params": params,
                        "setting": ", ".join("%s %s" % (fam.get("names", {}).get(k, k), params[k]) for k in keys)})
    return out


def cmd_backtest(a, cfg):
    raw, errors = load_all(cfg, a.offline, "10y")
    series = {k: Series(v) for k, v in raw.items()}
    if cfg["benchmark_etf"] not in series:
        print("No data for the benchmark %s: %s" % (cfg["benchmark_etf"], "; ".join(errors[:5])))
        return 1
    cal = [x["d"] for x in series[cfg["benchmark_etf"]].b]
    end = cal[-1]
    start = max(cal[0], (dt.date.fromisoformat(end) - dt.timedelta(days=int(365.25 * cfg["years"]))).isoformat())
    span = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
    split = (dt.date.fromisoformat(start) + dt.timedelta(days=int(span * cfg["is_fraction"]))).isoformat()
    rows = []
    for v in expand(cfg):
        row = {k: v[k] for k in ("id", "family", "label", "kind", "universe", "setting", "params")}
        try:
            row["is"] = simulate(v, series, cfg, start, split)
            row["oos"] = simulate(v, series, cfg, split, end)
            row["full"] = simulate(v, series, cfg, start, end)
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)
    bench = next((r for r in rows if r["family"] == "dca_world" and "error" not in r), None)
    winners = []
    for fam in cfg["families"]:
        cand = [r for r in rows if r["family"] == fam["family"] and "error" not in r and r["is"]["irr_pct"] is not None]
        if not cand:
            continue
        best = max(cand, key=lambda r: r["is"]["irr_pct"])
        w = {"id": best["id"], "family": best["family"], "label": best["label"], "setting": best["setting"],
             "settings_tested": len(cand),
             "is_irr": best["is"]["irr_pct"], "oos_irr": best["oos"]["irr_pct"], "full_irr": best["full"]["irr_pct"],
             "oos_gain_pct": best["oos"]["gain_pct"], "oos_drawdown": best["oos"]["max_drawdown_pct"],
             "oos_orders": best["oos"]["orders"], "oos_cost_share": best["oos"]["cost_share_pct"],
             "full_final": best["full"]["final"], "full_contributed": best["full"]["contributed"],
             "full_cost_share": best["full"]["cost_share_pct"], "full_drawdown": best["full"]["max_drawdown_pct"],
             "median_is_irr": median([r["is"]["irr_pct"] for r in cand]),
             "median_oos_irr": median([r["oos"]["irr_pct"] for r in cand if r["oos"]["irr_pct"] is not None])}
        if bench:
            w["beats_dca_oos"] = (w["oos_irr"] or -99) > (bench["oos"]["irr_pct"] or -99)
            w["edge_vs_dca_oos"] = round((w["oos_irr"] or 0) - (bench["oos"]["irr_pct"] or 0), 2)
        winners.append(w)
    winners.sort(key=lambda w: -(w["oos_irr"] if w["oos_irr"] is not None else -99))
    curves = {}
    for w in winners:
        v = next(x for x in expand(cfg) if x["id"] == w["id"])
        c = simulate(v, series, cfg, start, end, keep_curve=True)["curve"]
        step = max(1, len(c) // 200)
        curves[w["family"]] = [{"d": p["d"], "value": p["value"], "contributed": p["contributed"]}
                               for p in c[::step] + c[-1:]]
    doc = {"generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "monthly_eur": cfg["monthly"], "from": start, "split": split, "to": end,
           "configs": len(rows), "errors": sum(1 for r in rows if "error" in r),
           "benchmark": bench["full"] if bench else None, "benchmark_oos": bench["oos"] if bench else None,
           "winners": winners, "rows": [dict(r, **{k: dict(r[k], holdings=None) for k in ("is", "oos", "full") if k in r})
                                        for r in rows],
           "curves": curves, "data_errors": errors}
    S.save_json(os.path.join(DATA, "lab.json"), doc)
    S.save_json(os.path.join(DATA, "lab_winners.json"), {"generated_utc": doc["generated_utc"],
                                                          "winners": [w["id"] for w in winners]})
    print("Lab v2: %d settings, %s to %s (in sample to %s), %s EUR a month, costs included." % (
        len(rows), start, end, split, cfg["monthly"]))
    if bench:
        print("Benchmark, world ETF every month: %s%% a year (out of sample %s%%)." % (
            bench["full"]["irr_pct"], bench["oos"]["irr_pct"]))
    for w in winners:
        print("  %-34s %-36s oos %6s%%/yr  full %6s%%/yr  costs %5.1f%%  %s" % (
            w["label"][:34], w["setting"][:36], w["oos_irr"], w["full_irr"], w["full_cost_share"],
            "beats the ETF" if w.get("beats_dca_oos") else "does not beat the ETF"))
    if errors:
        print("Data errors: %s" % "; ".join(errors[:8]))
    return 0


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return round(xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2, 2)


def cmd_signals(a, cfg):
    win = S.load_json(os.path.join(DATA, "lab_winners.json"))
    if not win:
        print("No lab winners yet: run the backtest first.")
        return 1
    raw, errors = load_all(cfg, a.offline, "2y")
    series = {k: Series(v) for k, v in raw.items()}
    cal = [x["d"] for x in series[cfg["benchmark_etf"]].b]
    start = (dt.date.fromisoformat(cal[-1]) - dt.timedelta(days=380)).isoformat()
    by_id = {v["id"]: v for v in expand(cfg)}
    out = []
    for wid in win["winners"]:
        v = by_id.get(wid)
        if not v:
            continue
        syms = universe_syms(v["universe"], cfg, series)
        cal_sym = "AAPL" if v["universe"] == "us" and "AAPL" in series else cfg["benchmark_etf"]
        c2 = [x["d"] for x in series[cal_sym].b]
        k = len(c2) - 1
        held = []
        try:
            r = simulate(v, series, cfg, start, c2[-1])
            held = list(r["holdings"])
        except Exception as e:
            out.append({"id": wid, "family": v["family"], "label": v["label"], "error": str(e)})
            continue
        # force a decision on the last close, whatever the calendar says
        target = decide(dict(v, kind=v["kind"]), series, syms, c2[k], k, c2[:k + 1] + ["9999-12-31"], held)
        if target is None:
            target = held
        out.append({"id": wid, "family": v["family"], "label": v["label"], "setting": v["setting"],
                    "as_of": c2[k], "holds_now": held, "target": target,
                    "buy": [x for x in target if x not in held], "sell": [x for x in held if x not in target],
                    "next_check": "monthly" if v["kind"] in ("dca", "trend", "dual_momentum", "momentum")
                    else ("weekly" if v["kind"] == "high52" else "daily")})
    doc = {"generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "signals": out, "data_errors": errors}
    S.save_json(os.path.join(DATA, "lab_signals.json"), doc)
    for s in out:
        if s.get("error"):
            print("  %-34s error: %s" % (s["label"][:34], s["error"]))
        else:
            print("  %-34s holds %s -> target %s" % (s["label"][:34], ",".join(s["holds_now"]) or "cash",
                                                      ",".join(s["target"]) or "cash"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Strategy lab v2, paper only.")
    ap.add_argument("cmd", choices=["backtest", "signals"])
    ap.add_argument("--offline")
    ap.add_argument("--config", default=LAB)
    a = ap.parse_args(argv)
    cfg = S.load_json(a.config)
    return {"backtest": cmd_backtest, "signals": cmd_signals}[a.cmd](a, cfg)


if __name__ == "__main__":
    sys.exit(main())
