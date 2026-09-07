import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from news_monitor.database import Database
from news_monitor.app import (
    evaluate_viral_post, find_similar_viral_alert, parse_channel_inputs,
    parse_proofreading_pair, proofreading_backfill_start,
)
from news_monitor.proofreading import proofread
from news_monitor.report import (
    build_engagement_leaderboard, build_engagement_report, build_engagement_topic_analysis,
    build_missed_channel_report, build_missed_overview,
    build_report, build_speed_detail_pages, gregorian_to_jalali, split_html, _speed_time_score,
)
from news_monitor.textmatch import (
    _number_mentions, _shared_fragment_score, duplicate_match, news_match, normalize,
    signature, similarity,
)


class MatchingTests(unittest.TestCase):
    def test_proofreading_pair_and_high_confidence_issues(self):
        self.assertEqual(
            parse_proofreading_pair("/proofread_add @source https://t.me/+secretHash"),
            ("@source", "https://t.me/+secretHash"),
        )
        issues = proofread("این اتفاق رخ داد، ولی هنوز مسول و مسول تایید نکرده‌اند.")
        suggestions = {issue.suggestion for issue in issues}
        self.assertIn("مسئول", suggestions)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].count, 2)

    def test_proofreading_ignores_style_and_punctuation_preferences(self):
        issues = proofread("این اتفاق احتمالا رخ می دهد،ولی خبر تایید شد ...")
        self.assertEqual([issue.suggestion for issue in issues], ["می‌دهد"])
        self.assertEqual(proofread("درمان‌های نوین و موثر بررسی شدند."), [])

    def test_proofreading_catches_safe_half_spaces_without_names(self):
        issues = proofread("تحریم های تازه اعلام شد و دولت می تواند پاسخ دهد.")
        self.assertEqual({issue.suggestion for issue in issues}, {"تحریم‌های", "می‌تواند"})
        self.assertEqual(proofread("از ماه می تاکنون نیروگاه می دونگ فعال است."), [])

    def test_proofreading_catches_common_persian_orthography_misses(self):
        issues = proofread("این خبر احتمالن درست است و راجب آن میتونم توضیح بدهم؛ او نمیشه بیاید.")
        suggestions = {issue.suggestion for issue in issues}
        self.assertIn("راجع", suggestions)
        self.assertIn("می‌توانم", suggestions)
        self.assertIn("نمی‌شود", suggestions)

    def test_proofreading_backfills_from_eight_on_add_day(self):
        tz = ZoneInfo("Asia/Tehran")
        added_at = datetime(2026, 8, 21, 12, 45, tzinfo=timezone.utc).isoformat()
        start = proofreading_backfill_start(added_at, tz)
        self.assertEqual((start.year, start.month, start.day, start.hour), (2026, 8, 21, 8))
        self.assertEqual(start.tzinfo, tz)
    def test_multiple_channel_input_formats(self):
        text = "/add @one, two\nhttps://t.me/three/ @one"
        self.assertEqual(parse_channel_inputs(text), ["@one", "@two", "@three"])

    def test_persian_variants_and_related_news(self):
        a = normalize("خبر فوری؛ زلزله امروز تهران را لرزاند")
        b = normalize("زلزله امروز تهران را لرزاند!")
        self.assertGreater(similarity(a, b), 0.56)
        self.assertEqual(normalize("كي يار؟"), "کی یار")

    def test_unrelated_news(self):
        self.assertLess(similarity(normalize("قیمت دلار کاهش یافت"), normalize("تیم ملی پیروز شد")), 0.2)

    def test_similar_leads_with_different_titles(self):
        left = signature("تیتر فوری\nوزیر اقتصاد اعلام کرد نرخ مالیات اصناف امسال تغییر نمی‌کند")
        right = signature("جزئیات نشست امروز\nوزیر اقتصاد اعلام کرد نرخ مالیات اصناف در سال جاری تغییر نخواهد کرد")
        self.assertGreater(similarity(left, right), 0.56)

    def test_new_signature_never_weakens_old_full_text_match(self):
        raw = "تیتر خبر\nاین لید کامل خبر است و جزئیات مهم رویداد را توضیح می‌دهد"
        self.assertGreaterEqual(similarity(signature(raw), normalize(raw)), 0.99)

    def test_rewritten_event_uses_shared_news_matcher(self):
        original = signature(
            "حکم پرونده دیوان عالی\n"
            "دیوان عالی کشور حکم حبس دوازده ساله متهم پرونده فساد را تایید کرد"
        )
        rewritten = signature(
            "تایید محکومیت متهم فساد\n"
            "محکوم این پرونده پس از بررسی در دیوان عالی باید دوازده سال را در زندان بگذراند"
        )
        matched, _ = news_match(original, rewritten)
        duplicate, _ = duplicate_match(original, rewritten)
        self.assertTrue(matched)
        self.assertTrue(duplicate)

    def test_same_topic_different_event_is_not_news_match(self):
        first = signature(
            "افزایش قیمت طلا\nقیمت هر گرم طلای هجده عیار امروز به هشت میلیون تومان رسید"
        )
        second = signature(
            "کاهش نرخ دلار\nقیمت دلار در بازار آزاد امروز به نود هزار تومان کاهش یافت"
        )
        matched, _ = news_match(first, second)
        self.assertFalse(matched)

    def test_numeric_mentions_fold_digits_and_persian_words(self):
        self.assertEqual(_number_mentions("حبس دوازده ساله و جریمه ۲۵۰ میلیون تومان"),
                         {"12", "250000000"})
        self.assertEqual(
            _number_mentions("حبس ۱۲ ساله و جریمه دویست و پنجاه میلیون تومان"),
            {"12", "250000000"},
        )

    def test_rewritten_event_gets_numeric_similarity_boost(self):
        left = signature("محکومیت متهم پرونده\nدادگاه او را به دوازده سال حبس محکوم کرد")
        right = signature("صدور حکم نهایی\nمتهم باید ۱۲ سال در زندان بماند")
        _, score = news_match(left, right)
        left_parts, right_parts = left.split("␞"), right.split("␞")
        base = max(
            similarity(left, right),
            0.0,
        )
        self.assertGreaterEqual(_number_mentions(left_parts[2]) & _number_mentions(right_parts[2]), {"12"})
        self.assertGreaterEqual(score, base)

    def test_shared_text_fragment_matches_rewritten_news(self):
        left = signature(
            "تصمیم جدید دولت\n"
            "این تصمیم پس از بررسی‌های کارشناسی و برگزاری چند جلسه اتخاذ شد و اجرای آن از هفته آینده آغاز می‌شود"
        )
        right = signature(
            "جزئیات مصوبه تازه\n"
            "پس از تغییرات گسترده در متن، این تصمیم پس از بررسی کارشناسی و برگزاری چند جلسه اتخاذ شد؛ اجرای طرح هفته بعد شروع خواهد شد"
        )
        fragment_score, fragment_length = _shared_fragment_score(left.split("␞")[2], right.split("␞")[2])
        matched, score = news_match(left, right)
        self.assertGreaterEqual(fragment_length, 6)
        self.assertGreaterEqual(fragment_score, 0.78)
        self.assertTrue(matched)
        self.assertGreaterEqual(score, 0.56)

    def test_generic_short_fragment_does_not_match_unrelated_news(self):
        left = signature("خبر فوری\nاین موضوع پس از بررسی کارشناسی اعلام شد")
        right = signature("گزارش جدید\nاین موضوع پس از بررسی کارشناسی تکذیب شد")
        fragment_score, fragment_length = _shared_fragment_score(left.split("␞")[2], right.split("␞")[2])
        self.assertLess(fragment_length, 6)
        self.assertEqual(fragment_score, 0.0)

    def test_same_channel_handle_does_not_make_unrelated_posts_match(self):
        first = signature(
            "معاون وزیر صمت مصوبه شورای عالی کار برای جابه‌جایی روز تعطیل هفتگی واحد های تولیدی به روزی غیر از جمعه و جبران ساعات از دست‌رفته ناشی از محدودیت برق را ابلاغ کرد\n@Titretejarat"
        )
        second = signature(
            "سخنگوی آموزش‌وپرورش گفته از اول مهر حق‌التدریس معلمان شاغل و بازنشسته ۲ برابر می‌شود\n@Titretejarat"
        )
        self.assertFalse(news_match(first, second)[0])


