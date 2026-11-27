//+------------------------------------------------------------------+
//| TzujenRollSync.mq5                                                |
//| 唯讀：每幾秒把帳戶（結餘、淨值）與持倉（手數、進場價、停損）回報給    |
//| gold-scalper 的滾倉計算器（/tools/roll-sync）。                      |
//| 這支 EA 沒有任何下單、改單、平倉的程式碼——刻意不 include Trade.mqh。 |
//+------------------------------------------------------------------+
#property copyright "tzujenlab"
#property version   "1.00"

input string ServerURL = "https://gold-live.tzujenlab.com";   // 設了 ROLL_SYNC_TOKEN 的 gold-scalper 服務（要加到 允許 WebRequest）
input string Token     = "";                             // 與服務的 ROLL_SYNC_TOKEN 相同
input int    SyncMs    = 3000;                           // 回報間隔（毫秒）

#define EA_VERSION "1.00"

int      g_fail = 0;
datetime g_last_ok = 0;

int OnInit()
  {
   if(StringLen(Token) < 8)
     {
      Alert("TzujenRollSync：請在參數填入 Token（與服務的 ROLL_SYNC_TOKEN 相同）");
      return(INIT_PARAMETERS_INCORRECT);
     }
   EventSetMillisecondTimer(MathMax(SyncMs, 1000));
   Print("TzujenRollSync ", EA_VERSION, " 啟動（唯讀，不下單），伺服器 ", ServerURL);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason) { EventKillTimer(); Comment(""); }

string Esc(string s)
  {
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   StringReplace(s, "\n", " ");
   StringReplace(s, "\r", " ");
   return s;
  }

string D(double v, int digits = 5) { return DoubleToString(v, digits); }

bool InList(string &arr[], string s)
  {
   for(int i = 0; i < ArraySize(arr); i++) if(arr[i] == s) return true;
   return false;
  }

void OnTimer()
  {
   string syms[];
   string pos = "";
   for(int i = 0; i < PositionsTotal(); i++)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0 || !PositionSelectByTicket(t)) continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      if(!InList(syms, sym)) { int n = ArraySize(syms); ArrayResize(syms, n + 1); syms[n] = sym; }
      string one = "{\"ticket\":" + IntegerToString((long)t) +
                   ",\"symbol\":\"" + Esc(sym) + "\"" +
                   ",\"type\":" + IntegerToString(PositionGetInteger(POSITION_TYPE)) +
                   ",\"volume\":" + D(PositionGetDouble(POSITION_VOLUME), 2) +
                   ",\"price\":" + D(PositionGetDouble(POSITION_PRICE_OPEN)) +
                   ",\"sl\":" + D(PositionGetDouble(POSITION_SL)) +
                   ",\"tp\":" + D(PositionGetDouble(POSITION_TP)) +
                   ",\"profit\":" + D(PositionGetDouble(POSITION_PROFIT), 2) +
                   ",\"swap\":" + D(PositionGetDouble(POSITION_SWAP), 2) +
                   ",\"magic\":" + IntegerToString(PositionGetInteger(POSITION_MAGIC)) +
                   ",\"time\":" + IntegerToString(PositionGetInteger(POSITION_TIME)) + "}";
      pos += (StringLen(pos) > 0 ? "," : "") + one;
     }
   if(!InList(syms, _Symbol)) { int n = ArraySize(syms); ArrayResize(syms, n + 1); syms[n] = _Symbol; }
   string ticks = "";
   for(int k = 0; k < ArraySize(syms); k++)
     {
      MqlTick tk;
      if(!SymbolInfoTick(syms[k], tk)) continue;
      ticks += (StringLen(ticks) > 0 ? "," : "") + "\"" + Esc(syms[k]) + "\":{\"bid\":" + D(tk.bid) +
               ",\"ask\":" + D(tk.ask) + ",\"time\":" + IntegerToString((long)tk.time) + "}";
     }
   string body = "{\"ea\":\"TzujenRollSync\",\"version\":\"" + EA_VERSION + "\"" +
                 ",\"login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) +
                 ",\"server\":\"" + Esc(AccountInfoString(ACCOUNT_SERVER)) + "\"" +
                 ",\"company\":\"" + Esc(AccountInfoString(ACCOUNT_COMPANY)) + "\"" +
                 ",\"currency\":\"" + Esc(AccountInfoString(ACCOUNT_CURRENCY)) + "\"" +
                 ",\"trade_mode\":" + IntegerToString(AccountInfoInteger(ACCOUNT_TRADE_MODE)) +
                 ",\"leverage\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)) +
                 ",\"balance\":" + D(AccountInfoDouble(ACCOUNT_BALANCE), 2) +
                 ",\"equity\":" + D(AccountInfoDouble(ACCOUNT_EQUITY), 2) +
                 ",\"margin\":" + D(AccountInfoDouble(ACCOUNT_MARGIN), 2) +
                 ",\"margin_free\":" + D(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) +
                 ",\"positions\":[" + pos + "]" +
                 ",\"ticks\":{" + ticks + "}}";

   char data[], res[];
   string resHeaders;
   int len = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(len > 0) ArrayResize(data, len - 1);   // 去掉結尾的 0
   string headers = "Content-Type: application/json\r\nX-Token: " + Token + "\r\n";
   ResetLastError();
   int code = WebRequest("POST", ServerURL + "/tools/roll-sync", headers, 8000, data, res, resHeaders);
   if(code == 200)
     {
      g_fail = 0;
      g_last_ok = TimeLocal();
      Comment("TzujenRollSync ", EA_VERSION, "（唯讀）\n回報正常 ", TimeToString(g_last_ok, TIME_SECONDS),
              "\n持倉 ", PositionsTotal(), " 筆");
      return;
     }
   g_fail++;
   string why;
   if(code == -1)
     {
      int err = GetLastError();
      why = "WebRequest 錯誤 " + IntegerToString(err);
      if(err == 4014) why = "網址未被允許：工具→選項→EA交易，勾選允許 WebRequest 並加入 " + ServerURL;
     }
   else if(code == 401) why = "Token 不對（要跟服務的 ROLL_SYNC_TOKEN 一樣）";
   else if(code == 404) why = "服務沒有啟用滾倉同步（沒設 ROLL_SYNC_TOKEN），或網址不對";
   else why = "HTTP " + IntegerToString(code) + " " + CharArrayToString(res, 0, WHOLE_ARRAY, CP_UTF8);
   Comment("TzujenRollSync ", EA_VERSION, "（唯讀）\n回報失敗 ", g_fail, " 次：", why,
           (g_last_ok > 0 ? "\n上次成功 " + TimeToString(g_last_ok, TIME_SECONDS) : ""));
   if(g_fail == 1 || g_fail % 20 == 0) Print("TzujenRollSync：", why);
  }
