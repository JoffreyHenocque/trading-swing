#!/usr/bin/env python3
"""Daily news digest, paper only. Standard library only.

    python3 swing/news.py            # headlines + moves for the watch list -> data/news.json

For every stock and ETF the lab watches (CAC 40, large US stocks, the lab's
ETFs) and the two indices, it reads Yahoo Finance's headline feed and the
daily chart, then puts the two side by side:
  - each headline gets a tone score (word lists, French and English) and tags
    (results, analyst, deal, guidance, legal, macro...);
  - each ticker gets its headline count and average tone over 24 hours, its
    move over 1 and 5 sessions, its volume against the 20-day average, its
    trend (close against the 50-day average), and whether a lab strategy
    holds or targets it (data/lab_signals.json);
  - a ticker is flagged when tone and price agree (good news confirmed by a
    rise on volume, bad news confirmed by a fall) or disagree.

A flag is a lead to read, not a signal to buy. Headlines are free, delayed and
incomplete. The tone score is a word count, not an understanding of the story.
Not financial advice.
"""

import argparse
import datetime as dt
import email.utils
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swing as S  # noqa: E402

ROOT, DATA = S.ROOT, S.DATA
UA = "Mozilla/5.0 (compatible; swing-paper-news/1.0)"
RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=%s&lang=%s"
INDICES = {"^FCHI": "CAC 40", "^GSPC": "S&P 500"}

POS = """beat beats record surge surges soar soars jump jumps rally rallies gain gains rise rises
upgrade upgraded outperform buy strong growth profit profits raises raised boost boosts win wins
approval approved partnership contract beat-estimates bullish rebound recovers higher tops exceed exceeds
hausse bondit bond grimpe progresse relève relevé record bénéfice bénéfices croissance contrat accord
rachat succès dépasse solide solides optimiste rebond relèvement surperformance achat favorable
relèvent relevée envol envole flambe s'envole signe gagne""".split()
NEG = """miss misses plunge plunges fall falls drop drops sink sinks slump slumps cut cuts downgrade downgraded
underperform sell weak loss losses lawsuit probe investigation recall warning warns lower lowers layoffs
bearish fraud fine fined decline declines slowdown tariff tariffs strike halt default
baisse chute recule plonge dégringole perte pertes avertissement abaisse abaissé dégradation enquête
amende grève licenciements ralentissement procès rappel déception décevant vente défavorable
abaissent recul reculent inquiète inquiétude chutent sanction""".split()
TAGS = {
    "résultats": r"\b(earnings|results|revenue|quarter|q[1-4]|résultats|chiffre d'affaires|trimestre|semestriel)\b",
    "analyste": r"\b(upgrade|downgrade|price target|analyst|rating|objectif de cours|relève|abaisse|recommandation)\b",
    "opération": r"\b(acquisition|acquire|merger|deal|takeover|buyout|rachat|fusion|offre publique|opa|cession)\b",
    "prévisions": r"\b(guidance|outlook|forecast|prévision|prévisions|perspectives|objectifs annuels)\b",
    "juridique": r"\b(lawsuit|probe|investigation|fine|regulator|sec|enquête|amende|procès|autorité)\b",
    "macro": r"\b(fed|ecb|bce|inflation|rates|taux|tariff|tariffs|droits de douane|recession|récession|gdp|pib|chômage|jobs)\b",
}


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def tone(text):
    words = re.findall(r"[a-zà-ÿ\-']+", text.lower())
    p = sum(1 for w in words if w in POS)
    n = sum(1 for w in words if w in NEG)
    if p == n:
        return 0
    return 1 if p > n else -1


def tags(text):
    t = text.lower()
    return [k for k, rx in TAGS.items() if re.search(rx, t)]


def parse_rss(data, ticker):
    out = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "").strip()
        pub = it.findtext("pubDate")
        try:
            when = email.utils.parsedate_to_datetime(pub).astimezone(dt.timezone.utc) if pub else None
        except (TypeError, ValueError):
            when = None
        if not title:
            continue
        text = title + " " + desc
        out.append({"ticker": ticker, "title": title, "link": link,
                    "time_utc": when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None,
                    "tone": tone(text), "tags": tags(text),
                    "id": hashlib.sha1((link or title).encode("utf-8")).hexdigest()[:12]})
    return out


def watch_list():
    lab = S.load_json(os.path.join(ROOT, "lab.json")) or {}
    names = {}
    for sym, name in S.read_universe(os.path.join(ROOT, "universe.txt")):
        names[sym] = name
    for sym in lab.get("universes", {}).get("us", []):
        names.setdefault(sym, sym)
    for sym in lab.get("universes", {}).get("etf", []):
        names.setdefault(sym, sym + " (ETF)")
    names.update(INDICES)
    return names


