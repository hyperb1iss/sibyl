"""An in-memory pub/sub channel shared by several ``EventBus`` transports.

Every publish reaches every subscribed transport, the sender included, which
is how Redis pub/sub behaves for the processes subscribed to one channel.
"""

from __future__ import annotations

from typing import Any

from sibyl.coordination.events import EventSubscriber


class FakeChannel:
    def __init__(self) -> None:
        self.transports: list[FakeTransport] = []


class FakeTransport:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel
        self.subscribers: list[EventSubscriber] = []

    async def connect(self) -> None:
        self._channel.transports.append(self)

    async def disconnect(self) -> None:
        self._channel.transports.remove(self)

    async def subscribe(self, subscriber: EventSubscriber) -> None:
        self.subscribers.append(subscriber)

    async def publish(self, event: str, data: dict[str, Any], org_id: str | None = None) -> None:
        for transport in list(self._channel.transports):
            for subscriber in transport.subscribers:
                await subscriber(event, data, org_id)
