"""Unit tests for the bot layer (M2). All external modules are mocked."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tgdl.bot import handlers, responses, runtime
from tgdl.config import Settings
from tgdl.downloader.models import (
    DownloadError,
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
            id=777, username="alice", first_name="Alice", last_name="A"
        )
    else:
        msg.from_user = None

    status = MagicMock(name="StatusMessage")
    status.delete = AsyncMock()

    msg.answer = AsyncMock(return_value=status)
    msg.reply = AsyncMock(return_value=status)
    msg.answer_video = AsyncMock(return_value=sent_video("vid-file-id"))
    msg.reply_video = AsyncMock(return_value=sent_video("vid-file-id"))
    msg.answer_photo = AsyncMock(return_value=sent_photo("photo-file-id"))
    msg.reply_photo = AsyncMock(return_value=sent_photo("photo-file-id"))
    msg.answer_animation = AsyncMock(return_value=sent_animation("anim-file-id"))
    msg.reply_animation = AsyncMock(return_value=sent_animation("anim-file-id"))
    msg.answer_media_group = AsyncMock(return_value=[sent_photo("group-file-id")])
    msg.reply_media_group = AsyncMock(return_value=[sent_photo("group-file-id")])

    msg.bot = MagicMock()
    msg.bot.send_chat_action = AsyncMock()

    msg._status = status
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


@pytest.fixture
def mock_repo(monkeypatch):
    """Patch every repo function used by handlers; returns the namespace of mocks."""
    user_row = SimpleNamespace(id=5)
    request_row = SimpleNamespace(id=99)
    mocks = SimpleNamespace(
        get_or_create_user=AsyncMock(return_value=user_row),
        create_request=AsyncMock(return_value=request_row),
        mark_success=AsyncMock(),
        mark_failure=AsyncMock(),
    )
    for name in ("get_or_create_user", "create_request", "mark_success", "mark_failure"):
        monkeypatch.setattr(handlers.repo, name, getattr(mocks, name))
    return mocks


@pytest.fixture
def mock_download(monkeypatch):
    """Patch service.download_media; test sets .return_value / .side_effect."""
    mock = AsyncMock()
    monkeypatch.setattr(handlers.service, "download_media", mock)
    return mock


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
        msg.reply.assert_awaited()  # status message was a reply too

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
        # No from_user -> no user upsert, and the request is anonymous.
        mock_repo.get_or_create_user.assert_not_awaited()
        assert mock_repo.create_request.await_args.kwargs["user_id"] is None

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

    async def test_status_message_shown_then_deleted(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        msg.answer.assert_awaited_once_with(responses.STATUS_WORKING)
        msg._status.delete.assert_awaited_once()

    async def test_upload_chat_action_sent(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        msg.bot.send_chat_action.assert_awaited_once()
        assert msg.bot.send_chat_action.await_args.args[1] == "upload_video"

    async def test_audit_user_and_request_recorded(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc", chat_id=111, message_id=42)

        await handlers.handle_private(msg, settings)

        mock_repo.get_or_create_user.assert_awaited_once()
        assert mock_repo.get_or_create_user.await_args.kwargs["telegram_id"] == 777

        req = mock_repo.create_request.await_args.kwargs
        assert req["user_id"] == 5
        assert req["chat_id"] == 111
        assert req["message_id"] == 42
        assert req["url"] == "https://youtu.be/abc"
        assert req["platform"] == "youtube"


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
        assert MediaTooLargeError.user_message in sent_texts
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
        assert UnsupportedUrlError.user_message in sent_texts

    async def test_group_error_is_replied(self, settings, mock_repo, mock_download):
        mock_download.side_effect = UnsupportedUrlError()
        msg = make_message(f"@{BOT_USERNAME} https://x.com/1", chat_type="group")

        await handlers.handle_group(msg, settings)

        sent_texts = [c.args[0] for c in msg.reply.await_args_list]
        assert UnsupportedUrlError.user_message in sent_texts

    async def test_unexpected_exception_shows_generic_message(
        self, settings, mock_repo, mock_download
    ):
        boom = RuntimeError("ffmpeg exploded")
        mock_download.side_effect = boom
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        sent_texts = [c.args[0] for c in msg.answer.await_args_list]
        assert responses.GENERIC_ERROR in sent_texts
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

    async def test_status_deleted_on_failure(
        self, settings, mock_repo, mock_download
    ):
        mock_download.side_effect = UnsupportedUrlError()
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        msg._status.delete.assert_awaited_once()

    async def test_send_failure_is_caught(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")
        msg.answer_video = AsyncMock(side_effect=RuntimeError("telegram 500"))

        await handlers.handle_private(msg, settings)  # must not raise

        sent_texts = [c.args[0] for c in msg.answer.await_args_list]
        assert responses.GENERIC_ERROR in sent_texts
        mock_repo.mark_failure.assert_awaited_once()


# -------------------------------------------------- audit resilience & cleanup


class TestAuditResilience:
    async def test_repo_failures_do_not_break_flow(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        mock_repo.get_or_create_user.side_effect = RuntimeError("db down")
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

    async def test_status_delete_failure_ignored(
        self, settings, tmp_path, mock_repo, mock_download
    ):
        mock_download.return_value = [make_media(tmp_path)]
        msg = make_message("https://youtu.be/abc")
        msg._status.delete = AsyncMock(side_effect=RuntimeError("already deleted"))

        await handlers.handle_private(msg, settings)  # must not raise

        mock_repo.mark_success.assert_awaited_once()


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
        runtime.configure(2, BOT_USERNAME)

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

        tasks = [
            asyncio.create_task(
                handlers.handle_private(make_message("https://youtu.be/abc"), settings)
            )
            for _ in range(5)
        ]
        await asyncio.sleep(0.05)
        observed_peak = peak
        release.set()
        await asyncio.gather(*tasks)

        assert observed_peak == 2, f"expected max 2 concurrent downloads, saw {observed_peak}"


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


class TestDownloadErrorBase:
    async def test_base_download_error_default_message(
        self, settings, mock_repo, mock_download
    ):
        mock_download.side_effect = DownloadError()
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        sent_texts = [c.args[0] for c in msg.answer.await_args_list]
        assert DownloadError.user_message in sent_texts

    async def test_custom_user_message_is_used(
        self, settings, mock_repo, mock_download
    ):
        mock_download.side_effect = DownloadError("internals", user_message="Nope, sorry.")
        msg = make_message("https://youtu.be/abc")

        await handlers.handle_private(msg, settings)

        sent_texts = [c.args[0] for c in msg.answer.await_args_list]
        assert "Nope, sorry." in sent_texts
