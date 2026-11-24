"""撈取台股（上市 + 上櫃）近十年日線價格與配息資料。

資料來源：
- 股票清單：FinMind TaiwanStockInfo（免 token）
- 日線 OHLCV、現金股利、股票股利（以 split 呈現）：Yahoo Finance chart API

輸出：
- data/stock_list.csv           股票清單
- data/prices/<id>.csv.gz        個股近十年日線（含還原權值 adjclose）
- data/dividends.csv             所有現金股利事件（已依股票股利調整，與 close 同基準）
- data/splits.csv                股票股利 / 分割事件
- data/fetch_log.csv             每檔抓取結果
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PRICES = DATA / "prices"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
TZ = dt.timezone(dt.timedelta(hours=8))


def fetch_stock_list() -> pd.DataFrame:
    r = requests.get("https://api.finmindtrade.com/api/v4/data",
                     params={"dataset": "TaiwanStockInfo"}, headers=UA, timeout=60)
    r.raise_for_status()
    df = pd.DataFrame(r.json()["data"])
    df = df[df["type"].isin(["twse", "tpex"]) & (df["industry_category"] != "Index")].copy()
    df["stock_name"] = df["stock_name"].str.rstrip("*")
    # 同一檔可能屬多個產業分類、或曾轉換上市櫃別：產業合併，市場別取最新一筆
    inds = df.groupby("stock_id")["industry_category"].agg(lambda s: "/".join(sorted(set(s))))
    df = (df.sort_values("date").groupby("stock_id").tail(1)
            [["stock_id", "stock_name", "type"]].set_index("stock_id"))
    df["industry"] = inds
    df = df.reset_index()
    df["yahoo"] = df["stock_id"] + df["type"].map({"twse": ".TW", "tpex": ".TWO"})
    return df.sort_values("stock_id").reset_index(drop=True)


def fetch_one(stock_id: str, yahoo: str, rng: str, session: requests.Session):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo}"
    params = {"range": rng, "interval": "1d", "events": "div|split"}
    for attempt in range(5):
        try:
            r = session.get(url, params=params, headers=UA, timeout=30)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None, "not_found"
            r.raise_for_status()
            res = r.json()["chart"]["result"]
            if not res:
                return None, "empty"
            return res[0], "ok"
        except Exception as e:  # noqa: BLE001
            err = repr(e)
            time.sleep(2 * (attempt + 1))
    return None, f"error: {err[:200]}"


def parse(stock_id: str, res: dict):
    ts = res.get("timestamp") or []
    if not ts:
        return None, [], []
    q = res["indicators"]["quote"][0]
    adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose", [None] * len(ts))
    px = pd.DataFrame({
        "date": [dt.datetime.fromtimestamp(t, TZ).date() for t in ts],
        "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
        "close": q.get("close"), "adjclose": adj, "volume": q.get("volume"),
    }).dropna(subset=["close"])
    px = px.drop_duplicates("date", keep="last")
    ev = res.get("events", {})
    divs = [{"stock_id": stock_id,
             "date": dt.datetime.fromtimestamp(v["date"], TZ).date(),
             "cash_dividend": v["amount"]} for v in ev.get("dividends", {}).values()]
    splits = [{"stock_id": stock_id,
               "date": dt.datetime.fromtimestamp(v["date"], TZ).date(),
               "ratio": v["numerator"] / v["denominator"],
               "split_ratio": v["splitRatio"]} for v in ev.get("splits", {}).values()]
    return px, divs, splits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", default="10y")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 檔（測試用）")
    args = ap.parse_args()

    PRICES.mkdir(parents=True, exist_ok=True)
    stocks = fetch_stock_list()
    stocks.to_csv(DATA / "stock_list.csv", index=False)
    if args.limit:
        stocks = stocks.head(args.limit)
    print(f"共 {len(stocks)} 檔，開始下載 {args.range} 日線…", flush=True)

    all_divs, all_splits, log = [], [], []
    session = requests.Session()

    def job(row):
        res, status = fetch_one(row.stock_id, row.yahoo, args.range, session)
        if status == "not_found":  # 市場別資料可能過時，改試另一個後綴
            alt = row.yahoo.replace(".TWO", ".TW") if row.yahoo.endswith(".TWO") else row.yahoo + "O"
            res, status = fetch_one(row.stock_id, alt, args.range, session)
        if res is None:
            return row.stock_id, status, None, [], []
        px, divs, splits = parse(row.stock_id, res)
        return row.stock_id, status if px is not None else "no_prices", px, divs, splits

    with ThreadPoolExecutor(args.workers) as ex:
        futs = [ex.submit(job, r) for r in stocks.itertuples()]
        for i, f in enumerate(as_completed(futs), 1):
            sid, status, px, divs, splits = f.result()
            n = 0
            if px is not None and len(px):
                px.to_csv(PRICES / f"{sid}.csv.gz", index=False, compression="gzip")
                n = len(px)
            all_divs += divs
            all_splits += splits
            log.append({"stock_id": sid, "status": status, "rows": n,
                        "first_date": px["date"].min() if n else None,
                        "last_date": px["date"].max() if n else None})
            if i % 200 == 0:
                print(f"  {i}/{len(futs)}", flush=True)

    dv = pd.DataFrame(all_divs).sort_values(["stock_id", "date"])
    # Yahoo 偶有同一筆股利重複出現在相鄰日期（如 00858 2024-07-25/26），去重
    dv["date"] = pd.to_datetime(dv["date"])
    prev = dv.groupby("stock_id")[["date", "cash_dividend"]].shift()
    dup = ((dv["date"] - prev["date"]).dt.days.le(7)
           & (dv["cash_dividend"] - prev["cash_dividend"]).abs().lt(1e-6))
    print(f"移除重複股利事件 {int(dup.sum())} 筆")
    dv[~dup].assign(date=lambda x: x["date"].dt.date).to_csv(DATA / "dividends.csv", index=False)
    pd.DataFrame(all_splits, columns=["stock_id", "date", "ratio", "split_ratio"]) \
        .sort_values(["stock_id", "date"]).to_csv(DATA / "splits.csv", index=False)
    lg = pd.DataFrame(log).sort_values("stock_id")
    lg.to_csv(DATA / "fetch_log.csv", index=False)
    print(lg["status"].value_counts().to_string())


if __name__ == "__main__":
    main()
