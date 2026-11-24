"""股利「入帳」行事曆：每一筆現金股利 / 股票股利的除權息日、入帳（發放）日與每股金額。

資料來源優先順序：
1. FinMind 配息公告（data/finmind_dividends/<id>.json）：有實際的現金股利發放日、股票股利與
   已公告但尚未除息的配息。
2. Yahoo Finance（data/dividends.csv、data/splits.csv）：只有除權息日，入帳日用推估。

推估規則：
- 入帳日 = 除息日 + 該檔過去的平均間隔天數；沒有紀錄就用同類（個股 / ETF）的中位數，
  再沒有則個股 30 天、ETF 24 天。
- 股票股利（配股）的入帳日以除權日 + 30 天推估。
- 2027 年：今年每一筆現金配息往後推一年，金額沿用今年同期（保守估計，不假設成長）。
- 尚未公告的配息沿用 forecast 的規則：去年有、今年還沒發生者，季配月配以今年最近一次金額、
  年配以去年同期金額估算。

金額基準：amount_raw 是公告當時的每股金額；amount 已換算成「目前每一股」（依之後的配股 / 分割調整），
可直接乘上現在的持股數。股票股利的 amount 是每一股配發的股數（例如 0.01 = 每千股配 10 股）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
YEAR = 2026
DEFAULT_LAG = {"個股": 30, "ETF": 24}
STOCK_LAG = 30


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def finmind_events(sid: str) -> list[dict] | None:
    p = ROOT / "data" / "finmind_dividends" / f"{sid}.json"
    if not p.exists():
        return None
    out = []
    for r in json.loads(p.read_text()):
        cash = _f(r.get("CashEarningsDistribution")) + _f(r.get("CashStatutorySurplus"))
        stock = _f(r.get("StockEarningsDistribution")) + _f(r.get("StockStatutorySurplus"))
        cex, cpay, sex = r.get("CashExDividendTradingDate"), r.get("CashDividendPaymentDate"), r.get("StockExDividendTradingDate")
        if cash > 0 and cex:
            out.append({"type": "cash", "ex": pd.Timestamp(cex), "pay": pd.Timestamp(cpay) if cpay else None,
                        "raw": cash, "src": "FinMind"})
        if stock > 0 and sex:
            out.append({"type": "stock", "ex": pd.Timestamp(sex), "pay": None, "raw": stock / 10, "src": "FinMind"})
    return out


def build(stocks: pd.DataFrame, end: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
    """回傳 (payments 明細, 每檔 2026 入帳彙總)。stocks 需有 id, name, kind 欄。"""
    d = pd.read_csv(ROOT / "data" / "dividends.csv", dtype={"stock_id": str}, parse_dates=["date"])
    sp = pd.read_csv(ROOT / "data" / "splits.csv", dtype={"stock_id": str}, parse_dates=["date"])
    d = d[d["date"] >= f"{YEAR - 2}-10-01"]
    dg = {k: g.sort_values("date") for k, g in d.groupby("stock_id")}
    sg = {k: list(zip(g["date"], g["ratio"])) for k, g in sp.groupby("stock_id")}

    def factor_after(sid, t):
        f = 1.0
        for sd, r in sg.get(sid, []):
            if sd > t:
                f *= r
        return f

    # 1) 收集事件
    per = {}
    for s in stocks.itertuples():
        ev = finmind_events(s.id)
        if ev is None:
            ev = []
            for r in dg.get(s.id, pd.DataFrame(columns=["date", "cash_dividend"])).itertuples():
                ev.append({"type": "cash", "ex": r.date, "pay": None,
                           "raw": r.cash_dividend * factor_after(s.id, r.date), "src": "Yahoo"})
            for t, r in sg.get(s.id, []):
                if 1 < r < 1.5 and t >= pd.Timestamp(f"{YEAR - 2}-10-01"):
                    ev.append({"type": "stock", "ex": t, "pay": None, "raw": r - 1, "src": "Yahoo"})
        for e in ev:
            e["amount"] = e["raw"] / factor_after(s.id, e["ex"])
        per[s.id] = sorted(ev, key=lambda e: e["ex"])

    # 2) 入帳間隔：個別標的的平均，否則同類中位數
    lags = {}
    for s in stocks.itertuples():
        ls = [(e["pay"] - e["ex"]).days for e in per[s.id] if e["type"] == "cash" and e["pay"] is not None]
        if ls:
            lags[s.id] = sum(ls) / len(ls)
    kind_of = dict(zip(stocks["id"], stocks["kind"]))
    kind_lag = {}
    for k in DEFAULT_LAG:
        v = [lags[i] for i in lags if kind_of.get(i) == k]
        kind_lag[k] = float(pd.Series(v).median()) if len(v) >= 5 else DEFAULT_LAG[k]

    rows, summary = [], []
    for s in stocks.itertuples():
        ev = per[s.id]
        lag = lags.get(s.id, kind_lag[s.kind])
        for e in ev:
            if e["pay"] is None:
                e["pay"] = e["ex"] + pd.Timedelta(days=round(lag if e["type"] == "cash" else STOCK_LAG))
                e["pay_est"] = True
            else:
                e["pay_est"] = False
        # 3) 尚未公告的現金股利推估
        cash = [e for e in ev if e["type"] == "cash"]
        c25 = [e for e in cash if e["ex"].year == YEAR - 1]
        c26 = [e for e in cash if e["ex"].year == YEAR]
        freq = max(len(c25), len(c26))
        last_amt = c26[-1]["amount"] if c26 else None
        for i, e in enumerate(c25):
            if i < len(c26):
                continue
            exp = e["ex"] + pd.DateOffset(years=1)
            if exp <= end:
                continue
            ev.append({"type": "cash", "ex": exp, "pay": exp + (e["pay"] - e["ex"]), "pay_est": True,
                       "raw": last_amt if (freq >= 2 and last_amt is not None) else e["amount"],
                       "amount": last_amt if (freq >= 2 and last_amt is not None) else e["amount"],
                       "src": "推估", "projected": True})
        # 4) 明年（2027）推估：今年每一筆現金配息（含上面推估的待除息）往後推一年，金額沿用今年同期。配股不推估。
        c26_all = sorted([e for e in ev if e["type"] == "cash" and e["ex"].year == YEAR], key=lambda e: e["ex"])
        for e in c26_all:
            amt = e["amount"]
            ex2 = e["ex"] + pd.DateOffset(years=1)
            ev.append({"type": "cash", "ex": ex2, "pay": ex2 + (e["pay"] - e["ex"]), "pay_est": True,
                       "raw": amt, "amount": amt, "src": "推估", "projected": True, "next_year": True})
        recv_cash = recv_done = recv_stock = recv_next = 0.0
        pays = []
        for e in sorted(ev, key=lambda e: e["pay"]):
            proj = e.get("projected", False)
            if e.get("next_year"):
                status = f"預估（{YEAR + 1} 年）"
            elif proj:
                status = "預估（尚未公告）"
            elif e["ex"] > end:
                status = "已公告，待除息"
            elif e["pay"] > end:
                status = "已除息，待入帳"
            else:
                status = "已入帳"
            if e["pay"].year < YEAR - 1 or e["pay"].year > YEAR + 1:
                continue
            rows.append({
                "id": s.id, "type": "現金" if e["type"] == "cash" else "股票",
                "ex_date": e["ex"].strftime("%Y-%m-%d"), "pay_date": e["pay"].strftime("%Y-%m-%d"),
                "pay_est": int(bool(e["pay_est"])), "status": status, "source": e["src"],
                "amount_raw": round(e["raw"], 6), "amount": round(e["amount"], 6),
            })
            if e["pay"].year == YEAR + 1 and e["type"] == "cash":
                recv_next += e["amount"]
            if e["pay"].year == YEAR:
                if e["type"] == "cash":
                    recv_cash += e["amount"]
                    pays.append(e["amount"])
                    if status == "已入帳":
                        recv_done += e["amount"]
                else:
                    recv_stock += e["amount"]
        summary.append({"id": s.id, "recv_2026": round(recv_cash, 4), "recv_done_2026": round(recv_done, 4),
                        "stock_2026": round(recv_stock, 6), "recv_2027": round(recv_next, 4), "pay_lag": round(lag), "pay_src": "FinMind" if any(e["src"] == "FinMind" for e in ev) else "推估"})
    return pd.DataFrame(rows), pd.DataFrame(summary)
