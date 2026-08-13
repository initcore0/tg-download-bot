"""URL extraction, platform detection and normalization."""
from __future__ import annotations

import pytest

from tgdl.downloader.urls import detect_platform, extract_urls, normalize_url


class TestExtractUrls:
    def test_empty_and_none_text(self):
        assert extract_urls("") == []
        assert extract_urls("no links here at all") == []

    def test_single_url(self):
        assert extract_urls("check https://youtu.be/abc123 out") == ["https://youtu.be/abc123"]

    def test_multiple_urls_in_order(self):
        text = "first http://a.com/1 then https://b.com/2"
        assert extract_urls(text) == ["http://a.com/1", "https://b.com/2"]

    def test_strips_trailing_sentence_punctuation(self):
        assert extract_urls("see https://x.com/i/status/1.") == ["https://x.com/i/status/1"]
        assert extract_urls("see https://x.com/a,") == ["https://x.com/a"]
        assert extract_urls("wow https://x.com/a!") == ["https://x.com/a"]

    def test_keeps_balanced_parens_but_strips_unbalanced(self):
        assert extract_urls("(see https://en.wikipedia.org/wiki/Foo)") == [
            "https://en.wikipedia.org/wiki/Foo"
        ]
        assert extract_urls("https://en.wikipedia.org/wiki/Foo_(bar)") == [
            "https://en.wikipedia.org/wiki/Foo_(bar)"
        ]

    def test_keeps_query_and_fragment(self):
        url = "https://www.youtube.com/watch?v=abc&t=30s"
        assert extract_urls(f"link {url} end") == [url]

    def test_ignores_non_http_schemes(self):
        assert extract_urls("ftp://files.example.com/a mailto:me@example.com") == []

    def test_url_at_string_boundaries(self):
        assert extract_urls("https://a.com/x") == ["https://a.com/x"]
        assert extract_urls("\nhttps://a.com/x\n") == ["https://a.com/x"]

    def test_multiline_message(self):
        text = "line one https://tiktok.com/@u/video/1\nline two https://pin.it/xyz"
        assert extract_urls(text) == ["https://tiktok.com/@u/video/1", "https://pin.it/xyz"]

    def test_rejects_scheme_without_host(self):
        assert extract_urls("https:// nothing") == []


class TestDetectPlatform:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.youtube.com/watch?v=abc", "youtube"),
            ("https://youtu.be/abc", "youtube"),
            ("https://m.youtube.com/watch?v=abc", "youtube"),
            ("https://www.youtube-nocookie.com/embed/abc", "youtube"),
            ("https://youtube.com/shorts/abc", "youtube"),
            ("https://www.tiktok.com/@user/video/123", "tiktok"),
            ("https://vm.tiktok.com/ZMabc/", "tiktok"),
            ("https://www.instagram.com/reel/abc/", "instagram"),
            ("https://instagr.am/p/abc/", "instagram"),
            ("https://twitter.com/user/status/123", "twitter"),
            ("https://x.com/user/status/123", "twitter"),
            ("https://www.twitch.tv/videos/123", "twitch"),
            ("https://clips.twitch.tv/SomeClip", "twitch"),
            ("https://www.pinterest.com/pin/123/", "pinterest"),
            ("https://pin.it/abc", "pinterest"),
            ("https://www.reddit.com/r/aww/comments/abc/title/", "reddit"),
            ("https://old.reddit.com/r/aww/comments/abc/title/", "reddit"),
            ("https://reddit.com/r/pics/comments/xyz/", "reddit"),
            ("https://redd.it/abc123", "reddit"),
            ("https://i.redd.it/abc123.jpg", "reddit"),
            ("https://v.redd.it/abc123", "reddit"),
            ("https://example.com/video.mp4", "other"),
            ("https://vimeo.com/12345", "other"),
        ],
    )
    def test_platform_mapping(self, url, expected):
        assert detect_platform(url) == expected

    def test_case_insensitive_host(self):
        assert detect_platform("https://WWW.YouTube.COM/watch?v=a") == "youtube"

    def test_malformed_url_is_other(self):
        assert detect_platform("not a url") == "other"
        assert detect_platform("") == "other"

    def test_country_variant_hosts(self):
        assert detect_platform("https://pinterest.co.uk/pin/1/") == "pinterest"


