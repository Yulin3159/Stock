"""從 FinMind 抓個股配息公告（含現金股利發放日、股票股利），存成 data/finmind_dividends/<id>.json。

免費、不登入的額度約每小時 300 次，所以只抓指定清單（持股 + 排行前段），其餘標的的發放日用推估。
已抓過的會略過；遇到額度上限會停下，下次再跑會從沒抓到的繼續。

用法：python3 src/fetch_finmind_dividends.py 0050 0056 ...   # 指定代號
      python3 src/fetch_finmind_dividends.py --top               # output/dashboard/ranking.csv 各類前 100 名
"""
import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "finmind_dividends"
START = "2024-10-01"


def top_ids():
    r = pd.read_csv(ROOT / "output" / "dashboard" / "ranking.csv", dtype={"id": str})
    ids = list(r.head(100)["id"]) + list(r[r.kind == "個股"].head(100)["id"]) + list(r[r.kind == "ETF"].head(100)["id"])
    return list(dict.fromkeys(ids))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]
    ids = []
    for a in args:
        ids += top_ids() if a == "--top" else [a]
    ids = list(dict.fromkeys(ids))
    todo = [i for i in ids if not (OUT / f"{i}.json").exists()]
    print(f"{len(ids)} 檔，需抓 {len(todo)} 檔", flush=True)
    for n, sid in enumerate(todo, 1):
        for attempt in range(3):
            try:
                r = requests.get("https://api.finmindtrade.com/api/v4/data", timeout=30, params={
                    "dataset": "TaiwanStockDividend", "data_id": sid, "start_date": START})
                j = r.json()
                break
            except Exception as e:  # noqa: BLE001
                j = {"status": -1, "msg": repr(e)}
                time.sleep(5)
        if j.get("status") != 200:
            print(f"停止於 {sid}：{j.get('msg')}", flush=True)
            return
        (OUT / f"{sid}.json").write_text(json.dumps(j["data"], ensure_ascii=False))
        if n % 25 == 0:
            print(f"  {n}/{len(todo)}", flush=True)
        time.sleep(1.2)
    print("完成", flush=True)


if __name__ == "__main__":
    main()
