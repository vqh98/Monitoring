from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape


@dataclass(frozen=True)
class ProofreadingIssue:
    kind: str
    original: str
    suggestion: str
    explanation: str
    context: str = ""
    count: int = 1


# حالت «دقت بالا»: فقط صورت‌هایی که غلط‌بودنشان روشن است. صورت‌های پذیرفته‌شده،
# سلیقه‌های ویرایشی و نشانه‌گذاری عمداً در این فهرست نیستند.
OBVIOUS_SPELLING_ERRORS = {
    "باتریط": "باتری",
    "تضمینن": "تضمینا",
    "برگذار": "برگزار",
    "برگذاری": "برگزاری",
    "بلاخره": "بالاخره",
    "توجیح": "توجیه",
    "توجیع": "توجیه",
    "حاظر": "حاضر",
    "حتمن": "حتما",
    "خواهشن": "خواهشا",
    "زخیره": "ذخیره",
    "ظبط": "ضبط",
    "ضاهر": "ظاهر",
    "ضمینه": "زمینه",
    "عتراض": "اعتراض",
    "گاهاً": "گاهی",
    "گزاشت": "گذاشت",
    "گزاشتن": "گذاشتن",
    "گزشته": "گذشته",
    "ملاحضه": "ملاحظه",
    "مئثر": "مؤثر",
    "ماثر": "مؤثر",
    "متاسفانه": "متأسفانه",
    "محصوص": "مخصوص",
    "مسول": "مسئول",
    "مسولان": "مسئولان",
    "مسولیت": "مسئولیت",
    "معزرت": "معذرت",
    "مطممعن": "مطمئن",
    "مطمعن": "مطمئن",
    "موئسسه": "مؤسسه",
    "نقطه نزر": "نقطه‌نظر",
    "نضام": "نظام",
    "وضیفه": "وظیفه",
    "هیئت علمیی": "هیئت علمی",
    "گاها": "گاهی",
    "لطفن": "لطفا",
    "یقینن": "یقینا",
    "احتمالن": "احتمالا",
    "راجب": "راجع",
    "بر علیه": "علیه",
    "میخام": "می‌خواهم",
    "میخوام": "می‌خواهم",
    "می‌خوام": "می‌خواهم",
    "نمیدونم": "نمی‌دانم",
    "نمی‌دونم": "نمی‌دانم",
    "نمیشه": "نمی‌شود",
    "نمی‌شه": "نمی‌شود",
    "میتونه": "می‌تواند",
    "می‌تونه": "می‌تواند",
    "میتونم": "می‌توانم",
    "می‌تونم": "می‌توانم",
    "واسه": "برای",
}

HALF_SPACE_COMPOUNDS = {
    "نخست وزیر": "نخست‌وزیر",
    "نشان دهنده": "نشان‌دهنده",
    "سوخت رسانی": "سوخت‌رسانی",
    "گواهی نامه": "گواهی‌نامه",
    "همان طور": "همان‌طور",
}

# محدودکردن «می/نمی» به فعل‌های شناخته‌شده جلوی خطا روی «ماه می» و نام‌هایی
# مانند «می دونگ» را می‌گیرد.
VERB_STEMS = (
    "تواند", "توانند", "شود", "شوند", "کند", "کنند", "باشد", "باشند",
    "گوید", "گویند", "دهد", "دهند", "رود", "روند", "آید", "آیند",
    "گیرد", "گیرند", "رسد", "رسند", "گذرد", "گذرند", "ماند", "مانند",
    "خواهد", "خواهند", "توانم", "توانیم", "توانید", "دارد", "دارند", "گفت", "گفتند",
)

def _excerpt(text: str, start: int, end: int, radius: int = 38) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    value = re.sub(r"\s+", " ", text[left:right]).strip()
    return ("…" if left else "") + value + ("…" if right < len(text) else "")


