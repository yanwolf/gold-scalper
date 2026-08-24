# 黃金極短線分析工具

架構參考自 crypto-screener：Python/FastAPI 後端 + 前端頁面同源提供。
資料層 + 分析層(分價量表/纏論) + 簡易視覺化 dashboard 都已經可以動，
下一步是訊號邏輯設計和接軌 MT5 自動下單。

## 資料源

- **Binance XAUUSDT 永續合約（主力資料源）**：24/7 交易（含週末），tick級即時WebSocket，
  不需要API Key。分析模組(K線、分價量表、纏論)固定用這條線的逐筆成交(aggTrade)真實成交量。
  是衍生品合成價格，不是實體黃金或COMEX期貨本身，只作為目前的開發用資料源，不應該當作最終下單依據。
- **OANDA XAU_USD（暫緩）**：申請流程需要桌機才能完成「Manage API Access」設定，手機無法申請，先擱置。
  未來接軌CFD執行端時，這裡的角色可能會被實際下單的經紀商取代（見下方MT5備忘）。
- ~~GoldAPI.io~~：已移除。免費方案只有每月100次請求額度，撐不住持續輪詢，架構因此簡化為Binance單一主力資料源。

## 專案結構

```
gold-scalper/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI 進入點：health / REST / WebSocket / dashboard
│   ├── oanda_client.py    # 背景執行緒，OANDA streaming（暫緩，缺憑證時顯示連線失敗，不影響其他資料源）
│   ├── binance_client.py  # 背景執行緒，Binance XAUUSDT WebSocket（主力資料源）+ 定期寫入資料庫
│   ├── db.py               # PostgreSQL 持久化：schema、批次寫入、歷史回填
│   ├── analysis.py         # K線聚合 / 分價量表 / 纏論分型-筆-中樞-背馳
│   └── static/
│       └── dashboard.html  # 視覺化頁面，由後端同源提供
├── Dockerfile
├── requirements.txt
├── .env.example
└── .gitignore
```

## 本機測試

```bash
cp .env.example .env
# 本機測試可以先什麼都不填，會自動退回純記憶體模式運作

pip install -r requirements.txt
python -m app.main
```

啟動後打開瀏覽器測試：
- http://localhost:8000/dashboard — 視覺化頁面（分價量表 + 纏論分析）
- http://localhost:8000/health — 資料源與資料庫連線狀態
- http://localhost:8000/analysis/volume-profile — 分價量表原始JSON
- http://localhost:8000/analysis/chan — 纏論分析原始JSON

## 部署到 Zeabur

1. 把這個資料夾推到 GitHub repo（`.env` 不會被推上去，這是故意的）
2. Zeabur 建立新服務，選這個 repo，會自動偵測 `Dockerfile` 並用容器方式建置
3. 環境變數（可先什麼都不填，服務一樣能正常啟動）：
   - `BINANCE_GOLD_SYMBOL`（預設 xauusdt，不用改也可以）
   - `DATABASE_URL`（見下方「資料持久化」章節）
   - `OANDA_API_TOKEN` / `OANDA_ACCOUNT_ID`（之後有機會用電腦申請完再補）
4. 不用手動設定 `PORT`，Zeabur 會自動注入
5. 部署完打 `/health`，確認 `binance_stream.connected` 是 `true`
   （`oanda_stream.connected` 目前預期是 `false`，屬正常狀態）

## 資料持久化：PostgreSQL

Zeabur重新部署或容器重啟，記憶體裡的tick/trade歷史本來會被清空，分析模組(尤其纏論的
5分鐘K線)每次都要重新累積資料才能用。`app/db.py` 解決了這個問題：把Binance逐筆成交批次
寫進PostgreSQL，服務啟動時自動從資料庫回填歷史資料到記憶體。

**設定步驟：**
1. Zeabur專案裡新增一個服務：Marketplace搜尋 **PostgreSQL**，一鍵部署
2. 回到 gold-scalper 服務的環境變數頁，新增：
   ```
   DATABASE_URL=${POSTGRES_CONNECTION_STRING}
   ```
   （Zeabur的變數引用語法，會自動抓PostgreSQL服務的連線字串，不用手動複製帳密）
3. 重新部署後打 `/health`，確認 `database_persistence_enabled` 是 `true`

