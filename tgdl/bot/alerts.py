"""Process-wide admin alerting: DM the admin when the *bot* is unhealthy.

The failures that matter operationally — a flagged Instagram session, YouTube
bot-checks from a stale runtime, a broken ffmpeg — are invisible until a user
complains. When ADMIN_USER_ID is configured (and ADMIN_ALERTS is on), this module
turns them into a Telegram message to the admin.

Two things it deliberately does not do:

* **Alert on ordinary user-level failures.** "That link isn't supported" and "that
  video is too large" are answers, not outages, and an alert on either would train
  the admin to ignore the channel.
* **Spam.** Every alert passes a per-key cooldown, and the noisy error classes are
  additionally rate-limited by a sliding-window burst counter: one throttled request
  is weather, three in fifteen minutes is a climate.

**Anonymity (ARCHITECTURE.md §6).** An alert may name the failing URL, the platform,
and the error class/text. It never carries anything about *who* asked — no user ids,
chat ids, or chat types. The admin learns that the bot is broken, not who was using
it. Nothing here is persisted either; the counters live in memory for the run.

State is module-global in the style of `runtime.py`: `configure()` at startup,
`reset()` for tests.
"""
from __future__ import annotations

import html
import logging
import time
from collections import defaultdict

from aiogram import Bot

from tgdl.downloader.gallerydl import AuthRequiredError
from tgdl.downloader.models import (
    DownloadError,
    DownloadTimeoutError,
    ExtractionError,
    MediaTooLargeError,
    TranscodeError,
    TransientExtractionError,
    UnsupportedUrlError,
)

log = logging.getLogger(__name__)

#: Minimum gap between two alerts sharing a key. An outage that lasts all afternoon
#: is one message an hour, not one per failed request.
COOLDOWN_S = 3600

#: Sliding window for the burst tier, and how many failures inside it constitute an
#: outage worth waking someone for. A single throttled request is normal operation.
BURST_WINDOW_S = 900
BURST_THRESHOLD = 3

#: How much of an error's text an alert carries. Enough to recognize the failure
#: mode; not so much that a stack-trace-ish message fills the screen.
SAMPLE_CHARS = 200

#: Prefix on every alert, so the admin can tell these from the bot's normal output.
ALERT_PREFIX = "⚠️ tgdl:"

#: Error classes that are the *answer* to a request, not a symptom of a sick bot.
_NEVER_ALERT: tuple[type[BaseException], ...] = (UnsupportedUrlError, MediaTooLargeError)

#: Real failures, but ones that happen to healthy bots one at a time — only their
#: rate is interesting, so they go through the burst counter.
_BURST_TIER: tuple[type[BaseException], ...] = (
    TransientExtractionError,
    AuthRequiredError,
    ExtractionError,
    DownloadTimeoutError,
)

_bot: Bot | None = None
_admin_user_id: int = 0

#: Alert key -> monotonic timestamp of the last message sent for it.
_last_sent: dict[str, float] = {}

#: (platform, error class) -> monotonic timestamps of recent failures, pruned to
#: BURST_WINDOW_S on every touch so it cannot grow without bound.
_bursts: dict[tuple[str, str], list[float]] = defaultdict(list)


def configure(bot: Bot | None, admin_user_id: int) -> None:
    """Point alerting at the admin's DM. `admin_user_id=0` (or no bot) disables it.

    Called from `main.run()` once the Bot exists. With alerting off, every public
    function here is a no-op — the admin keeps /stats and gets no DMs.
    """
    global _bot, _admin_user_id
    _bot = bot if admin_user_id else None
    _admin_user_id = admin_user_id if bot is not None else 0
    _last_sent.clear()
    _bursts.clear()
    if enabled():
        log.info("admin alerts enabled")


def enabled() -> bool:
    """True when alerts have somewhere to go."""
    return _bot is not None and _admin_user_id != 0


