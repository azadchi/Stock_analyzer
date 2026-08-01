# -*- coding: utf-8 -*-
"""brs.py — هسته برنامه «تحلیل‌ساز بورس». تمام HTML در پوشه templates است.

تغییرات نسبت به نسخه قبلی:
  ۱) رفع باگ سینتکسی در parse_json (بک‌تیک‌های مارک‌داون به‌اشتباه در کد افتاده بودند
     و کل فایل اصلاً اجرا نمی‌شد).
  ۲) مهاجرت از SDK منسوخ «google-generativeai» به SDK جدید و یکپارچه «google-genai».
     همچنین مدل پیش‌فرض به‌روزرسانی شد چون gemini-2.0-flash از ۱ ژوئن ۲۰۲۶ کاملاً
     shutdown شده و دیگر پاسخ نمی‌دهد.
  ۳) escape کردن تمام متن‌ها قبل از رندر در PDF (reportlab بخشی از تگ‌های XML/HTML را
     تفسیر می‌کند؛ بدون escape، یک '<' یا '&' در خروجی مدل می‌توانست ساخت PDF را بشکند).
  ۴) اعتبارسنجی واقعی محتوای تصویر با Pillow (به‌جای اعتماد به mimetype که کلاینت
     اعلام می‌کند و به‌راحتی قابل جعل است) + کوچک‌سازی خودکار تصاویر بزرگ برای کاهش
     هزینه و زمان تحلیل.
  ۵) لایه احراز هویت اختیاری با کلید ساده (BRS_API_KEY) + محدودکننده نرخ درخواست
     برای جلوگیری از هزینه کنترل‌نشده روی API مدل.
  ۶) لاگ‌گیری منظم به فایل به‌جای بلعیدن سکوت‌آمیز خطاها.
  ۷) صفحه‌بندی واقعی تاریخچه + ایندکس روی دیتابیس.
  ۸) retry با backoff برای خطاهای موقت مدل + بررسی فونت فارسی هنگام بالا آمدن سرور.

نصب وابستگی‌ها:
    pip install flask google-genai pillow reportlab arabic-reshaper python-bidi
"""

import io
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from xml.sax.saxutils import escape as xml_escape

from flask import Flask, jsonify, render_template, request, send_file

from market_data import load_ohlcv_from_file, MarketDataError
from indicators import build_indicator_summary

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "analysis.db")
FONT_DIR = os.path.join(BASE_DIR, "fonts")
LOG_PATH = os.path.join(BASE_DIR, "brs.log")

MAX_IMAGES = 6
MAX_UPLOAD_MB = 16
MAX_IMAGE_DIMENSION = 1600            # حداکثر ضلع تصویر پس از کوچک‌سازی خودکار (px)
IMAGE_JPEG_QUALITY = 85
ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp"}
TIMEFRAMES = ["روزانه", "هفتگی", "ماهانه", "60 دقیقه", "15 دقیقه"]
VALID_SIGNALS = {"خرید", "فروش", "نگهداری", "نامشخص"}

# توجه: نام مدل‌های Gemini مدام تغییر می‌کند و مدل‌های قدیمی shutdown می‌شوند.
# قبل از استقرار، لیست فعلی را اینجا چک کن: https://ai.google.dev/gemini-api/docs/changelog
MODEL_NAME = os.environ.get("BRS_MODEL", "gemini-3.6-flash")

API_KEY_HEADER = "X-API-Key"
RATE_LIMIT_WINDOW_SEC = 60
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("BRS_RATE_LIMIT", "10"))
GEMINI_TIMEOUT_SEC = 60
GEMINI_MAX_RETRIES = 3

# ------------------------------------------------------------------------ لاگ
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("brs")

app = Flask(__name__)
app.secret_key = os.environ.get("BRS_SECRET_KEY") or secrets.token_hex(32)
if not os.environ.get("BRS_SECRET_KEY"):
    log.warning(
        "BRS_SECRET_KEY تنظیم نشده؛ یک کلید تصادفی موقت ساخته شد "
        "(با هر ری‌استارت سرور عوض می‌شود؛ برای پروڈاکشن آن را در env ثابت کن)."
    )

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
try:
    app.json.ensure_ascii = False
except Exception:
    app.config["JSON_AS_ASCII"] = False

API_KEY = os.environ.get("BRS_API_KEY", "").strip()
if not API_KEY:
    log.warning(
        "BRS_API_KEY تنظیم نشده؛ سرور بدون احراز هویت اجرا می‌شود "
        "(فقط برای استفاده کاملاً لوکال و شخصی مناسب است)."
    )