**運作方式：**
- 每20秒把新累積的逐筆成交批次寫進資料庫一次
- 服務啟動時自動從資料庫回填最近的歷史成交進記憶體(上限 `MAX_TRADE_HISTORY`，目前20000筆)
- **沒設定 `DATABASE_URL` 也完全沒問題**：自動退回純記憶體模式，不會讓服務起不來
- 寫入/讀取失敗只記錄log，不會讓即時資料流中斷

## 分析模組

`app/analysis.py`，資料源固定用 Binance 的逐筆成交(aggTrade)，因為只有這條線有真實成交量。

三個 endpoint：
- `GET /analysis/candles?interval_seconds=300` — K線聚合，預設5分鐘，改參數可切其他週期(如60=1分鐘)
- `GET /analysis/volume-profile?bucket_size=1.0` — 分價量表，含POC(成交量最大價位)和Value Area(70%成交量區間)
- `GET /analysis/chan?interval_seconds=300` — 纏論分析：分型 -> 筆 -> 中樞 -> 背馳判斷

**纏論邏輯的實作範圍(第一版)：**
- 分型/筆/中樞用標準教學版簡化規則實作，已用合成資料測試過能正常產出結果
- 背馳判斷用MACD柱狀圖面積比較「同方向筆」的動能是否縮小，屬於簡化版，還沒處理盤整背馳、趨勢背馳的細分類
- 目前只抓「三筆重疊」的基本中樞，還沒做中樞延伸/擴張的判斷邏輯

## 視覺化 Dashboard

`GET /dashboard`（打開服務網址根目錄 `/` 也會自動跳轉過去），由後端同源提供，不用貼API網址，
也不會遇到瀏覽器/App沙盒環境擋跨網域fetch的問題。

內容：
- 分價量表：紅色=現價之上的阻力區、綠色=現價之下的支撐區，金色外框那列是POC，每10秒自動刷新
- 纏論分析面板：分型數/筆數/合併K棒數、最新中樞上下界、背馳訊號橫幅(紅色高亮=有背馳)、最近的筆列表
- 可切換1分鐘/5分鐘/15分鐘K線週期

## 未來接 MT5 自動交易的架構備忘

MT5 官方 Python 套件只能在 Windows、且要跟正在執行的 MT5 終端機同一台機器運作，Zeabur(雲端Linux容器)
沒辦法直接跑MT5。之後的分工建議：
1. 這個 Zeabur 服務持續負責訊號生成，新增一個 `/signal/latest` endpoint
2. 另外一台 Windows VPS 跑 MT5 終端機 + 一個 MQL5 寫的 EA，EA 用 `WebRequest()` 定期輪詢
   `/signal/latest`，收到訊號後用 MT5 原生下單函式執行
3. 兩邊分離，訊號邏輯迭代不用碰 Windows/MT5 那一側
4. 執行端經紀商(例如 Exness、IC Markets 等提供 MT5 的CFD商)確定後，該經紀商的報價會自然成為
   「主要訊號源」，取代目前暫緩的OANDA角色，確保訊號跟執行價格基準一致

## 已知限制 / 下一步

- 沒有多服務實例的資料同步機制。若 Zeabur 上開多個 instance，每個 instance 會各自連一條 streaming，
  這點在垂直擴展前要注意。
- 訊號邏輯(什麼時候算看多/看空)還沒設計，是下一步的主要工作。
- OANDA API憑證申請卡在需要桌機完成的設定頁面，之後有機會用電腦時再回頭補上。

## 修正記錄

**Binance WebSocket 路由分流 (2026)**：Binance 把 WebSocket 資料流拆成 `/public`、`/market`、
`/private` 三個路由，`bookTicker`(報價)屬於`/public`，`aggTrade`(逐筆成交)屬於`/market`。
沒有指定路由的舊式連線只會收到`/public`的資料，`/market`底下的頻道會被靜默丟棄(不報錯，就是
收不到)。`binance_client.py` 已修正為兩條獨立連線分別接兩個路由，`/health` 的 `binance_stream`
有 `public_connected` / `market_connected` 兩個欄位可以分別檢查。

## 訊號引擎：綜合纏論 + 分價量表

新增 `app/signal.py`，把纏論和分價量表的判斷綜合成一個分階段的多空訊號，
不是機器學習模型，是規則式(rule-based)判斷，每條規則對應標準的纏論/Volume Profile概念，
方便之後逐條檢視、調整權重。

