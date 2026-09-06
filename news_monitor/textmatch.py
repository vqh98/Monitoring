from __future__ import annotations

import math
import re
from collections import Counter
from difflib import SequenceMatcher
import unicodedata

URL_RE = re.compile(r"(?:https?://|t\.me/)\S+", re.I)
SPACE_RE = re.compile(r"\s+")
NOISE_RE = re.compile(r"[^\w\s]", re.UNICODE)
ARABIC_MAP = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "ؤ": "و", "إ": "ا", "أ": "ا"})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = URL_RE.sub(" ", text).translate(ARABIC_MAP).lower()
    text = text.replace("\u200c", " ").replace("\u200f", " ").replace("\u200e", " ")
    text = "".join(
        char for char in text
        if not unicodedata.combining(char)
        and unicodedata.category(char) not in {"Cf", "Cs"}
    )
    text = NOISE_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()[:1200]


def signature(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    title = normalize(lines[0] if lines else text)
    lead = normalize(" ".join(lines[1:4]) if len(lines) > 1 else text)
    full = normalize(text)
    return "␞".join((title[:240], lead[:600], full))


def _ngrams(text: str, size: int = 3) -> Counter[str]:
    compact = text.replace(" ", "_")
    return Counter(compact[i : i + size] for i in range(max(0, len(compact) - size + 1)))


def _basic_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    a, b = _ngrams(left), _ngrams(right)
    dot = sum(value * b.get(key, 0) for key, value in a.items())
    denom = math.sqrt(sum(v * v for v in a.values()) * sum(v * v for v in b.values()))
    cosine = dot / denom if denom else 0.0
    aw, bw = set(left.split()), set(right.split())
    jaccard = len(aw & bw) / len(aw | bw) if aw | bw else 0.0
    return 0.75 * cosine + 0.25 * jaccard


_MATCH_STOPWORDS = {
    "به", "از", "در", "با", "برای", "که", "و", "یا", "این", "آن", "یک",
    "را", "است", "شد", "شدند", "کرد", "کرده", "می", "شود", "شده", "بر",
    "تا", "هم", "اما", "اگر", "پس", "نیز", "ها", "های", "خبر", "گزارش",
    "امروز", "دیروز", "اکنون", "اعلام", "اعلامیه", "تازه", "جدید", "مهم",
    "فوری", "جزئیات", "میزان", "مورد", "موضوع", "خصوص", "رئیس", "وزیر",
    "کشور", "شهر", "منطقه", "خبرگزاری",
}

_NUMBER_WORDS = {
    "صفر": 0, "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5,
    "شش": 6, "هفت": 7, "هشت": 8, "نه": 9, "ده": 10, "یازده": 11,
    "دوازده": 12, "سیزده": 13, "چهارده": 14, "پانزده": 15,
    "شانزده": 16, "هفده": 17, "هجده": 18, "نوزده": 19, "بیست": 20,
    "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60, "هفتاد": 70,
    "هشتاد": 80, "نود": 90, "صد": 100, "یکصد": 100,
    "دویست": 200, "سیصد": 300, "چهارصد": 400, "پانصد": 500,
    "ششصد": 600, "هفتصد": 700, "هشتصد": 800, "نهصد": 900,
    "هزار": 1000, "میلیون": 1_000_000, "میلیارد": 1_000_000_000,
}
_NUMBER_CONNECTORS = {"و", "یک", "صد", "هزار", "میلیون", "میلیارد"}
_DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def _number_mentions(text: str) -> set[str]:
    """Extract numeric mentions, folding Persian number words to digits."""
    compact = normalize(text).translate(_DIGIT_TRANSLATION)
    mentions = set()
    words = compact.replace("-", " ").split()
    index = 0
    while index < len(words):
        if not (words[index] in _NUMBER_WORDS or re.fullmatch(r"\d+(?:[./:-]\d+)*", words[index])):
            index += 1
            continue
        total = 0
        current = 0
        consumed = False
        while index < len(words):
            word = words[index]
            is_digit = bool(re.fullmatch(r"\d+(?:[./:-]\d+)*", word))
            if not is_digit and word not in _NUMBER_WORDS and word not in _NUMBER_CONNECTORS:
                break
            if word == "و":
                index += 1
                continue
            value = int(word) if is_digit else _NUMBER_WORDS.get(word)
            if value is None:
                break
            consumed = True
            if value >= 1000:
                total += (current or 1) * value
                current = 0
            elif value == 100:
                current = (current or 1) * value
            elif value == 1 and current:
                current += value
            else:
                current += value
            index += 1
        if consumed:
            mentions.add(str(total + current))
        else:
            index += 1
    return mentions


def _numeric_similarity(left: str, right: str) -> float:
    left_numbers, right_numbers = _number_mentions(left), _number_mentions(right)
    if not left_numbers or not right_numbers:
        return 0.0
    return len(left_numbers & right_numbers) / min(len(left_numbers), len(right_numbers))


def _match_tokens(text: str) -> set[str]:
    tokens = set()
    for token in text.split():
        if len(token) <= 2 or token in _MATCH_STOPWORDS:
            continue
        stem = re.sub(r"(?:های|ها|ترین|تر|ای|ی|ان|ات|ه)$", "", token)
        tokens.add(stem or token)
    return tokens


def _match_word_list(text: str) -> list[str]:
    """Return ordered meaningful words for contiguous-fragment matching."""
    words = []
    for token in text.split():
        if len(token) <= 2 or token in _MATCH_STOPWORDS:
            continue
        stem = re.sub(r"(?:های|ها|ترین|تر|ای|ی|ان|ات|ه)$", "", token)
        words.append(stem or token)
    return words


def _shared_fragment_score(left: str, right: str, min_words: int = 4) -> tuple[float, int]:
    """Score the strongest contiguous shared text fragment.

    A long verbatim slice remains useful even when the surrounding article has
    been paraphrased. Generic stopwords are removed before matching.
    """
    left_words, right_words = _match_word_list(left), _match_word_list(right)
    if not left_words or not right_words:
        return 0.0, 0
    previous = {}
    longest = 0
    for i, left_word in enumerate(left_words):
        current = {}
        for j, right_word in enumerate(right_words):
            if left_word == right_word:
                length = previous.get(j - 1, 0) + 1
                current[j] = length
                longest = max(longest, length)
        previous = current
    if longest < max(2, min_words):
        return 0.0, longest
    # Absolute phrase length carries more signal than its share of a long
    # article, while the normalized term prevents short generic matches.
    ratio = longest / max(1, min(len(left_words), len(right_words)))
    score = min(1.0, 0.50 + 0.06 * longest + 0.20 * ratio)
    return score, longest


def _distinctive_similarity(left: str, right: str) -> float:
    """Token similarity that survives common Persian news paraphrases."""
    left_tokens, right_tokens = _match_tokens(left), _match_tokens(right)
    if len(left_tokens) < 3 or len(right_tokens) < 3:
        return 0.0
    shared = left_tokens & right_tokens
    overlap = len(shared) / min(len(left_tokens), len(right_tokens))
    containment = len(shared) / max(len(left_tokens), len(right_tokens))
    return 0.55 * overlap + 0.45 * containment


def similarity(left: str, right: str, fragment_min_words: int = 4,
               numeric_boost: float = 0.12) -> float:
    if "␞" in left and "␞" not in right:
        left_title, left_lead, left_full = (left.split("␞", 2) + ["", ""])[:3]
        return max(
            _basic_similarity(left_full, right),
            _basic_similarity(left_title, right),
            0.88 * _basic_similarity(left_lead, right),
        )
    if "␞" in right and "␞" not in left:
        return similarity(right, left)
    if "␞" not in left:
        base_score = max(
            _basic_similarity(left, right),
            _distinctive_similarity(left, right),
            _shared_fragment_score(left, right, fragment_min_words)[0],
        )
        numeric_score = _numeric_similarity(left, right)
        return min(1.0, base_score + numeric_boost * numeric_score) if base_score >= 0.35 else base_score
    left_title, left_lead, left_full = (left.split("␞", 2) + ["", ""])[:3]
    right_title, right_lead, right_full = (right.split("␞", 2) + ["", ""])[:3]
    title_score = _basic_similarity(left_title, right_title)
    lead_score = _basic_similarity(left_lead, right_lead)
    full_score = _basic_similarity(left_full, right_full)
    distinctive_full = _distinctive_similarity(left_full, right_full)
    distinctive_lead = _distinctive_similarity(left_lead, right_lead)
    fragment_score, _ = _shared_fragment_score(left_full, right_full, fragment_min_words)
    base_score = max(
        full_score,
        0.35 * title_score + 0.65 * lead_score,
        0.88 * lead_score,
        distinctive_full,
        0.85 * distinctive_lead,
        fragment_score,
    )
    numeric_score = _numeric_similarity(left_full, right_full)
    return min(1.0, base_score + numeric_boost * numeric_score) if base_score >= 0.35 else base_score


_DUPLICATE_STOPWORDS = {
    "به", "از", "در", "با", "برای", "که", "و", "یا", "این", "آن", "یک",
    "را", "است", "شد", "شدند", "کرد", "کرده", "می", "شود", "شده", "بر",
    "تا", "هم", "اما", "اگر", "پس", "نیز", "ها", "های", "خبر", "گزارش",
}
_DUPLICATE_GENERIC = {
    "امروز", "دیروز", "اکنون", "اعلام", "اعلامیه", "تازه", "جدید", "مهم",
    "فوری", "جزئیات", "میزان", "مورد", "موضوع", "خصوص", "رئیس", "وزیر",
    "کشور", "شهر", "منطقه", "خبرگزاری",
}
_NUMBER_RE = re.compile(r"\d+(?:[./:-]\d+)*")


def _duplicate_tokens(text: str) -> set[str]:
    tokens = set()
    for token in text.split():
        if len(token) <= 2 or token in _DUPLICATE_STOPWORDS or token in _DUPLICATE_GENERIC:
            continue
        # A light Persian suffix fold helps match «زلزله‌ای/زلزله» and
        # «اعلام شد/اعلام‌شده» without requiring a heavyweight NLP model.
        stem = re.sub(r"(?:های|ها|ترین|تر|ای|ی|ان|ات|ه)$", "", token)
        tokens.add(stem or token)
    return tokens


def duplicate_match(left: str, right: str, fragment_min_words: int = 4,
                    fragment_threshold: float = 0.78,
                    numeric_boost: float = 0.12) -> tuple[bool, float]:
    """High-precision duplicate detector for news captions.

    Unlike ``similarity`` (which is intentionally permissive for clustering),
    this requires corroborating evidence from the title/body and meaningful
    shared words, preventing generic news-template captions from matching.
    """
    if not left or not right:
        return False, 0.0
    separator = "␞"
    left_parts = (left.split(separator, 2) + ["", ""])[:3]
    right_parts = (right.split(separator, 2) + ["", ""])[:3]
    if separator not in left:
        left_parts = ["", "", left]
    if separator not in right:
        right_parts = ["", "", right]
    left_full, right_full = left_parts[2], right_parts[2]
    if left_full and left_full == right_full:
        return True, 1.0
    title_score = _basic_similarity(left_parts[0], right_parts[0])
    lead_score = _basic_similarity(left_parts[1], right_parts[1])
    full_score = _basic_similarity(left_full, right_full)
    left_tokens = _duplicate_tokens(left_full)
    right_tokens = _duplicate_tokens(right_full)
    shared = left_tokens & right_tokens
    overlap = len(shared) / max(1, min(len(left_tokens), len(right_tokens)))
    min_tokens = min(len(left_tokens), len(right_tokens))
    if min_tokens < 4 or len(shared) < 3:
        return False, max(full_score, title_score)
    left_words, right_words = left_full.split(), right_full.split()
    sequence = SequenceMatcher(None, left_words, right_words, autojunk=False).ratio()
    containment = len(shared) / max(1, max(len(left_tokens), len(right_tokens)))
    left_numbers, right_numbers = set(_NUMBER_RE.findall(left_full)), set(_NUMBER_RE.findall(right_full))
    # Conflicting dates, amounts, scores, etc. are strong evidence that these
    # are two different reports despite sharing a topic.
    if left_numbers and right_numbers and not (left_numbers & right_numbers):
        return False, max(full_score, title_score)
    distinctive_full = _distinctive_similarity(left_full, right_full)
    fragment_score, fragment_length = _shared_fragment_score(left_full, right_full, fragment_min_words)
    numeric_score = _numeric_similarity(left_full, right_full)
    score = (
        0.27 * sequence + 0.18 * full_score + 0.13 * overlap
        + 0.08 * title_score + 0.18 * distinctive_full
        + 0.12 * numeric_score + 0.12 * fragment_score
    )
    matched = (
        (sequence >= 0.90 and overlap >= 0.72)
        or (containment >= 0.82 and overlap >= 0.88)
        or (full_score >= 0.84 and overlap >= 0.70 and len(shared) >= 5)
        or (title_score >= 0.90 and lead_score >= 0.78 and overlap >= 0.70)
        # Paraphrased copies: lower lexical similarity is acceptable when the
        # same distinctive entities/events dominate both captions.
        or (full_score >= 0.52 and title_score >= 0.30 and overlap >= 0.55
            and len(shared) >= 3 and containment >= 0.45)
        or (distinctive_full >= 0.68 and len(shared) >= 4 and containment >= 0.55)
        or (numeric_score >= 0.5 and full_score >= 0.45 and overlap >= 0.45
            and len(shared) >= 3)
            or (fragment_length >= max(6, fragment_min_words + 2) and fragment_score >= fragment_threshold
            and len(shared) >= 3)
    )
    return matched, score


def news_match(left: str, right: str, threshold: float = 0.56,
               fragment_min_words: int = 4, fragment_threshold: float = 0.78,
               numeric_boost: float = 0.12) -> tuple[bool, float]:
    """Match two reports of the same news event, including paraphrased copies.

    This is the shared decision rule for clustering (speed/missed reports) and
    duplicate detection. Exact/strong duplicates use the high-precision gates;
    ordinary clustering can also accept the broader similarity score.
    """
    duplicate, duplicate_score = duplicate_match(
        left, right, fragment_min_words, fragment_threshold, numeric_boost,
    )
    broad_score = similarity(left, right, fragment_min_words, numeric_boost)
    return duplicate or broad_score >= threshold, max(broad_score, duplicate_score)
