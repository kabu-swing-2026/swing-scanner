"""スイングトレード・スキャナー

GitHub Actions で毎営業日の大引け後に自動実行され、結果を GitHub Pages に公開する。
ローカルでのテスト:  python scan.py --limit 300   (300銘柄だけで試す)

データ元:
  - JPX 上場銘柄一覧 (銘柄・市場・業種)
  - Yahoo Finance (yfinance) 日足株価・決算予定日・主要指数
  - TDnet 適時開示 (やのしん WebAPI 経由)
  - Google ニュース RSS (銘柄ニュース・市況ニュース)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import logging
import os
import re
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

logging.getLogger("yfinance").setLevel(logging.CRITICAL)
BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
DOCS = BASE / "docs"
HIST_FILE = BASE / "history" / "picks.json"
LATEST_FILE = BASE / "history" / "latest.json"  # 前回の通常スキャン結果 (ザラ場チェックで使う)
COMPANY_FILE = BASE / "history" / "company.json"  # 企業情報キャッシュ
UNIVERSE_CACHE = BASE / "universe.csv"
JST = dt.timezone(dt.timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
INDICES = [("^N225", "日経平均"), ("1306.T", "TOPIX(ETF)"), ("^GSPC", "S&P500"),
           ("^IXIC", "NASDAQ"), ("^SOX", "SOX半導体"), ("^VIX", "VIX恐怖指数"), ("JPY=X", "ドル円")]
STRATS = {"pullback": "押し目買い", "breakout": "ブレイクアウト"}
STRAT_DESC = {
    "pullback": "【メイン戦略】上昇トレンド(25日線>75日線、75日線上向き)の銘柄が25日線まで下げ、反発の陽線が出た日。"
                "値動きが荒すぎる銘柄(ATRが株価の4%超)は除外し、売買代金の大きい順に表示。",
    "breakout": "【参考】20日高値を出来高1.5倍以上で上抜けた日。5年間の検証では前半(2021〜24年)の損益がほぼゼロで、"
                "優位性が安定しないため参考表示です。",
}
WARMUP = 80


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(JST):%H:%M:%S}] {msg}", flush=True)


def http_get(url: str, timeout: int = 30) -> bytes:
    ua = "swing-scanner/1.0 (personal stock screener)" if "wikipedia.org" in url else UA
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- データ取得
def load_universe() -> pd.DataFrame:
    try:
        page = http_get(JPX_PAGE, 30).decode("utf-8", "ignore")
        m = re.search(r'href="([^"]+data_j\.xlsx?)"', page)
        url = urllib.parse.urljoin(JPX_PAGE, m.group(1)) if m else JPX_PAGE
        df = pd.read_excel(io.BytesIO(http_get(url, 60)), dtype=str)
        mk = df["市場・商品区分"].astype(str)
        df = df[mk.str.contains("内国株式") & mk.str.contains("プライム|スタンダード|グロース")]
        uni = pd.DataFrame({
            "code": df["コード"].str.strip(),
            "name": df["銘柄名"].str.strip(),
            "market": df["市場・商品区分"].str.extract("(プライム|スタンダード|グロース)")[0],
            "sector": df["33業種区分"].str.strip(),
        })
        uni = uni[uni.code.str.len() == 4]  # 優先株など5桁コードを除外
        uni.to_csv(UNIVERSE_CACHE, index=False, encoding="utf-8")
        log(f"JPX銘柄一覧: {len(uni)} 銘柄")
        return uni
    except Exception as e:  # JPX が落ちていたら前回の一覧を使う
        log(f"JPX一覧の取得失敗 ({e}) → キャッシュを使用")
        uni = pd.read_csv(UNIVERSE_CACHE, dtype=str)
        return uni[uni.code.str.len() == 4]


def download(tickers: list[str], period: str, need_volume: bool = True,
             min_rows: int = 30) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    chunk = int(CFG.get("download_chunk", 150))
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(part, period=period, interval="1d", group_by="ticker",
                                 auto_adjust=True, threads=True, progress=False)
                if df is not None and not df.empty:
                    break
            except Exception as e:
                log(f"  取得エラー ({attempt + 1}/3): {e}")
            time.sleep(15 * (attempt + 1))
        if df is None or df.empty:
            continue
        names = set(df.columns.get_level_values(0)) if isinstance(df.columns, pd.MultiIndex) else set()
        for t in part:
            if t not in names:
                continue
            d = df[t].dropna(subset=["Close"])
            if need_volume:
                d = d[d["Volume"] > 0]
            if len(d) < min_rows:
                continue
            d = d[["Open", "High", "Low", "Close", "Volume"]].astype(float).copy()
            if d.index.tz is not None:
                d.index = d.index.tz_localize(None)
            out[t] = d
        if len(tickers) > chunk:
            log(f"  {min(i + chunk, len(tickers))}/{len(tickers)} 取得済 (有効 {len(out)})")
        time.sleep(1.5)
    return out


def earnings_date(ticker: str) -> dt.date | None:
    try:
        ed = yf.Ticker(ticker).get_earnings_dates(limit=4)
        if ed is not None and len(ed):
            today = dt.datetime.now(JST).date()
            days = sorted(x.tz_convert(JST).date() if x.tzinfo else x.date() for x in ed.index)
            future = [x for x in days if x >= today]
            return future[0] if future else None
    except Exception:
        pass
    return None


WIKI_BLOCKED = False


def wiki_summary(name: str) -> dict | None:
    """日本語版 Wikipedia から会社の概要を探す。制限(429)を受けたらその回は打ち切る。"""
    global WIKI_BLOCKED
    if WIKI_BLOCKED:
        return None
    q = unicodedata.normalize("NFKC", name).replace(" ", "").replace("　", "")
    time.sleep(1.0)
    try:
        api = ("https://ja.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=3&srsearch="
               + urllib.parse.quote(q + " 企業"))
        hits = json.loads(http_get(api, 15)).get("query", {}).get("search", [])
        for title in [q] + [h["title"] for h in hits if h["title"] != q]:
            try:
                time.sleep(0.5)
                s = json.loads(http_get("https://ja.wikipedia.org/api/rest_v1/page/summary/"
                                        + urllib.parse.quote(title.replace(" ", "_")), 15))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    raise
                continue
            except Exception:
                continue
            ext = s.get("extract") or ""
            norm = unicodedata.normalize("NFKC", title + ext).replace(" ", "").replace("　", "")
            if (s.get("type") == "standard" and q in norm
                    and any(w in ext for w in ("会社", "企業", "持株", "メーカー", "グループ"))):
                return {"title": title, "extract": ext[:260] + ("…" if len(ext) > 260 else ""),
                        "url": s.get("content_urls", {}).get("desktop", {}).get("page", "")}
    except urllib.error.HTTPError as e:
        if e.code == 429:
            WIKI_BLOCKED = True
            log("Wikipedia の取得制限 → 今回は企業概要の取得を省略")
    except Exception:
        pass
    return None


def company_info(ticker: str, name: str, cache: dict) -> dict:
    """財務指標 (Yahoo Finance) と会社概要 (Wikipedia)。30日キャッシュ。"""
    hit = cache.get(ticker)
    today = dt.datetime.now(JST).date()
    if hit and (today - dt.date.fromisoformat(hit["fetched"])).days < 30 and (hit.get("wiki") or WIKI_BLOCKED):
        return hit
    info = {}
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        pass
    pick = ("marketCap", "trailingPE", "forwardPE", "priceToBook", "dividendYield", "returnOnEquity",
            "revenueGrowth", "operatingMargins", "fullTimeEmployees", "website", "longBusinessSummary")
    res = {k: info.get(k) for k in pick}
    if hit and not info:  # Yahoo が失敗したら前回の値を使う
        res = {k: hit.get(k) for k in pick}
    res["wiki"] = wiki_summary(name) or (hit or {}).get("wiki")
    res["fetched"] = today.isoformat()
    cache[ticker] = res
    return res


def tdnet(code: str) -> list[dict]:
    try:
        data = json.loads(http_get(f"https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.json?limit=5", 15))
        cutoff = (dt.datetime.now(JST) - dt.timedelta(days=30)).strftime("%Y-%m-%d")
        res = []
        for it in data.get("items", []):
            t = it.get("Tdnet", {})
            if t.get("pubdate", "")[:10] >= cutoff:
                res.append({"date": t.get("pubdate", "")[:10], "title": t.get("title", ""),
                            "url": t.get("document_url", "")})
        return res[:4]
    except Exception:
        return []


def gnews(query: str, n: int = 3) -> list[dict]:
    try:
        q = urllib.parse.quote(query)
        root = ET.fromstring(http_get(f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja", 15))
        res = []
        for it in root.findall("./channel/item")[:n]:
            pub = it.findtext("pubDate") or ""
            try:
                pub = pd.Timestamp(pub).tz_convert(JST).strftime("%m/%d %H:%M")
            except Exception:
                pass
            res.append({"title": it.findtext("title") or "", "url": it.findtext("link") or "", "date": pub})
        return res
    except Exception:
        return []


# ---------------------------------------------------------------- 指標とシグナル
def add_indicators(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    c, h, l, o, v = d.Close, d.High, d.Low, d.Open, d.Volume
    d["sma25"] = c.rolling(25).mean()
    d["sma75"] = c.rolling(75).mean()
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    d["vol_ratio"] = v / v.rolling(20).mean().shift(1)
    d["turnover20"] = (c * v).rolling(20).mean()
    d["hi20"] = h.rolling(20).max().shift(1)
    d["ret20"] = c / c.shift(20) - 1
    d["ret60"] = c / c.shift(60) - 1
    d["chg"] = c / c.shift(1) - 1
    s25, s75 = d.sma25, d.sma75
    base = (d.turnover20 >= CFG["min_turnover_yen"]) & (c >= CFG["min_price"])
    trend = (s25 > s75) & (s75 > s75.shift(10)) & (c > s75)
    d["sig_pullback"] = (base & trend & (l.rolling(3).min() <= s25 * 1.01) & (c >= s25 * 0.98)
                         & (c <= s25 * 1.03) & d.rsi.between(40, 60) & (c > c.shift(1)) & (c > o))
    d["sig_breakout"] = (base & (s25 > s75) & (c > d.hi20) & (d.vol_ratio >= 1.5)
                         & (c < s25 * 1.12) & (d.rsi < 80) & (c > o))
    return d


def run_trade(o, h, l, c, atr, i: int, n: int) -> dict | None:
    """i日目のシグナル → 翌日寄付きで買い。

    損切り: 買値 − ATR×stop_atr から開始し、毎日の終値ベースで「最高終値 − ATR×trail_atr」まで引き上げる
            (トレーリングストップ。下げることはない)。
    利確:   target_r が設定されていればその水準。null なら利確指値なし (トレーリングで利益を伸ばす)。
    期限:   max_hold_days 営業日たったら終値で手仕舞い。
    """
    if i + 1 >= n:
        return None
    entry = o[i + 1]
    risk = CFG["stop_atr"] * atr[i]
    if not (entry > 0 and risk > 0):
        return None
    tr_k = CFG.get("trail_atr")
    stop = entry - risk
    target = entry + CFG["target_r"] * risk if CFG.get("target_r") else np.inf
    hi = entry
    last = i + CFG["max_hold_days"]
    for j in range(i + 1, min(last, n - 1) + 1):
        res = None
        why = "損切り" if stop < entry else "トレール決済"
        if j > i + 1 and o[j] <= stop:
            res = (o[j], why)
        elif j > i + 1 and o[j] >= target:
            res = (o[j], "利確")
        elif l[j] <= stop:  # 同じ日に両方触れたら損切りとみなす(保守的)
            res = (stop, why)
        elif h[j] >= target:
            res = (target, "利確")
        elif j == last:
            res = (c[j], "期限")
        if res:
            return {"status": "closed", "j": j, "entry": entry, "exit": res[0], "risk": risk, "reason": res[1],
                    "stop": stop}
        if tr_k:
            hi = max(hi, c[j])
            stop = max(stop, hi - tr_k * atr[j])
    return {"status": "open", "j": n - 1, "entry": entry, "exit": c[n - 1], "risk": risk, "reason": "保有中",
            "stop": stop, "days": n - 1 - i, "last_day": last}


def regime_series(idx_frames: dict) -> pd.Series:
    d = idx_frames.get("1306.T")
    if d is None:
        d = idx_frames.get("^N225")
    if d is None:
        return pd.Series(dtype=bool)
    s25 = d.Close.rolling(25).mean()
    return (d.Close > s25) & (s25 > s25.shift(5))


def market_phase(idx_frames: dict) -> dict:
    """日経平均の長期トレンドで3段階判定 (5年検証: 75日線の下かつ下向きの局面は押し目買いの期待値がほぼゼロ)。
    normal = 通常 / caution = 注意 (75日線の下 & 75日線下向き) / stop = 停止 (200日線の下 & 75日線<200日線)"""
    d = idx_frames.get("^N225")
    if d is None or len(d) < 210:
        return {"level": "normal", "risk_pct": CFG["risk_per_trade_pct"], "max_pos": CFG["max_positions"],
                "detail": "日経平均データ不足のため通常扱い"}
    c = d.Close
    s75, s200 = c.rolling(75).mean(), c.rolling(200).mean()
    p, a75, a200 = float(c.iloc[-1]), float(s75.iloc[-1]), float(s200.iloc[-1])
    down75 = bool(s75.iloc[-1] < s75.iloc[-11])
    if p < a200 and a75 < a200:
        level, risk, mp = "stop", 0.0, 0
    elif p < a75 and down75:
        level, risk, mp = "caution", CFG["caution_risk_pct"], CFG["caution_max_positions"]
    else:
        level, risk, mp = "normal", CFG["risk_per_trade_pct"], CFG["max_positions"]
    detail = (f"日経平均 {p:,.0f} / 75日線 {a75:,.0f} ({'下向き' if down75 else '上向き'}) / 200日線 {a200:,.0f}")
    return {"level": level, "risk_pct": risk, "max_pos": mp, "detail": detail}


def backtest(ind: dict, regime: pd.Series) -> dict:
    rows = []
    cost = CFG["cost_pct"] / 100
    for d in ind.values():
        n = len(d)
        o, h, l, c = (d[x].to_numpy() for x in ("Open", "High", "Low", "Close"))
        atr = d.atr.to_numpy()
        reg = regime.reindex(d.index).ffill().fillna(False).astype(bool).to_numpy()
        for key in STRATS:
            nxt = 0
            for i in np.flatnonzero(d[f"sig_{key}"].to_numpy()):
                if i < WARMUP or i < nxt:
                    continue
                if CFG.get("max_atr_pct") and atr[i] / c[i] > CFG["max_atr_pct"] / 100:
                    continue
                r = run_trade(o, h, l, c, atr, i, n)
                if not r or r["status"] != "closed":
                    continue
                pnl = r["exit"] - r["entry"] - r["entry"] * cost
                rows.append({"strat": key, "regime": bool(reg[i]), "pct": pnl / r["entry"],
                             "R": pnl / r["risk"], "days": r["j"] - i})
                nxt = r["j"] + 1
    df = pd.DataFrame(rows)
    out, dist = {}, {}
    for key in STRATS:
        for label, sub in (("all", df[df.strat == key] if len(df) else df),
                           ("regime", df[(df.strat == key) & df.regime] if len(df) else df)):
            out[f"{key}_{label}"] = summarize(sub)
        # 答え合わせ用: 1トレードごとの R (想定範囲の計算に使う)
        dist[key] = [round(float(x), 3) for x in df[df.strat == key].R] if len(df) else []
    return out, dist


def summarize(sub: pd.DataFrame) -> dict:
    if sub is None or len(sub) == 0:
        return {"n": 0}
    pos, neg = sub.pct[sub.pct > 0].sum(), -sub.pct[sub.pct <= 0].sum()
    return {"n": int(len(sub)), "win": float((sub.pct > 0).mean()), "avg": float(sub.pct.mean()),
            "R": float(sub.R.mean()), "pf": float(pos / neg) if neg > 0 else None,
            "days": float(sub.days.mean())}


# ---------------------------------------------------------------- 候補の作成
def make_candidates(ind: dict, uni: pd.DataFrame, last_date, risk_pct: float) -> list[dict]:
    meta = uni.set_index("code").to_dict("index")
    rs = pd.Series({t: d.ret60.iloc[-1] for t, d in ind.items() if d.index[-1] == last_date}).dropna()
    rs_pct = rs.rank(pct=True)
    budget, risk_yen = CFG["budget"], CFG["budget"] * risk_pct / 100
    cands = []
    for t, d in ind.items():
        if d.index[-1] != last_date:
            continue
        r = d.iloc[-1]
        for key in STRATS:
            if not bool(r[f"sig_{key}"]):
                continue
            code = t[:-2]
            m = meta.get(code, {})
            price, atr = float(r.Close), float(r.atr)
            if CFG.get("max_atr_pct") and atr / price > CFG["max_atr_pct"] / 100:
                continue  # 値動きが荒すぎる銘柄は除外 (検証で成績が最も悪かったグループ)
            stop = price - CFG["stop_atr"] * atr
            target = price + CFG["target_r"] * CFG["stop_atr"] * atr if CFG.get("target_r") else None
            rps = price - stop
            unit_cost, unit_risk = price * 100, rps * 100
            odd = int(min(risk_yen / rps, budget / price)) if rps > 0 else 0
            if odd < 1:
                how = f"1株{price:,.0f}円で損切幅が許容損失({risk_yen:,.0f}円)を超えるため見送り"
                how_cls = "bad"
            elif unit_cost <= budget and unit_risk <= budget * 0.05:
                how = f"100株で可 (約{unit_cost:,.0f}円 / 損切時 -{unit_risk:,.0f}円)"
                how_cls = "ok"
            elif unit_cost <= budget:
                how = f"100株は損切時 -{unit_risk:,.0f}円 (資金の{unit_risk / budget:.0%})と大きめ。単元未満株で{odd}株推奨"
                how_cls = "warn"
            else:
                how = f"単元未満株で{odd}株 (約{odd * price:,.0f}円 / 損切時 -{odd * rps:,.0f}円)"
                how_cls = "odd"
            trend = float(np.clip((r.sma25 / r.sma75 - 1) / 0.10, 0, 1))
            score = 50 * float(rs_pct.get(t, 0.5)) + 30 * min(float(r.vol_ratio or 0), 3) / 3 + 20 * trend
            spark = d.tail(60)
            cands.append({
                "ticker": t, "code": code, "name": m.get("name", code), "market": m.get("market", ""),
                "sector": m.get("sector", ""), "strategy": key, "score": round(score, 1),
                "price": price, "chg": float(r.chg), "stop": stop, "target": target, "atr": atr,
                "atr_pct": atr / price,
                "rsi": float(r.rsi), "vol_ratio": float(r.vol_ratio), "ret60": float(r.ret60),
                "turnover": float(r.turnover20), "how": how, "how_cls": how_cls, "odd_shares": odd,
                "spark": [float(x) for x in spark.Close], "spark25": [float(x) for x in spark.sma25],
            })
    # 並び順: 買えない銘柄は後ろ、それ以外は売買代金の大きい順 (流動性が高く売買しやすいものを優先)
    cands.sort(key=lambda x: (x["how_cls"] == "bad", -x["turnover"]))
    for key in STRATS:
        for k, c in enumerate([c for c in cands if c["strategy"] == key], 1):
            c["rank"] = k
    return cands


def enrich(cands: list[dict]) -> None:
    today = dt.datetime.now(JST).date()
    limit = today + dt.timedelta(days=21)  # 3週間以内の決算は保有中にまたぐ可能性が高い
    cache = json.loads(COMPANY_FILE.read_text(encoding="utf-8")) if COMPANY_FILE.exists() else {}
    for c in cands:
        c["company"] = company_info(c["ticker"], c["name"], cache)
        c["earnings"] = None
        ed = earnings_date(c["ticker"])
        if ed:
            c["earnings"] = ed.isoformat()
            c["earnings_soon"] = ed <= limit
            c["earn_flag"] = "today" if ed == today else ("soon" if ed <= limit else "later")
        c["tdnet"] = tdnet(c["code"])
        c["news"] = gnews(f'"{c["name"]}" when:7d')
        time.sleep(0.5)
    COMPANY_FILE.parent.mkdir(exist_ok=True)
    COMPANY_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- フォワードテスト (実際の推奨の成績)
def load_hist() -> list[dict]:
    return json.loads(HIST_FILE.read_text(encoding="utf-8")) if HIST_FILE.exists() else []


def save_hist(hist: list[dict]) -> None:
    """1件1行で保存 (git の差分が小さくなる)。"""
    HIST_FILE.parent.mkdir(exist_ok=True)
    body = ",\n".join(json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in hist)
    HIST_FILE.write_text(f"[\n{body}\n]\n", encoding="utf-8")


def update_history(ind: dict, cands: list[dict], last_date, regime_ok: bool, phase_level: str) -> list[dict]:
    hist = load_hist()
    cost = CFG["cost_pct"] / 100
    for p in hist:
        if p.get("status") == "closed" or p["ticker"] not in ind:
            continue
        d = ind[p["ticker"]]
        i = int(d.index.searchsorted(pd.Timestamp(p["date"]), side="right")) - 1
        if i < 0:
            continue
        o, h, l, c, atr = (d[x].to_numpy() for x in ("Open", "High", "Low", "Close", "atr"))
        r = run_trade(o, h, l, c, atr, i, len(d))
        if not r:
            continue
        if r["status"] == "open":
            p.update(stop_now=float(r["stop"]), days=int(r["days"]), last_close=float(c[-1]),
                     entry_date=d.index[i + 1].strftime("%Y-%m-%d"))
        pnl = r["exit"] - r["entry"] - r["entry"] * cost
        p.update(entry=float(r["entry"]), exit=float(r["exit"]), reason=r["reason"], status=r["status"],
                 pct=float(pnl / r["entry"]), risk=float(r["risk"]), R=float(pnl / r["risk"]),
                 exit_date=d.index[r["j"]].strftime("%Y-%m-%d"))
    day = last_date.strftime("%Y-%m-%d")
    have = {(p["date"], p["ticker"], p["strategy"]) for p in hist}
    # 推奨 (上位 history_picks_per_strategy) だけでなく上位 track_top_n まで記録し、順位による差も検証する
    track = max(CFG.get("track_top_n", 10), CFG["history_picks_per_strategy"])
    for key in STRATS:
        for c in [x for x in cands if x["strategy"] == key][:track]:
            if (day, c["ticker"], key) not in have:
                hist.append({"date": day, "ticker": c["ticker"], "code": c["code"], "name": c["name"],
                             "strategy": key, "rank": c["rank"], "score": c["score"], "atr": c["atr"],
                             "signal_close": c["price"], "regime": regime_ok, "phase": phase_level,
                             "buyable": c["how_cls"] != "bad", "status": "pending"})
    hist = hist[-12000:]
    save_hist(hist)
    return hist


# ---------------------------------------------------------------- HTML 出力
def esc(x) -> str:
    return html.escape(str(x))


def fpct(x, digits=1) -> str:
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:+.{digits}f}%"


def sparkline(vals, sma) -> str:
    vals = [v for v in vals if v == v]
    if len(vals) < 2:
        return ""
    allv = vals + [v for v in sma if v == v]
    lo, hi = min(allv), max(allv)
    span = (hi - lo) or 1
    w, hgt = 160, 44

    def pts(series):
        off = len(vals) - len(series)
        return " ".join(f"{(k + off) / (len(vals) - 1) * w:.1f},{hgt - (v - lo) / span * hgt:.1f}"
                        for k, v in enumerate(series) if v == v)
    return (f'<svg class="spark" viewBox="0 -2 {w} {hgt + 4}" preserveAspectRatio="none" aria-hidden="true">'
            f'<polyline class="s25" points="{pts(sma)}"/><polyline class="px" points="{pts(vals)}"/></svg>')


def company_html(co: dict | None) -> tuple[str, str]:
    """(カードに常時出す指標の行, 「企業の背景」の折りたたみ) を返す。"""
    if not co:
        return "", ""

    def num(k):
        v = co.get(k)
        return v if isinstance(v, (int, float)) and v == v else None
    chips = []
    mc = num("marketCap")
    if mc:
        size = "大型" if mc >= 1e12 else "中型" if mc >= 1e11 else "小型" if mc >= 1e10 else "超小型"
        chips.append(f"時価総額 {mc / 1e8:,.0f}億 ({size})")
    pe = num("forwardPE") or num("trailingPE")
    if pe and 0 < pe < 1000:
        chips.append(f"PER {pe:.1f}")
    if num("priceToBook"):
        chips.append(f"PBR {num('priceToBook'):.1f}")
    if num("dividendYield") is not None:
        chips.append(f"配当 {num('dividendYield'):.1f}%")
    if num("returnOnEquity") is not None:
        chips.append(f"ROE {num('returnOnEquity'):.0%}")
    if num("revenueGrowth") is not None:
        chips.append(f"売上 {num('revenueGrowth'):+.0%}")
    if num("operatingMargins") is not None:
        chips.append(f"営業利益率 {num('operatingMargins'):.0%}")
    line = f"<div class='fin'>{' · '.join(chips)}</div>" if chips else ""
    body = ""
    w = co.get("wiki")
    if w:
        body += (f"<p>{esc(w['extract'])} <a href='{esc(w['url'])}' target='_blank' rel='noopener'>Wikipedia</a></p>")
    if co.get("longBusinessSummary"):
        body += f"<h4>事業内容 (Yahoo Finance・英語)</h4><p class='en'>{esc(co['longBusinessSummary'][:500])}…</p>"
    extra = []
    if num("fullTimeEmployees"):
        extra.append(f"従業員 {num('fullTimeEmployees'):,.0f}人")
    if co.get("website"):
        extra.append(f"<a href='{esc(co['website'])}' target='_blank' rel='noopener'>公式サイト</a>")
    if extra:
        body += f"<p class='muted'>{' · '.join(extra)}</p>"
    return line, (f"<details><summary>企業の背景</summary>{body}</details>" if body else "")


def card(c: dict) -> str:
    fin_line, about = company_html(c.get("company"))
    links = (f'<a href="https://kabutan.jp/stock/chart?code={esc(c["code"])}" target="_blank" rel="noopener">株探</a>'
             f'<a href="https://finance.yahoo.co.jp/quote/{esc(c["code"])}.T" target="_blank" rel="noopener">Yahoo</a>'
             f'<a href="https://www.tradingview.com/chart/?symbol=TSE:{esc(c["code"])}" target="_blank" rel="noopener">TradingView</a>'
             f'<a href="sim.html#buy={esc(c["code"])}">🧪仮想で買う</a>')
    flags = ""
    if c.get("earn_flag") == "today":
        flags += f'<span class="flag bad">{esc(c["earnings"])} 決算発表 → 発表後の値動きを確認してから判断</span>'
    elif c.get("earnings_soon"):
        flags += f'<span class="flag bad">決算 {esc(c["earnings"])} (3週間以内) → 見送りか、決算前日に売る</span>'
    elif c.get("earnings"):
        flags += f'<span class="flag">決算予定 {esc(c["earnings"])}</span>'
    info = ""
    if c.get("tdnet"):
        info += "<h4>適時開示 (30日)</h4><ul>" + "".join(
            f'<li><span class="d">{esc(x["date"][5:])}</span> <a href="{esc(x["url"])}" target="_blank" rel="noopener">{esc(x["title"])}</a></li>'
            for x in c["tdnet"]) + "</ul>"
    if c.get("news"):
        info += "<h4>ニュース (7日)</h4><ul>" + "".join(
            f'<li><span class="d">{esc(x["date"])}</span> <a href="{esc(x["url"])}" target="_blank" rel="noopener">{esc(x["title"])}</a></li>'
            for x in c["news"]) + "</ul>"
    details = about
    details += f"<details><summary>開示・ニュース</summary>{info}</details>" if info else ""
    details += (f'<details class="tv" data-sym="TSE:{esc(c["code"])}"><summary>ライブチャート</summary>'
                f'<div class="tvbox"></div></details>')
    up = "up" if c["chg"] >= 0 else "down"
    lv = c.get("live")
    live_html = ""
    if lv:
        lcls = "up" if lv["chg"] >= 0 else "down"
        tgt = f" / 利確 <b class='up'>{lv['target']:,.0f}</b>" if lv.get("target") else ""
        buy = (f"<div>今買うなら 最初の損切り <b class='down'>{lv['stop']:,.0f}</b>{tgt}</div>"
               if lv["status"] in ("go", "hot") else "")
        live_html = (f"<div class='live {lv['status']}'><b>{esc(lv['label'])}</b>"
                     f"<div>現在値 <b>{lv['price']:,.0f}</b> <span class='{lcls}'>{fpct(lv['chg'])}</span>"
                     f" ・ 寄付 {lv['open']:,.0f} ({fpct(lv['gap'])})</div>{buy}</div>")
    return f"""
