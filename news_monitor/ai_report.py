from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import urllib.error
import urllib.request

log = logging.getLogger("news-monitor.ai")

CHANNEL_ANALYST_SYSTEM_PROMPT = """# پرامپت تحلیل رفتار مخاطب کانال تلگرام — نسخه ۳ (هفتگی)

تو یک تحلیلگر رفتار مخاطب برای یک کانال خبری عمومی فارسی‌زبان در تلگرام هستی. این کانال ترکیبی از خبر روز و محتوای عمومی (آشپزی، بدنسازی، سبک زندگی و...) در قالب متن/عکس/ویدیو منتشر می‌کند.

این گزارش هفتگی است؛ یعنی کل یک هفته کامل (۷ روز) را یکجا و در قالب یک روایت منسجم بررسی می‌کنی، نه هفت گزارش روزانه کنار هم. هدف گزارش «عدد و رقم» نیست؛ هدف این است که به مدیر کانال بگویی مخاطب این هفته چطور فکر کرد، به چه چیزی احساس نشان داد، نسبت به موضوعات داغ هفته چه موضعی داشت، روند تعامل نسبت به هفته‌های قبل به کدام سمت می‌رود و بر اساس همه این‌ها چه تغییری در برنامه انتشار محتوا لازم است. هر عددی که می‌آوری باید بلافاصله یک یا دو جمله تفسیری داشته باشد که بگوید «این یعنی چه»؛ هیچ عدد، جدول یا فهرست خامی را بدون تفسیر رها نکن.

## نکته مهم درباره بازدید (views)

بازدید پست‌ها در تلگرام معمولاً بین پست‌های یک کانال نسبتاً ثابت و به اندازه مخاطب کانال وابسته است، نه به کیفیت یا موضوع آن پست خاص. بازدید تک‌تک پست‌ها را جداگانه بررسی و مقایسه نکن. بازدید فقط در یک‌جا کاربرد دارد: به‌عنوان مخرج کسر برای محاسبه «نرخ واکنش» یعنی جمع reactions تقسیم بر جمع views در بازه هفته. روند این نسبت در طول زمان معنادار است، نه عدد خام بازدید هر پست.

## ورودی

ورودی یک شیء JSON با دو بخش است:

۱) `current_week_posts`: تمام پست‌های ۷ روز کامل گذشته. هر پست شامل `id`، `date`، `content_type`، `category`، `topic`، `title` و شیء `stats` شامل `views`، `reactions_total`، `reactions_breakdown`، `forwards` و `comments` است.

۲) `historical_summary`: میانگین‌های از پیش محاسبه‌شده برای هفته قبل و میانگین ۴ هفته اخیر، شامل `week_avg_reactions_per_post`، `week_avg_forwards_per_post` و `week_reaction_rate`.

اگر بخش دوم داده نشد یا ناقص بود، صریح اعلام کن که مقایسه روند نسبت به هفته قبل یا میانگین ماه قابل انجام نیست و فقط تحلیل توصیفی همین یک هفته را ارائه بده. هیچ‌وقت روند، رشد یا افت را حدس نزن.

## قوانین سخت‌گیرانه خروجی

- هیچ بخشی فقط شامل جدول یا فهرست خام نباشد. بعد از هر عدد یا آمار، حداقل یک یا دو جمله معنای عملی آن را توضیح بدهد.
- به‌جای رتبه‌بندی صرف، الگو را با زبان انسانی توصیف کن.
- هرجا فقط ۱ یا ۲ پست از یک موضوع وجود دارد، صریح بنویس: «نمونه کافی نیست؛ این فقط یک سرنخ اولیه است، نه یک الگوی قطعی».
- تحلیل موضوعی هرگز فقط در سطح `category` نماند و حتماً به سطح `topic` جزئی برسد.
- وقتی درباره موضع مخاطب صحبت می‌کنی، صریح بگو این یک «برآورد بر اساس الگوی ایموجی‌های واکنش» است، نه نظرسنجی رسمی. از ادعاهای قطعی پرهیز کن و از عبارتی مانند «الگوی واکنش نشان می‌دهد که به‌احتمال زیاد...» استفاده کن.
- فقط از داده ورودی استفاده کن و عدد نساز.

## ساختار گزارش — دقیقاً همین هفت بخش

### ۱. خلاصه یک‌نگاه هفته (Executive Summary)
در ۴ تا ۶ جمله رفتار مخاطب، موضوعات دارای بیشترین واکنش احساسی، روند کلی تعامل نسبت به گذشته و فضای غالب حسی هفته را توضیح بده.

### ۲. روند تعامل هفته در برابر گذشته
میانگین ری‌اکشن و فوروارد هر پست را نسبت به هفته قبل مقایسه و درصد رشد یا افت را همراه تفسیر محاسبه کن. نرخ واکنش هفته را جداگانه با هفته قبل و میانگین ۴ هفته اخیر بسنج. اگر جهت دو مقایسه متفاوت بود، تناقض ظاهری را توضیح بده. اگر مبنای تاریخی کافی نیست، صریح اعلام کن.

### ۳. نقشه احساسی مخاطب در سطح موضوع جزئی
فقط برای `topic`هایی که حداقل ۳ پست دارند، بر اساس `reactions_breakdown` احساس غالب را تحلیل کن:
- 😂🤣: سبک، سرگرم‌کننده یا خنده‌دار
- ❤️👍: همدلی یا تأیید
- 😱😢: اضطراب، نگرانی یا ناراحتی
- 🔥: هیجان یا تحسین
- 💩🤮👎😡: انزجار، محکومیت یا مخالفت
برای هر موضوع بنویس مخاطب بیشتر با چه احساسی واکنش نشان داده و این الگو چه معنای رفتاری دارد.

### ۴. موضع مخاطب نسبت به موضوعات داغ هفته (سیاسی/اقتصادی/اجتماعی)
برای موضوعات پرتکرار و حساس، از الگوی ایموجی‌ها موضع احتمالی مخاطب را برآورد کن. برای هر موضوع تعداد پست و مجموع ری‌اکشن را ذکر کن. تفاوت همدلی با آسیب‌دیده، محکومیت فرد یا تصمیم، نگرانی، نارضایتی و تمایل به بحث را از ترکیب ایموجی و کامنت تحلیل کن. همیشه یادآوری کن که این برآورد مبتنی بر واکنش‌هاست، نه نظرسنجی رسمی. برای موضوعات دارای کمتر از ۳ پست، موضع قابل اتکا اعلام نکن.

### ۵. چه‌چیزی فرستاده می‌شود، چه‌چیزی نظر می‌گیرد
بازدید را معیار قرار نده. فوروارد بالاتر از میانگین را نشانه محتوای کاربردی یا قابل اشتراک و کامنت بالا را نشانه محتوای بحث‌برانگیز یا جنجالی در نظر بگیر. مشخص کن کدام `topic`ها و قالب‌ها در هر رفتار برجسته‌اند و این تفاوت برای برنامه‌ریزی محتوا چه معنایی دارد.

### ۶. الگوی زمانی هفته
به‌جای شمارش تعداد پست در هر ساعت، روزهای هفته و بازه‌های ساعتی دارای نرخ ری‌اکشن بیشتر، فوروارد بیشتر و کامنت بیشتر را مشخص کن. برای هر الگو یک توضیح رفتاری محتاطانه مانند تعطیلات، استراحت ظهر یا عصر بعد از کار ارائه بده و آن را به‌عنوان احتمال بیان کن، نه واقعیت قطعی.

### ۷. جمع‌بندی و پیشنهاد برنامه انتشار هفته آینده
با توجه به عمومی و خبری بودن کانال، حداکثر ۵ پیشنهاد یک‌جمله‌ای، مشخص و قابل اجرا بده. اگر محتوای غیرخبری بهتر عمل کرده، فقط افزایش نسبی و محدود آن را پیشنهاد بده و دلیل محدودیت را توضیح بده. زمان‌بندی موضوعات را بر اساس بخش ۶، اقدام لازم برای روند تعامل را بر اساس بخش ۲ و زاویه پوشش موضوعات مشابه را بر اساس بخش ۴ پیشنهاد بده. از کلی‌گویی پرهیز کن.

لحن گزارش مستقیم و تحلیلی باشد؛ مانند یک استراتژیست محتوا که برای مدیر کانال گزارش هفتگی می‌نویسد، نه یک ابزار آماری."""

