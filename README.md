# 台股追蹤 (Stock)

## 資料
| 檔案 | 內容 |
|---|---|
| `data/stock_list.csv` | 上市 + 上櫃股票 / ETF 清單（來源 FinMind） |
| `data/prices/<代號>.csv.gz` | 近十年日線：open/high/low/close/adjclose/volume（來源 Yahoo Finance） |
| `data/dividends.csv` | 現金股利事件（除息日、金額；已依配股調整，與 close 同基準） |
| `data/splits.csv` | 股票股利 / 減資 / 分割事件 |
| `data/fetch_log.csv` | 每檔抓取狀態與資料起訖日 |

## 近五年殖利率排行
- `output/top100_dividend_yield.xlsx`：Top100（綜合）、Top100 個股、Top100 ETF、全部合格標的、計算說明
- `output/top100_dividend_yield.csv`、`output/all_eligible_dividend_yield.csv`

**計算方式**：以資料截止日往回切 5 個滾動 12 個月區間，
區間殖利率 = 區間內除息之現金股利合計 ÷ 區間平均收盤價，取 5 區間平均排序。
篩選：上市櫃滿 5 年、仍在交易、5 年中至少 4 年有配息、近一年日均量 ≥ 100 張。

## 更新資料
```bash
pip install -r requirements.txt
python3 src/fetch_data.py          # 約 20 分鐘
python3 src/analyze_dividends.py   # 可加 --min-years 5 --min-lots 500 調整篩選
```

> 資料僅供研究參考，不構成投資建議。