**判斷規則：**
- 纏論那一側：背馳反轉(優先權較高，因為是提早示警) > 中樞突破(站上ZG看多/跌破ZD看空) > 中樞內整理(中性)
- 分價量表那一側：站上/跌破Value Area(強訊號) > 相對POC偏移(弱訊號)
- 綜合：兩邊方向一致+至少一邊強 -> **訊號**；兩邊方向一致但都弱，或只有單邊有方向 -> **關注**；方向衝突 -> **中性**

**Endpoint：** `GET /signal/latest?interval_seconds=300&bucket_size=1.0`
回傳 `stage`(訊號/關注/中性)、`direction`(bullish/bearish/null)，以及纏論、分價量表兩側各自的判斷理由。
這是未來要給MT5 EA輪詢的endpoint，格式先在這裡驗證穩定，之後EA可以直接用`WebRequest()`定期打這支API。

Dashboard最上方新增了訊號面板：不同階段/方向會有不同配色(訊號=強烈發光框、關注=金色、中性=素色)，
下方列出纏論和分價量表各自的判斷理由，方便直接看懂「為什麼」給這個訊號，不是黑盒子。

## K線圖（Dashboard）

Dashboard 訊號面板下方新增了即時K線圖，用 TradingView 開源的 `lightweight-charts`
(透過CDN載入，不用額外安裝套件)。內容：
- 蠟燭圖(OHLC)，資料源是 `/analysis/candles`，週期跟纏論分析面板共用同一個選擇器(1/5/15分鐘)
- 下方疊加成交量長條圖
- 圖上用虛線標出 POC、Value Area高/低、目前市價，跟分價量表面板互相對照
- 每10秒跟其他資料一起自動刷新，切換K線週期時圖表也會同步更新

這個圖表庫是純前端的CDN引用，不影響後端架構，如果之後CDN連線有問題(較少見)，
可以改成把lightweight-charts的檔案下載下來放進`app/static/`一起提供。

## 修正記錄：訊號面板與分價量表POC數字不同步

`fetchSignal()` 原本把 `/signal/latest` 的 `trade_limit` 參數寫死成20000，
但下方分價量表面板用的是使用者自己選的「近N筆」下拉選單(1000/3000/6000)，
兩邊取樣範圍不同，算出來的POC/VAH/VAL自然對不上。已修正成兩處共用同一個
`els.tradeLimit` 的值，確保訊號框跟分價量表、K線圖上疊加的水平線永遠是同一組數字。

## 修正記錄：架構性修正 — 統一成單一資料快照

前一版的修正(讓trade_limit參數對齊)只解決了表面症狀，實際上還會不同步：
因為訊號框跟分價量表面板是**分開打兩支API**，Binance報價持續在跳動，
兩次獨立呼叫`get_recent_trades()`的時間點不同，「最近N筆成交」這個窗口
會跟著往前推移，算出來的POC自然對不上——尤其在價格快速變動時特別明顯。

**根本修正**：`/signal/latest` 現在在後端只呼叫一次`get_recent_trades()`，
`chan_detail`(完整纏論分析)和`profile_detail`(完整分價量表)都是從同一份
trades快照算出來的，一併包在回應裡回傳。前端 `fetchAndRender()` 也跟著
簡化成只打這一支API，分價量表、纏論分析、訊號框、K線圖上的水平線全部
從同一份回應取資料渲染，不再各自獨立打API，架構上保證永遠同步。

`/analysis/chan`、`/analysis/volume-profile` 這兩支獨立endpoint還留著
(給之後debug或其他用途單獨查看用)，但dashboard本身已經不再使用它們。

## 修正記錄：纏論資料不足問題

上一版把 chan_data 和 profile_data 綁在同一個 `trade_limit` 參數上(解決同步問題)，
卻意外造成新問題：分價量表刻意設計成只看「近3000筆」這種小範圍，但纏論需要更長的
歷史資料才能拼出足夠K棒(尤其1分鐘K線)，導致纏論常常「尚未形成中樞」、「筆數0」。

**修正**：纏論和分價量表仍然共用同一次 `get_recent_trades()` 呼叫(保持同步)，
但纏論固定用較大的回看範圍 `CHAN_LOOKBACK_TRADES`(20000筆)，分價量表則從這份
資料裡再切出使用者指定的 `trade_limit`(較小範圍，符合「看近期熱區」的設計本意)。
兩者是同一份快照的不同切片，既同步、又各自有適合的資料量。
