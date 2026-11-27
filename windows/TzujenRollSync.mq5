//+------------------------------------------------------------------+
//| TzujenRollSync.mq5  v2.00                                         |
//| 每幾秒把帳戶（結餘、淨值）、持倉、掛單回報給 gold-scalper 的滾倉計算器 |
//| （/tools/roll-sync）。                                             |
//| v2：伺服器在「執行」模式時會回傳指令（收緊停損、掛／刪自動加碼單），   |
//| 只有參數 AllowTrading=true 才會執行；預設 false＝跟 v1 一樣只回報。   |
//| EA 端自己再擋一次：停損只准收緊、只刪自己 magic 的單、手數有上限。     |
//+------------------------------------------------------------------+
#property copyright "tzujenlab"
#property version   "2.00"
#include <Trade\Trade.mqh>

input string ServerURL    = "https://gold-live.tzujenlab.com";   // 設了 ROLL_SYNC_TOKEN 的 gold-scalper 服務（要加到 允許 WebRequest）
input string Token        = "";                                  // 與服務的 ROLL_SYNC_TOKEN 相同
input int    SyncMs       = 3000;                                // 回報間隔（毫秒）
input bool   AllowTrading = false;                               // 第二把鑰匙：true 才會執行伺服器的指令
input double MaxLots      = 0.05;                                // 自動加碼單每張手數上限（EA 端再擋一次）
input long   RollMagic    = 26112801;                            // 自動加碼單的 magic（要跟伺服器一致）

#define EA_VERSION "2.00"

CTrade   g_trade;
int      g_fail = 0;
datetime g_last_ok = 0;
string   g_mode = "?";
string   g_results = "";        // 還沒回報給伺服器的執行結果（JSON 片段，用逗號接）
string   g_done[64];            // 執行過的指令 id（避免重複執行）
int      g_done_n = 0;
string   g_last_action = "";

