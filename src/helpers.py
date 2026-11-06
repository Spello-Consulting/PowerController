"""General helper functions"""
import csv
import datetime as dt
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from org_enums import RunPlanMode
from sc_foundation import DateHelper, SCConfigManager

from local_enumerations import (
    DEFAULT_CURRENCY_SYMBOL,
    DEFAULT_CURRENCY_SUBUNIT_SYMBOL,
    AmberChannel,
)

if TYPE_CHECKING:
    from pricing import PricingManager
    from scheduler import Scheduler


def resolve_current_price(
    *,
    pricing: "PricingManager",
    scheduler: "Scheduler",
    device_mode: RunPlanMode,
    amber_channel: AmberChannel,
    schedule: dict[str, Any] | None,
    schedule_name: str | None,
    as_at_time: dt.datetime | None = None,
) -> tuple[float, str]:
    """Resolve an output's price and the basis it comes from, for display and costing.

    This is the single source of truth for an output's "current price" so that every
    output type (smart device, meter, TeslaMate) resolves the price identically. A
    genuine zero or negative Amber price is kept as-is; the schedule/default fallback is
    used only when Amber price data is actually unavailable for the channel at that time.

    Args:
        pricing (PricingManager): The pricing manager used for Amber prices.
        scheduler (Scheduler): The scheduler used for schedule prices and the default price.
        device_mode (RunPlanMode): The output's pricing mode (BEST_PRICE or SCHEDULE).
        amber_channel (AmberChannel): The Amber channel used when in BEST_PRICE mode.
        schedule (dict | None): The output's resolved schedule, if any.
        schedule_name (str | None): The name of the output's schedule, used in the label.
        as_at_time (dt.datetime | None): The time to price for. Defaults to now.

    Returns:
        tuple[float, str]: The price (may be zero or negative) and a human-readable basis
            label (e.g. "Amber general", "Pool Solar schedule", "Default price").
    """
    at_time = as_at_time or DateHelper.now()

    if device_mode == RunPlanMode.BEST_PRICE:
        amber_price = pricing.get_price_or_none(as_at_time=at_time, channel_id=amber_channel)
        if amber_price is not None:
            return float(amber_price), f"Amber {amber_channel}"

    if schedule is not None:
        return float(scheduler.get_price(schedule, at_time)), f"{schedule_name} schedule"

    return float(scheduler.default_price), "Default price"


def get_currency_symbols(config: SCConfigManager) -> tuple[str, str]:
    """Get the major and minor currency symbols from the config file or failing that use the defaults.

    Args:
        config (SCConfigManager): The configuration manager for the system.

    Returns:
        tuple[str, str]: A tuple containing the major and minor currency symbols.
    """
    major = config.get("General", "CurrencySymbol", default=DEFAULT_CURRENCY_SYMBOL) or DEFAULT_CURRENCY_SYMBOL
    minor = config.get("General", "CurrencySubunitSymbol", default=DEFAULT_CURRENCY_SUBUNIT_SYMBOL) or DEFAULT_CURRENCY_SUBUNIT_SYMBOL

    return major, minor  # pyright: ignore[reportReturnType]


def get_location_coordinates(config: SCConfigManager) -> tuple[float | None, float | None]:
    """Resolve the configured latitude and longitude from the Location config section.

    Coordinates are taken from the GoogleMapsURL if present, otherwise from the explicit
    Latitude/Longitude config values. The Shelly-device location source (used by the
    scheduler for dawn/dusk) is not consulted here as it requires runtime device data.

    Args:
        config (SCConfigManager): The configuration manager for the system.

    Returns:
        tuple[float | None, float | None]: The (latitude, longitude) pair, or (None, None) if unavailable.
    """
    loc_conf = config.get("Location", default={}) or {}
    if not isinstance(loc_conf, dict):
        return None, None

    google_maps_url = loc_conf.get("GoogleMapsURL")
    if google_maps_url:
        match = re.search(r"@?([-]?\d+\.\d+),([-]?\d+\.\d+)", google_maps_url)
        if match:
            return float(match.group(1)), float(match.group(2))

    lat = loc_conf.get("Latitude")
    lon = loc_conf.get("Longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)

    return None, None


class DebugSupport:
    @staticmethod
    def dump_list_to_csv(file_name: str, list_obj: list[dict]) -> None:
        """
        Dump a list object to a CSV file for debugging.

        Args:
            file_name (str): The name of the CSV file to create.
            list_obj(list[dict]): The data to dump.
        """
        file_path = Path(file_name)

        # If no data, delete the existing file if there
        if not list_obj:
            if file_path.exists():
                file_path.unlink()
            return

        with file_path.open("w", newline="", encoding="utf-8") as csvfile:
            fieldnames = list_obj[0].keys()
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for item in list_obj:
                writer.writerow(item)
