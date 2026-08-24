# 黃金極短線分析工具（後端骨架）

架構參考自 crypto-screener：Python/FastAPI 後端，之後前端可以照 PWA 模式另外接。
目前這版只做「資料層」：雙資料源即時報價 + REST/WebSocket 對外介面。
分價量表、纏論中樞/背馳等分析邏輯尚未加入，是下一步的工作。

## 兩個資料源的角色分工

- **OANDA XAU_USD**（主要訊號源）：未來實際下單走 CFD 經紀商，訊號跟執行要用同一個價格基準，避免落差
- **Binance XAUUSDT 永續合約**（輔助confirmation訊號）：24/7 交易（含週末），可用來觀察 CFD 黃金收盤期間的價格動向、或跟主訊號源做交叉驗證。不需要 API Key，公開市場資料

## 專案結構

```
gold-scalper/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI 進入點：health / REST / WebSocket
│   ├── oanda_client.py    # 背景執行緒，持續連線 OANDA streaming（主要訊號源）
│   └── binance_client.py  # 背景執行緒，持續連線 Binance XAUUSDT WebSocket（輔助confirmation訊號）
├── Dockerfile
├── requirements.txt
├── .env.example
└── .gitignore
```

## 本機測試

```bash
cp .env.example .env
# 編輯 .env，填入 OANDA_API_TOKEN 和 OANDA_ACCOUNT_ID

pip install -r requirements.txt
python -m app.main
```

啟動後打開瀏覽器測試：
- http://localhost:8000/health — 確認服務與兩個資料源的連線狀態
- http://localhost:8000/price/latest — OANDA 最新一筆報價（主要訊號源）
- http://localhost:8000/price/latest/binance — Binance XAUUSDT 最新一筆報價（輔助confirmation）
- ws://localhost:8000/ws/price — OANDA WebSocket 即時推播
- ws://localhost:8000/ws/price/binance — Binance WebSocket 即時推播

## 部署到 Zeabur

1. 把這個資料夾推到一個 GitHub repo（記得 `.env` 不會被推上去，這是故意的）
2. Zeabur 建立新服務，選擇這個 repo，Zeabur 會自動偵測到 `Dockerfile` 並用容器方式建置
3. 在 Zeabur 服務的環境變數設定頁面，比照 `.env.example` 填入：
   - `OANDA_API_TOKEN`
   - `OANDA_ACCOUNT_ID`
   - `OANDA_ENVIRONMENT`（practice 或 live）
   - `OANDA_INSTRUMENT`（預設 XAU_USD，不用改也可以）
   - `BINANCE_GOLD_SYMBOL`（預設 xauusdt，不需要 API Key，不用改也可以）
4. 不用手動設定 `PORT`，Zeabur 會自動注入，程式已經照這個慣例寫（讀取 `PORT` 環境變數）
5. 部署完成後，打服務網址加 `/health`，確認 `oanda_stream.connected` 和 `binance_stream.connected` 都是 `true`

## 未來接 MT5 自動交易的架構備忘

MT5 官方 Python 套件只能在 Windows、且跟正在執行的 MT5 終端機同一台機器上運作，Zeabur（雲端 Linux 容器）沒辦法直接跑 MT5。
之後的分工建議：
1. 這個 Zeabur 服務持續負責訊號生成，新增一個 `/signal/latest` endpoint
2. 另外一台 Windows VPS 跑 MT5 終端機 + 一個 MQL5 寫的 EA，EA 用 `WebRequest()` 定期輪詢 `/signal/latest`，收到訊號後用 MT5 原生下單函式執行
3. 兩邊分離，訊號邏輯迭代不用碰 Windows/MT5 那一側

## 已知限制 / 下一步

- 目前用單一 in-process thread 做 streaming，重開機或服務重啟時 tick 歷史會清空（`MAX_TICK_HISTORY` 只存在記憶體）。之後若要長期回測，建議加一個資料庫寫入層（可以參考 crypto-screener 的作法）。
- 沒有多帳號/多服務實例的資料同步機制。若 Zeabur 上開多個 instance，每個 instance 會各自連一條 streaming，這點在垂直擴展前要注意。
- 分價量表、纏論中樞/背馳邏輯尚未實作，建議獨立成 `app/analysis.py`，同時讀取 OANDA 和 Binance 兩個資料源做交叉驗證。
- Binance XAUUSDT 是衍生品合成價格，不是實體黃金或COMEX期貨本身，只作為輔助訊號，不應該當作主要下單依據。
