"""DataAPI servicing module for PowerController.

This module provides a REST API for external clients to access system data.
It runs as a separate thread and provides endpoints for outputs, meters, temperature probes, and energy prices.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING, Annotated

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from threading import Event

    from sc_foundation import SCConfigManager, SCLogger

    from controller import PowerController


def _validate_access_key(config: SCConfigManager, logger: SCLogger, key_from_request: str | None) -> bool:
    """Validate the access key from the request.

    Args:
        config: Configuration manager instance
        logger: Logger instance
        key_from_request: Access key from the request (URL param or header)

    Returns:
        bool: True if access is allowed, False otherwise
    """
    expected_key = os.environ.get("DATAAPI_ACCESS_KEY")
    if not expected_key:
        expected_key = config.get("DataAPI", "AccessKey")
    if expected_key is None:
        return True
    if isinstance(expected_key, str) and not expected_key.strip():
        # Current behavior: empty AccessKey means open access.
        return True

    if key_from_request is None:
        logger.log_message("DataAPI: Missing access key.", "warning")
        return False
    key = key_from_request.strip()
    if not key:
        logger.log_message("DataAPI: Blank access key used.", "warning")
        return False
    if key != expected_key:
        logger.log_message("DataAPI: Invalid access key used.", "warning")
        return False
    return True


def _get_access_key_from_request(request: Request, access_key_param: str | None) -> str | None:
    """Extract access key from URL parameter or request header.

    Args:
        request: FastAPI request object
        access_key_param: Access key from URL query parameter

    Returns:
        str | None: The access key if found, None otherwise
    """
    # Try URL parameter first
    if access_key_param:
        return access_key_param
    # Try Authorization header
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:]
    # Try X-Access-Key header
    return request.headers.get("X-Access-Key")


def create_asgi_app(controller: PowerController, config: SCConfigManager, logger: SCLogger) -> FastAPI:
    """Create and configure the FastAPI application for the Data API.

    Args:
        controller: PowerController instance to retrieve data from
        config: Configuration manager
        logger: Logger instance

    Returns:
        FastAPI: Configured FastAPI application
    """
    app = FastAPI(title="PowerController Data API", version="1.0.0")

    @app.get("/outputs")
    async def get_outputs(
        request: Request,
        access_key: Annotated[str | None, Query(description="Access key for authentication")] = None,
    ) -> JSONResponse:
        """Get output device data.

        Returns:
            JSONResponse: JSON containing output data and last refresh timestamp.

        Raises:
            HTTPException: If access key validation fails (401).
        """
        key = _get_access_key_from_request(request, access_key)
        if not _validate_access_key(config, logger, key):
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing access key")

        data = await asyncio.to_thread(controller.get_api_data, "Outputs")
        if not data:
            raise HTTPException(status_code=503, detail="API data not available")
        return JSONResponse(content=data)

    @app.get("/meters")
    async def get_meters(
        request: Request,
        access_key: Annotated[str | None, Query(description="Access key for authentication")] = None,
    ) -> JSONResponse:
        """Get meter data.

        Returns:
            JSONResponse: JSON containing meter data and last refresh timestamp.

        Raises:
            HTTPException: If access key validation fails (401).
        """
        key = _get_access_key_from_request(request, access_key)
        if not _validate_access_key(config, logger, key):
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing access key")

        data = await asyncio.to_thread(controller.get_api_data, "Meters")
        if not data:
            raise HTTPException(status_code=503, detail="API data not available")
        return JSONResponse(content=data)

    @app.get("/tempprobes")
    async def get_tempprobes(
        request: Request,
        access_key: Annotated[str | None, Query(description="Access key for authentication")] = None,
    ) -> JSONResponse:
        """Get temperature probe data.

        Returns:
            JSONResponse: JSON containing temperature probe data and last refresh timestamp.

        Raises:
            HTTPException: If access key validation fails (401).
        """
        key = _get_access_key_from_request(request, access_key)
        if not _validate_access_key(config, logger, key):
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing access key")

        data = await asyncio.to_thread(controller.get_api_data, "TempProbes")
        if not data:
            raise HTTPException(status_code=503, detail="API data not available")
        return JSONResponse(content=data)

    @app.get("/energyprices")
    async def get_energyprices(
        request: Request,
        access_key: Annotated[str | None, Query(description="Access key for authentication")] = None,
    ) -> JSONResponse:
        """Get energy price data.

        Returns:
            JSONResponse: JSON containing energy price forecast data and last refresh timestamp.

        Raises:
            HTTPException: If access key validation fails (401).
        """
        key = _get_access_key_from_request(request, access_key)
        if not _validate_access_key(config, logger, key):
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing access key")

        data = await asyncio.to_thread(controller.get_api_data, "EnergyPrices")
        if not data:
            raise HTTPException(status_code=503, detail="API data not available")
        return JSONResponse(content=data)

    @app.get("/ups")
    async def get_ups(
        request: Request,
        access_key: Annotated[str | None, Query(description="Access key for authentication")] = None,
    ) -> JSONResponse:
        """Get UPS data.

        Returns:
            JSONResponse: JSON containing UPS data and last refresh timestamp.

        Raises:
            HTTPException: If access key validation fails (401).
        """
        key = _get_access_key_from_request(request, access_key)
        if not _validate_access_key(config, logger, key):
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing access key")

        data = await asyncio.to_thread(controller.get_api_data, "UPS")
        if not data:
            raise HTTPException(status_code=503, detail="API data not available")
        return JSONResponse(content=data)

    @app.get("/all")
    async def get_all(
        request: Request,
        access_key: Annotated[str | None, Query(description="Access key for authentication")] = None,
    ) -> JSONResponse:
        """Get all available data.

        Returns:
            JSONResponse: JSON containing all data categories and last refresh timestamp.

        Raises:
            HTTPException: If access key validation fails (401).
        """
        key = _get_access_key_from_request(request, access_key)
        logger.log_message(f"Received request for /all endpoint with access key: {'present' if key else 'missing'}", "debug")
        if not _validate_access_key(config, logger, key):
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing access key")

        data = await asyncio.to_thread(controller.get_api_data)
        if not data:
            raise HTTPException(status_code=503, detail="API data not available")
        return JSONResponse(content=data)

    @app.get("/")
    async def root() -> JSONResponse:
        """Root endpoint - returns API information.

        Returns:
            JSONResponse: API metadata including name, version, and available endpoints.
        """
        return JSONResponse(
            content={
                "name": "PowerController Data API",
                "version": "1.0.0",
                "endpoints": ["/outputs", "/meters", "/tempprobes", "/energyprices", "/all"],
            }
        )

    return app


def serve_asgi_blocking(app: FastAPI, config: SCConfigManager, logger: SCLogger, stop_event: Event) -> None:
    """Run the Data API ASGI server in the current thread with cooperative shutdown.

    Args:
        app: FastAPI application instance
        config: Configuration manager
        logger: Logger instance
        stop_event: Threading event to signal shutdown
    """
    host_raw = config.get("DataAPI", "HostingIP", default="127.0.0.1")
    host = host_raw if isinstance(host_raw, str) and host_raw else "127.0.0.1"
    port = int(config.get("DataAPI", "Port", default=8081) or 8081)  # pyright: ignore[reportArgumentType]

    # Uvicorn log config can be noisy; keep our SCLogger as the source of truth.
    uv_config = uvicorn.Config(app, host=host, port=port, log_level="warning", reload=False)
    server = uvicorn.Server(uv_config)
    # Running under ThreadManager in a non-main thread: avoid installing signal handlers.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    async def _run() -> None:
        async def _stop_watcher() -> None:
            # Block in a worker thread until the threading.Event is set.
            await asyncio.to_thread(stop_event.wait)
            server.should_exit = True

        watcher = asyncio.create_task(_stop_watcher())
        try:
            logger.log_message(f"Data API server listening on http://{host}:{port}", "summary")
            await server.serve()
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            logger.log_message("Data API server shutdown complete.", "detailed")

    try:
        asyncio.run(_run())
    except asyncio.CancelledError:
        # Can occur if background tasks are cancelled during interpreter shutdown.
        logger.log_message("Data API server cancelled during shutdown.", "debug")
