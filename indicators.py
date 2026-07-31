# -*- coding: utf-8 -*-
"""indicators.py — محاسبه‌ی دقیق اندیکاتورهای تکنیکال (RSI, MACD, ATR, ایچیموکو,
EMA, فیبوناچی) روی دیتای واقعی OHLCV؛ فقط با pandas/numpy خالص، بدون وابستگی به
pandas-ta، برای پایداری و سازگاری بیشتر.
"""
import numpy as np
import pandas as pd


def _ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def macd(close, fast=12, slow=26, signal=9):
    macd_line = _ema(close, fast) - _ema(close, slow)
    signal_line = _ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line


def atr(df, period=14):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def ichimoku(df, tenkan_p=9, kijun_p=26, senkou_b_p=52):
    high, low = df["High"], df["Low"]
    tenkan = (high.rolling(tenkan_p).max() + low.rolling(tenkan_p).min()) / 2
    kijun = (high.rolling(kijun_p).max() + low.rolling(kijun_p).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(kijun_p)
    senkou_b = ((high.rolling(senkou_b_p).max() + low.rolling(senkou_b_p).min()) / 2).shift(kijun_p)
    return tenkan, kijun, senkou_a, senkou_b


def fibonacci_levels(df, lookback=90):
    window = df.tail(min(lookback, len(df)))
    swing_high = float(window["High"].max())
    swing_low = float(window["Low"].min())
    diff = swing_high - swing_low
    ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    levels = {str(r): round(swing_high - diff * r, 2) for r in ratios}
    return levels, swing_high, swing_low


def build_indicator_summary(df):
    """خلاصه‌ی متنی فارسی از اندیکاتورهای دقیق محاسبه‌شده روی دیتای واقعی —
    برای الحاق مستقیم به پرامپت مدل، تا مدل به‌جای حدس بصری از عکس، از این اعداد
    استفاده کند."""
    close = df["Close"]
    last = float(close.iloc[-1])

    r = rsi(close)
    m_line, m_signal, m_hist = macd(close)
    a = atr(df)
    tenkan, kijun, senkou_a, senkou_b = ichimoku(df)
    ema20, ema50 = _ema(close, 20), _ema(close, 50)
    fib_levels, swing_high, swing_low = fibonacci_levels(df)

    kijun_last = kijun.iloc[-1]
    kijun_dist_txt = "نامشخص (داده کافی نیست)"
    if pd.notna(kijun_last) and kijun_last:
        kijun_dist_txt = f"{((last - kijun_last) / kijun_last * 100):.1f}٪"

    trend = "نامشخص"
    if pd.notna(ema20.iloc[-1]) and pd.notna(ema50.iloc[-1]):
        if last > ema20.iloc[-1] > ema50.iloc[-1]:
            trend = "صعودی"
        elif last < ema20.iloc[-1] < ema50.iloc[-1]:
            trend = "نزولی"
        else:
            trend = "خنثی/رنج"

    macd_state = "مثبت / تقاطع صعودی" if m_hist.iloc[-1] > 0 else "منفی / تقاطع نزولی"

    lines = [
        f"### دیتای عددی دقیق (محاسبه‌شده روی {len(df)} کندل واقعی دریافتی از TSETMC؛ "
        f"این اعداد را عیناً مبنای تحلیل قرار بده، عددی از خودت نساز):",
        f"- قیمت پایانی آخرین کندل: {last:,.2f}",
        f"- RSI(14): {r.iloc[-1]:.1f}",
        f"- MACD: خط={m_line.iloc[-1]:.3f} | سیگنال={m_signal.iloc[-1]:.3f} | "
        f"هیستوگرام={m_hist.iloc[-1]:.3f} ({macd_state})",
        f"- ATR(14): {a.iloc[-1]:,.2f} ({(a.iloc[-1] / last * 100 if last else 0):.1f}٪ از قیمت)",
        f"- EMA20: {ema20.iloc[-1]:,.2f} | EMA50: {ema50.iloc[-1]:,.2f} | روند بر اساس EMA: {trend}",
    ]

    if pd.notna(kijun_last) and pd.notna(tenkan.iloc[-1]):
        sa = senkou_a.iloc[-1]
        sb = senkou_b.iloc[-1]
        cloud_txt = f"{sa:,.2f}/{sb:,.2f}" if pd.notna(sa) and pd.notna(sb) else "داده کافی برای ابر نیست"
        lines.append(
            f"- ایچیموکو: تنکن‌سن={tenkan.iloc[-1]:,.2f} | کیجون‌سن={kijun_last:,.2f} | "
            f"فاصله‌ی قیمت تا کیجون‌سن={kijun_dist_txt} | ابر (سنکو A/B)={cloud_txt}"
        )
    else:
        lines.append("- ایچیموکو: داده‌ی تاریخی کافی برای محاسبه‌ی کامل ابر موجود نیست.")

    fib_txt = "، ".join(f"{k}={v:,.2f}" for k, v in fib_levels.items())
    lines.append(
        f"- فیبوناچی (بر مبنای سقف/کف {min(90, len(df))} کندل اخیر؛ سقف={swing_high:,.2f}، "
        f"کف={swing_low:,.2f}): {fib_txt}"
    )

    return "\n".join(lines)
