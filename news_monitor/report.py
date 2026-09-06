from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
from math import exp
import re
import statistics

PERSIAN_DIGITS = str.maketrans("0123456789.-+", "۰۱۲۳۴۵۶۷۸۹٫−+")


def fa(value) -> str:
    return str(value).translate(PERSIAN_DIGITS)


def gregorian_to_jalali(year: int, month: int, day: int) -> tuple[int, int, int]:
    days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gy, gm, gd = year - 1600, month - 1, day - 1
    g_day_no = 365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400
    g_day_no += sum(days_in_month[:gm])
    if gm > 1 and ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0):
        g_day_no += 1
    j_day_no = g_day_no + gd - 79
    j_np, j_day_no = divmod(j_day_no, 12053)
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    if j_day_no < 186:
        return jy, 1 + j_day_no // 31, 1 + j_day_no % 31
    return jy, 7 + (j_day_no - 186) // 30, 1 + (j_day_no - 186) % 30


def jalali_date(moment: datetime, tz) -> str:
    local = moment.astimezone(tz)
    year, month, day = gregorian_to_jalali(local.year, local.month, local.day)
    return fa(f"{year:04d}/{month:02d}/{day:02d}")


def clock(value: str | datetime, tz) -> str:
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return fa(moment.astimezone(tz).strftime("%H:%M"))


def clean_headline(text: str, limit: int = 92) -> str:
    compact = " ".join((text or "").split())
    if len(compact) > limit:
        compact = compact[: limit - 1].rstrip() + "…"
    return escape(compact)


def _speed_time_score(delay_minutes: float, time_scale_minutes: int) -> float:
    """Return a smooth time score instead of a hard cut-off at the configured scale.

    The previous linear rule made every delay at or above ``time_scale_minutes``
    equal to zero.  The stored data contains valid stories whose propagation
    continues well beyond that window, so an exponential decay preserves their
    relative signal while still rewarding early publication strongly.
    """
    scale = max(1.0, float(time_scale_minutes))
    delay = max(0.0, float(delay_minutes))
    return 100.0 * exp(-delay / scale)


