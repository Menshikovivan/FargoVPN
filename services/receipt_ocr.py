"""Local OCR and conservative automatic receipt validation.

The module intentionally treats OCR as an assistant, not as proof of a bank
transaction. Automatic approval is allowed only when every configured rule is
met and the receipt has not been seen before. Any uncertainty leaves the
payment in the normal manual-review queue.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_PHONE_WORDS = ("телефон", "номер", "сбп", "получател", "перевод", "карта")
_POSITIVE_AMOUNT_WORDS = (
    "сумма",
    "перевод",
    "получено",
    "получатель получил",
    "зачислено",
    "итого",
    "оплата",
)
_NEGATIVE_AMOUNT_WORDS = (
    "комиссия",
    "баланс",
    "остаток",
    "доступно",
    "на счету",
    "кэшбэк",
    "кешбэк",
    "возврат",
)
_FAILURE_WORDS = (
    "отклонено",
    "отменено",
    "операция отменена",
    "перевод отменен",
    "перевод отменён",
    "не выполнено",
    "неуспешно",
    "ошибка перевода",
    "недостаточно средств",
)
_RUSSIAN_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


@dataclass(frozen=True)
class ReceiptAnalysis:
    passed: bool
    status: str
    text: str
    file_sha256: str
    text_sha256: str
    perceptual_hash: str
    amount: float | None
    receipt_datetime: datetime | None
    phone_match: bool
    name_match: bool
    amount_match: bool
    date_match: bool
    explicit_failure: bool
    phone_evidence: str = ""
    name_evidence: str = ""
    duplicate_payment_id: int | None = None
    check_name: bool = True
    check_phone: bool = False
    check_amount: bool = True
    check_date: bool = False
    check_status: bool = True
    check_duplicate: bool = True
    reasons: tuple[str, ...] = ()
    error: str = ""

    @property
    def receipt_date_text(self) -> str:
        if not self.receipt_datetime:
            return ""
        return self.receipt_datetime.isoformat(timespec="minutes")

    def details_json(self) -> str:
        payload = {
            "passed": self.passed,
            "status": self.status,
            "amount": self.amount,
            "receipt_datetime": self.receipt_date_text,
            "phone_match": self.phone_match,
            "name_match": self.name_match,
            "amount_match": self.amount_match,
            "date_match": self.date_match,
            "explicit_failure": self.explicit_failure,
            "phone_evidence": self.phone_evidence,
            "name_evidence": self.name_evidence,
            "duplicate_payment_id": self.duplicate_payment_id,
            "filters": {
                "name": self.check_name,
                "phone": self.check_phone,
                "amount": self.check_amount,
                "date": self.check_date,
                "status": self.check_status,
                "duplicate": self.check_duplicate,
            },
            "reasons": list(self.reasons),
            "error": self.error,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def compact_summary(self) -> str:
        amount = "не найдена" if self.amount is None else f"{self.amount:g} ₽"
        date = self.receipt_datetime.strftime("%d.%m.%Y %H:%M") if self.receipt_datetime else "не найдена"

        def mark(enabled: bool, matched: bool) -> str:
            return "○" if not enabled else ("✓" if matched else "✗")

        rows = [
            f"{mark(self.check_name, self.name_match)} Имя получателя" + (" (фильтр выкл.)" if not self.check_name else ""),
            f"{mark(self.check_phone, self.phone_match)} Номер получателя" + (" (фильтр выкл.)" if not self.check_phone else ""),
            f"{mark(self.check_amount, self.amount_match)} Сумма: {amount}" + (" (фильтр выкл.)" if not self.check_amount else ""),
            f"{mark(self.check_date, self.date_match)} Дата: {date}" + (" (фильтр выкл.)" if not self.check_date else ""),
        ]
        if self.check_status:
            rows.append("✗ В чеке найден отказ/отмена" if self.explicit_failure else "✓ Нет признаков отказа/отмены")
        else:
            rows.append("○ Фильтр статуса отключён")
        if self.check_duplicate:
            if self.duplicate_payment_id:
                rows.append(f"✗ Дубликат платежа #{self.duplicate_payment_id}")
            else:
                rows.append("✓ Дубликат не найден")
        else:
            rows.append("○ Проверка дубликатов отключена")
        if self.error:
            rows.append(f"⚠ OCR: {self.error[:180]}")
        return "\n".join(rows)


def _normalize_space(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).lower().replace("ё", "е")
    return re.sub(r"\s+", " ", value).strip()


def _word_text(value: str) -> str:
    value = _normalize_space(value)
    return re.sub(r"[^0-9a-zа-я]+", " ", value, flags=re.IGNORECASE).strip()


def _phone_digits(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def _split_aliases(receiver: str, aliases: str | Iterable[str] | None) -> list[str]:
    values = [str(receiver or "").strip()]
    if isinstance(aliases, str):
        values.extend(re.split(r"[;,\n]+", aliases))
    elif aliases:
        values.extend(str(item) for item in aliases)
    result: list[str] = []
    for value in values:
        clean = _word_text(value)
        if clean and clean not in result:
            result.append(clean)
    return result


def _edit_distance_at_most_one(left: str, right: str) -> bool:
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    i = j = differences = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        differences += 1
        if differences > 1:
            return False
        if len(left) > len(right):
            i += 1
        elif len(right) > len(left):
            j += 1
        else:
            i += 1
            j += 1
    return differences + (i < len(left) or j < len(right)) <= 1


def match_receiver_name(text: str, receiver: str, aliases: str | Iterable[str] | None = None) -> tuple[bool, str]:
    normalized = _word_text(text)
    words = normalized.split()
    word_set = set(words)
    for alias in _split_aliases(receiver, aliases):
        tokens = alias.split()
        if not tokens:
            continue
        if re.search(rf"(?<![0-9a-zа-я]){re.escape(alias)}(?![0-9a-zа-я])", normalized):
            return True, alias
        if all(token in word_set for token in tokens):
            return True, alias
        if len(tokens) >= 2:
            first, surname = tokens[0], tokens[-1]
            first_matches = [word for word in words if _edit_distance_at_most_one(word, first)]
            surname_matches = [word for word in words if _edit_distance_at_most_one(word, surname)]
            if first_matches and surname_matches:
                return True, f"{first_matches[0]} {surname_matches[0]}"
            initial = surname[:1]
            if first in word_set and initial in word_set:
                return True, f"{first} {initial}."
            if surname in word_set and first[:1] in word_set:
                return True, f"{first[:1]}. {surname}"
    return False, ""


def _digit_pattern(digits: str) -> re.Pattern[str]:
    separator = r"[\s().+\-–—_*•·]*"
    return re.compile(separator.join(map(re.escape, digits)))


def match_recipient_phone(text: str, configured_phone: str, allow_masked: bool = True) -> tuple[bool, str]:
    target = _phone_digits(configured_phone)
    if len(target) < 10:
        return False, ""
    variants = {target, "8" + target[1:] if target.startswith("7") else target}
    for variant in variants:
        match = _digit_pattern(variant).search(text)
        if match:
            return True, match.group(0).strip()
    if not allow_masked:
        return False, ""
    last_four = target[-4:]
    for raw_line in text.splitlines():
        line = _normalize_space(raw_line)
        if not line:
            continue
        phone_context = any(word in line for word in _PHONE_WORDS) or "+7" in line or re.search(r"\*{2,}|•{2,}", line)
        if not phone_context:
            continue
        match = _digit_pattern(last_four).search(line)
        if match and ("*" in line or "•" in line or "+7" in line):
            return True, raw_line.strip()
    return False, ""


def _parse_decimal(raw: str) -> float | None:
    clean = raw.replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    clean = re.sub(r"[^0-9.]", "", clean)
    if clean.count(".") > 1:
        return None
    try:
        value = float(clean)
    except ValueError:
        return None
    return value if 0 <= value <= 10_000_000 else None


def extract_amount(text: str) -> float | None:
    candidates: list[tuple[int, float]] = []
    currency_pattern = re.compile(
        r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})*|\d+)(?:[.,](\d{1,2}))?\s*(?:₽|[рp](?:уб(?:\.|лей|ля)?)?\b)",
        re.IGNORECASE,
    )
    # Labels below explicitly describe money.  A generic word such as
    # "перевод" is intentionally excluded: on many receipts it is followed by
    # a phone number or a date and could otherwise be mistaken for an amount.
    labelled_pattern = re.compile(
        r"(?:сумма(?:\s+перевода)?|итого|зачислено|получено|к\s+зачислению)"
        r"\D{0,25}(\d{1,3}(?:[ \u00a0]\d{3})*|\d+)(?:[.,](\d{1,2}))?",
        re.IGNORECASE,
    )
    for raw_line in text.splitlines():
        line = _normalize_space(raw_line)
        if not line:
            continue
        score = 0
        if any(word in line for word in _POSITIVE_AMOUNT_WORDS):
            score += 10
        if any(word in line for word in _NEGATIVE_AMOUNT_WORDS):
            score -= 12
        # A leading dash/minus on a bank receipt means money was debited from
        # the sender. It is not a negative payment amount. Tesseract also often
        # reads the rouble sign as Latin "P", so treat lines like "-150 P" as
        # strong amount candidates and keep the numeric value positive.
        if re.fullmatch(
            r"[+\-−–—]?\s*\d{1,3}(?:[ \u00a0]\d{3})*(?:[.,]\d{1,2})?\s*(?:₽|[рp])",
            line,
            re.IGNORECASE,
        ):
            score += 15
        # Balance-before/after rows are not the transfer amount.
        if "->" in line or "→" in line:
            score -= 10
        for pattern, bonus in ((currency_pattern, 3), (labelled_pattern, 5)):
            for match in pattern.finditer(line):
                whole = match.group(1)
                fraction = match.group(2) or ""
                value = _parse_decimal(whole + ("." + fraction if fraction else ""))
                if value is not None:
                    candidates.append((score + bonus, value))
    if not candidates:
        return None
    best_score = max(score for score, _value in candidates)
    if best_score < 0:
        return None
    best = [value for score, value in candidates if score == best_score]
    return max(best) if best else None


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "Europe/Moscow"))
    except ZoneInfoNotFoundError:
        logger.warning("Неизвестный часовой пояс OCR %s, используется Europe/Moscow", name)
        return ZoneInfo("Europe/Moscow")


def _valid_datetime(year: int, month: int, day: int, hour: int, minute: int, zone: ZoneInfo) -> datetime | None:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=zone)
    except ValueError:
        return None


def extract_receipt_datetime(text: str, timezone_name: str = "Europe/Moscow", now: datetime | None = None) -> datetime | None:
    zone = _timezone(timezone_name)
    now = now.astimezone(zone) if now and now.tzinfo else (now.replace(tzinfo=zone) if now else datetime.now(zone))
    candidates: list[datetime] = []
    numeric = re.compile(
        r"(?<!\d)([0-3]?\d)[./-]([01]?\d)[./-](\d{2,4})(?:\s*(?:,|в)?\s*([0-2]?\d):([0-5]\d)(?::[0-5]\d)?)?",
        re.IGNORECASE,
    )
    for match in numeric.finditer(text):
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if year < 100:
            year += 2000
        hour = int(match.group(4) or 12)
        minute = int(match.group(5) or 0)
        value = _valid_datetime(year, month, day, hour, minute, zone)
        if value:
            candidates.append(value)

    month_names = "|".join(map(re.escape, _RUSSIAN_MONTHS))
    named = re.compile(
        rf"(?<!\d)([0-3]?\d)\s+({month_names})\s+(\d{{4}})(?:\s*(?:,|в)?\s*([0-2]?\d):([0-5]\d))?",
        re.IGNORECASE,
    )
    for match in named.finditer(_normalize_space(text)):
        value = _valid_datetime(
            int(match.group(3)),
            _RUSSIAN_MONTHS[match.group(2).lower()],
            int(match.group(1)),
            int(match.group(4) or 12),
            int(match.group(5) or 0),
            zone,
        )
        if value:
            candidates.append(value)

    relative = re.compile(r"\b(сегодня|вчера)\b(?:\s*(?:,|в)?\s*([0-2]?\d):([0-5]\d))?", re.IGNORECASE)
    for match in relative.finditer(_normalize_space(text)):
        base = now.date() - (timedelta(days=1) if match.group(1).lower() == "вчера" else timedelta())
        value = _valid_datetime(base.year, base.month, base.day, int(match.group(2) or 12), int(match.group(3) or 0), zone)
        if value:
            candidates.append(value)

    if not candidates:
        return None
    acceptable = [item for item in candidates if item <= now + timedelta(minutes=30)]
    if acceptable:
        return min(acceptable, key=lambda item: abs((now - item).total_seconds()))
    return min(candidates, key=lambda item: abs((now - item).total_seconds()))


def _prepare_image(content: bytes):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 30_000_000
    source = Image.open(io.BytesIO(content))
    if source.width * source.height > 30_000_000:
        raise ValueError("Изображение чека содержит слишком много пикселей")
    source.load()
    source = ImageOps.exif_transpose(source).convert("RGB")
    width, height = source.size
    if width < 1400:
        scale = min(3.0, 1600 / max(1, width))
        source = source.resize((int(width * scale), int(height * scale)))
    if source.width > 3200:
        scale = 3200 / source.width
        source = source.resize((3200, max(1, int(source.height * scale))))
    grayscale = ImageOps.autocontrast(ImageOps.grayscale(source))
    return source, grayscale


def perceptual_hash(content: bytes) -> str:
    try:
        from PIL import Image, ImageOps

        image = Image.open(io.BytesIO(content))
        if image.width * image.height > 30_000_000:
            return ""
        image.load()
        image = ImageOps.exif_transpose(image).convert("L").resize((9, 8))
        pixels = list(image.getdata())
        bits = []
        for row in range(8):
            start = row * 9
            bits.extend(pixels[start + column] > pixels[start + column + 1] for column in range(8))
        value = 0
        for bit in bits:
            value = (value << 1) | int(bit)
        return f"{value:016x}"
    except Exception:
        return ""


def recognize_text(content: bytes, languages: str = "rus+eng", timeout_seconds: int = 20) -> str:
    if not content:
        raise ValueError("Пустой файл чека")
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("Изображение чека превышает 20 МБ")
    try:
        import pytesseract
    except ImportError as error:
        raise RuntimeError("Python-модуль pytesseract не установлен") from error

    source, grayscale = _prepare_image(content)
    timeout_seconds = max(5, min(int(timeout_seconds), 120))
    attempts = [(grayscale, 6), (source, 11)]
    texts: list[str] = []
    last_error: Exception | None = None
    for image, psm in attempts:
        try:
            value = pytesseract.image_to_string(
                image,
                lang=str(languages or "rus+eng"),
                config=f"--oem 1 --psm {psm}",
                timeout=timeout_seconds,
            )
            if value.strip():
                texts.append(value.strip())
        except RuntimeError as error:
            last_error = error
            if "Failed loading language" in str(error) or "Error opening data file" in str(error):
                try:
                    value = pytesseract.image_to_string(
                        image,
                        lang="eng",
                        config=f"--oem 1 --psm {psm}",
                        timeout=timeout_seconds,
                    )
                    if value.strip():
                        texts.append(value.strip())
                except Exception as fallback_error:
                    last_error = fallback_error
        except Exception as error:
            last_error = error
    if not texts:
        raise RuntimeError(str(last_error or "OCR не нашёл текст"))
    unique_lines: list[str] = []
    seen: set[str] = set()
    for block in texts:
        for line in block.splitlines():
            clean = line.strip()
            key = _normalize_space(clean)
            if clean and key not in seen:
                seen.add(key)
                unique_lines.append(clean)
    return "\n".join(unique_lines)


def analyze_receipt(
    content: bytes,
    receiver: str,
    phone: str,
    minimum_amount: float = 150,
    max_age_hours: int = 24,
    aliases: str | Iterable[str] | None = None,
    allow_masked_phone: bool = True,
    timezone_name: str = "Europe/Moscow",
    languages: str = "rus+eng",
    timeout_seconds: int = 20,
    check_name: bool = True,
    check_phone: bool = False,
    check_amount: bool = True,
    check_date: bool = False,
    check_status: bool = True,
    now: datetime | None = None,
    ocr_text: str | None = None,
) -> ReceiptAnalysis:
    file_hash = hashlib.sha256(content).hexdigest()
    phash = perceptual_hash(content)
    text = ""
    error = ""
    try:
        text = ocr_text if ocr_text is not None else recognize_text(content, languages, timeout_seconds)
    except Exception as exc:
        error = str(exc)[:500]

    normalized_text = _normalize_space(text)
    text_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest() if normalized_text else ""
    name_match, name_evidence = match_receiver_name(text, receiver, aliases)
    phone_match, phone_evidence = match_recipient_phone(text, phone, allow_masked_phone)
    amount = extract_amount(text)
    receipt_datetime = extract_receipt_datetime(text, timezone_name, now=now) if text else None
    zone = _timezone(timezone_name)
    reference = now.astimezone(zone) if now and now.tzinfo else (now.replace(tzinfo=zone) if now else datetime.now(zone))
    if receipt_datetime:
        age = reference - receipt_datetime
        date_match = -timedelta(minutes=30) <= age <= timedelta(hours=max(1, int(max_age_hours)))
    else:
        date_match = False
    amount_match = amount is not None and amount >= float(minimum_amount)
    explicit_failure = any(word in normalized_text for word in _FAILURE_WORDS)

    reasons: list[str] = []
    enabled_checks = [
        (bool(check_name), name_match, "не найдено имя получателя"),
        (bool(check_phone), phone_match, "не найден номер получателя"),
        (bool(check_amount), amount_match, f"сумма должна быть не меньше {float(minimum_amount):g} ₽"),
        (bool(check_date), date_match, f"дата должна быть не старше {int(max_age_hours)} ч"),
    ]
    for enabled, matched, reason in enabled_checks:
        if enabled and not matched:
            reasons.append(reason)
    if check_status and explicit_failure:
        reasons.append("в чеке найден признак отмены или ошибки")
    if error:
        reasons.append("OCR не смог надёжно прочитать изображение")

    passed = (
        bool(text)
        and not error
        and all((not enabled) or matched for enabled, matched, _reason in enabled_checks)
        and (not check_status or not explicit_failure)
    )
    return ReceiptAnalysis(
        passed=passed,
        status="passed" if passed else ("error" if error else "manual"),
        text=text,
        file_sha256=file_hash,
        text_sha256=text_hash,
        perceptual_hash=phash,
        amount=amount,
        receipt_datetime=receipt_datetime,
        phone_match=phone_match,
        name_match=name_match,
        amount_match=amount_match,
        date_match=date_match,
        explicit_failure=explicit_failure,
        phone_evidence=phone_evidence[:250],
        name_evidence=name_evidence[:250],
        check_name=bool(check_name),
        check_phone=bool(check_phone),
        check_amount=bool(check_amount),
        check_date=bool(check_date),
        check_status=bool(check_status),
        reasons=tuple(reasons),
        error=error,
    )


def _connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def find_duplicate_receipt(
    db_path: str | Path,
    payment_id: int,
    analysis: ReceiptAnalysis,
) -> int | None:
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT id,receipt_sha256,receipt_text_sha256,receipt_phash,receipt_amount,receipt_date
            FROM payments
            WHERE id<>? AND status IN ('approved','declined') AND (
                (?<>'' AND receipt_sha256=?) OR
                (?<>'' AND receipt_text_sha256=?) OR
                (?<>'' AND receipt_phash=?)
            )
            ORDER BY id DESC LIMIT 100
            """,
            (
                int(payment_id),
                analysis.file_sha256,
                analysis.file_sha256,
                analysis.text_sha256,
                analysis.text_sha256,
                analysis.perceptual_hash,
                analysis.perceptual_hash,
            ),
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        if analysis.file_sha256 and row["receipt_sha256"] == analysis.file_sha256:
            return int(row["id"])
        if analysis.text_sha256 and row["receipt_text_sha256"] == analysis.text_sha256:
            return int(row["id"])
        # A visual hash alone is intentionally insufficient; bank templates are
        # similar. Require the same extracted amount and minute as corroboration.
        if analysis.perceptual_hash and row["receipt_phash"] == analysis.perceptual_hash:
            same_amount = row["receipt_amount"] is not None and analysis.amount is not None and abs(float(row["receipt_amount"]) - analysis.amount) < 0.01
            same_date = bool(row["receipt_date"] and analysis.receipt_date_text and str(row["receipt_date"]) == analysis.receipt_date_text)
            if same_amount and same_date:
                return int(row["id"])
    return None


