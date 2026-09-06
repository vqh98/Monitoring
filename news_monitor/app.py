from __future__ import annotations

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import secrets
import string
from html import escape
from datetime import datetime, time, timedelta, timezone

from telethon import Button, TelegramClient, events
from telethon.events import StopPropagation
from telethon import utils
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from .config import load_config
from .database import Database
from .report import (
    build_engagement_leaderboard, build_engagement_topic_analysis,
    build_channel_audience_analysis, channel_audience_metrics, engagement_topic,
    build_missed_channel_report, build_missed_overview,
    build_report, build_speed_detail_pages, fa, split_html,
)
from .textmatch import news_match, signature, similarity
from .proofreading import format_issues, proofread
from .ai_report import analyze_channel_audience

NEWS_MATCH_LOOKBACK_HOURS = 48

_log_handlers = [logging.StreamHandler()]
try:
    _log_dir = Path(os.getenv("MONITORING_BASE_DIR", Path(__file__).resolve().parent.parent)) / "data"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_handlers.append(RotatingFileHandler(
        _log_dir / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    ))
except OSError:
    pass
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=_log_handlers)
log = logging.getLogger("news-monitor")


def repair_message_text(value: str) -> str:
    """Repair legacy UTF-8-as-Latin-1 text and escaped newlines in messages."""
    text = (value or "").replace("\\\\n", "\n")
    # Older templates were saved with UTF-8 decoded as cp1252. Repair only
    # when the round-trip is valid and actually removes mojibake markers.
    if any(marker in text for marker in ("Ã", "Â", "Ù", "Ø", "â")):
        try:
            fixed = text.encode("cp1252").decode("utf-8")
            if fixed != text:
                text = fixed
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return text


def evaluate_viral_post(reactions: int, forwards: int, median_reactions: float,
                        median_forwards: float, sample_count: int,
                        phase_minutes: int, reaction_history=None,
                        forward_history=None, reaction_weight: float = 0.4,
                        score_floor_5: float = 90.0, score_floor_other: float = 88.0,
                        signal_floor_5: float = 93.0, signal_floor_other: float = 90.0,
                        established_threshold_5: float = 2.5,
                        established_threshold_other: float = 2.0) -> tuple[bool, str, float, float, float | None]:
    """Score a post against recent posts from the same channel at the same age.

    With raw history available, empirical percentiles replace ratios to the
    median. This is robust to outliers and zero-heavy channels and makes
    channels with very different audience sizes comparable.
    """
    reactions = max(0, int(reactions or 0))
    forwards = max(0, int(forwards or 0))
    median_reactions = max(0.0, float(median_reactions or 0.0))
    median_forwards = max(0.0, float(median_forwards or 0.0))
    sample_count = max(0, int(sample_count or 0))

    # Until a complete rolling baseline exists, do not classify posts.  This
    # guarantees that every alert is based on the channel/phase median rather
    # than on arbitrary absolute floors.
    if sample_count < 30:
        return False, "collecting", 0.0, 0.0, None

    reaction_history = [max(0, int(value or 0)) for value in (reaction_history or [])]
    forward_history = [max(0, int(value or 0)) for value in (forward_history or [])]
    history_size = min(len(reaction_history), len(forward_history))
    if history_size >= 30:
        reaction_history = reaction_history[:history_size]
        forward_history = forward_history[:history_size]

        def percentile(values, value):
            lower = sum(item < value for item in values)
            equal = sum(item == value for item in values)
            return 100.0 * (lower + 0.5 * equal) / len(values)

        def quantile(values, probability):
            ordered = sorted(values)
            position = (len(ordered) - 1) * probability
            low = math.floor(position)
            high = math.ceil(position)
            if low == high:
                return float(ordered[low])
            return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

        # Keep sparse-but-real forward signals usable (some media expose
        # forwards only on a small fraction of posts); the absolute floor
        # below still prevents a 1-vs-0 false positive.
        minimum_nonzero = max(3, math.ceil(history_size * 0.10))
        reaction_nonzero = sum(value > 0 for value in reaction_history)
        forward_nonzero = sum(value > 0 for value in forward_history)
        reactions_enabled = reaction_nonzero >= minimum_nonzero
        # Forward counts are often sparse by nature. Keep a small but real
        # signal usable, then apply the sparse-channel absolute floor below.
        forwards_enabled = forward_nonzero >= max(3, math.ceil(history_size * 0.10))
        reaction_percentile = percentile(reaction_history, reactions) if reactions_enabled else 0.0
        forward_percentile = percentile(forward_history, forwards) if forwards_enabled else 0.0
        reaction_weight = max(0.0, min(1.0, reaction_weight)) if reactions_enabled else 0.0
        forward_weight = (1.0 - reaction_weight) if forwards_enabled else 0.0
        weight_total = reaction_weight + forward_weight
        if not weight_total:
            return False, "percentile", reaction_percentile, forward_percentile, 0.0
        score = (
            reaction_weight * reaction_percentile
            + forward_weight * forward_percentile
        ) / weight_total

        strong_reaction = (
            reactions_enabled and reactions >= 3
            and reactions > quantile(reaction_history, 0.90)
        )
        sparse_forwards = (
            forwards_enabled
            and (
                forward_nonzero < max(8, math.ceil(history_size * 0.25))
                or quantile(forward_history, 0.75) <= 1
            )
        )
        forward_q90 = quantile(forward_history, 0.90)
        # Sparse channels need an absolute floor as well as a relative jump:
        # 1–2 forwards above an almost-all-zero baseline are not enough.
        forward_minimum = (
            max(3, math.ceil(quantile(forward_history, 0.75)) + 1)
            if sparse_forwards else 2
        )
        strong_forward = (
            forwards_enabled and forwards >= forward_minimum
            and forwards > forward_q90
        )
        # Lower adaptive floors catch meaningful trends while the strong
        # signal gate still prevents ordinary posts from being announced.
        score_floor = score_floor_5 if phase_minutes == 5 else score_floor_other
        signal_floor = signal_floor_5 if phase_minutes == 5 else signal_floor_other
        exceptional_signal = (
            (strong_reaction and reaction_percentile >= signal_floor)
            or (strong_forward and forward_percentile >= signal_floor)
        )
        return (
            score >= score_floor and exceptional_signal,
            "percentile",
            reaction_percentile,
            forward_percentile,
            score,
        )

    # Upgrade compatibility for a cached median without raw history.
    forward_ratio = forwards / max(1.0, median_forwards)
    threshold = established_threshold_5 if phase_minutes == 5 else established_threshold_other

    # Some channels disable reactions entirely.  Detect that from the
    # channel/phase baseline (rather than from the current post), so one
    # anomalous reaction can never switch the channel back to the mixed
    # formula.  In this mode the decision and score are based only on
    # forwards; reactions are intentionally ignored.
    reactions_enabled = median_reactions > 0
    if not reactions_enabled:
        reaction_ratio = 0.0
        score = forward_ratio
        viral_hit = score >= threshold
    else:
        reaction_ratio = reactions / max(1.0, median_reactions)
        reaction_weight = max(0.0, min(1.0, reaction_weight))
        score = reaction_weight * reaction_ratio + (1.0 - reaction_weight) * forward_ratio
        # A single unusually high metric is not sufficient: reactions and
        # forwards measure different kinds of interest, and relying on only
        # one made ordinary posts with a small burst of forwards look viral.
        # Requiring both ratios to clear a 2x floor improves precision
        # while retaining the original weighted score and phase thresholds.
        viral_hit = (
            score >= threshold
            and reaction_ratio >= 2.0
            and forward_ratio >= 2.0
        )
    return viral_hit, "established", reaction_ratio, forward_ratio, score


def find_similar_viral_alert(text: str, candidates, threshold: float = 0.72, matcher=None):
    """Return the earliest sufficiently similar alert, preferring the original post."""
    if not (text or "").strip():
        return None
    target = signature(text)
    best, best_score = None, 0.0
    for candidate in candidates:
        if matcher is None:
            matched, score = news_match(target, signature(candidate["text"] or ""), threshold)
        else:
            matched, score = matcher(target, signature(candidate["text"] or ""))
        if matched and score > best_score:
            best, best_score = candidate, score
    return best


def parse_channel_inputs(text: str) -> list[str]:
    text = re.sub(r"^/add(?:@\w+)?", "", text.strip(), flags=re.I)
    values = re.split(r"[\s,،;]+", text)
    channels = []
    for value in values:
        value = value.strip().rstrip("/")
        if not value:
            continue
        if "t.me/" in value:
            value = value.split("t.me/", 1)[1].split("?", 1)[0].strip("/").split("/", 1)[0]
        value = value.lstrip("@")
        if value and value not in channels:
            channels.append(value)
    return [f"@{value}" for value in channels]


def parse_proofreading_pair(text: str) -> tuple[str, str] | None:
    """Parse: /proofread_add <public source> <private destination invite/id>."""
    body = re.sub(r"^/proofread_add(?:@\w+)?\s*", "", text.strip(), flags=re.I)
    parts = body.split()
    if len(parts) != 2:
        return None
    sources = parse_channel_inputs("/add " + parts[0])
    return (sources[0], parts[1].strip()) if len(sources) == 1 else None


def day_window(config, now: datetime | None = None):
    now = now or datetime.now(config.timezone)
    start = datetime.combine(now.date(), time(config.day_start_hour), config.timezone)
    if now < start:
        start -= timedelta(days=1)
    return start, now


def proofreading_backfill_start(added_at: str, tz, start_hour: int = 8) -> datetime:
    """Start at 08:00 local time on the calendar day the mapping was added."""
    added = datetime.fromisoformat(added_at)
    if added.tzinfo is None:
        added = added.replace(tzinfo=timezone.utc)
    local_added = added.astimezone(tz)
    return datetime.combine(local_added.date(), time(start_hour), tz)


