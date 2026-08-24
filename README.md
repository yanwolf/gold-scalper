# 黃金極短線分析工具（後端骨架）

架構參考自 crypto-screener：Python/FastAPI 後端，之後前端可以照 PWA 模式另外接。
目前這版只做「資料層」：三資料源即時/輪詢報價 + REST/WebSocket 對外介面。
分價量表、纏論中樞/背馳等分析邏輯尚未加入，是下一步的工作。

## 三個資料源的角色分工與目前狀態

- **Binance XAUUSDT 永續合約（目前的主力開發資料源）**：24/7 交易（含週末），tick級即時WebSocket，不需要API Key，公開市場資料。目前分析模組開發階段先以這條線為主。
- **OANDA XAU_USD（暫緩，之後補上）**：申請流程需要桌機才能完成「Manage API Access」設定，手機無法申請，先擱置。未來若要接軌 CFD 執行端，這裡的角色可能會被實際下單的經紀商取代（見下方MT5備忘）。
- **GoldAPI.io（低頻confirmation訊號源）**：REST輪詢（非streaming），Google帳號登入即可申請、純手機可完成。免費方案有請求次數限制，預設輪詢間隔60秒，用來跟Binance的tick資料做交叉比對，不當作極短線主要判斷依據。

## 專案結構

```
gold-scalper/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI 進入點：health / REST / WebSocket
│   ├── oanda_client.py    # 背景執行緒，OANDA streaming（目前暫緩，缺API憑證時會顯示連線失敗，不影響其他資料源）
│   ├── binance_client.py  # 背景執行緒，Binance XAUUSDT WebSocket（目前主力資料源）
│   └── goldapi_client.py  # 背景執行緒，GoldAPI.io REST輪詢（低頻confirmation）
├── Dockerfile
├── requirements.txt
├── .env.example
└── .gitignore
```

## 本機測試

```bash
cp .env.example .env
# 編輯 .env，先只填 GOLDAPI_KEY（OANDA變數留空即可，該資料源會顯示連線失敗但不影響其他部分）

pip install -r requirements.txt
python -m app.main
```

啟動後打開瀏覽器測試：
- http://localhost:8000/health — 確認三個資料源的連線狀態
- http://localhost:8000/price/latest/binance — Binance XAUUSDT 最新一筆報價（目前主力）
- http://localhost:8000/price/latest/goldapi — GoldAPI.io 最新一筆報價（低頻confirmation）
- http://localhost:8000/price/latest — OANDA 最新一筆報價（暫緩中，未填憑證前會回傳訊息）
- ws://localhost:8000/ws/price/binance — Binance WebSocket 即時推播

## 部署到 Zeabur

1. 把這個資料夾推到一個 GitHub repo（記得 `.env` 不會被推上去，這是故意的）
2. Zeabur 建立新服務，選擇這個 repo，Zeabur 會自動偵測到 `Dockerfile` 並用容器方式建置
3. 在 Zeabur 服務的環境變數設定頁面，比照 `.env.example` 填入（OANDA相關可先留空）：
   - `BINANCE_GOLD_SYMBOL`（預設 xauusdt，不需要 API Key，不用改也可以）
   - `GOLDAPI_KEY`（用 Google 帳號登入 goldapi.io 取得）
   - `GOLDAPI_POLL_INTERVAL_SECONDS`（預設60秒）
   - `OANDA_API_TOKEN` / `OANDA_ACCOUNT_ID`（之後有機會用電腦申請完再補）
4. 不用手動設定 `PORT`，Zeabur 會自動注入，程式已經照這個慣例寫（讀取 `PORT` 環境變數）
5. 部署完成後，打服務網址加 `/health`，確認 `binance_stream.connected` 和 `goldapi_stream.connected` 是 `true`（`oanda_stream.connected` 目前預期是 `false`，屬正常狀態）

## 未來接 MT5 自動交易的架構備忘

