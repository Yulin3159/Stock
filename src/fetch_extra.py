"""補抓清單以外的個別標的（例如興櫃持股），合併進 data/ 的各檔案。

用法：python3 src/fetch_extra.py 7899 [其他代號...]
會從 FinMind 股票清單找名稱與市場別（含興櫃），再到 Yahoo 抓近十年日線、股利與配股。
"""
import sys

import pandas as pd
import requests

from fetch_data import DATA, PRICES, UA, fetch_one, parse


def main():
    ids = sys.argv[1:]
    info = pd.DataFrame(requests.get("https://api.finmindtrade.com/api/v4/data",
                                     params={"dataset": "TaiwanStockInfo"}, headers=UA, timeout=60).json()["data"])
    stocks = pd.read_csv(DATA / "stock_list.csv", dtype={"stock_id": str})
    divs = pd.read_csv(DATA / "dividends.csv", dtype={"stock_id": str})
    splits = pd.read_csv(DATA / "splits.csv", dtype={"stock_id": str})
    session = requests.Session()
    for sid in ids:
        m = info[info["stock_id"] == sid].sort_values("date")
        if m.empty:
            print(sid, "清單中找不到")
            continue
        row = m.iloc[-1]
        res = None
        for suffix in ([".TW", ".TWO"] if row["type"] == "twse" else [".TWO", ".TW"]):
            res, status = fetch_one(sid, sid + suffix, "10y", session)
            if res is not None:
                break
        if res is None:
            print(sid, "Yahoo 沒有資料")
            continue
        px, dv, sp = parse(sid, res)
        px.to_csv(PRICES / f"{sid}.csv.gz", index=False, compression="gzip")
        divs = pd.concat([divs[divs.stock_id != sid], pd.DataFrame(dv, columns=["stock_id", "date", "cash_dividend"])])
        splits = pd.concat([splits[splits.stock_id != sid], pd.DataFrame(sp, columns=["stock_id", "date", "ratio", "split_ratio"])])
        stocks = pd.concat([stocks[stocks.stock_id != sid], pd.DataFrame([{
            "stock_id": sid, "stock_name": row["stock_name"].rstrip("*"), "type": row["type"],
            "industry": row["industry_category"], "yahoo": sid + suffix}])])
        print(sid, row["stock_name"], row["type"], len(px), "天,", len(dv), "筆股利")
    stocks.sort_values("stock_id").to_csv(DATA / "stock_list.csv", index=False)
    divs.assign(date=pd.to_datetime(divs["date"]).dt.date).sort_values(["stock_id", "date"]).to_csv(DATA / "dividends.csv", index=False)
    splits.assign(date=pd.to_datetime(splits["date"]).dt.date).sort_values(["stock_id", "date"]).to_csv(DATA / "splits.csv", index=False)


if __name__ == "__main__":
    main()
