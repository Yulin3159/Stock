"""每日更新：最新收盤價、持股的配息公告與新聞，然後重算排行與儀表板資料。

一天跑兩次（早上 8 點、下午 2 點）時用：
    python3 src/daily_update.py --ids 0050,2330,... --news-out <路徑>/news.json

--ids：持股與定期定額的代號（存在儀表板的私人資料裡，不放進 repo）。

步驟：
1. TWSE / TPEx openapi 的當日全市場收盤價，寫進 data/prices/<id>.csv.gz（一次兩個請求就涵蓋上市櫃全部）。
2. 持股 + 各類排行前 100 名用 Yahoo Finance 近 3 個月日線覆蓋（含興櫃、盤後最新價），並補上新的除權息事件。
3. 持股近 3 天新聞（Google News RSS，抓不到時用 FinMind TaiwanStockNews）、上市櫃重大訊息、加權指數，寫進 --news-out。
4. （--dividends）持股重新抓 FinMind 配息公告（含新公告的發放日）。
5. 重跑 analyze_dividends.py 與 export_dashboard.py。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_data import UA, fetch_one, parse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PRICES = DATA / "prices"
FINMIND = "https://api.finmindtrade.com/api/v4/data"
TZ = dt.timezone(dt.timedelta(hours=8))
COLS = ["date", "open", "high", "low", "close", "adjclose", "volume"]


def roc(s: str) -> str:
    s = str(s).strip()
    return f"{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}"


def num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return None


def market_quotes() -> dict[str, dict]:
    """上市 + 上櫃當日收盤（openapi）。回傳 {代號: 一列日線}。"""
    out = {}
    try:
        for r in requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=60).json():
            c = num(r.get("ClosingPrice"))
            if c:
                out[r["Code"]] = {"date": roc(r["Date"]), "open": num(r["OpeningPrice"]), "high": num(r["HighestPrice"]),
                                  "low": num(r["LowestPrice"]), "close": c, "adjclose": c, "volume": num(r["TradeVolume"])}
    except Exception as e:  # noqa: BLE001
        print("TWSE 收盤價失敗：", repr(e)[:200])
    try:
        for r in requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=60).json():
            c = num(r.get("Close"))
            if c:
                out[r["SecuritiesCompanyCode"]] = {"date": roc(r["Date"]), "open": num(r["Open"]), "high": num(r["High"]),
                                                   "low": num(r["Low"]), "close": c, "adjclose": c, "volume": num(r["TradingShares"])}
    except Exception as e:  # noqa: BLE001
        print("TPEx 收盤價失敗：", repr(e)[:200])
    return out


def merge_prices(sid: str, new: pd.DataFrame) -> str | None:
    p = PRICES / f"{sid}.csv.gz"
    if not p.exists() or new.empty:
        return None
    old = pd.read_csv(p)
    new = new.assign(date=new["date"].astype(str))[COLS]
    df = pd.concat([old[~old["date"].isin(new["date"])], new]).sort_values("date")
    df.to_csv(p, index=False, compression="gzip")
    return df["date"].iloc[-1]


def focus_ids(extra: list[str]) -> list[str]:
    r = pd.read_csv(ROOT / "output" / "dashboard" / "ranking.csv", dtype={"id": str})
    ids = extra + list(r.head(100)["id"]) + list(r[r.kind == "個股"].head(100)["id"]) + list(r[r.kind == "ETF"].head(100)["id"])
    return list(dict.fromkeys(ids))


def merge_events(path: Path, new: list[dict], key: str):
    if not new:
        return 0
    old = pd.read_csv(path, dtype={"stock_id": str})
    old["date"] = pd.to_datetime(old["date"])
    add = []
    for e in new:
        t = pd.Timestamp(e["date"])
        same = old[(old.stock_id == e["stock_id"]) & ((old.date - t).abs() <= pd.Timedelta(days=7))]
        if same.empty or (same[key] - e[key]).abs().min() > 1e-6:
            add.append({**e, "date": t})
    if add:
        df = pd.concat([old, pd.DataFrame(add)]).sort_values(["stock_id", "date"])
        df.assign(date=df["date"].dt.date).to_csv(path, index=False)
    return len(add)


def finmind(dataset: str, sid: str, start: str):
    for _ in range(3):
        try:
            j = requests.get(FINMIND, timeout=30, params={"dataset": dataset, "data_id": sid, "start_date": start}).json()
            return j
        except Exception as e:  # noqa: BLE001
            j = {"status": -1, "msg": repr(e)}
            time.sleep(3)
    return j


def google_news(query: str):
    """Google News RSS（近 3 天）。偶爾回 503，退避重試；失敗回 None。"""
    import email.utils
    import urllib.parse
    import xml.etree.ElementTree as ET
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{query} when:3d")
           + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    for wait in (4, 10, 20, 40):
        time.sleep(wait)
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                                         "(KHTML, like Gecko) Chrome/120 Safari/537.36"}, timeout=30)
            if r.status_code != 200:
                continue
            out = []
            for it in ET.fromstring(r.content).iter("item"):
                try:
                    d = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:  # noqa: BLE001
                    d = ""
                out.append({"date": d, "title": it.findtext("title"), "source": it.findtext("source"), "link": it.findtext("link")})
            return out[:30]
        except Exception:  # noqa: BLE001
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="", help="持股與定期定額代號，逗號分隔")
    ap.add_argument("--news-out", default="")
    ap.add_argument("--skip-analysis", action="store_true")
    ap.add_argument("--dividends", action="store_true", help="也重抓持股的 FinMind 配息公告")
    a = ap.parse_args()
    mine = [s.strip() for s in a.ids.split(",") if s.strip()]
    now = dt.datetime.now(TZ)
    stocks = pd.read_csv(DATA / "stock_list.csv", dtype={"stock_id": str})
    log = pd.read_csv(DATA / "fetch_log.csv", dtype={"stock_id": str})
    last = dict(zip(log.stock_id, log.last_date))

    # 1) 全市場收盤
    q = market_quotes()
    n1 = 0
    for sid, row in q.items():
        d = merge_prices(sid, pd.DataFrame([row]))
        if d:
            last[sid] = d
            n1 += 1
    print(f"openapi 收盤價：{len(q)} 檔，更新 {n1} 檔（{next(iter(q.values()))['date'] if q else '-'}）", flush=True)

    # 2) Yahoo：持股 + 排行前段
    ymap = dict(zip(stocks.stock_id, stocks.yahoo))
    ids = [i for i in focus_ids(mine) if i in ymap]
    session = requests.Session()

    def job(sid):
        res, st = fetch_one(sid, ymap[sid], "3mo", session)
        if st == "not_found":
            y = ymap[sid]
            res, st = fetch_one(sid, y.replace(".TWO", ".TW") if y.endswith(".TWO") else y + "O", "3mo", session)
        return sid, (parse(sid, res) if res else (None, [], []))

    divs, splits, n2 = [], [], 0
    with ThreadPoolExecutor(6) as ex:
        for sid, (px, dv, sp) in ex.map(job, ids):
            if px is not None and len(px):
                d = merge_prices(sid, px)
                if d:
                    last[sid] = d
                    n2 += 1
            divs += dv
            splits += sp
    nd = merge_events(DATA / "dividends.csv", divs, "cash_dividend")
    ns = merge_events(DATA / "splits.csv", splits, "ratio")
    print(f"Yahoo：{n2}/{len(ids)} 檔，新增股利 {nd} 筆、配股 / 分割 {ns} 筆", flush=True)
    log["last_date"] = log.stock_id.map(last).fillna(log["last_date"])
    log.to_csv(DATA / "fetch_log.csv", index=False)

    # 3) 新聞、重大訊息、加權指數
    if a.news_out:
        names = dict(zip(stocks.stock_id, stocks.stock_name))
        start = (now - dt.timedelta(days=3)).strftime("%Y-%m-%d")
        news, seen = [], set()
        fm_ok, src_count = True, {"Google News": 0, "FinMind": 0, "無": 0}
        for sid in mine:
            # Google News 較即時（FinMind 新聞約晚 2～3 天），抓不到再用 FinMind
            items = google_news(f"{names.get(sid, sid).rstrip('*')} {sid}")
            if items is not None:
                src_count["Google News"] += 1
            elif fm_ok:
                j = finmind("TaiwanStockNews", sid, start)
                if j.get("status") == 200:
                    items = [{"date": r.get("date"), "title": r.get("title"), "source": r.get("source"), "link": r.get("link")}
                             for r in j["data"]]
                    src_count["FinMind"] += 1
                    time.sleep(1.2)
                else:
                    fm_ok = False
            if items is None:
                src_count["無"] += 1
            for r in items or []:
                k = (sid, r.get("title"))
                if k in seen:
                    continue
                seen.add(k)
                news.append({"stock": sid, "name": names.get(sid, ""), **r})
        print("新聞來源：", src_count)
        material = []
        for url, code_k, name_k, subj_k in [
            ("https://openapi.twse.com.tw/v1/opendata/t187ap04_L", "公司代號", "公司名稱", "主旨 "),
            ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O", "SecuritiesCompanyCode", "CompanyName", "主旨"),
        ]:
            try:
                for r in requests.get(url, timeout=60).json():
                    if r.get(code_k) in mine:
                        material.append({"stock": r[code_k], "name": r.get(name_k), "date": roc(r["發言日期"]),
                                         "title": (r.get(subj_k) or r.get("主旨") or "").replace("\r\n", ""),
                                         "detail": (r.get("說明") or "")[:600]})
            except Exception as e:  # noqa: BLE001
                print("重大訊息失敗：", repr(e)[:200])
        index = None
        res, st = fetch_one("TAIEX", "^TWII", "1mo", session)
        if res:
            px, _, _ = parse("TAIEX", res)
            if px is not None and len(px) >= 2:
                c, p0 = px["close"].iloc[-1], px["close"].iloc[-2]
                index = {"date": str(px["date"].iloc[-1]), "close": round(c, 2), "change": round(c - p0, 2),
                         "change_pct": round((c / p0 - 1) * 100, 2), "month_pct": round((c / px["close"].iloc[0] - 1) * 100, 2)}
        Path(a.news_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.news_out).write_text(json.dumps({"generated_at": now.isoformat(timespec="minutes"), "index": index,
                                                "news": sorted(news, key=lambda x: x["date"] or "", reverse=True),
                                                "material": material}, ensure_ascii=False, indent=1))
        print(f"新聞 {len(news)} 則、重大訊息 {len(material)} 則 → {a.news_out}", flush=True)

    # 4) 持股的 FinMind 配息公告（免費額度很緊，排在新聞之後；只在 --dividends 時抓）
    if a.dividends:
        out = DATA / "finmind_dividends"
        ok = 0
        for sid in mine:
            j = finmind("TaiwanStockDividend", sid, "2024-10-01")
            if j.get("status") != 200:
                print(f"FinMind 配息停止於 {sid}：{j.get('msg')}")
                break
            (out / f"{sid}.json").write_text(json.dumps(j["data"], ensure_ascii=False))
            ok += 1
            time.sleep(1.2)
        print(f"FinMind 配息公告：{ok}/{len(mine)} 檔", flush=True)

    # 5) 重算
    src = Path(__file__).resolve().parent
    if not a.skip_analysis:
        subprocess.run([sys.executable, "analyze_dividends.py"], cwd=src, check=True, stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable, "export_dashboard.py"], cwd=src, check=True)


if __name__ == "__main__":
    main()