def save_analysis(db_path: str | Path, payment_id: int, analysis: ReceiptAnalysis) -> None:
    connection = _connect(db_path)
    try:
        connection.execute(
            """
            UPDATE payments SET
                receipt_sha256=?,receipt_text_sha256=?,receipt_phash=?,receipt_amount=?,
                receipt_date=?,receipt_phone=?,receipt_receiver=?,ocr_status=?,ocr_text=?,
                ocr_details=?,auto_approved=COALESCE(auto_approved,0)
            WHERE id=?
            """,
            (
                analysis.file_sha256,
                analysis.text_sha256,
                analysis.perceptual_hash,
                analysis.amount,
                analysis.receipt_date_text or None,
                analysis.phone_evidence or None,
                analysis.name_evidence or None,
                analysis.status,
                analysis.text[:12000],
                analysis.details_json(),
                int(payment_id),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def analyze_payment_receipt(
    db_path: str | Path,
    payment_id: int,
    content: bytes,
    check_duplicate: bool = True,
    **kwargs: Any,
) -> ReceiptAnalysis:
    analysis = analyze_receipt(content, **kwargs)
    analysis = replace(analysis, check_duplicate=bool(check_duplicate))
    duplicate = find_duplicate_receipt(db_path, payment_id, analysis) if check_duplicate else None
    if duplicate:
        reasons = tuple(list(analysis.reasons) + [f"чек уже использован в платеже #{duplicate}"])
        analysis = replace(
            analysis,
            passed=False,
            status="duplicate",
            duplicate_payment_id=duplicate,
            reasons=reasons,
        )
    save_analysis(db_path, payment_id, analysis)
    return analysis
