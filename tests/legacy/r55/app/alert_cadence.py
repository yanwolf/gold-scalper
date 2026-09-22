"""
需要人處理的持續失敗狀態，告警節奏(BINANCE_LESSONS.md第8條「告警節奏」，r6三個專案統一)：
失敗第1、5、30次各發一次，之後第150、270、390…次(第30次後每隔120次)，恢復時再發一次。
只發一次的告警很容易被後續訊息淹沒、被當成已處理；每次都發又會變成噪音。

用在：backstop停損單掛不上/搬不動(第8條、第2條「補掛失敗」)、平倉後殘留單撤不掉(第13條)。
"""

FIRST_ALERTS = (1, 5, 30)
REPEAT_EVERY = 120


def should_alert(fail_count):
    """第fail_count次失敗要不要發提醒。"""
    if fail_count in FIRST_ALERTS:
        return True
    # 「之後每120次」＝第30次之後每隔120次：150、270、390…(r8精確定義，三個專案一致)
    return fail_count > FIRST_ALERTS[-1] and (fail_count - FIRST_ALERTS[-1]) % REPEAT_EVERY == 0
