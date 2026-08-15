"""Tests for the entrypoint (tgdl/main.py) and bot runtime state."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tgdl import main as main_mod
from tgdl.bot import alerts, handlers, runtime
from tgdl.config import Settings


@pytest.fixture(autouse=True)
def _reset_runtime():
    runtime.reset()
    yield
    runtime.reset()


class TestTokenValidation:
    def test_missing_token_exits_with_clear_error(self, monkeypatch, capsys):
        monkeypatch.setattr(
            main_mod, "load_settings", lambda: Settings(telegram_bot_token="")
        )

        with pytest.raises(SystemExit) as exc:
            main_mod.main()

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "TELEGRAM_BOT_TOKEN" in err

    def test_whitespace_token_also_rejected(self, monkeypatch, capsys):
        monkeypatch.setattr(
            main_mod, "load_settings", lambda: Settings(telegram_bot_token="   ")
        )

        with pytest.raises(SystemExit) as exc:
            main_mod.main()

        assert exc.value.code == 1
        assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err

    def test_valid_token_starts_run(self, monkeypatch, tmp_path):
        settings = Settings(
            telegram_bot_token="123:ABC", database_path=tmp_path / "db.sqlite"
        )
        monkeypatch.setattr(main_mod, "load_settings", lambda: settings)

        called: dict = {}

        def fake_asyncio_run(coro):
            called["ran"] = True
            coro.close()  # never actually poll Telegram

        monkeypatch.setattr(main_mod.asyncio, "run", fake_asyncio_run)

        main_mod.main()

        assert called.get("ran") is True


class TestRun:
    async def test_run_initializes_and_shuts_down(self, monkeypatch, tmp_path):
        settings = Settings(
            telegram_bot_token="123:ABC",
            database_path=tmp_path / "db.sqlite",
            max_concurrent_downloads=4,
        )

        init_db = AsyncMock()
        close_db = AsyncMock()
        monkeypatch.setattr(main_mod.repo, "init_db", init_db)
        monkeypatch.setattr(main_mod.repo, "close_db", close_db)

        bot = MagicMock()
        bot.me = AsyncMock(return_value=SimpleNamespace(username="mybot", id=1))
        bot.delete_webhook = AsyncMock()
        bot.session.close = AsyncMock()
        monkeypatch.setattr(main_mod, "Bot", MagicMock(return_value=bot))

        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock()
        dispatcher.__setitem__ = MagicMock()
        monkeypatch.setattr(main_mod, "Dispatcher", MagicMock(return_value=dispatcher))

        await main_mod.run(settings)

        init_db.assert_awaited_once_with(settings.database_path)
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=True)
        dispatcher.include_router.assert_called_once()

        polling_kwargs = dispatcher.start_polling.await_args.kwargs
        assert polling_kwargs["allowed_updates"] == [
            "message",
            "channel_post",
            "inline_query",
        ]

        # Username cached for mention matching; graceful shutdown ran.
        assert runtime.get_bot_username() == "mybot"
        close_db.assert_awaited_once()
        bot.session.close.assert_awaited_once()

    async def test_run_fails_fast_on_invalid_token(self, monkeypatch, tmp_path):
        """A 401 from Telegram -> SystemExit with a clear message, session still closed."""
        settings = Settings(
            telegram_bot_token="123:ABC", database_path=tmp_path / "db.sqlite"
        )

        monkeypatch.setattr(main_mod.repo, "init_db", AsyncMock())
        monkeypatch.setattr(main_mod.repo, "close_db", AsyncMock())

        bot = MagicMock()
        bot.me = AsyncMock(
            side_effect=main_mod.TelegramUnauthorizedError(
                method=MagicMock(), message="Unauthorized"
            )
        )
        bot.delete_webhook = AsyncMock()
        bot.session.close = AsyncMock()
        monkeypatch.setattr(main_mod, "Bot", MagicMock(return_value=bot))

        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock()
        dispatcher.__setitem__ = MagicMock()
        monkeypatch.setattr(main_mod, "Dispatcher", MagicMock(return_value=dispatcher))

        with pytest.raises(SystemExit, match="401"):
            await main_mod.run(settings)

        dispatcher.start_polling.assert_not_awaited()
        bot.session.close.assert_awaited_once()

    async def test_session_closed_even_when_polling_raises(self, monkeypatch, tmp_path):
        settings = Settings(
            telegram_bot_token="123:ABC", database_path=tmp_path / "db.sqlite"
        )
        monkeypatch.setattr(main_mod.repo, "init_db", AsyncMock())
        close_db = AsyncMock()
        monkeypatch.setattr(main_mod.repo, "close_db", close_db)

        bot = MagicMock()
        bot.me = AsyncMock(return_value=SimpleNamespace(username="mybot", id=1))
        bot.delete_webhook = AsyncMock()
        bot.session.close = AsyncMock()
        monkeypatch.setattr(main_mod, "Bot", MagicMock(return_value=bot))

        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock(side_effect=RuntimeError("network gone"))
        dispatcher.__setitem__ = MagicMock()
        monkeypatch.setattr(main_mod, "Dispatcher", MagicMock(return_value=dispatcher))

        with pytest.raises(RuntimeError):
            await main_mod.run(settings)

        close_db.assert_awaited_once()
        bot.session.close.assert_awaited_once()

    async def test_username_resolution_failure_is_tolerated(self, monkeypatch, tmp_path):
        settings = Settings(
            telegram_bot_token="123:ABC", database_path=tmp_path / "db.sqlite"
        )
        monkeypatch.setattr(main_mod.repo, "init_db", AsyncMock())
        monkeypatch.setattr(main_mod.repo, "close_db", AsyncMock())

        bot = MagicMock()
        bot.me = AsyncMock(side_effect=RuntimeError("unauthorized"))
        bot.delete_webhook = AsyncMock()
        bot.session.close = AsyncMock()
        monkeypatch.setattr(main_mod, "Bot", MagicMock(return_value=bot))

        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock()
        dispatcher.__setitem__ = MagicMock()
        monkeypatch.setattr(main_mod, "Dispatcher", MagicMock(return_value=dispatcher))

        await main_mod.run(settings)  # must not raise

        assert runtime.get_bot_username() is None


class TestWorkdirSweep:
    """Crashes and post-timeout zombie yt-dlp threads leak `req-*` dirs; sweep them."""

    TIMEOUT_S = 300

    def _workdir(self, base: Path, name: str, *, age_s: float) -> Path:
        d = base / name
        d.mkdir(parents=True)
        (d / "partial.part").write_bytes(b"junk")
        stamp = time.time() - age_s
        os.utime(d, (stamp, stamp))
        return d

    def test_old_workdirs_are_removed(self, tmp_path):
        old = self._workdir(tmp_path, "req-abc123", age_s=self.TIMEOUT_S * 5)

        assert main_mod.sweep_orphan_workdirs(tmp_path, self.TIMEOUT_S) == 1
        assert not old.exists()

    def test_recent_workdirs_survive(self, tmp_path):
        """A download running right now must never have its workdir pulled away."""
        live = self._workdir(tmp_path, "req-live", age_s=5)
        # Still inside the timeout window, just past it once over: not yet orphaned.
        recent = self._workdir(tmp_path, "req-recent", age_s=self.TIMEOUT_S * 1.5)

        assert main_mod.sweep_orphan_workdirs(tmp_path, self.TIMEOUT_S) == 0
        assert live.exists() and recent.exists()

    def test_unrelated_entries_are_left_alone(self, tmp_path):
        keeper = tmp_path / "keep-me"
        keeper.mkdir()
        os.utime(keeper, (0, 0))
        stray_file = tmp_path / "req-not-a-dir.txt"
        stray_file.write_bytes(b"x")
        os.utime(stray_file, (0, 0))

        assert main_mod.sweep_orphan_workdirs(tmp_path, self.TIMEOUT_S) == 0
        assert keeper.exists() and stray_file.exists()

    def test_missing_download_dir_is_not_an_error(self, tmp_path):
        assert main_mod.sweep_orphan_workdirs(tmp_path / "nope", self.TIMEOUT_S) == 0

    def test_sweep_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            main_mod.Path, "is_dir", MagicMock(side_effect=OSError("disk gone"))
        )
        assert main_mod.sweep_orphan_workdirs(tmp_path, self.TIMEOUT_S) == 0

    def test_prefix_matches_the_one_handlers_use(self, tmp_path):
        # The sweep is only correct while both sides agree on the prefix.
        real = self._workdir(
            tmp_path, f"{handlers.WORKDIR_PREFIX}xyz", age_s=self.TIMEOUT_S * 5
        )
        assert main_mod.sweep_orphan_workdirs(tmp_path, self.TIMEOUT_S) == 1
        assert not real.exists()


class TestStartupHousekeeping:
    """Neither prune nor sweep may ever keep the bot from starting."""

    def _patch_bot(self, monkeypatch):
        bot = MagicMock()
        bot.me = AsyncMock(return_value=SimpleNamespace(username="mybot", id=1))
        bot.delete_webhook = AsyncMock()
        bot.session.close = AsyncMock()
        monkeypatch.setattr(main_mod, "Bot", MagicMock(return_value=bot))

        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock()
        dispatcher.__setitem__ = MagicMock()
        monkeypatch.setattr(main_mod, "Dispatcher", MagicMock(return_value=dispatcher))

    async def test_run_prunes_audit_and_sweeps_workdirs(self, monkeypatch, tmp_path):
        settings = Settings(
            telegram_bot_token="123:ABC",
            database_path=tmp_path / "db.sqlite",
            download_dir=tmp_path / "downloads",
        )
        monkeypatch.setattr(main_mod.repo, "init_db", AsyncMock())
        monkeypatch.setattr(main_mod.repo, "close_db", AsyncMock())
        prune = AsyncMock(return_value={"deleted": 3, "stale_pending": 1})
        monkeypatch.setattr(main_mod.repo, "prune_audit", prune)
        sweep = MagicMock(return_value=0)
        monkeypatch.setattr(main_mod, "sweep_orphan_workdirs", sweep)
        self._patch_bot(monkeypatch)

        await main_mod.run(settings)

        prune.assert_awaited_once()
        sweep.assert_called_once_with(settings.download_dir, settings.download_timeout_s)

    async def test_prune_failure_does_not_block_startup(self, monkeypatch, tmp_path):
        settings = Settings(
            telegram_bot_token="123:ABC", database_path=tmp_path / "db.sqlite"
        )
        monkeypatch.setattr(main_mod.repo, "init_db", AsyncMock())
        monkeypatch.setattr(main_mod.repo, "close_db", AsyncMock())
        monkeypatch.setattr(
            main_mod.repo, "prune_audit", AsyncMock(side_effect=RuntimeError("db busy"))
        )
        self._patch_bot(monkeypatch)

        await main_mod.run(settings)  # must not raise

        assert runtime.get_bot_username() == "mybot"


class TestCookies:
    """Each jar can arrive as env-var content (raw, base64, or escaped) or a file path."""

    NETSCAPE = "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsid\tabc123\n"

    def _settings(self, **kwargs) -> Settings:
        # Pin every cookies field so the host's real .env/environment can't leak in.
        defaults: dict = {
            "telegram_bot_token": "t",
            "cookies": "",
            "cookies_file": None,
            "youtube_cookies": "",
            "youtube_cookies_file": None,
            "instagram_cookies": "",
            "instagram_cookies_file": None,
        }
        defaults.update(kwargs)
        return Settings(**defaults)

    def _cleanup(self, temp_files):
        for f in temp_files:
            f.unlink(missing_ok=True)

    def test_raw_content_is_written_to_temp_file(self):
        jars, temp = main_mod._materialize_cookies(self._settings(cookies=self.NETSCAPE))
        try:
            assert jars["generic"] is not None and jars["generic"] in temp
            assert jars["generic"].read_text() == self.NETSCAPE
            assert (jars["generic"].stat().st_mode & 0o777) == 0o600
            assert jars["youtube"] is None and jars["instagram"] is None
        finally:
            self._cleanup(temp)

    def test_base64_content_is_decoded(self):
        import base64

        encoded = base64.b64encode(self.NETSCAPE.encode()).decode()
        jars, temp = main_mod._materialize_cookies(self._settings(cookies=encoded))
        try:
            assert jars["generic"].read_text() == self.NETSCAPE
        finally:
            self._cleanup(temp)

    def test_single_line_escaped_content_is_unescaped(self):
        escaped = self.NETSCAPE.rstrip("\n").replace("\t", "\\t").replace("\n", "\\n")
        assert "\n" not in escaped  # what a one-line dashboard paste looks like
        jars, temp = main_mod._materialize_cookies(self._settings(cookies=escaped))
        try:
            assert jars["generic"].read_text() == self.NETSCAPE
        finally:
            self._cleanup(temp)

    def test_each_platform_gets_its_own_jar(self):
        jars, temp = main_mod._materialize_cookies(
            self._settings(
                youtube_cookies=self.NETSCAPE.replace("sid", "yt"),
                instagram_cookies=self.NETSCAPE.replace("sid", "ig"),
            )
        )
        try:
            assert jars["generic"] is None
            assert "yt" in jars["youtube"].read_text()
            assert "ig" in jars["instagram"].read_text()
            assert len(temp) == 2
        finally:
            self._cleanup(temp)

    def test_content_wins_over_file_path(self, tmp_path):
        file_cookies = tmp_path / "cookies.txt"
        file_cookies.write_text("# from file\n")
        jars, temp = main_mod._materialize_cookies(
            self._settings(cookies=self.NETSCAPE, cookies_file=file_cookies)
        )
        try:
            assert jars["generic"] != file_cookies
            assert jars["generic"].read_text() == self.NETSCAPE
        finally:
            self._cleanup(temp)

    def test_file_path_used_when_no_content(self, tmp_path):
        file_cookies = tmp_path / "insta.txt"
        file_cookies.write_text("# from file\n")
        jars, temp = main_mod._materialize_cookies(
            self._settings(instagram_cookies_file=file_cookies)
        )
        assert jars["instagram"] == file_cookies
        assert temp == []  # nothing materialized -> nothing to clean up

    def test_nothing_configured_returns_no_jars(self):
        jars, temp = main_mod._materialize_cookies(self._settings())
        assert all(v is None for v in jars.values())
        assert temp == []


class TestYtdlpFreshnessCheck:
    """Extractors break weekly; a stale yt-dlp must be visible in the logs."""

    def _stub_aiohttp(self, monkeypatch, *, latest: str | None, error: Exception | None = None):
        """Patch aiohttp so no request leaves the machine."""
        import aiohttp

        class _Response:
            def raise_for_status(self):
                pass

            async def json(self):
                return {"info": {"version": latest}}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Session:
            def __init__(self, *args, **kwargs):
                pass

            def get(self, url):
                if error is not None:
                    raise error
                return _Response()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(aiohttp, "ClientSession", _Session)

    async def test_warns_when_a_newer_release_exists(self, monkeypatch, caplog):
        import yt_dlp.version

        monkeypatch.setattr(yt_dlp.version, "__version__", "2020.01.01")
        self._stub_aiohttp(monkeypatch, latest="2099.12.31")

        with caplog.at_level("WARNING", logger="tgdl"):
            await main_mod.check_ytdlp_freshness()

        assert any(
            "2020.01.01" in r.message and "2099.12.31" in r.message
            for r in caplog.records
        )

    async def test_a_stale_version_also_alerts_the_admin(self, monkeypatch):
        """Nobody reads logs before the outage, so the admin is told once (§7.3)."""
        import yt_dlp.version

        monkeypatch.setattr(yt_dlp.version, "__version__", "2020.01.01")
        self._stub_aiohttp(monkeypatch, latest="2099.12.31")
        notify = AsyncMock()
        monkeypatch.setattr(main_mod.alerts, "notify", notify)

        await main_mod.check_ytdlp_freshness()

        notify.assert_awaited_once()
        key, text = notify.await_args.args
        assert key == "ytdlp-stale"
        assert "2020.01.01" in text and "2099.12.31" in text

    async def test_a_current_version_does_not_alert(self, monkeypatch):
        import yt_dlp.version

        monkeypatch.setattr(yt_dlp.version, "__version__", "2099.12.31")
        self._stub_aiohttp(monkeypatch, latest="2099.12.31")
        notify = AsyncMock()
        monkeypatch.setattr(main_mod.alerts, "notify", notify)

        await main_mod.check_ytdlp_freshness()

        notify.assert_not_awaited()

    async def test_silent_when_current(self, monkeypatch, caplog):
        import yt_dlp.version

        monkeypatch.setattr(yt_dlp.version, "__version__", "2099.12.31")
        self._stub_aiohttp(monkeypatch, latest="2099.12.31")

        with caplog.at_level("WARNING", logger="tgdl"):
            await main_mod.check_ytdlp_freshness()

        assert caplog.records == []

    async def test_network_failure_is_swallowed(self, monkeypatch, caplog):
        self._stub_aiohttp(monkeypatch, latest=None, error=OSError("no route to host"))

        with caplog.at_level("WARNING", logger="tgdl"):
            await main_mod.check_ytdlp_freshness()  # must not raise

        assert caplog.records == []

    async def test_unexpected_payload_is_swallowed(self, monkeypatch):
        import aiohttp

        class _Broken:
            def __init__(self, *a, **kw):
                pass

            def get(self, url):
                raise ValueError("not json")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(aiohttp, "ClientSession", _Broken)
        await main_mod.check_ytdlp_freshness()  # must not raise

    async def test_startup_launches_it_without_awaiting(self, monkeypatch, tmp_path):
        """It runs in the background and is cancelled cleanly on shutdown."""
        settings = Settings(
            telegram_bot_token="123:ABC", database_path=tmp_path / "db.sqlite"
        )
        monkeypatch.setattr(main_mod.repo, "init_db", AsyncMock())
        monkeypatch.setattr(main_mod.repo, "close_db", AsyncMock())
        monkeypatch.setattr(
            main_mod.repo, "prune_audit",
            AsyncMock(return_value={"deleted": 0, "stale_pending": 0}),
        )

        ran = asyncio.Event()

        async def _check():
            ran.set()

        monkeypatch.setattr(main_mod, "check_ytdlp_freshness", _check)

        bot = MagicMock()
        bot.me = AsyncMock(return_value=SimpleNamespace(username="mybot", id=1))
        bot.delete_webhook = AsyncMock()
        bot.session.close = AsyncMock()
        monkeypatch.setattr(main_mod, "Bot", MagicMock(return_value=bot))

        # Real polling blocks for a long time; a single yield stands in for that, and
        # is what lets the background task get its turn.
        async def _poll(*args, **kwargs):
            await asyncio.sleep(0)

        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock(side_effect=_poll)
        dispatcher.__setitem__ = MagicMock()
        monkeypatch.setattr(main_mod, "Dispatcher", MagicMock(return_value=dispatcher))

        await main_mod.run(settings)

        assert ran.is_set()

    async def test_a_hanging_check_does_not_block_shutdown(self, monkeypatch, tmp_path):
        settings = Settings(
            telegram_bot_token="123:ABC", database_path=tmp_path / "db.sqlite"
        )
        monkeypatch.setattr(main_mod.repo, "init_db", AsyncMock())
        close_db = AsyncMock()
        monkeypatch.setattr(main_mod.repo, "close_db", close_db)
        monkeypatch.setattr(
            main_mod.repo, "prune_audit",
            AsyncMock(return_value={"deleted": 0, "stale_pending": 0}),
        )

        async def _hang():
            await asyncio.Event().wait()  # never completes

        monkeypatch.setattr(main_mod, "check_ytdlp_freshness", _hang)

        bot = MagicMock()
        bot.me = AsyncMock(return_value=SimpleNamespace(username="mybot", id=1))
        bot.delete_webhook = AsyncMock()
        bot.session.close = AsyncMock()
        monkeypatch.setattr(main_mod, "Bot", MagicMock(return_value=bot))

        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock()
        dispatcher.__setitem__ = MagicMock()
        monkeypatch.setattr(main_mod, "Dispatcher", MagicMock(return_value=dispatcher))

        await asyncio.wait_for(main_mod.run(settings), timeout=5)

        close_db.assert_awaited_once()


class TestRuntimeState:
    def test_username_normalized_without_at(self):
        runtime.set_bot_username("@somebot")
        assert runtime.get_bot_username() == "somebot"

    def test_username_none(self):
        runtime.set_bot_username(None)
        assert runtime.get_bot_username() is None

    def test_semaphore_defaults_when_unconfigured(self):
        runtime.reset()
        assert runtime.get_semaphore()._value == 3

    def test_configure_sets_capacity(self):
        runtime.configure(7, "bot")
        assert runtime.get_semaphore()._value == 7
        assert runtime.get_bot_username() == "bot"

    def test_semaphore_is_stable_across_calls(self):
        runtime.configure(2)
        assert runtime.get_semaphore() is runtime.get_semaphore()

    def test_reset_clears_the_coalescing_state(self):
        runtime.configure(2, follower_timeout_s=12)
        runtime._leaders["https://x.test/1"] = asyncio.Event()

        runtime.reset()

        assert runtime._leaders == {}
        assert runtime._follower_timeout_s == runtime.DEFAULT_FOLLOWER_TIMEOUT_S


class TestCoalesceGate:
    """Leader/follower gate for identical in-flight URLs (runtime-level contract)."""

    async def test_first_caller_is_the_leader(self):
        runtime.configure(2)
        async with runtime.coalesce("https://x.test/1") as leader:
            assert leader is True
            assert "https://x.test/1" in runtime._leaders

    async def test_entry_is_removed_and_event_set_on_exit(self):
        runtime.configure(2)
        async with runtime.coalesce("https://x.test/1"):
            event = runtime._leaders["https://x.test/1"]
        assert runtime._leaders == {}
        assert event.is_set()

    async def test_entry_is_released_even_when_the_body_raises(self):
        runtime.configure(2)
        with pytest.raises(RuntimeError):
            async with runtime.coalesce("https://x.test/1"):
                event = runtime._leaders["https://x.test/1"]
                raise RuntimeError("leader exploded")
        assert runtime._leaders == {}
        assert event.is_set()

    async def test_entry_is_released_on_cancellation(self):
        runtime.configure(2)
        entered = asyncio.Event()

        async def _leader():
            async with runtime.coalesce("https://x.test/1"):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(_leader())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert runtime._leaders == {}

    async def test_follower_waits_for_the_leader_then_reports_false(self):
        runtime.configure(2)
        order: list[str] = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _leader():
            async with runtime.coalesce("https://x.test/1"):
                entered.set()
                await release.wait()
                order.append("leader done")

        async def _follower():
            async with runtime.coalesce("https://x.test/1") as leader:
                assert leader is False
                order.append("follower resumed")

        leader_task = asyncio.create_task(_leader())
        await entered.wait()
        follower_task = asyncio.create_task(_follower())
        await asyncio.sleep(0.02)
        assert order == []  # still parked

        release.set()
        await asyncio.gather(leader_task, follower_task)
        assert order == ["leader done", "follower resumed"]

    async def test_follower_gives_up_after_the_configured_timeout(self):
        runtime.configure(2, follower_timeout_s=0.02)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _leader():
            async with runtime.coalesce("https://x.test/1"):
                entered.set()
                await release.wait()

        leader_task = asyncio.create_task(_leader())
        await entered.wait()

        async with runtime.coalesce("https://x.test/1") as leader:
            assert leader is False  # released by timeout, not by the leader

        release.set()
        await leader_task

    async def test_a_follower_does_not_become_a_leader(self):
        """One level only — no election loop for a third caller to queue behind."""
        runtime.configure(2, follower_timeout_s=0.02)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _leader():
            async with runtime.coalesce("https://x.test/1"):
                entered.set()
                await release.wait()

        leader_task = asyncio.create_task(_leader())
        await entered.wait()

        async with runtime.coalesce("https://x.test/1"):
            # The map still points at the real leader, never at the follower.
            assert runtime._leaders["https://x.test/1"] is not None

        release.set()
        await leader_task
        assert runtime._leaders == {}

    async def test_empty_url_is_never_gated(self):
        runtime.configure(2)
        async with runtime.coalesce("") as leader:
            assert leader is True
        assert runtime._leaders == {}

    async def test_distinct_urls_do_not_block_each_other(self):
        runtime.configure(2)
        async with (
            runtime.coalesce("https://x.test/1"),
            runtime.coalesce("https://x.test/2") as leader,
        ):
            assert leader is True


class TestResponses:
    def test_start_and_help_include_username(self):
        from tgdl.bot import responses

        assert "@mybot" in responses.start_text("mybot")
        assert "@mybot" in responses.help_text("mybot")

    def test_fallback_username_when_unknown(self):
        from tgdl.bot import responses

        assert responses.DEFAULT_USERNAME in responses.start_text(None)

    def test_help_mentions_the_audio_command(self):
        from tgdl.bot import responses

        assert "/mp3" in responses.help_text("mybot")
        assert "/mp3" in responses.help_text("mybot", "ru")

    def test_start_mentions_the_audio_command(self):
        from tgdl.bot import responses

        assert "/mp3" in responses.start_text("mybot")
        assert "/mp3" in responses.start_text("mybot", "ru")


STATS_DATA = {
    "requests": 10,
    "success": 7,
    "failed": 2,
    "pending": 1,
    "cache_hits": 3,
    "hit_rate": 3 / 7,
    "platforms": {
        "youtube": {"count": 5, "p50_s": 4.2, "p95_s": 19.0},
        "tiktok": {"count": 2, "p50_s": 1.1, "p95_s": 1.9},
    },
}


class TestStatsFormatting:
    """`/stats` is admin-facing English, rendered as an escaped monospace block."""

    def test_includes_counts_and_hit_rate(self):
        from tgdl.bot import responses

        text = responses.stats_text(STATS_DATA)

        assert "requests   10" in text
        assert "success    7" in text
        assert "cache hits 3 (43%)" in text

    def test_includes_the_platform_table(self):
        from tgdl.bot import responses

        text = responses.stats_text(STATS_DATA)

        assert "youtube" in text and "tiktok" in text
        assert "4.2s" in text and "19.0s" in text

    def test_wrapped_in_pre_and_html_escaped(self):
        from tgdl.bot import responses

        text = responses.stats_text(
            {**STATS_DATA, "platforms": {"<b>evil": {"count": 1, "p50_s": 0.0, "p95_s": 0.0}}}
        )

        assert text.startswith("<pre>") and text.endswith("</pre>")
        assert "&lt;b&gt;evil" in text

    def test_empty_platform_table_is_omitted(self):
        from tgdl.bot import responses

        text = responses.stats_text(
            {"requests": 0, "success": 0, "failed": 0, "pending": 0,
             "cache_hits": 0, "hit_rate": 0.0, "platforms": {}}
        )

        assert "last 30d" not in text
        assert "requests   0" in text


class TestSelfHostedBotApi:
    """TELEGRAM_API_URL swaps the API server; unset keeps Telegram's cloud API."""

    def _settings(self, **overrides) -> Settings:
        return Settings(telegram_bot_token="123:ABC", **overrides)

    def test_cloud_api_by_default(self, monkeypatch):
        bot_cls = MagicMock()
        monkeypatch.setattr(main_mod, "Bot", bot_cls)

        main_mod._make_bot(self._settings())

        assert bot_cls.call_args.kwargs["session"] is None

    def test_custom_url_builds_a_session_for_that_server(self, monkeypatch):
        bot_cls = MagicMock()
        monkeypatch.setattr(main_mod, "Bot", bot_cls)

        main_mod._make_bot(self._settings(telegram_api_url="http://local-api:8081"))

        session = bot_cls.call_args.kwargs["session"]
        assert session is not None
        # The session must actually address the local server, not api.telegram.org.
        assert "local-api:8081" in session.api.api_url(token="123:ABC", method="getMe")

    def test_blank_url_is_treated_as_unset(self, monkeypatch):
        bot_cls = MagicMock()
        monkeypatch.setattr(main_mod, "Bot", bot_cls)

        main_mod._make_bot(self._settings(telegram_api_url="   "))

        assert bot_cls.call_args.kwargs["session"] is None

    async def test_run_builds_the_main_bot_through_the_helper(self, monkeypatch, tmp_path):
        settings = Settings(
            telegram_bot_token="123:ABC",
            database_path=tmp_path / "db.sqlite",
            telegram_api_url="http://local-api:8081",
        )
        monkeypatch.setattr(main_mod.repo, "init_db", AsyncMock())
        monkeypatch.setattr(main_mod.repo, "close_db", AsyncMock())

        bot = MagicMock()
        bot.me = AsyncMock(return_value=SimpleNamespace(username="mybot", id=1))
        bot.delete_webhook = AsyncMock()
        bot.session.close = AsyncMock()
        bot_cls = MagicMock(return_value=bot)
        monkeypatch.setattr(main_mod, "Bot", bot_cls)

        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock()
        dispatcher.__setitem__ = MagicMock()
        monkeypatch.setattr(main_mod, "Dispatcher", MagicMock(return_value=dispatcher))

        await main_mod.run(settings)

        assert bot_cls.call_args.kwargs["session"] is not None

    async def test_healthcheck_uses_the_same_server(self, monkeypatch):
        """The probe must not check a different API than the bot actually polls."""
        settings = Settings(
            telegram_bot_token="123:ABC", telegram_api_url="http://local-api:8081"
        )
        bot = MagicMock()
        bot.get_me = AsyncMock()
        bot.session.close = AsyncMock()
        bot_cls = MagicMock(return_value=bot)
        monkeypatch.setattr(main_mod, "Bot", bot_cls)

        assert await main_mod._healthcheck(settings) == 0
        assert bot_cls.call_args.kwargs["session"] is not None

    async def test_healthcheck_uses_the_cloud_api_when_unset(self, monkeypatch):
        settings = Settings(telegram_bot_token="123:ABC")
        bot = MagicMock()
        bot.get_me = AsyncMock()
        bot.session.close = AsyncMock()
        bot_cls = MagicMock(return_value=bot)
        monkeypatch.setattr(main_mod, "Bot", bot_cls)

        assert await main_mod._healthcheck(settings) == 0
        assert bot_cls.call_args.kwargs["session"] is None


