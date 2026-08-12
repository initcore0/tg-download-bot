"""Tiny message catalog for English + Russian.

Locale is chosen per-message from the Telegram user's `language_code` (an IETF tag
like ``ru``, ``ru-RU``, ``en-US``); nothing is stored, so this does not affect the
bot's anonymity. English is the default for any unrecognized or missing language.

Usage:
    from tgdl import i18n
    loc = i18n.locale_of(message.from_user.language_code)  # -> "en" | "ru"
    text = i18n.t("help", loc, username="mybot")
"""
from __future__ import annotations

DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = ("en", "ru")

# Every user-facing string, keyed by a stable id. HTML is allowed (the bot sends with
# parse_mode=HTML); `{name}` placeholders are filled via str.format in `t()`.
_MESSAGES: dict[str, dict[str, str]] = {
    "start": {
        "en": (
            "👋 Hi! Send me a link to a video or photo and I'll send the media straight "
            "back — no captions, no watermarks, ready to forward.\n\n"
            "Works with YouTube, TikTok, Instagram, X/Twitter, Twitch clips, Pinterest "
            "and many more.\n\n"
            "In a group, mention me with the link: <code>@{username} &lt;link&gt;</code>"
        ),
        "ru": (
            "👋 Привет! Пришли мне ссылку на видео или фото, и я отправлю медиа обратно "
            "— без подписей, без водяных знаков, готовое к пересылке.\n\n"
            "Работает с YouTube, TikTok, Instagram, X/Twitter, клипами Twitch, Pinterest "
            "и многими другими.\n\n"
            "В группе упомяни меня вместе со ссылкой: <code>@{username} &lt;ссылка&gt;</code>"
        ),
    },
    "help": {
        "en": (
            "📥 <b>How to use me</b>\n\n"
            "• <b>Private chat</b>: just send a link.\n"
            "• <b>Groups &amp; channels</b>: mention me together with the link "
            "(<code>@{username} &lt;link&gt;</code>).\n\n"
            "I grab the media at up to 720p and under 48 MB, then send it back as plain "
            "video or photo.\n\n"
            "Commands: /start, /help"
        ),
        "ru": (
            "📥 <b>Как мной пользоваться</b>\n\n"
            "• <b>Личные сообщения</b>: просто пришли ссылку.\n"
            "• <b>Группы и каналы</b>: упомяни меня вместе со ссылкой "
            "(<code>@{username} &lt;ссылка&gt;</code>).\n\n"
            "Я скачиваю медиа в качестве до 720p и размером до 48 МБ, затем отправляю "
            "обратно как обычное видео или фото.\n\n"
            "Команды: /start, /help"
        ),
    },
    "busy_per_user": {
        "en": "⏳ I'm still working on your previous link — please wait for it to finish.",
        "ru": "⏳ Я всё ещё обрабатываю твою предыдущую ссылку — подожди, пока она закончится.",
    },
    "generic_error": {
        "en": "❌ Something went wrong.",
        "ru": "❌ Что-то пошло не так.",
    },
    # --- download error messages (keyed by DownloadError.message_key) ---
    "error.generic": {
        "en": "Sorry, I couldn't download that.",
        "ru": "Извини, не удалось это скачать.",
    },
    "error.unsupported_url": {
        "en": "I don't recognize a downloadable link in that message.",
        "ru": "Я не нашёл в сообщении ссылку, которую можно скачать.",
    },
    "error.extraction": {
        "en": (
            "I couldn't fetch media from that link. It may be private, deleted, or "
            "unsupported."
        ),
        "ru": (
            "Не удалось получить медиа по этой ссылке. Возможно, оно приватное, удалено "
            "или не поддерживается."
        ),
    },
    "error.transient": {
        "en": (
            "That service is rate-limiting or temporarily unavailable. Please try again "
            "in a moment."
        ),
        "ru": (
            "Этот сервис ограничивает частоту запросов или временно недоступен. "
            "Попробуй ещё раз через мгновение."
        ),
    },
    "error.too_large": {
        "en": (
            "That video is too large to send over Telegram (50 MB bot limit), even after "
            "compression."
        ),
        "ru": (
            "Это видео слишком большое для отправки через Telegram (лимит бота 50 МБ), "
            "даже после сжатия."
        ),
    },
    "error.transcode": {
        "en": "I downloaded the media but couldn't convert it to a Telegram-friendly format.",
        "ru": "Я скачал медиа, но не смог преобразовать его в подходящий для Telegram формат.",
    },
    "error.login_required": {
        "en": (
            "That content is only visible to logged-in accounts (stories and some "
            "posts). The bot isn't signed in to that service, so it can't fetch it."
        ),
        "ru": (
            "Этот контент доступен только авторизованным аккаунтам (истории и "
            "некоторые посты). Бот не авторизован в этом сервисе и не может его "
            "получить."
        ),
    },
    "error.timeout": {
        "en": "That download took too long and was cancelled. Please try a shorter video.",
        "ru": "Скачивание заняло слишком много времени и было отменено. Попробуй видео покороче.",
    },
}


def locale_of(language_code: str | None) -> str:
    """Map a Telegram IETF `language_code` to a supported locale.

    Uses the primary subtag (``ru-RU`` -> ``ru``). Anything not supported, and a
    missing/empty code, fall back to English.
    """
    if not language_code:
        return DEFAULT_LOCALE
    primary = language_code.split("-", 1)[0].strip().lower()
    return primary if primary in SUPPORTED_LOCALES else DEFAULT_LOCALE


def t(key: str, locale: str = DEFAULT_LOCALE, /, **kwargs: object) -> str:
    """Translate `key` into `locale`, formatting any `{placeholders}` with kwargs.

    Falls back to English, then to the raw key, so a missing translation degrades
    gracefully instead of raising.
    """
    variants = _MESSAGES.get(key)
    if variants is None:
        return key
    template = variants.get(locale) or variants.get(DEFAULT_LOCALE) or key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template
