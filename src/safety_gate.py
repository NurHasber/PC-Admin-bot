from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_PASSWORD_ATTEMPTS = 3
DEFAULT_ELEVATION_SECONDS = 10 * 60

# Heuristic triggers for "ask password before sending to the agent".
_SUSPICIOUS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bformat\b",
        r"формат\w*",
        r"\bwipe\b",
        r"\bdiskpart\b",
        r"удал\w*\s+(весь\s+)?диск",
        r"rm\s+-rf",
        r"del\s+/[sf]",
        r"powershell\s+-enc",
        r"invoke-webrequest",
        r"\bmimikatz\b",
        r"keylog",
        r"ransomware",
        r"шифр\w*\s+диск",
        r"персональн\w*\s+данн",
        r"personal\s+data",
        r"паспорт",
        r"cvv|card\s*number",
        r"выведи\s+(все\s+)?(парол|ключ|token|секрет)",
        r"show\s+(me\s+)?(all\s+)?(password|secret|token|key)",
        r"отправ\w*\s+.*\.env",
        r"send\s+.*\.env",
        r"CURSOR_API_KEY|TELEGRAM_BOT_TOKEN",
        r"выгруз\w*\s+баз",
        r"dump\s+(database|db|memory)",
        r"shutdown\s+/[sf]",
        r"выключ\w*\s+(пк|компьютер|windows)",
        r"переустанов\w*\s+windows",
        r"reg\s+delete",
        r"net\s+user\s+\w+\s+/add",
    )
)


@dataclass
class SafetyGate:
    """Suspicious prompt → password → short elevated window; topic change clears it."""

    password: str
    elevation_seconds: int = DEFAULT_ELEVATION_SECONDS
    max_attempts: int = MAX_PASSWORD_ATTEMPTS
    locked: bool = False
    awaiting_password: bool = False
    pending_text: str | None = None
    attempts_left: int = field(default=MAX_PASSWORD_ATTEMPTS)
    elevated_until: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def is_elevated(self) -> bool:
        if not self.enabled:
            return True
        if self.elevated_until <= 0:
            return False
        if time.monotonic() >= self.elevated_until:
            self.elevated_until = 0.0
            return False
        return True

    def elevation_remaining_seconds(self) -> int:
        if not self.is_elevated():
            return 0
        return max(0, int(self.elevated_until - time.monotonic()))

    def grant_elevation(self) -> None:
        self.elevated_until = time.monotonic() + self.elevation_seconds
        logger.info(
            "Safety elevation granted for %s seconds", self.elevation_seconds
        )

    def revoke_elevation(self, reason: str = "") -> str | None:
        """Clear elevated window. Returns a user-facing note if it was active."""
        was = self.is_elevated()
        self.elevated_until = 0.0
        if was:
            note = "Сессия повышенного доступа сброшена"
            if reason:
                note += f" ({reason})"
            note += ". Для опасных запросов снова нужен пароль."
            logger.info("Safety elevation revoked: %s", reason or "unspecified")
            return note
        return None

    def looks_sensitive(self, text: str) -> bool:
        """Heuristic match only (ignores elevation / enabled)."""
        normalized = text.strip()
        if not normalized:
            return False
        for pattern in _SUSPICIOUS_PATTERNS:
            if pattern.search(normalized):
                logger.info("Suspicious prompt matched: %s", pattern.pattern)
                return True
        return False

    def is_suspicious(self, text: str) -> bool:
        """True when a password challenge should start (gate on, not elevated)."""
        if not self.enabled:
            return False
        if self.is_elevated():
            return False
        return self.looks_sensitive(text)


    def begin_challenge(self, pending_text: str) -> str:
        self.awaiting_password = True
        self.pending_text = pending_text
        self.attempts_left = self.max_attempts
        minutes = max(1, self.elevation_seconds // 60)
        return (
            "Внимание: запрос выглядит потенциально опасным.\n"
            f"Введите пароль подтверждения ({self.attempts_left} попытки).\n"
            f"После успеха опасные запросы будут разрешены на {minutes} мин "
            "(сбрасывается при смене темы).\n"
            "Или /cancel — отменить запрос."
        )

    def cancel_challenge(self) -> str:
        self.awaiting_password = False
        self.pending_text = None
        self.attempts_left = self.max_attempts
        return "Запрос отменён."

    def locked_message(self) -> str:
        return (
            "Доступ заблокирован после неверных попыток пароля.\n"
            "Перезапустите PC Admin Bot на ПК, затем снова напишите боту.\n"
            "После перезапуска опасные команды снова потребуют пароль (3 попытки)."
        )

    def submit_password(self, candidate: str) -> tuple[str, str | None]:
        """
        Returns (reply_to_user, pending_text_if_unlocked).
        pending_text_if_unlocked is set only when password is correct.
        """
        if self.locked:
            return self.locked_message(), None
        if not self.awaiting_password:
            return "Сейчас пароль не запрашивается.", None

        if candidate == self.password:
            pending = self.pending_text
            self.awaiting_password = False
            self.pending_text = None
            self.attempts_left = self.max_attempts
            self.grant_elevation()
            minutes = max(1, self.elevation_seconds // 60)
            return (
                "Пароль принят. "
                f"Повышенный доступ на {minutes} мин "
                f"(осталось ~{self.elevation_remaining_seconds() // 60} мин "
                f"{self.elevation_remaining_seconds() % 60} с). "
                "Смена темы сбросит доступ. Отправляю запрос агенту…",
                pending,
            )

        self.attempts_left -= 1
        if self.attempts_left <= 0:
            self.locked = True
            self.awaiting_password = False
            self.pending_text = None
            self.elevated_until = 0.0
            logger.warning("Safety gate locked after failed password attempts")
            return self.locked_message(), None

        return (
            f"Неверный пароль. Осталось попыток: {self.attempts_left}.\n"
            "/cancel — отменить запрос.",
            None,
        )
