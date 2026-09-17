# SPDX-License-Identifier: GPL-3.0-or-later
"""Async client for the Nicotine+ MCP Bridge socket (protocol v1/v2)."""

import asyncio
import json

from .config import bridge_socket_path


class BridgeError(Exception):
    pass


class BridgeUnavailable(BridgeError):
    pass


class RateLimited(BridgeError):

    def __init__(self, retry_after):
        super().__init__(f"rate_limited (retry in {retry_after}s)")
        self.retry_after = float(retry_after or 5)


class BridgeClient:

    def __init__(self, socket_path=None):
        self._socket_path = socket_path

    @property
    def socket_path(self):
        return self._socket_path or bridge_socket_path()

    async def call(self, method, **params):
        path = self.socket_path

        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path, limit=64 * 1024 * 1024), 5)
        except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError) as error:
            raise BridgeUnavailable(
                f"Cannot reach Nicotine+ at {path} ({type(error).__name__}). Is Nicotine+ running with the "
                "'MCP Bridge' plugin enabled?"
            ) from None

        try:
            payload = {"method": method, "params": {k: v for k, v in params.items() if v is not None}}
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), 30)
        finally:
            writer.close()

        if not line:
            raise BridgeError("Nicotine+ closed the connection without replying")

        response = json.loads(line)

        if not response.get("ok"):
            error = response.get("error", "unknown error from Nicotine+")

            if error == "rate_limited":
                raise RateLimited(response.get("retry_after"))

            raise BridgeError(error)

        return response["result"]

    async def status(self):
        return await self.call("status")
