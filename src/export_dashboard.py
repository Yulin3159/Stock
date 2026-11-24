"""把排行結果整理成儀表板用的精簡資料檔（output/dashboard/）。

- ranking.csv  所有合格標的（英文欄名，殖利率以 % 表示）
- monthly.csv  綜合 / 個股 / ETF 各前 100 名的近五年月均價與當月現金股利
- forecast.csv 所有有資料標的的去年 / 今年除權息日、每股股利（原始未調整金額）與今年配息預估

金額基準：ex_2025 / ex_2026 列出公告當時的實際每股金額；div_2025、paid_2026、est_2026 等加總
則換算成「目前每一股」的基準（已依之後的配股 / 分割調整），可直接乘上現在持有股數。

今年配息預估：今年已除息的金額為實際值；去年有、但今年尚未發生的配息視為「待除息」，
季配 / 月配等一年多次配息者以今年最近一次金額估算，年配者以去年同期金額估算。
去年同期日期已過、今年仍未除息者不列入預估，另標註「去年同期已過，今年未除息」。
"""
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output" / "dashboard"


YEAR = 2026  # 今年


def raw_factor(dates, splits):
    """Yahoo 股利與股價皆已依「之後的」配股 / 分割調整；乘回這個係數得到當時實際每股金額。"""
    out = []
    for d in dates:
        f = 1.0
        for sd, r in splits:
            if sd > d:
                f *= r
        out.append(f)
    return out


def rights(st, year):
    """配股（除權）以每千股配幾股表示；比例 ≥ 1.5 視為股票分割。"""
    out = []
    for t, r in st:
        if t.year != year:
            continue
        out.append(f"{t:%m/%d} 分割 1 拆 {r:g}" if r >= 1.5 else f"{t:%m/%d} 每千股配 {(r - 1) * 1000:.0f} 股")
    return "、".join(out)