def stats_from_chart(doc):
    b = S.bars_from_chart(doc)
    if len(b) < 25:
        return None
    res = (doc.get("chart") or {}).get("result") or [{}]
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    vols = [v for v in (q.get("volume") or []) if v]
    c = [x["c"] for x in b]
    sma50 = sum(c[-50:]) / min(50, len(c))
    vr = None
    if len(vols) >= 21 and sum(vols[-21:-1]):
        vr = round(vols[-1] / (sum(vols[-21:-1]) / 20.0), 2)
    return {"last": round(c[-1], 2), "date": b[-1]["d"],
            "move_1d_pct": round(100 * (c[-1] / c[-2] - 1), 2),
            "move_5d_pct": round(100 * (c[-1] / c[-6] - 1), 2) if len(c) > 6 else None,
            "trend_up": c[-1] > sma50, "volume_ratio": vr}


def lab_positions():
    sig = S.load_json(os.path.join(DATA, "lab_signals.json")) or {}
    out = {}
    for s in sig.get("signals", []):
        for sym in set(s.get("holds_now", [])) | set(s.get("target", [])):
            out.setdefault(sym, []).append(s.get("label"))
    return out


def classify(st, items):
    if not st or not items:
        return None
    avg = sum(i["tone"] for i in items) / len(items)
    up, vol = st["move_1d_pct"], st["volume_ratio"] or 1.0
    if avg > 0 and up > 0.5 and vol >= 1.2:
        return "bonne nouvelle confirmée par le cours"
    if avg < 0 and up < -0.5:
        return "mauvaise nouvelle confirmée par le cours"
    if avg > 0 and up < -1:
        return "divergence : ton positif mais le cours baisse"
    if avg < 0 and up > 1:
        return "divergence : ton négatif mais le cours monte"
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Daily news digest, paper only.")
    ap.add_argument("--offline", help="folder with rss-<SYM>.xml and <SYM>.json chart files")
    ap.add_argument("--hours", type=int, default=36)
    ap.add_argument("--pause", type=float, default=0.4)
    a = ap.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    names = watch_list()
    items, tick, errors = [], {}, []
    for sym in names:
        us = "." not in sym and not sym.startswith("^") or sym == "^GSPC"
        try:
            if a.offline:
                p = os.path.join(a.offline, "rss-%s.xml" % sym)
                data = open(p, "rb").read() if os.path.exists(p) else b""
            else:
                data = http_get(RSS % (urllib.parse.quote(sym), "US" if us else "FR", "en-US" if us else "fr-FR"))
                time.sleep(a.pause)
            items.extend(parse_rss(data, sym))
        except Exception as e:
            errors.append("headlines %s: %s" % (sym, e))
        try:
            if a.offline:
                doc = S.load_json(os.path.join(a.offline, sym + ".json"))
            else:
                doc = S.fetch_chart(sym, "3mo", a.pause)
                time.sleep(a.pause)
            st = stats_from_chart(doc) if doc else None
            if st:
                tick[sym] = st
        except Exception as e:
            errors.append("chart %s: %s" % (sym, e))
    cutoff = (now - dt.timedelta(hours=a.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen, recent = set(), []
    for it in sorted(items, key=lambda x: x["time_utc"] or "", reverse=True):
        key = it["title"].lower()
        if key in seen or (it["time_utc"] and it["time_utc"] < cutoff and not a.offline):
            continue
        seen.add(key)
        recent.append(it)
    held = lab_positions()
    rows = []
    for sym, name in names.items():
        mine = [i for i in recent if i["ticker"] == sym]
        st = tick.get(sym)
        flag = classify(st, mine)
        rows.append({"ticker": sym, "name": name, "headlines": len(mine),
                     "tone_avg": round(sum(i["tone"] for i in mine) / len(mine), 2) if mine else None,
                     "positive": sum(1 for i in mine if i["tone"] > 0),
                     "negative": sum(1 for i in mine if i["tone"] < 0),
                     "move_1d_pct": st["move_1d_pct"] if st else None,
                     "move_5d_pct": st["move_5d_pct"] if st else None,
                     "trend_up": st["trend_up"] if st else None,
                     "volume_ratio": st["volume_ratio"] if st else None,
                     "lab_strategies": held.get(sym, []), "flag": flag,
                     "top_headline": mine[0]["title"] if mine else None})
    flagged = sorted([r for r in rows if r["flag"]], key=lambda r: -abs(r["move_1d_pct"] or 0))
    doc = {"generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "hours": a.hours,
           "headlines": recent[:120], "tickers": rows, "flagged": flagged,
           "indices": {k: tick.get(k) for k in INDICES},
           "counts": {"headlines": len(recent), "tickers_with_news": sum(1 for r in rows if r["headlines"]),
                      "flagged": len(flagged)},
           "errors": errors[:40], "note": "Tone = word count, not understanding. Leads to read, not signals to buy."}
    S.save_json(os.path.join(DATA, "news.json"), doc)
    print("News: %d headlines over %dh on %d tickers, %d flagged." % (
        len(recent), a.hours, doc["counts"]["tickers_with_news"], len(flagged)))
    for r in flagged[:15]:
        print("  %-9s %+6.2f%%  %s%s" % (r["ticker"], r["move_1d_pct"] or 0, r["flag"],
                                         ("  [labo: %s]" % ", ".join(r["lab_strategies"])) if r["lab_strategies"] else ""))
    if errors:
        print("Errors: %d (first: %s)" % (len(errors), errors[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
