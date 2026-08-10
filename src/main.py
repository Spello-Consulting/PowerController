"""Main initialisation module for the PowerController app."""

# Check that Python version is >= 3.13, else exit with error.
import argparse
import os
import signal
import sys
from pathlib import Path
from threading import Event
from mergedeep import merge

from sc_foundation import SCCommon, SCConfigManager, SCLogger, RestartPolicy, ThreadManager

from sc_smart_device import SmartDeviceWorker, SCSmartDevice, smart_devices_validator

from config_schemas import ConfigSchema
from controller import PowerController
from dataapi import create_asgi_app as create_data_api_app
from dataapi import serve_asgi_blocking as serve_data_api_blocking
from local_enumerations import CONFIG_FILE
from webapp import create_asgi_app, serve_asgi_blocking


def parse_command_line_args() -> dict[str, str | None]:
    """Parse and validate command line arguments.

    Returns:
        dict: Dictionary containing parsed arguments with keys:
            - 'config_file': Path to configuration file (always present)
            - 'homedir': Project home directory (for logging purposes, may be None)

    Exits:
        Exits with code 1 if arguments are invalid.
    """
    parser = argparse.ArgumentParser(
        description="PowerController - Intelligent power management system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py
  python main.py --config /path/to/config.yaml
  python main.py --homedir /opt/powercontroller --config config.yaml
        """
    )

    parser.add_argument(
        "--homedir",
        type=str,
        metavar="PATH",
        help="Specify the project home directory",
    )

    parser.add_argument(
        "--config",
        type=str,
        metavar="FILE",
        help=f"Path to configuration file (default: {CONFIG_FILE})",
    )

    args = parser.parse_args()

    # Determine the base directory for resolving relative paths
    if args.homedir:
        homedir = Path(args.homedir)
        if not homedir.exists():
            print(f"ERROR: Specified homedir does not exist: {args.homedir}", file=sys.stderr)
            sys.exit(1)
        if not homedir.is_dir():
            print(f"ERROR: Specified homedir is not a directory: {args.homedir}", file=sys.stderr)
            sys.exit(1)
        base_dir = homedir.resolve()

        # Set the project root environment variable for use by sc-foundation and other components
        os.environ["SC_FOUNDATION_PROJECT_ROOT"] = str(base_dir)
    else:
        base_dir = Path(SCCommon.get_project_root())

    # Determine the config file path
    if args.config:
        config_path = Path(args.config)
        # If relative path, resolve it relative to base_dir
        if not config_path.is_absolute():
            config_path = base_dir / config_path
        config_file = str(config_path.resolve())

        # Validate that the config file exists
        if not Path(config_file).exists():
            print(f"ERROR: Configuration file does not exist: {config_file}", file=sys.stderr)
            sys.exit(1)
        if not Path(config_file).is_file():
            print(f"ERROR: Configuration path is not a file: {config_file}", file=sys.stderr)
            sys.exit(1)
    else:
        config_file = CONFIG_FILE

    return {
        "config_file": config_file,
        "homedir": str(base_dir) if args.homedir else None,
    }


def main():  # noqa: PLR0915
    """Main entry point for the PowerController app."""
    wake_event = Event()    # Wakes the main controller loop from a timed sleep
    stop_event = Event()    # Use to signal the main controller loop that the app is exiting

    if sys.version_info < (3, 13):   # noqa: UP036
        print(f"ERROR: Python 3.13 or higher is required. You are running {sys.version}", file=sys.stderr)
        sys.exit(1)

    # Parse command line arguments
    cmd_args = parse_command_line_args()

    # Install SIGINT handler early
    def handle_sigint(_sig, _frame):
        stop_event.set()
        wake_event.set()
    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    # Get our default schema, validation schema, and placeholders.
    schemas = ConfigSchema()

    # Merge the SmartDevices validation schema with the default validation schema
    merged_schema = merge(schemas.validation, smart_devices_validator)
    assert isinstance(merged_schema, dict), "Merged schema should be type dict"

    # Initialize the SC_ConfigManager class
    try:
        config_file = cmd_args["config_file"]
        assert isinstance(config_file, str), "config_file must be a string"
        config = SCConfigManager(
            config_file=config_file,
            validation_schema=merged_schema,
            placeholders=schemas.placeholders
        )
    except RuntimeError as e:
        print(f"Configuration file error: {e}", file=sys.stderr)
        return
    else:
        assert isinstance(config, SCConfigManager)

    # Initialize the SCLogger class
    try:
        heartbeat_config = config.get("HeartbeatMonitor")
        logger = SCLogger(config.get_logger_settings(), heartbeat_config=heartbeat_config)
        # Setup email
        logger.register_email_settings(config.get_email_settings())
    except (RuntimeError, TypeError, ValueError) as e:
        print(f"Logger initialisation error: {e}", file=sys.stderr)
        return
    else:
        assert isinstance(logger, SCLogger)
        logger.log_message("", "summary")
        logger.log_message("", "summary")
        logger.log_message("PowerController application starting.", "summary")
        if cmd_args["homedir"]:
            logger.log_message(f"Home directory: {cmd_args['homedir']}", "debug")
        logger.log_message(f"Configuration file: {cmd_args['config_file']}", "debug")

    # Initialize the SCSmartDevice class
    smart_switch_settings = config.get("SCSmartDevices")
    if smart_switch_settings is None:
        logger.log_fatal_error("No SmartDevices settings found in the configuration file.")
        return

    try:
        smart_switch_control = SCSmartDevice(logger, smart_switch_settings, wake_event)
    except RuntimeError as e:
        error_msg = f"SCSmartDevice initialization error: {e}"
        raise RuntimeError(error_msg) from e
    logger.log_message(f"SCSmartDevice initialized successfully with {len(smart_switch_control.devices)} devices.", "summary")


    # Now create instances of the main worked classes
    smart_device_worker = None
    controller = None
    asgi_app = None
    data_api_app = None
    try:
        # Create an instance of the SmartDeviceWorker class
        smart_device_worker = SmartDeviceWorker(smart_switch_control, logger, wake_event)

        # Create an instance of the main PowerController class which orchestrates the power control
        controller = PowerController(config, logger, smart_device_worker, wake_event)

        asgi_app, web_notifier = create_asgi_app(controller, config, logger)
        controller.set_webapp_notifier(web_notifier.notify)

        # Create Data API app if enabled
        if config.get("DataAPI", "Enable", default=False):
            data_api_app = create_data_api_app(controller, config, logger)
    except (RuntimeError, TypeError) as e:
        logger.log_fatal_error(f"Fatal error at startup: {e}")
        return

    # Now start the thread manager and create our worker threads
    tm = ThreadManager(logger, global_stop=stop_event)

    tm.add(
        name="smart device",
        target=smart_device_worker.run,
        restart=RestartPolicy(mode="on_crash", max_restarts=3, backoff_seconds=2.0),
        stop_event=stop_event,  # still used by ThreadManager for signaling
    )

    # Manage the controller loop as a thread too
    tm.add(
        name="controller",
        target=controller.run,
        kwargs={"stop_event": stop_event},
        restart=RestartPolicy(mode="never"),
    )

    # Manage the ASGI webapp as a blocking worker in its own managed thread
    tm.add(
        name="webapp",
        target=serve_asgi_blocking,
        args=(asgi_app, config, logger, stop_event),
        restart=RestartPolicy(mode="on_crash", max_restarts=3, backoff_seconds=2.0),
    )

    # Manage the Data API as a blocking worker in its own managed thread (if enabled)
    if data_api_app is not None:
        tm.add(
            name="dataapi",
            target=serve_data_api_blocking,
            args=(data_api_app, config, logger, stop_event),
            restart=RestartPolicy(mode="on_crash", max_restarts=3, backoff_seconds=2.0),
        )

    tm.start_all()
    # (SIGINT handler already installed; remove later duplicate)
    try:
        while not stop_event.is_set():
            if tm.any_crashed():
                logger.log_fatal_error("A managed thread crashed. Initiating shutdown.", report_stack=False)
                stop_event.set()
                wake_event.set()
                break
            stop_event.wait(timeout=1.0)
    finally:
        tm.stop_all()
        tm.join_all(timeout_per_thread=10.0)
        logger.log_message("PowerController application stopped.", "summary")


if __name__ == "__main__":
    main()
