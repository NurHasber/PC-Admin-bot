"""Confirm how much chat text to load: everything, or a date range."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

_MONTHS: tuple[tuple[str, int], ...] = (
    ("январ", 1),
    ("феврал", 2),
    ("март", 3),
    ("апрел", 4),
    ("мая", 5),
    ("май", 5),
    ("июн", 6),
    ("июл", 7),
    ("август", 8),
    ("сентябр", 9),
    ("октябр", 10),
    ("ноябр", 11),
    ("декабр", 12),
)
_MONTH_NAMES = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_MONTH_WORD = (
    r"январ\w*|феврал\w*|март\w*|апрел\w*|мая|май|июн\w*|июл\w*|"
    r"август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*"
)
_NUM_DATE = r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?"
_ALL_RE = re.compile(
    r"^(?:все|всю|целиком|all|"
    r"все сообщения|всю переписку|"
    r"за все время|без ограничения по дате|без периода)\.?$"
)
_YES_RE = re.compile(
    r"^(?:да|верно|так|ок|окей|угу|ага|yes|подтверждаю)\.?$"
)
_ROLLING_MONTH_RE = re.compile(
    r"(?:за\s+)?последн\w*\s+месяц\w*|месяц\w*\s+назад|"
    r"от\s+сегодня(?:\s+на)?\s+месяц\w*\s+назад|"
    r"от\s+текущ\w*\s+дат\w*(?:\s+на)?\s+месяц\w*"
)
_PREVIOUS_MONTH_RE = re.compile(r"(?:за\s+)?прошл\w*\s+месяц\w*")
_CURRENT_MONTH_RE = re.compile(
    r"(?:за\s+)?(?:текущ\w*|этот|этого|этому|этим|этом|календарн\w*)"
    r"\s+(?:календарн\w*\s+)?месяц\w*"
)
_BARE_MONTH_RE = re.compile(r"(?:^|[\s,])(?:за\s+)?месяц\w*\b")
_PERIOD_STRIP_RE = re.compile(
    r"\bза\s+последн\w*\s+\d{1,3}\s+дн\w*"
    r"|\bза\s+\d{1,3}\s+дн\w*"
    r"|\b(?:за\s+)?последн\w*\s+месяц\w*"
    r"|\bмесяц\w*\s+назад"
    r"|\b(?:за\s+)?прошл\w*\s+месяц\w*"
    r"|\b(?:за\s+)?(?:текущ\w*|этот|этого|этому|этим|этом|календарн\w*)"
    r"\s+(?:календарн\w*\s+)?месяц\w*"
    r"|\bза\s+месяц\w*"
    r"|\bза\s+недел\w*"
    r"|\bза\s+год\w*"
    r"|\bсегодня\b|\bвчера\b",
    re.IGNORECASE,
)
_ROLLING_ANSWER_RE = re.compile(
    r"^(?:за\s+)?(?:последн\w*|назад|месяц назад|от сегодня)\.?$"
)
_CURRENT_ANSWER_RE = re.compile(
    r"^(?:за\s+)?(?:текущ\w*|этот|календарн\w*|календарный месяц)\.?$"
)


@dataclass(frozen=True)
class HistoryScope:
    """Inclusive local start, exclusive local end. Both empty means all time."""

    start_unix: int | None = None
    end_unix: int | None = None

    @property
    def unbounded(self) -> bool:
        return self.start_unix is None and self.end_unix is None


@dataclass
class PendingHistoryAsk:
    prompt: str
    history_chat_id: int
    source_message_id: int
    kind: str
    shared: bool
    session_key: str
    chat_title: str
    system_prefix: str
    search_query: str = ""
    usernames: tuple[str, ...] = ()
    message_ids: tuple[int, ...] = ()
    proposed: HistoryScope | None = None
    question_message_id: int = 0
    reply_to_message_id: int | None = None
    asked_at: float = 0.0


def normalize_scope_text(text: str) -> str:
    return " ".join((text or "").casefold().replace("ё", "е").split())


def parse_period(text: str, *, today: date | None = None) -> HistoryScope | None:
    """Pull a date range out of a sentence. None if the text names no period."""
    raw = normalize_scope_text(text)
    if not raw:
        return None
    today = today or _today()

    if _ROLLING_MONTH_RE.search(raw):
        return _rolling_month(today)
    if _PREVIOUS_MONTH_RE.search(raw):
        return _previous_calendar_month(today)
    if _CURRENT_MONTH_RE.search(raw):
        return _month_scope(today.year, today.month)

    if re.search(r"\bсегодня\b", raw):
        return _days(today, today)
    if re.search(r"\bвчера\b", raw):
        day = today - timedelta(days=1)
        return _days(day, day)

    counted = re.search(r"за\s+(?:последн\w+\s+)?(\d{1,3})\s+дн", raw)
    if counted:
        days = max(1, int(counted.group(1)))
        return _days(today - timedelta(days=days - 1), today)
    if re.search(r"за\s+недел", raw):
        return _days(today - timedelta(days=6), today)
    if re.search(r"за\s+год\b", raw):
        return _days(today - timedelta(days=364), today)

    numeric = re.search(rf"\bс\s+{_NUM_DATE}\s+по\s+{_NUM_DATE}", raw)
    if numeric:
        start = _from_numeric(numeric.group(1), numeric.group(2), numeric.group(3), today)
        end = _from_numeric(numeric.group(4), numeric.group(5), numeric.group(6), today)
        if start and end:
            return _ordered_days(start, end)

    textual = re.search(
        rf"\bс\s+(\d{{1,2}})(?:\s+({_MONTH_WORD}))?(?:\s+(\d{{4}}))?"
        rf"\s+по\s+(\d{{1,2}})(?:\s+({_MONTH_WORD}))?(?:\s+(\d{{4}}))?",
        raw,
    )
    if textual:
        month2 = _month_num(textual.group(5) or "")
        month1 = _month_num(textual.group(2) or "") or month2 or today.month
        year2 = _year(textual.group(6), month2 or month1, today)
        year1 = _year(textual.group(3), month1, today) if textual.group(3) else year2
        start = _safe_date(year1, month1, int(textual.group(1)))
        end = _safe_date(year2, month2 or month1, int(textual.group(4)))
        if start and end:
            return _ordered_days(start, end)

    named = re.search(rf"\bза\s+({_MONTH_WORD})(?:\s+(\d{{4}}))?\b", raw)
    if named:
        month = _month_num(named.group(1))
        if month:
            year = _year(named.group(2), month, today)
            return _month_scope(year, month)

    one = _one_bound(raw, today)
    if one is not None:
        return one

    single = _first_date(raw, today)
    if single is not None:
        return _days(single, single)
    return None


def parse_scope_answer(
    text: str,
    *,
    proposed: HistoryScope | None = None,
    today: date | None = None,
) -> HistoryScope | None:
    """Read a confirmation reply. None if this is not a scope answer."""
    raw = normalize_scope_text(text)
    if not raw:
        return None
    period = parse_period(raw, today=today)
    if period is not None:
        return period
    today = today or _today()
    if _ROLLING_ANSWER_RE.match(raw):
        return _rolling_month(today)
    if _CURRENT_ANSWER_RE.match(raw):
        return _month_scope(today.year, today.month)
    if _ALL_RE.match(raw):
        return HistoryScope()
    if _YES_RE.match(raw):
        if proposed is not None and not proposed.unbounded:
            return proposed
        return HistoryScope()
    return None


def format_period(scope: HistoryScope) -> str:
    if scope.unbounded:
        return "за всё время"
    start = _fmt_unix(scope.start_unix) if scope.start_unix is not None else ""
    end_day = None
    if scope.end_unix is not None:
        end_day = datetime.fromtimestamp(scope.end_unix).astimezone().date() - timedelta(days=1)
    end = _fmt_date(end_day) if end_day is not None else ""
    if start and end and start == end:
        return f"за {start}"
    if start and end:
        return f"с {start} по {end}"
    if start:
        return f"с {start}"
    return f"по {end}"


def scope_question(kind: str, query: str, proposed: HistoryScope | None) -> str:
    """Question shown in chat. No storage or tooling words."""
    period = ""
    if proposed is not None and not proposed.unbounded:
        period = format_period(proposed)
    if kind == "search":
        subject = f"все сообщения, где есть «{query}»"
        if period:
            return (
                f"Понял так: {subject}, {period}. "
                "Так и взять? Напиши «да», «все» (за всё время) или другой период."
            )
        return (
            f"Понял так: нужны {subject}. "
            "Взять их все или только за период? "
            "Напиши «все», «последний месяц» (от сегодня назад), "
            "«текущий месяц» или даты, например «с 1 по 10 сентября»."
        )
    if period:
        return (
            f"Понял так: вся переписка, {period}. "
            "Так и взять? Напиши «да», «все» (за всё время) или другой период."
        )
    return (
        "Понял так: нужна вся переписка. "
        "Взять все сообщения или только за период? "
        "Напиши «все», «последний месяц» (от сегодня назад), "
        "«текущий месяц» или даты, например «с 1 сентября»."
    )


def month_needs_choice(text: str) -> bool:
    """True when the text says «месяц» but not which kind."""
    raw = normalize_scope_text(text)
    if not raw or parse_period(raw) is not None:
        return False
    if _ROLLING_MONTH_RE.search(raw) or _PREVIOUS_MONTH_RE.search(raw):
        return False
    if _CURRENT_MONTH_RE.search(raw):
        return False
    return _BARE_MONTH_RE.search(raw) is not None


def month_clarification() -> str:
    return (
        "Месяц можно взять по-разному: от сегодня на месяц назад "
        "или текущий календарный месяц. Напиши «последний» или «текущий»."
    )


def start_ack(kind: str, query: str, scope: HistoryScope) -> str:
    """One line before the work starts. No storage words."""
    period = format_period(scope)
    if kind == "search" and query:
        return (
            f"Понял: ищу все сообщения, где есть «{query}», {period}. Делаю."
        )
    return f"Понял: беру переписку {period}. Делаю."


def strip_period_phrases(text: str) -> str:
    return _PERIOD_STRIP_RE.sub(" ", text or "")


def _today() -> date:
    return datetime.now().astimezone().date()


def _tz():
    return datetime.now().astimezone().tzinfo


def _start(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=_tz()).timestamp())


def _days(first: date, last: date) -> HistoryScope:
    if last < first:
        first, last = last, first
    return HistoryScope(_start(first), _start(last + timedelta(days=1)))


def _ordered_days(first: date, last: date) -> HistoryScope:
    return _days(first, last)


def _shift_months(day: date, delta: int) -> date:
    month_index = day.month - 1 + delta
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _rolling_month(today: date) -> HistoryScope:
    return _days(_shift_months(today, -1), today)


def _previous_calendar_month(today: date) -> HistoryScope:
    first = date(today.year, today.month, 1) - timedelta(days=1)
    return _month_scope(first.year, first.month)


def _month_scope(year: int, month: int) -> HistoryScope:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return HistoryScope(_start(start), _start(end))


def _month_num(word: str) -> int | None:
    raw = (word or "").casefold().replace("ё", "е")
    if not raw:
        return None
    for prefix, num in _MONTHS:
        if raw.startswith(prefix):
            return num
    return None


def _year(raw: str | None, month: int, today: date) -> int:
    if raw:
        year = int(raw)
        return year if year >= 100 else 2000 + year
    if month > today.month:
        return today.year - 1
    return today.year


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _from_numeric(
    day_s: str, month_s: str, year_s: str | None, today: date
) -> date | None:
    month = int(month_s)
    return _safe_date(_year(year_s, month, today), month, int(day_s))


def _one_bound(raw: str, today: date) -> HistoryScope | None:
    numeric_from = re.search(rf"\bс\s+{_NUM_DATE}", raw)
    if numeric_from:
        day = _from_numeric(
            numeric_from.group(1),
            numeric_from.group(2),
            numeric_from.group(3),
            today,
        )
        if day:
            return HistoryScope(_start(day), None)
    textual_from = re.search(
        rf"\bс\s+(\d{{1,2}})\s+({_MONTH_WORD})(?:\s+(\d{{4}}))?",
        raw,
    )
    if textual_from:
        month = _month_num(textual_from.group(2))
        if month:
            day = _safe_date(
                _year(textual_from.group(3), month, today),
                month,
                int(textual_from.group(1)),
            )
            if day:
                return HistoryScope(_start(day), None)
    numeric_to = re.search(rf"\bпо\s+{_NUM_DATE}", raw)
    if numeric_to:
        day = _from_numeric(
            numeric_to.group(1), numeric_to.group(2), numeric_to.group(3), today
        )
        if day:
            return HistoryScope(None, _start(day + timedelta(days=1)))
    textual_to = re.search(
        rf"\bпо\s+(\d{{1,2}})\s+({_MONTH_WORD})(?:\s+(\d{{4}}))?",
        raw,
    )
    if textual_to:
        month = _month_num(textual_to.group(2))
        if month:
            day = _safe_date(
                _year(textual_to.group(3), month, today),
                month,
                int(textual_to.group(1)),
            )
            if day:
                return HistoryScope(None, _start(day + timedelta(days=1)))
    return None


def _first_date(raw: str, today: date) -> date | None:
    numeric = re.search(rf"\b{_NUM_DATE}", raw)
    if numeric:
        return _from_numeric(numeric.group(1), numeric.group(2), numeric.group(3), today)
    textual = re.search(rf"\b(\d{{1,2}})\s+({_MONTH_WORD})(?:\s+(\d{{4}}))?", raw)
    if textual:
        month = _month_num(textual.group(2))
        if month:
            return _safe_date(
                _year(textual.group(3), month, today),
                month,
                int(textual.group(1)),
            )
    return None


def _fmt_unix(unix: int) -> str:
    return _fmt_date(datetime.fromtimestamp(unix).astimezone().date())


def _fmt_date(day: date) -> str:
    return f"{day.day} {_MONTH_NAMES[day.month]} {day.year}"
