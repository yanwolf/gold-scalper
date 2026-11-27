# 把 TzujenRollSync(唯讀同步，不下單)裝進這台電腦所有的 MT5，並自動編譯
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot
$Host.UI.RawUI.WindowTitle = '安裝 TzujenRollSync EA'
function Pause-Exit($code) { Write-Host ''; Read-Host '按 Enter 關閉視窗' | Out-Null; exit $code }

$src = Join-Path $PSScriptRoot 'TzujenRollSync.mq5'
if (-not (Test-Path $src)) { $src = Join-Path (Split-Path $PSScriptRoot -Parent) 'ea\TzujenRollSync.mq5' }
if (-not (Test-Path $src)) { Write-Host '找不到 TzujenRollSync.mq5' -ForegroundColor Red; Pause-Exit 1 }

$root = Join-Path $env:APPDATA 'MetaQuotes\Terminal'
$dirs = Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path (Join-Path $_.FullName 'origin.txt') }
if (-not $dirs) {
    Write-Host '沒有找到 MT5 資料夾。請先安裝券商的 MT5、登入過一次，再執行這個腳本。' -ForegroundColor Red
    Pause-Exit 1
}
$n = 0
foreach ($d in $dirs) {
    $bytes = [IO.File]::ReadAllBytes((Join-Path $d.FullName 'origin.txt'))
    if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) { $install = [Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2) }
    else { $install = [Text.Encoding]::Default.GetString($bytes) }
    $install = $install.Trim([char]0, ' ', "`r", "`n")
    $me = Join-Path $install 'metaeditor64.exe'
    if (-not (Test-Path $me)) { continue }
    $n++
    Write-Host ''
    Write-Host ("#{0}  {1}" -f $n, $install) -ForegroundColor Yellow
    $expDir = Join-Path $d.FullName 'MQL5\Experts'
    New-Item -ItemType Directory -Force -Path $expDir | Out-Null
    $dst = Join-Path $expDir 'TzujenRollSync.mq5'
    Copy-Item $src $dst -Force
    $log = Join-Path $env:TEMP 'TzujenRollSync_compile.log'
    Remove-Item $log -ErrorAction SilentlyContinue
    Start-Process -FilePath $me -ArgumentList "/compile:`"$dst`"", "/log:`"$log`"" -Wait
    $ex5 = Join-Path $expDir 'TzujenRollSync.ex5'
    if (Test-Path $ex5) {
        Write-Host '  已安裝並編譯完成' -ForegroundColor Green
    } else {
        Write-Host '  編譯失敗，錯誤訊息如下（截圖給 Claude）：' -ForegroundColor Red
        if (Test-Path $log) { Get-Content $log -Encoding Unicode | Select-String -Pattern 'error' | Select-Object -First 15 | ForEach-Object { Write-Host "  $_" } }
    }
}
if ($n -eq 0) { Write-Host '找到 MT5 資料夾，但找不到 metaeditor64.exe。' -ForegroundColor Red; Pause-Exit 1 }

Write-Host ''
Write-Host '這支 EA 只回報、不下單。只要掛在 Vantage 那個 MT5 上，Pepperstone 那個不用掛。' -ForegroundColor Cyan
Write-Host ''
Write-Host '接下來在 Vantage 的 MT5 裡做三件事：' -ForegroundColor Cyan
Write-Host '  1. 工具 → 選項 → EA交易：勾選「允許演算法交易」與「允許 WebRequest 的網址」，加入 https://gold-live.tzujenlab.com'
Write-Host '  2. 導航視窗 → EA交易 → 把 TzujenRollSync 拖到 XAUUSD+ 的圖表上，參數 Token 填 ROLL_SYNC_TOKEN 那串'
Write-Host '  3. 上方工具列的「演算法交易」按鈕要是開啟狀態（綠色）；圖表左上角出現「回報正常」就完成'
Pause-Exit 0
