"""Tests for consistent Current Price resolution across output types (issue #117).

Covers:
- PricingManager.get_price_or_none — distinguishes "no data" (None) from a real zero/negative price.
- helpers.resolve_current_price — the single shared resolver used by every output type, so that
  outputs on the same pricing basis always agree, and the basis is reported for the web app.
"""

import datetime as dt
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from helpers import resolve_current_price
from local_enumerations import AmberChannel
from org_enums import RunPlanMode
from pricing import PricingManager

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

_AT = dt.datetime(2026, 11, 6, 12, 0, 0)


def _slot(start: dt.datetime, price: float, minutes: int = 30) -> dict:
    """Build a raw price-data slot like PricingManager stores internally."""
    end = start + dt.timedelta(minutes=minutes)
    return {"StartDateTime": start, "EndDateTime": end, "Price": price}


def _make_pricing_with_price(config, logger, price, channel=AmberChannel.GENERAL) -> PricingManager:
    """Return a PricingManager whose GENERAL channel has a single slot covering _AT."""
    pm = PricingManager(config, logger)
    slot_start = _AT.replace(minute=0)
    pm.raw_price_data = [{"Name": channel, "PriceData": [_slot(slot_start, price)]}]
    # today_forecast_data only needs the channel name present for _is_channel_valid().
    pm.today_forecast_data = [{"Name": channel, "PriceData": [], "SortedPriceData": []}]
    return pm


class _FakePricing:
    """Minimal stand-in exposing just get_price_or_none."""

    def __init__(self, price: float | None) -> None:
        self._price = price

    def get_price_or_none(self, as_at_time: dt.datetime, channel_id: AmberChannel) -> float | None:  # noqa: ARG002
        return self._price


class _FakeScheduler:
    """Minimal stand-in exposing get_price and default_price."""

    def __init__(self, schedule_price: float, default_price: float) -> None:
        self._schedule_price = schedule_price
        self.default_price = default_price

    def get_price(self, schedule: dict, as_at_time: dt.datetime) -> float:  # noqa: ARG002
        return self._schedule_price


# ---------------------------------------------------------------------------
# PricingManager.get_price_or_none
# ---------------------------------------------------------------------------

class TestGetPriceOrNone:
    def test_positive_price_returned(self, config, logger):
        pm = _make_pricing_with_price(config, logger, 13.9)
        assert pm.get_price_or_none(_AT, AmberChannel.GENERAL) == pytest.approx(13.9)

    def test_zero_price_returned_not_none(self, config, logger):
        pm = _make_pricing_with_price(config, logger, 0.0)
        assert pm.get_price_or_none(_AT, AmberChannel.GENERAL) == 0.0

    def test_negative_price_returned(self, config, logger):
        pm = _make_pricing_with_price(config, logger, -3.0)
        assert pm.get_price_or_none(_AT, AmberChannel.GENERAL) == pytest.approx(-3.0)

    def test_no_slot_for_time_returns_none(self, config, logger):
        pm = _make_pricing_with_price(config, logger, 13.9)
        # A time outside the single slot's window.
        assert pm.get_price_or_none(_AT + dt.timedelta(hours=6), AmberChannel.GENERAL) is None

    def test_no_data_returns_none(self, config, logger):
        pm = PricingManager(config, logger)
        pm.raw_price_data = []
        pm.today_forecast_data = []
        assert pm.get_price_or_none(_AT, AmberChannel.GENERAL) is None

    def test_invalid_channel_returns_none(self, config, logger):
        pm = _make_pricing_with_price(config, logger, 13.9, channel=AmberChannel.GENERAL)
        # CONTROLLED_LOAD is absent from today_forecast_data, so it is invalid here.
        assert pm.get_price_or_none(_AT, AmberChannel.CONTROLLED_LOAD) is None


# ---------------------------------------------------------------------------
# helpers.resolve_current_price
# ---------------------------------------------------------------------------

class TestResolveCurrentPrice:
    def _resolve(self, pricing, scheduler, *, mode, schedule=None, schedule_name=None):
        return resolve_current_price(
            pricing=pricing,
            scheduler=scheduler,
            device_mode=mode,
            amber_channel=AmberChannel.GENERAL,
            schedule=schedule,
            schedule_name=schedule_name,
            as_at_time=_AT,
        )

    def test_best_price_uses_amber(self, logger):  # noqa: ARG002
        price, basis = self._resolve(
            _FakePricing(13.9), _FakeScheduler(15.0, 32.0), mode=RunPlanMode.BEST_PRICE,
        )
        assert price == pytest.approx(13.9)
        assert basis == "Amber general"

    def test_best_price_keeps_zero(self, logger):  # noqa: ARG002
        price, basis = self._resolve(
            _FakePricing(0.0), _FakeScheduler(15.0, 32.0), mode=RunPlanMode.BEST_PRICE,
        )
        assert price == 0.0
        assert basis == "Amber general"

    def test_best_price_keeps_negative(self, logger):  # noqa: ARG002
        price, basis = self._resolve(
            _FakePricing(-3.0), _FakeScheduler(15.0, 32.0), mode=RunPlanMode.BEST_PRICE,
        )
        assert price == pytest.approx(-3.0)
        assert basis == "Amber general"

    def test_best_price_no_amber_falls_to_schedule(self, logger):  # noqa: ARG002
        price, basis = self._resolve(
            _FakePricing(None), _FakeScheduler(15.0, 32.0),
            mode=RunPlanMode.BEST_PRICE, schedule={"Name": "Pool Solar"}, schedule_name="Pool Solar",
        )
        assert price == pytest.approx(15.0)
        assert basis == "Pool Solar schedule"

    def test_best_price_no_amber_no_schedule_falls_to_default(self, logger):  # noqa: ARG002
        price, basis = self._resolve(
            _FakePricing(None), _FakeScheduler(15.0, 32.0), mode=RunPlanMode.BEST_PRICE,
        )
        assert price == pytest.approx(32.0)
        assert basis == "Default price"

    def test_schedule_mode_uses_schedule(self, logger):  # noqa: ARG002
        price, basis = self._resolve(
            _FakePricing(13.9), _FakeScheduler(15.0, 32.0),
            mode=RunPlanMode.SCHEDULE, schedule={"Name": "Pool Solar"}, schedule_name="Pool Solar",
        )
        assert price == pytest.approx(15.0)
        assert basis == "Pool Solar schedule"

    def test_same_basis_outputs_agree(self, logger):  # noqa: ARG002
        """The core of issue #117: two outputs on the same basis must resolve identically."""
        pricing = _FakePricing(13.9)
        scheduler = _FakeScheduler(15.0, 32.0)
        result_a = self._resolve(pricing, scheduler, mode=RunPlanMode.BEST_PRICE)
        result_b = self._resolve(pricing, scheduler, mode=RunPlanMode.BEST_PRICE)
        assert result_a == result_b == (pytest.approx(13.9), "Amber general")
