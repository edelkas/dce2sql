"""Value conversion and enum resolution."""

from __future__ import annotations

import pytest

from dce2sql import enums
from dce2sql.util import (
    color,
    discriminator,
    snowflake,
    snowflake_timestamp,
    timestamp,
)


class TestSnowflake:
    def test_parses_the_string_form_exports_use(self):
        assert snowflake("197765375503368192") == 197765375503368192

    @pytest.mark.parametrize("value", [None, "", "   ", "not-an-id"])
    def test_rejects_rubbish(self, value):
        assert snowflake(value) is None


class TestDiscriminator:
    def test_keeps_the_leading_zeros_meaning(self):
        # '0042' is 42, not octal and not a string
        assert discriminator("0042") == 42

    def test_the_retired_value_is_zero_not_none(self):
        assert discriminator("0000") == 0


class TestColor:
    def test_packs_a_hex_string(self):
        assert color("#F1C40F") == (0xF1 << 16) | (0xC4 << 8) | 0x0F

    def test_null_is_the_default_colour_not_black(self):
        assert color(None) is None
        assert color("#000000") == 0


class TestTimestamp:
    def test_normalizes_an_offset_to_utc(self):
        # The same instant written from two timezones has to land on one number, or a
        # re-import from another machine would look like an edit
        assert timestamp("2025-01-01T01:00:00+01:00") == timestamp(
            "2025-01-01T00:00:00+00:00"
        )

    def test_accepts_dotnet_seven_digit_fractions(self):
        # Utf8JsonWriter emits up to 7 fractional digits; Python accepts 6
        assert timestamp("2026-09-19T15:49:03.7580259+02:00") is not None

    def test_accepts_a_trailing_z(self):
        assert timestamp("2025-01-01T00:00:00Z") == timestamp(
            "2025-01-01T00:00:00+00:00"
        )

    def test_rejects_rubbish(self):
        assert timestamp("yesterday") is None


class TestSnowflakeTimestamp:
    """Recovering a creation date from an ID, which is the only place Discord keeps one."""

    def test_decodes_a_known_id(self):
        # Ground truth: this is what Discord itself reports for the account, and the value
        # DCE now writes as 'createdAt' for it
        assert snowflake_timestamp("66155023779758080") == timestamp(
            "2015-07-02T13:16:26.770+00:00"
        )

    def test_agrees_with_the_field_it_stands_in_for(self):
        # The whole point of the fallback: an export that states the date and one that does
        # not must land on the same number, or an archive would disagree with itself about
        # when an account was made depending on which export happened to mention it
        for value, stated in [
            ("57595500404146176", "2015-06-08T22:23:57.369+00:00"),
            # Deliberately not UTC: 'createdAt' carries the exporting machine's offset unless
            # --utc was passed, so the two only agree once that has been normalized away
            ("197765375503368192", "2016-06-29T19:28:59.823+02:00"),
            ("1282519086105886753", "2024-09-08T18:52:58.180-07:00"),
        ]:
            assert snowflake_timestamp(value) == timestamp(stated), value

    def test_the_epoch_is_discords_own(self):
        # The smallest usable snowflake lands on 2015-01-01T00:00:00Z, not on the Unix epoch
        assert snowflake_timestamp(1 << 22) == timestamp("2015-01-01T00:00:00Z")

    def test_accepts_the_integer_form_too(self):
        # Callers hand it an already-parsed ID, not the string from the JSON
        assert snowflake_timestamp(66155023779758080) == snowflake_timestamp(
            "66155023779758080"
        )

    @pytest.mark.parametrize("value", [None, "", "   ", "not-an-id", 0, -1])
    def test_rejects_what_is_not_an_id(self, value):
        # 0 would otherwise decode to Discord's epoch and read as a real date
        assert snowflake_timestamp(value) is None


class TestEnums:
    def test_maps_a_dce_name_to_the_official_value(self):
        assert enums.resolve_channel_type("GuildTextChat") == 0
        assert enums.resolve_message_type("PollResult") == 46
        assert enums.resolve_reference_type("Forward") == 1

    def test_accepts_the_bare_number_dce_falls_back_to(self):
        # DCE casts the raw int, so a value its enum doesn't name round-trips as a number
        assert enums.resolve_message_type("23") == 23
        assert enums.resolve_channel_type("16") == 16

    def test_an_unknown_name_resolves_to_none(self):
        assert enums.resolve_message_type("SomethingDiscordAddedLater") is None

    def test_a_missing_reference_type_defaults_to_zero(self):
        # Exports predating forwards carry no type on the reference at all
        assert enums.resolve_reference_type(None) == 0

    def test_seed_tables_cover_every_value_dce_can_write(self):
        assert set(enums.DCE_CHANNEL_KINDS.values()) <= set(enums.CHANNEL_TYPES)
        assert set(enums.DCE_MESSAGE_KINDS.values()) <= set(enums.MESSAGE_TYPES)
        assert set(enums.DCE_REFERENCE_KINDS.values()) <= set(enums.REFERENCE_TYPES)

    def test_sticker_formats_are_uppercased(self):
        assert enums.resolve_sticker_format("Lottie") == "LOTTIE"
        assert enums.resolve_sticker_format(None) is None
