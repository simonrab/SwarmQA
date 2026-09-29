from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from swarmqa.errors import AQAError
from swarmqa.mcp import campaigns, server, tools
from swarmqa.models import CampaignStatus
from swarmqa.orchestrator.status import write_status
from swarmqa.report.layout import ensure_campaign_layout

HAS_SDK = importlib.util.find_spec("mcp") is not None


@pytest.fixture(autouse=True)
def _reset_settings():
    yield
    campaigns.configure()


class _ToolError(Exception):
    pass


class _FakeServer:
    def __init__(self, name, instructions=None):
        self.name = name
        self.instructions = instructions
        self.tools: dict = {}

    def add_tool(self, fn, name=None, description=None):
        self.tools[name] = (fn, description)


def _campaign(root: Path, campaign_id: str = "20260101T000000Z-aaaaaa") -> Path:
    path = root / "reports" / campaign_id
    ensure_campaign_layout(path)
    write_status(CampaignStatus(campaign_id=campaign_id, state="finished", backend="local", workers_configured=1), path)
    return path


def test_build_server_registers_every_tool(tmp_path: Path):
    built = server.build_server(root=tmp_path, sdk=(_FakeServer, _ToolError))
    assert list(built.tools) == [tool.__name__ for tool in tools.TOOLS]
    assert built.name == "swarmqa" and "start_campaign" in built.instructions
    assert campaigns.project_root() == tmp_path.resolve()
    fn, description = built.tools["list_findings"]
    assert description == "Findings for a campaign, or for the latest campaign on a PR."
    assert fn.__name__ == "list_findings"


def test_json_tool_serialises_and_maps_errors(tmp_path: Path):
    campaigns.configure(root=tmp_path)
    wrapped = server.json_tool(tools.campaign_status, _ToolError)
    with pytest.raises(_ToolError, match="no campaigns"):
        wrapped()
    _campaign(tmp_path)
    data = json.loads(wrapped(campaign_id="20260101T000000Z-aaaaaa"))
    assert data["campaign_id"] == "20260101T000000Z-aaaaaa" and data["state"] == "finished"


def test_json_tool_signature_is_resolved():
    import inspect

    wrapped = server.json_tool(tools.list_findings, _ToolError)
    signature = inspect.signature(wrapped)
    assert signature.return_annotation is str
    assert signature.parameters["pr"].annotation == (int | None)
    assert signature.parameters["min_severity"].default == "low"
    assert not isinstance(signature.parameters["min_severity"].annotation, str)


def test_main_without_sdk_prints_install_hint(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def missing():
        raise server.SdkMissing(server.INSTALL_HINT)

    monkeypatch.setattr(server, "load_sdk", missing)
    assert server.main(["--config", "aqa.config.toml"]) == 2
    assert "uv tool install 'swarmqa[mcp]'" in capsys.readouterr().err


def test_main_runs_stdio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runs: list = []

    class _Runnable(_FakeServer):
        def run(self, transport):
            runs.append(transport)

    monkeypatch.setattr(server, "load_sdk", lambda: (_Runnable, _ToolError))
    assert server.main(["--config", "custom.toml", "--root", str(tmp_path)]) == 0
    assert runs == ["stdio"]
    assert campaigns.config_path() == tmp_path.resolve() / "custom.toml"


@pytest.mark.skipif(not HAS_SDK, reason="the mcp extra is not installed")
def test_in_process_server_lists_and_calls_tools(tmp_path: Path):
    import anyio

    _campaign(tmp_path)
    built = server.build_server(root=tmp_path)

    async def exercise():
        async with _connect(built) as client:
            listed = await client.list_tools()
            names = [tool.name for tool in listed.tools]
            ok = await client.call_tool("campaign_status", {})
            bad = await client.call_tool("get_finding", {"finding_id": "nope"})
            return names, ok, bad

    names, ok, bad = anyio.run(exercise)
    assert names == [tool.__name__ for tool in tools.TOOLS]
    assert not _is_error(ok)
    assert json.loads(ok.content[0].text)["campaign_id"] == "20260101T000000Z-aaaaaa"
    assert _is_error(bad)
    assert "unknown finding 'nope'" in bad.content[0].text


@pytest.mark.skipif(not HAS_SDK, reason="the mcp extra is not installed")
def test_stdio_server_process(tmp_path: Path):
    import sys

    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _campaign(tmp_path)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "swarmqa.mcp", "--config", "aqa.config.toml"],
        cwd=str(tmp_path),
        env={"PYTHONPATH": str(Path(server.__file__).resolve().parents[2])},
    )

    async def exercise():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("campaign_status", {})

    result = anyio.run(exercise)
    assert not _is_error(result)
    assert json.loads(result.content[0].text)["state"] == "finished"


def _is_error(result) -> bool:
    return bool(getattr(result, "is_error", getattr(result, "isError", False)))


def _connect(built):
    """An in-process client for either SDK line."""
    try:
        from mcp.client import Client  # mcp >= 2

        return Client(built)
    except ImportError:
        from mcp.shared.memory import create_connected_server_and_client_session  # mcp 1.x

        return create_connected_server_and_client_session(built._mcp_server)


def test_tools_raise_aqa_errors_not_sdk_errors(tmp_path: Path):
    campaigns.configure(root=tmp_path)
    with pytest.raises(AQAError):
        tools.get_finding("x")


def test_main_runs_from_the_project_root(tmp_path, monkeypatch):
    import os

    from swarmqa.mcp import server

    seen = {}

    class FakeServer:
        def run(self, transport):
            seen["cwd"] = os.getcwd()

    monkeypatch.setattr(server, "build_server", lambda config, root: FakeServer())
    monkeypatch.chdir(tmp_path.parent)
    assert server.main(["--root", str(tmp_path)]) == 0
    assert seen["cwd"] == str(tmp_path)