async def main() -> None:
    config = load_config()
    db = Database(config.database_path, config.owner_id)
    session_dir = config.database_path.parent
    session_dir.mkdir(parents=True, exist_ok=True)
    user = TelegramClient(str(session_dir / "user_session"), config.api_id, config.api_hash)
    bot = TelegramClient(str(session_dir / "bot_session"), config.api_id, config.api_hash)
    async def start_with_retry(client, bot_token=None):
        while True:
            try:
                if bot_token:
                    await client.start(bot_token=bot_token)
                else:
                    await client.start()
                return
            except (ConnectionError, OSError):
                log.exception("Telegram connection unavailable; retrying in 30 seconds")
                await asyncio.sleep(30)

    async def ensure_user_connected() -> bool:
        """Reconnect the collector promptly after a transient Telegram disconnect."""
        if user.is_connected():
            return True
        for attempt in range(1, 4):
            try:
                await user.connect()
                if user.is_connected():
                    log.info("collector Telegram connection restored (attempt %s)", attempt)
                    return True
            except (ConnectionError, OSError):
                log.warning("collector reconnect attempt %s failed", attempt, exc_info=True)
            await asyncio.sleep(min(30, 2 ** attempt))
        return False

    db.use_user(config.owner_id)
    for channel in config.initial_channels:
        db.add_channel(channel)

    def authorized(event) -> bool:
        if not event.is_private or not db.is_authorized(event.chat_id):
            return False
        db.use_user(event.chat_id)
        return True

    def channel_names() -> tuple[str, ...]:
        return tuple(row["channel"] for row in db.channels())

    def viral_channel_names() -> tuple[str, ...]:
        return tuple(row["channel"] for row in db.viral_channels())

    def monitoring_rules() -> tuple[int, int, int, int]:
        count = len(channel_names())
        speed_default = max(2, count // 2 + 1)
        speed_mode = db.get_global_setting("speed_rule_mode", "half_plus_one")
        if speed_mode == "half_plus_one":
            speed_value = speed_default
        elif speed_mode == "two_thirds":
            speed_value = max(2, (2 * count + 2) // 3)
        elif speed_mode == "all_minus_two":
            speed_value = max(2, count - 2)
        else:
            speed_value = db.get_global_int_setting("speed_min_channels", speed_default)
        speed_min = min(max(2, count), speed_value)
        missed_max = max(1, speed_min - 1)
        missed_min = min(missed_max, max(2, db.get_global_int_setting("missed_min_publishers", 3)))
        grace_minutes = min(180, max(5, db.get_global_int_setting("miss_grace_minutes", config.miss_grace_minutes)))
        return speed_min, missed_min, missed_max, grace_minutes

    def speed_rule_label() -> str:
        return {
            "half_plus_one": "نصف کانال‌ها + ۱",
            "two_thirds": "دوسوم کانال‌ها",
            "all_minus_two": "همهٔ کانال‌ها − ۲",
            "fixed": "عدد ثابت",
        }.get(db.get_global_setting("speed_rule_mode", "half_plus_one"), "عدد ثابت")

    def monitoring_rules_buttons():
        return with_back([
            [Button.inline("نصف + ۱", b"speed_formula:half_plus_one"), Button.inline("دوسوم", b"speed_formula:two_thirds")],
            [Button.inline("همه − ۲", b"speed_formula:all_minus_two"), Button.inline("عدد ثابت", b"speed_formula:fixed")],
            [Button.inline("➖ آستانه سرعت", b"adjust_rule:speed:-1"), Button.inline("➕ آستانه سرعت", b"adjust_rule:speed:1")],
            [Button.inline("➖ ۵ دقیقه", b"adjust_rule:grace:-5"), Button.inline("➕ ۵ دقیقه", b"adjust_rule:grace:5")],
            [Button.inline("➖ حداقل سوخت", b"adjust_rule:missed_min:-1"), Button.inline("➕ حداقل سوخت", b"adjust_rule:missed_min:1")],
        ])

    def monitoring_rules_text() -> str:
        speed_min, missed_min, missed_max, grace_minutes = monitoring_rules()
        return (
            "🎚 <b>قوانین پایش</b>\n\n"
            f"فرمول سرعت: <b>{speed_rule_label()}</b>\n"
            f"حداقل کانال برای سرعت: <b>{fa(speed_min)}</b>\n\n"
            "🔥 <b>فرمول سوخت خبر</b>\n"
            f"خبر مهم: انتشار در حداقل <b>{fa(speed_min)}</b> کانال\n"
            "سوخت هر رسانه: خبر مهمی که آن رسانه منتشر نکرده است\n"
            f"مهلت انتظار: <b>{fa(grace_minutes)} دقیقه</b>\n"
            "خبرهای کم‌پوشش وارد گزارش سوخت نمی‌شوند."
        )

    def speed_scoring_settings() -> tuple[int, int, int]:
        return (
            # Empirical defaults from the collected corpus: propagation to the
            # fifth publisher has a 34-minute median, so a 45-minute decay scale
            # preserves useful timing differences without flattening late posts.
            min(90, max(10, db.get_global_int_setting("speed_rank_weight", 55))),
            min(120, max(5, db.get_global_int_setting("speed_time_cap_minutes", 45))),
            # More shrinkage is appropriate now that the lower coverage boundary
            # yields more stories but unequal sample counts between channels.
            min(30, max(0, db.get_global_int_setting("speed_confidence_k", 8))),
        )

    def matching_settings() -> tuple[float, int, float, float]:
        threshold = db.get_global_setting("similarity_threshold", str(config.similarity_threshold))
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            threshold = config.similarity_threshold
        try:
            fragment_threshold = float(db.get_global_setting("match_fragment_threshold", "0.78"))
        except (TypeError, ValueError):
            fragment_threshold = 0.78
        try:
            numeric_boost = float(db.get_global_setting("match_numeric_boost", "0.12"))
        except (TypeError, ValueError):
            numeric_boost = 0.12
        return (
            min(0.95, max(0.20, threshold)),
            min(20, max(3, db.get_global_int_setting("match_fragment_min_words", 4))),
            min(1.0, max(0.50, fragment_threshold)),
            min(0.30, max(0.0, numeric_boost)),
        )

    def match_news(left: str, right: str) -> tuple[bool, float]:
        threshold, fragment_min_words, fragment_threshold, numeric_boost = matching_settings()
        return news_match(left, right, threshold, fragment_min_words, fragment_threshold, numeric_boost)

    async def store_message(message, chat=None):
        text = message_text(message)
        cleaned = signature(text)
        chat = chat or await message.get_chat()
        username = getattr(chat, "username", None)
        channel = f"@{username}" if username else str(getattr(chat, "id", "unknown"))
        if username:
            link = f"https://t.me/{username}/{message.id}"
        else:
            internal_id = str(getattr(chat, "id", "")).removeprefix("-100")
            link = f"https://t.me/c/{internal_id}/{message.id}" if internal_id else ""
        published = message.date or datetime.now(timezone.utc)
        best_id, best_score = None, 0.0
        if len(cleaned) >= 25:
            for cluster in db.recent_clusters(NEWS_MATCH_LOOKBACK_HOURS):
                matched, score = match_news(cleaned, cluster["representative"])
                if matched and score > best_score:
                    best_id, best_score = cluster["id"], score
            if best_id is None:
                best_id = db.create_cluster(cleaned, published)
            if db.add_post(channel, message.id, published, text, cleaned, link, best_id):
                log.info("stored %s/%s cluster=%s similarity=%.2f", channel, message.id, best_id, best_score)
        reactions, forwards = message_counts(message)
        views, replies = message_extra_counts(message)
        db.upsert_interaction_post(
            channel, message.id, published, text, link, reactions, forwards,
            message_media_type(message), views, replies, reaction_breakdown(message),
        )

    async def send_report(start: datetime, end: datetime):
        speed_min, missed_min, missed_max, grace_minutes = monitoring_rules()
        rank_weight, time_cap, confidence_k = speed_scoring_settings()
        report = build_report(db, channel_names(), start, end, config.timezone,
                              config.important_min_channels, grace_minutes, "speed",
                              speed_min, missed_max, rank_weight, time_cap, confidence_k, missed_min)
        callback = f"speed_details:{int(start.timestamp())}:{int(end.timestamp())}:0".encode()
        await bot.send_message(
            db.current_user_id() or config.report_chat_id, report, parse_mode="html", link_preview=False,
            buttons=[[Button.inline("📰 جزئیات گزارش", callback)]],
        )

    async def send_missed_report(start: datetime, end: datetime):
        speed_min, missed_min, missed_max, grace_minutes = monitoring_rules()
        report, counts = build_missed_overview(
            db, channel_names(), start, end, config.timezone,
            config.important_min_channels, grace_minutes,
            speed_min, missed_max, missed_min,
        )
        buttons = [
            [Button.inline(f"{channel} ({fa(count)})", f"mc:{int(start.timestamp())}:{int(end.timestamp())}:{channel}".encode())]
            for channel, count in counts
        ]
        await bot.send_message(
            db.current_user_id() or config.report_chat_id, report, parse_mode="html", link_preview=False,
            buttons=buttons or None,
        )

    def message_counts(message) -> tuple[int, int]:
        reactions = getattr(message, "reactions", None)
        reaction_count = sum(int(getattr(result, "count", 0) or 0)
                             for result in (getattr(reactions, "results", None) or []))
        return reaction_count, int(getattr(message, "forwards", 0) or 0)

    def reaction_breakdown(message) -> dict[str, int]:
        result = {}
        reactions = getattr(message, "reactions", None)
        for item in getattr(reactions, "results", None) or []:
            reaction = getattr(item, "reaction", None)
            emoji = getattr(reaction, "emoticon", None) or getattr(reaction, "emoji", None)
            if not emoji:
                # Telegram custom emoji reactions expose a document id rather
                # than an emoticon; keep them so the AI can still compare
                # reaction types instead of silently dropping their counts.
                document_id = getattr(reaction, "document_id", None)
                emoji = f"custom:{document_id}" if document_id else None
            if emoji:
                result[str(emoji)] = result.get(str(emoji), 0) + int(getattr(item, "count", 0) or 0)
        return result

    def message_media_type(message) -> str:
        """Classify the primary Telegram payload for audience comparisons."""
        if getattr(message, "grouped_id", None):
            return "album"
        if getattr(message, "video", None) or getattr(message, "gif", None):
            return "video"
        if getattr(message, "photo", None):
            return "image"
        if getattr(message, "voice", None) or getattr(message, "audio", None):
            return "audio"
        if getattr(message, "document", None):
            return "document"
        if getattr(message, "poll", None):
            return "poll"
        return "text"

    def message_extra_counts(message) -> tuple[int, int]:
        views = max(0, int(getattr(message, "views", 0) or 0))
        replies = getattr(message, "replies", None)
        reply_count = max(0, int(getattr(replies, "replies", 0) or 0))
        return views, reply_count

    def message_text(message) -> str:
        """Return body or media caption across Telethon message variants."""
        return str(getattr(message, "raw_text", None) or getattr(message, "message", None) or getattr(message, "caption", None) or "").strip()

    async def refresh_interactions(channel: str, start: datetime, end: datetime) -> None:
        chat = await user.get_entity(channel)
        username = getattr(chat, "username", None)
        canonical = f"@{username}" if username else channel
        async for message in user.iter_messages(chat, offset_date=end.astimezone(timezone.utc)):
            published = message.date or datetime.now(timezone.utc)
            if published < start.astimezone(timezone.utc):
                break
            if published >= end.astimezone(timezone.utc):
                continue
            if username:
                link = f"https://t.me/{username}/{message.id}"
            else:
                internal_id = str(getattr(chat, "id", "")).removeprefix("-100")
                link = f"https://t.me/c/{internal_id}/{message.id}" if internal_id else ""
            reactions, forwards = message_counts(message)
            views, replies = message_extra_counts(message)
            db.upsert_interaction_post(
                canonical, message.id, published, message_text(message),
                link, reactions, forwards, message_media_type(message),
                views, replies, reaction_breakdown(message),
            )

    async def resolve_viral_output_chat():
        saved = db.get_setting("viral_output_chat_id")
        if saved:
            return int(saved)
        if config.viral_chat_id is None:
            return None
        db.set_setting("viral_output_chat_id", str(config.viral_chat_id))
        return int(config.viral_chat_id)

    def detailed_topic(text: str, category: str) -> str:
        """Choose a repeatable, more specific topic than the broad category."""
        normalized = re.sub(r"\s+", " ", (text or "")).casefold()
        topic_rules = (
            ("قیمت بنزین و سوخت", ("بنزین", "گازوئیل", "سوخت", "سهمیه سوخت")),
            ("بازار ارز و دلار", ("دلار", "نرخ ارز", "بازار ارز", "ارز آزاد")),
            ("طلا و سکه", ("طلا", "سکه", "اونس")),
            ("تورم و معیشت", ("تورم", "گرانی", "معیشت", "سبد معیشت", "قدرت خرید")),
            ("مسکن و اجاره", ("مسکن", "اجاره", "مستاجر", "خانه")),
            ("مذاکرات و پرونده هسته‌ای", ("مذاکرات هسته", "پرونده هسته", "غنی‌سازی", "آژانس اتمی")),
            ("تحریم‌ها و سیاست خارجی", ("تحریم", "سیاست خارجی", "وزارت خارجه")),
            ("جنگ و درگیری منطقه‌ای", ("جنگ", "حمله موشکی", "آتش‌بس", "غزه", "اسرائیل", "لبنان")),
            ("جنگ اوکراین", ("اوکراین", "کی‌یف", "زلنسکی")),
            ("دولت و سیاست داخلی", ("دولت", "رئیس جمهور", "رئیس‌جمهور", "هیئت دولت")),
            ("مجلس و قانون‌گذاری", ("مجلس", "نماینده مجلس", "قالیباف", "لایحه", "طرح مجلس")),
            ("حوادث و جرایم", ("قتل", "سرقت", "تصادف", "حادثه", "بازداشت", "کلاهبرداری")),
            ("بلایای طبیعی و هوا", ("زلزله", "سیل", "طوفان", "هواشناسی", "آلودگی هوا")),
            ("سلامت و درمان", ("سلامت", "بیماری", "درمان", "پزشک", "دارو")),
            ("تغذیه و آشپزی", ("آشپزی", "طرز تهیه", "دستور پخت", "غذا", "کیک", "شیرینی")),
            ("ورزش و بدنسازی", ("بدنسازی", "تمرین", "عضله", "فیتنس", "ورزش")),
            ("فوتبال", ("فوتبال", "لیگ", "تیم ملی", "پرسپولیس", "استقلال")),
            ("فناوری و اینترنت", ("اینترنت", "فیلترینگ", "هوش مصنوعی", "تلگرام", "فناوری")),
            ("آموزش و دانشگاه", ("مدرسه", "دانشگاه", "کنکور", "دانش‌آموز", "دانشجو")),
            ("سبک زندگی", ("سبک زندگی", "روانشناسی", "خواب", "مد", "زیبایی")),
        )
        for topic, keywords in topic_rules:
            if any(keyword in normalized for keyword in keywords):
                return topic
        return category or "سایر موضوعات"

    def weekly_metrics(channel: str, start: datetime, end: datetime) -> dict | None:
        rows = list(db.channel_posts_between(channel, start, end))
        if not rows:
            return None
        count = len(rows)
        total_reactions = sum(max(0, int(row["reaction_count"] or 0)) for row in rows)
        total_forwards = sum(max(0, int(row["forward_count"] or 0)) for row in rows)
        total_views = sum(max(0, int(row["view_count"] or 0)) for row in rows)
        return {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "post_count": count,
            "week_avg_reactions_per_post": round(total_reactions / count, 4),
            "week_avg_forwards_per_post": round(total_forwards / count, 4),
            "week_reaction_rate": round(total_reactions / total_views, 8) if total_views else None,
        }

    def weekly_history(channel: str, current_start: datetime) -> dict:
        previous_start = current_start - timedelta(days=7)
        weekly_rows = [
            weekly_metrics(
                channel,
                current_start - timedelta(days=7 * index),
                current_start - timedelta(days=7 * (index - 1)),
            )
            for index in range(1, 5)
        ]
        valid = [item for item in weekly_rows if item]
        def average(key):
            values = [item[key] for item in valid if item.get(key) is not None]
            return round(sum(values) / len(values), 8) if values else None
        return {
            "previous_week": weekly_metrics(channel, previous_start, current_start),
            "four_week_average": {
                "week_avg_reactions_per_post": average("week_avg_reactions_per_post"),
                "week_avg_forwards_per_post": average("week_avg_forwards_per_post"),
                "week_reaction_rate": average("week_reaction_rate"),
                "weeks_available": len(valid),
            },
        }

    async def send_engagement_report(start: datetime, end: datetime,
                                     channel_override: str | None = None,
                                     channel_title_override: str | None = None,
                                     output_chat_override: int | None = None,
                                     ai_enabled: bool = True,
                                     recipient_chat: int | None = None,
                                     send_leaderboard: bool = False):
        channel = channel_override
        if not channel:
            return
        try:
            await refresh_interactions(channel, start, end)
            leaderboard = build_engagement_leaderboard(
                db, channel, start, end, config.timezone,
                channel_title=channel_title_override,
            )
            analysis = build_engagement_topic_analysis(
                db, channel, start, end, config.timezone,
                channel_title=channel_title_override,
            )
            profile = build_channel_audience_analysis(db, channel, start, end, config.timezone)
            metrics = channel_audience_metrics(db, channel, start, end, config.timezone)
            history = weekly_history(channel, start)
            control_summary = {
                "post_count": metrics["post_count"],
                "total_reactions": metrics["total_reactions"],
                "total_forwards": metrics["total_forwards"],
                "total_views": metrics["total_views"],
                "total_comments": metrics["total_replies"],
                "reaction_rate": (
                    round(metrics["total_reactions"] / metrics["total_views"], 8)
                    if metrics["total_views"] else None
                ),
            }
            rows = []
            for row in db.channel_posts_between(channel, start, end):
                media_type = row["media_type"] or "text"
                content_type = {"image": "photo"}.get(media_type, media_type)
                title = (row["text"] or "").replace("\n", " ").strip()[:180]
                category = engagement_topic(row["text"] or "")
                try:
                    published_at = datetime.fromisoformat(row["published_at"]).astimezone(config.timezone).isoformat()
                except (TypeError, ValueError):
                    published_at = row["published_at"]
                rows.append({
                    "id": str(row["message_id"]),
                    "date": published_at,
                    "content_type": content_type,
                    "category": category,
                    "topic": detailed_topic(row["text"] or "", category),
                    "title": title,
                    "stats": {
                        "views": int(row["view_count"] or 0),
                        "reactions_total": int(row["reaction_count"] or 0),
                        "reactions_breakdown": json.loads(row["reaction_breakdown"] or "{}")
                        if "reaction_breakdown" in row.keys() else {},
                        "forwards": int(row["forward_count"] or 0),
                        "comments": int(row["reply_count"] or 0),
                    },
                })
            cache_key = (channel.casefold(), start.isoformat(), end.isoformat())
            ai_analysis = ""
            if ai_enabled:
                ai_analysis = channel_analysis_cache.get(cache_key)
                if ai_analysis is None:
                    ai_analysis = await analyze_channel_audience(
                        json.dumps(control_summary, ensure_ascii=False), metrics, rows,
                        config.ai_api_key, config.ai_base_url, config.ai_model,
                        historical_summary=history,
                    )
                    channel_analysis_cache[cache_key] = ai_analysis or ""
            output_chat = output_chat_override
            # The leaderboard is part of the public engagement report and
            # must also be delivered when a special-analysis mapping routes
            # the report to its dedicated destination.
            if send_leaderboard and output_chat is not None:
                for chunk in split_html(leaderboard):
                    await bot.send_message(output_chat, chunk, parse_mode="html", link_preview=False)
            if ai_analysis:
                owner_report = "🧠 <b>گزارش هفتگی رفتار مخاطب</b>\n\n" + escape(ai_analysis)
            else:
                owner_report = analysis + "\n\n" + profile
            target = recipient_chat or db.current_user_id() or config.report_chat_id
            for chunk in split_html(owner_report):
                await bot.send_message(target, chunk, parse_mode="html", link_preview=False)
            return
        except Exception:
            log.exception("interaction report failed for %s", channel)
            report = f"⚠️ دریافت یا ارسال تحلیل تعامل {channel} ناموفق بود؛ دسترسی کانال مبدأ و مقصد را بررسی کنید."
        fallback_target = recipient_chat or db.current_user_id() or config.report_chat_id
        for chunk in split_html(report):
            await bot.send_message(fallback_target, chunk, parse_mode="html", link_preview=False)

    async def send_special_nightly_leaderboard(start: datetime, end: datetime,
                                               source_channel: str, source_title: str,
                                               destination_chat: int):
        try:
            await refresh_interactions(source_channel, start, end)
            leaderboard = build_engagement_leaderboard(
                db, source_channel, start, end, config.timezone,
                channel_title=source_title,
            )
            for chunk in split_html(leaderboard):
                await bot.send_message(destination_chat, chunk, parse_mode="html", link_preview=False)
        except Exception:
            log.exception("special nightly leaderboard failed for %s", source_channel)

    async def scan_and_send_viral(start: datetime, end: datetime) -> int:
        viral_output_chat = await resolve_viral_output_chat()
        if viral_output_chat is None:
            return 0
        if not await ensure_user_connected():
            log.warning("skipping viral scan while Telegram collector is disconnected")
            return 0
        refresh_start = max(start, end - timedelta(minutes=26))
        for channel in viral_channel_names():
            try:
                await refresh_interactions(channel, refresh_start, end)
            except Exception:
                log.exception("viral interaction refresh failed for %s", channel)
        sent = 0
        now = datetime.now(timezone.utc)
        for phase_minutes in (5, 10, 20):
            for post in db.posts_due_for_snapshot(now, phase_minutes):
                baseline = db.interaction_baseline(post["channel"], phase_minutes)
                reaction_history, forward_history = db.interaction_history(
                    post["channel"], phase_minutes,
                )
                db.save_interaction_snapshot(post["channel"], post["message_id"], phase_minutes,
                                             post["reaction_count"], post["forward_count"], now)
                if baseline is None:
                    continue
                median_reactions, median_forwards, sample_count = baseline
                viral_reaction_weight = min(0.9, max(0.1, db.get_int_setting("viral_reaction_weight", 40) / 100))
                viral_threshold_5 = min(10.0, max(1.0, db.get_int_setting("viral_threshold_5", 25) / 10))
                viral_threshold_other = min(10.0, max(1.0, db.get_int_setting("viral_threshold_other", 20) / 10))
                viral_hit, rule_mode, reaction_ratio, forward_ratio, score = evaluate_viral_post(
                    post["reaction_count"], post["forward_count"], median_reactions,
                    median_forwards, sample_count, phase_minutes,
                    reaction_history, forward_history,
                    viral_reaction_weight, 90.0, 88.0, 93.0, 90.0,
                    viral_threshold_5, viral_threshold_other,
                )
                if not viral_hit:
                    continue
                reasons = []
                if rule_mode == "percentile":
                    reasons.append(f"🔥 ری‌اکشن: <b>{fa(post['reaction_count'])}</b> | صدک عملکرد: <b>{fa(round(reaction_ratio, 1))}</b>")
                    reasons.append(f"📤 فوروارد: <b>{fa(post['forward_count'])}</b> | صدک عملکرد: <b>{fa(round(forward_ratio, 1))}</b>")
                    baseline_text = (
                        f"\nمبنا: {fa(min(len(reaction_history), len(forward_history)))} پست اخیر "
                        f"همین رسانه در دقیقهٔ {fa(phase_minutes)}"
                    )
                else:
                    reasons.append(f"🔥 ری‌اکشن: <b>{fa(post['reaction_count'])}</b> | میانه: <b>{fa(round(median_reactions, 1))}</b> | نسبت: <b>{fa(round(reaction_ratio, 1))}</b>")
                    reasons.append(f"📤 فوروارد: <b>{fa(post['forward_count'])}</b> | میانه: <b>{fa(round(median_forwards, 1))}</b> | نسبت: <b>{fa(round(forward_ratio, 1))}</b>")
                    baseline_text = "\nمبنای میانه: ۳۰ پست اخیر همین کانال"
                title = (post["text"] or "پست بدون متن").strip().replace("\n", " ")[:220]
                alert = (
                    f"<b>متن کپشن:</b>\n{escape(title)}\n\n"
                    "🚨 <b>محتوای وایرال شناسایی شد</b>\n\n"
                    f"کانال: <b>{escape(post['channel'])}</b>\n"
                    f"مرحلهٔ سنجش: <b>{fa(phase_minutes)} دقیقه پس از انتشار</b>\n"
                    + (f"امتیاز انتخاب: <b>{fa(round(score, 1))} از ۱۰۰</b>\n" if score is not None else "مرحله: <b>در حال جمع‌آوری ۳۰ نمونه</b>\n")
                    + "\n".join(reasons)
                    + baseline_text
                    + (f"\n<a href=\"{escape(post['link'], quote=True)}\">مشاهده پست</a>" if post["link"] else "")
                )
                alert = repair_message_text(alert)
                try:
                    duplicate = find_similar_viral_alert(
                        post["text"],
                        db.recent_viral_alert_candidates(now - timedelta(hours=NEWS_MATCH_LOOKBACK_HOURS)),
                        matcher=match_news,
                    )
                    # First occurrence is standalone; cross-channel matches
                    # are short replies on the original alert only.
                    if duplicate is not None:
                        if duplicate["channel"].lower() == post["channel"].lower():
                            db.mark_viral_alerted(post["channel"], post["message_id"])
                            continue
                        reply = (
                            f"📌 همان محتوا در کانال <b>{escape(post['channel'])}</b> "
                            "هم پربازخورد شد.\n"
                            + (f'<a href="{escape(post["link"], quote=True)}">مشاهده پست</a>' if post["link"] else "")
                        )
                        sent_message = await bot.send_message(
                            viral_output_chat, reply, parse_mode="html",
                            link_preview=False,
                            reply_to=duplicate["destination_message_id"],
                        )
                    else:
                        sent_message = await bot.send_message(
                            viral_output_chat, alert, parse_mode="html",
                            link_preview=False,
                        )
                    db.mark_viral_alerted(
                        post["channel"], post["message_id"], sent_message.id,
                    )
                    sent += 1
                except Exception:
                    log.exception("cannot send viral alert for %s/%s", post["channel"], post["message_id"])
        return sent

    menu = [
        [Button.text("📊 تحلیل ویژه", resize=True)],
        [Button.text("📡 فهرست کانال‌ها", resize=True), Button.text("⚡️ گزارش سرعت", resize=True)],
        [Button.text("🔥 سوخت خبر", resize=True)],
        [Button.text("🛠️ باگ‌یاب کانال‌ها", resize=True)],
        [Button.text("⚙️ تنظیمات", resize=True)],
    ]
    owner_menu = menu + [[Button.text("🔑 ساخت کد ورود", resize=True)]]

    owner_menu.append([Button.text("👥 مدیریت کاربران", resize=True)])

    def menu_for(chat_id: int):
        return owner_menu if db.is_owner(chat_id) else menu

    def back_to_menu_button():
        return Button.inline("🏠 بازگشت به منوی اصلی", b"main_menu")

    def with_back(buttons):
        return list(buttons) + [[back_to_menu_button()]]
    pending_action: dict[int, str] = {}
    pending_proofreading_source: dict[int, str] = {}
    pending_analysis_source: dict[int, str] = {}
    channel_analysis_cache: dict[tuple[str, str, str], str] = {}

    def proofreading_buttons():
        rows = db.proofreading_channels()
        buttons = [
            [Button.inline("➕ تنظیم کانال جدید", b"proofreading_add")],
            [Button.inline("🔄 نمایش فهرست", b"proofreading_list")],
        ]
        buttons.extend([
            [Button.inline(f"🗑 حذف {row['source_channel']}", f"proofreading_remove:{row['source_channel']}".encode())]
            for row in rows
        ])
        return with_back(buttons)

    async def show_proofreading_panel(event):
        rows = db.proofreading_channels()
        body = "\n".join(
            f"{index}. <b>{escape(row['source_channel'])}</b> ← {escape(row['destination_title'] or str(row['destination_chat_id']))}"
            for index, row in enumerate(rows, 1)
        ) or "هنوز کانالی تنظیم نشده است."
        await event.respond(
            "🛠️ <b>باگ‌یاب کانال‌ها</b>\n\n" + body +
            "\n\nبرای تعریف مسیر جدید، دکمهٔ «تنظیم کانال جدید» را بزنید.",
            parse_mode="html", buttons=proofreading_buttons(),
        )

    def analysis_buttons():
        rows = db.analysis_channels()
        buttons = [[Button.inline("➕ افزودن کانال تحلیل", b"analysis_add")]]
        for row in rows:
            state = "فعال" if row["ai_enabled"] else "خاموش"
            source = row["source_channel"]
            buttons.append([
                Button.inline(f"🤖 هوش مصنوعی: {state}", f"analysis_ai:{source}".encode()),
                Button.inline("🗑 حذف", f"analysis_remove:{source}".encode()),
            ])
        return with_back(buttons)

    async def show_analysis_panel(event):
        rows = db.analysis_channels()
        body = "\n".join(
            f"{i}. <b>{escape(row['source_channel'])}</b> ← "
            f"{escape(row['destination_title'] or str(row['destination_chat_id']))} | "
            f"گزارش هوشمند: {'فعال' if row['ai_enabled'] else 'خاموش'}"
            for i, row in enumerate(rows, 1)
        ) or "هنوز کانالی برای تحلیل ویژه ثبت نشده است."
        await event.respond(
            "📊 <b>تحلیل ویژه</b>\n\n" + body +
            "\n\nحداکثر ۵ کانال و حداکثر ۳ گزارش هوشمند هم‌زمان قابل فعال‌سازی است.",
            parse_mode="html", buttons=analysis_buttons(),
        )

    async def add_analysis_mapping(event, source: str, destination_value: str):
        try:
            destination = await resolve_private_destination(destination_value)
            if not getattr(destination, "broadcast", False):
                raise ValueError("مقصد باید کانال باشد")
            # Verify the bot can publish before saving the mapping.
            permissions = await bot.get_permissions(destination, "me")
            if getattr(permissions, "send_messages", True) is False:
                raise ValueError("ربات اجازهٔ ارسال ندارد")
            result = db.add_analysis_channel(
                source, getattr(destination, "title", ""), utils.get_peer_id(destination),
                getattr(destination, "title", ""), destination_value,
            )
            if result == "limit":
                await event.reply("حداکثر ۵ کانال برای تحلیل ویژه قابل ثبت است.", buttons=menu)
            elif result == "duplicate":
                await event.reply("این کانال قبلاً در تحلیل ویژه ثبت شده است.", buttons=menu)
            else:
                await event.reply("✅ کانال برای تحلیل ویژه ثبت شد.", buttons=menu)
        except Exception:
            log.exception("cannot add analysis destination for %s", source)
            await event.reply(
                "❌ مقصد قابل دسترسی نیست یا ربات اجازهٔ ارسال در آن ندارد. "
                "لینک عمومی، لینک دعوت، لینک پیام خصوصی، شناسهٔ عددی یا نام کاربری کانال را ارسال کنید.",
                buttons=menu,
            )

    async def resolve_private_destination(value: str):
        value = value.strip()
        # Accept Telegram's common invite URL variants, including tg:// links,
        # optional www/https prefixes, query strings and trailing slashes.
        tg_invite = re.search(r"tg://join\?(?:[^#]*&)?invite=([\w-]+)", value, re.I)
        if tg_invite:
            value = tg_invite.group(1)
        # A private channel may be supplied either as an invite link
        # (t.me/+hash or t.me/joinchat/hash), a numeric peer id, or a
        # message link (t.me/c/<internal-id>/<message-id>).  Telethon
        # cannot resolve the latter as a username, but the peer id is
        # deterministic: Telegram channel ids use the -100 prefix.
        private_message_match = re.search(
            r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/c/(\d+)(?:/\d+)?",
            value, re.I,
        )
        if private_message_match:
            internal_id = private_message_match.group(1)
            return await user.get_entity(int(f"-100{internal_id}"))

        invite_match = re.search(
            r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:joinchat/|\+)([\w-]+)(?:[/?#].*)?$",
            value, re.I,
        )
        if invite_match:
            invite_hash = invite_match.group(1)
            try:
                checked = await user(CheckChatInviteRequest(invite_hash))
                chat = getattr(checked, "chat", None)
            except Exception:
                # If the user account is already a member, Telegram may reject
                # checking the invite.  Importing the invite is idempotent for
                # an existing member and returns the channel entity.
                chat = None
            if chat is None:
                joined = await user(ImportChatInviteRequest(invite_hash))
                chat = joined.chats[0] if joined.chats else None
            if chat is None:
                raise ValueError("private channel is unavailable")
            return chat
        # Accept a bare invite hash as a convenience, while retaining
        # support for regular usernames and numeric peer ids.
        if re.fullmatch(r"[\w-]{16,}", value) and not value.startswith("@"):
            try:
                checked = await user(CheckChatInviteRequest(value))
                chat = getattr(checked, "chat", None)
            except Exception:
                chat = None
            if chat is None:
                joined = await user(ImportChatInviteRequest(value))
                chat = joined.chats[0] if joined.chats else None
            if chat is None:
                raise ValueError("private channel is unavailable")
            return chat
        return await user.get_entity(int(value) if re.fullmatch(r"-?\d+", value) else value)

    async def add_proofreading_mapping(event, text: str):
        pair = parse_proofreading_pair(text)
        if pair is None:
            await event.reply(
                "فرمت درست:\n<code>/proofread_add @source https://t.me/+privateInvite</code>\n\n"
                "بین کانال مبدأ و لینک کانال خصوصی یک فاصله بگذارید.", parse_mode="html", buttons=menu,
            )
            return
        source, destination_value = pair
        try:
            source_entity = await user.get_entity(source)
            if not getattr(source_entity, "broadcast", False):
                await event.reply("❌ مبدأ باید یک کانال تلگرام باشد.", buttons=menu)
                return
            destination = await resolve_private_destination(destination_value)
            if not getattr(destination, "broadcast", False):
                await event.reply("❌ مقصد باید یک کانال خصوصی باشد، نه گروه یا گفت‌وگوی شخصی.", buttons=menu)
                return
            permissions = await bot.get_permissions(destination, "me")
            if getattr(permissions, "send_messages", True) is False:
                await event.reply("❌ ربات در کانال مقصد اجازهٔ ارسال پست ندارد. ابتدا ربات را در کانال ادمین کنید.", buttons=menu)
                return
            canonical = f"@{source_entity.username}" if source_entity.username else str(utils.get_peer_id(source_entity))
            added = db.add_proofreading_channel(
                canonical, getattr(source_entity, "title", ""), utils.get_peer_id(destination),
                getattr(destination, "title", ""), destination_value,
            )
            if added:
                await event.reply(
                    f"✅ باگ‌یابی لحظه‌ای {canonical} فعال شد.\n"
                    f"مقصد: {getattr(destination, 'title', 'کانال خصوصی')}\n"
                    "از این پس فقط پست‌های جدیدِ دارای خطا به مقصد فرستاده می‌شوند.", buttons=menu,
                )
            else:
                await event.reply(
                    f"ℹ️ برای {canonical} از قبل مقصد ثبت شده است؛ ابتدا آن را حذف کنید.", buttons=menu,
                )
        except Exception:
            log.exception("cannot add proofreading mapping %s", source)
            await event.reply(
                "❌ مبدأ یا مقصد قابل دسترسی نیست. لینک دعوت مقصد باید معتبر باشد و حساب متصل "
                "در کانال خصوصی اجازهٔ ارسال پیام داشته باشد.", buttons=menu,
            )

    async def show_proofreading_channels(event):
        rows = db.proofreading_channels()
        body = "\n".join(
            f"{index}. {row['source_channel']} ← {row['destination_title'] or row['destination_chat_id']}"
            for index, row in enumerate(rows, 1)
        ) or "هنوز نگاشتی برای باگ‌یاب ثبت نشده است."
        await event.reply("🛠️ کانال‌های باگ‌یاب:\n" + body, buttons=menu)

    async def show_menu(event, message="یکی از گزینه‌ها را انتخاب کنید:"):
        await event.respond(message, buttons=menu_for(event.chat_id))

    @bot.on(events.CallbackQuery(data=b"main_menu"))
    async def main_menu_callback(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید.", alert=True)
            return
        await event.answer()
        await event.respond("مدیریت پایش کانال‌ها", buttons=menu_for(event.chat_id))

    async def add_many(event, text: str):
        requested = parse_channel_inputs(text)
        if not requested:
            await event.reply("شناسه یا لینک کانال‌ها را وارد کنید؛ هرکدام در یک خط یا با فاصله و ویرگول.")
            return
        results = []
        for channel in requested:
            try:
                entity = await user.get_entity(channel)
                if not getattr(entity, "broadcast", False):
                    results.append(f"❌ {channel}: کانال نیست")
                    continue
                canonical = f"@{entity.username}" if entity.username else channel
                if db.add_channel(canonical, getattr(entity, "title", "")):
                    results.append(f"✅ {canonical}: اضافه شد")
                else:
                    results.append(f"ℹ️ {canonical}: از قبل موجود بود")
            except Exception:
                log.exception("cannot add channel %s", channel)
                results.append(f"❌ {channel}: پیدا نشد یا قابل مشاهده نیست")
        await event.reply("\n".join(results), buttons=menu)

    async def remove_many(event, text: str):
        requested = parse_channel_inputs("/add " + text)
        if not requested:
            await event.reply("شناسه یا لینک کانال‌هایی که باید حذف شوند را بفرستید.")
            return
        results = [f"✅ {channel}: حذف شد" if db.remove_channel(channel) else f"ℹ️ {channel}: در فهرست نبود" for channel in requested]
        await event.reply("\n".join(results), buttons=menu)

    async def show_channels(event):
        rows = db.channels()
        body = "\n".join(f"{i}. {row['channel']}" for i, row in enumerate(rows, 1)) or "هنوز کانالی اضافه نشده است."
        channel_buttons = [
            [Button.inline("➕ افزودن کانال", b"channels_add"),
             Button.inline("➖ حذف کانال", b"channels_remove")],
        ]
        await event.reply("📡 کانال‌های تحت پایش:\n" + body, buttons=with_back(channel_buttons))

    async def show_viral_channels(event):
        rows = db.viral_channels()
        body = "\n".join(f"{i}. {row['channel']}" for i, row in enumerate(rows, 1)) or "هنوز کانالی برای پایش محتوای وایرال اضافه نشده است."
        await event.respond(
            "🚨 <b>فهرست اختصاصی پایش وایرال</b>\n\n" + body,
            parse_mode="html", buttons=[[back_to_menu_button()]],
        )

    async def add_viral_many(event, text: str):
        requested = parse_channel_inputs("/add " + text)
        if not requested:
            await event.reply("شناسه یا لینک کانال‌هایی که باید برای محتوای وایرال بررسی شوند را بفرستید.")
            return
        results = []
        for channel in requested:
            try:
                entity = await user.get_entity(channel)
                if not getattr(entity, "broadcast", False):
                    results.append(f"❌ {channel}: کانال نیست")
                    continue
                canonical = f"@{entity.username}" if entity.username else channel
                added = db.add_viral_channel(canonical, getattr(entity, "title", ""))
                results.append(f"✅ {canonical}: اضافه شد" if added else f"ℹ️ {canonical}: از قبل موجود بود")
            except Exception:
                log.exception("cannot add viral channel %s", channel)
                results.append(f"❌ {channel}: پیدا نشد یا با حساب فعلی قابل مشاهده نیست")
        await event.reply("\n".join(results), buttons=menu)

    async def remove_viral_many(event, text: str):
        requested = parse_channel_inputs("/add " + text)
        if not requested:
            await event.reply("شناسه یا لینک کانال‌هایی که باید از فهرست وایرال حذف شوند را بفرستید.")
            return
        results = [f"✅ {channel}: حذف شد" if db.remove_viral_channel(channel)
                   else f"ℹ️ {channel}: در فهرست وایرال نبود" for channel in requested]
        await event.reply("\n".join(results), buttons=menu)

    async def set_viral_output_channel(event, text: str):
        value = text.strip()
        if not value:
            await event.reply("لینک یا شناسهٔ کانال مقصد محتوای پربازخورد را بفرستید.")
            return
        try:
            entity = await resolve_private_destination(value)
            if not getattr(entity, "broadcast", False):
                await event.reply("این شناسه مربوط به کانال نیست.")
                return
            chat_id = utils.get_peer_id(entity)
            # Resolve through the bot as well: this confirms the bot has
            # access to the destination and can publish there.
            bot_entity = await bot.get_entity(chat_id)
            permissions = await bot.get_permissions(bot_entity, "me")
            if getattr(permissions, "send_messages", True) is False:
                raise ValueError("bot cannot publish to destination")
            db.set_setting("viral_output_chat_id", str(chat_id))
            db.set_setting("viral_output_chat_title", getattr(entity, "title", "") or value)
            await event.reply("✅ کانال مقصد محتوای پربازخورد تنظیم شد.", buttons=menu)
        except Exception:
            log.exception("cannot set viral output channel %s", value)
            await event.reply("❌ کانال پیدا نشد یا ربات به آن دسترسی ندارد. ابتدا ربات را در کانال ادمین کنید.", buttons=menu)

    @bot.on(events.NewMessage)
    async def access_gate(event):
        if not event.is_private:
            raise StopPropagation
        if db.is_authorized(event.chat_id):
            return
        if db.consume_access_invite(event.raw_text.strip(), event.chat_id):
            db.use_user(event.chat_id)
            await event.reply("✅ دسترسی شما فعال شد. تنظیمات شما مستقل و از ابتدا خالی است.")
            await show_menu(event)
        else:
            await event.reply("🔐 کد ورود معتبر و استفاده‌نشده را ارسال کنید.")
        raise StopPropagation

    @bot.on(events.NewMessage(pattern=r"^(?:🔑 ساخت کد ورود|/invite(?:@\w+)?)$"))
    async def create_access_code(event):
        if not db.is_owner(event.chat_id) or not authorized(event):
            return
        alphabet = string.ascii_uppercase + string.digits
        code = "NF-" + "".join(secrets.choice(alphabet) for _ in range(10))
        db.create_access_invite(code)
        await event.reply(
            "🔑 <b>کد ورود یک‌بارمصرف</b>\n\n"
            f"<code>{code}</code>\n\n"
            "این کد فقط توسط یک کاربر قابل استفاده است و پس از اولین ورود باطل می‌شود.",
            parse_mode="html", buttons=menu_for(event.chat_id),
        )

    @bot.on(events.NewMessage(pattern=r"^/users(?:@\w+)?$"))
    async def manage_users(event):
        if not db.is_owner(event.chat_id) or not authorized(event):
            return
        rows = db.authorized_users()
        body = "\n".join(
            f"{index}. <code>{row['user_id']}</code>"
            + (" — مالک" if row["is_owner"] else "")
            for index, row in enumerate(rows, 1)
        ) or "کاربر مجازی ثبت نشده است."
        buttons = [
            [Button.inline(f"لغو دسترسی {row['user_id']}", f"user_revoke:{row['user_id']}".encode())]
            for row in rows if not row["is_owner"]
        ]
        buttons.append([Button.inline("🔄 تازه‌سازی", b"users_list")])
        await event.reply("👥 <b>مدیریت کاربران</b>\n\n" + body,
                          parse_mode="html", buttons=with_back(buttons))

    @bot.on(events.NewMessage(pattern=r"^👥 مدیریت کاربران$"))
    async def manage_users_button(event):
        await manage_users(event)

    @bot.on(events.CallbackQuery(data=b"users_list"))
    async def users_list_callback(event):
        if not db.is_owner(event.chat_id) or not authorized(event):
            await event.answer("دسترسی ندارید.", alert=True)
            return
        await event.answer()
        await manage_users(event)

    @bot.on(events.CallbackQuery(pattern=rb"^user_revoke:(\d+)$"))
    async def user_revoke_callback(event):
        if not db.is_owner(event.chat_id) or not authorized(event):
            await event.answer("دسترسی ندارید.", alert=True)
            return
        user_id = int(event.pattern_match.group(1))
        removed = db.revoke_user(user_id)
        await event.answer("دسترسی لغو شد." if removed else "کاربر پیدا نشد.")
        await manage_users(event)

    @bot.on(events.NewMessage(pattern=r"^/(start|menu)(?:@\w+)?$"))
    async def start_menu(event):
        if authorized(event):
            pending_action.pop(event.chat_id, None)
            await show_menu(event, "مدیریت پایش کانال‌ها")

    @bot.on(events.NewMessage(pattern=r"^/(report|today|ranking)(?:@\w+)?$"))
    async def command(event):
        if not authorized(event):
            return
        await event.reply("⏳ گزارش در حال آماده‌سازی است؛ ممکن است در چند پیام ارسال شود.")
        start, end = day_window(config)
        await send_report(start, end)

    @bot.on(events.NewMessage(pattern=r"^/missed(?:@\w+)?$"))
    async def missed_command(event):
        if not authorized(event):
            return
        await event.reply("⏳ گزارش سوخت خبر در حال آماده‌سازی است.")
        start, end = day_window(config)
        await send_missed_report(start, end)

    @bot.on(events.NewMessage(pattern=r"^/status(?:@\w+)?$"))
    async def status(event):
        if authorized(event):
            await event.reply(f"✅ پایش فعال است. تعداد کانال‌ها: {len(channel_names())}", buttons=menu)

    @bot.on(events.NewMessage(pattern=r"^/add(?:@\w+)?(?:\s|$)"))
    async def add_channel(event):
        if not authorized(event):
            return
        await add_many(event, event.raw_text)

    @bot.on(events.NewMessage(pattern=r"^/remove(?:@\w+)?(?:\s+(.+))?$"))
    async def remove_channel(event):
        if not authorized(event):
            return
        await remove_many(event, event.pattern_match.group(1) or "")

    @bot.on(events.NewMessage(pattern=r"^/channels(?:@\w+)?$"))
    async def list_channels(event):
        if not authorized(event):
            return
        await show_channels(event)

    @bot.on(events.NewMessage(pattern=r"^/proofread_add(?:@\w+)?(?:\s|$)"))
    async def proofread_add_command(event):
        if not authorized(event):
            return
        # Start the guided two-step flow when the command has no arguments.
        # The legacy one-line form remains supported for backwards compatibility.
        if not event.raw_text.split(maxsplit=1)[1:]:
            pending_action[event.chat_id] = "proofreading_source"
            pending_proofreading_source.pop(event.chat_id, None)
            await event.reply(
                "1️⃣ شناسه یا لینک کانال مبدأ را بفرستید.\n"
                "مثال: <code>@source_channel</code>", parse_mode="html",
            )
        else:
            await add_proofreading_mapping(event, event.raw_text)

    @bot.on(events.NewMessage(pattern=r"^(?:📊 تحلیل ویژه|/special(?:@\w+)?)$"))
    async def special_analysis_entry(event):
        if not authorized(event):
            return
        await show_analysis_panel(event)

    @bot.on(events.NewMessage(pattern=r"^/special_add(?:@\w+)?(?:\s|$)"))
    async def special_analysis_add_command(event):
        if not authorized(event):
            return
        pending_action[event.chat_id] = "analysis_source"
        await event.reply("شناسه یا لینک کانال مبدأ را بفرستید.")

    @bot.on(events.CallbackQuery(data=b"analysis_add"))
    async def analysis_add_button(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید.", alert=True)
            return
        if len(db.analysis_channels()) >= 5:
            await event.answer("حداکثر ۵ کانال ثبت شده است.", alert=True)
            return
        pending_action[event.chat_id] = "analysis_source"
        await event.answer()
        await event.respond("۱) شناسه یا لینک کانال مبدأ را بفرستید.")

    @bot.on(events.CallbackQuery(pattern=rb"^analysis_remove:(.+)$"))
    async def analysis_remove_button(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید.", alert=True)
            return
        source = event.pattern_match.group(1).decode("utf-8")
        removed = db.remove_analysis_channel(source)
        await event.answer("حذف شد." if removed else "کانال پیدا نشد.")
        await show_analysis_panel(event)

    @bot.on(events.CallbackQuery(pattern=rb"^analysis_ai:(.+)$"))
    async def analysis_ai_button(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید.", alert=True)
            return
        source = event.pattern_match.group(1).decode("utf-8")
        row = next((item for item in db.analysis_channels()
                    if item["source_channel"].casefold() == source.casefold()), None)
        if row is None:
            await event.answer("کانال پیدا نشد.", alert=True)
            return
        enabled = not bool(row["ai_enabled"])
        if enabled and db.count_analysis_ai() >= 3:
            await event.answer("حداکثر ۳ گزارش هوشمند را می‌توانید فعال کنید.", alert=True)
            return
        db.set_analysis_ai(source, enabled)
        await event.answer("گزارش هوشمند فعال شد." if enabled else "گزارش هوشمند خاموش شد.")
        await show_analysis_panel(event)

    @bot.on(events.NewMessage(pattern=r"^/proofread_remove(?:@\w+)?(?:\s+(.+))?$"))
    async def proofread_remove_command(event):
        if not authorized(event):
            return
        requested = parse_channel_inputs("/add " + (event.pattern_match.group(1) or ""))
        if len(requested) != 1:
            await event.reply("فرمت درست: <code>/proofread_remove @source</code>", parse_mode="html")
            return
        removed = db.remove_proofreading_channel(requested[0])
        await event.reply("✅ باگ‌یابی این کانال حذف شد." if removed else "ℹ️ این کانال در فهرست نبود.", buttons=menu)

    @bot.on(events.NewMessage(pattern=r"^/proofread_channels(?:@\w+)?$"))
    async def proofread_channels_command(event):
        if authorized(event):
            await show_proofreading_channels(event)

    @bot.on(events.NewMessage(pattern=r"^(➕ افزودن کانال|➖ حذف کانال|📡 فهرست کانال‌ها|⚡️ گزارش سرعت|🔥 سوخت خبر|🛠️ باگ‌یاب کانال‌ها|✅ وضعیت پایش|⚙️ تنظیمات)$"))
    async def menu_action(event):
        if not authorized(event):
            return
        if event.raw_text == "➕ افزودن کانال":
            pending_action[event.chat_id] = "add"
            await event.reply("شناسه یا لینک همهٔ کانال‌ها را بفرستید؛ می‌توانید هرکدام را در یک خط بنویسید.")
        elif event.raw_text == "➖ حذف کانال":
            pending_action[event.chat_id] = "remove"
            await show_channels(event)
            await event.reply("شناسهٔ کانال‌هایی که باید حذف شوند را بفرستید.")
        elif event.raw_text == "📡 فهرست کانال‌ها":
            await show_channels(event)
        elif event.raw_text == "⚡️ گزارش سرعت":
            await event.reply("⏳ گزارش سرعت در حال آماده‌سازی است؛ ممکن است در چند پیام ارسال شود.")
            start, end = day_window(config)
            await send_report(start, end)
        elif event.raw_text == "🔥 سوخت خبر":
            await event.reply("⏳ گزارش سوخت خبر در حال آماده‌سازی است.")
            start, end = day_window(config)
            await send_missed_report(start, end)
        elif event.raw_text == "🛠️ باگ‌یاب کانال‌ها":
            await show_proofreading_panel(event)
        elif event.raw_text == "✅ وضعیت پایش":
            await event.reply(f"✅ پایش فعال است. تعداد کانال‌ها: {len(channel_names())}", buttons=menu)
        else:
            current_hour = db.get_int_setting("daily_report_hour", config.daily_report_hour)
            weekly_ai_hour = db.get_int_setting("weekly_ai_report_hour", 22)
            time_buttons = [
                [Button.inline("۲۰:۰۰", b"report_hour:20"), Button.inline("۲۱:۰۰", b"report_hour:21")],
                [Button.inline("۲۲:۰۰", b"report_hour:22"), Button.inline("۲۳:۰۰", b"report_hour:23")],
                [Button.inline("۰۰:۰۰", b"report_hour:0")],
                [Button.inline("🧠 گزارش هفتگی AI: ۲۰", b"weekly_ai_hour:20"),
                 Button.inline("۲۱", b"weekly_ai_hour:21"),
                 Button.inline("۲۲", b"weekly_ai_hour:22"),
                 Button.inline("۲۳", b"weekly_ai_hour:23")],
                [Button.inline("🧠 گزارش هفتگی AI: ۰۰", b"weekly_ai_hour:0")],
                [Button.inline("🚨 فهرست پایش وایرال", b"viral_channels")],
                [Button.inline("🧮 فرمول تشخیص وایرال", b"viral_formula")],
                [Button.inline("📣 تغییر کانال محتوای پربازخورد", b"change_viral_output_channel")],
            ]
            if db.is_owner(event.chat_id):
                time_buttons.insert(-2, [Button.inline("🎚 قوانین پایش", b"monitor_rules")])
                time_buttons.insert(-2, [Button.inline("⚡ فرمول امتیاز سرعت", b"speed_scoring")])
                time_buttons.insert(-2, [Button.inline("🧩 فرمول تشخیص خبر مشابه", b"matching_formula")])
            time_buttons.append([back_to_menu_button()])
            current_viral_output = db.get_setting("viral_output_chat_title") or db.get_setting("viral_output_chat_id") or "تنظیم نشده"
            await event.reply(
                f"⏰ زمان گزارش شبانه: {current_hour:02d}:00\n"
                f"🧠 زمان گزارش هفتگی AI (جمعه): {weekly_ai_hour:02d}:00\n"
                f"📣 کانال محتوای پربازخورد: {current_viral_output}\n"
                "گزینه موردنظر را انتخاب کنید:",
                buttons=time_buttons,
            )

    @bot.on(events.CallbackQuery(data=b"channels_add"))
    async def channels_add_button(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        pending_action[event.chat_id] = "add"
        await event.answer()
        await event.respond("شناسه یا لینک کانال‌ها را بفرستید؛ هر کانال را می‌توانید در یک خط بنویسید.")

    @bot.on(events.CallbackQuery(data=b"channels_remove"))
    async def channels_remove_button(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        pending_action[event.chat_id] = "remove"
        await event.answer()
        await event.respond("شناسهٔ کانال‌هایی که باید حذف شوند را بفرستید.")

    @bot.on(events.CallbackQuery(data=b"change_viral_output_channel"))
    async def change_viral_output_channel(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        pending_action[event.chat_id] = "viral_output"
        await event.answer()
        await event.respond(
            "📣 لینک یا شناسهٔ کانال مقصد محتوای پربازخورد را بفرستید.\n"
            "ابتدا ربات را در کانال ادمین کنید تا بتواند هشدارها را ارسال کند."
        )

    @bot.on(events.CallbackQuery(data=b"proofreading_add"))
    async def proofreading_add_button(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        pending_action[event.chat_id] = "proofreading_source"
        pending_proofreading_source.pop(event.chat_id, None)
        await event.answer()
        await event.respond(
            "1️⃣ شناسه یا لینک کانال مبدأ را بفرستید.\n"
            "مثال: <code>@khabarfarda</code>", parse_mode="html",
        )

    @bot.on(events.CallbackQuery(data=b"proofreading_list"))
    async def proofreading_list_button(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        await event.answer()
        await show_proofreading_panel(event)

    @bot.on(events.CallbackQuery(pattern=rb"^proofreading_remove:(.+)$"))
    async def proofreading_remove_button(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        source = event.pattern_match.group(1).decode("utf-8")
        removed = db.remove_proofreading_channel(source)
        await event.answer("حذف شد" if removed else "در فهرست نبود")
        await show_proofreading_panel(event)

    @bot.on(events.CallbackQuery(pattern=rb"^report_hour:(\d{1,2})$"))
    async def set_report_hour(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        hour = int(event.pattern_match.group(1))
        if not 0 <= hour <= 23:
            await event.answer("زمان نامعتبر است", alert=True)
            return
        db.set_setting("daily_report_hour", str(hour))
        await event.answer("ذخیره شد")
        await event.edit(f"✅ گزارش شبانه از ساعت ۸ صبح تا {hour:02d}:00 محاسبه و همان موقع ارسال می‌شود.")
        await bot.send_message(event.chat_id, "تنظیمات ذخیره شد.", buttons=menu)

    @bot.on(events.CallbackQuery(pattern=rb"^weekly_ai_hour:(\d{1,2})$"))
    async def set_weekly_ai_hour(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        hour = int(event.pattern_match.group(1))
        if not 0 <= hour <= 23:
            await event.answer("زمان نامعتبر است", alert=True)
            return
        db.set_setting("weekly_ai_report_hour", str(hour))
        await event.answer("ذخیره شد")
        await event.edit(f"✅ گزارش هوش مصنوعی هفتگی، جمعه‌ها ساعت {hour:02d}:00 ارسال می‌شود.")
        await bot.send_message(event.chat_id, "تنظیمات ذخیره شد.", buttons=menu)

    @bot.on(events.CallbackQuery(pattern=rb"^speed_details:(\d+):(\d+):(\d+)$"))
    async def speed_details(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        start_ts, end_ts, requested_page = map(int, event.pattern_match.groups())
        start = datetime.fromtimestamp(start_ts, config.timezone)
        end = datetime.fromtimestamp(end_ts, config.timezone)
        speed_min, missed_min, missed_max, grace_minutes = monitoring_rules()
        rank_weight, time_cap, confidence_k = speed_scoring_settings()
        pages = build_speed_detail_pages(
            db, channel_names(), start, end, config.timezone,
            config.important_min_channels, grace_minutes, 4,
            speed_min, missed_max, rank_weight, time_cap, confidence_k, missed_min,
        )
        page = min(requested_page, len(pages) - 1)
        navigation = []
        if page > 0:
            navigation.append(Button.inline("◀️ قبلی", f"speed_details:{start_ts}:{end_ts}:{page-1}".encode()))
        navigation.append(Button.inline(f"{page+1}/{len(pages)}", b"page_noop"))
        if page + 1 < len(pages):
            navigation.append(Button.inline("بعدی ▶️", f"speed_details:{start_ts}:{end_ts}:{page+1}".encode()))
        back = Button.inline("🏆 بازگشت به رتبه‌بندی", f"speed_summary:{start_ts}:{end_ts}".encode())
        await event.answer()
        await event.edit(
            pages[page], parse_mode="html", link_preview=False,
            buttons=[navigation, [back], [back_to_menu_button()]],
        )

    @bot.on(events.CallbackQuery(pattern=rb"^speed_summary:(\d+):(\d+)$"))
    async def speed_summary(event):
        start_ts, end_ts = map(int, event.pattern_match.groups())
        start = datetime.fromtimestamp(start_ts, config.timezone)
        end = datetime.fromtimestamp(end_ts, config.timezone)
        speed_min, missed_min, missed_max, grace_minutes = monitoring_rules()
        rank_weight, time_cap, confidence_k = speed_scoring_settings()
        report = build_report(db, channel_names(), start, end, config.timezone,
                              config.important_min_channels, grace_minutes, "speed",
                              speed_min, missed_max, rank_weight, time_cap, confidence_k, missed_min)
        details = Button.inline("📰 جزئیات گزارش", f"speed_details:{start_ts}:{end_ts}:0".encode())
        await event.answer()
        await event.edit(
            report, parse_mode="html", link_preview=False,
            buttons=[[details], [back_to_menu_button()]],
        )

    @bot.on(events.CallbackQuery(data=b"page_noop"))
    async def page_noop(event):
        await event.answer()

    @bot.on(events.CallbackQuery(pattern=rb"^mc:(\d+):(\d+):(.+)$"))
    async def missed_channel_details(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        start_raw, end_raw, channel_raw = event.pattern_match.groups()
        start = datetime.fromtimestamp(int(start_raw), config.timezone)
        end = datetime.fromtimestamp(int(end_raw), config.timezone)
        channel = channel_raw.decode()
        channels = channel_names()
        if channel not in channels:
            await event.answer("این کانال دیگر در فهرست پایش نیست", alert=True)
            return
        speed_min, missed_min, missed_max, grace_minutes = monitoring_rules()
        report = build_missed_channel_report(
            db, channels, channel, start, end, config.timezone,
            config.important_min_channels, grace_minutes,
            speed_min, missed_max, missed_min,
        )
        await event.answer()
        for chunk in split_html(report):
            await event.respond(chunk, parse_mode="html", link_preview=False)

    @bot.on(events.CallbackQuery(data=b"monitor_rules"))
    async def show_monitor_rules(event):
        if not authorized(event) or not db.is_owner(event.chat_id):
            await event.answer("فقط مالک می‌تواند این تنظیمات را مدیریت کند.", alert=True)
            return
        await event.answer()
        await event.edit(monitoring_rules_text(), parse_mode="html", buttons=monitoring_rules_buttons())

    def matching_formula_text() -> str:
        threshold, min_words, fragment_threshold, numeric_boost = matching_settings()
        return (
            "🧩 <b>فرمول تشخیص خبر مشابه</b>\n\n"
            f"آستانه شباهت پایه: <b>{threshold:.2f}</b>\n"
            f"بازه بررسی خبرهای مشابه: <b>{fa(NEWS_MATCH_LOOKBACK_HOURS)} ساعت</b>\n"
            f"حداقل طول برش مشترک: <b>{fa(min_words)} کلمه</b>\n"
            f"آستانه امتیاز برش: <b>{fragment_threshold:.2f}</b>\n"
            f"تقویت تطابق عددی: <b>{numeric_boost:.2f}</b>\n\n"
            "این مقادیر روی گزارش سرعت، سوخت خبر و باگ‌یاب همه کاربران اعمال می‌شود."
        )

    def matching_formula_buttons():
        return with_back([
            [Button.inline("➖ آستانه پایه", b"match_adjust:threshold:-0.02"),
             Button.inline("➕ آستانه پایه", b"match_adjust:threshold:0.02")],
            [Button.inline("➖ طول برش", b"match_adjust:min_words:-1"),
             Button.inline("➕ طول برش", b"match_adjust:min_words:1")],
            [Button.inline("➖ آستانه برش", b"match_adjust:fragment:-0.02"),
             Button.inline("➕ آستانه برش", b"match_adjust:fragment:0.02")],
            [Button.inline("➖ تقویت عددی", b"match_adjust:numeric:-0.02"),
             Button.inline("➕ تقویت عددی", b"match_adjust:numeric:0.02")],
            [Button.inline("↩️ بازنشانی", b"match_adjust:reset:0")],
        ])

    @bot.on(events.CallbackQuery(data=b"matching_formula"))
    async def show_matching_formula(event):
        if not authorized(event) or not db.is_owner(event.chat_id):
            await event.answer("فقط مالک می‌تواند این تنظیمات را مدیریت کند.", alert=True)
            return
        await event.answer()
        await event.edit(matching_formula_text(), parse_mode="html", buttons=matching_formula_buttons())

    @bot.on(events.CallbackQuery(pattern=rb"^match_adjust:(threshold|min_words|fragment|numeric|reset):(-?(?:0\.02|1|0))$"))
    async def adjust_matching_formula(event):
        if not authorized(event) or not db.is_owner(event.chat_id):
            await event.answer("فقط مالک می‌تواند این تنظیمات را مدیریت کند.", alert=True)
            return
        kind = event.pattern_match.group(1).decode()
        delta = float(event.pattern_match.group(2))
        if kind == "reset":
            for key, value in {
                "similarity_threshold": str(config.similarity_threshold),
                "match_window_hours": str(NEWS_MATCH_LOOKBACK_HOURS),
                "match_fragment_min_words": "4",
                "match_fragment_threshold": "0.78",
                "match_numeric_boost": "0.12",
            }.items():
                db.set_global_setting(key, value)
        elif kind == "threshold":
            value = min(0.95, max(0.20, float(db.get_global_setting("similarity_threshold", str(config.similarity_threshold))) + delta))
            db.set_global_setting("similarity_threshold", f"{value:.2f}")
        elif kind == "min_words":
            value = min(20, max(3, db.get_global_int_setting("match_fragment_min_words", 4) + int(delta)))
            db.set_global_setting("match_fragment_min_words", str(value))
        elif kind == "fragment":
            value = min(1.0, max(0.50, float(db.get_global_setting("match_fragment_threshold", "0.78")) + delta))
            db.set_global_setting("match_fragment_threshold", f"{value:.2f}")
        elif kind == "numeric":
            value = min(0.30, max(0.0, float(db.get_global_setting("match_numeric_boost", "0.12")) + delta))
            db.set_global_setting("match_numeric_boost", f"{value:.2f}")
        await event.answer("فرمول سراسری ذخیره شد.")
        await event.edit(matching_formula_text(), parse_mode="html", buttons=matching_formula_buttons())

    def speed_scoring_buttons():
        return with_back([
            [Button.inline("➖ وزن رتبه", b"speed_score:rank:-10"), Button.inline("➕ وزن رتبه", b"speed_score:rank:10")],
            [Button.inline("➖ سقف زمان", b"speed_score:cap:-5"), Button.inline("➕ سقف زمان", b"speed_score:cap:5")],
            [Button.inline("➖ اعتبار", b"speed_score:confidence:-1"), Button.inline("➕ اعتبار", b"speed_score:confidence:1")],
            [Button.inline("↩️ بازنشانی فرمول پیشنهادی", b"speed_score:reset:0")],
        ])

    def speed_scoring_text() -> str:
        rank_weight, time_cap, confidence_k = speed_scoring_settings()
        return (
            "⚡ <b>فرمول امتیاز سرعت</b>\n\n"
            f"وزن رتبه: <b>{fa(rank_weight)}٪</b>\n"
            f"وزن زمان: <b>{fa(100-rank_weight)}٪</b>\n"
            f"سقف تأخیر: <b>{fa(time_cap)} دقیقه</b>\n"
            f"ضریب اعتبار نمونه: <b>{fa(confidence_k)}</b>\n\n"
            "امتیاز هر خبر = وزن رتبه × امتیاز رتبه + وزن زمان × امتیاز زمان\n"
            "امتیاز نهایی = میانگین امتیاز خبرها × تعداد نمونه ÷ (تعداد نمونه + ضریب اعتبار)"
        )

    @bot.on(events.CallbackQuery(data=b"speed_scoring"))
    async def show_speed_scoring(event):
        if not authorized(event) or not db.is_owner(event.chat_id):
            await event.answer("فقط مالک می‌تواند این تنظیمات را مدیریت کند.", alert=True)
            return
        await event.answer()
        await event.edit(speed_scoring_text(), parse_mode="html", buttons=speed_scoring_buttons())

    @bot.on(events.CallbackQuery(pattern=rb"^speed_score:(rank|cap|confidence|reset):(-?\d+)$"))
    async def adjust_speed_scoring(event):
        if not authorized(event) or not db.is_owner(event.chat_id):
            await event.answer("فقط مالک می‌تواند این تنظیمات را مدیریت کند.", alert=True)
            return
        kind = event.pattern_match.group(1).decode()
        delta = int(event.pattern_match.group(2))
        if kind == "reset":
            db.set_global_setting("speed_rank_weight", "55")
            db.set_global_setting("speed_time_cap_minutes", "45")
            db.set_global_setting("speed_confidence_k", "8")
        elif kind == "rank":
            value = min(90, max(10, db.get_global_int_setting("speed_rank_weight", 55) + delta))
            db.set_global_setting("speed_rank_weight", str(value))
        elif kind == "cap":
            value = min(120, max(5, db.get_global_int_setting("speed_time_cap_minutes", 45) + delta))
            db.set_global_setting("speed_time_cap_minutes", str(value))
        else:
            value = min(30, max(0, db.get_global_int_setting("speed_confidence_k", 8) + delta))
            db.set_global_setting("speed_confidence_k", str(value))
        await event.answer("فرمول ذخیره شد")
        await event.edit(speed_scoring_text(), parse_mode="html", buttons=speed_scoring_buttons())

    @bot.on(events.CallbackQuery(data=b"viral_channels"))
    async def viral_channels_settings(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        buttons = [
            [Button.inline("➕ افزودن کانال", b"viral_channels_add")],
            [Button.inline("➖ حذف کانال", b"viral_channels_remove")],
            [Button.inline("📋 نمایش فهرست", b"viral_channels_list")],
        ]
        buttons = with_back(buttons)
        await event.answer()
        await event.edit(
            f"🚨 <b>فهرست اختصاصی پایش وایرال</b>\n\nتعداد کانال‌ها: <b>{fa(len(viral_channel_names()))}</b>\n"
            "پایش ۵، ۱۰ و ۲۰ دقیقه‌ای فقط روی کانال‌های این فهرست انجام می‌شود.",
            parse_mode="html", buttons=buttons,
        )

    def viral_formula_buttons():
        return with_back([
            [Button.inline("➖ سهم ری‌اکشن", b"viral_adjust:weight:-5"),
             Button.inline("➕ سهم ری‌اکشن", b"viral_adjust:weight:5")],
            [Button.inline("➖ آستانه وایرال", b"viral_adjust:threshold:-1"),
             Button.inline("➕ آستانه وایرال", b"viral_adjust:threshold:1")],
            [Button.inline("↩️ بازنشانی فرمول", b"viral_adjust:reset:0")],
        ])

    def viral_formula_text() -> str:
        weight = db.get_int_setting("viral_reaction_weight", 40)
        threshold_5 = db.get_int_setting("viral_threshold_5", 25) / 10
        threshold_other = db.get_int_setting("viral_threshold_other", 20) / 10
        return (
            "🧮 <b>فرمول تشخیص وایرال</b>\n\n"
            f"سهم ری‌اکشن: <b>{fa(weight)}٪</b> | سهم فوروارد: <b>{fa(100-weight)}٪</b>\n"
            f"آستانه مرحلهٔ ۵ دقیقه: <b>{fa(f'{threshold_5:.1f}')} برابر</b>\n"
            f"آستانه مراحل ۱۰ و ۲۰ دقیقه: <b>{fa(f'{threshold_other:.1f}')} برابر</b>\n\n"
            "با دکمه‌های زیر سهم ری‌اکشن و حساسیت تشخیص را کم یا زیاد کنید."
        )

    @bot.on(events.CallbackQuery(data=b"viral_formula"))
    async def viral_formula_settings(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        await event.answer()
        await event.edit(viral_formula_text(), parse_mode="html", buttons=viral_formula_buttons())
        return
        await event.edit(
            "🧮 <b>فرمول تشخیص محتوای وایرال</b>\n\n"
            "تا قبل از جمع‌شدن ۳۰ نمونهٔ هم‌سن از همان رسانه، هشداری صادر نمی‌شود.\n\n"
            "امتیاز انتخاب: ۴۰٪ صدک ری‌اکشن + ۶۰٪ صدک فوروارد در ۶۰ پست اخیر.\n"
            "دقیقهٔ ۵: امتیاز حداقل ۹۴ و یک سیگنال در صدک ۹۷؛ دقیقهٔ ۱۰ و ۲۰: امتیاز ۹۲ و صدک ۹۵.\n\n"
            "فوروارد وزن بیشتری دارد؛ شاخص غیرفعال هر رسانه خودکار از فرمول حذف می‌شود.",
            parse_mode="html",
        )

    @bot.on(events.CallbackQuery(pattern=rb"^viral_adjust:(weight|threshold|reset):(-?\d+)$"))
    async def adjust_viral_formula(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید.", alert=True)
            return
        kind = event.pattern_match.group(1).decode()
        delta = int(event.pattern_match.group(2))
        if kind == "reset":
            db.set_setting("viral_reaction_weight", "40")
            db.set_setting("viral_threshold_5", "25")
            db.set_setting("viral_threshold_other", "20")
        elif kind == "weight":
            value = min(90, max(10, db.get_int_setting("viral_reaction_weight", 40) + delta))
            db.set_setting("viral_reaction_weight", str(value))
        else:
            value = min(40, max(10, db.get_int_setting("viral_threshold_5", 25) + delta * 2))
            db.set_setting("viral_threshold_5", str(value))
            other = min(40, max(10, db.get_int_setting("viral_threshold_other", 20) + delta * 2))
            db.set_setting("viral_threshold_other", str(other))
        await event.answer("فرمول ذخیره شد.")
        await event.edit(viral_formula_text(), parse_mode="html", buttons=viral_formula_buttons())

    @bot.on(events.CallbackQuery(pattern=rb"^viral_channels_(add|remove|list)$"))
    async def viral_channels_action(event):
        if not authorized(event):
            await event.answer("دسترسی ندارید", alert=True)
            return
        action = event.pattern_match.group(1).decode()
        await event.answer()
        if action == "list":
            await show_viral_channels(event)
        elif action == "add":
            pending_action[event.chat_id] = "viral_add"
            await event.respond("شناسه یا لینک کانال‌های موردنظر برای پایش وایرال را بفرستید؛ هرکدام می‌تواند در یک خط باشد.")
        else:
            pending_action[event.chat_id] = "viral_remove"
            await show_viral_channels(event)
            await event.respond("شناسه یا لینک کانال‌هایی که باید از فهرست وایرال حذف شوند را بفرستید.")

    @bot.on(events.CallbackQuery(pattern=rb"^adjust_rule:(speed|missed_min|grace):(-?(?:1|5))$"))
    async def adjust_monitor_rule(event):
        if not authorized(event) or not db.is_owner(event.chat_id):
            await event.answer("فقط مالک می‌تواند این تنظیمات را مدیریت کند.", alert=True)
            return
        kind = event.pattern_match.group(1).decode()
        delta = int(event.pattern_match.group(2))
        count = max(2, len(channel_names()))
        speed_min, missed_min, missed_max, grace_minutes = monitoring_rules()
        if kind == "speed":
            speed_min = min(count, max(missed_min + 1, speed_min + delta))
            db.set_global_setting("speed_rule_mode", "fixed")
            db.set_global_setting("speed_min_channels", str(speed_min))
        elif kind == "missed_min":
            missed_min = min(missed_max, max(2, missed_min + delta))
            db.set_global_setting("missed_min_publishers", str(missed_min))
        else:
            grace_minutes = min(180, max(5, grace_minutes + delta))
            db.set_global_setting("miss_grace_minutes", str(grace_minutes))
        await event.answer("ذخیره شد")
        await event.edit(monitoring_rules_text(), parse_mode="html", buttons=monitoring_rules_buttons())

    @bot.on(events.CallbackQuery(pattern=rb"^speed_formula:(half_plus_one|two_thirds|all_minus_two|fixed)$"))
    async def set_speed_formula(event):
        if not authorized(event) or not db.is_owner(event.chat_id):
            await event.answer("فقط مالک می‌تواند این تنظیمات را مدیریت کند.", alert=True)
            return
        mode = event.pattern_match.group(1).decode()
        db.set_global_setting("speed_rule_mode", mode)
        speed_min, missed_min, missed_max, grace_minutes = monitoring_rules()
        if mode == "fixed":
            db.set_global_setting("speed_min_channels", str(speed_min))
        await event.answer("فرمول ذخیره شد")
        await event.edit(monitoring_rules_text(), parse_mode="html", buttons=monitoring_rules_buttons())

    @bot.on(events.NewMessage)
    async def pending_input(event):
        menu_labels = {"➕ افزودن کانال", "➖ حذف کانال", "📡 فهرست کانال‌ها", "⚡️ گزارش سرعت", "🔥 سوخت خبر", "🛠️ باگ‌یاب کانال‌ها", "✅ وضعیت پایش", "⚙️ تنظیمات"}
        if not authorized(event) or event.raw_text.startswith("/") or event.raw_text in menu_labels:
            return
        action = pending_action.get(event.chat_id)
        if action == "add":
            pending_action.pop(event.chat_id, None)
            await add_many(event, event.raw_text)
        elif action == "remove":
            pending_action.pop(event.chat_id, None)
            await remove_many(event, event.raw_text)
        elif action == "viral_output":
            pending_action.pop(event.chat_id, None)
            await set_viral_output_channel(event, event.raw_text)
        elif action == "viral_add":
            pending_action.pop(event.chat_id, None)
            await add_viral_many(event, event.raw_text)
        elif action == "viral_remove":
            pending_action.pop(event.chat_id, None)
            await remove_viral_many(event, event.raw_text)
        elif action == "analysis_source":
            requested = parse_channel_inputs("/add " + event.raw_text)
            if len(requested) != 1:
                await event.reply("فقط یک کانال مبدأ را ارسال کنید.")
                return
            try:
                entity = await user.get_entity(requested[0])
                if not getattr(entity, "broadcast", False):
                    await event.reply("این شناسه مربوط به کانال نیست.")
                    return
                source = f"@{entity.username}" if entity.username else str(utils.get_peer_id(entity))
                pending_analysis_source[event.chat_id] = source
                pending_action[event.chat_id] = "analysis_destination"
                await event.reply(
                    f"✅ مبدأ: {source}\n\nحالا لینک یا شناسهٔ کانال خصوصی مقصد را بفرستید. "
                    "ربات باید در آن کانال اجازهٔ ارسال پست داشته باشد."
                )
            except Exception:
                await event.reply("کانال مبدأ پیدا نشد یا قابل دسترسی نیست.")
        elif action == "analysis_destination":
            source = pending_analysis_source.get(event.chat_id)
            if not source:
                pending_action.pop(event.chat_id, None)
                await event.reply("فرایند منقضی شده است.")
                return
            pending_action.pop(event.chat_id, None)
            pending_analysis_source.pop(event.chat_id, None)
            await add_analysis_mapping(event, source, event.raw_text.strip())
        elif action == "proofreading_source":
            requested = parse_channel_inputs("/add " + event.raw_text)
            if len(requested) != 1:
                await event.reply("فقط شناسه یا لینک یک کانال مبدأ را بفرستید.")
                return
            try:
                entity = await user.get_entity(requested[0])
                if not getattr(entity, "broadcast", False):
                    await event.reply("❌ این شناسه مربوط به کانال نیست؛ دوباره ارسال کنید.")
                    return
                source = f"@{entity.username}" if entity.username else str(utils.get_peer_id(entity))
                pending_proofreading_source[event.chat_id] = source
                pending_action[event.chat_id] = "proofreading_destination"
                await event.reply(
                    f"✅ مبدأ: {source}\n\n"
                    "2️⃣ حالا لینک دعوت کانال خصوصیِ مخصوص گزارش این کانال را بفرستید.\n"
                    "مثال: <code>https://t.me/+PrivateInvite</code>", parse_mode="html",
                )
            except Exception:
                log.exception("cannot resolve proofreading source %s", requested[0])
                await event.reply("❌ کانال پیدا نشد یا با حساب متصل قابل مشاهده نیست؛ دوباره ارسال کنید.")
        elif action == "proofreading_destination":
            source = pending_proofreading_source.get(event.chat_id)
            if not source:
                pending_action.pop(event.chat_id, None)
                await event.reply("فرایند تنظیم منقضی شده است؛ دوباره دکمهٔ تنظیم کانال جدید را بزنید.", buttons=menu)
                return
            pending_action.pop(event.chat_id, None)
            pending_proofreading_source.pop(event.chat_id, None)
            await add_proofreading_mapping(event, f"/proofread_add {source} {event.raw_text.strip()}")

    async def scheduler():
        last_sent = None
        last_weekly_analysis_sent = None
        while True:
            now = datetime.now(config.timezone)
            report_hour = db.get_int_setting("daily_report_hour", config.daily_report_hour)
            weekly_ai_hour = db.get_int_setting("weekly_ai_report_hour", 22)
            if now.hour == report_hour and now.minute == 0 and last_sent != now.date():
                report_day = now.date() - timedelta(days=1) if report_hour <= config.day_start_hour else now.date()
                start = datetime.combine(report_day, time(config.day_start_hour), config.timezone)
                end = datetime.combine(now.date(), time(report_hour), config.timezone)
                await send_report(start, end)
                await send_missed_report(start, end)
                last_sent = now.date()
                # The special-channel leaderboard is mechanical and belongs
                # to the nightly report, separate from the weekly AI report.
                for mapping in db.all_analysis_channels():
                    try:
                        if mapping["user_id"] is not None:
                            db.use_user(mapping["user_id"])
                        start_nightly, end_nightly = day_window(config)
                        await send_special_nightly_leaderboard(
                            start_nightly, end_nightly,
                            mapping["source_channel"],
                            mapping["source_title"],
                            mapping["destination_chat_id"],
                        )
                    except Exception:
                        log.exception("special nightly report failed for %s", mapping["source_channel"])
                db.use_user(db.owner_id)
            if (now.weekday() == 4 and now.hour == weekly_ai_hour and now.minute == 0
                    and last_weekly_analysis_sent != now.date()):
                end = now.replace(second=0, microsecond=0)
                start = end - timedelta(days=7)
                mappings = db.all_analysis_channels()
                for mapping in mappings:
                    try:
                        if not mapping["ai_enabled"]:
                            continue
                        if mapping["user_id"] is not None:
                            db.use_user(mapping["user_id"])
                        await send_engagement_report(
                            start, end,
                            channel_override=mapping["source_channel"],
                            channel_title_override=mapping["source_title"],
                            ai_enabled=bool(mapping["ai_enabled"]),
                            recipient_chat=mapping["destination_chat_id"],
                        )
                    except Exception:
                        log.exception("special analysis failed for %s", mapping["source_channel"])
                db.use_user(db.owner_id)
                last_weekly_analysis_sent = now.date()
            await asyncio.sleep(20)

    async def poll_channels():
        while True:
            start, _ = day_window(config)
            start_utc = start.astimezone(timezone.utc)
            for watched in db.all_channels():
                channel = watched["channel"]
                try:
                    chat = await user.get_entity(channel)
                    newest = watched["last_message_id"]
                    pending = []
                    async for message in user.iter_messages(chat, min_id=watched["last_message_id"]):
                        if message.date < start_utc:
                            break
                        pending.append(message)
                    for message in reversed(pending):
                        await store_message(message, chat)
                        newest = max(newest, message.id)
                    if newest:
                        db.update_channel_cursor(channel, newest)
                except Exception:
                    log.exception("poll failed for %s", channel)
            await asyncio.sleep(config.poll_interval_seconds)

    async def poll_proofreading_channels():
        while True:
            for watched in db.all_proofreading_channels():
                source = watched["source_channel"]
                newest = watched["last_message_id"]
                try:
                    db.use_user(watched["user_id"])
                    source_chat = await user.get_entity(source)
                    destination = await user.get_entity(watched["destination_chat_id"])
                    backfill_start_utc = proofreading_backfill_start(
                        watched["added_at"], config.timezone, config.day_start_hour,
                    ).astimezone(timezone.utc)
                    pending = []
                    async for message in user.iter_messages(source_chat, min_id=watched["last_message_id"]):
                        if message.date and message.date < backfill_start_utc:
                            break
                        pending.append(message)
                    for message in reversed(pending):
                        newest = max(newest, message.id)
                        raw_text = message.raw_text or ""
                        issues = proofread(raw_text)
                        published = message.date or datetime.now(timezone.utc)
                        normalized = signature(raw_text)
                        duplicate = None
                        if len(normalized) >= 20:
                            candidates = db.recent_proofreading_posts(
                                published - timedelta(hours=NEWS_MATCH_LOOKBACK_HOURS)
                            )
                            best_score = 0.0
                            for candidate in candidates:
                                if candidate["source_channel"].lower() == source.lower() and candidate["message_id"] == message.id:
                                    continue
                                matched, score = match_news(normalized, candidate["normalized"])
                                if matched and score > best_score:
                                    duplicate, best_score = candidate, score
                        # Persist every post so duplicate checks work even when no spelling issue exists.
                        chat_username = getattr(source_chat, "username", None)
                        link = (f"https://t.me/{chat_username}/{message.id}" if chat_username
                                else f"https://t.me/c/{str(getattr(source_chat, 'id', '')).removeprefix('-100')}/{message.id}")
                        db.add_proofreading_post(source, message.id, published, raw_text, normalized, link)
                        if not issues and not duplicate:
                            continue
                        forwarded = await user.forward_messages(destination, message)
                        forwarded_message = forwarded[0] if isinstance(forwarded, (list, tuple)) else forwarded
                        if issues:
                            for chunk in split_html(format_issues(issues), 3900):
                                await user.send_message(
                                    destination, chunk, parse_mode="html",
                                    reply_to=forwarded_message.id, link_preview=False,
                                )
                        if duplicate:
                            alert = (
                                "♻️ <b>هشدار پست تکراری</b>\n"
                                f"این محتوا پیش‌تر در <b>{escape(duplicate['source_channel'])}</b> منتشر شده است.\n"
                                f'<a href="{escape(duplicate["link"], quote=True)}">مشاهده نسخهٔ قبلی</a>'
                            )
                            await user.send_message(
                                destination, alert, parse_mode="html",
                                reply_to=forwarded_message.id, link_preview=False,
                            )
                        log.info("bug finder report sent for %s/%s (issues=%s duplicate=%s)",
                                 source, message.id, len(issues), bool(duplicate))
                    if newest:
                        db.update_proofreading_cursor(source, newest, watched["user_id"])
                except Exception:
                    log.exception("proofreading poll failed for %s", source)
            await asyncio.sleep(config.proofreading_interval_seconds)

    async def viral_scanner():
        while True:
            start, end = day_window(config)
            try:
                await ensure_user_connected()
                await scan_and_send_viral(start, end)
            except Exception:
                log.exception("viral scan failed")
            await asyncio.sleep(max(60, config.viral_scan_interval_seconds))

    log.info("starting bot independently from the channel collector")
    async def run_client_forever(client, bot_token=None, name="telegram"):
        """Keep a client alive; Telethon returns from run_until_disconnected on drops."""
        delay = 2
        while True:
            try:
                await start_with_retry(client, bot_token)
                delay = 2
                await client.run_until_disconnected()
                log.warning("%s client disconnected; reconnecting", name)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError):
                log.exception("%s client connection failed", name)
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)

    async def run_collector_client():
        await run_client_forever(user, name="collector")

    await start_with_retry(bot, config.bot_token)
    log.info("management bot connected; collector may reconnect independently")
    try:
        await asyncio.gather(
            run_collector_client(),
            run_client_forever(bot, config.bot_token, name="management bot"),
            scheduler(),
            poll_channels(),
            poll_proofreading_channels(),
            viral_scanner(),
        )
    finally:
        # Cancellation during a controlled restart should not leave SQLite
        # WAL handles or Telegram sessions open.
        db.close()
        for client in (user, bot):
            if client.is_connected():
                await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
