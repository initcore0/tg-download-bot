"""Runtime patches for upstream yt-dlp extractor breakages.

Twitch periodically rotates the sha256 hashes of its GraphQL "persisted queries",
which breaks yt-dlp's Twitch clips extractor until upstream ships a new hash
(KeyError('data') / PersistedQueryNotFound — e.g. yt-dlp issues #14396, #16464).
Twitch's GQL endpoint also accepts the full query text, which is immune to hash
rotation, so we rewrite the ShareClipRenderStatus operation to send the query
itself. The response shape is identical, so the extractor's parsing is untouched.

Patches are applied lazily (yt-dlp is imported lazily in ytdlp.py), idempotently,
and defensively: if yt-dlp's internals change so a patch no longer fits, we log
and continue unpatched — a broken patch must never take down every extraction.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_applied = False

# Mirrors the fields yt-dlp's TwitchClipsIE._real_extract reads from the
# ShareClipRenderStatus persisted query (broadcaster.isPartner is omitted: it is
# not queryable anonymously and only feeds optional channel_is_verified metadata).
_SHARE_CLIP_QUERY = """\
query ShareClipRenderStatus($slug: ID!) {
  clip(slug: $slug) {
    id
    title
    viewCount
    createdAt
    durationSeconds
    thumbnailURL
    broadcaster { id displayName followers { totalCount } }
    curator { id displayName }
    game { displayName }
    playbackAccessToken(
      params: {platform: "web", playerType: "clips-embed", playerBackend: "mediaplayer"}
    ) { signature value }
    assets {
      aspectRatio
      thumbnailURL(height: 480)
      videoQualities { frameRate quality sourceURL }
    }
  }
}
"""


def apply() -> None:
    """Apply all patches once. Safe to call repeatedly and never raises."""
    global _applied
    if _applied:
        return
    _applied = True
    try:
        _patch_twitch_clip_query()
        log.info("applied Twitch clips full-query patch to yt-dlp")
    except Exception:
        log.warning(
            "could not patch yt-dlp Twitch clips extractor; continuing unpatched",
            exc_info=True,
        )


def _patch_twitch_clip_query() -> None:
    from yt_dlp.extractor.twitch import TwitchBaseIE

    original = TwitchBaseIE._download_gql

    def _download_gql(self, video_id, ops, note, fatal=True):
        if not any(op.get("operationName") == "ShareClipRenderStatus" for op in ops):
            return original(self, video_id, ops, note, fatal)
        new_ops = []
        for op in ops:
            if op.get("operationName") == "ShareClipRenderStatus":
                new_ops.append(
                    {
                        "operationName": "ShareClipRenderStatus",
                        "query": _SHARE_CLIP_QUERY,
                        "variables": op.get("variables", {}),
                    }
                )
            else:
                op["extensions"] = {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": self._OPERATION_HASHES[op["operationName"]],
                    },
                }
                new_ops.append(op)
        return self._download_base_gql(video_id, new_ops, note, fatal=fatal)

    # Sanity-check the surface we rely on before swapping the method in.
    if not callable(getattr(TwitchBaseIE, "_download_base_gql", None)):
        raise TypeError("TwitchBaseIE._download_base_gql is gone")
    TwitchBaseIE._download_gql = _download_gql
