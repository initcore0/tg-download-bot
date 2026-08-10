"""Tests for the entrypoint (tgdl/main.py) and bot runtime state."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tgdl import main as main_mod
from tgdl.bot import runtime
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
        assert polling_kwargs["allowed_updates"] == ["message", "channel_post"]

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


class TestResponses:
    def test_start_and_help_include_username(self):
        from tgdl.bot import responses

        assert "@mybot" in responses.start_text("mybot")
        assert "@mybot" in responses.help_text("mybot")

    def test_fallback_username_when_unknown(self):
        from tgdl.bot import responses

        assert responses.DEFAULT_USERNAME in responses.start_text(None)
