"""Unit tests for admin alerting (§7.3). The aiogram Bot is mocked throughout.

Two properties carry the feature: it must be *quiet* (ordinary user failures never
alert, and nothing repeats inside its cooldown), and it must never break the request
it is reporting on.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tgdl.bot import alerts
from tgdl.downloader.gallerydl import AuthRequiredError
from tgdl.downloader.models import (
    DownloadTimeoutError,
    ExtractionError,
    MediaTooLargeError,
    TranscodeError,
    TransientExtractionError,
    UnsupportedUrlError,
)

ADMIN_ID = 424242


@pytest.fixture(autouse=True)
def _reset_alerts():
    alerts.reset()
    yield
    alerts.reset()


@pytest.fixture
def bot() -> MagicMock:
    """A Bot double whose send_message records what the admin would receive."""
    mock = MagicMock(name="Bot")
    mock.send_message = AsyncMock()
    return mock


@pytest.fixture
def configured(bot: MagicMock) -> MagicMock:
    alerts.configure(bot, ADMIN_ID)
    return bot


def sent_texts(bot: MagicMock) -> list[str]:
    return [call.args[1] for call in bot.send_message.await_args_list]


class TestConfiguration:
    def test_enabled_with_an_admin_id(self, configured):
        assert alerts.enabled() is True

    def test_disabled_without_an_admin_id(self, bot):
        alerts.configure(bot, 0)
        assert alerts.enabled() is False

    def test_disabled_without_a_bot(self):
        alerts.configure(None, ADMIN_ID)
        assert alerts.enabled() is False

    async def test_notify_is_a_no_op_when_admin_id_is_zero(self, bot):
        alerts.configure(bot, 0)

        await alerts.notify("k", "something broke")

        bot.send_message.assert_not_awaited()

    async def test_report_failure_is_a_no_op_when_disabled(self, bot):
        """This is the ADMIN_ALERTS=false wiring: main passes bot=None."""
        alerts.configure(None, ADMIN_ID)

        await alerts.report_failure("youtube", TranscodeError("ffmpeg gone"))

        bot.send_message.assert_not_awaited()

    async def test_configure_clears_previous_state(self, bot):
        alerts.configure(bot, ADMIN_ID)
        await alerts.notify("k", "first")

        # A fresh configure (a restart) must not inherit the old cooldown.
        alerts.configure(bot, ADMIN_ID)
        await alerts.notify("k", "second")

        assert len(sent_texts(bot)) == 2


class TestNotify:
    async def test_sends_to_the_admin_with_the_prefix(self, configured):
        await alerts.notify("k", "disk is on fire")

        configured.send_message.assert_awaited_once()
        chat_id, text = configured.send_message.await_args.args
        assert chat_id == ADMIN_ID
        assert alerts.ALERT_PREFIX in text
        assert "disk is on fire" in text

    async def test_same_key_is_suppressed_inside_the_cooldown(self, configured):
        await alerts.notify("k", "first")
        await alerts.notify("k", "second")

        assert sent_texts(configured) == [f"{alerts.ALERT_PREFIX} first"]

    async def test_different_keys_are_independent(self, configured):
        await alerts.notify("a", "one")
        await alerts.notify("b", "two")

        assert len(sent_texts(configured)) == 2

    async def test_key_alerts_again_once_the_cooldown_expires(self, configured, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(alerts.time, "monotonic", lambda: clock[0])

        await alerts.notify("k", "first")
        clock[0] += alerts.COOLDOWN_S + 1
        await alerts.notify("k", "later")

        assert len(sent_texts(configured)) == 2

    async def test_a_telegram_failure_never_raises(self, configured):
        configured.send_message.side_effect = RuntimeError("admin blocked the bot")

        await alerts.notify("k", "anything")  # must not raise


class TestNeverAlertTier:
    """User-level failures are answers, not outages."""

    @pytest.mark.parametrize(
        "error",
        [UnsupportedUrlError("not a link"), MediaTooLargeError("1.2GB")],
    )
    async def test_user_level_errors_never_alert(self, configured, error):
        # Far more than the burst threshold: these must never alert at any rate.
        for _ in range(alerts.BURST_THRESHOLD * 3):
            await alerts.report_failure("youtube", error, "https://x.test/1")

        configured.send_message.assert_not_awaited()


class TestImmediateTier:
    async def test_transcode_error_alerts_on_the_first_occurrence(self, configured):
        await alerts.report_failure("youtube", TranscodeError("ffmpeg not found"))

        text = sent_texts(configured)[0]
        assert "TranscodeError" in text
        assert "youtube" in text
        assert "ffmpeg not found" in text

    async def test_unexpected_exception_alerts_immediately(self, configured):
        await alerts.report_failure("tiktok", ValueError("nonsense"))

        text = sent_texts(configured)[0]
        assert "ValueError" in text
        assert "nonsense" in text

    async def test_repeats_are_cooldown_suppressed(self, configured):
        for _ in range(5):
            await alerts.report_failure("youtube", TranscodeError("boom"))

        assert len(sent_texts(configured)) == 1

    async def test_the_failing_url_is_included(self, configured):
        await alerts.report_failure("youtube", ValueError("x"), "https://x.test/abc")

        assert "https://x.test/abc" in sent_texts(configured)[0]

    async def test_long_error_text_is_truncated(self, configured):
        await alerts.report_failure("youtube", ValueError("z" * 5000))

        text = sent_texts(configured)[0]
        assert "z" * alerts.SAMPLE_CHARS in text
        assert "z" * (alerts.SAMPLE_CHARS + 1) not in text

    async def test_error_text_is_html_escaped(self, configured):
        await alerts.report_failure("youtube", ValueError("<b>not markup</b>"))

        text = sent_texts(configured)[0]
        assert "&lt;b&gt;not markup&lt;/b&gt;" in text


class TestBurstTier:
    @pytest.mark.parametrize(
        "factory",
        [
            lambda: TransientExtractionError("429 slow down"),
            lambda: AuthRequiredError("login required"),
            lambda: ExtractionError("no video"),
            lambda: DownloadTimeoutError("gave up"),
        ],
    )
    async def test_silent_below_the_threshold(self, configured, factory):
        for _ in range(alerts.BURST_THRESHOLD - 1):
            await alerts.report_failure("instagram", factory())

        configured.send_message.assert_not_awaited()

    async def test_alerts_at_the_threshold_with_the_count(self, configured):
        for _ in range(alerts.BURST_THRESHOLD):
            await alerts.report_failure("instagram", TransientExtractionError("429"))

        text = sent_texts(configured)[0]
        assert str(alerts.BURST_THRESHOLD) in text
        assert "TransientExtractionError" in text
        assert "instagram" in text
        assert "429" in text

    async def test_the_latest_sample_and_url_are_carried(self, configured):
        for index in range(alerts.BURST_THRESHOLD):
            await alerts.report_failure(
                "instagram", ExtractionError(f"failure {index}"), f"https://ig.test/{index}"
            )

        text = sent_texts(configured)[0]
        last = alerts.BURST_THRESHOLD - 1
        assert f"failure {last}" in text
        assert f"https://ig.test/{last}" in text

    async def test_continued_failures_are_cooldown_suppressed(self, configured):
        for _ in range(alerts.BURST_THRESHOLD * 4):
            await alerts.report_failure("instagram", TransientExtractionError("429"))

        assert len(sent_texts(configured)) == 1

    async def test_platforms_are_counted_separately(self, configured):
        for _ in range(alerts.BURST_THRESHOLD - 1):
            await alerts.report_failure("instagram", ExtractionError("x"))
            await alerts.report_failure("youtube", ExtractionError("x"))

        configured.send_message.assert_not_awaited()

    async def test_error_classes_are_counted_separately(self, configured):
        for _ in range(alerts.BURST_THRESHOLD - 1):
            await alerts.report_failure("instagram", ExtractionError("x"))
            await alerts.report_failure("instagram", DownloadTimeoutError("x"))

        configured.send_message.assert_not_awaited()

    async def test_old_occurrences_fall_out_of_the_window(self, configured, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(alerts.time, "monotonic", lambda: clock[0])

        # Two failures, then a long quiet spell: the pair must not combine with a
        # later one into a false outage.
        for _ in range(alerts.BURST_THRESHOLD - 1):
            await alerts.report_failure("instagram", ExtractionError("x"))
        clock[0] += alerts.BURST_WINDOW_S + 1
        await alerts.report_failure("instagram", ExtractionError("x"))

        configured.send_message.assert_not_awaited()

    async def test_pruning_keeps_the_window_bounded(self, configured, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(alerts.time, "monotonic", lambda: clock[0])

        for _ in range(50):
            await alerts.report_failure("instagram", ExtractionError("x"))
            clock[0] += alerts.BURST_WINDOW_S + 1

        assert all(len(stamps) == 1 for stamps in alerts._bursts.values())

    async def test_auth_error_mentions_cookies(self, configured):
        for _ in range(alerts.BURST_THRESHOLD):
            await alerts.report_failure("instagram", AuthRequiredError("login wall"))

        text = sent_texts(configured)[0].lower()
        assert "cookies" in text or "session" in text
        assert "instagram" in text

    async def test_a_send_failure_never_raises(self, configured):
        configured.send_message.side_effect = RuntimeError("network down")

        for _ in range(alerts.BURST_THRESHOLD):
            await alerts.report_failure("instagram", ExtractionError("x"))  # must not raise


class TestReset:
    async def test_reset_disables_and_clears(self, configured):
        await alerts.report_failure("youtube", TranscodeError("boom"))

        alerts.reset()

        assert alerts.enabled() is False
        assert alerts._bursts == {}
        assert alerts._last_sent == {}