# -------------------------------------------------------------- احراز هویت ساده
def require_api_key(view):
    """اگر BRS_API_KEY ست شده باشد، درخواست باید هدر X-API-Key یا پارامتر ?key=
    را با همان مقدار ارسال کند. اگر ست نشده باشد، این دکوراتور اثری ندارد
    (حالت مناسب برای اجرای کاملاً لوکال روی سیستم خودت)."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not API_KEY:
            return view(*args, **kwargs)
        supplied = request.headers.get(API_KEY_HEADER) or request.args.get("key", "")
        if not secrets.compare_digest(supplied, API_KEY):
            return jsonify(ok=False, error="دسترسی غیرمجاز."), 401
        return view(*args, **kwargs)

    return wrapper


# ----------------------------------------------------------- محدودکننده نرخ درخواست
_rate_buckets = defaultdict(deque)


def rate_limited(max_requests=RATE_LIMIT_MAX_REQUESTS, window=RATE_LIMIT_WINDOW_SEC):
    """محدودیت ساده در حافظه بر اساس IP. هدف: جلوگیری از فراخوانی بی‌رویه و پرهزینه
    API مدل، نه امنیت کامل (برای آن از یک rate-limiter واقعی مثل Redis استفاده کن)."""

    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
            now = time.time()
            bucket = _rate_buckets[ip]
            while bucket and now - bucket[0] > window:
                bucket.popleft()
            if len(bucket) >= max_requests:
                return jsonify(ok=False, error="تعداد درخواست‌ها بیش از حد مجاز است؛ کمی صبر کن."), 429
            bucket.append(now)
            return view(*args, **kwargs)

        return wrapper

    return deco


# ---------------------------------------------------------------- بانک اطلاعات
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db():
    ddl = (
        "CREATE TABLE IF NOT EXISTS analyses ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT NOT NULL,"
        " symbol TEXT NOT NULL,"
        " timeframe TEXT,"
        " signal TEXT,"
        " confidence INTEGER DEFAULT 0,"
        " entry TEXT, stop_loss TEXT, targets TEXT,"
        " summary TEXT, reasons TEXT, risks TEXT,"
        " raw_json TEXT, image_count INTEGER DEFAULT 0)"
    )
    conn = get_db()
    try:
        conn.execute(ddl)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_analyses_created ON analyses(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_analyses_symbol ON analyses(symbol)")
        conn.commit()
    finally:
        conn.close()


def check_fonts():
    known = ("Vazirmatn-Regular.ttf", "Vazir.ttf", "Sahel.ttf", "IRANSans.ttf")
    found = [n for n in known if os.path.exists(os.path.join(FONT_DIR, n))]
    if not found:
        log.warning(
            "هیچ فونت فارسی در %s پیدا نشد؛ خروجی PDF با Helvetica ساخته می‌شود که گلیف "
            "فارسی/عربی ندارد و متن به‌درستی نمایش داده نخواهد شد. یکی از این فونت‌ها را "
            "در پوشه fonts قرار بده: %s",
            FONT_DIR, ", ".join(known),
        )
    else:
        log.info("فونت فارسی یافت‌شده برای PDF: %s", found[0])


init_db()
check_fonts()


# ------------------------------------------------------------------- دستور مدل
def build_prompt(symbol, timeframe, notes, n_images, indicator_text=None):
    lines = [
        "تو یک تحلیل‌گر ارشد و محافظه‌کار بازار سهام ایران هستی.",
        "وظیفه: تحلیل تکنیکال + تابلوخوانی نماد زیر و صدور یک سیگنال شفاف.",
        "",
        "نماد: " + symbol,
        "تایم‌فریم: " + timeframe,
        "تعداد تصویر پیوست‌شده (اسکرین‌شات تابلوی معاملاتی TSETMC): " + str(n_images),
    ]
    if notes:
        lines += ["یادداشت کاربر: " + notes]
    if indicator_text:
        lines += ["", indicator_text, "",
                   "توجه: اعداد بالا از فایل تکنیکال دقیق (اکسل/CSV سابقه‌ی قیمت) محاسبه شده‌اند "
                   "و باید مبنای اصلی تحلیل باشند."]
    else:
        lines += ["", "توجه: فایل تکنیکال (اکسل/CSV) برای این درخواست ارسال نشده؛ اندیکاتورهای "
                       "دقیق (RSI/MACD/ایچیموکو/فیبوناچی) در دسترس نیستند. اگر لازم بود از روی "
                       "تصویر تخمین بزنی، صراحتاً بنویس که تخمینی است."]
    if n_images:
        lines += ["",
                   "تصویر(های) پیوست‌شده، اسکرین‌شات «تابلوی معاملاتی» است (نه صرفاً نمودار). "
                   "این اطلاعات را که فقط از روی همین تصویر قابل‌خواندن است و در فایل اکسل نیست، "
                   "استخراج و در تحلیل لحاظ کن:",
                   "- قدرت خریدار/فروشنده حقیقی و حقوقی (نسبت خرید به فروش هرکدام)",
                   "- ورود یا خروج پول حقیقی (مثبت/منفی بودن جریان نقدینگی حقیقی)",
                   "- صف خرید یا صف فروش (در صورت وجود) و حجم تقریبی آن",
                   "- تعداد خریداران در برابر تعداد فروشندگان",
                   "- آخرین قیمت، قیمت پایانی، و درصد تغییر نسبت به روز قبل (اگر در تصویر دیده می‌شود)",
                   "این موارد را زیر یک بخش جداگانه در «summary» یا در «reasons» به‌صراحت ذکر کن."]
    lines += [
        "",
        "قواعد الزامی:",
        "۱) اگر تصویر ناخوانا یا داده کافی نبود، signal را «نامشخص» بگذار و دلیلش را بنویس.",
        "۲) عدد از خودت نساز؛ فقط از سطوح قابل مشاهده در تصاویر استفاده کن.",
        "۳) confidence عددی بین ۰ تا ۱۰۰.",
        "۴) کل خروجی فارسی باشد.",
        "۵) reasons حداکثر ۴ مورد، risks حداکثر ۳ مورد، هرکدام یک جمله‌ی کوتاه (نه پاراگراف).",
        "۶) summary حداکثر ۳ خط کوتاه.",
        "",
        "فقط و فقط یک شیء JSON کامل و معتبر با این کلیدها برگردان (بدون توضیح اضافه، بدون "
        "فنس مارک‌داون، و بدون هیچ متنی قبل یا بعد از JSON):",
        '{"signal": "خرید|فروش|نگهداری|نامشخص",',
        ' "confidence": 0,',
        ' "entry": "محدوده ورود",',
        ' "stop_loss": "حد ضرر",',
        ' "targets": ["هدف اول", "هدف دوم"],',
        ' "trend": "صعودی|نزولی|خنثی",',
        ' "indicators": "خلاصه RSI و MACD و میانگین‌ها",',
        ' "reasons": ["دلیل اول", "دلیل دوم"],',
        ' "risks": ["ریسک اول"],',
        ' "summary": "جمع‌بندی نهایی در چند خط"}',
    ]
    return "\n".join(lines)


def validate_and_prepare_image(file_storage):
    """اعتبارسنجی واقعی محتوای تصویر (نه فقط mimetype اعلامی از سمت کلاینت که به‌راحتی
    قابل جعل است) + کوچک‌سازی تصاویر بزرگ برای کاهش هزینه/زمان تحلیل.

    خروجی: (mime, bytes, None) در صورت موفقیت، یا (None, None, پیام_خطا) در غیر این صورت.
    """
    from PIL import Image, UnidentifiedImageError

    raw = file_storage.read()
    if not raw:
        return None, None, "فایل «%s» خالی است." % file_storage.filename

    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw))  # بعد از verify باید دوباره باز شود
        img.load()
    except Exception:
        return None, None, "فایل «%s» یک تصویر معتبر نیست یا خراب است." % file_storage.filename

    fmt = (img.format or "").upper()
    fmt_to_mime = {"PNG": "image/png", "JPEG": "image/jpeg", "JPG": "image/jpeg", "WEBP": "image/webp"}
    mime = fmt_to_mime.get(fmt)
    if mime not in ALLOWED_MIME:
        return None, None, "فرمت واقعی فایل «%s» (%s) پشتیبانی نمی‌شود." % (file_storage.filename, fmt)

    if max(img.size) > MAX_IMAGE_DIMENSION:
        img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)
        buf = io.BytesIO()
        if mime == "image/png":
            img.save(buf, format="PNG", optimize=True)
        else:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
            mime = "image/jpeg"
        return mime, buf.getvalue(), None

    return mime, raw, None


def call_gemini(prompt, images):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY تنظیم نشده است. متغیرهای محیطی را بررسی کن.")

    # SDK جدید و یکپارچه گوگل (google-genai)؛ کتابخانه قدیمی google-generativeai
    # از مارس ۲۰۲۵ منسوخ شده و مدل‌های جدید را اصلاً پشتیبانی نمی‌کند.
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_SEC * 1000))

    parts = [types.Part.from_text(text=prompt)]
    for mime, data in images:
        parts.append(types.Part.from_bytes(data=data, mime_type=mime))

    config = types.GenerateContentConfig(
        temperature=0.25,
        max_output_tokens=8192,
        response_mime_type="application/json",
    )

    last_error = None
    for attempt in range(1, GEMINI_MAX_RETRIES + 2):
        try:
            resp = client.models.generate_content(
                model=MODEL_NAME,
                contents=[types.Content(role="user", parts=parts)],
                config=config,
            )
            text = (getattr(resp, "text", "") or "").strip()
            finish_reason = None
            try:
                finish_reason = resp.candidates[0].finish_reason
            except Exception:
                pass
            if finish_reason and str(finish_reason).upper().find("MAX_TOKEN") != -1:
                log.warning(
                    "پاسخ Gemini به‌خاطر رسیدن به سقف طول (max_output_tokens) بریده شده "
                    "و ممکن است JSON ناقص باشد. طول متن دریافتی: %d کاراکتر.", len(text)
                )
            if text:
                return text
            raise RuntimeError(
                "پاسخی از مدل دریافت نشد" + (" (دلیل: %s)" % finish_reason if finish_reason else "") + "."
            )
        except (APIError, ConnectionError, OSError, TimeoutError) as exc:
            last_error = exc
            log.warning("خطای موقت/شبکه‌ای در ارتباط با Gemini (تلاش %d/%d): %s",
                        attempt, GEMINI_MAX_RETRIES + 1, exc)
            if attempt <= GEMINI_MAX_RETRIES:
                time.sleep(1.5 * attempt)
                continue
            if isinstance(exc, (ConnectionError, OSError, TimeoutError)) and not isinstance(exc, APIError):
                raise RuntimeError(
                    "اتصال به سرور Gemini برقرار نشد (خطای شبکه: %s). اگر از ایران وصل می‌شوید، "
                    "معمولاً این یعنی VPN لازم است یا اتصال VPN ناپایدار است؛ یا آنتی‌ویروس/فایروال "
                    "ارتباط را قطع کرده. با VPN پایدار دوباره امتحان کن." % exc
                )
            raise
    raise last_error or RuntimeError("خطای نامشخص در ارتباط با مدل.")


def parse_json(text):
    """چند مرحله‌ای: خام، بدون فنس مارک‌داون، استخراج با regex."""
    if not text:
        return None
    candidates = [text.strip()]

    cleaned = re.sub(r"^```[a-zA-Z]*", "", text.strip())
    cleaned = re.sub(r"```$", "", cleaned).strip()
    candidates.append(cleaned)

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        candidates.append(match.group(0))

    for item in candidates:
        try:
            data = json.loads(item)
            if isinstance(data, dict):
                return data
        except Exception:
            continue

    log.error("پاسخ مدل قابل تجزیه به JSON نبود؛ ۵۰۰ کاراکتر ابتدایی: %s", text[:500])
    return None


def normalize(data):
    def txt(value):
        return "" if value is None else str(value).strip()

    def listify(value, limit=5):
        if value is None:
            return []
        if isinstance(value, (str, int, float)):
            value = [value]
        out = []
        for item in value:
            s = txt(item)
            if s:
                out.append(s)
        return out[:limit]

    signal = txt(data.get("signal"))
    mapping = {"buy": "خرید", "sell": "فروش", "hold": "نگهداری"}
    signal = mapping.get(signal.lower(), signal)
    if signal not in VALID_SIGNALS:
        signal = "نامشخص"

    try:
        confidence = int(float(data.get("confidence") or 0))
    except Exception:
        confidence = 0
    confidence = max(0, min(100, confidence))

    return {
        "signal": signal,
        "confidence": confidence,
        "entry": txt(data.get("entry")) or "—",
        "stop_loss": txt(data.get("stop_loss")) or "—",
        "targets": listify(data.get("targets"), 4),
        "trend": txt(data.get("trend")) or "—",
        "indicators": txt(data.get("indicators")) or "—",
        "reasons": listify(data.get("reasons")),
        "risks": listify(data.get("risks")),
        "summary": txt(data.get("summary")) or "جمع‌بندی ارائه نشد.",
    }


# ----------------------------------------------------------------------- مسیرها
@app.get("/api/health")
def health():
    return jsonify(ok=True, status="up", model=MODEL_NAME, time=datetime.now(timezone.utc).isoformat())


@app.route("/")
def index():
    return render_template(
        "index.html",
        timeframes=TIMEFRAMES,
        max_images=MAX_IMAGES,
        max_mb=MAX_UPLOAD_MB,
        model_name=MODEL_NAME,
    )


@app.post("/analyze")
@require_api_key
@rate_limited()
def analyze():
    symbol = (request.form.get("symbol") or "").strip()[:64]
    timeframe = (request.form.get("timeframe") or TIMEFRAMES[0]).strip()
    notes = (request.form.get("notes") or "").strip()[:2000]

    if not symbol:
        return jsonify(ok=False, error="نام نماد را وارد کنید."), 400
    if timeframe not in TIMEFRAMES:
        timeframe = TIMEFRAMES[0]

    files = request.files.getlist("images")
    if len(files) > MAX_IMAGES:
        log.info("تعداد تصاویر ارسالی (%d) بیش از حد مجاز (%d) بود؛ مازاد نادیده گرفته شد.",
                  len(files), MAX_IMAGES)

    images = []
    for file in files[:MAX_IMAGES]:
        if not file or not file.filename:
            continue
        mime, data, err = validate_and_prepare_image(file)
        if err:
            return jsonify(ok=False, error=err), 400
        images.append((mime, data))

    manual_file = request.files.get("market_data_file")
    has_manual_file = bool(manual_file and manual_file.filename)

    if not images and not has_manual_file:
        return jsonify(
            ok=False,
            error="حداقل یکی از این دو مورد لازم است: فایل تکنیکال (اکسل/CSV) یا تصویر تابلو.",
        ), 400

    market_note = None
    indicator_text = None
    if has_manual_file:
        try:
            market_df = load_ohlcv_from_file(manual_file)
            indicator_text = build_indicator_summary(market_df)
            log.info("دیتای عددی از فایل دستی آپلودشده (%s) خوانده شد.", manual_file.filename)
        except MarketDataError as exc:
            market_note = "خطا در فایل دستی آپلودشده: %s" % exc
            log.warning(market_note)
    else:
        market_note = (
            "فایل تکنیکال (اکسل/CSV) آپلود نشده؛ اپ به‌خاطر مشکلات اتصال اینترنت در ایران "
            "دیگر خودکار سراغ اینترنت/TSETMC نمی‌رود. تحلیل فقط بر مبنای تصویر(های) تابلو انجام می‌شود."
        )
        log.info(market_note)

    prompt = build_prompt(symbol, timeframe, notes, len(images), indicator_text)

    try:
        raw = call_gemini(prompt, images)
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        log.exception("خطا در ارتباط با Gemini")
        return jsonify(ok=False, error="خطا در ارتباط با مدل: %s" % exc), 502

    parsed = parse_json(raw)
    if parsed is None:
        return jsonify(ok=False, error="پاسخ مدل قابل خواندن نبود. دوباره تلاش کن."), 502

    result = normalize(parsed)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO analyses (created_at, symbol, timeframe, signal, confidence,"
            " entry, stop_loss, targets, summary, reasons, risks, raw_json, image_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now, symbol, timeframe, result["signal"], result["confidence"],
                result["entry"], result["stop_loss"],
                json.dumps(result["targets"], ensure_ascii=False),
                result["summary"],
                json.dumps(result["reasons"], ensure_ascii=False),
                json.dumps(result["risks"], ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                len(images),
            ),
        )
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()

    result["id"] = new_id
    result["created_at"] = now
    result["symbol"] = symbol
    result["timeframe"] = timeframe
    result["image_count"] = len(images)
    result["used_real_data"] = bool(indicator_text)
    result["market_note"] = market_note
    return jsonify(ok=True, data=result)


@app.get("/api/history")
@require_api_key
def api_history():
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = min(100, max(1, int(request.args.get("page_size", 20))))
    except ValueError:
        return jsonify(ok=False, error="پارامتر صفحه‌بندی نامعتبر است."), 400
    offset = (page - 1) * page_size

    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM analyses").fetchone()["c"]
        rows = conn.execute(
            "SELECT id, created_at, symbol, timeframe, signal, confidence, image_count"
            " FROM analyses ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
    finally:
        conn.close()
    return jsonify(ok=True, data=[dict(row) for row in rows], page=page, page_size=page_size, total=total)


@app.get("/api/analysis/<int:item_id>")
@require_api_key
def api_analysis(item_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT raw_json, symbol, timeframe, created_at FROM analyses WHERE id=?",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify(ok=False, error="یافت نشد."), 404

    data = parse_json(row["raw_json"]) or {}
    data.update(
        id=item_id,
        symbol=row["symbol"],
        timeframe=row["timeframe"],
        created_at=row["created_at"],
    )
    return jsonify(ok=True, data=data)


@app.delete("/api/analysis/<int:item_id>")
@require_api_key
def api_delete(item_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM analyses WHERE id=?", (item_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify(ok=True)


# --------------------------------------------------------------------- خروجی PDF
def shape_fa(text):
    """قبل از reshape، متن escape می‌شود چون Paragraph در reportlab زیرمجموعه‌ای از
    تگ‌های XML/HTML (مثل <b>, <font>) را تفسیر می‌کند؛ بدون escape، یک '<' یا '&'
    داخل متن (چه از کاربر، چه از خروجی مدل) می‌تواند ساخت PDF را بشکند یا فرمت
    ناخواسته ایجاد کند."""
    safe = xml_escape(str(text))
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(safe))
    except Exception:
        return safe


def register_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for name in ("Vazirmatn-Regular.ttf", "Vazir.ttf", "Sahel.ttf", "IRANSans.ttf"):
        path = os.path.join(FONT_DIR, name)
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("BRSFont", path))
                return "BRSFont"
            except Exception:
                continue
    log.warning("فونت فارسی هنگام ساخت PDF یافت نشد؛ از Helvetica استفاده می‌شود.")
    return "Helvetica"


@app.get("/pdf/<int:item_id>")
@require_api_key
def export_pdf(item_id):
    try:
        from reportlab.lib.enums import TA_RIGHT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except Exception:
        return "reportlab نصب نیست. دستور: pip install reportlab", 500

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT raw_json, symbol, timeframe, created_at FROM analyses WHERE id=?",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return "رکورد یافت نشد.", 404

    data = normalize(parse_json(row["raw_json"]) or {})
    font = register_font()

    title = ParagraphStyle("t", fontName=font, fontSize=16, alignment=TA_RIGHT,
                           leading=24, spaceAfter=8)
    body = ParagraphStyle("b", fontName=font, fontSize=11, alignment=TA_RIGHT,
                          leading=20)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=18 * mm,
                            leftMargin=18 * mm, topMargin=18 * mm,
                            bottomMargin=18 * mm)

    story = [Paragraph(shape_fa("گزارش تحلیل نماد " + row["symbol"]), title)]
    rows = [
        ("تاریخ", row["created_at"]),
        ("تایم‌فریم", row["timeframe"]),
        ("سیگنال", data["signal"]),
        ("درجه اطمینان", str(data["confidence"]) + " ٪"),
        ("روند", data["trend"]),
        ("محدوده ورود", data["entry"]),
        ("حد ضرر", data["stop_loss"]),
        ("اهداف", "، ".join(data["targets"]) or "—"),
        ("اندیکاتورها", data["indicators"]),
    ]
    for label, value in rows:
        story.append(Paragraph(shape_fa(label + ": " + str(value)), body))

    story.append(Spacer(1, 8))
    story.append(Paragraph(shape_fa("دلایل:"), title))
    for item in data["reasons"] or ["—"]:
        story.append(Paragraph(shape_fa("• " + item), body))

    story.append(Spacer(1, 8))
    story.append(Paragraph(shape_fa("ریسک‌ها:"), title))
    for item in data["risks"] or ["—"]:
        story.append(Paragraph(shape_fa("• " + item), body))

    story.append(Spacer(1, 8))
    story.append(Paragraph(shape_fa("جمع‌بندی:"), title))
    story.append(Paragraph(shape_fa(data["summary"]), body))

    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name="analysis_%d.pdf" % item_id)


@app.errorhandler(413)
def too_large(_):
    return jsonify(ok=False, error="حجم فایل‌ها بیش از %d مگابایت است." % MAX_UPLOAD_MB), 413


@app.errorhandler(429)
def too_many(_):
    return jsonify(ok=False, error="تعداد درخواست‌ها بیش از حد مجاز است؛ کمی صبر کن."), 429


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)