"""Run the shared social feed without starting gallery or scheduler features."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from aiohttp import web

from web_server import GalleryServer


logger = logging.getLogger(__name__)


def _port(value: object) -> int:
    try:
        port = int(str(value or "18889"))
    except ValueError as exc:
        raise RuntimeError("SOCIAL_HUB_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("SOCIAL_HUB_PORT must be between 1 and 65535")
    return port


def _runtime_server_token(explicit_token: str | None) -> str:
    if explicit_token is not None:
        return str(explicit_token).strip()
    environment_token = os.environ.get("SOCIAL_SERVER_TOKEN", "").strip()
    if environment_token:
        return environment_token
    token_path = os.environ.get("SOCIAL_SERVER_TOKEN_FILE", "").strip()
    if not token_path:
        return ""
    try:
        with open(token_path, "r", encoding="ascii") as token_file:
            content = token_file.read(1025)
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Unable to read SOCIAL_SERVER_TOKEN_FILE") from exc
    if len(content) > 1024:
        raise RuntimeError("SOCIAL_SERVER_TOKEN_FILE is too large")
    return content.strip()


def create_social_hub_server(
    *,
    data_dir: str | None = None,
    host: str | None = None,
    port: int | str | None = None,
    server_token: str | None = None,
) -> GalleryServer:
    token = _runtime_server_token(server_token)
    if len(token) < 24:
        raise RuntimeError("SOCIAL_SERVER_TOKEN must contain at least 24 characters")
    if len(token) > 256 or not token.isascii() or any(
        ord(character) < 33 or ord(character) > 126 for character in token
    ):
        raise RuntimeError(
            "SOCIAL_SERVER_TOKEN must contain 24-256 printable ASCII characters"
        )

    resolved_data_dir = os.path.abspath(
        data_dir or os.environ.get("SOCIAL_HUB_DATA_DIR", "/app/data")
    )
    resolved_host = str(
        host if host is not None else os.environ.get("SOCIAL_HUB_HOST", "127.0.0.1")
    ).strip()
    if not resolved_host:
        raise RuntimeError("SOCIAL_HUB_HOST must not be empty")
    resolved_port = _port(
        port if port is not None else os.environ.get("SOCIAL_HUB_PORT", "18889")
    )
    return GalleryServer(
        {
            "gallery": {"host": resolved_host, "port": resolved_port},
            "social": {"server_token": token},
        },
        resolved_data_dir,
        social_hub_only=True,
    )


async def serve(server: GalleryServer) -> None:
    # Nginx is the public edge. Avoid retaining client IP/path access logs in
    # the application process; operational errors still go to stderr.
    runner = web.AppRunner(server.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, server.host, server.port)
    await site.start()
    logger.info("Social hub listening on http://%s:%s", server.host, server.port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(stop_signal, stop_event.set)
        except NotImplementedError:
            pass
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(serve(create_social_hub_server()))


if __name__ == "__main__":
    main()
