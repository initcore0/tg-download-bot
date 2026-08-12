"""Per-platform cookie routing (tgdl/downloader/cookies.py)."""
from __future__ import annotations

import pytest

from tgdl.downloader import cookies


@pytest.fixture(autouse=True)
def reset():
    cookies.configure()
    yield
    cookies.configure()


@pytest.fixture
def jars(tmp_path):
    generic = tmp_path / "generic.txt"
    youtube = tmp_path / "youtube.txt"
    instagram = tmp_path / "instagram.txt"
    for f in (generic, youtube, instagram):
        f.write_text("# Netscape HTTP Cookie File\n")
    return generic, youtube, instagram


class TestResolve:
    def test_youtube_prefers_its_own_jar(self, jars):
        generic, youtube, instagram = jars
        cookies.configure(generic=generic, youtube=youtube, instagram=instagram)
        assert cookies.resolve("youtube") == youtube

    def test_youtube_falls_back_to_generic(self, jars):
        generic, _, _ = jars
        cookies.configure(generic=generic)
        assert cookies.resolve("youtube") == generic

    def test_youtube_never_gets_the_instagram_jar(self, jars):
        _, _, instagram = jars
        cookies.configure(instagram=instagram)
        assert cookies.resolve("youtube") is None

    def test_instagram_post_is_anonymous_even_with_all_jars(self, jars):
        generic, youtube, instagram = jars
        cookies.configure(generic=generic, youtube=youtube, instagram=instagram)
        assert cookies.resolve("instagram") is None

    def test_instagram_story_gets_instagram_jar(self, jars):
        generic, _, instagram = jars
        cookies.configure(generic=generic, instagram=instagram)
        assert cookies.resolve("instagram", story=True) == instagram

    def test_instagram_story_falls_back_to_generic(self, jars):
        generic, _, _ = jars
        cookies.configure(generic=generic)
        assert cookies.resolve("instagram", story=True) == generic

    def test_instagram_login_retry_uses_instagram_jar(self, jars):
        _, _, instagram = jars
        cookies.configure(instagram=instagram)
        assert cookies.resolve("instagram", use_login=True) == instagram

    def test_other_platforms_use_generic_only(self, jars):
        generic, youtube, instagram = jars
        cookies.configure(generic=generic, youtube=youtube, instagram=instagram)
        assert cookies.resolve("twitter") == generic
        assert cookies.resolve("pinterest") == generic
        assert cookies.resolve("other") == generic

    def test_nothing_configured_is_all_anonymous(self):
        for platform in ("youtube", "instagram", "twitter", "other"):
            assert cookies.resolve(platform) is None
        assert cookies.instagram_login() is None

    def test_missing_file_is_ignored(self, tmp_path):
        cookies.configure(youtube=tmp_path / "nope.txt")
        assert cookies.resolve("youtube") is None
