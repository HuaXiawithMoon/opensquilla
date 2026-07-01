from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

import opensquilla.channels.wecom as wecom_module
from opensquilla.channels.contract import (
    ChannelCapabilities,
    ChannelPlatformCapabilityStatus,
    ChannelPlatformCategories,
)
from opensquilla.channels.registry import parse_channel_entry
from opensquilla.channels.types import OutgoingMessage
from opensquilla.channels.wecom import WeComChannel, WeComChannelConfig
from opensquilla.gateway.config import WeComChannelEntry


class _FakeMessage:
    def __init__(self, data: str, type_name: str = "TEXT") -> None:
        self.data = data
        self.type = SimpleNamespace(name=type_name)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.close_code: int | None = None
        self._subscribe_acked = False
        self._queue: asyncio.Queue[_FakeMessage] = asyncio.Queue()

    def feed(self, payload: dict[str, Any]) -> None:
        self._queue.put_nowait(_FakeMessage(json.dumps(payload)))

    def close_from_server(self, *, code: int = 1000, detail: str = "") -> None:
        self.closed = True
        self.close_code = code
        self._queue.put_nowait(_FakeMessage(detail, "CLOSED"))

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.closed:
            raise RuntimeError("fake websocket is closed")
        self.sent.append(payload)

    async def receive(self) -> _FakeMessage:
        if not self._subscribe_acked and self.sent:
            subscribe = self.sent[0]
            self._subscribe_acked = True
            return _FakeMessage(
                json.dumps(
                    {
                        "cmd": "aibot_subscribe",
                        "headers": {"req_id": subscribe["headers"]["req_id"]},
                        "errcode": 0,
                    }
                )
            )
        return await self._queue.get()

    async def close(self) -> None:
        self.closed = True


def _install_fake_aiohttp(
    monkeypatch: pytest.MonkeyPatch, *websockets: _FakeWebSocket
) -> tuple[list[dict[str, Any]], list[Any]]:
    calls: list[dict[str, Any]] = []
    sessions: list[Any] = []
    available = deque(websockets)

    class _FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.closed = False
            sessions.append(self)

        async def ws_connect(self, url: str, **kwargs: Any) -> _FakeWebSocket:
            calls.append({"url": url, "kwargs": kwargs})
            if not available:
                raise AssertionError("no fake websocket available")
            return available.popleft()

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(ClientSession=_FakeSession))
    return calls, sessions


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition was not met")
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_wecom_websocket_subscribes_to_ai_bot_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_TRUST_ENV", raising=False)
    ws = _FakeWebSocket()
    calls, sessions = _install_fake_aiohttp(monkeypatch, ws)
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
            device_id="device-1",
        )
    )

    await channel.start()
    try:
        assert sessions[0].kwargs == {"trust_env": False}
        assert calls == [
            {
                "url": "wss://openws.work.weixin.qq.com",
                "kwargs": {
                    "heartbeat": 30.0,
                    "timeout": 10.0,
                    "compress": 0,
                },
            }
        ]
        assert "wsagent" not in calls[0]["url"]
        assert "access_token" not in calls[0]["url"]
        assert ws.sent[0]["cmd"] == "aibot_subscribe"
        assert ws.sent[0]["body"] == {
            "bot_id": "bot-id",
            "secret": "bot-secret",
            "device_id": "device-1",
        }
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_wecom_websocket_inbound_callback_can_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _FakeWebSocket()
    _install_fake_aiohttp(monkeypatch, ws)
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    await channel.start()
    try:
        ws.feed(
            {
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "inbound-1"},
                "body": {
                    "msgid": "msg-1",
                    "chatid": "chat-1",
                    "chattype": "group",
                    "msgtype": "text",
                    "from": {"userid": "user-1"},
                    "text": {"content": "hello"},
                },
            }
        )
        incoming = await asyncio.wait_for(channel.receive(), timeout=1)
        assert incoming.content == "hello"
        assert incoming.channel_id == "chat-1"
        assert incoming.metadata["wecom_protocol"] == "aibot"
        assert incoming.metadata["wecom_req_id"] == "inbound-1"

        send_task = asyncio.create_task(channel.send(OutgoingMessage(content="world")))
        await _wait_until(lambda: len(ws.sent) >= 2)
        assert ws.sent[1] == {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": "inbound-1"},
            "body": {"msgtype": "markdown", "markdown": {"content": "world"}},
        }
        ws.feed({"cmd": "aibot_respond_msg", "headers": {"req_id": "inbound-1"}, "errcode": 0})
        await asyncio.wait_for(send_task, timeout=1)
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_wecom_websocket_reconnects_after_server_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wecom_module, "_WEBSOCKET_RECONNECT_BACKOFF_S", (0.0,))
    ws1 = _FakeWebSocket()
    ws2 = _FakeWebSocket()
    calls, _sessions = _install_fake_aiohttp(monkeypatch, ws1, ws2)
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    await channel.start()
    try:
        ws1.feed(
            {
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "inbound-2"},
                "body": {
                    "msgid": "msg-2",
                    "chatid": "chat-2",
                    "chattype": "group",
                    "msgtype": "text",
                    "from": {"userid": "user-2"},
                    "text": {"content": "hello again"},
                },
            }
        )
        incoming = await asyncio.wait_for(channel.receive(), timeout=1)
        assert incoming.channel_id == "chat-2"

        ws1.close_from_server(detail="idle timeout")
        await _wait_until(lambda: len(calls) == 2)
        assert ws2.sent[0]["cmd"] == "aibot_subscribe"

        send_task = asyncio.create_task(channel.send(OutgoingMessage(content="after reconnect")))
        await _wait_until(lambda: len(ws2.sent) >= 2)
        assert ws2.sent[1] == {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": "inbound-2"},
            "body": {"msgtype": "markdown", "markdown": {"content": "after reconnect"}},
        }
        ws2.feed({"cmd": "aibot_respond_msg", "headers": {"req_id": "inbound-2"}, "errcode": 0})
        await asyncio.wait_for(send_task, timeout=1)
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_wecom_websocket_falls_back_to_chat_send_when_reply_req_id_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWebSocket()
    _install_fake_aiohttp(monkeypatch, ws)
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    await channel.start()
    try:
        ws.feed(
            {
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "expired-reply"},
                "body": {
                    "msgid": "msg-expired",
                    "chatid": "chat-expired",
                    "chattype": "group",
                    "msgtype": "text",
                    "from": {"userid": "user-expired"},
                    "text": {"content": "hello"},
                },
            }
        )
        await asyncio.wait_for(channel.receive(), timeout=1)

        send_task = asyncio.create_task(channel.send(OutgoingMessage(content="fallback")))
        await _wait_until(lambda: len(ws.sent) >= 2)
        assert ws.sent[1]["cmd"] == "aibot_respond_msg"
        ws.feed(
            {
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": "expired-reply"},
                "errcode": 40013,
                "errmsg": "reply context expired",
            }
        )

        await _wait_until(lambda: len(ws.sent) >= 3)
        fallback = ws.sent[2]
        assert fallback["cmd"] == "aibot_send_msg"
        assert fallback["body"] == {
            "chatid": "chat-expired",
            "msgtype": "markdown",
            "markdown": {"content": "fallback"},
        }
        ws.feed(
            {
                "cmd": "aibot_send_msg",
                "headers": {"req_id": fallback["headers"]["req_id"]},
                "errcode": 0,
            }
        )
        await asyncio.wait_for(send_task, timeout=1)
    finally:
        await channel.stop()