def proofread(text: str) -> list[ProofreadingIssue]:
    """Find only explicit spelling and half-space mistakes with high confidence."""
    if not (text or "").strip():
        return []

    found: list[tuple[int, str, str, str, str]] = []
    for wrong, correct in sorted(OBVIOUS_SPELLING_ERRORS.items(), key=lambda item: -len(item[0])):
        pattern = re.compile(rf"(?<![\w\u200c]){re.escape(wrong)}(?![\w\u200c])", re.IGNORECASE)
        for match in pattern.finditer(text):
            found.append((match.start(), "غلط املایی قطعی", match.group(0), correct,
                          _excerpt(text, match.start(), match.end())))

    # در متن فارسی، عددهای لاتین باید با رقم فارسی نوشته شوند؛
    # عددهای داخل لینک، کنار واژهٔ انگلیسی، و تاریخ میلادی مستثنا هستند.
    latin_digit_pattern = re.compile(r"(?<![A-Za-z])([0-9]+(?:[.,][0-9]+)*)(?![A-Za-z])")
    latin_digits = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    url_pattern = re.compile(r"(?:https?://|www\.|t\.me/)[^\s<>\u200c]+", re.IGNORECASE)
    gregorian_date_pattern = re.compile(
        r"\b[0-9]{1,2}\s+(?:ژانویه|فوریه|مارس|آوریل|مه|ژوئن|ژوئیه|اوت|آگوست|سپتامبر|اکتبر|نوامبر|دسامبر)\s+[0-9]{4}\b"
    )

    exempt_spans = [match.span() for match in url_pattern.finditer(text)]
    exempt_spans.extend(match.span() for match in gregorian_date_pattern.finditer(text))

    def is_exempt(match: re.Match[str]) -> bool:
        start, end = match.span()
        if any(start >= left and end <= right for left, right in exempt_spans):
            return True
        # A number separated by whitespace from an English word is allowed
        # (e.g. "version 2" or "مدل MQ 9").
        left = text[:start].rstrip()
        right = text[end:].lstrip()
        return bool(
            re.search(r"[A-Za-z][A-Za-z0-9_-]*$", left)
            or re.match(r"^[A-Za-z][A-Za-z0-9_-]*", right)
        )

    for match in latin_digit_pattern.finditer(text):
        if is_exempt(match):
            continue
        original = match.group(0)
        found.append((match.start(), "رقم لاتین در متن فارسی", original,
                      original.translate(latin_digits),
                      _excerpt(text, match.start(), match.end())))

    # نیم‌فاصله و فاصله‌گذاری فعلاً طبق تنظیم کاربر بررسی نمی‌شوند.
    grouped: dict[tuple[str, str, str], tuple[int, str, int]] = {}
    for position, kind, original, correct, context in found:
        key = (kind, original, correct)
        if key in grouped:
            first_position, first_context, count = grouped[key]
            grouped[key] = (first_position, first_context, count + 1)
        else:
            grouped[key] = (position, context, 1)
    ordered = sorted(grouped.items(), key=lambda item: item[1][0])
    suffix_pattern = re.compile(r"(?<![\w\u200c])([آ-ی]+)\s+(ها|های)(?![\w\u200c])")
    for match in suffix_pattern.finditer(text):
        found.append((match.start(), "نیم‌فاصلهٔ قطعی", match.group(0),
                      f"{match.group(1)}‌{match.group(2)}",
                      _excerpt(text, match.start(), match.end())))

    verb_pattern = re.compile(
        rf"(?<![\w\u200c])(ن?می)\s+({'|'.join(VERB_STEMS)})(?![\w\u200c])"
    )
    for match in verb_pattern.finditer(text):
        found.append((match.start(), "نیم‌فاصلهٔ قطعی", match.group(0),
                      f"{match.group(1)}‌{match.group(2)}",
                      _excerpt(text, match.start(), match.end())))

    # شکل چسبیدهٔ «می/نمی» به فعل نیز در نوشتار فارسی معیار خطاست.
    glued_verb_pattern = re.compile(
        rf"(?<![\w\u200c])(ن?می)({'|'.join(VERB_STEMS)})(?![\w\u200c])"
    )
    for match in glued_verb_pattern.finditer(text):
        found.append((match.start(), "نیم‌فاصلهٔ قطعی", match.group(0),
                      f"{match.group(1)}‌{match.group(2)}",
                      _excerpt(text, match.start(), match.end())))

    for wrong, correct in HALF_SPACE_COMPOUNDS.items():
        pattern = re.compile(rf"(?<![\w\u200c]){re.escape(wrong)}(?![\w\u200c])")
        for match in pattern.finditer(text):
            found.append((match.start(), "نیم‌فاصلهٔ قطعی", match.group(0), correct,
                          _excerpt(text, match.start(), match.end())))

    grouped: dict[tuple[str, str, str], tuple[int, str, int]] = {}
    for position, kind, original, correct, context in found:
        key = (kind, original, correct)
        if key in grouped:
            first_position, first_context, count = grouped[key]
            grouped[key] = (first_position, first_context, count + 1)
        else:
            grouped[key] = (position, context, 1)

    ordered = sorted(grouped.items(), key=lambda item: item[1][0])
    return [
        ProofreadingIssue(
            kind=kind,
            original=wrong,
            suggestion=correct,
            explanation="صورت درست جایگزین شود.",
            context=context,
            count=count,
        )
        for (kind, wrong, correct), (_position, context, count) in ordered
    ]


def format_issues(issues: list[ProofreadingIssue]) -> str:
    total = sum(issue.count for issue in issues)
    lines = [
        "🔎 <b>موارد قطعی املایی و نیم‌فاصله</b>",
        f"تعداد: <b>{total}</b>",
    ]
    for index, issue in enumerate(issues, 1):
        repeated = f" <i>({issue.count} بار)</i>" if issue.count > 1 else ""
        lines.append(
            f"\n<b>{index}) {escape(issue.kind)}</b>\n"
            f"<code>{escape(issue.original)}</code>  ←  "
            f"<code>{escape(issue.suggestion)}</code>{repeated}\n"
            f"متن: <i>{escape(issue.context)}</i>"
        )
    lines.append("\n<i>موارد سلیقه‌ای و کم‌اطمینان گزارش نشده‌اند.</i>")
    return "\n".join(lines)
