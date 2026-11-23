"""Embedded MQTT 3.1.1 broker (amqtt) so the stack runs with zero installs.

In production / docker-compose an Eclipse Mosquitto container is used
instead; both expose TCP 1883 and MQTT-over-WebSocket 9001.
"""
from __future__ import annotations

import asyncio
import logging
import os

from amqtt.broker import Broker


def config(port: int = 1883, ws_port: int = 9001) -> dict:
    return {
        "listeners": {
            "default": {"type": "tcp", "bind": f"0.0.0.0:{port}"},
            "ws": {"type": "ws", "bind": f"0.0.0.0:{ws_port}"},
        },
        "plugins": {
            "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True},
        },
    }


async def run(port: int = 1883, ws_port: int = 9001):
    broker = Broker(config(port, ws_port))
    await broker.start()
    logging.getLogger(__name__).info("MQTT broker on tcp:%d ws:%d", port, ws_port)
    try:
        await asyncio.Event().wait()
    finally:
        await broker.shutdown()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s broker %(levelname)s %(message)s")
    for name in ("amqtt", "transitions"):
        logging.getLogger(name).setLevel(logging.WARNING)
    asyncio.run(run(int(os.getenv("MQTT_PORT", "1883")), int(os.getenv("MQTT_WS_PORT", "9001"))))


if __name__ == "__main__":
    main()