async def notify(key: str, text: str) -> None:
    """Send one alert to the admin, subject to the per-key cooldown. Never raises.

    `key` is the deduplication identity: a key that alerted within COOLDOWN_S is
    dropped silently. Telegram failures (admin never started the bot, network down)
    are a debug line — an alert that cannot be delivered must not become a second
    incident.
    """
    try:
        if not enabled():
            return

        now = time.monotonic()
        last = _last_sent.get(key)
        if last is not None and now - last < COOLDOWN_S:
            log.debug("alert %s suppressed by cooldown", key)
            return
        _last_sent[key] = now

        await _bot.send_message(_admin_user_id, f"{ALERT_PREFIX} {text}")
        log.info("admin alert sent: %s", key)
    except Exception:
        log.debug("admin alert %s could not be delivered", key, exc_info=True)


async def report_failure(platform: str, error: BaseException, url: str | None = None) -> None:
    """Classify one failed request and alert the admin if it says the bot is sick.

    The single hook the handlers call on every failure. Three tiers:

    * **Never**: unsupported link, media too large — the user's problem, not ours.
    * **Immediate**: a broken ffmpeg (TranscodeError) or any non-DownloadError
      exception. Both mean *every* request is about to fail, so the first one is
      already worth a message.
    * **Burst**: extraction, auth, throttling and timeout failures, which healthy
      bots produce occasionally. Alert only once BURST_THRESHOLD of them land in
      the same BURST_WINDOW_S for one (platform, error class).

    Never raises: alerting is diagnostics, and diagnostics must not break the flow
    they are diagnosing (same rule as the audit wrappers).
    """
    try:
        if not enabled() or isinstance(error, _NEVER_ALERT):
            return

        name = type(error).__name__

        # Anything unexpected is a bug in us, and TranscodeError means ffmpeg itself
        # is broken — neither gets to happen three times before we say so.
        if isinstance(error, TranscodeError) or not isinstance(error, DownloadError):
            await notify(
                f"immediate:{name}",
                f"<b>{html.escape(name)}</b> on <b>{html.escape(platform)}</b>\n"
                f"{_detail(error, url)}",
            )
            return

        if not isinstance(error, _BURST_TIER):
            return

        count = _record_burst(platform, name)
        if count < BURST_THRESHOLD:
            return

        await notify(
            f"burst:{platform}:{name}",
            f"<b>{count} × {html.escape(name)}</b> on <b>{html.escape(platform)}</b> "
            f"in the last {BURST_WINDOW_S // 60} min\n{_detail(error, url)}",
        )
    except Exception:  # pragma: no cover - defensive: alerting never breaks a request
        log.debug("report_failure failed", exc_info=True)


def reset() -> None:
    """Test helper: clear all alert state."""
    global _bot, _admin_user_id
    _bot = None
    _admin_user_id = 0
    _last_sent.clear()
    _bursts.clear()


# --------------------------------------------------------------------------- internals


def _record_burst(platform: str, error_name: str) -> int:
    """Add one occurrence to the sliding window and return the window's new size.

    Old timestamps are dropped on every call and empty buckets removed, so the map
    is bounded by the number of (platform, error class) pairs actually failing right
    now rather than by everything that ever has.
    """
    now = time.monotonic()
    cutoff = now - BURST_WINDOW_S
    key = (platform, error_name)
    recent = [stamp for stamp in _bursts[key] if stamp > cutoff]
    recent.append(now)
    _bursts[key] = recent
    return len(recent)


def _detail(error: BaseException, url: str | None) -> str:
    """The body shared by both tiers: a hint, a text sample, and the failing link.

    Escaped for HTML parse mode, and truncated — never anything identifying.
    """
    lines: list[str] = []
    if isinstance(error, AuthRequiredError):
        # The one failure with an obvious fix, so name it instead of making the
        # admin infer it from an escaped subprocess message.
        lines.append(
            "Cookies/session for this platform look expired, or the account is "
            "flagged — refresh the jar and check the account."
        )

    sample = str(error).strip()
    if sample:
        if len(sample) > SAMPLE_CHARS:
            sample = sample[:SAMPLE_CHARS] + "…"
        lines.append(f"<code>{html.escape(sample)}</code>")
    if url:
        lines.append(html.escape(url))
    return "\n".join(lines)
