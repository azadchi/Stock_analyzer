"""
اپلیکیشن تحلیل بورس - مرحله ۱: تحلیل تک‌نماد با Gemini API
استفاده: python analyze.py مسیر_عکس_چارت.jpg
"""

import os
import sys
from google import genai
from PIL import Image

PROMPT = """
تو نقش یک مدیر سرمایه‌گذاری حرفه‌ای صندوق بورس ایران رو داری. این عکس یک چارت تکنیکال یک سهم بورس ایرانه.

لطفاً:
1. تمام اعداد و اندیکاتورهای قابل مشاهده در تصویر رو دقیق استخراج کن (قیمت فعلی، Ichimoku، RSI، MACD، MFI، OBV، ATR، حجم)
2. وضعیت روند، حمایت و مقاومت رو مشخص کن
3. برای هر اندیکاتور یک تحلیل کوتاه بده
4. نقطه ورود پیشنهادی، حد ضرر (بر اساس ATR)، و اهداف قیمتی رو بگو
5. جمع‌بندی نهایی رو با یکی از این سه حالت بده: 🟢 خرید | 🟡 صبر و بررسی بیشتر | 🔴 فروش
6. اگر داده‌ی کافی برای تصمیم قطعی نیست، صریح بگو

توجه: این فقط جنبه‌ی آموزشی داره و توصیه مالی نیست.
"""

MODEL = "gemini-3.1-flash-lite"


def analyze_chart(image_path: str) -> str:
    if not os.environ.get("GEMINI_API_KEY"):
        raise EnvironmentError(
            "متغیر GEMINI_API_KEY تنظیم نشده. اول کلید API رو ست کن."
        )

    if not os.path.exists(image_path):
        raise FileNotFoundError(f"فایل پیدا نشد: {image_path}")

    client = genai.Client()
    image = Image.open(image_path)

    response = client.models.generate_content(
        model=MODEL,
        contents=[PROMPT, image],
    )
    return response.text


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("استفاده: python analyze.py مسیر_عکس_چارت.jpg")
        sys.exit(1)

    image_path = sys.argv[1]
    print(f"در حال تحلیل {image_path} ...\n")

    try:
        result = analyze_chart(image_path)
        print(result)
    except Exception as e:
        print(f"خطا: {e}")
