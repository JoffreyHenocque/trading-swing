#!/usr/bin/env python3
"""Turn data/*.json into the flat row files the Claude dashboard reads.

    python3 swing/export_dashboard.py [OUT_DIR]    # default ./dash

Writes strategies.json, curves.json, account.json, positions.json, trades.json
(labo v1 and the paper account), lab_winners.json, lab_curves.json,
lab_signals.json, news_flagged.json and news_headlines.json (labo v2 and news).
A missing source gives an empty list, so the page shows its empty state.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SHORT = {"breakout": "Swing cassure 20 jours", "hold_cac": "CAC 40 acheté et gardé",
         "hold_world": "Monde acheté et gardé", "trend_world": "Monde + filtre de tendance",
         "momentum_cac": "Momentum CAC 40 (3 actions)", "dca_world": "100 €/mois sur l'ETF Monde",
         "breakout_m": "Swing cassure 20 jours, 100 €/mois", "hold_cac_m": "100 €/mois sur l'ETF CAC 40",
         "trend_world_m": "Monde + filtre de tendance, 100 €/mois", "momentum_cac_m": "Momentum CAC 40, 100 €/mois"}


def load(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def main(out):
    os.makedirs(out, exist_ok=True)
    files = {}
    c, l = load("compare.json"), load("latest.json")
    if c:
        files["strategies"] = [{
            "strategy": r["name"],
            "mode": r.get("mode") or ("monthly" if r["name"] == "dca_world" or r["name"].endswith("_m") else "lump"),
            "label": SHORT.get(r["name"], r["label"]), "detail": r["label"],
            "invested_eur": r.get("contributed"), "final_eur": r.get("final"), "return_pct": r.get("return_pct"),
            "orders": r.get("trades"), "costs_eur": r.get("costs"), "max_drawdown_pct": r.get("max_drawdown_pct"),
            "period_from": c["from"], "period_to": c["to"]} for r in c["rows"] if not r.get("error")]
        files["curves"] = [{"date": p["d"], "strategy": r["name"], "return_pct": p["pct"]}
                           for r in c["rows"] for p in r.get("curve", [])]
    if l:
        st = l["stats"]
        files["account"] = [{"as_of": l["as_of"], "capital_start_eur": l["config"]["capital"], "equity_eur": st["equity"],
                             "return_pct": st["return_pct"], "closed_trades": st["trades"], "win_rate_pct": st["win_rate"],
                             "avg_r": st["avg_r"], "costs_eur": st["costs"], "max_drawdown_pct": st["max_drawdown_pct"],
                             "open_positions": len(l["positions"]), "pending_orders": len(l["pending"])}]
        files["positions"] = [{"symbol": p["symbol"], "status": "Ouverte", "qty": p["qty"], "since": p["entry_date"],
                               "entry_eur": p["entry"], "last_eur": p["last"], "stop_eur": p["stop"],
                               "target_eur": p["target"]} for p in l["positions"]] + [
            {"symbol": o["symbol"], "status": "Achat à l'ouverture", "qty": o["qty"], "since": o["created"],
             "entry_eur": None, "last_eur": o["signal"]["close"], "stop_eur": None, "target_eur": None}
            for o in l["pending"]]
        files["trades"] = [{"symbol": t["symbol"], "entry_date": t["entry_date"], "exit_date": t["exit_date"],
                            "reason": t["reason"], "pnl_eur": t["pnl"], "r_net": t["r"]} for t in l["recent_trades"]]
    lab = load("lab.json")
    files["lab_winners"] = []
    files["lab_curves"] = []
    if lab:
        b, bo = lab.get("benchmark") or {}, lab.get("benchmark_oos") or {}
        for rank, w in enumerate(lab["winners"], 1):
            files["lab_winners"].append({
                "family": w["family"], "rank": rank, "label": w["label"], "setting": w.get("setting") or "",
                "settings_tested": w["settings_tested"], "oos_irr_pct": w["oos_irr"], "full_irr_pct": w["full_irr"],
                "edge_vs_etf_pts": w.get("edge_vs_dca_oos"), "beats_etf": bool(w.get("beats_dca_oos")),
                "is_reference": w["family"] == "dca_world", "cost_share_pct": w["full_cost_share"],
                "max_drawdown_pct": w["full_drawdown"], "final_eur": w["full_final"],
                "contributed_eur": w["full_contributed"], "monthly_eur": lab["monthly_eur"],
                "period_from": lab["from"], "split": lab["split"], "period_to": lab["to"],
                "etf_irr_pct": b.get("irr_pct"), "etf_oos_irr_pct": bo.get("irr_pct")})
        for fam, pts in lab.get("curves", {}).items():
            for p in pts:
                files["lab_curves"].append({"date": p["d"], "family": fam,
                                            "gain_pct": round(100 * (p["value"] / p["contributed"] - 1), 2)})
    sig = load("lab_signals.json")
    files["lab_signals"] = [{"family": s["family"], "label": s["label"], "setting": s.get("setting") or "",
                             "as_of": s.get("as_of"), "holds": ", ".join(s.get("holds_now") or []) or "liquidités",
                             "target": ", ".join(s.get("target") or []) or "liquidités",
                             "buy": ", ".join(s.get("buy") or []), "sell": ", ".join(s.get("sell") or []),
                             "rhythm": {"monthly": "mensuel", "weekly": "hebdo", "daily": "quotidien"}.get(
                                 s.get("next_check"), "")} for s in (sig or {}).get("signals", []) if not s.get("error")]
    news = load("news.json")
    files["news_flagged"] = []
    files["news_headlines"] = []
    if news:
        files["news_flagged"] = [{"ticker": r["ticker"], "name": r["name"], "flag": r["flag"],
                                  "move_1d_pct": r["move_1d_pct"], "volume_ratio": r["volume_ratio"],
                                  "trend": None if r["trend_up"] is None else ("haussière" if r["trend_up"] else "baissière"),
                                  "headlines": r["headlines"], "lab": ", ".join(r["lab_strategies"]),
                                  "top_headline": r["top_headline"], "as_of": news["generated_utc"]}
                                 for r in news["flagged"]]
        files["news_headlines"] = [{"id": h["id"], "ticker": h["ticker"], "title": h["title"], "link": h["link"],
                                    "time_utc": h["time_utc"],
                                    "tone": {1: "positif", -1: "négatif"}.get(h["tone"], "neutre"),
                                    "tags": ", ".join(h["tags"])} for h in news["headlines"][:40]]
    for name, rows in files.items():
        with open(os.path.join(out, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False)
    print("Wrote %s to %s" % (", ".join("%s (%d)" % (k, len(v)) for k, v in files.items()), out))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dash")
