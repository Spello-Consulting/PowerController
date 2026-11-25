"""Configuration schemas for use with the SCConfigManager class."""


class ConfigSchema:
    """Base class for configuration schemas."""

    def __init__(self):
        self.placeholders = {
            "DeviceType": {
                "WebsiteAccessKey": "<Your website API key here>",
            },
            "AmberAPI": {
                "APIKey": "<Your API Key Here>",
            },
            "Email": {
                "SMTPUsername": "<Your SMTP username here>",
                "SMTPPassword": "<Your SMTP password here>",
            }
        }

        self.validation = {
            "Files": {
                "type": "dict",
                "required": True,
                "schema": {
                    "SavedStateFile": {"type": "string", "required": True},
                },
            },
            "SCSmartDevices": {
                "type": "dict",
                "required": True,
                "schema": {
                    "MaxConcurrentErrors": {"type": "number", "required": False, "nullable": True, "min": 0},
                    "Devices": {
                        "schema": {
                            "schema": {
                                "DeviceAlertTemp": {"type": "number", "required": False, "nullable": True, "min": 20.0, "max": 100.0},
                            },
                        },
                    },
                },
            },
            "General": {
                "type": "dict",
                "required": True,
                "schema": {
                    "Label": {"type": "string", "required": False, "nullable": True},
                    "PollingInterval": {"type": "number", "required": False, "nullable": True, "min": 10, "max": 600},
                    "TempProbeAveragingWindow": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 86400},
                    "ReportCriticalErrorsDelay": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 3600},
                    "PrintToConsole": {"type": "boolean", "required": False, "nullable": True},
                    "DefaultPrice": {"type": "number", "required": False, "nullable": True, "min": 10, "max": 1000},
                    "TestingMode": {"type": "boolean", "required": False, "nullable": True},
                    "CurrencySymbol": {"type": "string", "required": False, "nullable": True},
                    "SubunitSymbol": {"type": "string", "required": False, "nullable": True},
                },
            },
            "Website": {
                "type": "dict",
                "required": False,
                "schema": {
                    "HostingIP": {"type": "string", "required": False, "nullable": True},
                    "Port": {"type": "number", "required": False, "nullable": True, "min": 80, "max": 65535},
                    "PageAutoRefresh": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 3600},
                    "DebugMode": {"type": "boolean", "required": False, "nullable": True},
                    "AccessKey": {"type": "string", "required": False, "nullable": True},
                },
            },
            "OutputMetering": {
                "type": "dict",
                "required": False,
                "schema": {
                    "Enable": {"type": "boolean", "required": False, "nullable": True},
                    "DataFile": {"type": "string", "required": True, "nullable": False},
                    "DataFileMaxDays": {"type": "number", "required": False, "nullable": True, "min": -1, "max": 365},
                    "OutputsToLog": {
                        "type": "list",
                        "required": True,
                        "nullable": False,
                        "schema": {
                            "type": "dict",
                            "schema": {
                                "Output": {"type": "string", "required": True},
                                "DisplayName": {"type": "string", "required": False, "nullable": True},
                                "HideFromViewerApp": {"type": "boolean", "required": False, "nullable": True},
                            },
                        },
                    },
                },
            },
            "AmberAPI": {
                "type": "dict",
                "schema": {
                    "Mode": {"type": "string", "required": False, "nullable": True, "allowed": ["Live", "Offline", "Disabled"]},
                    "APIURL": {"type": "string", "required": False, "nullable": True},
                    "APIKey": {"type": "string", "required": False, "nullable": True},
                    "Timeout": {"type": "number", "required": False, "nullable": True, "min": 5, "max": 60},
                    "MaxConcurrentErrors": {"type": "number", "required": False, "nullable": True, "min": 0},
                    "RefreshInterval": {"type": "number", "required": False, "nullable": True, "min": 5, "max": 30},
                    "UsageDataFile": {"type": "string", "required": False, "nullable": True},
                    "UsageMaxDays": {"type": "number", "required": False, "nullable": True, "min": -1, "max": 365},
                    "PricesCacheFile": {"type": "string", "required": False, "nullable": True},
                },
            },
            "Location": {
                "type": "dict",
                "required": False,
                "nullable": True,
                "schema": {
                    "GoogleMapsURL": {"type": "string", "required": False, "nullable": True},
                    "Timezone": {"type": "string", "required": False, "nullable": True},
                    "Latitude": {"type": "number", "required": False, "nullable": True},
                    "Longitude": {"type": "number", "required": False, "nullable": True},
                },
            },
            "OperatingSchedules": {
                "type": "list",
                "required": True,
                "nullable": False,
                "schema": {
                    "type": "dict",
                    "schema": {
                        "Name": {"type": "string", "required": True},
                        "UsePowerTariff": {"type": "boolean", "required": False, "nullable": True},
                        "Windows": {
                            "type": "list",
                            "required": True,
                            "schema": {
                                "type": "dict",
                                "schema": {
                                    "StartTime": {"type": "string", "required": True},
                                    "EndTime": {"type": "string", "required": True},
                                    "Price": {"type": "number", "required": False, "nullable": True},
                                    "DaysOfWeek": {"type": "string", "required": False, "nullable": True},
                                },
                            },
                        },
                    },
                },
            },

            "PowerTariff": {
                "type": "list",
                "required": False,
                "nullable": True,
                "schema": {
                    "type": "dict",
                    "schema": {
                        "Name": {"type": "string", "required": True},
                        "StartTime": {"type": "string", "required": True},
                        "EndTime": {"type": "string", "required": True},
                        "Price": {"type": "number", "required": False, "nullable": True},
                        "DaysOfWeek": {"type": "string", "required": False, "nullable": True},
                    },
                },
            },            
            "Outputs": {
                "type": "list",
                "required": True,
                "nullable": False,
                "schema": {
                    "type": "dict",
                    "schema": {
                        "Name": {"type": "string", "required": True},
                        "Type": {"type": "string", "required": False, "nullable": True, "allowed": ["smart_device", "teslamate", "meter"]},
                        "CarID": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 1000000},
                        "DeviceOutput": {"type": "string", "required": False, "nullable": True},
                        "Mode": {"type": "string", "required": False, "nullable": True, "allowed": ["Schedule", "BestPrice"]},
                        "Disable": {"type": "boolean", "required": False, "nullable": True},
                        "Schedule": {"type": "string", "required": False, "nullable": True},
                        "ConstraintSchedule": {"type": "string", "required": False, "nullable": True},
                        "AmberChannel": {"type": "string", "required": False, "nullable": True, "allowed": ["general", "controlledLoad"]},
                        "DaysOfHistory": {"type": "number", "required": False, "nullable": True, "min": 2, "max": 60},
                        "TargetHours": {"type": "number", "required": False, "nullable": True, "min": -1, "max": 24},
                        "MonthlyTargetHours": {"type": "dict", "required": False, "nullable": True},
                        "MinHours": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 20},
                        "MaxHours": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 24},
                        "MinHoursAlert": {"type": "boolean", "required": False, "nullable": True},
                        "MaxShortfallHours": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 24},
                        "MaxBestPrice": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 1000},
                        "MaxPriorityPrice": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 1000},
                        "DatesOff": {
                            "type": "list",
                            "required": False,
                            "nullable": True,
                            "schema": {
                                "type": "dict",
                                "schema": {
                                    "StartDate": {"type": "date", "required": False, "nullable": True},
                                    "EndDate": {"type": "date", "required": False, "nullable": True},
                                },
                            },
                        },
                        "DeviceMeter": {"type": "string", "required": False, "nullable": True},
                        "PowerOnThresholdWatts": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 20000},
                        "PowerOffThresholdWatts": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 20000},
                        "MinEnergyToLog": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 10000},
                        "MaxDailyEnergyUse": {"type": "number", "required": False, "nullable": True, "min": 100, "max": 50000},
                        "MinDailyEnergyUse": {"type": "number", "required": False, "nullable": True, "min": 10, "max": 50000},
                        "DeviceInput": {"type": "string", "required": False, "nullable": True},
                        "DeviceInputMode": {"type": "string", "required": False, "nullable": True, "allowed": ["Ignore", "TurnOn", "TurnOff"]},
                        "ParentOutput": {"type": "string", "required": False, "nullable": True},
                        "StopOnExit": {"type": "boolean", "required": False, "nullable": True},
                        "MinOnTime": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 1200},
                        "MinOffTime": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 1200},
                        "MaxOffTime": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 1200},
                        "MaxAppOnTime": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 2880},
                        "MaxAppOffTime": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 2880},
                        "TurnOnSequence": {"type": "string", "required": False, "nullable": True},
                        "TurnOffSequence": {"type": "string", "required": False, "nullable": True},
                        "HideFromWebApp": {"type": "boolean", "required": False, "nullable": True},
                        "HideFromViewerApp": {"type": "boolean", "required": False, "nullable": True},
                        "HideFromTempProbesSummary": {"type": "boolean", "required": False, "nullable": True},
                        "TempProbeConstraints": {
                            "type": "list",
                            "required": False,
                            "nullable": True,
                            "schema": {
                                "type": "dict",
                                "schema": {
                                    "TempProbe": {"type": "string", "required": True},
                                    "Condition": {"type": "string", "required": True, "allowed": ["GreaterThan", "LessThan"]},
                                    "Temperature": {"type": "number", "required": True, "min": -50.0, "max": 150.0},
                                    "FallBackTemp": {"type": "number", "required": False, "nullable": True, "min": -50.0, "max": 150.0},
                                },
                            },
                        },
                        "UPSIntegration": {
                            "type": "dict",
                            "required": False,
                            "nullable": True,
                            "schema": {
                                "UPS": {"type": "string", "required": True},
                                "ActionIfUnhealthy": {"type": "string", "required": True, "allowed": ["TurnOn", "TurnOff"]},
                            },
                        },
                        "WeatherConstraint": {
                            "type": "dict",
                            "required": False,
                            "nullable": True,
                            "schema": {
                                "SkyCondition": {"type": "string", "required": False, "nullable": True},
                                "TemperatureBelow": {"type": "number", "required": False, "nullable": True, "min": -50.0, "max": 100.0},
                                "TemperatureAbove": {"type": "number", "required": False, "nullable": True, "min": -50.0, "max": 100.0},
                                "PrecipitationProbabilityBelow": {"type": "number", "required": False, "nullable": True, "min": 0.0, "max": 1.0},
                                "PrecipitationProbabilityAbove": {"type": "number", "required": False, "nullable": True, "min": 0.0, "max": 1.0},
                                "ActionIfMatch": {"type": "string", "required": True, "allowed": ["TurnOn", "TurnOff"]},
                            },
                        },
                    },
                },
            },
            "OutputSequences": {
                "type": "list",
                "required": False,
                "nullable": True,
                "schema": {
                    "type": "dict",
                    "schema": {
                        "Name": {"type": "string", "required": True},
                        "Description": {"type": "string", "required": False, "nullable": True},
                        "Timeout": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 3600},
                        "Steps": {
                            "type": "list",
                            "required": True,
                            "schema": {
                                "type": "dict",
                                "schema": {
                                    "Type": {"type": "string", "required": True},
                                    "Seconds": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 3600},
                                    "OutputIdentity": {"type": "string", "required": False, "nullable": True},
                                    "DeviceIdentity": {"type": "string", "required": False, "nullable": True},
                                    "State": {"type": "boolean", "required": False, "nullable": True},
                                    "Retries": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 10},
                                    "RetryBackoff": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 60},
                                },
                            },
                        },
                    },
                },
            },

            "WeatherClient": {
                "type": "dict",
                "required": False,
                "schema": {
                    "RefreshIntervalMin": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 1440},
                    "PreferredProvider": {"type": "string", "required": False, "nullable": True, "allowed": ["OpenWeatherMap", "open_meteo"]},
                    "OWMAPIKey": {"type": "string", "required": False, "nullable": True},
                },
            },
            "TempProbeLogging": {
                "type": "dict",
                "required": False,
                "schema": {
                    "Enable": {"type": "boolean", "required": False, "nullable": True},
                    "Probes": {
                        "type": "list",
                        "required": True,
                        "nullable": False,
                        "schema": {
                            "type": "dict",
                            "schema": {
                                "Name": {"type": "string", "required": True},
                                "DisplayName": {"type": "string", "required": False, "nullable": True},
                                "Colour": {"type": "string", "required": False, "nullable": True},
                                "HideFromViewerApp": {"type": "boolean", "required": False, "nullable": True},
                            },
                        },
                    },
                    "LoggingInterval": {"type": "number", "required": True, "min": 1, "max": 1440},
                    "LastReadingWithinMinutes": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 7200},
                    "SavedStateFileMaxDays": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 14},
                    "HistoryDataFile": {"type": "string", "required": False, "nullable": True},
                    "HistoryDataFileMaxDays": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 365},
                    "Charting": {
                        "type": "dict",
                        "schema": {
                            "Enable": {"type": "boolean", "required": False, "nullable": True},
                            "Charts": {
                                "type": "list",
                                "required": True,
                                "nullable": False,
                                "schema": {
                                    "type": "dict",
                                    "schema": {
                                        "Name": {"type": "string", "required": True},
                                        "DaysToShow": {"type": "number", "required": True, "min": 1, "max": 30},
                                        "Probes": {
                                            "type": "list",
                                            "required": True,
                                            "nullable": False,
                                            "schema": {
                                                "type": "string",
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "ViewerWebsite": {
                "type": "dict",
                "required": False,
                "schema": {
                    "Enable": {"type": "boolean", "required": False, "nullable": True},
                    "Label": {"type": "string", "required": False, "nullable": True},
                    "BaseURL": {"type": "string", "required": False, "nullable": True},
                    "AccessKey": {"type": "string", "required": False, "nullable": True},
                    "APITimeout": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 60},
                    "Frequency": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 3600},
                },
            },
            "TeslaMate": {
                "type": "dict",
                "required": False,
                "schema": {
                    "Enable": {"type": "boolean", "required": False, "nullable": True},
                    "RefreshInterval": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 60},
                    "Host": {"type": "string", "required": False, "nullable": True},
                    "Port": {"type": "number", "required": False, "nullable": True},
                    "DatabaseName": {"type": "string", "required": False, "nullable": True},
                    "DBUsername": {"type": "string", "required": False, "nullable": True},
                    "DBPassword": {"type": "string", "required": False, "nullable": True},
                    "GeofenceName": {"type": "string", "required": False, "nullable": True},
                    "SaveRawData": {"type": "boolean", "required": False, "nullable": True},
                },
            },
            "UPSIntegration": {
                "type": "dict",
                "required": False,
                "nullable": True,
                "schema": {
                    "Enable": {"type": "boolean", "required": False, "nullable": True},
                    "PollingInterval": {"type": "number", "required": True, "nullable": True, "min": 10, "max": 3600},
                    "DataFile": {"type": "string", "required": False, "nullable": True},
                    "DataFileWriteInterval": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 3600},
                    "DataFileMaxDays": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 720},
                    "UPSDevices": {
                        "type": "list",
                        "required": True,
                        "nullable": True,
                        "schema": {
                            "type": "dict",
                            "schema": {
                                "Name": {"type": "string", "required": True},
                                "Script": {"type": "string", "required": True},
                                "MinRuntimeWhenDischarging": {"type": "number", "required": False, "nullable": True, "min": 0},
                                "MinChargeWhenDischarging": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 100},
                                "MinRuntimeWhenCharging": {"type": "number", "required": False, "nullable": True, "min": 0},
                                "MinChargeWhenCharging": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 100},
                            },
                        },
                    },
                },
            },
            "DataAPI": {
                "type": "dict",
                "required": False,
                "schema": {
                    "Enable": {"type": "boolean", "required": False, "nullable": True},
                    "HostingIP": {"type": "string", "required": True, "nullable": True},
                    "Port": {"type": "number", "required": True, "nullable": True, "min": 80, "max": 65535},
                    "RefreshInterval": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 3600},
                    "AccessKey": {"type": "string", "required": False, "nullable": True},
                    "TempProbeHistoryDays": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 365},
                    "Outputs": {
                        "type": "list",
                        "required": False,
                        "nullable": True,
                        "schema": {
                            "type": "dict",
                            "schema": {
                                "Name": {"type": "string", "required": True},
                                "DisplayName": {"type": "string", "required": False, "nullable": True},
                            },
                        },
                    },
                    "Meters": {
                        "type": "list",
                        "required": False,
                        "nullable": True,
                        "schema": {
                            "type": "dict",
                            "schema": {
                                "Name": {"type": "string", "required": True},
                                "DisplayName": {"type": "string", "required": False, "nullable": True},
                            },
                        },
                    },
                    "TempProbes": {
                        "type": "list",
                        "required": False,
                        "nullable": True,
                        "schema": {
                            "type": "dict",
                            "schema": {
                                "Name": {"type": "string", "required": True},
                                "DisplayName": {"type": "string", "required": False, "nullable": True},
                            },
                        },
                    },
                    "EnergyPrices": {
                        "type": "dict",
                        "required": False,
                        "nullable": True,
                        "schema": {
                            "AmberChannel": {"type": "string", "required": False, "nullable": True, "allowed": ["general", "controlledLoad"]},
                            "IntervalTime": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 1440},
                            "NumIntervals": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 200},
                            "WarningPrice": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 1000},
                            "CriticalPrice": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 1000},
                        },
                    },
                },
            }
        }

        self.amber_usage_csv_config = [
            {
                "name": "Date",
                "type": "date",
                "format": "%Y-%m-%d",
                "match": True,
                "sort": 1,
            },
            {
                "name": "Channel",
                "type": "str",
                "match": True,
                "sort": 2,
            },
            {
                "name": "StartDateTime",
                "type": "datetime",
                "format": "%Y-%m-%d %H:%M:%S",
            },
            {
                "name": "EndDateTime",
                "type": "datetime",
                "format": "%Y-%m-%d %H:%M:%S",
            },
            {
                "name": "Minutes",
                "type": "int",
            },
            {
                "name": "Usage",
                "type": "float",
            },
            {
                "name": "Price",
                "type": "float",
            },
            {
                "name": "Cost",
                "type": "float",
            },
        ]

        self.temp_probe_history_config = [
            {
                "name": "Timestamp",
                "type": "datetime",
                "format": "%Y-%m-%d %H:%M:%S",
                "match": True,
                "sort": 1,
                "minimum": 365,
            },
            {
                "name": "ProbeName",
                "type": "str",
                "match": True,
                "sort": 2,
            },
            {
                "name": "Temperature",
                "type": "float",
            },
        ]

        self.output_consumption_history_config = [
            {
                "name": "Date",
                "type": "date",
                "format": "%Y-%m-%d",
                "match": True,
                "sort": 1,
            },
            {
                "name": "OutputName",
                "type": "str",
                "match": True,
                "sort": 2,
            },
            {
                "name": "ActualHours",
                "type": "float",
            },
            {
                "name": "TargetHours",
                "type": "float",
            },
            {
                "name": "EnergyUsed",
                "type": "float",
            },
            {
                "name": "TotalCost",
                "type": "float",
            },
            {
                "name": "AveragePrice",
                "type": "float",
            },
        ]

        self.ups_data_file_csv_config = [
            {
                "name": "Timestamp",
                "type": "datetime",
                "format": "%Y-%m-%d %H:%M:%S",
                "minimum": 365,
            },
            {
                "name": "UPSName",
                "type": "str",
            },
            {
                "name": "BatteryChargePercent",
                "type": "float",
            },
            {
                "name": "BatteryRuntimeSeconds",
                "type": "float",
            },
            {
                "name": "BatteryState",
                "type": "str",
            },
            {
                "name": "IsHealthy",
                "type": "bool",
            },
        ]
