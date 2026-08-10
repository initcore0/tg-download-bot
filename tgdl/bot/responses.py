"""User-facing strings. Kept in one place so wording stays consistent.

Note: media itself is always sent WITHOUT captions or branding (ARCHITECTURE.md §2).
These strings are only for command replies, status, and errors.
"""
from __future__ import annotations

START = (
    "👋 Hi! Send me a link to a video or photo and I'll send the media straight back "
    "— no captions, no watermarks, ready to forward.\n\n"
    "Works with YouTube, TikTok, Instagram, X/Twitter, Twitch clips, Pinterest and many more.\n\n"
    "In a group, mention me with the link: <code>@{username} &lt;link&gt;</code>"
)

HELP = (
    "📥 <b>How to use me</b>\n\n"
    "• <b>Private chat</b>: just send a link.\n"
    "• <b>Groups &amp; channels</b>: mention me together with the link "
    "(<code>@{username} &lt;link&gt;</code>).\n\n"
    "I grab the media at up to 720p and under 48 MB, then send it back as plain "
    "video or photo.\n\n"
    "Commands: /start, /help"
)

# Fallback when the bot username isn't known yet (bot.me() not resolved).
DEFAULT_USERNAME = "the_bot"

GENERIC_ERROR = "❌ Something went wrong."

BUSY_PER_USER = "⏳ I'm still working on your previous link — please wait for it to finish."


def start_text(username: str | None) -> str:
    return START.format(username=username or DEFAULT_USERNAME)


def help_text(username: str | None) -> str:
    return HELP.format(username=username or DEFAULT_USERNAME)