class TestAdminAlertWiring:
    """main.run() must configure alerting before anything can want to alert (§7.3)."""

    async def _run(self, monkeypatch, tmp_path, settings):
        monkeypatch.setattr(main_mod.repo, "init_db", AsyncMock())
        monkeypatch.setattr(main_mod.repo, "close_db", AsyncMock())
        monkeypatch.setattr(
            main_mod.repo, "prune_audit",
            AsyncMock(return_value={"deleted": 0, "stale_pending": 0}),
        )
        monkeypatch.setattr(main_mod, "check_ytdlp_freshness", AsyncMock())

        bot = MagicMock()
        bot.me = AsyncMock(return_value=SimpleNamespace(username="mybot", id=1))
        bot.delete_webhook = AsyncMock()
        bot.session.close = AsyncMock()
        monkeypatch.setattr(main_mod, "Bot", MagicMock(return_value=bot))

        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock()
        dispatcher.__setitem__ = MagicMock()
        monkeypatch.setattr(main_mod, "Dispatcher", MagicMock(return_value=dispatcher))

        await main_mod.run(settings)
        return bot

    @pytest.fixture(autouse=True)
    def _reset_alerts(self):
        alerts.reset()
        yield
        alerts.reset()

    async def test_enabled_when_an_admin_id_is_configured(self, monkeypatch, tmp_path):
        settings = Settings(
            telegram_bot_token="123:ABC",
            database_path=tmp_path / "db.sqlite",
            admin_user_id=555,
        )

        await self._run(monkeypatch, tmp_path, settings)

        assert alerts.enabled() is True

    async def test_disabled_without_an_admin_id(self, monkeypatch, tmp_path):
        settings = Settings(
            telegram_bot_token="123:ABC", database_path=tmp_path / "db.sqlite"
        )

        await self._run(monkeypatch, tmp_path, settings)

        assert alerts.enabled() is False

    async def test_admin_alerts_false_keeps_stats_without_dms(self, monkeypatch, tmp_path):
        settings = Settings(
            telegram_bot_token="123:ABC",
            database_path=tmp_path / "db.sqlite",
            admin_user_id=555,
            admin_alerts=False,
        )

        await self._run(monkeypatch, tmp_path, settings)

        assert alerts.enabled() is False
        assert settings.admin_user_id == 555  # /stats is untouched

    async def test_configure_runs_before_the_freshness_task(self, monkeypatch, tmp_path):
        """The freshness check alerts, so it must not start before configure()."""
        settings = Settings(
            telegram_bot_token="123:ABC",
            database_path=tmp_path / "db.sqlite",
            admin_user_id=555,
        )
        seen: list[bool] = []

        async def _check():
            seen.append(alerts.enabled())

        monkeypatch.setattr(main_mod.repo, "init_db", AsyncMock())
        monkeypatch.setattr(main_mod.repo, "close_db", AsyncMock())
        monkeypatch.setattr(
            main_mod.repo, "prune_audit",
            AsyncMock(return_value={"deleted": 0, "stale_pending": 0}),
        )
        monkeypatch.setattr(main_mod, "check_ytdlp_freshness", _check)

        bot = MagicMock()
        bot.me = AsyncMock(return_value=SimpleNamespace(username="mybot", id=1))
        bot.delete_webhook = AsyncMock()
        bot.session.close = AsyncMock()
        monkeypatch.setattr(main_mod, "Bot", MagicMock(return_value=bot))

        async def _poll(*args, **kwargs):
            await asyncio.sleep(0)  # let the background task run

        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock(side_effect=_poll)
        dispatcher.__setitem__ = MagicMock()
        monkeypatch.setattr(main_mod, "Dispatcher", MagicMock(return_value=dispatcher))

        await main_mod.run(settings)

        assert seen == [True]
