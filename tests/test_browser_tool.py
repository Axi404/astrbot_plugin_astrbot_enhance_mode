from __future__ import annotations

import pytest

from astrbot_plugin_astrbot_enhance_mode import main as main_module
from astrbot_plugin_astrbot_enhance_mode.main import Main
from astrbot_plugin_astrbot_enhance_mode.plugin_config import (
    BrowserToolConfig,
    PluginConfig,
)


class _DummyEvent:
    def __init__(self, origin: str = "origin-test") -> None:
        self.unified_msg_origin = origin


class _FakeProcess:
    def __init__(
        self,
        stdout: str,
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self._stdout = stdout.encode("utf-8")
        self._stderr = stderr.encode("utf-8")
        self.returncode = returncode
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


def _build_plugin() -> Main:
    plugin = Main.__new__(Main)
    return plugin


@pytest.mark.asyncio
async def test_run_browser_tool_command_uses_isolated_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _build_plugin()
    event = _DummyEvent()
    cfg = PluginConfig(
        browser_tool=BrowserToolConfig(
            enable=True,
            timeout_sec=20,
            allowed_domains=["example.com"],
        )
    )
    captured: dict[str, object] = {}

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["args"] = list(args)
        captured["env"] = kwargs["env"]
        return _FakeProcess(
            stdout='{"success":true,"data":{"snapshot":"tree","refs":{"e1":{"role":"link","name":"Docs"}}}}'
        )

    monkeypatch.setattr(
        main_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    result = await plugin._run_browser_tool_command(event, ["snapshot", "-i"], cfg)

    assert result["ok"] is True
    assert result["action"] == "snapshot"
    assert result["data"] == {
        "snapshot": "tree",
        "refs": {"e1": {"role": "link", "name": "Docs"}},
    }

    command_args = captured["args"]
    assert command_args[0] == "agent-browser"
    assert "--session" in command_args
    session_index = command_args.index("--session") + 1
    assert str(command_args[session_index]).startswith("astrbot-origin-test-")
    assert "--session-name" in command_args
    assert "--allowed-domains" in command_args
    assert command_args[-2:] == ["snapshot", "-i"]

    env = captured["env"]
    assert env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] == "300000"
    assert env["AGENT_BROWSER_DEFAULT_TIMEOUT"] == "20000"


@pytest.mark.asyncio
async def test_run_browser_tool_command_reports_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _build_plugin()
    event = _DummyEvent()
    cfg = PluginConfig(browser_tool=BrowserToolConfig(enable=True))

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        raise FileNotFoundError()

    monkeypatch.setattr(
        main_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    result = await plugin._run_browser_tool_command(event, ["open", "https://example.com"], cfg)

    assert result["ok"] is False
    assert "npm install -g agent-browser" in str(result["error"])


@pytest.mark.asyncio
async def test_enhance_browser_action_maps_fill_command() -> None:
    plugin = _build_plugin()
    plugin.config = {"browser_tool": {"enable": True}}
    event = _DummyEvent()
    captured: dict[str, object] = {}

    async def _fake_run_browser_tool_command(event_arg, command_args, cfg):  # noqa: ANN001
        captured["event"] = event_arg
        captured["command_args"] = command_args
        captured["cfg"] = cfg
        return {"ok": True, "action": command_args[0], "data": {"done": True}}

    plugin._run_browser_tool_command = _fake_run_browser_tool_command  # type: ignore[method-assign]

    result_text = await plugin.enhance_browser_action(
        event,
        action="fill",
        target="@e2",
        value="hello@example.com",
    )

    assert captured["event"] is event
    assert captured["command_args"] == ["fill", "@e2", "hello@example.com"]
    assert '"ok": true' in result_text
