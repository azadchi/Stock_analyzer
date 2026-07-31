# -*- coding: utf-8 -*-
"""brs.py — هسته برنامه «تحلیل‌ساز بورس» (نسخه Vision-Only).

معماری: تحلیل فقط بر مبنای تصاویر آپلودی (نمودار + تابلو TSETMC) انجام می‌شود.
هیچ درخواست اینترنتی به TSETMC ارسال نمی‌شود.

نصب وابستگی‌ها:
    pip install flask google-genai pillow reportlab arabic-reshaper python-bidi python-dotenv
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "analysis.db")
FONT_DIR = os.path.join(BASE_DIR, "fonts")
LOG_PATH = os.path.join(BASE_DIR, "brs.log")
ENV_PATH = os.path.join(BASE_DIR, ".env")

MAX_IMAGES = 6
MAX_UPLOAD_MB = 16
MAX_IMAGE_DIMENSION = 1600
IMAGE_JPEG_QUALITY = 85
ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp"}
TIMEFRAMES = ["روزانه", "هفتگی", "ماهانه", "60 دقیقه", "15 دقیقه"]
VALID_SIGNALS = {"خرید", "فروش", "نگهداری", "نامشخص"}

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

# ---------------------------------------------------- کلید امنیتی ماندگار
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
except ImportError:
    log.warning("python-dotenv نصب نیست؛ فایل .env خوانده نمی‌شود. نصب: pip install python-dotenv")

def get_or_create_secret_key():
    """کلید را از env می‌خواند؛ اگر نبود یک‌بار می‌سازد و در .env ذخیره می‌کند
    تا با هر ری‌استارت سرور عوض نشود."""
    key = os.environ.get("BRS_SECRET_KEY", "").strip()
    if key:
        return key
    key = secrets.token_hex(32)
    try:
        with open(ENV_PATH, "a", encoding="utf-8") as f:
            f.write("\nBRS_SECRET_KEY=%s\n" % key)
        log.info("BRS_SECRET_KEY جدید ساخته و در .env ذخیره شد (ماندگار).")
    except OSError as e:
        log.warning("ذخیره کلید در .env ممکن نشد (%s)؛ کلید فقط برای این اجرا معتبر است.", e)
    return key

app = Flask(__name__)
app.secret_key = get_or_create_secret_key()

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
try:
    app.json.ensure_ascii = False
except Exception:
    app.config["JSON_AS_ASCII"] = False

API_KEY = os.environ.get("BRS_API_KEY", "").strip()
if not API_KEY:
    log.warning("BRS_API_KEY تنظیم نشده؛ سرور بدون احراز هویت اجرا می‌شود (فقط برای استفاده لوکال).")


# -------------------------------------------------------------- احراز هویت ساده
def require_api_key(view):
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
        log.warning("هیچ فونت فارسی در %s پیدا نشد؛ PDF فارسی درست نمایش داده نمی‌شود. "
                    "یکی از این‌ها را در پوشه fonts بگذار: %s", FONT_DIR, ", ".join(known))
    else:
        log.info("فونت فارسی یافت شده برای PDF: %s", found[0])


init_db()
check_fonts()


# ------------------------------------------------------------------- دستور مدل
def build_prompt(symbol, timeframe, notes, n_images):
    lines = [
        "تو یک تحلیل‌گر ارشد و محافظه‌کار بازار سهام ایران هستی.",
        "وظیفه: تحلیل تکنیکال نماد زیر فقط بر مبنای تصاویر پیوست (نمودار و تابلو TSETMC).",
        “”,

"نماد: " + symbol,

"تایم‌فریم: " + timeframe,

"تعداد تصویر پیوست: " + str(n_images),

]

if notes:

lines += ["یادداشت کاربر: " + notes]

lines += [

“”,

“قواعد الزامی:”,

“۱) اگر تصویر ناخوانا یا داده کافی نبود، signal را «نامشخص» بگذار.”,

“۲) عدد از خودت نساز؛ فقط سطوح قابل مشاهده در تصاویر را گزارش کن.”,

“۳) confidence عددی بین ۰ تا ۱۰۰.”,

“۴) کل خروجی فارسی باشد.”,

“”,

“فقط و فقط یک شیء JSON با این کلیدها برگردان (بدون توضیح اضافه):”,

‘{“signal”: “خرید|فروش|نگهداری|نامشخص”, “confidence”: 0, “entry”: “…”, “stop_loss”: “…”, “targets”: [“…”, “…”], “trend”: “…”, “indicators”: “…”, “reasons”: [“…”, “…”], “risks”: [“…”], “summary”: “…”}’

]

return “\n”.join(lines)

def validate_and_prepare_image(file_storage):

from PIL import Image, UnidentifiedImageError

raw = file_storage.read()

if not raw:

return None, None, “فایل «%s» خالی است.” % file_storage.filename

try:

img = Image.open(io.BytesIO(raw))

img.verify()

img = Image.open(io.BytesIO(raw))

img.load()

except (UnidentifiedImageError, OSError, ValueError):

return None, None, “فایل «%s» یک تصویر معتبر نیست.” % file_storage.filename

fmt = (img.format or “”).upper()

fmt_to_mime = {“PNG”: “image/png”, “JPEG”: “image/jpeg”, “JPG”: “image/jpeg”, “WEBP”: “image/webp”}

mime = fmt_to_mime.get(fmt)

if mime not in ALLOWED_MIME:

return None, None, “فرمت فایل «%s» (%s) پشتیبانی نمی‌شود.” % (file_storage.filename, fmt)

if max(img.size) > MAX_IMAGE_DIMENSION:

img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)

buf = io.BytesIO()

if mime == “image/png”:

img.save(buf, format=“PNG”, optimize=True)

else:

if img.mode in (“RGBA”, “P”):

img = img.convert(“RGB”)

img.save(buf, format=“JPEG”, quality=IMAGE_JPEG_QUALITY, optimize=True)

mime = “image/jpeg”

return mime, buf.getvalue(), None

return mime, raw, None

def call_gemini(prompt, images):

api_key = os.environ.get(“GEMINI_API_KEY”, “”).strip()

if not api_key:

raise RuntimeError(“GEMINI_API_KEY تنظیم نشده است.”)

from google import genai

from google.genai import types

from google.genai.errors import APIError

client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_SEC * 1000))

parts = [types.Part.from_text(text=prompt)]

for mime, data in images:

parts.append(types.Part.from_bytes(data=data, mime_type=mime))

config = types.GenerateContentConfig(temperature=0.25, max_output_tokens=2048, response_mime_type=“application/json”)

last_error = None

for attempt in range(1, GEMINI_MAX_RETRIES + 2):

try:

resp = client.models.generate_content(model=MODEL_NAME, contents=[types.Content(role=“user”, parts=parts)], config=config)

text = (getattr(resp, “text”, “”) or “”).strip()

if text: return text

raise RuntimeError(“پاسخی از مدل دریافت نشد.”)

except (APIError, ConnectionError, OSError, TimeoutError) as exc:

last_error = exc

log.warning(“تلاش مجدد (%d): %s”, attempt, exc)

if attempt <= GEMINI_MAX_RETRIES:

time.sleep(1.5 * attempt)

continue

raise

raise last_error or RuntimeError(“خطای ناشناخته در ارتباط با مدل.”)

def parse_json(text):

if not text: return None

cleaned = re.sub(r"^

[a-zA-Z]*",
    cleaned = re.sub(r"
```$", "", cleaned).strip()
match = re.search(r"\{.*\}", cleaned, re.DOTALL)
item = match.group(0) if match else cleaned
try:
return json.loads(item)
except json.JSONDecodeError:
log.debug("خطا در پارس JSON: %s", item)
return None


# ----------------------------------------------------------- روت‌ها
@app.route("/", methods=["GET"])
def index(): return render_template("index.html")

@app.route("/analyze", methods=["POST"])
@require_api_key
@rate_limited()
def analyze():
symbol = request.form.get("symbol", "").strip()
timeframe = request.form.get("timeframe", "").strip()
notes = request.form.get("notes", "").strip()

if not symbol or timeframe not in TIMEFRAMES:
return jsonify(ok=False, error="داده‌های ورودی ناقص است."), 400
images_uploaded = request.files.getlist("images")
if not images_uploaded:
return jsonify(ok=False, error="حداقل یک تصویر آپلود کن."), 400

processed_images = []
for i, file_storage in enumerate(images_uploaded):
mime, data, err = validate_and_prepare_image(file_storage)
if err: return jsonify(ok=False, error="خطا در تصویر %d: %s" % (i + 1, err)), 400
processed_images.append((mime, data))

prompt = build_prompt(symbol, timeframe, notes, len(processed_images))
try:
raw_json_str = call_gemini(prompt, processed_images)
data = parse_json(raw_json_str)
if not data: raise RuntimeError("خروجی مدل معتبر نیست.")
except Exception as e:
log.error("خطا: %s", e)
return jsonify(ok=False, error=str(e)), 500

created_at = datetime.now(timezone.utc).isoformat()
conn = get_db()
try:
cursor = conn.cursor()
cursor.execute("INSERT INTO analyses (created_at, symbol, timeframe, signal, confidence, entry, stop_loss, targets, summary, reasons, risks, raw_json, image_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
(created_at, symbol, timeframe, data.get("signal", "نامشخص"), int(data.get("confidence", 0)), 
data.get("entry", ""), data.get("stop_loss", ""), json.dumps(data.get("targets", [])), 
data.get("summary", ""), json.dumps(data.get("reasons", [])), json.dumps(data.get("risks", [])), 
raw_json_str, len(processed_images)))
conn.commit()
return jsonify(ok=True, analysis_id=cursor.lastrowid, data=data), 200
finally:
conn.close()

# (سایر توابع مثل history و pdf هم دقیقاً مانند قبل هستند، فقط مطمئن شو از importهای درستی استفاده میکنی)

if __name__ == "__main__":
app.run(debug=True)