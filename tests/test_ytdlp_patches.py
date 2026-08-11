"""Tests for the yt-dlp runtime patches (Twitch clips full-query rewrite)."""
from __future__ import annotations

import pytest

from tgdl.downloader import ytdlp_patches

yt_dlp = pytest.importorskip("yt_dlp")
from yt_dlp.extractor.twitch import TwitchBaseIE, TwitchClipsIE


@pytest.fixture(autouse=True)
def fresh_patch_state(monkeypatch):
    """Each test applies the patch from a pristine state and restores the original."""
    monkeypatch.setattr(ytdlp_patches, "_applied", False)
    original = TwitchBaseIE.__dict__.get("_download_gql")
    yield
    if original is not None:
        TwitchBaseIE._download_gql = original


def _captured_ops(url_slug: str) -> list[dict]:
    """Run the patched _download_gql on a stub extractor; return the ops it POSTs."""
    ytdlp_patches.apply()
    ie = TwitchClipsIE()
    sent: list[dict] = []

    def fake_base_gql(video_id, ops, note, fatal=True):
        sent.extend(ops)
        return [{"data": {"clip": None}}]

    ie._download_base_gql = fake_base_gql
    ie._download_gql(
        url_slug,
        [{"operationName": "ShareClipRenderStatus", "variables": {"slug": url_slug}}],
        "Downloading clip GraphQL",
    )
    return sent


def test_share_clip_op_is_rewritten_to_full_query():
    ops = _captured_ops("SomeClipSlug")

    assert len(ops) == 1
    op = ops[0]
    # Full query text replaces the rotation-prone persisted-query hash.
    assert "extensions" not in op
    assert op["operationName"] == "ShareClipRenderStatus"
    assert op["variables"] == {"slug": "SomeClipSlug"}
    assert "query ShareClipRenderStatus($slug: ID!)" in op["query"]
    # The fields TwitchClipsIE._real_extract actually parses must be requested.
    for field in ("playbackAccessToken", "videoQualities", "sourceURL", "assets",
                  "durationSeconds", "broadcaster", "curator"):
        assert field in op["query"], f"query is missing {field}"


def test_other_operations_keep_persisted_query_path():
    ytdlp_patches.apply()
    ie = TwitchClipsIE()
    sent: list[dict] = []

    def fake_base_gql(video_id, ops, note, fatal=True):
        sent.extend(ops)
        return [{"data": {}}, {"data": {"clip": None}}]

    ie._download_base_gql = fake_base_gql
    any_other = next(iter(TwitchBaseIE._OPERATION_HASHES))
    ie._download_gql(
        "slug",
        [
            {"operationName": any_other, "variables": {}},
            {"operationName": "ShareClipRenderStatus", "variables": {"slug": "s"}},
        ],
        "note",
    )

    assert sent[0]["extensions"]["persistedQuery"]["sha256Hash"] == (
        TwitchBaseIE._OPERATION_HASHES[any_other]
    )
    assert "query" not in sent[0]
    assert "extensions" not in sent[1]
    assert "query" in sent[1]


def test_apply_is_idempotent():
    ytdlp_patches.apply()
    patched_once = TwitchBaseIE._download_gql
    ytdlp_patches.apply()
    assert TwitchBaseIE._download_gql is patched_once


def test_apply_survives_missing_internals(monkeypatch, caplog):
    """If yt-dlp's surface changes, apply() must log and leave extraction usable."""
    monkeypatch.delattr(TwitchBaseIE, "_download_base_gql")
    ytdlp_patches.apply()  # must not raise
    assert any("continuing unpatched" in r.message for r in caplog.records)


@pytest.mark.network
async def test_real_twitch_clip_extracts(tmp_path):
    """End-to-end: the clip that produced KeyError('data') in production resolves."""
    from tgdl.downloader import ytdlp

    entries = await ytdlp.extract(
        "https://www.twitch.tv/encrypted_cat/clip/AltruisticHilariousBasenjiFailFish-G5gowh-Ovu9ZabTn",
        tmp_path,
        max_height=720,
        download=False,
        max_attempts=1,
    )
    assert len(entries) == 1
    assert entries[0].get("duration") == 6
    assert entries[0].get("formats")
