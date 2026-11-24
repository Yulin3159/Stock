"""計算近五年現金殖利率，排出前 100 名。

定義（以資料最後交易日 END 往回切 5 個滾動 12 個月區間）：
  區間 k（k=1..5）：(END - k 年, END - (k-1) 年]
  區間殖利率 = 該區間除息之現金股利合計 / 該區間平均收盤價
  近五年平均殖利率 = 5 個區間殖利率的平均

Yahoo 會把股票股利當成 split 並同步調整歷史股價與股利，
因此「股利 / 收盤價」是同一基準，比值不受配股影響。

篩選條件（可用參數調整）：
  - 上市櫃滿 5 年（有涵蓋整個 5 年區間的價格資料）
  - 目前仍在交易（最後交易日距 END 不超過 30 天）
  - 5 個區間中至少 --min-years 個區間有配息（預設 4）
  - 近一年日均成交量 ≥ --min-lots 張（預設 100 張，排除流動性極差標的）
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output"


def load_prices(sid: str) -> pd.DataFrame | None:
    p = DATA / "prices" / f"{sid}.csv.gz"
    if not p.exists():
        return None
    df = pd.read_csv(p, parse_dates=["date"])
    return df[df["close"] > 0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-years", type=int, default=4)
    ap.add_argument("--min-lots", type=float, default=100)
    ap.add_argument("--top", type=int, default=100)
    args = ap.parse_args()

    stocks = pd.read_csv(DATA / "stock_list.csv", dtype={"stock_id": str})
    divs = pd.read_csv(DATA / "dividends.csv", dtype={"stock_id": str}, parse_dates=["date"])
    divs = divs[divs["cash_dividend"] > 0]
    log = pd.read_csv(DATA / "fetch_log.csv", dtype={"stock_id": str}, parse_dates=["last_date"])
    end = log["last_date"].max()
    bounds = [end - pd.DateOffset(years=k) for k in range(6)]  # bounds[0]=END ... bounds[5]=END-5y
    win_labels = [f"{bounds[k].date()}~{bounds[k-1].date()}" for k in range(5, 0, -1)]  # 舊→新

    div_by = {sid: g for sid, g in divs.groupby("stock_id")}
    rows = []
    for s in stocks.itertuples():
        px = load_prices(s.stock_id)
        if px is None or px.empty:
            continue
        d = div_by.get(s.stock_id, divs.iloc[:0])
        rec = {"代號": s.stock_id, "名稱": s.stock_name,
               "市場": "上市" if s.type == "twse" else "上櫃",
               "類型": "ETF" if s.stock_id.startswith("00") else "個股", "產業": s.industry,
               "首筆資料日": px["date"].min().date(), "最後交易日": px["date"].max().date()}
        ylds, sums = [], []
        for k in range(5, 0, -1):  # 舊→新
            lo, hi = bounds[k], bounds[k - 1]
            w = px[(px["date"] > lo) & (px["date"] <= hi)]
            dsum = d[(d["date"] > lo) & (d["date"] <= hi)]["cash_dividend"].sum()
            y = dsum / w["close"].mean() if len(w) >= 120 else float("nan")
            ylds.append(y)
            sums.append(dsum)
        for lab, y, ds in zip(win_labels, ylds, sums):
            rec[f"殖利率 {lab}"] = y
            rec[f"股利 {lab}"] = ds
        last = px.iloc[-1]
        y1 = px[px["date"] > bounds[1]]
        y5 = px[px["date"] > bounds[5]]
        rec.update({
            "近五年平均殖利率": pd.Series(ylds).mean(skipna=False),
            "近五年最低殖利率": min(ylds),
            "配息年數(近5年)": int(sum(1 for x in sums if x > 0)),
            "近五年現金股利合計": sum(sums),
            "最新收盤價": last["close"],
            "近一年殖利率(以最新價)": sums[-1] / last["close"],
            "近五年含息報酬率": (y5["adjclose"].iloc[-1] / y5["adjclose"].iloc[0] - 1) if len(y5) > 1 else float("nan"),
            "近一年日均量(張)": y1["volume"].mean() / 1000 if len(y1) else 0.0,
            "_covers5y": px["date"].min() <= bounds[5] + pd.Timedelta(days=10),
            "_active": px["date"].max() >= end - pd.Timedelta(days=30),
        })
        rows.append(rec)

    df = pd.DataFrame(rows)
    ok = (df["_covers5y"] & df["_active"] & df["近五年平均殖利率"].notna()
          & (df["配息年數(近5年)"] >= args.min_years)
          & (df["近一年日均量(張)"] >= args.min_lots))
    elig = df[ok].drop(columns=["_covers5y", "_active"]) \
                 .sort_values("近五年平均殖利率", ascending=False).reset_index(drop=True)
    elig.insert(0, "排名", range(1, len(elig) + 1))
    # 單一年度殖利率異常高（>25%）通常是減資退還現金或資料異常，標記供人工確認
    ycols = [c for c in elig.columns if c.startswith("殖利率 ")]
    elig["備註"] = elig[ycols].max(axis=1).gt(0.25).map({True: "單年殖利率>25%，請人工確認", False: ""})
    top = elig.head(args.top)

    OUT.mkdir(exist_ok=True)
    top.to_csv(OUT / "top100_dividend_yield.csv", index=False, encoding="utf-8-sig")
    elig.to_csv(OUT / "all_eligible_dividend_yield.csv", index=False, encoding="utf-8-sig")
    note = pd.DataFrame({"說明": [
        f"資料截止日：{end.date()}",
        "近五年 = 以資料截止日往回 5 個滾動 12 個月區間：" + "、".join(win_labels),
        "區間殖利率 = 區間內除息之現金股利合計 ÷ 區間平均收盤價；近五年平均殖利率 = 5 區間平均",
        f"篩選：上市櫃滿 5 年、仍在交易、近 5 年至少 {args.min_years} 年有配息、近一年日均量 ≥ {args.min_lots:g} 張",
        "股利與股價皆經股票股利（配股）調整，比值不受配股影響；殖利率不含股票股利本身的價值",
        "資料來源：股票清單 FinMind；股價與股利 Yahoo Finance。僅供研究參考，不構成投資建議",
    ]})
    pct = ycols + ["近五年平均殖利率", "近五年最低殖利率", "近一年殖利率(以最新價)", "近五年含息報酬率"]
    with pd.ExcelWriter(OUT / "top100_dividend_yield.xlsx", engine="openpyxl") as xw:
        sheets = [("Top100", top),
                  ("Top100 個股", elig[elig["類型"] == "個股"].head(args.top)),
                  ("Top100 ETF", elig[elig["類型"] == "ETF"].head(args.top)),
                  ("全部合格標的", elig), ("說明", note)]
        for name, frame in sheets:
            frame.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for j, col in enumerate(frame.columns, 1):
                letter = ws.cell(row=1, column=j).column_letter
                ws.column_dimensions[letter].width = 60 if name == "說明" else max(10, min(28, len(str(col)) * 1.6))
                fmt = "0.00%" if col in pct else ("#,##0.00" if frame[col].dtype.kind == "f" else None)
                if fmt:
                    for c in ws[letter][1:]:
                        c.number_format = fmt
            ws.freeze_panes = "D2" if name != "說明" else None
    print(f"資料截止 {end.date()}；全部 {len(df)} 檔，合格 {len(elig)} 檔")
    show = top[["排名", "代號", "名稱", "市場", "類型", "近五年平均殖利率", "近五年最低殖利率", "配息年數(近5年)", "最新收盤價", "近一年日均量(張)", "備註"]].copy()
    for c in ["近五年平均殖利率", "近五年最低殖利率"]:
        show[c] = (show[c] * 100).round(2)
    print(show.to_string(index=False))


if __name__ == "__main__":
    main()