MT5 官方 Python 套件只能在 Windows、且跟正在執行的 MT5 終端機同一台機器上運作，Zeabur（雲端 Linux 容器）沒辦法直接跑 MT5。
之後的分工建議：
1. 這個 Zeabur 服務持續負責訊號生成，新增一個 `/signal/latest` endpoint
2. 另外一台 Windows VPS 跑 MT5 終端機 + 一個 MQL5 寫的 EA，EA 用 `WebRequest()` 定期輪詢 `/signal/latest`，收到訊號後用 MT5 原生下單函式執行
3. 兩邊分離，訊號邏輯迭代不用碰 Windows/MT5 那一側
4. 執行端經紀商（例如 Exness、IC Markets 等提供 MT5 的CFD商）確定後，該經紀商的報價會自然成為「主要訊號源」，取代目前暫緩的OANDA角色，確保訊號跟執行價格基準一致

## 已知限制 / 下一步

- 目前用單一 in-process thread 做 streaming/輪詢，重開機或服務重啟時 tick 歷史會清空（`MAX_TICK_HISTORY` 只存在記憶體）。之後若要長期回測，建議加一個資料庫寫入層（可以參考 crypto-screener 的作法）。
- 沒有多帳號/多服務實例的資料同步機制。若 Zeabur 上開多個 instance，每個 instance 會各自連一條 streaming，這點在垂直擴展前要注意。
- 分價量表、纏論中樞/背馳邏輯尚未實作，建議獨立成 `app/analysis.py`，優先以 Binance 資料開發，GoldAPI做交叉驗證。
- Binance XAUUSDT 是衍生品合成價格，不是實體黃金或COMEX期貨本身，只作為目前的開發用資料源，不應該當作最終下單依據。
- OANDA API憑證申請卡在需要桌機完成的設定頁面，之後有機會用電腦時再回頭補上。

## 分析模組（分價量表 / 纏論）

新增 `app/analysis.py`，資料源固定用 Binance 的逐筆成交(aggTrade)，因為只有這條線有真實成交量。
`binance_client.py` 因此也從單純訂閱 bookTicker 改成 combined stream，同時訂閱 bookTicker(報價) + aggTrade(逐筆成交)。

三個新 endpoint：

- `GET /analysis/candles?interval_seconds=300` — K線聚合，預設5分鐘，改參數就能切其他週期(例如60=1分鐘)
- `GET /analysis/volume-profile?bucket_size=1.0` — 分價量表，含POC(成交量最大價位)和Value Area(70%成交量區間)
- `GET /analysis/chan?interval_seconds=300` — 纏論分析：分型 -> 筆 -> 中樞 -> 背馳判斷，預設用5分鐘K線

GoldAPI目前還沒接進分析模組做「對齊校正」，那部分邏輯尚未實作，先留著只用Binance資料驗證整套分析流程能不能動起來。

### 纏論邏輯的實作範圍(第一版)

- 分型/筆/中樞用標準教學版簡化規則實作，已用合成資料測試過能正常產出結果
- 背馳判斷用MACD柱狀圖面積比較「同方向筆」的動能是否縮小，屬於簡化版，還沒處理盤整背馳、趨勢背馳的細分類
- 目前只抓「三筆重疊」的基本中樞，還沒做中樞延伸/擴張的判斷邏輯

## 修正記錄：Binance WebSocket 路由分流 (2026)

Binance 近期把 WebSocket 資料流拆成 `/public`、`/market`、`/private` 三個路由：
- `bookTicker`（報價）屬於 `/public`
- `aggTrade`（逐筆成交）屬於 `/market`

沒有指定路由的舊式連線只會收到 `/public` 的資料，`/market` 底下的頻道會被**靜默丟棄**（不報錯，就是收不到）。
`binance_client.py` 已經修正為兩條獨立連線分別接 `/public` 和 `/market`。

`/health` 的 `binance_stream` 現在多了 `public_connected` 和 `market_connected` 兩個欄位，方便未來若又有類似問題時快速定位是哪條路由出狀況。`trade_count` 應該會隨時間持續增加，如果部署後過一段時間還是0，先檢查這兩個欄位是否都是 `true`。
