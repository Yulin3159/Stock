"""把排行結果整理成儀表板用的精簡資料檔（output/dashboard/）。

- ranking.csv  所有合格標的（英文欄名，殖利率以 % 表示）
- monthly.csv  綜合 / 個股 / ETF 各前 100 名的近五年月均價與當月現金股利
"""
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output" / "dashboard"


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
    print(len(r), "ranking rows;", len(ids), "stocks in monthly")


if __name__ == "__main__":
    main()