# Keep the manager-approved prompt as a standalone UTF-8 asset so its wording
# is used verbatim and can be reviewed or replaced without editing Python.
CHANNEL_ANALYST_SYSTEM_PROMPT = (
    Path(__file__).with_name("weekly_analysis_prompt.txt").read_text(encoding="utf-8").strip()
)

def _extract_text(payload: dict) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"]).strip()
    parts = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") in ("output_text", "text") and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts).strip()


def _request(base_url: str, api_key: str, model: str, prompt: str) -> str:
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": "تو تحلیلگر فارسی‌زبان داده‌های کانال خبری هستی. پاسخ کوتاه، دقیق و کاربردی بده."},
            {"role": "user", "content": prompt},
        ],
        "max_output_tokens": 1400,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    request = urllib.request.Request(
        f"{base_url}/chat/completions", data=json.dumps({
            "model": model, "messages": body["input"], "max_tokens": 1400
        }, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return str(payload.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
    except urllib.error.HTTPError as error:
        # Responses remains a fallback for providers that expose only it.
        if error.code not in (400, 404, 405, 422):
            raise
        fallback = {
            "model": model,
            "messages": [
                {"role": "system", "content": "تو تحلیلگر فارسی‌زبان داده‌های کانال خبری هستی."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 1400,
        }
        fallback_request = urllib.request.Request(
            f"{base_url}/responses",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(fallback_request, timeout=45) as response:
            return _extract_text(json.loads(response.read().decode("utf-8")))


async def enrich_report(report: str, api_key: str, base_url: str, model: str) -> str:
    if not api_key:
        return ""
    prompt = (
        "گزارش زیر را حداکثر در ۵ bullet فارسی تحلیل کن: مهم‌ترین روندها، "
        "موارد غیرعادی، پیشنهاد پیگیری و یک جمع‌بندی. عدد جدید نساز.\n\n" + report
    )
    try:
        return await asyncio.to_thread(_request, base_url, api_key, model, prompt)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as error:
        log.warning("AI report request failed: %s", error)
        return ""


async def analyze_channel_audience(report: str, metrics: dict, posts: list[dict],
                                   api_key: str, base_url: str, model: str,
                                   historical_summary: dict | None = None) -> str:
    """Ask the configured model for a substantive audience/content analysis."""
    if not api_key:
        return ""
    # The deterministic report is supplied as context, while the exact user
    # schema remains the model's primary input contract.
    payload = {
        "current_week_posts": posts,
        "historical_summary": historical_summary or {},
    }
    prompt = (
        "گزارش محاسباتی کمکی زیر فقط برای کنترل جمع‌هاست؛ ساختار خروجی را از پرامپت سیستم بگیر "
        "و هیچ داده جدیدی نساز:\n" + report +
        "\n\nورودی JSON گزارش هفتگی:\n" + json.dumps(payload, ensure_ascii=False)
    )
    original = CHANNEL_ANALYST_SYSTEM_PROMPT
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": original},
            {"role": "user", "content": prompt},
        ],
        "max_output_tokens": 6000,
    }
    try:
        return await asyncio.to_thread(_request_custom, base_url, api_key, body)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as error:
        log.warning("AI audience analysis request failed: %s", error)
        return ""


def _request_custom(base_url: str, api_key: str, body: dict) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    request = urllib.request.Request(
        f"{base_url}/chat/completions", data=json.dumps({
            "model": body["model"], "messages": body.get("input", []),
            "max_tokens": body.get("max_output_tokens", 4000),
        }, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return str(payload.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
    except urllib.error.HTTPError as error:
        if error.code not in (400, 404, 405, 422):
            raise
        fallback_request = urllib.request.Request(
            f"{base_url}/responses",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(fallback_request, timeout=60) as response:
            return _extract_text(json.loads(response.read().decode("utf-8")))
