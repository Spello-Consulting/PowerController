"""The pricing module that manages the interface to Amber and determines when to run based on the best pricing strategy."""
import datetime as dt
import operator
import os
from pathlib import Path

# from zoneinfo import ZoneInfo
import requests
from org_enums import RunPlanMode
from sc_foundation import (
    CSVReader,
    DateHelper,
    JSONEncoder,
    SCCommon,
    SCConfigManager,
    SCLogger,
)

from config_schemas import ConfigSchema
from local_enumerations import (
    PRICE_SLOT_INTERVAL,
    PRICES_DATA_FILE,
    USAGE_AGGREGATION_INTERVAL,
    AmberAPIMode,
    AmberChannel,
    PriceFetchMode,
)
from run_plan import RunPlanner


class PricingManager:
    """Manages the pricing data from Amber and determines when to run based on the best pricing strategy."""
    # Public Functions ============================================================================
    def __init__(self, config: SCConfigManager, logger: SCLogger):
        """Initializes the PricingManager.

        Args:
            config (SCConfigManager): The configuration manager for the system.
            logger (SCLogger): The logger for the system.
        """
        self.config = config
        self.logger = logger
        self.next_refresh = DateHelper.now()
        self.usage_data: list = []  # Usage data

        # Amber specific information
        self.concurrent_error_count = 0
        self.api_error_count = 0
        self.site_id = None
        self.raw_price_data = []   # The raw pricing data retrieved from Amber
        self.today_forecast_data = []       # The processed pricing data

        self.initialise()
        self.logger.log_message("Pricing manager initialised.", "debug")

    def initialise(self):
        """(re) initialise the pricing manager."""
        # Re-read the configuration settings
        if self.config.get("AmberAPI") is None:
            self.logger.log_message("Amber API configuration section is missing, disabling Amber pricing.", "debug")
            self.mode = AmberAPIMode.DISABLED
            return

        self.mode = self.config.get("AmberAPI", "Mode", default=AmberAPIMode.LIVE)
        self.timeout = self.config.get("AmberAPI", "Timeout", default=10)
        self.refresh_interval = self.config.get("AmberAPI", "RefreshInterval", default=5) or 5
        assert isinstance(self.refresh_interval, (int, float))

        self.base_url = self.config.get("AmberAPI", "APIURL")
        self.api_key = os.environ.get("AMBER_API_KEY")
        if not self.api_key:
            self.api_key = self.config.get("AmberAPI", "APIKey")
        if not self.base_url or not self.api_key:
            if self.mode != AmberAPIMode.DISABLED:
                self.logger.log_message("Amber API is not properly configured, disabling Amber pricing.", "error")
            self.mode = AmberAPIMode.DISABLED
            return
        self.report_critical_errors_delay = self.config.get("General", "ReportCriticalErrorsDelay", default=None)
        if isinstance(self.report_critical_errors_delay, (int, float)):
            self.report_critical_errors_delay = round(self.report_critical_errors_delay, 0)
        else:
            self.report_critical_errors_delay = None

        # Save the usage data
        self._save_usage_data()

        # If the price cache file exists, read from it rather than live prices to save time
        _, mod_time = self._get_price_cache_file_info()
        if self.mode == AmberAPIMode.LIVE and mod_time is not None and (DateHelper.now() - mod_time).total_seconds() < (self.refresh_interval * 60):
            self._refresh_price_data(load_from_file=True)
            self.next_refresh = DateHelper.add_datetime(mod_time, minutes=self.refresh_interval)
        else:
            self._refresh_price_data()

    def refresh_price_data_if_time(self, is_new_day: bool) -> bool:
        """Refresh the pricing data if the refresh interval has passed.

        Args:
            is_new_day (bool): Indicates if it's a new day.

        Returns:
            result(bool): True if the refresh was successful or AmberPricing disabled, False if there was an error.
        """
        time_now = DateHelper.now()
        if time_now >= self.next_refresh or is_new_day:
            self._refresh_price_data()
            self._save_usage_data()
            return True
        return False

    def get_current_price(self, channel_id: AmberChannel = AmberChannel.GENERAL) -> float:
        """Fetches the current price from the Amber API.

        Args:
            channel_id (AmberChannel): The ID of the channel to get the price for.

        Returns:
            price(float): The current price in AUD/kWh, or 0 if channel is invalid or price data is not available.
        """
        if not self._is_channel_valid(channel_id):
            self.logger.log_message(f"Invalid channel ID '{channel_id}' specified when checking price data duration.", "error")
            return 0.0

        price_data = self._get_channel_forecast_prices(channel_id)
        if not price_data:
            return 0.0

        return price_data[0]["Price"]

    def get_price(self, as_at_time: dt.datetime, channel_id: AmberChannel = AmberChannel.GENERAL) -> float:
        """Fetches the price for the specified time from the Amber API.

        Args:
            as_at_time (dt.datetime): The datetime to get the price for.
            channel_id (AmberChannel): The ID of the channel to get the price for.

        Returns:
            price(float): The price in AUD/kWh at the specified time, or 0 if channel is invalid or price data is not available.
        """
        if not self._is_channel_valid(channel_id):
            self.logger.log_message(f"Invalid channel ID '{channel_id}' specified when checking price data duration.", "error")
            return 0.0

        assert isinstance(self.raw_price_data, list)
        raw_data = next((channel.get("PriceData", []) for channel in self.raw_price_data if channel.get("Name") == channel_id), [])
        if not raw_data:
            return 0.0

        # Search raw_data for the entry matching as_at_time
        entry = next((e for e in raw_data if isinstance(e.get("StartDateTime"), dt.datetime) and isinstance(e.get("EndDateTime"), dt.datetime) and e["StartDateTime"] <= as_at_time < e["EndDateTime"]), None)
        if not entry:
            return 0.0

        return entry["Price"] or 0.0

    def get_run_plan(self,
                     required_hours: float,
                     priority_hours: float,
                     max_price: float,
                     max_priority_price: float,
                     channel_id: AmberChannel = AmberChannel.GENERAL,
                     hourly_energy_usage: float = 0.0,
                     slot_min_minutes: int = 0,
                     slot_min_gap_minutes: int = 0,
                     constraint_slots: list[dict] | None = None) -> dict | None:
        """Determines when to run based on the best pricing strategy.

        Args:
            required_hours (float): The number of hours required for the task. Set to -1 to get all remaining hours that can be filled by price.
            priority_hours (float): The number of hours that should be prioritized.
            max_price (float): The maximum price to consider for the run plan.
            max_priority_price (float): The maximum price to consider for priority hours in the run plan.
            channel_id (str | None): The ID of the channel to use for pricing.
            hourly_energy_usage (float): The average hourly energy usage in Wh. Used to estimate cost of the run plan.
            slot_min_minutes (int): The minimum length of each time slot in minutes.
            slot_min_gap_minutes (int): The minimum gap between time slots in minutes.
            constraint_slots (list[dict]): A list of constraint slots to consider when calculating the run plan.

        Returns:
            plan (list[dict]): A list of dictionaries containing the run plan.
        """
        if self.mode == AmberAPIMode.DISABLED:
            return None

        try:
            # Create a run planner instance
            run_planner = RunPlanner(self.logger, RunPlanMode.BEST_PRICE, channel_id)

            sorted_price_data = self._get_channel_forecast_prices(channel_id=channel_id, which_type=PriceFetchMode.SORTED)
            self.logger.log_message(f"Calculating best price run plan for {required_hours:.2f} hours ({priority_hours:.2f} priority) on channel {channel_id} with max prices {max_price} / {max_priority_price}.", "debug")
            run_plan = run_planner.calculate_run_plan(sorted_price_data, required_hours, priority_hours, max_price, max_priority_price, hourly_energy_usage, slot_min_minutes, slot_min_gap_minutes, constraint_slots)
        except RuntimeError as e:
            self.logger.log_message(f"Error occurred while calculating best price run plan: {e}", "error")
            return None
        else:
            return run_plan

    def get_daily_usage_totals(self):
        """Gets the total energy usage for each day available.

        Note: Energy usage is returned in kWh.

        Returns:
            daily_totals (list): A list of daily totals, or an empty list if no data is available.
        """
        # First scan self.usage_data and make sure we have entries on or before the start_date and on or after the end_date
        if not self.usage_data:
            return []

        # Now aggregate the usage data for each date
        current_date: dt.date | None = None
        day_entry: dict | None = None
        daily_totals: list[dict] = []

        for entry in self.usage_data:
            entry_date = entry.get("Date")
            if not isinstance(entry_date, dt.date):
                continue

            if current_date is None:
                current_date = entry_date
                day_entry = {
                    "Date": current_date,
                    "EnergyUsed": 0.0,
                    "Cost": 0.0,
                }
            elif entry_date != current_date:
                # Date changed: append the prior day and start a new one.
                if day_entry is not None:
                    daily_totals.append(day_entry)
                current_date = entry_date
                day_entry = {
                    "Date": current_date,
                    "EnergyUsed": 0.0,
                    "Cost": 0.0,
                }

            assert day_entry is not None
            day_entry["EnergyUsed"] += entry.get("Usage", 0.0) or 0.0
            day_entry["Cost"] += entry.get("Cost", 0.0) or 0.0

        # Append the final day entry
        if day_entry is not None:
            daily_totals.append(day_entry)

        return daily_totals

    def get_prices_for_data_api(self, channel_id: AmberChannel = AmberChannel.GENERAL, interval_time: int = 30, number_of_intervals: int = 12, price_warning: float | None = None, price_critical: float | None = None) -> list[dict]:
        """Gets the current and forecasted price data for selected channel.

        Args:
            channel_id (AmberChannel): The ID of the channel to get the price data for.
            interval_time (int): The interval time in minutes for each price slot (e.g. 30 for half-hourly prices, 60 for hourly prices).
            number_of_intervals (int): The number of future intervals to include in the data.
            price_warning (float | None): An optional price threshold for warning level. If provided and a slot's price exceeds this threshold, then the status key will be set to "Warning" for that slot.
            price_critical (float | None): An optional price threshold for critical level. If provided and a slot's price exceeds this threshold, then the status key will be set to "Critical" for that slot.

        Returns:
            list[dict]: A list of dictionaries containing the price data for the next 24 hours. Each dictionary contains the following keys:
                - StartDateTime (datetime): The start datetime of the price slot.
                - EndDateTime (datetime): The end datetime of the price slot.
                - Price (float): The price in AUD/kWh for that slot.
                - Status (str): "OK", "Warning", or "Critical" based on the price thresholds provided.
                - Type (str): "Current" for the current slot, "Forecast" for future slots.
        """
        # Get raw price data for the specified channel
        assert isinstance(self.raw_price_data, list)
        raw_data = next((channel.get("PriceData", []) for channel in self.raw_price_data if channel.get("Name") == channel_id), [])
        if not raw_data:
            return []

        now = DateHelper.now()
        current_slot_start = PricingManager._find_current_slot_start(raw_data, now)
        if current_slot_start is None:
            return []

        # Generate intervals starting from the current slot
        result = []
        interval_start = current_slot_start

        for i in range(number_of_intervals):
            if i == 0:
                # First slot starts at current slot start and ends at the next interval boundary
                # (or full interval if already on a boundary).
                midnight = interval_start.replace(hour=0, minute=0, second=0, microsecond=0)
                minutes_since_midnight = int((interval_start - midnight).total_seconds() // 60)
                remainder = minutes_since_midnight % interval_time

                if remainder == 0 and interval_start.second == 0 and interval_start.microsecond == 0:
                    interval_end = DateHelper.add_datetime(interval_start, minutes=interval_time)
                else:
                    minutes_to_boundary = interval_time - remainder
                    interval_end = DateHelper.add_datetime(interval_start, minutes=minutes_to_boundary)
            else:
                # Subsequent slots always start on interval boundaries.
                interval_end = DateHelper.add_datetime(interval_start, minutes=interval_time)

            overlapping_entries = PricingManager._find_overlapping_entries(raw_data, interval_start, interval_end)

            if not overlapping_entries:
                continue

            avg_price = PricingManager._calculate_weighted_average_price(overlapping_entries, interval_start, interval_end)
            status = PricingManager._determine_status(avg_price, price_warning, price_critical)
            slot_type = "Current" if i == 0 else "Forecast"
            minutes = int((interval_end - interval_start).total_seconds() / 60)

            result.append({
                "StartDateTime": interval_start,
                "EndDateTime": interval_end,
                "Minutes": minutes,
                "Price": round(avg_price, 2),
                "Status": status,
                "Type": slot_type,
            })

            interval_start = interval_end

        return result

    @staticmethod
    def _find_current_slot_start(raw_data: list[dict], now: dt.datetime) -> dt.datetime | None:
        """Find the start time of the current or next available price slot.

        Args:
            raw_data: List of raw price data entries.
            now: Current datetime.

        Returns:
            Start datetime of the current/next slot, or None if not found.
        """
        # Find the current time slot
        for entry in raw_data:
            if isinstance(entry.get("StartDateTime"), dt.datetime) and isinstance(entry.get("EndDateTime"), dt.datetime) and entry["StartDateTime"] <= now < entry["EndDateTime"]:
                return entry["StartDateTime"]

        # If no current slot, use the first future slot
        future_slots = [e for e in raw_data if isinstance(e.get("StartDateTime"), dt.datetime) and e["StartDateTime"] > now]
        return future_slots[0]["StartDateTime"] if future_slots else None

    @staticmethod
    def _find_overlapping_entries(raw_data: list[dict], interval_start: dt.datetime, interval_end: dt.datetime) -> list[dict]:
        """Find all raw data entries that overlap with the specified interval.

        Args:
            raw_data: List of raw price data entries.
            interval_start: Start of the interval.
            interval_end: End of the interval.

        Returns:
            List of overlapping entries.
        """
        overlapping = []
        for entry in raw_data:
            if not isinstance(entry.get("StartDateTime"), dt.datetime) or not isinstance(entry.get("EndDateTime"), dt.datetime):
                continue
            entry_start = entry["StartDateTime"]
            entry_end = entry["EndDateTime"]

            # Check if there's any overlap
            if entry_start < interval_end and entry_end > interval_start:
                overlapping.append(entry)

        return overlapping

    @staticmethod
    def _calculate_weighted_average_price(overlapping_entries: list[dict], interval_start: dt.datetime, interval_end: dt.datetime) -> float:
        """Calculate weighted average price based on overlap duration.

        Args:
            overlapping_entries: List of entries that overlap with the interval.
            interval_start: Start of the interval.
            interval_end: End of the interval.

        Returns:
            Weighted average price.
        """
        total_overlap_minutes = 0.0
        weighted_price_sum = 0.0

        for entry in overlapping_entries:
            entry_start = entry["StartDateTime"]
            entry_end = entry["EndDateTime"]

            # Calculate the overlap period
            overlap_start = max(interval_start, entry_start)
            overlap_end = min(interval_end, entry_end)
            overlap_minutes = (overlap_end - overlap_start).total_seconds() / 60.0

            if overlap_minutes > 0:
                entry_price = entry.get("Price", 0.0) or 0.0
                weighted_price_sum += entry_price * overlap_minutes
                total_overlap_minutes += overlap_minutes

        return weighted_price_sum / total_overlap_minutes if total_overlap_minutes > 0 else 0.0

    @staticmethod
    def _determine_status(price: float, price_warning: float | None, price_critical: float | None) -> str:
        """Determine status based on price thresholds.

        Args:
            price: The price to check.
            price_warning: Warning threshold.
            price_critical: Critical threshold.

        Returns:
            Status string: "OK", "Warning", or "Critical".
        """
        if price_critical is not None and price >= price_critical:
            return "Critical"
        if price_warning is not None and price >= price_warning:
            return "Warning"
        return "OK"

    # Private Functions ===========================================================================
    @staticmethod
    def _convert_utc_dt_string(utc_time_str: str) -> dt.datetime:
        """
        Converts a UTC datetime string (e.g. '2025-09-26T16:25:01Z') to a local datetime object.

        Args:
            utc_time_str (str): The UTC datetime string in ISO format.

        Returns:
            dt.datetime: The corresponding local datetime object.
        """
        # Parse the UTC string to a datetime object
        utc_dt = DateHelper.extract_datetime(utc_time_str, format_str="%Y-%m-%dT%H:%M:%SZ")
        utc_dt = utc_dt.replace(tzinfo=dt.UTC)

        # Convert to local timezone
        local_dt = DateHelper.convert_timezone(utc_dt)
        local_dt = local_dt.replace(second=0, microsecond=0)
        return local_dt

    def _refresh_price_data(self, load_from_file: bool = False) -> bool:
        """Refreshes the pricing data from Amber.

        Args:
            load_from_file (bool): If True, load the pricing data from the local cache file instead of Amber.

        Returns:
            result(bool): True if the refresh was successful or AmberPricing disabled, False if there was an error.
        """
        if self.mode == AmberAPIMode.DISABLED:
            return True

        if not self._get_amber_prices(load_from_file):
            return False
        assert isinstance(self.raw_price_data, list)

        self.logger.log_message("Starting refresh of Amber pricing", "debug")

        # Now build the self.today_forecast_data list into 5 minute increments for today
        today = DateHelper.today()
        time_now = DateHelper.now()
        # Round down to the nearest 5 minutes
        rounded_minute = time_now.minute - (time_now.minute % PRICE_SLOT_INTERVAL)
        first_start_time = time_now.replace(minute=rounded_minute, second=0, microsecond=0)
        self.today_forecast_data.clear()
        for channel in self.raw_price_data:

            channel_data = {
                "Name": channel["Name"],
                "PriceData": []
            }

            for entry in channel["PriceData"]:
                start_time: dt.datetime = entry["StartDateTime"]
                end_time: dt.datetime = entry["EndDateTime"]
                if end_time >= first_start_time and start_time.date() == today:
                    while start_time < end_time and start_time.date() == today:
                        if start_time >= first_start_time:
                            slot_end_time = DateHelper.add_datetime(start_time, minutes=PRICE_SLOT_INTERVAL)
                            channel_data["PriceData"].append({
                                "Date": start_time.date(),
                                "StartTime": start_time.time(),
                                "StartDateTime": start_time,
                                "EndTime": slot_end_time.time(),
                                "EndDateTime": slot_end_time,
                                "Minutes": PRICE_SLOT_INTERVAL,
                                "Price": entry["Price"]
                            })
                        start_time = DateHelper.add_datetime(start_time, minutes=PRICE_SLOT_INTERVAL)

            self.today_forecast_data.append(channel_data)

        # Finally create a best price sorted version of each channel's data
        for channel in self.today_forecast_data:
            channel["SortedPriceData"] = sorted(channel["PriceData"], key=operator.itemgetter("Price"))

        return True

    def _amber_authenticate(self) -> bool:
        """Login to Amber and get the site ID.

        Returns:
            result (bool): True if the site ID was retrieved, False if Amber unreachable.
        """
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        try:
            url = self.base_url + "/sites"  # type: ignore[attr-defined]

            response = requests.get(f"{url}", headers=headers, timeout=self.timeout)  # type: ignore[attr-defined]
            response.raise_for_status()
            sites = response.json()
            for site in sites:
                if site.get("status") == "active":
                    self.api_error_count = 0
                    self.site_id = site.get("id")
                    self.concurrent_error_count = 0  # reset the error count
                    return True

            self.logger.log_fatal_error("No active Amber sites found.")

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:  # Trap connection and timeout errors
            self.logger.log_message(f"Connection error or timeout while authenticating to Amber: {e}", "warning")
            self.concurrent_error_count += 1
            return False

        except requests.exceptions.RequestException as e:
            self.logger.log_message(f"Error fetching Amber site ID: {e}", "error")
            self.concurrent_error_count += 1
            return False
        else:
            return False

        return True

    def _get_amber_prices(self, load_from_file: bool = False) -> bool:  # noqa: PLR0914, PLR0915
        """Retrieves the current raw pricing data from Amber.

        Returns:
            result(bool): True if the refresh was successful or AmberPricing disabled, False if there was an error.
        """
        connection_error = False
        max_errors = 10
        time_now = DateHelper.now()
        date_today = DateHelper.today()
        # If Amber pricing is disabled, nothing to do
        if self.mode == AmberAPIMode.DISABLED:
            self.next_refresh = DateHelper.add_datetime(time_now, minutes=self.refresh_interval)
            return True
        if self.mode == AmberAPIMode.LIVE and not load_from_file:
            # Maximum number of API query errors before we send an email notification
            max_errors = self.config.get("AmberAPI", "MaxConcurrentErrors", default=10)

            # By default, our next refresh is 5 mins from now
            self.next_refresh = DateHelper.add_datetime(time_now, minutes=self.refresh_interval)

            # Authenticate to Amber
            assert isinstance(self.raw_price_data, list)
            while True:
                if not self._amber_authenticate():
                    connection_error = True
                    break
                # We authenticated to Amber, so go get the default pricing data
                self.raw_price_data.clear()  # Clear the list

                # Download the 30 min data for the prior 35 days and future 2 days
                result = self._download_raw_amber_data(interval_window=30, future_intervals=100, prior_intervals=1800)
                if not result:
                    connection_error = True
                    break
                channel_list, raw_data = result

                # Remove any records that are more than 35 days old or more than 2 days in the future
                oldest_date = DateHelper.add_date(date_today, days=-35)
                newest_date = DateHelper.add_date(date_today, days=2)
                price_data_30min = [entry for entry in raw_data if (entry.get("Date") >= oldest_date and entry.get("Date") <= newest_date)]  # pyright: ignore[reportOptionalOperand, reportAttributeAccessIssue]

                # Download the 5 min data for the prior 35 days and today
                result = self._download_raw_amber_data(interval_window=5, future_intervals=36, prior_intervals=1800)
                if not result:
                    connection_error = True
                    break
                _, raw_data = result

                # Remove any records that are more than 5 days old and any that aren't 5 min slots
                oldest_date = DateHelper.add_date(date_today, days=-5)
                price_data_5min = [entry for entry in raw_data if (entry.get("Date") >= oldest_date and entry.get("Minutes") == 5)]  # pyright: ignore[reportOptionalOperand, reportAttributeAccessIssue]

                # Consolidate the two data sets
                for channel in channel_list:
                    channel_data = {
                        "Name": channel,
                        "PriceData": [],
                    }

                    channel_30min = [e for e in price_data_30min if e.get("Channel") == channel]
                    channel_5min = [e for e in price_data_5min if e.get("Channel") == channel]

                    merged = self._merge_price_data_5min_into_30min(price_data_30min=channel_30min, price_data_5min=channel_5min)
                    for entry in merged:
                        entry.pop("Channel", None)
                    channel_data["PriceData"] = merged

                    self.raw_price_data.append(channel_data)

                # And finally save the lot to file
                self._save_prices()
                self.next_refresh = DateHelper.add_datetime(time_now, minutes=self.refresh_interval)
                self.logger.log_message(f"Refreshed Amber pricing. Next refresh at {self.next_refresh.strftime('%H:%M:%S')}", "debug")
                break

        # If we get here but there was a connection error along the way
        if connection_error:
            if max_errors and self.concurrent_error_count >= max_errors and self.report_critical_errors_delay:  # pyright: ignore[reportOperatorIssue]
                assert isinstance(self.report_critical_errors_delay, int)
                self.logger.report_notifiable_issue(entity="Amber API", issue_type="Connection Error", send_delay=self.report_critical_errors_delay * 60, message=f"API is still not responding after {max_errors} connection attempts.")
            self.next_refresh = DateHelper.add_datetime(time_now, minutes=1)  # Shorten the refresh interval if we previously errored
            self.logger.log_message(f"Amber unavailable, reverting to cached prices. Next attempt at {self.next_refresh.strftime('%H:%M:%S')}", "warning")
        else:
            self.logger.clear_notifiable_issue(entity="Amber API", issue_type="Connection Error")

        if connection_error or self.mode == AmberAPIMode.OFFLINE or load_from_file:
            # If we had an error but still within limits, revert to default pricing
            self.next_refresh = DateHelper.add_datetime(time_now, minutes=1)  # Shorten the refresh interval if we previously errored
            self._import_prices()

        return True

    @staticmethod
    def _rewindow_price_record(entry: dict, start_dt: dt.datetime, end_dt: dt.datetime) -> dict | None:
        """Return a copy of an Amber price record adjusted to a new time window.

        Args:
            entry: Existing record dict.
            start_dt: New StartDateTime.
            end_dt: New EndDateTime.

        Returns:
            A new record dict, or None if the window is <= 0 minutes.
        """
        minutes = int((end_dt - start_dt).total_seconds() / 60)
        if minutes <= 0:
            return None

        new_entry = dict(entry)
        new_entry["Date"] = start_dt.date()
        new_entry["StartTime"] = start_dt.time()
        new_entry["StartDateTime"] = start_dt
        new_entry["EndTime"] = end_dt.time()
        new_entry["EndDateTime"] = end_dt
        new_entry["Minutes"] = minutes
        return new_entry

    @classmethod
    def _merge_price_data_5min_into_30min(cls, *, price_data_30min: list[dict], price_data_5min: list[dict]) -> list[dict]:
        """Merge 5-min records into 30-min records, splitting 30-min windows as needed.

        Any 5-min record replaces overlapping portions of 30-min records. If a 5-min
        record falls in the middle of a 30-min record, the 30-min record is split into
        non-overlapping fragments around the 5-min window.

        Args:
            price_data_30min: List of 30-min records for a single channel.
            price_data_5min: List of 5-min records for the same channel.

        Returns:
            A merged list of records (may include mixed durations), sorted by StartDateTime.
        """
        segments: list[dict] = [dict(e) for e in price_data_30min]
        segments.sort(key=operator.itemgetter("StartDateTime", "EndDateTime"))

        five_sorted = [dict(e) for e in price_data_5min]
        five_sorted.sort(key=operator.itemgetter("StartDateTime", "EndDateTime"))

        for five in five_sorted:
            five_start = five.get("StartDateTime")
            five_end = five.get("EndDateTime")
            if not isinstance(five_start, dt.datetime) or not isinstance(five_end, dt.datetime):
                continue

            # Ensure minutes matches the actual window for safety.
            rew_five = cls._rewindow_price_record(five, five_start, five_end)
            if rew_five is None:
                continue

            new_segments: list[dict] = []
            for seg in segments:
                seg_start = seg.get("StartDateTime")
                seg_end = seg.get("EndDateTime")
                if not isinstance(seg_start, dt.datetime) or not isinstance(seg_end, dt.datetime):
                    continue

                # No overlap.
                if seg_end <= five_start or seg_start >= five_end:
                    new_segments.append(seg)
                    continue

                # Overlap: split into up to two fragments outside the 5-min window.
                before_end = min(seg_end, five_start)
                after_start = max(seg_start, five_end)

                if seg_start < before_end:
                    before = cls._rewindow_price_record(seg, seg_start, before_end)
                    if before is not None:
                        new_segments.append(before)

                if after_start < seg_end:
                    after = cls._rewindow_price_record(seg, after_start, seg_end)
                    if after is not None:
                        new_segments.append(after)

            new_segments.append(rew_five)
            segments = new_segments

        segments.sort(key=operator.itemgetter("StartDateTime", "EndDateTime"))
        return segments

    def _download_raw_amber_data(self, interval_window: int, future_intervals: int = 0, prior_intervals: int = 0) -> tuple[list, list[dict]] | None:
        """Gets the raw pricing data from Amber for a given number of intervals.

        Cleans up the raw data provided by Amber and returns the processed data.

        Args:
            interval_window (int): The interval window in minutes (5 or 30).
            prior_intervals (int): The number of prior intervals to fetch.
            future_intervals (int): The number of future intervals to fetch.

        Returns:
            channel_list (list[str]): The list of channels to fetch data for.
            price_data (list[dict]): The requested pricing data, or None if there was an issue
        """
        if not self.site_id:
            self.logger.log_fatal_error("Functional called before Amber authentication.", report_stack=True)
        if interval_window not in {5, 30} or prior_intervals < 0 or future_intervals < 0:
            self.logger.log_fatal_error("Invalid parameters.", report_stack=True)
        if prior_intervals + future_intervals > 2048:
            self.logger.log_fatal_error("Requested intervals exceed maximum of 2048.", report_stack=True)
        if prior_intervals + future_intervals == 0:
            self.logger.log_fatal_error("Total intervals requested is 0.", report_stack=True)

        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        url = f"{self.base_url}/sites/{self.site_id}/prices/current?next={future_intervals}&previous={prior_intervals}&resolution={interval_window}"

        try:
            response = requests.get(url, headers=headers, timeout=self.timeout)  # type: ignore[attr-defined]
            response.raise_for_status()
            response_data = response.json()

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:  # Trap connection and timeout errors
            self.logger.log_message(f"Connection error or timeout while getting Amber price data: {e}", "warning")
            self.concurrent_error_count += 1
            return None

        except requests.exceptions.RequestException as e:
            self.logger.log_message(f"Error fetching Amber prices: {e}", "error")
            self.concurrent_error_count += 1
            return None

        self.concurrent_error_count = 0  # reset the error count
        # Extract just the key/value pairs we care about
        price_data = []
        channel_list = []
        for entry in response_data:
            dt_start = self._convert_utc_dt_string(entry["startTime"])
            dt_end = self._convert_utc_dt_string(entry["endTime"])
            new_entry = {
                "Date": dt_start.date(),
                "Channel": entry["channelType"],
                "StartTime": dt_start.time(),
                "StartDateTime": dt_start,
                "EndTime": dt_end.time(),
                "EndDateTime": dt_end,
                "Minutes": int(entry["duration"]),  # Duration of this slot in minutes
                "Price": float(entry["perKwh"]),
                "IsForecast": entry.get("type") == "ForecastInterval",
            }
            price_data.append(new_entry)
            if entry["channelType"] not in channel_list:
                channel_list.append(entry["channelType"])

        return channel_list, price_data

    def _get_price_cache_file_info(self) -> tuple[Path, dt.datetime | None]:
        """Returns the path and last modified time of the pricing cache file.

        Returns:
            tuple(Path, dt.datetime) | None: The path to the pricing cache file and its last modified time, or None if not found.
        """
        file_name = self.config.get("AmberAPI", "PricesCacheFile", default=PRICES_DATA_FILE) or PRICES_DATA_FILE
        file_path = SCCommon.select_file_location(file_name)  # pyright: ignore[reportArgumentType]
        assert isinstance(file_path, Path)
        if not file_path.exists():
            return file_path, None
        try:
            mod_time = DateHelper.get_file_datetime(file_path)
        except OSError as e:
            self.logger.log_message(f"Error getting pricing cache file info: {e}", "error")
            return file_path, None
        else:
            return file_path, mod_time

    def _save_prices(self) -> bool:
        """Saves the raw pricing data to disk.

        Returns:
            result (bool): True if the pricing data was saved, False if not.
        """
        file_path, _ = self._get_price_cache_file_info()
        try:
            return JSONEncoder.save_to_file(self.raw_price_data, file_path)
        except RuntimeError as e:
            self.logger.log_message(f"Error saving raw price data file {file_path}: {e}", "error")
            return False

    def _import_prices(self) -> bool:
        """Loads the default pricing data from disk if available.

        Returns:
            result (bool): True if the pricing data was loaded, False if not.
        """
        file_path, _ = self._get_price_cache_file_info()
        assert isinstance(self.raw_price_data, list)
        if not file_path.exists():
            return False
        self.raw_price_data.clear()

        def is_date_only(x):
            return isinstance(x, dt.date) and not isinstance(x, dt.datetime)

        try:
            self.raw_price_data = JSONEncoder.read_from_file(file_path)
            assert isinstance(self.raw_price_data, list)
            # Make sure the StartDateTime and EndDateTime keys are actual dt.datetime objects
            for channel in self.raw_price_data:
                for entry in channel["PriceData"]:
                    if is_date_only(entry["StartDateTime"]):
                        entry["StartDateTime"] = DateHelper.combine(entry["StartDateTime"], dt.time.min)
                    if is_date_only(entry["EndDateTime"]):
                        entry["EndDateTime"] = DateHelper.combine(entry["EndDateTime"], dt.time.min)
        except RuntimeError as e:
            self.logger.log_message(f"Error importing raw price data file {file_path}: {e}", "error")
            return False
        else:
            return True

    def _download_amber_usage_data(self, start_date: dt.date) -> list[dict]:
        """Downloads the Amber usage data for the past 7 days up to yesterday.

        Args:
            start_date (dt.date): The date to start downloading usage data from. Must be no more than 7 days ago.

        Returns:
            usage_data (list[dict]): The usage data retrieved from Amber, or an empty list if there was an error.
        """
        connection_error = False
        response_data = []
        end_date = DateHelper.today()

        # Validate the start date
        if (end_date - start_date).days > 7:
            self.logger.log_message("Start date for Amber usage data download must be no more than 7 days ago.", "error")
            start_date = DateHelper.add_date(end_date, days=-7)

        # Only attempt to download usage data if in LIVE mode
        if self.mode == AmberAPIMode.LIVE:
            # Authenticate to Amber
            while True:
                if not self._amber_authenticate():
                    connection_error = True
                    break

                # We authenticated to Amber, so go get the usage data for the last 7 days starting from today
                headers = {
                    "accept": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                }
                start_date_str = start_date.strftime("%Y-%m-%d")
                end_date_str = end_date.strftime("%Y-%m-%d")
                url = f"{self.base_url}/sites/{self.site_id}/usage?startDate={start_date_str}&endDate={end_date_str}"

                try:
                    response = requests.get(url, headers=headers, timeout=self.timeout)  # type: ignore[attr-defined]
                    response.raise_for_status()
                    response_data = response.json()

                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:  # Trap connection and timeout errors
                    self.logger.log_message(f"Connection error or timeout while getting Amber usage data: {e}", "warning")
                    self.concurrent_error_count += 1
                    return []

                except requests.exceptions.RequestException as e:
                    self.logger.log_message(f"Error fetching Amber usage data: {e}", "error")
                    self.concurrent_error_count += 1
                    return []
                else:
                    self.concurrent_error_count = 0  # reset the error count
                    self.logger.log_message("Downloaded latest Amber usage data.", "debug")
                    break

        # Save response_data to a JSON file for debugging
        # debug_file_path = SCCommon.select_file_location("amber_usage_debug.json")
        # try:
        #     JSONEncoder.save_to_file(response_data, debug_file_path)
        #     self.logger.log_message(f"Saved Amber usage response data to {debug_file_path}", "debug")
        # except RuntimeError as e:
        #     self.logger.log_message(f"Error saving debug usage data: {e}", "warning")

        # Note: If there was a connection error along the way we'll let the download prices function handle it
        usage_data = []
        if not connection_error:
            self.logger.clear_notifiable_issue(entity="Amber API", issue_type="Connection Error")

            # Build a return list[dict] in a format suitable for CSV writing
            for entry in response_data:
                entry_date = DateHelper.extract_date(entry["date"], "%Y-%m-%d")  # pyright: ignore[reportArgumentType]
                dt_start = self._convert_utc_dt_string(entry["startTime"])
                if entry_date == end_date:
                    continue    # Skip records for today
                dt_end = self._convert_utc_dt_string(entry["endTime"])
                new_entry = {
                    "Date": entry_date,
                    "Channel": entry["channelType"],
                    "StartDateTime": dt_start,
                    "EndDateTime": dt_end,
                    "Minutes": int(entry["duration"]),  # Duration of this slot in minutes
                    "Usage": float(entry["kwh"]),
                    "Price": float(entry["perKwh"]),
                    "Cost": float(entry["cost"]) / 100.0,  # Convert from cents to AUD
                }
                usage_data.append(new_entry)

        return usage_data

    def _save_usage_data(self) -> bool:  # noqa: PLR0914, PLR0915
        """Saves the raw usage data a CSV file, appending and truncating as needed.

        Implements https://github.com/Spello-Consulting/PowerController/issues/11

        Note: Energy usage is saved in kWh.

        Returns:
            result (bool): True if the usage data was saved, False if not.
        """
        file_name = self.config.get("AmberAPI", "UsageDataFile")
        if not file_name or self.mode == AmberAPIMode.DISABLED: # Issue 99
            return False    # No file configured, nothing to do
        
        file_path = SCCommon.select_file_location(file_name)  # pyright: ignore[reportArgumentType]
        if not file_path:
            self.logger.log_message(f"No valid path for Amber usage data file {file_name}.", "error")
            return False
        max_history_days = self.config.get("AmberAPI", "UsageMaxDays", default=30) or 0
        assert isinstance(max_history_days, int)
        # Check for -1 meaning unlimited
        max_history_days = None if max_history_days == -1 else max_history_days
        today = DateHelper.today()

        # Create a CSVreader to read the existing data
        csv_reader = None
        try:
            schemas = ConfigSchema()
            csv_reader = CSVReader(file_path, schemas.amber_usage_csv_config)
            csv_data = csv_reader.read_csv()
            if not csv_data:
                csv_data = []
        except (ImportError, TypeError, ValueError) as e:
            self.logger.log_message(f"Error initializing CSVReader in _save_usage_data(): {e}", "error")
            return False
        else:
            assert isinstance(csv_reader, CSVReader)

        # If there are any records older than max_history_days or any records for today, remove them
        if max_history_days is not None:
            cutoff_date = DateHelper.add_date(today, days=-max_history_days)
            csv_data = [row for row in csv_data if row["Date"] > cutoff_date]

        # Now determine the most recent date in the existing data
        existing_dates = {row["Date"] for row in csv_data}
        last_date = max(existing_dates) if existing_dates else None

        # Set the start to be last_date or 6 days prior to today, which ever is later
        start_date = DateHelper.add_date(today, days=-6)
        if last_date and last_date >= start_date:
            start_date = DateHelper.add_date(last_date, days=1)
        start_date = min(start_date, today)

        # Call _download_amber_usage_data and append any new data
        new_usage_data = self._download_amber_usage_data(start_date)
        csv_data.extend(new_usage_data)

        # Aggregate any 5 min data into hourly data
        aggregated_data = []
        i = 0
        while i < len(csv_data):
            row = csv_data[i]
            row_date = row["Date"]
            duration_minutes = int(row["Minutes"])

            if duration_minutes < USAGE_AGGREGATION_INTERVAL:
                # Parse the start time to get the hour
                start_time = row["StartDateTime"]
                end_time = row["EndDateTime"]
                current_hour = start_time.hour

                # Aggregate all rows for this hour
                total_usage = row["Usage"]
                total_cost = row["Cost"]
                total_minutes = duration_minutes

                # Look ahead to find more rows in the same hour
                j = i + 1
                while j < len(csv_data):
                    next_row = csv_data[j]
                    next_date = next_row["Date"]
                    next_start_time = next_row["StartDateTime"]

                    # Stop if we've moved to a different date, channel, or hour
                    if (next_date != row_date or
                        next_row["Channel"] != row["Channel"] or
                        next_start_time.hour != current_hour):
                        break

                    # Add this row's data to our aggregate
                    total_usage += next_row["Usage"]
                    total_cost += next_row["Cost"]
                    total_minutes += int(next_row["Minutes"])
                    end_time = next_row["EndDateTime"]
                    j += 1

                # Create aggregated entry
                new_entry = {
                    "Date": row["Date"],
                    "Channel": row["Channel"],
                    "StartDateTime": start_time,
                    "EndDateTime": end_time,
                    "Minutes": total_minutes,
                    "Usage": total_usage,
                    "Price": total_cost / total_usage * 100 if total_usage > 0 else 0,
                    "Cost": total_cost,
                }
                aggregated_data.append(new_entry)

                # Skip all the rows we just processed
                i = j
            else:
                # No aggregation needed for today or yesterday
                aggregated_data.append(row)
                i += 1

        if not aggregated_data:
            return True  # No data to save

        # Write the updated CSV data back to file, overwriting the existing file
        try:
            aggregated_data = csv_reader.sort_csv_data(aggregated_data)
            csv_reader.write_csv(aggregated_data)
        except (ValueError) as e:
            self.logger.log_message(f"Error writing usage data file {file_path}: {e}", "error")
            return False
        else:
            self.usage_data = aggregated_data   # Save the data
            return True

    def _get_channel_forecast_prices(self, channel_id: AmberChannel = AmberChannel.GENERAL, which_type: PriceFetchMode = PriceFetchMode.NORMAL) -> list[dict]:
        """Returns the list of prices for the specified channel.

        Args:
            channel_id (AmberChannel): The ID of the channel to get the prices for.
            which_type (PriceFetchMode): The type of prices to get (normal or sorted).

        Returns:
            prices (list[float]): A list of prices in AUD/kWh for the specified channel, or an empty list if invalid.
        """
        if not self._is_channel_valid(channel_id):
            self.logger.log_message(f"Invalid channel ID '{channel_id}' specified when getting channel prices.", "error")
            return []
        if which_type not in PriceFetchMode:
            self.logger.log_message(f"Invalid price type '{which_type}' specified when getting channel prices.", "error")
            return []

        for channel in self.today_forecast_data:
            if channel["Name"] == channel_id:
                if which_type == PriceFetchMode.SORTED:
                    return channel["SortedPriceData"]
                return channel["PriceData"]
        return []

    def _is_channel_valid(self, channel_id: AmberChannel) -> bool:
        """Checks if the specified channel ID is valid.

        Args:
            channel_id (AmberChannel): The ID of the channel to check.

        Returns:
            is_valid (bool): True if the channel ID is valid, False otherwise.
        """
        if channel_id is None:
            return False
        return any(channel["Name"] == channel_id for channel in self.today_forecast_data)
