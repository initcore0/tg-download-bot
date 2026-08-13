"""Unit tests for the bot layer (M2). All external modules are mocked."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tgdl import i18n
from tgdl.bot import handlers, runtime
from tgdl.config import Settings
from tgdl.downloader.models import (
    DownloadError,
    ExtractionError,
    MediaResult,
    MediaTooLargeError,
    UnsupportedUrlError,
)

BOT_USERNAME = "tgdl_test_bot"


@pytest.fixture(autouse=True)
def _reset_runtime(tmp_path):
    runtime.reset()
    runtime.configure(3, BOT_USERNAME)
    yield
    runtime.reset()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        telegram_bot_token="test-token",
        database_path=tmp_path / "test.db",
        download_dir=tmp_path / "downloads",
        max_file_size_mb=48,
        max_height=720,
        max_concurrent_downloads=3,
        download_timeout_s=300,
    )


def make_message(
    text: str | None = None,
    *,
    chat_type: str = "private",
    chat_id: int = 111,
    message_id: int = 42,
    from_user: bool = True,
    user_id: int = 777,
    language_code: str | None = None,
    caption: str | None = None,
) -> MagicMock:
    """A Message test double with AsyncMock send methods."""
    msg = MagicMock(name="Message")
    msg.text = text
    msg.caption = caption
    msg.message_id = message_id
    msg.chat = SimpleNamespace(id=chat_id, type=chat_type)

    if from_user:
        msg.from_user = SimpleNamespace(
            id=user_id, username="alice", first_name="Alice", last_name="A",
            language_code=language_code,
        )
    else:
        msg.from_user = None

    msg.answer = AsyncMock(return_value=MagicMock(name="SentText"))
    msg.reply = AsyncMock(return_value=MagicMock(name="SentText"))
    msg.answer_video = AsyncMock(return_value=sent_video("vid-file-id"))
    msg.reply_video = AsyncMock(return_value=sent_video("vid-file-id"))
    msg.answer_photo = AsyncMock(return_value=sent_photo("photo-file-id"))
    msg.reply_photo = AsyncMock(return_value=sent_photo("photo-file-id"))
    msg.answer_animation = AsyncMock(return_value=sent_animation("anim-file-id"))
    msg.reply_animation = AsyncMock(return_value=sent_animation("anim-file-id"))
    msg.answer_media_group = AsyncMock(return_value=[sent_photo("group-file-id")])
    msg.reply_media_group = AsyncMock(return_value=[sent_photo("group-file-id")])
    msg.answer_audio = AsyncMock(return_value=sent_audio("audio-file-id"))
    msg.reply_audio = AsyncMock(return_value=sent_audio("audio-file-id"))
    msg.react = AsyncMock()

    msg.bot = MagicMock()
    msg.bot.send_chat_action = AsyncMock()

    return msg


def sent_video(file_id: str):
    return SimpleNamespace(
        video=SimpleNamespace(file_id=file_id), photo=None, animation=None
    )


def sent_photo(file_id: str):
    return SimpleNamespace(
        video=None, animation=None, photo=[SimpleNamespace(file_id=file_id)]
    )


def sent_animation(file_id: str):
    return SimpleNamespace(
        video=None, photo=None, animation=SimpleNamespace(file_id=file_id)
    )


def sent_audio(file_id: str):
    return SimpleNamespace(
        video=None, photo=None, animation=None, audio=SimpleNamespace(file_id=file_id)
    )


def make_media(
    tmp_path: Path, kind: str = "video", name: str = "out.mp4", **overrides
) -> MediaResult:
    path = tmp_path / name
    path.write_bytes(b"fake-media-bytes")
    defaults = {
        "path": path,
        "kind": kind,
        "source_url": "https://example.com/v/1",
        "platform": "youtube",
        "filesize": path.stat().st_size,
        "title": "A clip",
        "width": 1280,
        "height": 720,
        "duration_s": 12.7,
        "transcoded": False,
        "elapsed_s": 1.5,
    }
    defaults.update(overrides)
    return MediaResult(**defaults)


def cached_row(*, file_ids: list[str] | None = None, **overrides) -> SimpleNamespace:
    """An audit row as `find_cached` returns it (a cache hit).

    `file_ids` fills the JSON `telegram_file_ids` column the way `mark_success` does;
    by default the row carries just its single `telegram_file_id`.
    """
    defaults = {
        "id": 7,
        "telegram_file_id": "cached-file-id",
        "telegram_file_ids": json.dumps(file_ids) if file_ids else None,
        "media_kind": "video",
        "platform": "youtube",
        "title": "A clip",
        "filesize_bytes": 4242,
        "width": 1280,
        "height": 720,
        "duration_s": 12.7,
        "transcoded": False,
    }
    defaults.update(overrides)
    if file_ids:
        defaults["telegram_file_id"] = file_ids[0]
    return SimpleNamespace(**defaults)


@pytest.fixture
def mock_repo(monkeypatch):
    """Patch every repo function used by handlers; returns the namespace of mocks.

    `find_cached` defaults to a miss so the standard tests exercise the normal
    download path. `decode_file_ids` is left real — it is pure row parsing.
    """
    request_row = SimpleNamespace(id=99)
    mocks = SimpleNamespace(
        create_request=AsyncMock(return_value=request_row),
        mark_success=AsyncMock(),
        mark_failure=AsyncMock(),
        find_cached=AsyncMock(return_value=None),
    )
    for name in ("create_request", "mark_success", "mark_failure", "find_cached"):
        monkeypatch.setattr(handlers.repo, name, getattr(mocks, name))
    return mocks


@pytest.fixture
def mock_download(monkeypatch):
    """Patch service.download_media; test sets .return_value / .side_effect."""
    mock = AsyncMock()
    monkeypatch.setattr(handlers.service, "download_media", mock)
    return mock


@pytest.fixture
def mock_audio(monkeypatch):
    """Patch audio.download_audio; test sets .return_value / .side_effect."""
    mock = AsyncMock()
    monkeypatch.setattr(handlers.audio_mod, "download_audio", mock)
    return mock


def make_audio(tmp_path: Path, name: str = "track.m4a", **overrides):
    """An AudioResult as `download_audio` returns it."""
    path = tmp_path / name
    path.write_bytes(b"fake-audio-bytes")
    defaults = {
        "path": path,
        "title": "A Song",
        "duration_s": 123.4,
        "filesize": path.stat().st_size,
        "performer": "A Band",
    }
    defaults.update(overrides)
    return handlers.audio_mod.AudioResult(**defaults)


def audio_row(*, file_ids: list[str] | None = None, **overrides) -> SimpleNamespace:
    """A cached audio audit row, as `find_cached(media_kinds=("audio",))` returns it."""
    return cached_row(
        media_kind="audio",
        file_ids=file_ids or ["cached-audio-id"],
        duration_s=123.4,
        **overrides,
    )


@pytest.fixture(autouse=True)
def _stub_urls(monkeypatch):
    """urls.py is an M1 stub raising NotImplementedError -> exercise the fallback regex."""
    from tgdl.downloader import urls as urls_mod

    monkeypatch.setattr(
        urls_mod, "detect_platform", lambda url: "youtube", raising=False
    )
    monkeypatch.setattr(urls_mod, "normalize_url", lambda url: url, raising=False)


# ------------------------------------------------------------------ URL extraction


class TestUrlExtraction:
    def test_finds_plain_url(self):
        assert (
            handlers.extract_first_url("look at https://youtu.be/abc123 nice")
            == "https://youtu.be/abc123"
        )

    def test_returns_first_of_many(self):
        text = "https://a.com/1 and https://b.com/2"
        assert handlers.extract_first_url(text) == "https://a.com/1"

    def test_none_when_no_url(self):
        assert handlers.extract_first_url("just some words") is None
        assert handlers.extract_first_url(None) is None
        assert handlers.extract_first_url("") is None

    def test_uses_extract_urls_when_available(self, monkeypatch):
        from tgdl.downloader import urls as urls_mod

        monkeypatch.setattr(
            urls_mod, "extract_urls", lambda text: ["https://from-module.example/x"]
        )
        assert (
            handlers.extract_first_url("https://ignored.example/y")
            == "https://from-module.example/x"
        )

    def test_falls_back_when_module_is_stub(self):
        # urls.extract_urls still raises NotImplementedError (M1 pending).
        assert (
            handlers.extract_first_url("go https://fallback.example/z")
            == "https://fallback.example/z"
        )


class TestMentionMatching:
    def test_detects_mention_case_insensitively(self):
        msg = make_message(f"@{BOT_USERNAME.upper()} https://x.com/1", chat_type="group")
        assert handlers.mentions_bot(msg, BOT_USERNAME) is True

    def test_no_mention(self):
        msg = make_message("https://x.com/1", chat_type="group")
        assert handlers.mentions_bot(msg, BOT_USERNAME) is False

    def test_no_username_configured(self):
        msg = make_message(f"@{BOT_USERNAME} https://x.com/1", chat_type="group")
        assert handlers.mentions_bot(msg, None) is False


# ---------------------------------------------------------------------- commands


class TestCommands:
    async def test_start_sends_usage(self):
        msg = make_message("/start")
        await handlers.cmd_start(msg)
        msg.answer.assert_awaited_once()
        assert BOT_USERNAME in msg.answer.await_args.args[0]

    async def test_help_sends_usage(self):
        msg = make_message("/help")
        await handlers.cmd_help(msg)
        msg.answer.assert_awaited_once()
        text = msg.answer.await_args.args[0]
        assert "/start" in text and "/help" in text


class TestLocalization:
    async def test_start_default_is_english(self):
        msg = make_message("/start", language_code=None)
        await handlers.cmd_start(msg)
        assert msg.answer.await_args.args[0] == i18n.t("start", "en", username=BOT_USERNAME)

    async def test_help_russian_for_ru_user(self):
        msg = make_message("/help", language_code="ru-RU")
        await handlers.cmd_help(msg)
        text = msg.answer.await_args.args[0]
        assert text == i18n.t("help", "ru", username=BOT_USERNAME)
        assert "Как мной пользоваться" in text  # sanity: actually Russian

    async def test_english_for_unsupported_language(self):
        msg = make_message("/help", language_code="de")
        await handlers.cmd_help(msg)
        assert msg.answer.await_args.args[0] == i18n.t("help", "en", username=BOT_USERNAME)

    async def test_download_error_localized_to_russian(
        self, settings, mock_repo, mock_download
    ):
        mock_download.side_effect = UnsupportedUrlError()
        msg = make_message("https://youtu.be/abc", language_code="ru")

        await handlers.handle_private(msg, settings)

        sent = [c.args[0] for c in msg.answer.await_args_list]
        assert i18n.t("error.unsupported_url", "ru") in sent
        assert i18n.t("error.unsupported_url", "en") not in sent

    async def test_busy_message_localized_to_russian(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        runtime.reset()
        runtime.configure(5, BOT_USERNAME, max_per_user=1)

        release = asyncio.Event()
        started = asyncio.Event()

        async def _download(url, workdir, **kwargs):
            started.set()
            await release.wait()
            return [make_media(Path(workdir))]

        mock_download.side_effect = _download

        first = asyncio.create_task(
            handlers.handle_private(
                make_message("https://youtu.be/abc", user_id=42, language_code="ru"), settings
            )
        )
        await started.wait()
        second = make_message("https://youtu.be/def", user_id=42, language_code="ru")
        await handlers.handle_private(second, settings)

        second.answer.assert_awaited_once_with(i18n.t("busy_per_user", "ru"))

        release.set()
        await first


# ------------------------------------------------------------------ routing rules


class TestRouting:
    async def test_private_url_is_processed(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        mock_download.assert_awaited_once()
        assert mock_download.await_args.args[0] == "https://youtu.be/abc"
        msg.answer_video.assert_awaited_once()

    async def test_private_non_url_ignored(self, settings, mock_repo, mock_download):
        msg = make_message("hello there, how are you?")

        await handlers.handle_private(msg, settings)

        mock_download.assert_not_awaited()
        msg.answer.assert_not_awaited()
        mock_repo.create_request.assert_not_awaited()

    async def test_private_caption_url_is_processed(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message(None, caption="see https://vm.tiktok.com/xyz")

        await handlers.handle_private(msg, settings)

        mock_download.assert_awaited_once()
        assert mock_download.await_args.args[0] == "https://vm.tiktok.com/xyz"

    async def test_group_without_mention_ignored(
        self, settings, mock_repo, mock_download
    ):
        msg = make_message("https://youtu.be/abc", chat_type="group")

        await handlers.handle_group(msg, settings)

        mock_download.assert_not_awaited()
        msg.reply.assert_not_awaited()
        mock_repo.create_request.assert_not_awaited()

    async def test_group_with_mention_processed_and_replied(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message(
            f"@{BOT_USERNAME} https://youtu.be/abc", chat_type="supergroup"
        )

        await handlers.handle_group(msg, settings)

        mock_download.assert_awaited_once()
        # Group results reply-to the trigger, never a plain send.
        msg.reply_video.assert_awaited_once()
        msg.answer_video.assert_not_awaited()
        msg.reply.assert_not_awaited()  # success sends media only, no text

    async def test_group_mention_without_url_ignored(
        self, settings, mock_repo, mock_download
    ):
        msg = make_message(f"@{BOT_USERNAME} hi bot", chat_type="group")

        await handlers.handle_group(msg, settings)

        mock_download.assert_not_awaited()

    async def test_channel_post_with_mention_processed(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message(
            f"@{BOT_USERNAME} https://youtu.be/abc", chat_type="channel", from_user=False
        )

        await handlers.handle_channel_post(msg, settings)

        mock_download.assert_awaited_once()
        msg.reply_video.assert_awaited_once()
        # The request is recorded anonymously: only chat_type, no identifiers.
        req = mock_repo.create_request.await_args.kwargs
        assert req["chat_type"] == "channel"
        assert "user_id" not in req and "chat_id" not in req and "message_id" not in req

    async def test_channel_post_without_mention_ignored(
        self, settings, mock_repo, mock_download
    ):
        msg = make_message(
            "https://youtu.be/abc", chat_type="channel", from_user=False
        )

        await handlers.handle_channel_post(msg, settings)

        mock_download.assert_not_awaited()


# -------------------------------------------------------------------- happy paths


class TestSuccessFlow:
    async def test_video_sent_with_metadata_and_audited(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        media = make_media(tmp_path, width=1280, height=720, duration_s=12.7)
        mock_download.return_value = [media]
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        kwargs = msg.answer_video.await_args.kwargs
        assert kwargs["width"] == 1280
        assert kwargs["height"] == 720
        assert kwargs["duration"] == 12  # int-coerced
        assert kwargs["supports_streaming"] is True
        # Plain media: no caption anywhere.
        assert "caption" not in kwargs

        mock_repo.mark_success.assert_awaited_once()
        success_kwargs = mock_repo.mark_success.await_args.kwargs
        assert success_kwargs["request_id"] == 99
        assert success_kwargs["telegram_file_id"] == "vid-file-id"
        assert success_kwargs["media"] is media
        assert success_kwargs["elapsed_s"] >= 0

    async def test_download_called_with_settings(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        kwargs = mock_download.await_args.kwargs
        assert kwargs["max_size_bytes"] == settings.max_file_size_bytes
        assert kwargs["max_height"] == 720
        assert kwargs["timeout_s"] == 300
        workdir = mock_download.await_args.args[1]
        assert Path(settings.download_dir) in Path(workdir).parents

    async def test_image_sent_as_photo(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path, kind="image", name="a.jpg")]
        msg = make_message("https://example.com/pic")

        await handlers.handle_private(msg, settings)

        msg.answer_photo.assert_awaited_once()
        assert mock_repo.mark_success.await_args.kwargs["telegram_file_id"] == (
            "photo-file-id"
        )

    async def test_animation_sent_as_animation(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [
            make_media(tmp_path, kind="animation", name="a.gif")
        ]
        msg = make_message("https://example.com/gif")

        await handlers.handle_private(msg, settings)

        msg.answer_animation.assert_awaited_once()
        assert mock_repo.mark_success.await_args.kwargs["telegram_file_id"] == (
            "anim-file-id"
        )

    async def test_multiple_images_sent_as_media_group(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [
            make_media(tmp_path, kind="image", name=f"i{i}.jpg") for i in range(3)
        ]
        msg = make_message("https://instagram.com/p/carousel")

        await handlers.handle_private(msg, settings)

        msg.answer_media_group.assert_awaited_once()
        group = msg.answer_media_group.await_args.args[0]
        assert len(group) == 3
        msg.answer_photo.assert_not_awaited()

    async def test_media_group_capped_at_ten(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [
            make_media(tmp_path, kind="image", name=f"i{i}.jpg") for i in range(14)
        ]
        msg = make_message("https://instagram.com/p/big-carousel")

        await handlers.handle_private(msg, settings)

        group = msg.answer_media_group.await_args.args[0]
        assert len(group) == handlers.MEDIA_GROUP_LIMIT == 10

    async def test_no_status_text_on_success(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        """Status is conveyed via chat action only — success sends media, zero text."""
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        msg.answer.assert_not_awaited()
        msg.reply.assert_not_awaited()
        msg.bot.send_chat_action.assert_awaited()

    async def test_initial_chat_action_is_neutral_typing(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        # Until the download succeeds we don't know whether the link is even
        # downloadable, or whether it's a video or photos — so the immediate
        # feedback must be a neutral "typing…", never "sending a video…".
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        msg.bot.send_chat_action.assert_awaited_once()
        assert msg.bot.send_chat_action.await_args.args[1] == "typing"

    async def test_failed_download_never_claims_media_action(
        self, settings, mock_repo, mock_download
    ):
        mock_download.side_effect = ExtractionError("boom")
        msg = make_message("https://instagram.com/p/x")

        await handlers.handle_private(msg, settings)

        actions = [c.args[1] for c in msg.bot.send_chat_action.await_args_list]
        assert all(a == "typing" for a in actions)

    async def test_request_recorded_anonymously(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc", chat_id=111, message_id=42)

        await handlers.handle_private(msg, settings)

        # Only anonymous fields are passed to the audit layer.
        req = mock_repo.create_request.await_args.kwargs
        assert req["chat_type"] == "private"
        assert req["url"] == "https://youtu.be/abc"
        assert req["platform"] == "youtube"
        # No identifying data is forwarded.
        for identifying in ("user_id", "telegram_id", "chat_id", "message_id", "username"):
            assert identifying not in req


# ------------------------------------------------------------------ failure paths


class TestFailureFlow:
    async def test_download_error_shows_user_message_and_marks_failure(
        self, settings, mock_repo, mock_download
    ):
        err = MediaTooLargeError("1.2GB")
        mock_download.side_effect = err
        msg = make_message("https://youtu.be/huge")

        await handlers.handle_private(msg, settings)

        # The user sees the friendly message, not the internal detail.
        sent_texts = [c.args[0] for c in msg.answer.await_args_list]
        assert i18n.t("error.too_large", "en") in sent_texts
        assert "1.2GB" not in " ".join(sent_texts)

        mock_repo.mark_failure.assert_awaited_once()
        assert mock_repo.mark_failure.await_args.kwargs["error"] is err
        mock_repo.mark_success.assert_not_awaited()

    async def test_unsupported_url_error_message(
        self, settings, mock_repo, mock_download
    ):
        mock_download.side_effect = UnsupportedUrlError()
        msg = make_message("https://example.com/nope")

        await handlers.handle_private(msg, settings)

        sent_texts = [c.args[0] for c in msg.answer.await_args_list]
        assert i18n.t("error.unsupported_url", "en") in sent_texts

    async def test_group_error_is_replied(self, settings, mock_repo, mock_download):
        mock_download.side_effect = UnsupportedUrlError()
        msg = make_message(f"@{BOT_USERNAME} https://x.com/1", chat_type="group")

        await handlers.handle_group(msg, settings)

        sent_texts = [c.args[0] for c in msg.reply.await_args_list]
        assert i18n.t("error.unsupported_url", "en") in sent_texts

    async def test_unexpected_exception_shows_generic_message(
        self, settings, mock_repo, mock_download
    ):
        boom = RuntimeError("ffmpeg exploded")
        mock_download.side_effect = boom
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        sent_texts = [c.args[0] for c in msg.answer.await_args_list]
        assert i18n.t("generic_error", "en") in sent_texts
        assert "ffmpeg exploded" not in " ".join(sent_texts)
        assert mock_repo.mark_failure.await_args.kwargs["error"] is boom

    async def test_empty_results_treated_as_failure(
        self, settings, mock_repo, mock_download
    ):
        mock_download.return_value = []
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        mock_repo.mark_failure.assert_awaited_once()
        mock_repo.mark_success.assert_not_awaited()

    async def test_failure_sends_exactly_one_text(
        self, settings, mock_repo, mock_download
    ):
        """The only text a user ever sees is the error message itself."""
        mock_download.side_effect = UnsupportedUrlError()
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        msg.answer.assert_awaited_once_with(i18n.t("error.unsupported_url", "en"))

    async def test_send_failure_is_caught(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")
        msg.answer_video = AsyncMock(side_effect=RuntimeError("telegram 500"))

        await handlers.handle_private(msg, settings)  # must not raise

        sent_texts = [c.args[0] for c in msg.answer.await_args_list]
        assert i18n.t("generic_error", "en") in sent_texts
        mock_repo.mark_failure.assert_awaited_once()


# -------------------------------------------------- audit resilience & cleanup


class TestAuditResilience:
    async def test_repo_failures_do_not_break_flow(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        mock_repo.create_request.side_effect = RuntimeError("db down")
        mock_repo.mark_success.side_effect = RuntimeError("db down")
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)  # must not raise

        # The user still gets their video despite the audit layer being broken.
        msg.answer_video.assert_awaited_once()

    async def test_mark_failure_error_does_not_propagate(
        self, settings, mock_repo, mock_download
    ):
        mock_download.side_effect = UnsupportedUrlError()
        mock_repo.mark_failure.side_effect = RuntimeError("db down")
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)  # must not raise


class TestWorkdirCleanup:
    async def test_workdir_removed_on_success(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        seen: dict[str, Path] = {}

        async def _download(url, workdir, **kwargs):
            seen["workdir"] = Path(workdir)
            assert Path(workdir).is_dir()
            return [make_media(Path(workdir))]

        mock_download.side_effect = _download
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        assert not seen["workdir"].exists()

    async def test_workdir_removed_on_failure(
        self, settings, mock_repo, mock_download
    ):
        seen: dict[str, Path] = {}

        async def _download(url, workdir, **kwargs):
            seen["workdir"] = Path(workdir)
            (Path(workdir) / "partial.part").write_bytes(b"junk")
            raise UnsupportedUrlError()

        mock_download.side_effect = _download
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        assert not seen["workdir"].exists()

    async def test_each_request_gets_its_own_workdir(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        seen: list[Path] = []

        async def _download(url, workdir, **kwargs):
            seen.append(Path(workdir))
            return [make_media(Path(workdir))]

        mock_download.side_effect = _download

        for _ in range(2):
            await handlers.handle_private(make_message("https://youtu.be/abc"), settings)

        assert len(set(seen)) == 2


class TestConcurrency:
    async def test_semaphore_limits_parallel_downloads(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        runtime.reset()
        # High per-user cap so the GLOBAL semaphore (2) is the binding constraint;
        # distinct users so the per-user guard doesn't gate them first.
        runtime.configure(2, BOT_USERNAME, max_per_user=10)

        active = 0
        peak = 0
        release = asyncio.Event()

        async def _download(url, workdir, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1
            return [make_media(Path(workdir))]

        mock_download.side_effect = _download

        # Distinct URLs, so the coalescing gate lets all five through and the global
        # semaphore is what actually limits them.
        tasks = [
            asyncio.create_task(
                handlers.handle_private(
                    make_message(f"https://youtu.be/clip{i}", user_id=1000 + i), settings
                )
            )
            for i in range(5)
        ]
        await asyncio.sleep(0.05)
        observed_peak = peak
        release.set()
        await asyncio.gather(*tasks)

        assert observed_peak == 2, f"expected max 2 concurrent downloads, saw {observed_peak}"

    async def test_per_user_limit_rejects_second_concurrent(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        runtime.reset()
        runtime.configure(5, BOT_USERNAME, max_per_user=1)

        release = asyncio.Event()
        started = asyncio.Event()

        async def _download(url, workdir, **kwargs):
            started.set()
            await release.wait()
            return [make_media(Path(workdir))]

        mock_download.side_effect = _download

        # Same user fires twice; the first holds the only slot.
        first = asyncio.create_task(
            handlers.handle_private(make_message("https://youtu.be/abc", user_id=42), settings)
        )
        await started.wait()
        second_msg = make_message("https://youtu.be/def", user_id=42)
        await handlers.handle_private(second_msg, settings)

        # The second was rejected immediately with the busy message; no download ran.
        second_msg.answer.assert_awaited_once_with(i18n.t("busy_per_user", "en"))
        assert mock_download.await_count == 1

        release.set()
        await first

    async def test_per_user_limit_allows_different_users(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        runtime.reset()
        runtime.configure(5, BOT_USERNAME, max_per_user=1)
        mock_download.return_value = [make_media(tmp_path)]

        await handlers.handle_private(make_message("https://youtu.be/a", user_id=1), settings)
        await handlers.handle_private(make_message("https://youtu.be/b", user_id=2), settings)

        assert mock_download.await_count == 2


class TestFileIdCache:
    """A link we've already uploaded is re-sent by file_id — no download at all."""

    async def test_cache_hit_sends_by_file_id_and_skips_download(
        self, settings, mock_repo, mock_download
    ):
        mock_repo.find_cached.return_value = cached_row()
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        mock_download.assert_not_awaited()
        msg.answer_video.assert_awaited_once()
        # The file_id string is passed straight through, not an FSInputFile.
        assert msg.answer_video.await_args.args[0] == "cached-file-id"
        kwargs = msg.answer_video.await_args.kwargs
        assert kwargs["width"] == 1280 and kwargs["height"] == 720
        assert kwargs["duration"] == 12  # int-coerced
        assert kwargs["supports_streaming"] is True

    async def test_cache_hit_is_audited_as_success(
        self, settings, mock_repo, mock_download
    ):
        mock_repo.find_cached.return_value = cached_row()

        await handlers.handle_private(make_message("https://youtu.be/abc"), settings)

        success_kwargs = mock_repo.mark_success.await_args.kwargs
        assert success_kwargs["request_id"] == 99
        assert success_kwargs["telegram_file_id"] == "vid-file-id"
        media = success_kwargs["media"]
        assert media.kind == "video"
        assert media.filesize == 4242
        assert media.width == 1280 and media.duration_s == 12.7

    async def test_cached_animation_uses_animation_sender(
        self, settings, mock_repo, mock_download
    ):
        mock_repo.find_cached.return_value = cached_row(media_kind="animation")

        await handlers.handle_private(make_message("https://example.com/gif"), settings)

        msg_call = mock_repo.mark_success.await_args.kwargs["media"]
        assert msg_call.kind == "animation"
        mock_download.assert_not_awaited()

    async def test_cache_hit_replies_in_groups(
        self, settings, mock_repo, mock_download
    ):
        mock_repo.find_cached.return_value = cached_row()
        msg = make_message(f"@{BOT_USERNAME} https://youtu.be/abc", chat_type="group")

        await handlers.handle_group(msg, settings)

        msg.reply_video.assert_awaited_once()
        msg.answer_video.assert_not_awaited()

    async def test_cache_hit_does_not_hold_the_download_semaphore(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        """A cached re-send must not queue behind (or block) real downloads."""
        runtime.reset()
        runtime.configure(1, BOT_USERNAME, max_per_user=10)
        # Only the second link is cached; the first must really download.
        mock_repo.find_cached.side_effect = (
            lambda url, **kwargs: cached_row() if url.endswith("abc") else None
        )

        release = asyncio.Event()
        started = asyncio.Event()

        async def _download(url, workdir, **kwargs):
            started.set()
            await release.wait()
            return [make_media(Path(workdir))]

        mock_download.side_effect = _download

        # A real download holds the only global slot for the whole test.
        blocker = asyncio.create_task(
            handlers.handle_private(
                make_message("https://youtu.be/slow", user_id=1), settings
            )
        )
        await started.wait()

        cached_msg = make_message("https://youtu.be/abc", user_id=2)
        await asyncio.wait_for(
            handlers.handle_private(cached_msg, settings), timeout=2
        )
        cached_msg.answer_video.assert_awaited_once()

        release.set()
        await blocker

    async def test_stale_file_id_falls_through_to_download(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        """Telegram rejecting a forgotten file_id is a cache miss, not a failure."""
        mock_repo.find_cached.return_value = cached_row()
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")
        msg.answer_video = AsyncMock(
            side_effect=[RuntimeError("wrong file identifier"), sent_video("vid-file-id")]
        )

        await handlers.handle_private(msg, settings)

        mock_download.assert_awaited_once()
        assert msg.answer_video.await_count == 2
        mock_repo.mark_success.assert_awaited_once()
        mock_repo.mark_failure.assert_not_awaited()

    async def test_repo_error_falls_through_to_download(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_repo.find_cached.side_effect = RuntimeError("db down")
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)  # must not raise

        mock_download.assert_awaited_once()
        msg.answer_video.assert_awaited_once()

    async def test_image_row_without_file_id_list_is_not_served(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        """A pre-migration image row holds only the first item — never replay it.

        The repo query already excludes these; this guards the handler side too.
        """
        mock_repo.find_cached.return_value = cached_row(media_kind="image")
        mock_download.return_value = [
            make_media(tmp_path, kind="image", name=f"i{i}.jpg") for i in range(3)
        ]
        msg = make_message("https://instagram.com/p/carousel")

        await handlers.handle_private(msg, settings)

        msg.answer_photo.assert_not_awaited()
        mock_download.assert_awaited_once()
        msg.answer_media_group.assert_awaited_once()

    async def test_cached_single_image_is_replayed_as_a_photo(
        self, settings, mock_repo, mock_download
    ):
        mock_repo.find_cached.return_value = cached_row(
            media_kind="image", file_ids=["photo-1"]
        )
        msg = make_message("https://instagram.com/p/single")

        await handlers.handle_private(msg, settings)

        mock_download.assert_not_awaited()
        msg.answer_photo.assert_awaited_once_with("photo-1")
        msg.answer_media_group.assert_not_awaited()

    async def test_cached_carousel_is_replayed_as_a_media_group(
        self, settings, mock_repo, mock_download
    ):
        """Every item comes back, as file_id strings — no download, no re-upload."""
        mock_repo.find_cached.return_value = cached_row(
            media_kind="image", file_ids=["p1", "p2", "p3"]
        )
        msg = make_message("https://instagram.com/p/carousel")

        await handlers.handle_private(msg, settings)

        mock_download.assert_not_awaited()
        group = msg.answer_media_group.await_args.args[0]
        assert [item.media for item in group] == ["p1", "p2", "p3"]
        msg.answer_photo.assert_not_awaited()

    async def test_cached_carousel_is_capped_at_the_media_group_limit(
        self, settings, mock_repo, mock_download
    ):
        mock_repo.find_cached.return_value = cached_row(
            media_kind="image", file_ids=[f"p{i}" for i in range(14)]
        )
        msg = make_message("https://instagram.com/p/big")

        await handlers.handle_private(msg, settings)

        group = msg.answer_media_group.await_args.args[0]
        assert len(group) == handlers.MEDIA_GROUP_LIMIT == 10

    async def test_cache_hit_is_audited_with_cache_hit_flag(
        self, settings, mock_repo, mock_download
    ):
        mock_repo.find_cached.return_value = cached_row(
            media_kind="image", file_ids=["p1", "p2"]
        )
        msg = make_message("https://instagram.com/p/carousel")
        msg.answer_media_group = AsyncMock(
            return_value=[sent_photo("p1"), sent_photo("p2")]
        )

        await handlers.handle_private(msg, settings)

        kwargs = mock_repo.mark_success.await_args.kwargs
        assert kwargs["cache_hit"] is True
        assert kwargs["telegram_file_ids"] == ["p1", "p2"]
        assert kwargs["telegram_file_id"] == "p1"

    async def test_fresh_download_is_not_flagged_as_a_cache_hit(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]

        await handlers.handle_private(make_message("https://youtu.be/abc"), settings)

        kwargs = mock_repo.mark_success.await_args.kwargs
        assert kwargs["cache_hit"] is False
        assert kwargs["telegram_file_ids"] == ["vid-file-id"]

    async def test_instagram_stories_bypass_the_cache(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        """Stories expire within 24h — a cached file_id would be a stale surprise."""
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://instagram.com/stories/someone/123/")

        await handlers.handle_private(msg, settings)

        mock_repo.find_cached.assert_not_awaited()
        mock_download.assert_awaited_once()

    async def test_cache_lookup_uses_the_normalized_url(
        self, settings, tmp_path, monkeypatch, mock_repo, mock_download
    ):
        from tgdl.downloader import urls as urls_mod

        monkeypatch.setattr(
            urls_mod, "normalize_url", lambda url: "https://youtube.com/watch?v=abc"
        )
        mock_download.return_value = [make_media(tmp_path)]

        await handlers.handle_private(
            make_message("https://youtu.be/abc?si=track"), settings
        )

        assert mock_repo.find_cached.await_args.args[0] == (
            "https://youtube.com/watch?v=abc"
        )


class TestFileIdExtraction:
    def test_video_file_id(self):
        assert handlers._extract_file_id(sent_video("v1")) == "v1"

    def test_photo_uses_largest_size(self):
        msg = SimpleNamespace(
            video=None,
            animation=None,
            photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")],
        )
        assert handlers._extract_file_id(msg) == "large"

    def test_animation_file_id(self):
        assert handlers._extract_file_id(sent_animation("a1")) == "a1"

    def test_media_group_list_uses_first(self):
        assert handlers._extract_file_id([sent_photo("p1"), sent_photo("p2")]) == "p1"

    def test_none_and_empty(self):
        assert handlers._extract_file_id(None) is None
        assert handlers._extract_file_id([]) is None

    def test_all_file_ids_of_a_media_group(self):
        """The cache needs every id, not just the first — that's the whole carousel."""
        group = [sent_photo("p1"), sent_photo("p2"), sent_photo("p3")]
        assert handlers._extract_file_ids(group) == ["p1", "p2", "p3"]

    def test_all_file_ids_of_a_single_send(self):
        assert handlers._extract_file_ids(sent_video("v1")) == ["v1"]

    def test_all_file_ids_none_and_empty(self):
        assert handlers._extract_file_ids(None) == []
        assert handlers._extract_file_ids([]) == []


class TestCoalescing:
    """Two people posting the same link at once download it once, not twice."""

    async def test_follower_waits_and_gets_the_leaders_upload_from_cache(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        runtime.reset()
        runtime.configure(5, BOT_USERNAME, max_per_user=10)

        started = asyncio.Event()
        release = asyncio.Event()
        # The cache is empty until the leader's upload lands.
        uploaded: list[str] = []
        mock_repo.find_cached.side_effect = (
            lambda url, **kw: cached_row() if uploaded else None
        )

        async def _download(url, workdir, **kwargs):
            started.set()
            await release.wait()
            uploaded.append(url)
            return [make_media(Path(workdir))]

        mock_download.side_effect = _download

        leader = asyncio.create_task(
            handlers.handle_private(
                make_message("https://youtu.be/viral", user_id=1), settings
            )
        )
        await started.wait()

        follower_msg = make_message("https://youtu.be/viral", user_id=2)
        follower = asyncio.create_task(handlers.handle_private(follower_msg, settings))
        await asyncio.sleep(0.05)
        # Still parked on the gate: only the leader's own lookup has happened, and
        # the follower has not started a second download.
        assert mock_repo.find_cached.await_count == 1
        assert mock_download.await_count == 1

        release.set()
        await asyncio.gather(leader, follower)

        # Exactly one download, and the follower was served the leader's file_id.
        assert mock_download.await_count == 1
        assert follower_msg.answer_video.await_args.args[0] == "cached-file-id"

    async def test_follower_downloads_itself_when_the_leader_fails(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        runtime.reset()
        runtime.configure(5, BOT_USERNAME, max_per_user=10)

        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def _download(url, workdir, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                started.set()
                await release.wait()
                raise ExtractionError("leader boom")
            return [make_media(Path(workdir))]

        mock_download.side_effect = _download

        leader = asyncio.create_task(
            handlers.handle_private(
                make_message("https://youtu.be/viral", user_id=1), settings
            )
        )
        await started.wait()
        follower_msg = make_message("https://youtu.be/viral", user_id=2)
        follower = asyncio.create_task(handlers.handle_private(follower_msg, settings))
        await asyncio.sleep(0.05)

        release.set()
        await asyncio.gather(leader, follower)

        # The follower found nothing cached and simply downloaded for itself.
        assert mock_download.await_count == 2
        follower_msg.answer_video.assert_awaited_once()

    async def test_gate_entry_is_released_when_the_leader_raises(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        """A crashing leader must not leave the URL permanently gated."""
        mock_download.side_effect = RuntimeError("kaboom")

        await handlers.handle_private(make_message("https://youtu.be/abc"), settings)

        assert runtime._leaders == {}

        # And the very next request for the same link runs normally.
        mock_download.side_effect = None
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")
        await asyncio.wait_for(handlers.handle_private(msg, settings), timeout=2)
        msg.answer_video.assert_awaited_once()

    async def test_wedged_leader_releases_followers_on_timeout(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        """A leader that never finishes must not strand followers forever."""
        runtime.reset()
        runtime.configure(5, BOT_USERNAME, max_per_user=10, follower_timeout_s=0.05)

        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def _download(url, workdir, **kwargs):
            calls.append(url)
            if len(calls) == 1:  # only the leader wedges
                started.set()
                await release.wait()
            return [make_media(Path(workdir))]

        mock_download.side_effect = _download

        leader = asyncio.create_task(
            handlers.handle_private(
                make_message("https://youtu.be/wedged", user_id=1), settings
            )
        )
        await started.wait()

        follower_msg = make_message("https://youtu.be/wedged", user_id=2)
        await asyncio.wait_for(
            handlers.handle_private(follower_msg, settings), timeout=2
        )

        # It gave up on the leader and downloaded for itself.
        follower_msg.answer_video.assert_awaited_once()
        assert mock_download.await_count == 2

        release.set()
        await leader

    async def test_different_urls_are_not_gated_against_each_other(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        runtime.reset()
        runtime.configure(5, BOT_USERNAME, max_per_user=10)

        started = asyncio.Event()
        release = asyncio.Event()

        async def _download(url, workdir, **kwargs):
            if url.endswith("slow"):
                started.set()
                await release.wait()
            return [make_media(Path(workdir))]

        mock_download.side_effect = _download

        slow = asyncio.create_task(
            handlers.handle_private(make_message("https://youtu.be/slow", user_id=1), settings)
        )
        await started.wait()

        other = make_message("https://youtu.be/other", user_id=2)
        await asyncio.wait_for(handlers.handle_private(other, settings), timeout=2)
        other.answer_video.assert_awaited_once()

        release.set()
        await slow


class TestReactionAck:
    """A 👀 on the user's own message while we work, cleared when we're done."""

    async def test_reaction_set_then_cleared(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        assert msg.react.await_count == 2
        first, last = msg.react.await_args_list
        assert first.args[0][0].emoji == handlers.ACK_EMOJI
        assert last.args[0] == []  # cleared

    async def test_reaction_cleared_after_a_failure_too(
        self, settings, mock_repo, mock_download
    ):
        mock_download.side_effect = UnsupportedUrlError()
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        assert msg.react.await_args_list[-1].args[0] == []

    async def test_reaction_failures_never_break_the_flow(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        """Old messages, missing permissions, reactions disabled — all just ignored."""
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")
        msg.react = AsyncMock(side_effect=RuntimeError("REACTION_INVALID"))

        await handlers.handle_private(msg, settings)  # must not raise

        msg.answer_video.assert_awaited_once()
        mock_repo.mark_success.assert_awaited_once()

    async def test_channel_posts_are_not_reacted_to(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        """No from_user, and reaction permissions in channels are unreliable."""
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message(
            f"@{BOT_USERNAME} https://youtu.be/abc", chat_type="channel", from_user=False
        )

        await handlers.handle_channel_post(msg, settings)

        msg.react.assert_not_awaited()
        msg.reply_video.assert_awaited_once()

    async def test_group_messages_are_reacted_to(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message(f"@{BOT_USERNAME} https://youtu.be/abc", chat_type="group")

        await handlers.handle_group(msg, settings)

        assert msg.react.await_count == 2


class TestDownloadErrorBase:
    async def test_base_download_error_default_message(
        self, settings, mock_repo, mock_download
    ):
        mock_download.side_effect = DownloadError()
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        sent_texts = [c.args[0] for c in msg.answer.await_args_list]
        assert i18n.t("error.generic", "en") in sent_texts

    async def test_custom_user_message_is_used(
        self, settings, mock_repo, mock_download
    ):
        mock_download.side_effect = DownloadError("internals", user_message="Nope, sorry.")
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        sent_texts = [c.args[0] for c in msg.answer.await_args_list]
        assert "Nope, sorry." in sent_texts


# ------------------------------------------------------------------------- /stats


class TestStatsCommand:
    """Admin-only, private-only, and silent in every other case."""

    ADMIN_ID = 4242

    @pytest.fixture
    def admin_settings(self, settings) -> Settings:
        return settings.model_copy(update={"admin_user_id": self.ADMIN_ID})

    @pytest.fixture
    def mock_stats(self, monkeypatch):
        mock = AsyncMock(
            return_value={
                "requests": 5, "success": 4, "failed": 1, "pending": 0,
                "cache_hits": 2, "hit_rate": 0.5,
                "platforms": {"youtube": {"count": 4, "p50_s": 2.0, "p95_s": 9.0}},
            }
        )
        monkeypatch.setattr(handlers.repo, "stats", mock)
        return mock

    async def test_admin_in_private_gets_the_summary(self, admin_settings, mock_stats):
        msg = make_message("/stats", user_id=self.ADMIN_ID)

        await handlers.cmd_stats(msg, admin_settings)

        mock_stats.assert_awaited_once()
        text = msg.answer.await_args.args[0]
        assert "requests   5" in text
        assert "youtube" in text

    async def test_other_user_gets_nothing_at_all(self, admin_settings, mock_stats):
        """Not an error either: the command must not advertise its own existence."""
        msg = make_message("/stats", user_id=self.ADMIN_ID + 1)

        await handlers.cmd_stats(msg, admin_settings)

        mock_stats.assert_not_awaited()
        msg.answer.assert_not_awaited()
        msg.reply.assert_not_awaited()

    async def test_admin_in_a_group_gets_nothing(self, admin_settings, mock_stats):
        """Even the admin: an ops readout does not belong in a shared chat."""
        msg = make_message("/stats", chat_type="group", user_id=self.ADMIN_ID)

        await handlers.cmd_stats(msg, admin_settings)

        mock_stats.assert_not_awaited()
        msg.answer.assert_not_awaited()
        msg.reply.assert_not_awaited()

    async def test_disabled_when_admin_id_is_unset(self, settings, mock_stats):
        assert settings.admin_user_id == 0
        msg = make_message("/stats", user_id=0)

        await handlers.cmd_stats(msg, settings)

        mock_stats.assert_not_awaited()
        msg.answer.assert_not_awaited()

    async def test_message_without_a_user_is_ignored(self, admin_settings, mock_stats):
        msg = make_message("/stats", from_user=False)

        await handlers.cmd_stats(msg, admin_settings)

        mock_stats.assert_not_awaited()

    async def test_repo_failure_does_not_raise(self, admin_settings, monkeypatch):
        monkeypatch.setattr(
            handlers.repo, "stats", AsyncMock(side_effect=RuntimeError("db down"))
        )
        msg = make_message("/stats", user_id=self.ADMIN_ID)

        await handlers.cmd_stats(msg, admin_settings)  # must not raise

        assert msg.answer.await_args.args[0] == i18n.t("generic_error", "en")


# --------------------------------------------------------------------------- /mp3


class TestMp3Command:
    async def test_usage_hint_when_no_url(self, settings, mock_repo, mock_audio):
        msg = make_message("/mp3")

        await handlers.cmd_mp3(msg, settings)

        mock_audio.assert_not_awaited()
        assert msg.answer.await_args.args[0] == i18n.t("usage.mp3", "en")

    async def test_usage_hint_is_localized(self, settings, mock_repo, mock_audio):
        msg = make_message("/mp3", language_code="ru")

        await handlers.cmd_mp3(msg, settings)

        assert msg.answer.await_args.args[0] == i18n.t("usage.mp3", "ru")

    async def test_audio_sent_with_metadata_and_audited(
        self, settings, tmp_path, mock_repo, mock_audio
    ):
        mock_audio.return_value = make_audio(tmp_path)
        msg = make_message("/mp3 https://youtu.be/abc")

        await handlers.cmd_mp3(msg, settings)

        mock_audio.assert_awaited_once()
        kwargs = msg.answer_audio.await_args.kwargs
        assert kwargs["title"] == "A Song"
        assert kwargs["performer"] == "A Band"
        assert kwargs["duration"] == 123  # int-coerced

        success = mock_repo.mark_success.await_args.kwargs
        assert success["telegram_file_id"] == "audio-file-id"
        assert success["media_kind_override"] == "audio"

    async def test_audio_alias_works(self, settings, tmp_path, mock_repo, mock_audio):
        mock_audio.return_value = make_audio(tmp_path)
        msg = make_message("/audio https://youtu.be/abc")

        await handlers.cmd_mp3(msg, settings)

        msg.answer_audio.assert_awaited_once()

    async def test_download_is_given_the_settings_cap_and_timeout(
        self, settings, tmp_path, mock_repo, mock_audio
    ):
        mock_audio.return_value = make_audio(tmp_path)

        await handlers.cmd_mp3(make_message("/mp3 https://youtu.be/abc"), settings)

        kwargs = mock_audio.await_args.kwargs
        assert kwargs["max_size_bytes"] == settings.max_file_size_bytes
        assert kwargs["timeout_s"] == settings.download_timeout_s

    async def test_works_in_groups_without_a_mention(
        self, settings, tmp_path, mock_repo, mock_audio
    ):
        """An explicit command is already explicit — no @botname required."""
        mock_audio.return_value = make_audio(tmp_path)
        msg = make_message("/mp3 https://youtu.be/abc", chat_type="group")

        await handlers.cmd_mp3(msg, settings)

        msg.reply_audio.assert_awaited_once()
        msg.answer_audio.assert_not_awaited()

    async def test_error_is_localized_and_audited(
        self, settings, mock_repo, mock_audio
    ):
        mock_audio.side_effect = MediaTooLargeError()
        msg = make_message("/mp3 https://youtu.be/abc", language_code="ru")

        await handlers.cmd_mp3(msg, settings)

        assert msg.answer.await_args.args[0] == i18n.t("error.too_large", "ru")
        mock_repo.mark_failure.assert_awaited_once()

    async def test_unexpected_error_does_not_escape(
        self, settings, mock_repo, mock_audio
    ):
        mock_audio.side_effect = RuntimeError("boom")
        msg = make_message("/mp3 https://youtu.be/abc")

        await handlers.cmd_mp3(msg, settings)  # must not raise

        assert msg.answer.await_args.args[0] == i18n.t("generic_error", "en")
        mock_repo.mark_failure.assert_awaited_once()

    async def test_workdir_removed_after_the_download(
        self, settings, tmp_path, mock_repo, mock_audio
    ):
        seen: list[Path] = []

        async def _download(url, workdir, **kwargs):
            seen.append(Path(workdir))
            return make_audio(Path(workdir))

        mock_audio.side_effect = _download

        await handlers.cmd_mp3(make_message("/mp3 https://youtu.be/abc"), settings)

        assert seen and not seen[0].exists()

    async def test_reaction_ack_is_set_and_cleared(
        self, settings, tmp_path, mock_repo, mock_audio
    ):
        mock_audio.return_value = make_audio(tmp_path)
        msg = make_message("/mp3 https://youtu.be/abc")

        await handlers.cmd_mp3(msg, settings)

        assert msg.react.await_count == 2

    async def test_per_user_limit_applies(self, settings, tmp_path, mock_repo, mock_audio):
        runtime.reset()
        runtime.configure(5, BOT_USERNAME, max_per_user=1)
        release = asyncio.Event()

        async def _download(url, workdir, **kwargs):
            await release.wait()
            return make_audio(Path(workdir))

        mock_audio.side_effect = _download

        first = asyncio.create_task(
            handlers.cmd_mp3(make_message("/mp3 https://youtu.be/a"), settings)
        )
        await asyncio.sleep(0.05)
        second = make_message("/mp3 https://youtu.be/b")
        await handlers.cmd_mp3(second, settings)

        assert second.answer.await_args.args[0] == i18n.t("busy_per_user", "en")
        release.set()
        await first


class TestAudioCache:
    """/mp3 has its own cache lane: audio rows only, and never a video row."""

    async def test_cache_hit_replays_as_audio(self, settings, mock_repo, mock_audio):
        mock_repo.find_cached.return_value = audio_row()
        msg = make_message("/mp3 https://youtu.be/abc")

        await handlers.cmd_mp3(msg, settings)

        mock_audio.assert_not_awaited()
        assert msg.answer_audio.await_args.args[0] == "cached-audio-id"
        assert msg.answer_audio.await_args.kwargs["duration"] == 123

    async def test_cache_hit_is_audited_as_an_audio_hit(
        self, settings, mock_repo, mock_audio
    ):
        mock_repo.find_cached.return_value = audio_row()

        await handlers.cmd_mp3(make_message("/mp3 https://youtu.be/abc"), settings)

        success = mock_repo.mark_success.await_args.kwargs
        assert success["cache_hit"] is True
        assert success["media_kind_override"] == "audio"

    async def test_mp3_asks_the_repo_only_for_audio_rows(
        self, settings, tmp_path, mock_repo, mock_audio
    ):
        mock_audio.return_value = make_audio(tmp_path)

        await handlers.cmd_mp3(make_message("/mp3 https://youtu.be/abc"), settings)

        assert mock_repo.find_cached.await_args.kwargs["media_kinds"] == ("audio",)

    async def test_video_flow_asks_only_for_video_kinds(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]

        await handlers.handle_private(make_message("https://youtu.be/abc"), settings)

        kinds = mock_repo.find_cached.await_args.kwargs["media_kinds"]
        assert kinds == ("video", "animation", "image")
        assert "audio" not in kinds

    async def test_a_video_row_is_never_served_to_mp3(
        self, settings, tmp_path, mock_repo, mock_audio
    ):
        """The repo filter does this; the handler double-checks the kind too."""
        mock_repo.find_cached.return_value = cached_row(media_kind="video")
        mock_audio.return_value = make_audio(tmp_path)
        msg = make_message("/mp3 https://youtu.be/abc")

        await handlers.cmd_mp3(msg, settings)

        msg.answer_video.assert_not_awaited()
        mock_audio.assert_awaited_once()  # fell through to a real audio download

    async def test_an_audio_row_is_never_served_to_the_video_flow(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_repo.find_cached.return_value = audio_row()
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        msg.answer_audio.assert_not_awaited()
        mock_download.assert_awaited_once()

    async def test_repo_error_falls_through_to_a_download(
        self, settings, tmp_path, mock_repo, mock_audio
    ):
        mock_repo.find_cached.side_effect = RuntimeError("db down")
        mock_audio.return_value = make_audio(tmp_path)
        msg = make_message("/mp3 https://youtu.be/abc")

        await handlers.cmd_mp3(msg, settings)  # must not raise

        mock_audio.assert_awaited_once()
        msg.answer_audio.assert_awaited_once()

    async def test_stale_file_id_falls_through_to_a_download(
        self, settings, tmp_path, mock_repo, mock_audio
    ):
        mock_repo.find_cached.return_value = audio_row()
        mock_audio.return_value = make_audio(tmp_path)
        msg = make_message("/mp3 https://youtu.be/abc")
        msg.answer_audio = AsyncMock(
            side_effect=[RuntimeError("wrong file identifier"), sent_audio("audio-file-id")]
        )

        await handlers.cmd_mp3(msg, settings)

        mock_audio.assert_awaited_once()
        assert msg.answer_audio.await_count == 2
        mock_repo.mark_failure.assert_not_awaited()

    async def test_coalescing_keys_do_not_collide(
        self, settings, tmp_path, mock_repo, mock_audio, mock_download
    ):
        """A video download of a link must not gate an /mp3 of the same link."""
        runtime.reset()
        runtime.configure(5, BOT_USERNAME, max_per_user=10)
        release = asyncio.Event()
        video_started = asyncio.Event()

        async def _video(url, workdir, **kwargs):
            video_started.set()
            await release.wait()
            return [make_media(Path(workdir))]

        mock_download.side_effect = _video
        mock_audio.return_value = make_audio(tmp_path)

        video = asyncio.create_task(
            handlers.handle_private(
                make_message("https://youtu.be/abc", user_id=1), settings
            )
        )
        await video_started.wait()

        # If the keys collided this would block until the video leader finished.
        audio_msg = make_message("/mp3 https://youtu.be/abc", user_id=2)
        await asyncio.wait_for(handlers.cmd_mp3(audio_msg, settings), timeout=2)

        audio_msg.answer_audio.assert_awaited_once()
        release.set()
        await video

    async def test_audio_gate_key_is_prefixed(self, settings, tmp_path, mock_repo, mock_audio):
        seen: list[str] = []
        real_coalesce = runtime.coalesce

        def _spy(key):
            seen.append(key)
            return real_coalesce(key)

        handlers.runtime.coalesce = _spy
        try:
            mock_audio.return_value = make_audio(tmp_path)
            await handlers.cmd_mp3(make_message("/mp3 https://youtu.be/abc"), settings)
        finally:
            handlers.runtime.coalesce = real_coalesce

        assert seen == [handlers.AUDIO_GATE_PREFIX + "https://youtu.be/abc"]


# ---------------------------------------------------------------------- inline mode


def make_inline_query(query: str = "", *, language_code: str | None = None) -> MagicMock:
    """An InlineQuery test double with an AsyncMock `answer`."""
    iq = MagicMock(name="InlineQuery")
    iq.query = query
    iq.from_user = SimpleNamespace(id=555, language_code=language_code)
    iq.answer = AsyncMock()
    return iq


class TestInlineMode:
    """Inline serves the file_id cache and nothing else — never a download."""

    async def test_cached_video_is_answered(self, mock_repo):
        mock_repo.find_cached.return_value = cached_row()
        iq = make_inline_query("https://youtu.be/abc")

        await handlers.handle_inline_query(iq)

        results = iq.answer.await_args.args[0]
        assert len(results) == 1
        assert results[0].video_file_id == "cached-file-id"
        assert results[0].type == "video"

    async def test_cached_animation_is_answered_as_mpeg4_gif(self, mock_repo):
        mock_repo.find_cached.return_value = cached_row(media_kind="animation")

        iq = make_inline_query("https://example.com/gif")
        await handlers.handle_inline_query(iq)

        (result,) = iq.answer.await_args.args[0]
        assert result.mpeg4_file_id == "cached-file-id"

    async def test_cached_audio_is_answered_as_audio(self, mock_repo):
        mock_repo.find_cached.return_value = audio_row()

        iq = make_inline_query("https://youtu.be/abc")
        await handlers.handle_inline_query(iq)

        (result,) = iq.answer.await_args.args[0]
        assert result.audio_file_id == "cached-audio-id"

    async def test_single_cached_photo_is_answered(self, mock_repo):
        mock_repo.find_cached.return_value = cached_row(
            media_kind="image", file_ids=["p1"]
        )

        iq = make_inline_query("https://instagram.com/p/x")
        await handlers.handle_inline_query(iq)

        (result,) = iq.answer.await_args.args[0]
        assert result.photo_file_id == "p1"

    async def test_gallery_expands_to_one_result_per_photo(self, mock_repo):
        """Inline has no media groups, so a carousel is offered item by item."""
        mock_repo.find_cached.return_value = cached_row(
            media_kind="image", file_ids=["p1", "p2", "p3"]
        )

        iq = make_inline_query("https://instagram.com/p/carousel")
        await handlers.handle_inline_query(iq)

        results = iq.answer.await_args.args[0]
        assert [r.photo_file_id for r in results] == ["p1", "p2", "p3"]
        assert len({r.id for r in results}) == 3, "result ids must be unique"

    async def test_gallery_is_capped(self, mock_repo):
        mock_repo.find_cached.return_value = cached_row(
            media_kind="image", file_ids=[f"p{i}" for i in range(14)]
        )

        iq = make_inline_query("https://instagram.com/p/big")
        await handlers.handle_inline_query(iq)

        assert len(iq.answer.await_args.args[0]) == handlers.MEDIA_GROUP_LIMIT

    async def test_hit_uses_a_long_impersonal_cache_time(self, mock_repo):
        mock_repo.find_cached.return_value = cached_row()

        iq = make_inline_query("https://youtu.be/abc")
        await handlers.handle_inline_query(iq)

        kwargs = iq.answer.await_args.kwargs
        assert kwargs["cache_time"] == handlers.INLINE_CACHE_TIME_HIT_S
        assert kwargs["is_personal"] is False

    async def test_miss_answers_empty_with_a_switch_to_pm_button(self, mock_repo):
        mock_repo.find_cached.return_value = None

        iq = make_inline_query("https://youtu.be/uncached")
        await handlers.handle_inline_query(iq)

        assert iq.answer.await_args.args[0] == []
        button = iq.answer.await_args.kwargs["button"]
        assert button.text == i18n.t("inline.no_cache", "en")
        assert button.start_parameter == handlers.INLINE_START_PARAMETER
        assert iq.answer.await_args.kwargs["cache_time"] == handlers.INLINE_CACHE_TIME_MISS_S

    async def test_button_text_is_localized(self, mock_repo):
        mock_repo.find_cached.return_value = None

        iq = make_inline_query("https://youtu.be/uncached", language_code="ru-RU")
        await handlers.handle_inline_query(iq)

        assert iq.answer.await_args.kwargs["button"].text == i18n.t("inline.no_cache", "ru")

    async def test_query_without_a_url_is_a_miss_and_never_hits_the_repo(self, mock_repo):
        iq = make_inline_query("just typing")

        await handlers.handle_inline_query(iq)

        mock_repo.find_cached.assert_not_awaited()
        assert iq.answer.await_args.args[0] == []
        assert iq.answer.await_args.kwargs["button"] is not None

    async def test_empty_query_is_a_miss(self, mock_repo):
        iq = make_inline_query("")

        await handlers.handle_inline_query(iq)

        assert iq.answer.await_args.args[0] == []

    async def test_stories_are_never_served_inline(self, mock_repo):
        """They expire — a hit would ship content the poster already removed."""
        iq = make_inline_query("https://instagram.com/stories/someone/123")

        await handlers.handle_inline_query(iq)

        mock_repo.find_cached.assert_not_awaited()
        assert iq.answer.await_args.args[0] == []

    async def test_repo_failure_degrades_to_a_miss(self, mock_repo):
        mock_repo.find_cached.side_effect = RuntimeError("db down")

        iq = make_inline_query("https://youtu.be/abc")
        await handlers.handle_inline_query(iq)  # must not raise

        assert iq.answer.await_args.args[0] == []

    async def test_image_row_without_a_file_id_list_is_a_miss(self, mock_repo):
        mock_repo.find_cached.return_value = cached_row(media_kind="image")

        iq = make_inline_query("https://instagram.com/p/old")
        await handlers.handle_inline_query(iq)

        assert iq.answer.await_args.args[0] == []

    async def test_hit_is_audited_anonymously(self, mock_repo):
        mock_repo.find_cached.return_value = cached_row()

        iq = make_inline_query("https://youtu.be/abc")
        await handlers.handle_inline_query(iq)

        create_kwargs = mock_repo.create_request.await_args.kwargs
        assert create_kwargs["chat_type"] == "inline"
        assert create_kwargs["url"] == "https://youtu.be/abc"
        # Nothing identifying, ever — same rule as every other flow.
        assert not {"user_id", "chat_id", "message_id"} & set(create_kwargs)

        success = mock_repo.mark_success.await_args.kwargs
        assert success["cache_hit"] is True
        assert success["media_kind_override"] == "video"

    async def test_audio_hit_is_audited_with_the_audio_kind(self, mock_repo):
        mock_repo.find_cached.return_value = audio_row()

        await handlers.handle_inline_query(make_inline_query("https://youtu.be/abc"))

        assert mock_repo.mark_success.await_args.kwargs["media_kind_override"] == "audio"

    async def test_a_miss_is_not_audited(self, mock_repo):
        mock_repo.find_cached.return_value = None

        await handlers.handle_inline_query(make_inline_query("https://youtu.be/x"))

        mock_repo.create_request.assert_not_awaited()

    async def test_audit_failure_never_breaks_the_answer(self, mock_repo):
        mock_repo.find_cached.return_value = cached_row()
        mock_repo.create_request.side_effect = RuntimeError("db down")

        iq = make_inline_query("https://youtu.be/abc")
        await handlers.handle_inline_query(iq)  # must not raise

        assert len(iq.answer.await_args.args[0]) == 1

    async def test_inline_looks_up_all_cacheable_kinds(self, mock_repo):
        """Unlike the message flows, inline can render every kind — including audio."""
        mock_repo.find_cached.return_value = None

        await handlers.handle_inline_query(make_inline_query("https://youtu.be/abc"))

        assert "media_kinds" not in mock_repo.find_cached.await_args.kwargs
