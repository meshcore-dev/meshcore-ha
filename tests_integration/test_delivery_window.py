"""Outgoing channel messages collect repeats for as long as the preset needs.

A repeater holds a flood back for up to 2.5x its airtime, so long messages on
slow presets used to be counted as "0 Repeaters" at a fixed four seconds (#366).
"""

import pytest

from custom_components.meshcore.logbook import _collection_passes, _repeats_observable
from custom_components.meshcore.utils import lora_airtime

EU_NARROW = {"radio_sf": 8, "radio_bw": 62.5, "radio_cr": 8}
US_RECOMMENDED = {"radio_sf": 7, "radio_bw": 62.5, "radio_cr": 5}


@pytest.mark.parametrize(
    ("payload", "sf", "bw", "cr", "seconds"),
    [(30, 7, 62.5, 5, 0.160), (102, 8, 62.5, 8, 0.968), (50, 12, 125.0, 5, 2.564)],
)
def test_lora_airtime(payload: int, sf: int, bw: float, cr: int, seconds: float) -> None:
    assert lora_airtime(payload, sf, bw, cr) == pytest.approx(seconds, abs=0.001)


def test_long_message_on_a_slow_preset_waits_past_four_seconds() -> None:
    assert _collection_passes(EU_NARROW, "Gorsag", "x" * 80) == 6


def test_short_or_fast_messages_keep_the_four_second_window() -> None:
    assert _collection_passes(EU_NARROW, "Gorsag", "Test") == 4
    assert _collection_passes(US_RECOMMENDED, "PonyBot", "x" * 140) == 4


def test_the_window_is_capped() -> None:
    slow = {"radio_sf": 12, "radio_bw": 125.0, "radio_cr": 5}
    assert _collection_passes(slow, "node", "x" * 120) == 20


@pytest.mark.parametrize(
    "radio", [{}, {"radio_sf": None, "radio_bw": 62.5, "radio_cr": 5}, {**EU_NARROW, "radio_bw": 0}]
)
def test_unknown_radio_settings_keep_four_passes(radio: dict) -> None:
    assert _collection_passes(radio, "node", "x" * 80) == 4


# The companion only pushes a received packet that fits one serial frame, so the
# repeat of a long channel message never reaches HA (#367).


def test_a_short_message_can_be_confirmed() -> None:
    assert _repeats_observable("Hub", "Hello") is True


def test_the_frame_limit_boundary() -> None:
    # "Gorsag: " + 131 bytes encrypts to 144; one more byte pads to 160 and,
    # with the largest relay overhead, no longer fits a 172-byte frame.
    assert _repeats_observable("Gorsag", "x" * 131) is True
    assert _repeats_observable("Gorsag", "x" * 132) is False


def test_the_limit_counts_utf8_bytes() -> None:
    assert _repeats_observable("Gorsag", "ł" * 66) is False