def test_wecom_websocket_capabilities_do_not_advertise_corp_app_file_upload() -> None:
    channel = WeComChannel(
        WeComChannelConfig(
            connection_mode="websocket",
            bot_id="bot-id",
            bot_secret="bot-secret",
        )
    )

    assert channel.capability_profile.supports(ChannelCapabilities.WEBSOCKET)
    assert not channel.capability_profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)
    assert (
        channel.platform_capability_manifest.get(ChannelPlatformCategories.FILES).status
        == ChannelPlatformCapabilityStatus.UNSUPPORTED
    )


def test_wecom_websocket_config_requires_bot_credentials() -> None:
    with pytest.raises(ValueError, match="bot_id and bot_secret"):
        parse_channel_entry(
            {
                "type": "wecom",
                "name": "wecom",
                "connection_mode": "websocket",
                "corp_id": "corp",
                "corp_secret": "corp-secret",
                "agent_id_int": 1001,
            }
        )

    entry = parse_channel_entry(
        {
            "type": "wecom",
            "name": "wecom",
            "connection_mode": "websocket",
            "bot_id": "bot",
            "bot_secret": "secret",
            "device_id": "device-from-config",
        }
    )
    assert isinstance(entry, WeComChannelEntry)
    assert entry.websocket_url == "wss://openws.work.weixin.qq.com"
    assert entry.device_id == "device-from-config"


def test_wecom_webhook_config_remains_supported() -> None:
    entry = parse_channel_entry(
        {
            "type": "wecom",
            "name": "wecom-callback",
            "connection_mode": "webhook",
            "corp_id": "corp",
            "corp_secret": "corp-secret",
            "agent_id_int": 1001,
            "token": "token",
            "encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        }
    )
    assert isinstance(entry, WeComChannelEntry)
    assert entry.connection_mode == "webhook"