int OnInit()
  {
   if(StringLen(Token) < 8)
     {
      Alert("TzujenRollSync：請在參數填入 Token（與服務的 ROLL_SYNC_TOKEN 相同）");
      return(INIT_PARAMETERS_INCORRECT);
     }
   g_trade.SetExpertMagicNumber((ulong)RollMagic);
   g_trade.SetDeviationInPoints(50);
   EventSetMillisecondTimer(MathMax(SyncMs, 1000));
   Print("TzujenRollSync ", EA_VERSION, " 啟動，AllowTrading=", (AllowTrading ? "true" : "false"), "，伺服器 ", ServerURL);
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

void AddSym(string &arr[], string s)
  {
   if(InList(arr, s)) return;
   int n = ArraySize(arr);
   ArrayResize(arr, n + 1);
   arr[n] = s;
  }

bool SeenId(string id)
  {
   for(int i = 0; i < MathMin(g_done_n, 64); i++) if(g_done[i] == id) return true;
   return false;
  }

void RememberId(string id) { g_done[g_done_n % 64] = id; g_done_n++; }

void AddResult(string id, bool ok, long retcode, string msg)
  {
   string one = "{\"id\":\"" + Esc(id) + "\",\"ok\":" + (ok ? "true" : "false") +
                ",\"retcode\":" + IntegerToString(retcode) + ",\"msg\":\"" + Esc(msg) + "\"}";
   g_results += (StringLen(g_results) > 0 ? "," : "") + one;
   if(ok) RememberId(id);      // 只記成功的；失敗的伺服器過一陣子會再送(重試)
   g_last_action = (ok ? "OK " : "失敗 ") + msg;
   Print("TzujenRollSync 執行 ", id, "：", (ok ? "成功 " : "失敗 "), retcode, " ", msg);
  }

bool RetOk(uint rc) { return rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_PLACED || rc == TRADE_RETCODE_DONE_PARTIAL; }

//--- 收緊某張持倉的停損（EA 端再檢查一次：只准往有利方向）
void DoModify(string id, ulong ticket, double sl)
  {
   if(!PositionSelectByTicket(ticket)) { AddResult(id, false, -1, "找不到持倉 #" + IntegerToString((long)ticket)); return; }
   string sym = PositionGetString(POSITION_SYMBOL);
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   sl = NormalizeDouble(sl, digits);
   double cur = PositionGetDouble(POSITION_SL);
   long type = PositionGetInteger(POSITION_TYPE);
   bool tighter = (type == POSITION_TYPE_BUY) ? (sl > cur) : (cur == 0 || sl < cur);
   if(cur == 0 || !tighter)
     {
      AddResult(id, false, -2, "#" + IntegerToString((long)ticket) + " 新停損 " + D(sl, digits) + " 沒有比目前 " + D(cur, digits) + " 緊，不改");
      return;
     }
   bool ok = g_trade.PositionModify(ticket, sl, PositionGetDouble(POSITION_TP));
   uint rc = g_trade.ResultRetcode();
   AddResult(id, ok && RetOk(rc), rc, "#" + IntegerToString((long)ticket) + " 停損 " + D(cur, digits) + "→" + D(sl, digits) +
             " " + g_trade.ResultRetcodeDescription());
  }

//--- 掛自動加碼單（Buy Stop／Sell Stop）
void DoPending(string id, string typ, double price, double vol, double sl)
  {
   string sym = _Symbol;
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   price = NormalizeDouble(price, digits);
   sl = NormalizeDouble(sl, digits);
   if(vol <= 0 || vol > MaxLots + 1e-9) { AddResult(id, false, -3, "手數 " + D(vol, 2) + " 超過 EA 上限 " + D(MaxLots, 2)); return; }
   for(int i = 0; i < OrdersTotal(); i++)
     {
      ulong t = OrderGetTicket(i);
      if(t > 0 && OrderSelect(t) && OrderGetString(ORDER_SYMBOL) == sym && OrderGetInteger(ORDER_MAGIC) == RollMagic)
        { AddResult(id, false, -4, "已經有一張自動加碼單 #" + IntegerToString((long)t) + "，不重複掛"); return; }
     }
   bool ok = false;
   if(typ == "BUY_STOP")       ok = g_trade.BuyStop(vol, price, sym, sl, 0, ORDER_TIME_GTC, 0, "roll-add");
   else if(typ == "SELL_STOP") ok = g_trade.SellStop(vol, price, sym, sl, 0, ORDER_TIME_GTC, 0, "roll-add");
   else { AddResult(id, false, -5, "不認得的掛單種類 " + typ); return; }
   uint rc = g_trade.ResultRetcode();
   AddResult(id, ok && RetOk(rc), rc, typ + " " + D(vol, 2) + " 手 @" + D(price, digits) + " 停損 " + D(sl, digits) + " " +
             g_trade.ResultRetcodeDescription());
  }

//--- 刪自動加碼單（只刪自己 magic 的）
void DoDelete(string id, ulong ticket)
  {
   if(!OrderSelect(ticket)) { AddResult(id, false, -6, "找不到掛單 #" + IntegerToString((long)ticket)); return; }
   if(OrderGetInteger(ORDER_MAGIC) != RollMagic) { AddResult(id, false, -7, "#" + IntegerToString((long)ticket) + " 不是自動加碼單，不刪"); return; }
   bool ok = g_trade.OrderDelete(ticket);
   uint rc = g_trade.ResultRetcode();
   AddResult(id, ok && RetOk(rc), rc, "刪除掛單 #" + IntegerToString((long)ticket) + " " + g_trade.ResultRetcodeDescription());
  }

//--- 解析伺服器回應：第一行 OK|mode，之後每行一個指令
void HandleResponse(string text)
  {
   string lines[];
   int n = StringSplit(text, '\n', lines);
   for(int i = 0; i < n; i++)
     {
      string ln = lines[i];
      StringTrimLeft(ln);
      StringTrimRight(ln);
      if(StringLen(ln) == 0) continue;
      string p[];
      int k = StringSplit(ln, '|', p);
      if(i == 0)
        {
         if(k >= 2 && p[0] == "OK") g_mode = p[1];
         continue;
        }
      if(k < 3) continue;
      string id = p[1];
      if(SeenId(id)) continue;   // 已經成功執行過的同一個指令不再做
      if(!AllowTrading) { AddResult(id, false, -9, "EA 參數 AllowTrading 沒開，沒有執行"); continue; }
      if(p[0] == "MOD" && k >= 4)       DoModify(id, (ulong)StringToInteger(p[2]), StringToDouble(p[3]));
      else if(p[0] == "PEND" && k >= 6) DoPending(id, p[2], StringToDouble(p[3]), StringToDouble(p[4]), StringToDouble(p[5]));
      else if(p[0] == "DEL" && k >= 3) DoDelete(id, (ulong)StringToInteger(p[2]));
      else AddResult(id, false, -8, "看不懂的指令：" + ln);
     }
  }

string SymInfo(string sym)
  {
   MqlTick tk;
   if(!SymbolInfoTick(sym, tk)) return "";
   return "\"" + Esc(sym) + "\":{\"bid\":" + D(tk.bid) + ",\"ask\":" + D(tk.ask) + ",\"time\":" + IntegerToString((long)tk.time) +
          ",\"point\":" + D(SymbolInfoDouble(sym, SYMBOL_POINT), 8) +
          ",\"digits\":" + IntegerToString(SymbolInfoInteger(sym, SYMBOL_DIGITS)) +
          ",\"stops_level\":" + IntegerToString(SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL)) +
          ",\"vol_min\":" + D(SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN), 2) +
          ",\"vol_step\":" + D(SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP), 2) + "}";
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
      AddSym(syms, sym);
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
   string ords = "";
   for(int j = 0; j < OrdersTotal(); j++)
     {
      ulong t = OrderGetTicket(j);
      if(t == 0 || !OrderSelect(t)) continue;
      string sym = OrderGetString(ORDER_SYMBOL);
      string one = "{\"ticket\":" + IntegerToString((long)t) +
                   ",\"symbol\":\"" + Esc(sym) + "\"" +
                   ",\"type\":" + IntegerToString(OrderGetInteger(ORDER_TYPE)) +
                   ",\"volume\":" + D(OrderGetDouble(ORDER_VOLUME_CURRENT), 2) +
                   ",\"price\":" + D(OrderGetDouble(ORDER_PRICE_OPEN)) +
                   ",\"sl\":" + D(OrderGetDouble(ORDER_SL)) +
                   ",\"magic\":" + IntegerToString(OrderGetInteger(ORDER_MAGIC)) + "}";
      ords += (StringLen(ords) > 0 ? "," : "") + one;
     }
   AddSym(syms, _Symbol);
   string ticks = "";
   for(int k = 0; k < ArraySize(syms); k++)
     {
      string one = SymInfo(syms[k]);
      if(StringLen(one) > 0) ticks += (StringLen(ticks) > 0 ? "," : "") + one;
     }
   string sending = g_results;
   string body = "{\"ea\":\"TzujenRollSync\",\"version\":\"" + EA_VERSION + "\"" +
                 ",\"allow_trading\":" + (AllowTrading ? "true" : "false") +
                 ",\"magic\":" + IntegerToString(RollMagic) +
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
                 ",\"orders\":[" + ords + "]" +
                 ",\"results\":[" + sending + "]" +
                 ",\"ticks\":{" + ticks + "}}";

   char data[], res[];
   string resHeaders;
   int len = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(len > 0) ArrayResize(data, len - 1);   // 去掉結尾的 0
   string headers = "Content-Type: application/json\r\nX-Token: " + Token + "\r\nX-Proto: 2\r\n";
   ResetLastError();
   int code = WebRequest("POST", ServerURL + "/tools/roll-sync", headers, 8000, data, res, resHeaders);
   if(code == 200)
     {
      g_fail = 0;
      g_last_ok = TimeLocal();
      // 已送出的結果清掉（送出後才新增的保留）
      if(StringLen(sending) > 0)
        {
         if(StringLen(g_results) > StringLen(sending)) g_results = StringSubstr(g_results, StringLen(sending) + 1);
         else g_results = "";
        }
      HandleResponse(CharArrayToString(res, 0, WHOLE_ARRAY, CP_UTF8));
      Comment("TzujenRollSync ", EA_VERSION, "　伺服器模式：", g_mode, "　EA 執行：", (AllowTrading ? "開" : "關"),
              "\n回報正常 ", TimeToString(g_last_ok, TIME_SECONDS), "　持倉 ", PositionsTotal(), " 筆　掛單 ", OrdersTotal(), " 張",
              (StringLen(g_last_action) > 0 ? "\n最近執行：" + g_last_action : ""));
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
   Comment("TzujenRollSync ", EA_VERSION, "　EA 執行：", (AllowTrading ? "開" : "關"), "\n回報失敗 ", g_fail, " 次：", why,
           (g_last_ok > 0 ? "\n上次成功 " + TimeToString(g_last_ok, TIME_SECONDS) : ""));
   if(g_fail == 1 || g_fail % 20 == 0) Print("TzujenRollSync：", why);
  }