<article class="card">
  <header><div><span class="code">{esc(c['code'])}</span> <b>{esc(c['name'])}</b>
    <div class="sub">{esc(c['market'])} · {esc(c['sector'])}</div>{fin_line}</div>
    <div class="score" title="売買代金の大きい順の順位">#{c.get('rank', '')}</div></header>
  {live_html}
  <div class="row"><div class="price"><small class="muted">シグナル日終値 </small>{c['price']:,.0f}<small>円</small> <span class="{up}">{fpct(c['chg'])}</span></div>{sparkline(c['spark'], c['spark25'])}</div>
  <dl>
    <div><dt>最初の損切り</dt><dd class="down">{c['stop']:,.0f}</dd></div>
    <div><dt>{'利確目安' if c.get('target') else '値動き(ATR)'}</dt><dd>{f"{c['target']:,.0f}" if c.get('target') else f"{c['atr']:,.0f}円 ({c.get('atr_pct', 0):.1%})"}</dd></div>
    <div><dt>RSI</dt><dd>{c['rsi']:.0f}</dd></div>
    <div><dt>出来高</dt><dd>{c['vol_ratio']:.1f}倍</dd></div>
    <div><dt>3ヶ月</dt><dd>{fpct(c['ret60'], 0)}</dd></div>
    <div><dt>売買代金</dt><dd>{c['turnover'] / 1e8:,.1f}億</dd></div>
  </dl>
  <p class="how {c['how_cls']}">💴 {esc(c['how'])}</p>
  {flags}
  <div class="links">{links}</div>
  {details}