def _analyze(db, channels, start, end, tz, important_min_channels, miss_grace_minutes,
             speed_min_channels=None, missed_max_publishers=None,
             rank_weight=60, time_cap_minutes=30, confidence_k=5,
             missed_min_publishers=3):
    rows = db.posts_between(start, end)
    grouped = defaultdict(list)
    channel_lookup = {channel.casefold(): channel for channel in channels}
    for row in rows:
        if row["cluster_id"] is not None and row["channel"].casefold() in channel_lookup:
            grouped[row["cluster_id"]].append(row)

    score_totals = defaultdict(float)
    score_samples = defaultdict(int)
    speed_items = []
    missed_items = []
    cutoff = end - timedelta(minutes=miss_grace_minutes)
    total_channels = len(channels)
    # Keep the two classifications adjacent.  The old fallback used
    # ``total_channels // 2 - 1`` which created a silent gap for an odd
    # number of channels (for example, with five channels: speed >= 3 but
    # missed <= 1, so two-channel stories were never reported anywhere).
    speed_min_channels = min(
        total_channels,
        max(2, speed_min_channels or (total_channels // 2 + 1)),
    ) if total_channels else 0
    missed_max_publishers = (
        speed_min_channels - 1
        if missed_max_publishers is None
        else min(speed_min_channels - 1, max(1, missed_max_publishers))
    )
    missed_min_publishers = max(2, min(missed_min_publishers, missed_max_publishers))

    for items in grouped.values():
        first_by_channel = {}
        for item in items:
            first_by_channel.setdefault(item["channel"].casefold(), item)
        ordered = sorted(first_by_channel.values(), key=lambda x: x["published_at"])
        present = len(ordered)
        absent = [channel for channel in channels if channel.casefold() not in first_by_channel]
        missing_count = len(absent)
        if present < 2:
            continue

        # A story is important only after it reaches the configured coverage
        # threshold (normally a strict majority of monitored channels).  The
        # same important-story set drives both speed and missed-news reports:
        # publishers are scored for speed, while absent channels are misses.
        if present >= speed_min_channels:
            first = datetime.fromisoformat(ordered[0]["published_at"])
            scored = []
            time_weight = 100 - rank_weight
            position_by_time = {}
            for index, item in enumerate(ordered):
                position_by_time.setdefault(item["published_at"], []).append(index)
            for index, item in enumerate(ordered):
                moment = datetime.fromisoformat(item["published_at"])
                delay = max(0.0, (moment - first).total_seconds() / 60)
                tied_positions = position_by_time[item["published_at"]]
                rank_position = sum(tied_positions) / len(tied_positions)
                rank_score = 100.0 if present == 1 else 100 * (present - 1 - rank_position) / (present - 1)
                time_score = _speed_time_score(delay, time_cap_minutes)
                point = (rank_weight * rank_score + time_weight * time_score) / 100
                canonical_channel = channel_lookup[item["channel"].casefold()]
                score_totals[canonical_channel] += point
                score_samples[canonical_channel] += 1
                scored.append((item, point, rank_score, time_score, delay))
            speed_items.append({
                "ordered": ordered, "scored": scored, "absent": absent,
            })
            earliest = datetime.fromisoformat(ordered[0]["published_at"])
            if absent and earliest <= cutoff:
                missed_items.append({"sample": ordered[0], "absent": absent, "present": present})

    scores = {}
    averages = {}
    for channel in channels:
        samples = score_samples[channel]
        average = score_totals[channel] / samples if samples else 0.0
        averages[channel] = average
        scores[channel] = average * samples / (samples + max(0, confidence_k)) if samples else 0.0
    return {"rows": rows, "scores": scores, "averages": averages, "samples": score_samples,
            "speed": speed_items, "missed": missed_items}


def _ranking_lines(channels, scores, averages, samples):
    ranking = sorted(channels, key=lambda channel: scores[channel], reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for index, channel in enumerate(ranking):
        marker = medals[index] if index < 3 else f"{fa(index + 1)}."
        lines.append(
            f'{marker} <b>{escape(channel)}</b> — <b>{fa(f"{scores[channel]:.1f}")}</b>'
            f' | میانگین {fa(f"{averages[channel]:.1f}")} | {fa(samples[channel])} خبر'
        )
    return lines


def build_report(db, channels: tuple[str, ...], start: datetime, end: datetime, tz,
                 important_min_channels: int, miss_grace_minutes: int,
                 report_type: str = "speed", speed_min_channels: int | None = None,
                 missed_max_publishers: int | None = None, rank_weight: int = 60,
                 time_cap_minutes: int = 30, confidence_k: int = 5,
                 missed_min_publishers: int = 3) -> str:
    result = _analyze(db, channels, start, end, tz, important_min_channels, miss_grace_minutes,
                      speed_min_channels, missed_max_publishers, rank_weight,
                      time_cap_minutes, confidence_k, missed_min_publishers)
    if report_type == "missed":
        items = []
        for missed in result["missed"]:
            sample = missed["sample"]
            link = f'<a href="{escape(sample["link"])}">مشاهده نمونه خبر</a>' if sample["link"] else ""
            items.append(
                f'🔸 <b>{clean_headline(sample["text"], 80)}</b>\n'
                f'نزدند: {escape("، ".join(missed["absent"]))}\n{link}'
            )
        header = (
            "🔥 <b>گزارش مستقل سوخت خبر</b>\n"
            f"📅 {jalali_date(start, tz)}  |  ⏱ {clock(start, tz)} تا {clock(end, tz)}\n"
            f"معیار خبر مهم: پوشش در حداقل <b>{fa(speed_min_channels)}</b> کانال | "
            f"مهلت <b>{fa(miss_grace_minutes)} دقیقه</b>\n"
            f"تعداد موارد: <b>{fa(len(items))}</b>\n━━━━━━━━━━━━━━"
        )
        return header + "\n\n" + ("\n\n".join(items) if items else "✅ مورد قطعی ثبت نشد.")

    ranking_lines = _ranking_lines(channels, result["scores"], result["averages"], result["samples"])
    return (
        "⚡️ <b>رده‌بندی سرعت انتشار</b>\n"
        f"📅 {jalali_date(start, tz)}  |  ⏱ {clock(start, tz)} تا {clock(end, tz)}\n"
        f"خبرهای محاسبه‌شده: <b>{fa(len(result['speed']))}</b>\n"
        f"فرمول: رتبه <b>{fa(rank_weight)}٪</b> + زمان <b>{fa(100-rank_weight)}٪</b> | "
        f"سقف تأخیر <b>{fa(time_cap_minutes)} دقیقه</b> | اعتبار <b>{fa(confidence_k)}</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        + ("\n\n".join(ranking_lines) if ranking_lines else "داده‌ای موجود نیست.")
    )


def build_missed_overview(db, channels: tuple[str, ...], start: datetime, end: datetime, tz,
                          important_min_channels: int, miss_grace_minutes: int,
                          speed_min_channels: int, missed_max_publishers: int,
                          missed_min_publishers: int = 3) -> tuple[str, list[tuple[str, int]]]:
    result = _analyze(
        db, channels, start, end, tz, important_min_channels, miss_grace_minutes,
        speed_min_channels, missed_max_publishers, missed_min_publishers=missed_min_publishers,
    )
    counts = [
        (channel, sum(channel in item["absent"] for item in result["missed"]))
        for channel in channels
    ]
    counts.sort(key=lambda item: (item[1], item[0].casefold()))
    lines = [f"• {escape(channel)}: <b>{fa(count)}</b> خبر" for channel, count in counts]
    text = (
        "🔥 <b>خلاصهٔ سوخت خبر</b>\n"
        f"📅 {jalali_date(start, tz)}  |  ⏱ {clock(start, tz)} تا {clock(end, tz)}\n"
        f"معیار خبر مهم: پوشش در حداقل <b>{fa(speed_min_channels)}</b> کانال | "
        f"مهلت <b>{fa(miss_grace_minutes)} دقیقه</b>\n"
        f"تعداد کل خبرهای سوخت‌شده: <b>{fa(len(result['missed']))}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<b>تعداد سوخت هر کانال</b>\n"
        + ("\n".join(lines) if lines else "کانالی برای پایش ثبت نشده است.")
        + "\n\nبرای مشاهدهٔ فهرست خبرها، دکمهٔ کانال را انتخاب کنید."
    )
    return text, counts


def build_missed_channel_report(db, channels: tuple[str, ...], channel: str,
                                start: datetime, end: datetime, tz,
                                important_min_channels: int, miss_grace_minutes: int,
                                speed_min_channels: int, missed_max_publishers: int,
                                missed_min_publishers: int = 3) -> str:
    result = _analyze(
        db, channels, start, end, tz, important_min_channels, miss_grace_minutes,
        speed_min_channels, missed_max_publishers, missed_min_publishers=missed_min_publishers,
    )
    items = [item for item in result["missed"] if channel in item["absent"]]
    cards = []
    for number, item in enumerate(items, 1):
        sample = item["sample"]
        link = f'<a href="{escape(sample["link"])}">مشاهده نمونه خبر</a>' if sample["link"] else ""
        cards.append(
            f'<b>{fa(number)}) {clean_headline(sample["text"], 90)}</b>\n'
            f'منتشرکنندگان: <b>{fa(item["present"])}</b> کانال'
            + (f"\n{link}" if link else "")
        )
    header = (
        f"🔥 <b>سوخت خبر {escape(channel)}</b>\n"
        f"📅 {jalali_date(start, tz)}  |  ⏱ {clock(start, tz)} تا {clock(end, tz)}\n"
        f"تعداد: <b>{fa(len(items))}</b> خبر\n━━━━━━━━━━━━━━\n\n"
    )
    return header + ("\n\n".join(cards) if cards else "✅ سوخت خبری برای این کانال ثبت نشده است.")


def build_engagement_report(db, channel: str, start: datetime, end: datetime, tz) -> str:
    rows = list(db.channel_posts_between(channel, start, end))
    reaction_hits = []
    forward_hits = []
    if len(rows) >= 2:
        for row in rows:
            # Leave-one-out medians are stable when one post is an extreme
            # outlier; a leave-one-out average would move the denominator and
            # hide exactly the content this report is meant to surface.
            other_reactions = [r["reaction_count"] for r in rows if r is not row]
            other_forwards = [r["forward_count"] for r in rows if r is not row]
            other_reaction_median = statistics.median(other_reactions)
            other_forward_median = statistics.median(other_forwards)
            if row["reaction_count"] > 0 and row["reaction_count"] >= 2 * other_reaction_median:
                reaction_hits.append((row, other_reaction_median))
            if row["forward_count"] > 0 and row["forward_count"] >= 2 * other_forward_median:
                forward_hits.append((row, other_forward_median))

    def item(row, average, metric):
        value = row[metric]
        ratio = value / average if average > 0 else None
        ratio_text = f" · {fa(f'{ratio:.1f}')} برابر میانگین" if ratio is not None else " · سایر پست‌ها صفر"
        title = clean_headline(row["text"] or "پست رسانه‌ای", 70)
        link = f'<a href="{escape(row["link"])}">مشاهده پست</a>' if row["link"] else ""
        return f'🔸 <b>{title}</b>\nتعداد: <b>{fa(value)}</b>{ratio_text} · {link}'

    reactions = "\n\n".join(item(row, avg, "reaction_count") for row, avg in reaction_hits)
    forwards = "\n\n".join(item(row, avg, "forward_count") for row, avg in forward_hits)
    if len(rows) < 2:
        body = "برای مقایسه حداقل دو پست در این بازه لازم است."
    else:
        body = (
            "❤️ <b>ری‌اکشن حداقل دو برابر میانگین سایر پست‌ها</b>\n"
            + (reactions or "موردی ثبت نشد.")
            + "\n\n↗️ <b>فوروارد حداقل دو برابر میانگین سایر پست‌ها</b>\n"
            + (forwards or "موردی ثبت نشد.")
        )
    return (
        f"📈 <b>گزارش رتبه‌بندی تعامل</b>\n"
        f"کانال: <b>{escape(channel)}</b>\n"
        f"📅 {jalali_date(start, tz)}  |  ⏱ {clock(start, tz)} تا {clock(end, tz)}\n"
        f"پست‌های بررسی‌شده: <b>{fa(len(rows))}</b>\n━━━━━━━━━━━━━━\n\n{body}"
    )


TOPIC_KEYWORDS = {
    "آشپزی": ("آشپزی", "غذا", "دستور پخت", "طرز تهیه", "کیک", "شیرینی", "کوکو", "خورش"),
    "بدنسازی و ورزش": ("بدنسازی", "تمرین", "ورزش", "عضله", "پروتئین", "فیتنس", "کالری", "مربی"),
    "سبک زندگی": ("سبک زندگی", "سلامت", "تغذیه", "خواب", "روانشناسی", "مد", "زیبایی", "خانه"),
    "سیاسی و دولت": ("دولت", "رئیس جمهور", "وزیر", "مجلس", "انتخابات", "سیاسی", "مذاکره", "تحریم"),
    "اقتصاد و بازار": ("دلار", "طلا", "ارز", "بورس", "تورم", "اقتصاد", "قیمت", "بانک", "مسکن"),
    "حوادث و بحران": ("حادثه", "کشته", "زخمی", "آتش", "زلزله", "سیل", "انفجار", "تصادف", "بحران"),
    "بین‌الملل": ("آمریکا", "روسیه", "چین", "اروپا", "اسرائیل", "غزه", "اوکراین", "بین الملل"),
    "ورزش": ("فوتبال", "لیگ", "تیم ملی", "استقلال", "پرسپولیس", "ورزش", "مسابقه"),
    "اجتماعی و زندگی": ("مدرسه", "دانشگاه", "سلامت", "درمان", "هوا", "تعطیلی", "اجتماعی", "مردم"),
    "فناوری و رسانه": ("اینترنت", "فیلترینگ", "هوش مصنوعی", "فضای مجازی", "تلگرام", "فناوری"),
}


def engagement_topic(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").replace("ي", "ی").replace("ك", "ک")).lower()
    scores = {topic: sum(normalized.count(word) for word in words) for topic, words in TOPIC_KEYWORDS.items()}
    best = max(scores, key=scores.get, default="سایر موضوعات")
    return best if scores.get(best, 0) else "سایر موضوعات"


def engagement_rankings(db, channel: str, start: datetime, end: datetime):
    """Rank posts by within-channel percentile, robust to one extreme post."""
    rows = list(db.channel_posts_between(channel, start, end))
    reaction_values = [max(0, int(row["reaction_count"] or 0)) for row in rows]
    forward_values = [max(0, int(row["forward_count"] or 0)) for row in rows]

    def percentile(values, value):
        if not values:
            return 0.0
        lower = sum(item < value for item in values)
        equal = sum(item == value for item in values)
        return 100.0 * (lower + 0.5 * equal) / len(values)

    # Ignore a signal that the channel effectively does not expose. The
    # remaining weights are normalized, so forward-only channels stay fair.
    minimum_nonzero = max(1, round(len(rows) * 0.15))
    reactions_enabled = sum(value > 0 for value in reaction_values) >= minimum_nonzero
    forwards_enabled = sum(value > 0 for value in forward_values) >= minimum_nonzero
    reaction_weight = 0.4 if reactions_enabled else 0.0
    forward_weight = 0.6 if forwards_enabled else 0.0
    weight_total = reaction_weight + forward_weight
    ranked = []
    for row in rows:
        reaction_score = percentile(reaction_values, row["reaction_count"]) if reactions_enabled else 0.0
        forward_score = percentile(forward_values, row["forward_count"]) if forwards_enabled else 0.0
        combined = (
            (reaction_weight * reaction_score + forward_weight * forward_score) / weight_total
            if weight_total else 0.0
        )
        ranked.append({
            "row": row, "reaction_score": reaction_score, "forward_score": forward_score,
            "combined": combined,
            "topic": engagement_topic(row["text"]),
        })
    return ranked


def _engagement_channel_name(channel: str, channel_title: str | None = None) -> str:
    """Return the human-readable channel name used in engagement headings."""
    return (channel_title or "").strip() or channel.lstrip("@").strip() or channel


def build_engagement_leaderboard(db, channel: str, start: datetime, end: datetime, tz,
                                 limit: int = 5, channel_title: str | None = None) -> str:
    ranked = engagement_rankings(db, channel, start, end)
    display_name = escape(_engagement_channel_name(channel, channel_title))

    def card(item, metric: str) -> str:
        row = item["row"]
        link = f'<a href="{escape(row["link"], quote=True)}">مشاهده پست</a>' if row["link"] else ""
        value = row[metric] if metric in ("reaction_count", "forward_count") else item["combined"]
        return (f'• <b>{clean_headline(row["text"] or "پست رسانه‌ای", 74)}</b>\n'
                f'موضوع: {escape(item["topic"])} | مقدار: <b>{fa(round(value, 1))}</b>'
                + (f' | {link}' if link else ''))

    reactions = sorted(ranked, key=lambda x: (x["row"]["reaction_count"], x["row"]["forward_count"]), reverse=True)[:limit]
    forwards = sorted(ranked, key=lambda x: (x["row"]["forward_count"], x["row"]["reaction_count"]), reverse=True)[:limit]
    combined = sorted(ranked, key=lambda x: x["combined"], reverse=True)[:limit]
    empty = "برای این بازه پستی ثبت نشده است."
    return (
        f"🏆 <b>پربازخوردترین محتوای {display_name}</b>\n"
        f"📅 {jalali_date(start, tz)} | ⏱ {clock(start, tz)} تا {clock(end, tz)}\n"
        f"تعداد پست‌ها: <b>{fa(len(ranked))}</b>\n"
        "فرمول امتیاز انتخاب: <b>۴۰٪ صدک ری‌اکشن + ۶۰٪ صدک فوروارد</b> (از ۱۰۰)\n"
        "━━━━━━━━━━━━━━\n\n"
        "❤️ <b>بیشترین ری‌اکشن</b>\n" + ("\n\n".join(card(x, "reaction_count") for x in reactions) or empty)
        + "\n\n📤 <b>بیشترین فوروارد</b>\n" + ("\n\n".join(card(x, "forward_count") for x in forwards) or empty)
        + "\n\n🔥 <b>برترین عملکرد ترکیبی</b>\n" + ("\n\n".join(card(x, "combined") for x in combined) or empty)
    )


def build_engagement_topic_analysis(db, channel: str, start: datetime, end: datetime, tz,
                                    channel_title: str | None = None) -> str:
    ranked = engagement_rankings(db, channel, start, end)
    display_name = escape(_engagement_channel_name(channel, channel_title))
    groups = defaultdict(list)
    for item in ranked:
        groups[item["topic"]].append(item)
    summaries = []
    for topic, items in groups.items():
        count = len(items)
        summaries.append({
            "topic": topic, "count": count,
            "reactions": sum(x["row"]["reaction_count"] for x in items) / count,
            "forwards": sum(x["row"]["forward_count"] for x in items) / count,
            "combined": sum(x["combined"] for x in items) / count,
        })

    def line(label, metric):
        item = max(summaries, key=lambda x: x[metric], default=None)
        if not item:
            return f"{label}: داده کافی نیست"
        return f'{label}: <b>{escape(item["topic"])}</b> (میانگین {fa(f"{item[metric]:.1f}")} در {fa(item["count"])} پست)'

    return (
        f"📊 <b>تحلیل شبانه مخاطب {display_name}</b>\n"
        f"📅 {jalali_date(start, tz)} | تا ساعت {clock(end, tz)}\n"
        f"مبنای تحلیل: <b>{fa(len(ranked))}</b> پست\n━━━━━━━━━━━━━━\n\n"
        + line("❤️ بیشترین تمایل به ری‌اکشن", "reactions") + "\n"
        + line("📤 بیشترین تمایل به فوروارد", "forwards") + "\n"
        + line("🔥 بیشترین اثر هم‌زمان", "combined")
        + "\n\n<i>برای مقایسه عادلانه، میانگین هر پست در هر موضوع محاسبه شده است؛ نه صرفاً مجموع موضوعات پرتعداد.</i>"
    )


def channel_audience_metrics(db, channel: str, start: datetime, end: datetime, tz) -> dict:
    """Return compact, model-friendly statistics for a channel time window."""
    rows = list(db.channel_posts_between(channel, start, end))
    media_labels = {
        "text": "متنی", "image": "تصویری", "video": "ویدیویی",
        "album": "آلبومی", "audio": "صوتی", "document": "فایل", "poll": "نظرسنجی",
    }
    by_media = {}
    for row in rows:
        media = row["media_type"] if "media_type" in row.keys() else "text"
        media = media or "text"
        bucket = by_media.setdefault(media, {"count": 0, "reactions": 0, "forwards": 0,
                                             "views": 0, "replies": 0})
        bucket["count"] += 1
        bucket["reactions"] += max(0, int(row["reaction_count"] or 0))
        bucket["forwards"] += max(0, int(row["forward_count"] or 0))
        bucket["views"] += max(0, int(row["view_count"] or 0)) if "view_count" in row.keys() else 0
        bucket["replies"] += max(0, int(row["reply_count"] or 0)) if "reply_count" in row.keys() else 0
    for media, bucket in by_media.items():
        count = bucket["count"] or 1
        bucket["avg_reactions"] = round(bucket["reactions"] / count, 2)
        bucket["avg_forwards"] = round(bucket["forwards"] / count, 2)
        bucket["avg_views"] = round(bucket["views"] / count, 2)
        bucket["avg_replies"] = round(bucket["replies"] / count, 2)
        bucket["engagement"] = round(
            (bucket["reactions"] + bucket["forwards"] + bucket["replies"]) / count, 2
        )
        bucket["label"] = media_labels.get(media, media)

    topics = defaultdict(lambda: {"count": 0, "reactions": 0, "forwards": 0, "views": 0})
    hours = defaultdict(int)
    for row in rows:
        topic = engagement_topic(row["text"])
        item = topics[topic]
        item["count"] += 1
        item["reactions"] += max(0, int(row["reaction_count"] or 0))
        item["forwards"] += max(0, int(row["forward_count"] or 0))
        item["views"] += max(0, int(row["view_count"] or 0)) if "view_count" in row.keys() else 0
        try:
            moment = datetime.fromisoformat(row["published_at"]).astimezone(tz)
            hours[moment.hour] += 1
        except (TypeError, ValueError):
            pass
    for item in topics.values():
        count = item["count"] or 1
        item["avg_reactions"] = round(item["reactions"] / count, 2)
        item["avg_forwards"] = round(item["forwards"] / count, 2)
        item["avg_views"] = round(item["views"] / count, 2)

    return {
        "channel": channel,
        "post_count": len(rows),
        "total_reactions": sum(max(0, int(r["reaction_count"] or 0)) for r in rows),
        "total_forwards": sum(max(0, int(r["forward_count"] or 0)) for r in rows),
        "total_views": sum(max(0, int(r["view_count"] or 0)) if "view_count" in r.keys() else 0 for r in rows),
        "total_replies": sum(max(0, int(r["reply_count"] or 0)) if "reply_count" in r.keys() else 0 for r in rows),
        "by_media": by_media,
        "by_topic": dict(topics),
        "posting_hours": dict(sorted(hours.items())),
    }


def build_channel_audience_analysis(db, channel: str, start: datetime, end: datetime, tz) -> str:
    metrics = channel_audience_metrics(db, channel, start, end, tz)
    media_lines = []
    for key, item in sorted(metrics["by_media"].items(), key=lambda pair: pair[1]["engagement"], reverse=True):
        media_lines.append(
            f'• <b>{escape(item["label"])}</b>: {fa(item["count"])} پست | '
            f'میانگین واکنش {fa(item["avg_reactions"])} | فوروارد {fa(item["avg_forwards"])} | '
            f'بازدید {fa(item["avg_views"])} | تعامل ترکیبی {fa(item["engagement"])}'
        )
    topic_lines = []
    for topic, item in sorted(metrics["by_topic"].items(), key=lambda pair: pair[1]["avg_reactions"] + pair[1]["avg_forwards"], reverse=True)[:8]:
        topic_lines.append(
            f'• <b>{escape(topic)}</b>: {fa(item["count"])} پست | '
            f'واکنش میانگین {fa(item["avg_reactions"])} | فوروارد میانگین {fa(item["avg_forwards"])}'
        )
    periods = (
        ("بامداد", range(0, 6)),
        ("صبح", range(6, 12)),
        ("ظهر", range(12, 15)),
        ("عصر", range(15, 18)),
        ("شب", range(18, 24)),
    )
    hour_counts = metrics["posting_hours"]
    period_lines = []
    for label, period_hours in periods:
        count = sum(hour_counts.get(hour, 0) for hour in period_hours)
        share = (100 * count / metrics["post_count"]) if metrics["post_count"] else 0
        period_lines.append(f"• <b>{label}</b>: {fa(count)} پست ({fa(f'{share:.1f}')}٪)")
    peak_hour, peak_count = max(hour_counts.items(), key=lambda item: item[1], default=(None, 0))
    peak_line = (
        f"اوج انتشار: ساعت <b>{fa(peak_hour):0>2}:۰۰</b> با {fa(peak_count)} پست"
        if peak_hour is not None else "اوج انتشار: داده‌ای نیست"
    )
    return (
        "🧭 <b>پروفایل محتوایی و مخاطب کانال</b>\n"
        f"کانال: <b>{escape(channel)}</b>\n"
        f"بازه: {jalali_date(start, tz)} | {clock(start, tz)} تا {clock(end, tz)}\n"
        f"تعداد کل محتوا: <b>{fa(metrics['post_count'])}</b> | "
        f"واکنش: <b>{fa(metrics['total_reactions'])}</b> | فوروارد: <b>{fa(metrics['total_forwards'])}</b> | "
        f"بازدید: <b>{fa(metrics['total_views'])}</b> | پاسخ: <b>{fa(metrics['total_replies'])}</b>\n\n"
        "📦 <b>عملکرد بر اساس قالب محتوا</b>\n" + ("\n".join(media_lines) or "داده‌ای ثبت نشده است.") +
        "\n\n🧩 <b>موضوعات و واکنش مخاطب</b>\n" + ("\n".join(topic_lines) or "داده‌ای ثبت نشده است.") +
        "\n\n🕒 <b>الگوی زمانی انتشار</b>\n"
        + peak_line + "\n" + "\n".join(period_lines)
    )


def build_speed_detail_pages(db, channels: tuple[str, ...], start: datetime, end: datetime, tz,
                             important_min_channels: int, miss_grace_minutes: int,
                             per_page: int = 4, speed_min_channels: int | None = None,
                             missed_max_publishers: int | None = None, rank_weight: int = 60,
                             time_cap_minutes: int = 30, confidence_k: int = 5,
                             missed_min_publishers: int = 3) -> list[str]:
    result = _analyze(db, channels, start, end, tz, important_min_channels, miss_grace_minutes,
                      speed_min_channels, missed_max_publishers, rank_weight,
                      time_cap_minutes, confidence_k, missed_min_publishers)
    cards = []
    for number, news in enumerate(result["speed"], 1):
        lines = [f'<b>{fa(number)}) {clean_headline(news["ordered"][0]["text"])}</b>']
        for index, (item, point, rank_score, time_score, delay) in enumerate(news["scored"]):
            marker = "🥇" if index == 0 else ("🐢" if index == len(news["scored"]) - 1 else "▫️")
            link = f' · <a href="{escape(item["link"])}">پست</a>' if item["link"] else ""
            lines.append(
                f'{marker} {escape(item["channel"])} | {clock(item["published_at"], tz)} | '
                f'امتیاز {fa(f"{point:.1f}")} (رتبه {fa(f"{rank_score:.1f}")}، زمان {fa(f"{time_score:.1f}")}، '
                f'تأخیر {fa(f"{delay:.1f}")} دقیقه){link}'
            )
        for channel in news["absent"]:
            lines.append(f'🚫 {escape(channel)} | در این خبر منتشر نکرد')
        cards.append("\n".join(lines))

    if not cards:
        return ["📰 <b>جزئیات پایش</b>\n\nخبر واجد شرایطی ثبت نشده است."]
    pages = []
    total_pages = (len(cards) + per_page - 1) // per_page
    for page in range(total_pages):
        body = "\n\n".join(cards[page * per_page:(page + 1) * per_page])
        pages.append(
            f"📰 <b>جزئیات پایش سرعت</b>\nصفحه {fa(page + 1)} از {fa(total_pages)}\n━━━━━━━━━━━━━━\n\n{body}"
        )
    return pages


def split_html(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        if len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            piece = ""
            for line in block.splitlines():
                candidate_line = f"{piece}\n{line}" if piece else line
                if len(candidate_line) > limit and piece:
                    chunks.append(piece)
                    piece = line
                else:
                    piece = candidate_line
            if piece:
                chunks.append(piece)
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
