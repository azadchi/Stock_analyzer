# -*- coding: utf-8 -*-
"""market_data.py — دریافت دیتای واقعی OHLCV نماد از TSETMC (از طریق algotik-tse)
به‌جای حدس زدن اعداد از روی عکس چارت.

نکته: TSETMC یک API رسمی عمومی ندارد؛ این ماژول از کتابخانه‌ی متن‌باز algotik-tse
استفاده می‌کند که با وب‌اسکرپینگ ساختاریافته داده را از همان سایت می‌گیرد (دقیقاً
همان کاری که مرورگر شما انجام می‌دهد). به همین دلیل ممکن است گاهی کند یا خطا بدهد؛
این ماژول این حالت را با خطای قابل‌فهم مدیریت می‌کند تا بقیه‌ی برنامه کرش نکند.
"""
import io

import pandas as pd


class MarketDataError(Exception):
    """خطای قابل‌نمایش به کاربر هنگام ناموفق بودن دریافت یا پردازش داده‌ی بازار."""


TIMEFRAME_INTRADAY = {"60 دقیقه": "60min", "15 دقیقه": "15min"}
TIMEFRAME_RESAMPLE = {"هفتگی": "W", "ماهانه": "ME"}


def _standardize_columns(df):
    df = df.copy()
    df.columns = [str(c).strip().title() for c in df.columns]
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise MarketDataError(
            "ستون‌های مورد نیاز در داده‌ی دریافتی از TSETMC نبود: " + "، ".join(sorted(missing))
        )
    for col in ("Open", "High", "Low", "Close", "Volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["Open", "High", "Low", "Close"])


def _find_col(columns, keywords):
    """اولین ستونی که هر کدام از کلیدواژه‌ها (فارسی/انگلیسی) در نامش باشد را برمی‌گرداند."""
    for col in columns:
        low = str(col).strip().lower()
        for kw in keywords:
            if kw in low or kw in str(col):
                return col
    return None


def load_ohlcv_from_file(file_storage, limit=300):
    """دیتای OHLCV را از یک فایل CSV/Excel که کاربر دستی از سایت TSETMC (یا هر منبع
    دیگر) دانلود و آپلود کرده می‌خواند. این مسیر جایگزین وقتی است که اتصال زنده به
    TSETMC (algotik-tse) به هر دلیلی (فیلترینگ، قطعی، کندی) کار نمی‌کند.
    """
    filename = (getattr(file_storage, "filename", "") or "").lower()
    try:
        raw = file_storage.read()
        buf = io.BytesIO(raw)
        if filename.endswith(".csv") or filename.endswith(".txt"):
            df = pd.read_csv(buf)
        else:
            df = pd.read_excel(buf, sheet_name=0)
    except Exception as exc:
        raise MarketDataError("خواندن فایل آپلودشده ناموفق بود: %s" % exc)

    if df is None or len(df) == 0:
        raise MarketDataError("فایل آپلودشده خالی است یا قابل‌خواندن نبود.")

    df.columns = [str(c).strip() for c in df.columns]
    col_open = _find_col(df.columns, ["open", "اول", "قیمت اول"])
    col_high = _find_col(df.columns, ["high", "بیشترین", "حداکثر"])
    col_low = _find_col(df.columns, ["low", "کمترین", "حداقل"])
    col_close = _find_col(df.columns, ["close", "پایانی", "پايانی", "آخر"])
    col_vol = _find_col(df.columns, ["vol", "حجم"])
    col_date = _find_col(df.columns, ["date", "تاریخ", "تاريخ"])

    missing = [name for name, col in
               [("Open", col_open), ("High", col_high), ("Low", col_low),
                ("Close", col_close), ("Volume", col_vol)] if col is None]
    if missing:
        raise MarketDataError(
            "ستون‌های زیر در فایل آپلودشده پیدا نشد: %s. ستون‌های موجود در فایل: %s"
            % ("، ".join(missing), "، ".join(map(str, df.columns)))
        )

    out = pd.DataFrame({
        "Open": pd.to_numeric(df[col_open], errors="coerce"),
        "High": pd.to_numeric(df[col_high], errors="coerce"),
        "Low": pd.to_numeric(df[col_low], errors="coerce"),
        "Close": pd.to_numeric(df[col_close], errors="coerce"),
        "Volume": pd.to_numeric(df[col_vol], errors="coerce"),
    })
    if col_date:
        out["_date"] = pd.to_datetime(df[col_date], errors="coerce")
        out = out.sort_values("_date")

    out = out.dropna(subset=["Open", "High", "Low", "Close"]).reset_index(drop=True)
    if len(out) < 30:
        raise MarketDataError(
            "بعد از پردازش فایل، فقط %d ردیف معتبر باقی ماند که برای محاسبه‌ی "
            "اندیکاتورها کافی نیست (حداقل ۳۰ کندل لازم است)." % len(out)
        )
    return out.tail(limit).reset_index(drop=True)


def fetch_ohlcv(symbol, timeframe, limit=300):
    """دیتای OHLCV واقعی نماد را برای تایم‌فریم مشخص برمی‌گرداند (DataFrame با
    ستون‌های Open/High/Low/Close/Volume، مرتب از قدیم به جدید)."""
    try:
        import algotik_tse as tse
    except ImportError:
        raise MarketDataError(
            "کتابخانه‌ی algotik-tse نصب نیست. دستور نصب: pip install algotik-tse"
        )

    symbol = (symbol or "").strip()
    if not symbol:
        raise MarketDataError("نام نماد خالی است.")

    try:
        if timeframe in TIMEFRAME_INTRADAY:
            df = tse.get_intraday(symbol, interval=TIMEFRAME_INTRADAY[timeframe], progress=False)
        else:
            df = tse.get_history(
                symbol,
                limit=max(limit, 260),
                output_type="standard",
                date_format="gregorian",
                auto_adjust=True,
                progress=False,
            )
    except Exception as exc:  # کتابخانه‌های اسکرپر خطاهای متنوعی می‌دهند
        raise MarketDataError(
            "دریافت اطلاعات نماد «%s» از TSETMC ناموفق بود (%s). ممکن است سایت موقتاً "
            "کند/در دسترس نباشد، یا نام نماد دقیق نباشد." % (symbol, exc)
        )

    if df is None or len(df) == 0:
        raise MarketDataError(
            "داده‌ای برای نماد «%s» یافت نشد. نام نماد باید دقیقاً مطابق نام رسمی "
            "در TSETMC باشد (مثلاً «شبندر» نه «شبندر سهام»)." % symbol
        )

    df = _standardize_columns(df)

    if timeframe in TIMEFRAME_RESAMPLE:
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, errors="coerce")
        df = df[df.index.notna()]
        agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        df = df.resample(TIMEFRAME_RESAMPLE[timeframe]).agg(agg).dropna()

    if len(df) < 30:
        raise MarketDataError(
            "داده‌ی کافی برای محاسبه‌ی اندیکاتورها موجود نیست (فقط %d ردیف). "
            "تایم‌فریم دیگری (مثلاً روزانه) را امتحان کن." % len(df)
        )

    return df.tail(limit).reset_index(drop=True)