</article>"""


def bt_table(bt: dict) -> str:
    rows = ""
    for key, name in STRATS.items():
        for label, lab in (("all", "全期間"), ("regime", "地合い良好時のみ")):
            s = bt.get(f"{key}_{label}", {"n": 0})
            if not s.get("n"):
                rows += f"<tr><td>{name}</td><td>{lab}</td><td colspan=6>データなし</td></tr>"
                continue
            yen = s["R"] * CFG["budget"] * CFG["risk_per_trade_pct"] / 100
            cls = "up" if s["R"] > 0 else "down"
            pf = f'{s["pf"]:.2f}' if s["pf"] else "—"
            rows += (f"<tr><td>{name}</td><td>{lab}</td><td>{s['n']:,}</td><td>{s['win']:.0%}</td>"
                     f"<td class='{cls}'>{fpct(s['avg'], 2)}</td><td>{pf}</td>"
                     f"<td class='{cls}'>{yen:+,.0f}円</td><td>{s['days']:.1f}日</td></tr>")
    return ("<div class='tw'><table><thead><tr><th>戦略</th><th>条件</th><th>回数</th><th>勝率</th>"
            "<th>平均損益</th><th>PF</th><th>1回あたり期待値*</th><th>平均保有</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")


def is_rec(p: dict) -> bool:
    """推奨銘柄 (各戦略の上位 history_picks_per_strategy) か。順位のない古い記録は推奨のみだったので True。"""
    return p.get("rank", 1) <= CFG["history_picks_per_strategy"]


def fwd_section(hist: list[dict]) -> str:
    hist = [p for p in hist if is_rec(p)]
    closed = [p for p in hist if p.get("status") == "closed"]
    openp = [p for p in hist if p.get("status") == "open"]
    pend = [p for p in hist if p.get("status") == "pending"]
    head = f"<p>推奨の記録 {len(hist)}件 (決済済 {len(closed)} / 保有中 {len(openp)} / 翌日エントリー待ち {len(pend)})</p>"
    if closed:
        pcts = [p["pct"] for p in closed]
        wins = sum(1 for x in pcts if x > 0)
        head += (f"<p class='big'>決済済の勝率 <b>{wins / len(pcts):.0%}</b> ・ 平均損益 "
                 f"<b class='{'up' if np.mean(pcts) > 0 else 'down'}'>{fpct(float(np.mean(pcts)), 2)}</b></p>")
    head += ("<p><a href='check.html'><b>🔍 答え合わせページを開く</b></a> "
             "<span class='muted'>— バックテストの想定どおりか・順位や朝の◎判定は効いているか</span></p>")
    recent = sorted(closed + openp, key=lambda p: p["date"], reverse=True)[:30]
    if not recent:
        return head + "<p class='muted'>まだ結果がありません。数日〜2週間ほど運用すると、ここに実際の推奨銘柄の成績がたまります。</p>"
    rows = "".join(
        f"<tr><td>{esc(p['date'][5:])}</td><td>{esc(p['code'])} {esc(p['name'])}</td><td>{STRATS[p['strategy']]}</td>"
        f"<td>{p.get('entry', 0):,.0f}</td><td>{p.get('exit', 0):,.0f}</td>"
        f"<td class='{'up' if p.get('pct', 0) > 0 else 'down'}'>{fpct(p.get('pct'), 1)}</td><td>{esc(p.get('reason', ''))}</td></tr>"
        for p in recent)
    return head + ("<div class='tw'><table><thead><tr><th>日付</th><th>銘柄</th><th>戦略</th><th>買値</th>"
                   f"<th>売値/現値</th><th>損益</th><th>結果</th></tr></thead><tbody>{rows}</tbody></table></div>")


def positions_section(hist: list[dict]) -> str:
    hist = [p for p in hist if is_rec(p)]
    openp = sorted([p for p in hist if p.get("status") == "open" and p["strategy"] == "pullback"],
                   key=lambda p: p["date"], reverse=True)
    pend = [p for p in hist if p.get("status") == "pending" and p["strategy"] == "pullback"]
    note = (f"<p class='muted'>翌朝エントリー予定: {' / '.join(esc(p['code'] + ' ' + p['name']) for p in pend)}</p>"
            if pend else "")
    if not openp:
        return note + "<p class='muted'>現在、保有中の推奨ポジションはありません。</p>"
    rows = ""
    for p in openp:
        pct = p.get("pct", 0)
        rows += (f"<tr><td>{esc(p.get('entry_date', p['date'])[5:])}</td><td>{esc(p['code'])} {esc(p['name'])}</td>"
                 f"<td>{p.get('entry', 0):,.0f}</td><td>{p.get('last_close', p.get('exit', 0)):,.0f}</td>"
                 f"<td class='{'up' if pct > 0 else 'down'}'>{fpct(pct)}</td>"
                 f"<td><b class='down'>{p.get('stop_now', 0):,.0f}</b></td>"
                 f"<td>{p.get('days', 0)}/{CFG['max_hold_days']}日</td></tr>")
    return note + ("<div class='tw'><table><thead><tr><th>買付日</th><th>銘柄</th><th>買値</th><th>現値</th>"
                   f"<th>損益</th><th>明日の逆指値</th><th>保有</th></tr></thead><tbody>{rows}</tbody></table></div>")


# ---------------------------------------------------------------- 答え合わせページ (check.html)
LIVE_LABEL = {"go": "◎ 買い条件OK", "hot": "△ 上がりすぎ", "weak": "△ 弱い動き", "ng": "✕ 損切りライン到達",
              None: "判定なし"}
PHASE_LABEL = {"normal": "🟢 通常", "caution": "🟠 注意", "stop": "⛔ 停止", None: "(記録前)"}
MIN_JUDGE_N = 10  # これ未満の件数では判定を保留


def pick_r(p: dict) -> float | None:
    """損益を「最初の損切り幅」の何倍かで表したもの。+1R = 許容損失と同じ額の利益。"""
    if p.get("R") is not None:
        return p["R"]
    risk = p.get("risk") or CFG["stop_atr"] * p.get("atr", 0)
    if not risk or p.get("exit") is None or p.get("entry") is None:
        return None
    return (p["exit"] - p["entry"] - p["entry"] * CFG["cost_pct"] / 100) / risk


def rstats(ps: list[dict]) -> dict:
    rs = np.array([r for r in (pick_r(p) for p in ps) if r is not None], float)
    if not len(rs):
        return {"n": 0}
    pos, neg = rs[rs > 0].sum(), -rs[rs <= 0].sum()
    return {"n": int(len(rs)), "win": float((rs > 0).mean()), "avg": float(rs.mean()), "sum": float(rs.sum()),
            "pf": float(pos / neg) if neg > 0 else None}


def expect_band(dist: list[float] | None, n: int) -> dict | None:
    """バックテストの全トレードから n 件をランダムに選んだときの平均Rの分布 (5%〜95%)。"""
    arr = np.asarray(dist or [], float)
    if n < 1 or len(arr) < 30:
        return None
    m = np.random.default_rng(0).choice(arr, (2000, min(n, 3000))).mean(axis=1)
    lo, mid, hi = np.percentile(m, [5, 50, 95])
    return {"lo": float(lo), "mid": float(mid), "hi": float(hi)}


def verdict(s: dict, b: dict | None) -> str:
    if not s["n"]:
        return "<span class='muted'>まだ決済なし</span>"
    if not b:
        return "<span class='muted'>比較データなし</span>"
    if s["avg"] < b["lo"]:
        v = "<b class='down'>⚠ 想定より悪い</b>"
    elif s["avg"] > b["hi"]:
        v = "<b class='up'>★ 想定より良い</b>"
    else:
        v = "✅ 想定の範囲内"
    if s["n"] < MIN_JUDGE_N:
        v = f"<span class='muted'>判定保留 (あと{MIN_JUDGE_N - s['n']}件) ・ 現状 {v}</span>"
    return v


def stat_cells(s: dict) -> str:
    if not s["n"]:
        return "<td>0</td>" + "<td>—</td>" * 5
    ry = CFG["budget"] * CFG["risk_per_trade_pct"] / 100
    cls = "up" if s["avg"] > 0 else "down"
    pf = f"{s['pf']:.2f}" if s["pf"] else "—"
    return (f"<td>{s['n']}</td><td>{s['win']:.0%}</td><td class='{cls}'>{s['avg']:+.2f}R</td><td>{pf}</td>"
            f"<td class='{'up' if s['sum'] > 0 else 'down'}'>{s['sum']:+.1f}R</td>"
            f"<td class='{cls}'>{s['avg'] * ry:+,.0f}円</td>")


STAT_HEAD = "<th>決済数</th><th>勝率</th><th>平均R</th><th>PF</th><th>合計R</th><th>1回あたり*</th>"


def group_table(title: str, ps: list[dict], keyfn, order: list, labels: dict) -> str:
    groups: dict = {}
    for p in ps:
        groups.setdefault(keyfn(p), []).append(p)
    rows = "".join(f"<tr><td>{esc(labels.get(k, k))}</td>{stat_cells(rstats(groups[k]))}</tr>"
                   for k in order if k in groups)
    rows += "".join(f"<tr><td>{esc(labels.get(k, k))}</td>{stat_cells(rstats(v))}</tr>"
                    for k, v in groups.items() if k not in order)
    if not rows:
        rows = "<tr><td colspan=7 class='muted'>まだデータがありません</td></tr>"
    return (f"<div class='tw'><table><thead><tr><th>{esc(title)}</th>{STAT_HEAD}</tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")


def cum_chart(ps: list[dict], exp_r: float | None) -> str:
    """決済順の累積R (実線) と、バックテストの平均どおりだった場合の線 (点線)。"""
    ps = sorted(ps, key=lambda p: (p.get("exit_date", ""), p["date"]))
    rs = [r for r in (pick_r(p) for p in ps) if r is not None]
    if len(rs) < 2:
        return "<p class='muted'>決済が2件以上たまるとグラフを表示します。</p>"
    cum = np.concatenate([[0], np.cumsum(rs)])
    n = len(rs)
    exp_end = n * (exp_r or 0)
    lo, hi = min(cum.min(), exp_end, 0), max(cum.max(), exp_end, 0)
    if hi - lo < 1:
        hi += 0.5
        lo -= 0.5
    W, H, L, R_, T, B = 640, 220, 44, 12, 12, 26
    sx = lambda i: L + i * (W - L - R_) / n
    sy = lambda v: T + (hi - v) * (H - T - B) / (hi - lo)
    pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(cum))
    exp_line = (f"<line x1='{sx(0):.1f}' y1='{sy(0):.1f}' x2='{sx(n):.1f}' y2='{sy(exp_end):.1f}' class='exp'/>"
                if exp_r is not None else "")
    ticks = "".join(f"<text x='{L - 6}' y='{sy(v) + 4:.1f}' text-anchor='end'>{v:+.0f}R</text>"
                    f"<line x1='{L}' x2='{W - R_}' y1='{sy(v):.1f}' y2='{sy(v):.1f}' class='grid'/>"
                    for v in np.unique(np.round(np.linspace(lo, hi, 5))))
    return (f"<svg class='cum' viewBox='0 0 {W} {H}' role='img' aria-label='累積Rの推移'>{ticks}"
            f"<line x1='{L}' x2='{W - R_}' y1='{sy(0):.1f}' y2='{sy(0):.1f}' class='zero'/>{exp_line}"
            f"<polyline points='{pts}' class='act'/>"
            f"<text x='{L}' y='{H - 6}'>1件目</text><text x='{W - R_}' y='{H - 6}' text-anchor='end'>{n}件目</text></svg>"
            "<p class='muted'>実線 = 実際の累積R ／ 点線 = バックテストの平均どおりだった場合。"
            "実線が点線の上下を行き来していれば想定どおり。点線からどんどん離れて下がるなら要注意。</p>")


CHECK_CSS = """
svg.cum{width:100%;height:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
svg.cum text{font-size:11px;fill:var(--muted)}svg.cum .grid{stroke:var(--line);stroke-width:1}
svg.cum .zero{stroke:var(--muted);stroke-width:1}svg.cum .exp{stroke:var(--muted);stroke-dasharray:5 4;stroke-width:1.5}
svg.cum .act{fill:none;stroke:var(--accent);stroke-width:2}
.summary{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin:12px 0}
.summary .card b.v{font-size:1.15rem}
"""


def check_page(ctx: dict) -> str:
    hist, dist = ctx.get("hist") or [], ctx.get("bt_r") or {}
    ry = CFG["budget"] * CFG["risk_per_trade_pct"] / 100
    k_rec, k_track = CFG["history_picks_per_strategy"], max(CFG.get("track_top_n", 10), CFG["history_picks_per_strategy"])
    closed = [p for p in hist if p.get("status") == "closed"]
    openp = [p for p in hist if p.get("status") == "open"]

    # 1) 推奨 / それ以外 × 戦略 を、バックテストの想定範囲と比べる
    summary, rows = "", ""
    for key, name in STRATS.items():
        exp = float(np.mean(dist[key])) if dist.get(key) else None
        for label, sub in ((f"推奨 (上位{k_rec})", [p for p in closed if p["strategy"] == key and is_rec(p)]),
                           (f"{k_rec + 1}〜{k_track}位", [p for p in closed if p["strategy"] == key and not is_rec(p)])):
            s = rstats(sub)
            b = expect_band(dist.get(key), s["n"])
            rng = f"{b['lo']:+.2f}〜{b['hi']:+.2f}R" if b else "—"
            rows += (f"<tr><td>{name}</td><td>{label}</td>{stat_cells(s)}<td>{rng}</td>"
                     f"<td>{verdict(s, b)}</td></tr>")
            if label.startswith("推奨"):
                exp_txt = f"バックテスト平均 {exp:+.2f}R ({exp * ry:+,.0f}円)" if exp is not None else "バックテストなし"
                now = f"{s['avg']:+.2f}R ({s['n']}件)" if s["n"] else "まだ決済なし"
                summary += (f"<div class='card'><div class='muted'>{name}・推奨</div>"
                            f"<b class='v'>{verdict(s, b)}</b><div>実際 {now}</div>"
                            f"<div class='muted'>{exp_txt}</div></div>")
    main_tbl = ("<div class='tw'><table><thead><tr><th>戦略</th><th>対象</th>" + STAT_HEAD +
                f"<th>想定範囲 (90%)</th><th>判定</th></tr></thead><tbody>{rows}</tbody></table></div>")

    pb = [p for p in closed if p["strategy"] == "pullback"]
    pb_exp = float(np.mean(dist["pullback"])) if dist.get("pullback") else None
    rank_bucket = lambda p: ("1〜3位" if p.get("rank", 1) <= 3 else "4〜6位" if p["rank"] <= 6 else f"7〜{k_track}位")
    live_tbl = lambda slot: group_table("判定", pb, lambda p: p.get(slot),
                                        ["go", "hot", "weak", "ng", None], LIVE_LABEL)

    recent = sorted(closed + openp, key=lambda p: (p["date"], p["strategy"], p.get("rank", 1)), reverse=True)[:80]
    trows = ""
    for p in recent:
        r = pick_r(p)
        rtxt = f"{r:+.2f}R" if r is not None else "—"
        trows += (f"<tr><td>{esc(p['date'][5:])}</td><td>{esc(p['code'])} {esc(p['name'])}</td>"
                  f"<td>{STRATS[p['strategy']]}</td><td>{p.get('rank', '—')}</td>"
                  f"<td>{esc(LIVE_LABEL.get(p.get('live_am'), '—'))}</td>"
                  f"<td>{esc(PHASE_LABEL.get(p.get('phase'), '—'))}</td>"
                  f"<td>{p.get('entry', 0):,.0f}</td><td>{p.get('exit', 0):,.0f}</td>"
                  f"<td class='{'up' if (r or 0) > 0 else 'down'}'>{rtxt}</td><td>{esc(p.get('reason', ''))}</td></tr>")
    trade_tbl = ("<div class='tw'><table><thead><tr><th>シグナル日</th><th>銘柄</th><th>戦略</th><th>順位</th>"
                 "<th>朝の判定</th><th>相場</th><th>買値</th><th>売値/現値</th><th>R</th><th>結果</th></tr></thead>"
                 f"<tbody>{trows or '<tr><td colspan=10 class=muted>まだ記録がありません</td></tr>'}</tbody></table></div>")

    first = min((p["date"] for p in hist), default=None)
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>答え合わせ</title>
<style>{CSS}{CHECK_CSS}</style></head><body><main>
<p><a href="index.html">← スキャナーに戻る</a></p>
<h1>🔍 答え合わせ</h1>
<div class="muted">データ日付 {esc(ctx.get('data_date', ''))} ・ 記録開始 {esc(first or '—')} ・ 記録 {len(hist)}件
(決済済 {len(closed)} / 保有中 {len(openp)})</div>
<p class="muted">毎日の候補を各戦略の上位{k_track}位まで自動で記録し、「翌朝の寄付きで買い、ルールどおりに売った」場合の結果を追跡しています。
<b>R</b> = 損益 ÷ 最初の損切り幅。+1R は許容損失と同じ額の利益 (資金{CFG['budget']:,}円・{CFG['risk_per_trade_pct']:g}%なら {ry:,.0f}円)。
*1回あたり = 平均R × {ry:,.0f}円。</p>
<div class="summary">{summary}</div>

<h2>① バックテストどおりに動いているか</h2>
<p class="muted"><b>想定範囲</b> = バックテストの全トレードから同じ件数をランダムに選んだとき、平均Rが90%の確率で入る範囲。
件数が少ないうちは範囲が広く、数回負けても「範囲内」になります。<b>範囲の下に外れ続けたら、相場の性質が変わったかルールが合わなくなったサイン</b>です。
決済{MIN_JUDGE_N}件未満は判定保留。</p>
{main_tbl}

<h2>② 押し目買い・推奨の累積R</h2>
{cum_chart([p for p in pb if is_rec(p)], pb_exp)}

<h2>③ 朝のザラ場判定 (9:20) は役に立っているか — 押し目買い</h2>
<p class="muted">判定に関係なく「寄付きで買った」として集計しています。◎ の成績が △・✕ より良ければ、朝のチェックで選ぶ意味があります。
差がなければ、朝の判定は気にせず寄付きで買っても同じということです。</p>
{live_tbl('live_am')}
<h4>昼 (12:45) の判定</h4>
{live_tbl('live_pm')}

<h2>④ 順位で差があるか — 押し目買い</h2>
<p class="muted">候補は売買代金の大きい順に並んでいます。上位ほど成績が良いなら「上から選ぶ」ルールが正しいことになります。</p>
{group_table('順位', pb, rank_bucket, ['1〜3位', '4〜6位', f'7〜{k_track}位'], {})}

<h2>⑤ 相場判定ごとの成績 — 押し目買い</h2>
<p class="muted">「注意」「停止」の日の候補も、買ったものとして記録しています。注意・停止の成績が悪ければ、相場判定のルールが効いています。</p>
{group_table('相場判定', pb, lambda p: p.get('phase'), ['normal', 'caution', 'stop', None], PHASE_LABEL)}

<h2>⑥ 売った理由ごとの成績</h2>
{group_table('理由', closed, lambda p: p.get('reason'), ['損切り', 'トレール決済', '期限', '利確'], {})}

<h2>記録一覧 (新しい順・最大80件)</h2>
{trade_tbl}
<p class="muted">※ 数か月分たまるまでは偶然の影響が大きく、結論は出せません。目安として決済30件を超えると傾向が見え始めます。
本ページは公開データから機械的に作成した参考情報であり、投資助言ではありません。</p>
</main></body></html>"""


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--muted:#667085;--line:#e4e7ec;--up:#0f7b45;--down:#c4320a;
--accent:#2457c5;--warn-bg:#fff6e0;--warn:#8a5a00;--ok-bg:#e8f6ee;--bad-bg:#fdecea;--chip:#eef2f7}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0f1216;--card:#171b21;--fg:#e6e8eb;
--muted:#98a2b3;--line:#2a3039;--up:#3ccf86;--down:#ff7a59;--accent:#7aa2ff;--warn-bg:#3a2e12;--warn:#f5c56b;
--ok-bg:#14301f;--bad-bg:#3b1c17;--chip:#222832}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP","Yu Gothic UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:1.5rem;margin:.2em 0}
h2{font-size:1.15rem;margin:2em 0 .6em;padding-bottom:.3em;border-bottom:1px solid var(--line)}
h4{margin:.8em 0 .2em;font-size:.85rem;color:var(--muted)}a{color:var(--accent)}
.muted,.sub{color:var(--muted);font-size:.85rem}.up{color:var(--up)}.down{color:var(--down)}
.banner{padding:12px 14px;border-radius:10px;margin:12px 0;font-weight:600}
.banner.good{background:var(--ok-bg)}.banner.bad{background:var(--bad-bg)}.banner.warn{background:var(--warn-bg)}
.banner .muted{font-weight:400;font-size:.85rem}
.idx{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}
.idx div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.idx b{display:block;font-size:1.05rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.card header{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
.code{background:var(--chip);border-radius:6px;padding:1px 6px;font-size:.85rem}
.score{font-size:1.3rem;font-weight:700;color:var(--accent);min-width:2.2em;text-align:right}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:6px 0}
.price{font-size:1.35rem;font-weight:700;white-space:nowrap}.price small{font-size:.7em;margin-left:1px}
.price span{font-size:.75rem;margin-left:4px}
.spark{width:160px;height:44px;flex:none}.spark polyline{fill:none;stroke-width:1.6}
.spark .px{stroke:var(--accent)}.spark .s25{stroke:var(--muted);stroke-dasharray:3 3;stroke-width:1}
dl{display:grid;grid-template-columns:repeat(3,1fr);gap:4px 8px;margin:8px 0}dl div{min-width:0}
dt{font-size:.72rem;color:var(--muted)}dd{margin:0;font-weight:600}
.how{border-radius:8px;padding:6px 8px;font-size:.85rem;margin:8px 0}
.how.ok{background:var(--ok-bg)}.how.warn,.how.odd{background:var(--warn-bg);color:var(--warn)}
.how.bad{background:var(--bad-bg);color:var(--down)}
.live{border-radius:8px;padding:8px 10px;margin:8px 0;font-size:.88rem;border-left:4px solid var(--muted);background:var(--chip)}
.live b:first-child{display:block;font-size:.95rem}
.live.go{border-color:var(--up);background:var(--ok-bg)}.live.hot,.live.weak{border-color:var(--warn);background:var(--warn-bg)}
.live.ng{border-color:var(--down);background:var(--bad-bg)}
.tvbox iframe{width:100%;height:320px;border:0;border-radius:8px;margin-top:6px}
.banner.info{background:var(--chip);font-weight:500}
.fin{font-size:.76rem;color:var(--muted);margin-top:2px}
details p{margin:.4em 0}details .en{font-size:.8rem;color:var(--muted)}
.flag{display:inline-block;font-size:.78rem;border-radius:6px;padding:2px 6px;background:var(--chip);margin:2px 0}
.flag.bad{background:var(--bad-bg);color:var(--down);font-weight:600}
.links{display:flex;gap:12px;font-size:.85rem;margin-top:6px}
details{margin-top:6px;font-size:.85rem}summary{cursor:pointer;color:var(--accent)}
details ul{padding-left:1.1em;margin:.2em 0}details .d{color:var(--muted);font-size:.78rem}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:.85rem;white-space:nowrap}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--muted);font-weight:600}
.big{font-size:1.05rem}.rules{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 18px}
.rules li{margin:.35em 0}.sect{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media (max-width:640px){.sect{grid-template-columns:1fr}.grid{grid-template-columns:1fr}}
"""


def render(ctx: dict) -> str:
    ph = ctx.get("phase") or {"level": "normal", "risk_pct": CFG["risk_per_trade_pct"],
                               "max_pos": CFG["max_positions"], "detail": ""}
    b = CFG["budget"]
    if ph["level"] == "stop":
        banner = ('<div class="banner bad">⛔ <b>相場判定：停止</b> — 日経平均が200日線の下で、75日線も200日線を下回る下げ相場。'
                  '<b>新規の買いは止めて現金で待つ。</b>保有中の銘柄は逆指値に任せる。'
                  f'<br><span class="muted">{esc(ph["detail"])}</span></div>')
    elif ph["level"] == "caution":
        banner = ('<div class="banner warn">🟠 <b>相場判定：注意</b> — 日経平均が75日線の下で75日線も下向き。'
                  f'この局面は押し目買いの期待値がほぼゼロ。<b>1回の許容損失を{ph["risk_pct"]:g}%'
                  f'({b * ph["risk_pct"] / 100:,.0f}円)に下げ、同時保有は最大{ph["max_pos"]}銘柄。</b>'
                  '下の株数はこの設定で計算済み。'
                  f'<br><span class="muted">{esc(ph["detail"])}</span></div>')
    else:
        banner = (f'<div class="banner good">🟢 <b>相場判定：通常</b> — ルールどおり (許容損失{ph["risk_pct"]:g}% / '
                  f'同時保有最大{ph["max_pos"]}銘柄)<br><span class="muted">{esc(ph["detail"])}</span></div>')
    idx_html = ""
    for t, name in INDICES:
        s = ctx["indices"].get(t)
        if s:
            idx_html += (f"<div><span class='muted'>{name}</span><b>{s['close']:,.2f}</b>"
                         f"<span class='{'up' if s['chg'] >= 0 else 'down'}'>{fpct(s['chg'], 2)}</span>"
                         f" <span class='muted'>{'25日線↑' if s['above'] else '25日線↓'}</span></div>")
    mnews = "".join(f'<li><span class="d">{esc(x["date"])}</span> <a href="{esc(x["url"])}" target="_blank" rel="noopener">{esc(x["title"])}</a></li>'
                    for x in ctx["market_news"])
    sec_rows = lambda rows: "".join(f"<tr><td>{esc(s)}</td><td class='{'up' if v > 0 else 'down'}'>{fpct(v)}</td></tr>" for s, v in rows)
    cand_html = ""
    order = {"go": 0, "hot": 1, "weak": 2, "ng": 3}
    for key, name in STRATS.items():
        lst = [c for c in ctx["cands"] if c["strategy"] == key]
        shown = lst[:CFG["top_n"]]
        if ctx.get("live_at"):  # ザラ場チェック後は「買い条件OK」を先頭に
            shown.sort(key=lambda c: order.get((c.get("live") or {}).get("status"), 9))
        n_all = ctx.get("counts", {}).get(key, len(lst))
        cards = "".join(card(c) for c in shown) or "<p class='muted'>本日は該当なし</p>"
        cand_html += (f"<h2>{name} 候補 <span class='muted'>({n_all}銘柄中 上位{len(shown)})</span></h2>"
                      f"<p class='muted'>{STRAT_DESC[key]}</p><div class='grid'>{cards}</div>")
    live_banner = ""
    if ctx.get("live_at"):
        if ctx.get("live_ok"):
            live_banner = (f'<div class="banner info">🕘 {esc(ctx["live_at"])} 時点の株価で各候補を再チェック済み '
                           '(株価は最大20分ほど遅れます)。◎=買い条件OK / △=飛びつき注意・様子見 / ✕=見送り</div>')
        else:
            live_banner = (f'<div class="banner info">🕘 {esc(ctx["live_at"])} にチェックしましたが、'
                           '本日の取引データがまだありません (休場日 or 寄付き前)。</div>')
    rp = ph["risk_pct"] or CFG["caution_risk_pct"]
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>スイング候補スキャナー</title>
<style>{CSS}</style></head><body><main>
<h1>📈 スイング候補スキャナー <span class="muted" style="font-size:.9rem">(月単位・最長{CFG['max_hold_days']}営業日)</span></h1>
<p><a href="sim.html"><b>🧪 仮想売買シミュレーターを開く</b></a> <span class="muted">— お金を使わずにルールどおりの売買を練習</span>
・ <a href="check.html"><b>🔍 答え合わせ</b></a> <span class="muted">— 推奨の実際の成績</span></p>
<div class="muted">データ日付 {ctx['data_date']} ・ 更新 {ctx['generated']} JST ・ 対象 {ctx['n_scanned']:,}銘柄 (東証 プライム/スタンダード/グロース)</div>
{banner}
{live_banner}
<div class="idx">{idx_html}</div>
{cand_html}
<h2>推奨銘柄のポジション管理</h2>
<p class="muted">押し目買いの毎日の上位{CFG['history_picks_per_strategy']}銘柄を、ルールどおりに翌朝買った場合の保有状況です。
自分が同じ銘柄を持っているなら、<b>「明日の逆指値」に損切り注文を変更</b>してください (逆指値は上げるだけで、下げない)。</p>
{positions_section(ctx['hist'])}
<h2>セクターの強弱 (直近20日の中央値)</h2>
<div class="sect"><div class="tw"><table><thead><tr><th>強いセクター</th><th>20日</th></tr></thead><tbody>{sec_rows(ctx['sect_top'])}</tbody></table></div>
<div class="tw"><table><thead><tr><th>弱いセクター</th><th>20日</th></tr></thead><tbody>{sec_rows(ctx['sect_bottom'])}</tbody></table></div></div>
<h2>市況ニュース</h2><details open><summary>Google ニュース</summary><ul>{mnews}</ul></details>
<h2>この戦略の過去成績 (バックテスト・約1年半)</h2>
<p class="muted">シグナル翌日の寄付きで買い、最初の損切り = 買値 − ATR×{CFG['stop_atr']}。その後は毎日「買ってからの最高終値 − ATR×{CFG.get('trail_atr')}」まで損切りを引き上げ、最長{CFG['max_hold_days']}営業日で手仕舞い。手数料・スリッページとして往復{CFG['cost_pct']}%を差し引き。
*期待値 = 1回の許容損失を資金{b:,}円の{CFG['risk_per_trade_pct']}% ({b * CFG['risk_per_trade_pct'] / 100:,.0f}円)にした場合の1トレードあたり平均損益。現在上場している銘柄のみで計算しているため、実際より良く見える傾向があります。</p>
{bt_table(ctx['bt'])}
<h2>ルールの根拠 (5年間の検証)</h2>
<div class="rules"><p>2021年11月〜2026年11月の東証全銘柄で、前半3年でルールを比べ、後半2年で確かめました。</p>
<ul>
<li><b>売り方:</b> 旧ルール(利確2倍・10日)は1回あたり期待値 前半+0.03R / 後半+0.09R。トレーリングストップ・最長30日にすると 前半+0.12R / 後半+0.17R と両期間で改善。</li>
<li><b>除外:</b> ATRが株価の約3.6%を超える値動きの荒い銘柄は、両期間とも最も成績が悪いグループ(+0.03〜0.08R)。</li>
<li><b>短期の地合い (TOPIX 25日線):</b> 「地合い悪化の日は買わない」は後半2年では有効でしたが、前半3年では逆に成績を悪化させたため使っていません。</li>
<li><b>相場判定 (日経平均 75日線・200日線):</b> 75日線の下かつ75日線下向きの局面 (期間の約2割) では、押し目買いの1回あたり期待値が +0.18R → +0.02R とほぼゼロ。そのため「注意」では許容損失と同時保有数を減らします。急落で200日線を割った直後の買いはむしろ成績が良かったため、すぐには止めません。「停止」(200日線割れが続き75日線も200日線の下) は、この5年に長期の下げ相場がなく検証できていないため、安全のためのルールです。</li>
<li><b>保有期間:</b> 日計り(6手法)は全てマイナス、週単位はコストが少し増えるとマイナス。月単位が最も安定 (資金30万・最大5銘柄で年率中央値+14%)。空売り(戻り売り)は下げ局面でもマイナスでした。</li>
<li><b>5万円・最大3銘柄の試算:</b> 候補が毎日多数出るため、どれを選ぶかの運で結果が大きくぶれます。ランダムに選んだ場合の年率は中央値+7〜8%、10%の確率で年率マイナス、途中の最大下落は15〜30%程度。</li>
</ul></div>
<h2>実際の推奨の成績 (フォワードテスト)</h2>
<p class="muted">毎日、各戦略の上位{CFG['history_picks_per_strategy']}銘柄 (推奨) を自動で記録し、上と同じルールで売買した場合の結果を追跡しています。</p>
{fwd_section(ctx['hist'])}
<h2>資金{b:,}円での運用ルール</h2>
<ol class="rules">
<li><b>まずページ上部の相場判定を見る。</b>通常＝最大{CFG['max_positions']}銘柄・許容損失{CFG['risk_per_trade_pct']:g}% / 注意＝最大{CFG['caution_max_positions']}銘柄・{CFG['caution_risk_pct']:g}% / 停止＝新規の買いなし (現金で待つ)。</li>
<li><b>買うのは「押し目買い」候補の上位から。</b>朝のザラ場チェックで ◎ のものを、上の保有上限まで。決算発表をまたぐ銘柄は避ける。</li>
<li><b>1回の損失は資金の{rp:g}%({b * rp / 100:,.0f}円)まで。</b>買ったらすぐ「最初の損切り」に逆指値を入れる。株数はカードの💴の表示どおり (相場判定に合わせて計算済み)。</li>
<li><b>利確の指値は入れない。</b>毎日夕方このページの「ポジション管理」を見て、逆指値を「明日の逆指値」まで引き上げる (上げるだけ。下げない)。利益が出るほど損切りラインが上がり、利益を守りながら伸ばす。</li>
<li>逆指値に引っかかるか、{CFG['max_hold_days']}営業日たったら売る。ルール外の「期待」で持ち続けない。</li>
<li>100株単位で買えるのは株価{b // 100:,}円以下の銘柄だけ。それ以外は <b>単元未満株</b>(SBI証券「S株」、楽天証券「かぶミニ」など)で1株から買う。単元未満株は逆指値が使えない証券会社もあるので、その場合は夕方に終値が逆指値を下回っていたら翌朝売る。</li>
<li>勝率は4割程度。<b>負けが5〜6回続くのは普通</b>なので、ルールを途中で変えない。まず3ヶ月は5万円のまま続け、フォワードテストの成績がプラスか確認してから資金を増やす。</li>
</ol>
<p class="muted">※ 本ページは公開データから機械的に作成した参考情報であり、投資助言ではありません。株価データは遅延・欠損することがあります。売買の最終判断はご自身で行ってください。</p>
</main>
<script>
document.querySelectorAll('details.tv').forEach(function (d) {{
  d.addEventListener('toggle', function () {{
    if (!d.open || d.dataset.loaded) return;
    d.dataset.loaded = '1';
    var dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    var sym = d.dataset.sym;
    var f = document.createElement('iframe');
    f.loading = 'lazy';
    f.src = 'https://s.tradingview.com/widgetembed/?symbol=' + encodeURIComponent(sym) +
      '&interval=D&hidesidetoolbar=1&symboledit=0&saveimage=0&studies=%5B%5D&style=1' +
      '&timezone=Asia%2FTokyo&locale=ja&theme=' + (dark ? 'dark' : 'light');
    var box = d.querySelector('.tvbox');
    box.appendChild(f);
    var a = document.createElement('a');
    a.href = 'https://jp.tradingview.com/chart/?symbol=' + encodeURIComponent(sym);
    a.target = '_blank'; a.rel = 'noopener'; a.className = 'muted';
    a.textContent = '表示されない場合は TradingView で開く';
    box.appendChild(a);
  }});
}});
</script>
</body></html>"""


# ---------------------------------------------------------------- 通知 (任意)
def notify_discord(ctx: dict) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    page = os.environ.get("PAGE_URL") or (f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/" if "/" in repo else "")
    ph = ctx.get("phase") or {"level": "normal"}
    regime = {"normal": "🟢 相場判定:通常", "caution": "🟠 相場判定:注意 (許容損失・保有数を減らす)",
              "stop": "⛔ 相場判定:停止 (新規の買いなし)"}[ph["level"]]
    if ctx.get("live_at"):
        if not ctx.get("live_ok") or ph["level"] == "stop":
            return
        go = [c for c in ctx["cands"] if (c.get("live") or {}).get("status") == "go"]
        lines = [f"**🕘 {ctx['live_at']} ザラ場チェック** {regime}",
                 "◎ 買い条件OK: " + (" / ".join(
                     f"{c['code']} {c['name']} {c['live']['price']:,.0f}円 (損切{c['live']['stop']:,.0f})"
                     for c in go[:8]) or "なし")]
    else:
        lines = [f"**スイング候補 {ctx['data_date']}** {regime}"]
        for key, name in STRATS.items():
            top = [c for c in ctx["cands"] if c["strategy"] == key][:5]
            lines.append(f"__{name}__: " + (" / ".join(f"{c['code']} {c['name']} {c['price']:,.0f}円" for c in top) or "なし"))
    lines.append(page)
    body = json.dumps({"content": "\n".join(lines)[:1900]}).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log(f"Discord通知失敗: {e}")


# ---------------------------------------------------------------- ザラ場チェック (軽量)
def judge_live(c: dict, o: float, h: float, l: float, p: float) -> dict:
    """前日終値で出たシグナルが、今の値段でもまだ有効かを判定する。"""
    prev, atr = c["price"], c["atr"]
    if l <= c["stop"] or p <= c["stop"]:
        st, label = "ng", "✕ 損切りラインに到達 → シグナル無効・見送り"
    elif p - prev > atr:
        st, label = "hot", f"△ 前日比 {p / prev - 1:+.1%} と上がりすぎ → 飛びつかず押し目待ち"
    elif o < prev - 0.5 * atr or p < prev - 0.7 * atr:
        st, label = "weak", "△ 弱い動き → 様子見 (切り返してから判断)"
    else:
        st, label = "go", "◎ 買い条件OK"
    risk = CFG["stop_atr"] * atr
    return {"status": st, "label": label, "price": p, "open": o, "chg": p / prev - 1, "gap": o / prev - 1,
            "stop": p - risk, "target": p + CFG["target_r"] * risk if CFG.get("target_r") else None}


def run_live() -> None:
    ctx = json.loads(LATEST_FILE.read_text(encoding="utf-8"))
    ctx["hist"] = load_hist()
    today = dt.datetime.now(JST).date()
    tickers = [c["ticker"] for c in ctx["cands"]]
    log(f"ザラ場チェック: 候補 {len(tickers)} 銘柄 + 指数")
    live = download(tickers + [t for t, _ in INDICES], "5d", need_volume=False, min_rows=1)
    n_ok = 0
    for c in ctx["cands"]:
        c.pop("live", None)
        d = live.get(c["ticker"])
        if d is None or d.index[-1].date() != today or d.index[-1].strftime("%Y-%m-%d") <= ctx["data_date"]:
            continue
        r = d.iloc[-1]
        c["live"] = judge_live(c, float(r.Open), float(r.High), float(r.Low), float(r.Close))
        n_ok += 1
    for t, _ in INDICES:
        d = live.get(t)
        if d is not None and len(d) >= 2 and t in ctx["indices"]:
            ctx["indices"][t].update(close=float(d.Close.iloc[-1]), chg=float(d.Close.iloc[-1] / d.Close.iloc[-2] - 1))
    ctx["live_at"] = dt.datetime.now(JST).strftime("%m/%d %H:%M")
    ctx["live_ok"] = n_ok > 0
    # 答え合わせ用: その日の最初の朝/昼チェックの判定を記録に残す
    slot = "live_am" if dt.datetime.now(JST).hour < 11 else "live_pm"
    judged = {(c["ticker"], c["strategy"]): c["live"]["status"] for c in ctx["cands"] if c.get("live")}
    tagged = 0
    for p in ctx["hist"]:
        k = (p["ticker"], p["strategy"])
        if p["date"] == ctx["data_date"] and k in judged and slot not in p:
            p[slot] = judged[k]
            tagged += 1
    if tagged:
        save_hist(ctx["hist"])
    write_outputs(ctx)
    notify_discord(ctx)
    log(f"完了: {n_ok} 銘柄を判定")


def write_price_files(ind: dict, uni: pd.DataFrame) -> None:
    """仮想売買シミュレーター用: 銘柄ごとに直近80日の日足と ATR を docs/p/<code>.json に書き出す。"""
    pdir = DOCS / "p"
    pdir.mkdir(parents=True, exist_ok=True)
    names = uni.set_index("code")["name"].to_dict()
    rnd = lambda s: [round(float(v), 2) if v == v else None for v in s]
    idx = {}
    for t, d in ind.items():
        x = d.tail(80)
        code = t[:-2]
        idx[code] = names.get(code, code)
        obj = {"code": code, "name": idx[code], "d": [i.strftime("%Y-%m-%d") for i in x.index],
               "o": rnd(x.Open), "h": rnd(x.High), "l": rnd(x.Low), "c": rnd(x.Close), "a": rnd(x.atr)}
        (pdir / f"{code}.json").write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (pdir / "_names.json").write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def sim_page(ctx: dict) -> str:
    ph = ctx.get("phase") or {"level": "normal", "risk_pct": CFG["risk_per_trade_pct"], "max_pos": CFG["max_positions"]}
    cfg = {k: CFG[k] for k in ("budget", "stop_atr", "trail_atr", "max_hold_days", "cost_pct")}
    cfg |= {"phase": ph, "phase_risk_pct": ph["risk_pct"] or CFG["caution_risk_pct"]}
    tpl = (BASE / "sim_template.html").read_text(encoding="utf-8")
    for k, v in (("__CSS__", CSS), ("__CFG_JSON__", json.dumps(cfg, ensure_ascii=False)),
                 ("__MAXD__", str(CFG["max_hold_days"])), ("__STOPK__", f"{CFG['stop_atr']:g}"),
                 ("__TRAILK__", f"{CFG['trail_atr']:g}"), ("__COST__", f"{CFG['cost_pct']:g}"),
                 ("__DATE__", esc(ctx.get("data_date", "")))):
        tpl = tpl.replace(k, v)
    return tpl


def write_outputs(ctx: dict, all_cands: list[dict] | None = None) -> None:
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(render(ctx), encoding="utf-8")
    (DOCS / "sim.html").write_text(sim_page(ctx), encoding="utf-8")
    (DOCS / "check.html").write_text(check_page(ctx), encoding="utf-8")
    (DOCS / "data.json").write_text(json.dumps(
        {k: ctx.get(k) for k in ("data_date", "generated", "live_at", "regime_ok", "phase", "bt")} |
        {"candidates": [{k: v for k, v in c.items() if k not in ("spark", "spark25")}
                        for c in (all_cands or ctx["cands"])]},
        ensure_ascii=False, indent=1, default=str), encoding="utf-8")


# ---------------------------------------------------------------- メイン
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="テスト用: 銘柄数を絞る")
    ap.add_argument("--no-news", action="store_true", help="ニュース・開示・決算日の取得を省略")
    ap.add_argument("--live", action="store_true", help="ザラ場チェック: 前回の候補だけ現在値で再判定")
    args = ap.parse_args()
    if args.live:
        if LATEST_FILE.exists():
            return run_live()
        log("前回の結果がないため通常スキャンを実行")

    uni = load_universe()
    if args.limit:
        uni = uni.sample(min(args.limit, len(uni)), random_state=1)
    log("主要指数を取得")
    idx_frames = download([t for t, _ in INDICES], "2y", need_volume=False)
    log(f"株価を取得: {len(uni)} 銘柄")
    frames = download([f"{c}.T" for c in uni.code], CFG["history_period"])
    if not frames:
        raise SystemExit("株価データが取得できませんでした")
    now = dt.datetime.now(JST)
    if now.hour * 60 + now.minute < 15 * 60 + 45:  # 大引け前の実行なら作りかけの当日足は使わない
        today = pd.Timestamp(now.date())
        frames = {t: d[d.index < today] for t, d in frames.items()}
        idx_frames = {t: d[d.index < today] if t.endswith(".T") or t == "^N225" else d
                      for t, d in idx_frames.items()}

    ind = {t: add_indicators(d) for t, d in frames.items() if len(d) >= WARMUP}
    last_date = pd.Series([d.index[-1] for d in ind.values()]).mode().max()
    regime = regime_series(idx_frames)
    regime_ok = bool(regime.iloc[-1]) if len(regime) else True
    phase = market_phase(idx_frames)
    log(f"相場判定: {phase['level']} ({phase['detail']})")

    indices = {}
    for t, d in idx_frames.items():
        s25 = d.Close.rolling(25).mean()
        indices[t] = {"close": float(d.Close.iloc[-1]), "chg": float(d.Close.iloc[-1] / d.Close.iloc[-2] - 1),
                      "above": bool(d.Close.iloc[-1] > s25.iloc[-1])}

    log("バックテスト")
    bt, bt_r = backtest(ind, regime)
    log("候補抽出")
    cands = make_candidates(ind, uni, last_date, phase["risk_pct"] or CFG["caution_risk_pct"])
    shown = []
    for key in STRATS:
        shown += [c for c in cands if c["strategy"] == key][:CFG["top_n"]]
    if not args.no_news:
        log(f"ニュース・開示・決算日を取得 ({len(shown)}銘柄)")
        enrich(shown[: CFG["news_top_n"] * len(STRATS)])

    sec = uni.set_index("code")["sector"].to_dict()
    r20 = pd.DataFrame([{"sector": sec.get(t[:-2]), "r": d.ret20.iloc[-1]} for t, d in ind.items()
                        if d.index[-1] == last_date]).dropna()
    g = r20.groupby("sector").r.agg(["median", "count"])
    g = g[g["count"] >= 5]["median"].sort_values(ascending=False)

    hist = update_history(ind, cands, last_date, regime_ok, phase["level"])
    ctx = {
        "data_date": last_date.strftime("%Y-%m-%d"), "generated": dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "n_scanned": len(ind), "regime_ok": regime_ok, "phase": phase, "indices": indices, "cands": shown, "bt": bt,
        "bt_r": bt_r,
        "counts": {k: sum(1 for c in cands if c["strategy"] == k) for k in STRATS},
        "hist": hist, "market_news": [] if args.no_news else gnews("日経平均 株式市場 when:1d", 8),
        "sect_top": list(g.head(6).items()), "sect_bottom": list(g.tail(6)[::-1].items()),
    }
    write_outputs(ctx, cands)
    write_price_files(ind, uni)
    LATEST_FILE.parent.mkdir(exist_ok=True)
    LATEST_FILE.write_text(json.dumps({k: v for k, v in ctx.items() if k != "hist"},
                                      ensure_ascii=False, default=str), encoding="utf-8")
    notify_discord(ctx)
    log(f"完了: 候補 {len(cands)} 件 → docs/index.html")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