class ReportTests(unittest.TestCase):
    def test_speed_time_score_decays_smoothly_after_configured_scale(self):
        self.assertAlmostEqual(_speed_time_score(0, 30), 100.0)
        self.assertGreater(_speed_time_score(30, 30), 0.0)
        self.assertGreater(_speed_time_score(60, 30), _speed_time_score(120, 30))
        self.assertGreater(_speed_time_score(120, 30), 0.0)

    def test_authorization_and_user_settings_are_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "users.db", owner_id=100)
            db.set_setting("daily_report_hour", "7")
            db.add_channel("@owner")

            self.assertTrue(db.is_authorized(100))
            self.assertTrue(db.is_owner(100))
            self.assertFalse(db.is_authorized(200))
            self.assertFalse(db.is_owner(200))
            db.authorize_user(200)
            db.use_user(200)
            self.assertEqual(db.get_setting("daily_report_hour"), "")
            self.assertEqual(db.channels(), [])
            db.set_setting("daily_report_hour", "11")
            db.add_channel("@second")

            db.use_user(100)
            self.assertEqual(db.get_setting("daily_report_hour"), "7")
            self.assertEqual([row["channel"] for row in db.channels()], ["@owner"])
            db.use_user(200)
            self.assertEqual(db.get_setting("daily_report_hour"), "11")
            self.assertEqual([row["channel"] for row in db.channels()], ["@second"])
            db.close()

    def test_access_invite_can_only_be_used_once(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "invites.db", owner_id=100)
            db.create_access_invite("NF-TEST123456")
            self.assertTrue(db.consume_access_invite("NF-TEST123456", 200))
            self.assertTrue(db.is_authorized(200))
            self.assertFalse(db.consume_access_invite("NF-TEST123456", 300))
            self.assertFalse(db.is_authorized(300))
            self.assertFalse(db.consume_access_invite("NF-INVALID000", 400))
            db.close()

    def test_jalali_conversion(self):
        self.assertEqual(gregorian_to_jalali(2024, 3, 20), (1403, 1, 1))

    def test_speed_points_and_missed_news(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            tz = ZoneInfo("Asia/Tehran")
            start = datetime(2026, 8, 21, 8, tzinfo=tz)
            cluster = db.create_cluster("خبر مهم مشترک", start)
            for channel, minute in (("@fast", 0), ("@middle", 4), ("@slow", 10)):
                db.add_post(channel, minute + 1, start + timedelta(minutes=minute), "خبر مهم مشترک", "خبر مهم مشترک", f"https://t.me/{channel[1:]}/1", cluster)
            report = build_report(db, ("@fast", "@middle", "@slow", "@missed"), start, start + timedelta(hours=1), tz, 3, 5)
            self.assertIn("@fast", report)
            self.assertIn("رتبه <b>۶۰٪</b>", report)
            self.assertIn("سقف تأخیر <b>۳۰ دقیقه</b>", report)
            self.assertIn("۱۶٫۷", report)
            self.assertNotIn("سوخت خبر", report)
            details = build_speed_detail_pages(db, ("@fast", "@middle", "@slow", "@missed"), start, start + timedelta(hours=1), tz, 3, 5, 2)
            self.assertIn("امتیاز ۱۰۰٫۰", details[0])
            self.assertIn("تأخیر ۱۰٫۰ دقیقه", details[0])
            self.assertIn("در این خبر منتشر نکرد", details[0])
            missed_report = build_report(db, ("@fast", "@middle", "@slow", "@missed", "@missed2", "@missed3"), start, start + timedelta(hours=1), tz, 3, 5, "missed", 4, 3)
            self.assertIn("@missed3", missed_report)
            self.assertNotIn("رده‌بندی سرعت", missed_report)
            db.close()

    def test_split_respects_telegram_limit(self):
        chunks = split_html("\n".join(["یک خط گزارش" * 20] * 100), 500)
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))

    def test_missed_news_has_no_gap_before_speed_threshold(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "missed.db")
            tz = ZoneInfo("Asia/Tehran")
            start = datetime(2026, 8, 21, 8, tzinfo=tz)
            channels = tuple(f"@channel{i}" for i in range(1, 9))

            cluster_five = db.create_cluster("خبر منتشرشده در پنج کانال", start)
            for index, channel in enumerate(channels[:5]):
                db.add_post(channel, 100 + index, start + timedelta(minutes=index),
                            "خبر منتشرشده در پنج کانال", "خبر منتشرشده در پنج کانال",
                            f"https://t.me/{channel[1:]}/1", cluster_five)

            cluster_two = db.create_cluster("خبر منتشرشده در دو کانال", start)
            for index, channel in enumerate(channels[:2]):
                db.add_post(channel, 200 + index, start + timedelta(minutes=index),
                            "خبر منتشرشده در دو کانال", "خبر منتشرشده در دو کانال",
                            f"https://t.me/{channel[1:]}/2", cluster_two)

            report = build_report(
                db, channels, start, start + timedelta(hours=1), tz, 3, 45,
                "missed", 6, 5, 60, 30, 5, 3,
            )
            self.assertIn("خبر منتشرشده در پنج کانال", report)
            self.assertNotIn("خبر منتشرشده در دو کانال", report)
            self.assertIn("۳ تا ۵", report)

            overview, counts = build_missed_overview(
                db, channels, start, start + timedelta(hours=1), tz, 3, 45, 6, 5, 3,
            )
            self.assertIn("تعداد کل خبرهای سوخت‌شده: <b>۱</b>", overview)
            self.assertEqual(dict(counts)["@channel6"], 1)
            self.assertEqual(dict(counts)["@channel1"], 0)
            self.assertEqual([count for _, count in counts], sorted(count for _, count in counts))

            channel_report = build_missed_channel_report(
                db, channels, "@channel6", start, start + timedelta(hours=1), tz,
                3, 45, 6, 5, 3,
            )
            self.assertIn("سوخت خبر @channel6", channel_report)
            self.assertIn("خبر منتشرشده در پنج کانال", channel_report)
            self.assertNotIn("خبر منتشرشده در دو کانال", channel_report)
            db.close()

    def test_default_missed_formula_has_no_gap_for_odd_channel_count(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "missed-default.db")
            tz = ZoneInfo("Asia/Tehran")
            start = datetime(2026, 8, 21, 8, tzinfo=tz)
            channels = tuple(f"@channel{i}" for i in range(1, 6))
            cluster = db.create_cluster("خبر دو کاناله", start)
            for index, channel in enumerate(channels[:2]):
                db.add_post(
                    channel, index + 1, start + timedelta(minutes=index),
                    "خبر دو کاناله", "خبر دو کاناله",
                    f"https://t.me/{channel[1:]}/1", cluster,
                )

            report = build_report(
                db, channels, start, start + timedelta(hours=1), tz,
                3, 5, "missed",
            )
            self.assertIn("خبر دو کاناله", report)
            self.assertIn("۲ تا ۲", report)
            db.close()

    def test_dynamic_channel_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "channels.db")
            self.assertTrue(db.add_channel("@one", "One"))
            self.assertFalse(db.add_channel("@one", "One"))
            db.update_channel_cursor("@one", 42)
            self.assertEqual(db.channels()[0]["last_message_id"], 42)
            self.assertTrue(db.remove_channel("@ONE"))
            self.assertEqual(db.get_int_setting("daily_report_hour", 0), 0)
            db.set_setting("daily_report_hour", "22")
            self.assertEqual(db.get_int_setting("daily_report_hour", 0), 22)
            self.assertEqual(db.get_int_setting("viral_multiplier_x100", 200), 200)
            db.set_setting("viral_multiplier_x100", "250")
            self.assertEqual(db.get_int_setting("viral_multiplier_x100", 200), 250)
            db.set_setting("speed_rule_mode", "two_thirds")
            self.assertEqual(db.get_setting("speed_rule_mode"), "two_thirds")
            self.assertTrue(db.add_viral_channel("@viral", "Viral"))
            self.assertFalse(db.add_viral_channel("@viral", "Viral"))
            self.assertEqual(db.viral_channels()[0]["channel"], "@viral")
            self.assertTrue(db.remove_viral_channel("@VIRAL"))
            self.assertTrue(db.add_proofreading_channel("@source", "Source", -100123, "Private", "invite"))
            self.assertFalse(db.add_proofreading_channel("@source", "Source", -100456))
            db.update_proofreading_cursor("@SOURCE", 17)
            self.assertEqual(db.proofreading_channels()[0]["last_message_id"], 17)
            self.assertTrue(db.remove_proofreading_channel("@SOURCE"))
            db.close()

    def test_engagement_compares_each_post_with_other_posts(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "engagement.db")
            tz = ZoneInfo("Asia/Tehran")
            start = datetime(2026, 8, 21, 8, tzinfo=tz)
            values = [
                (1, "پست عادی اول", 10, 4),
                (2, "پست عادی دوم", 10, 4),
                (3, "پست پربازخورد", 50, 20),
            ]
            for message_id, text, reactions, forwards in values:
                db.upsert_interaction_post(
                    "@sample", message_id, start + timedelta(minutes=message_id), text,
                    f"https://t.me/sample/{message_id}", reactions, forwards,
                )
            report = build_engagement_report(db, "@sample", start, start + timedelta(hours=1), tz)
            self.assertIn("پست پربازخورد", report)
            self.assertNotIn("پست عادی اول</b>", report)
            self.assertIn("۵۰", report)
            self.assertIn("۲۰", report)
            self.assertIn("۵٫۰ برابر میانگین", report)
            db.close()

    def test_engagement_leaderboard_and_topic_analysis(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "engagement-ranking.db")
            tz = ZoneInfo("Asia/Tehran")
            start = datetime(2026, 8, 21, 8, tzinfo=tz)
            db.upsert_interaction_post("@sample", 1, start, "قیمت دلار و بازار ارز", "https://t.me/sample/1", 100, 5)
            db.upsert_interaction_post("@sample", 2, start, "خبر فوتبال و تیم ملی", "https://t.me/sample/2", 20, 50)
            leaderboard = build_engagement_leaderboard(
                db, "@sample", start, start + timedelta(hours=14), tz,
                channel_title="تیترتجارت",
            )
            analysis = build_engagement_topic_analysis(
                db, "@sample", start, start + timedelta(hours=14), tz,
                channel_title="تیترتجارت",
            )
            self.assertIn("پربازخوردترین محتوای تیترتجارت", leaderboard)
            self.assertIn("تحلیل شبانه مخاطب تیترتجارت", analysis)
            self.assertNotIn("خبرفردا", leaderboard)
            self.assertNotIn("خبرفردا", analysis)
            self.assertIn("۴۰٪", leaderboard)
            self.assertIn("۶۰٪", leaderboard)
            self.assertIn("قیمت دلار", leaderboard)
            self.assertIn("فوتبال", leaderboard)
            self.assertIn("اقتصاد و بازار", analysis)
            self.assertIn("ورزش", analysis)
            db.close()

    def test_viral_snapshots_use_each_channels_own_phase_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "viral.db")
            tz = ZoneInfo("Asia/Tehran")
            start = datetime(2026, 8, 21, 8, tzinfo=tz)
            db.save_interaction_snapshot("@one", 1, 5, 100, 20, start)
            db.save_interaction_snapshot("@one", 2, 5, 100, 40, start + timedelta(minutes=1))
            db.save_interaction_snapshot("@two", 1, 5, 10, 2, start)
            self.assertEqual(db.interaction_baseline("@one", 5), (0.0, 0.0, 2))
            self.assertEqual(db.interaction_baseline("@two", 5), (0.0, 0.0, 1))
            self.assertIsNone(db.interaction_baseline("@one", 10))

            now = start + timedelta(hours=1)
            db.add_viral_channel("@one", "One")
            db.upsert_interaction_post("@one", 3, now - timedelta(minutes=5), "viral", "link", 200, 60)
            due = db.posts_due_for_snapshot(now, 5)
            self.assertEqual([(row["channel"], row["message_id"]) for row in due], [("@one", 3)])
            db.save_interaction_snapshot("@one", 3, 5, 200, 60, now)
            self.assertEqual(db.posts_due_for_snapshot(now, 5), [])
            db.close()

    def test_viral_due_posts_exclude_non_viral_channels(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "viral-filter.db")
            now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
            db.add_viral_channel("@allowed", "Allowed")
            db.upsert_interaction_post("@allowed", 1, now - timedelta(minutes=5), "a", "link", 10, 3)
            db.upsert_interaction_post("@personal", 1, now - timedelta(minutes=5), "b", "link", 10, 3)
            due = db.posts_due_for_snapshot(now, 5)
            self.assertEqual([(row["channel"], row["message_id"]) for row in due], [("@allowed", 1)])
            db.close()

    def test_viral_baseline_keeps_last_valid_thirty_post_median(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "viral-baseline.db")
            now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
            for message_id in range(1, 31):
                db.save_interaction_snapshot("@one", message_id, 5, 10, 4, now + timedelta(seconds=message_id))
            self.assertEqual(db.interaction_baseline("@one", 5), (10.0, 4.0, 30))
            with db.conn:
                db.conn.execute("DELETE FROM interaction_snapshots WHERE channel=?", ("@one",))
            self.assertEqual(db.interaction_baseline("@one", 5), (10.0, 4.0, 30))
            db.close()

    def test_viral_baseline_is_shared_and_reused_for_same_channel(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "shared-viral-baseline.db")
            now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
            for message_id in range(1, 31):
                db.save_interaction_snapshot("@One", message_id, 5, 10, 4,
                                             now + timedelta(seconds=message_id))

            self.assertEqual(db.interaction_baseline("@one", 5), (10.0, 4.0, 30))
            # A second user/casing must consume the same cached baseline rather
            # than recalculating it from newly arrived snapshots.
            db.save_interaction_snapshot("@one", 31, 5, 999, 999, now + timedelta(seconds=31))
            self.assertEqual(db.interaction_baseline("@ONE", 5), (10.0, 4.0, 30))
            db.close()

    def test_viral_baseline_uses_only_the_requested_channel_id(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "channel-scoped-baseline.db")
            now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
            for message_id in range(1, 31):
                db.save_interaction_snapshot(
                    "@khabarfarda_ir", message_id, 5, 10, 4,
                    now + timedelta(seconds=message_id),
                )
                db.save_interaction_snapshot(
                    "@other_channel", message_id, 5, 999, 999,
                    now + timedelta(seconds=message_id),
                )

            self.assertEqual(
                db.interaction_baseline("@khabarfarda_ir", 5),
                (10.0, 4.0, 30),
            )
            self.assertEqual(
                db.interaction_history("@khabarfarda_ir", 5),
                ([10] * 30, [4] * 30),
            )
            db.close()

    def test_viral_formula_waits_for_thirty_comparable_samples(self):
        self.assertFalse(evaluate_viral_post(10, 0, 100, 100, 5, 5)[0])
        self.assertFalse(evaluate_viral_post(0, 3, 100, 100, 29, 5)[0])
        self.assertFalse(evaluate_viral_post(9, 2, 0, 0, 29, 5)[0])

    def test_viral_percentile_formula_selects_only_exceptional_posts(self):
        reactions = list(range(10, 70))
        forwards = list(range(2, 62))
        hit = evaluate_viral_post(
            80, 70, 39.5, 31.5, 30, 10, reactions, forwards,
        )
        ordinary = evaluate_viral_post(
            40, 32, 39.5, 31.5, 30, 10, reactions, forwards,
        )
        self.assertTrue(hit[0])
        self.assertEqual(hit[1], "percentile")
        self.assertGreater(hit[4], 95)
        self.assertFalse(ordinary[0])

    def test_viral_percentile_formula_handles_forward_only_channels(self):
        reactions = [0] * 60
        forwards = list(range(1, 61))
        result = evaluate_viral_post(
            999, 70, 0, 30.5, 30, 20, reactions, forwards,
        )
        self.assertTrue(result[0])
        self.assertEqual(result[2], 0.0)

    def test_viral_formula_after_thirty_samples(self):
        hit_5 = evaluate_viral_post(4, 3, 2, 1, 30, 5)
        self.assertTrue(hit_5[0])
        self.assertAlmostEqual(hit_5[4], 2.6)
        self.assertFalse(evaluate_viral_post(3, 2, 2, 1, 30, 5)[0])
        self.assertTrue(evaluate_viral_post(4, 2, 2, 1, 30, 10)[0])
        self.assertTrue(evaluate_viral_post(4, 2, 2, 1, 30, 20)[0])

    def test_viral_formula_without_reactions_uses_forwards_only(self):
        # A reaction on the current post must not affect a channel whose
        # established baseline has no reactions.
        result = evaluate_viral_post(0, 3, 0, 1, 30, 10)
        self.assertTrue(result[0])
        self.assertEqual(result[2], 0.0)
        self.assertEqual(result[4], result[3])
        self.assertFalse(evaluate_viral_post(999, 1, 0, 1, 30, 10)[0])

    def test_viral_formula_adaptive_threshold_catches_strong_trend(self):
        history = [1] * 15 + [2] * 10 + [3] * 5
        result = evaluate_viral_post(
            0, 5, 0, 2, 30, 10,
            [0] * 30, history,
        )
        self.assertTrue(result[0])

    def test_viral_formula_sparse_forwards_requires_absolute_floor(self):
        history = [0] * 27 + [1] * 3
        self.assertFalse(evaluate_viral_post(0, 2, 0, 0, 30, 10, [0] * 30, history)[0])
        self.assertTrue(evaluate_viral_post(0, 4, 0, 0, 30, 10, [0] * 30, history)[0])

    def test_similar_viral_content_reuses_original_alert(self):
        candidates = [{
            "channel": "@one", "message_id": 1, "destination_message_id": 99,
            "text": "زلزله شدید در تهران؛ جزئیات تکمیلی اعلام شد",
        }]
        duplicate = find_similar_viral_alert(
            "زلزله شدید در تهران / جزئیات تکمیلی اعلام شد", candidates,
        )
        self.assertIsNotNone(duplicate)
        self.assertEqual(duplicate["destination_message_id"], 99)
        self.assertIsNone(find_similar_viral_alert("قیمت جدید خودروهای داخلی", candidates))


if __name__ == "__main__":
    unittest.main()
