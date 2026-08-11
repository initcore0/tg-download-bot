"""Tests for the i18n catalog, locale resolution, and translation."""
from __future__ import annotations

import pytest

from tgdl import i18n
from tgdl.downloader.models import (
    DownloadError,
    DownloadTimeoutError,
    ExtractionError,
    MediaTooLargeError,
    TranscodeError,
    TransientExtractionError,
    UnsupportedUrlError,
)

ALL_ERRORS = [
    DownloadError,
    UnsupportedUrlError,
    ExtractionError,
    TransientExtractionError,
    MediaTooLargeError,
    TranscodeError,
    DownloadTimeoutError,
]


class TestLocaleOf:
    @pytest.mark.parametrize(
        "code,expected",
        [
            ("ru", "ru"),
            ("ru-RU", "ru"),
            ("RU", "ru"),
            ("en", "en"),
            ("en-US", "en"),
            ("en-GB", "en"),
            ("de", "en"),  # unsupported -> default
            ("fr-FR", "en"),
            ("", "en"),
            (None, "en"),
        ],
    )
    def test_resolution(self, code, expected):
        assert i18n.locale_of(code) == expected


class TestTranslate:
    def test_returns_locale_specific_text(self):
        assert i18n.t("generic_error", "en") == "❌ Something went wrong."
        assert i18n.t("generic_error", "ru") == "❌ Что-то пошло не так."
        # The two locales must actually differ (catches copy-paste mistakes).
        assert i18n.t("generic_error", "en") != i18n.t("generic_error", "ru")

    def test_defaults_to_english(self):
        assert i18n.t("help") == i18n.t("help", "en")

    def test_unknown_locale_falls_back_to_english(self):
        assert i18n.t("busy_per_user", "de") == i18n.t("busy_per_user", "en")

    def test_unknown_key_returns_key(self):
        assert i18n.t("no.such.key", "en") == "no.such.key"

    def test_placeholder_formatting(self):
        out_en = i18n.t("start", "en", username="mybot")
        out_ru = i18n.t("start", "ru", username="mybot")
        assert "@mybot" in out_en
        assert "@mybot" in out_ru

    def test_missing_placeholder_does_not_raise(self):
        # No username kwarg supplied: returns the template unformatted rather than raising.
        assert "{username}" in i18n.t("start", "en")


class TestCatalogCompleteness:
    def test_every_message_has_both_locales(self):
        for key, variants in i18n._MESSAGES.items():
            assert set(variants) >= {"en", "ru"}, f"{key} is missing a locale"
            assert variants["en"].strip(), f"{key} en is empty"
            assert variants["ru"].strip(), f"{key} ru is empty"

    def test_every_download_error_key_is_in_catalog(self):
        for cls in ALL_ERRORS:
            assert cls.message_key in i18n._MESSAGES, f"{cls.__name__}.message_key missing"

    def test_error_translations_differ_by_locale(self):
        for cls in ALL_ERRORS:
            en = i18n.t(cls.message_key, "en")
            ru = i18n.t(cls.message_key, "ru")
            assert en and ru
            assert en != ru, f"{cls.__name__} has identical en/ru text"

    def test_english_error_translation_matches_user_message(self):
        # The English catalog entry should equal the class's English user_message,
        # so non-bot callers and bot callers see the same English text.
        for cls in ALL_ERRORS:
            assert i18n.t(cls.message_key, "en") == cls.user_message