def build_forecast(stocks, end):
    d = pd.read_csv(ROOT / "data" / "dividends.csv", dtype={"stock_id": str}, parse_dates=["date"])
    sp = pd.read_csv(ROOT / "data" / "splits.csv", dtype={"stock_id": str}, parse_dates=["date"])
    d = d[d["date"].dt.year >= YEAR - 1]
    dg = {k: g.sort_values("date") for k, g in d.groupby("stock_id")}
    sg = {k: list(zip(g["date"], g["ratio"])) for k, g in sp.groupby("stock_id")}
    fmt = lambda t: t.strftime("%m/%d")
    rows = []
    for s in stocks.itertuples():
        p = ROOT / "data" / "prices" / f"{s.stock_id}.csv.gz"
        if not p.exists():
            continue
        px = pd.read_csv(p, parse_dates=["date"])
        px = px[px["close"] > 0]
        if px.empty or px["date"].max() < end - pd.Timedelta(days=30):
            continue  # 已下市 / 停止交易
        splits = sg.get(s.stock_id, [])
        ev = dg.get(s.stock_id)
        if ev is None:
            ev = pd.DataFrame({"date": pd.to_datetime([]), "cash_dividend": pd.Series(dtype=float)})
        ev = ev.assign(raw=ev["cash_dividend"].to_numpy() * pd.Series(raw_factor(list(ev["date"]), splits), dtype=float).to_numpy())
        e25 = ev[ev["date"].dt.year == YEAR - 1]
        e26 = ev[ev["date"].dt.year == YEAR]
        st = [(dt_, r) for dt_, r in splits if dt_.year >= YEAR - 1 and r > 1]  # 配股（除權）
        px25 = px[px["date"].dt.year == YEAR - 1]
        div25_adj = e25["cash_dividend"].sum()
        y25 = div25_adj / px25["close"].mean() * 100 if len(px25) >= 120 and div25_adj > 0 else (0.0 if len(px25) >= 120 else float("nan"))
        freq = max(len(e25), len(e26))
        pend, missed = [], []
        last_amt = e26["cash_dividend"].iloc[-1] if len(e26) else None
        for i, r in enumerate(e25.itertuples()):
            if i < len(e26):
                continue
            exp = r.date + pd.DateOffset(years=1)
            if exp <= end:
                missed.append(fmt(r.date))
                continue
            amt = last_amt if (freq >= 2 and last_amt is not None) else r.cash_dividend
            pend.append((exp, amt))
        paid26 = e26["cash_dividend"].sum()
        est26 = paid26 + sum(a for _, a in pend)
        close = px["close"].iloc[-1]
        events = lambda e: "、".join(f"{fmt(t)} ${a:g}" for t, a in zip(e["date"], e["raw"].round(4)))
        rows.append({
            "id": s.stock_id, "name": s.stock_name, "market": "上市" if s.type == "twse" else "上櫃",
            "kind": "ETF" if s.stock_id.startswith("00") else "個股",
            "close": round(close, 2),
            "div_2025": round(e25["cash_dividend"].sum(), 4), "yield_2025": round(y25, 2),
            "times_2025": len(e25), "ex_2025": events(e25),
            "rights_2025": rights(st, YEAR - 1),
            "paid_2026": round(paid26, 4), "ex_2026": events(e26),
            "rights_2026": rights(st, YEAR),
            "pending_2026": "、".join(f"{fmt(t)} ${a:g}" for t, a in [(t, round(a, 4)) for t, a in pend]),
            "next_ex": pend[0][0].strftime("%Y-%m-%d") if pend else "",
            "next_amt": round(pend[0][1], 4) if pend else "",
            "pays_2026": ";".join(f"{a:.4f}" for a in list(e26["cash_dividend"]) + [a for _, a in pend]),
            "est_2026": round(est26, 4), "est_yield_2026": round(est26 / close * 100, 2) if close else "",
            "missed_2026": ("去年 " + "、".join(missed) + " 有配，今年同期已過未除息") if missed else "",
        })
    return pd.DataFrame(rows)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    e = pd.read_csv(ROOT / "output" / "all_eligible_dividend_yield.csv", dtype={"代號": str})
    ycols = [c for c in e.columns if c.startswith("殖利率 ")]
    r = pd.DataFrame({
        "rank": e["排名"], "id": e["代號"], "name": e["名稱"], "market": e["市場"],
        "kind": e["類型"], "industry": e["產業"].fillna(""),
        "avg_yield": (e["近五年平均殖利率"] * 100).round(2),
        "min_yield": (e["近五年最低殖利率"] * 100).round(2),
        "years_paid": e["配息年數(近5年)"],
        **{f"y{i+1}": (e[c] * 100).round(2) for i, c in enumerate(ycols)},
        "div5y": e["近五年現金股利合計"].round(3),
        "close": e["最新收盤價"].round(2),
        "ttm_yield": (e["近一年殖利率(以最新價)"] * 100).round(2),
        "ret5y": (e["近五年含息報酬率"] * 100).round(1),
        "lots": e["近一年日均量(張)"].round(0).astype(int),
        "note": e["備註"].fillna(""),
    })
    log = pd.read_csv(ROOT / "data" / "fetch_log.csv", parse_dates=["last_date"])
    stocks = pd.read_csv(ROOT / "data" / "stock_list.csv", dtype={"stock_id": str})
    fc = build_forecast(stocks, log["last_date"].max())
    fc.to_csv(OUT / "forecast.csv", index=False)
    keep = ["id", "div_2025", "paid_2026", "yield_2025", "ex_2025", "ex_2026", "pending_2026", "est_2026", "est_yield_2026", "missed_2026"]
    r = r.merge(fc[keep], on="id", how="left")
    r.to_csv(OUT / "ranking.csv", index=False)

    ids = set(r.head(100)["id"]) | set(r[r.kind == "個股"].head(100)["id"]) | set(r[r.kind == "ETF"].head(100)["id"])
    d = pd.read_csv(ROOT / "data" / "dividends.csv", dtype={"stock_id": str}, parse_dates=["date"])
    start = pd.Timestamp(ycols[0].split(" ")[1].split("~")[0])
    rows = []
    for sid in sorted(ids):
        px = pd.read_csv(ROOT / "data" / "prices" / f"{sid}.csv.gz", parse_dates=["date"])
        px = px[px["date"] > start]
        m = px.set_index("date")["close"].resample("MS").mean()
        dv = d[(d.stock_id == sid) & (d.date > start)].set_index("date")["cash_dividend"].resample("MS").sum()
        mm = pd.DataFrame({"close": m.round(2), "dividend": dv.reindex(m.index).fillna(0).round(4)})
        mm["id"] = sid
        mm["month"] = mm.index.strftime("%Y-%m")
        rows.append(mm[["id", "month", "close", "dividend"]])
    pd.concat(rows).dropna(subset=["close"]).to_csv(OUT / "monthly.csv", index=False)
    print(len(r), "ranking rows;", len(ids), "stocks in monthly;", len(fc), "forecast rows")


if __name__ == "__main__":
    main()