class TestNormalizeUrl:
    def test_lowercases_host_only(self):
        assert normalize_url("https://WWW.Example.COM/Path") == "https://example.com/Path"

    def test_strips_www(self):
        assert normalize_url("https://www.youtube.com/watch?v=abc") == (
            "https://youtube.com/watch?v=abc"
        )

    def test_drops_fragment(self):
        assert normalize_url("https://example.com/a#section") == "https://example.com/a"

    def test_drops_utm_params(self):
        got = normalize_url("https://example.com/a?utm_source=x&utm_medium=y&keep=1")
        assert got == "https://example.com/a?keep=1"

    def test_drops_known_tracking_params(self):
        assert normalize_url("https://youtu.be/abc?si=XYZ") == "https://youtube.com/watch?v=abc"
        assert normalize_url("https://example.com/a?igsh=1&fbclid=2") == "https://example.com/a"

    def test_expands_youtu_be(self):
        assert normalize_url("https://youtu.be/jNQXAC9IVRw") == (
            "https://youtube.com/watch?v=jNQXAC9IVRw"
        )

    def test_expands_youtu_be_with_tracking(self):
        assert normalize_url("https://youtu.be/jNQXAC9IVRw?si=abc&t=10") == (
            "https://youtube.com/watch?v=jNQXAC9IVRw"
        )

    def test_keeps_youtube_v_param(self):
        assert normalize_url("https://www.youtube.com/watch?v=abc&feature=share") == (
            "https://youtube.com/watch?v=abc"
        )

    def test_strips_trailing_slash(self):
        assert normalize_url("https://example.com/a/b/") == "https://example.com/a/b"

    def test_bare_host_is_stable(self):
        assert normalize_url("https://example.com/") == "https://example.com"
        assert normalize_url("https://example.com") == "https://example.com"

    def test_idempotent(self):
        for url in [
            "https://www.youtube.com/watch?v=abc&si=1",
            "https://youtu.be/abc",
            "https://x.com/u/status/1/",
            "https://example.com/",
        ]:
            once = normalize_url(url)
            assert normalize_url(once) == once

    def test_equivalent_urls_collapse_to_same_key(self):
        a = normalize_url("https://youtu.be/dQw4w9WgXcQ?si=tracking")
        b = normalize_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=youtu.be")
        assert a == b

    def test_empty_input(self):
        assert normalize_url("") == ""

    def test_non_url_passthrough(self):
        assert normalize_url("just text") == "just text"

    def test_preserves_path_case_and_query_values(self):
        assert normalize_url("https://example.com/AbC?q=Hello") == "https://example.com/AbC?q=Hello"

    def test_reddit_urls_survive_the_generic_path(self):
        """Reddit needs no special-casing — just don't mangle its permalinks."""
        assert normalize_url("https://www.reddit.com/r/aww/comments/abc/a_title/") == (
            "https://reddit.com/r/aww/comments/abc/a_title"
        )
        # Tracking params go, the permalink stays.
        assert normalize_url(
            "https://www.reddit.com/r/aww/comments/abc/a_title/?utm_source=share"
        ) == "https://reddit.com/r/aww/comments/abc/a_title"
        # A share link's opaque id lives in the PATH, so the `s` drop-param (which
        # only ever applies to the query) must not touch it.
        assert normalize_url("https://www.reddit.com/r/aww/s/AbCdEf") == (
            "https://reddit.com/r/aww/s/AbCdEf"
        )
        # old.reddit is a distinct host and stays one; redd.it short links pass through.
        assert normalize_url("https://old.reddit.com/r/aww/comments/abc/") == (
            "https://old.reddit.com/r/aww/comments/abc"
        )
        assert normalize_url("https://redd.it/abc123") == "https://redd.it/abc123"


import socket

from tgdl.downloader.urls import is_safe_public_url


def _resolver_returning(ip: str):
    """Fake socket.getaddrinfo that always resolves to `ip`."""
    def _resolve(host, port, *args, **kwargs):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]
    return _resolve


class TestSsrfGuard:
    def test_public_host_allowed(self):
        assert is_safe_public_url(
            "https://youtube.com/watch?v=x", resolver=_resolver_returning("142.250.72.14")
        )

    def test_literal_public_ip_allowed(self):
        assert is_safe_public_url("https://8.8.8.8/x", resolver=_resolver_returning("8.8.8.8"))

    def test_cloud_metadata_blocked(self):
        # The classic SSRF target.
        assert not is_safe_public_url("http://169.254.169.254/latest/meta-data/")

    def test_localhost_blocked(self):
        assert not is_safe_public_url("http://localhost/x", resolver=_resolver_returning("127.0.0.1"))

    def test_private_range_blocked(self):
        assert not is_safe_public_url(
            "http://internal.local/x", resolver=_resolver_returning("10.1.2.3")
        )
        assert not is_safe_public_url(
            "http://nas.home/x", resolver=_resolver_returning("192.168.1.10")
        )

    def test_literal_private_ip_blocked(self):
        assert not is_safe_public_url("http://192.168.0.1/x")
        assert not is_safe_public_url("http://127.0.0.1:8080/x")
        assert not is_safe_public_url("http://[::1]/x")

    def test_ipv4_mapped_ipv6_metadata_blocked(self):
        assert not is_safe_public_url(
            "http://sneaky/x", resolver=_resolver_returning("::ffff:169.254.169.254")
        )

    def test_non_http_scheme_blocked(self):
        assert not is_safe_public_url("file:///etc/passwd")
        assert not is_safe_public_url("ftp://example.com/x")

    def test_dns_failure_fails_closed(self):
        def _boom(*args, **kwargs):
            raise socket.gaierror("nope")
        assert not is_safe_public_url("https://does-not-exist.invalid/x", resolver=_boom)

    def test_dns_rebinding_any_bad_answer_rejects(self):
        # A resolver returning both a public and a private address must be rejected.
        def _mixed(host, port, *args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.2.3.4", port)),
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
            ]
        assert not is_safe_public_url("https://evil.example/x", resolver=_mixed)
