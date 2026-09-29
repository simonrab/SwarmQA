"""`aqa mcp`: stdio MCP server exposing `swarmqa.mcp.tools.TOOLS`.

The official `mcp` SDK is an optional extra and is imported only when the
server starts. Both SDK lines work: `mcp.server.mcpserver.MCPServer` (2.x)
and `mcp.server.fastmcp.FastMCP` (1.x). Each tool returns its result as JSON
text built with `swarmqa.serialize.to_plain`; tool errors come back as MCP
tool errors carrying the exception message.
"""

from __future__ import annotations

import argparse
import functools
import inspect
import json
import os
import sys
import typing
from pathlib import Path
from typing import Any, Callable

from swarmqa import __version__
from swarmqa.errors import AQAError
from swarmqa.mcp import campaigns
from swarmqa.mcp.tools import TOOLS
from swarmqa.serialize import to_plain

SERVER_NAME = "swarmqa"
INSTALL_HINT = "install the extra: uv tool install 'swarmqa[mcp]'"
INSTRUCTIONS = (
    "SwarmQA runs QA campaigns against an iOS or macOS app. start_campaign returns a "
    "campaign id at once; poll campaign_status until state is finished or stopped, then "
    "list_findings and get_finding. After fixing a finding, call verify_fix and poll "
    "verify_status: passed means the finding no longer reproduces."
)


class SdkMissing(ImportError):
    """The optional `mcp` package is not installed."""


def load_sdk() -> tuple[type, type[Exception]]:
    """The SDK's high-level server class and its ToolError; raises SdkMissing when `mcp` is absent."""
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2
        from mcp.server.mcpserver.exceptions import ToolError

        return MCPServer, ToolError
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x
        from mcp.server.fastmcp.exceptions import ToolError as V1ToolError

        return FastMCP, V1ToolError
    except ImportError as exc:
        raise SdkMissing(INSTALL_HINT) from exc


def json_tool(fn: Callable[..., Any], tool_error: type[Exception] = RuntimeError) -> Callable[..., str]:
    """Wrap a tool so it returns JSON text; keep its name, docs and resolved parameter types.

    Errors an agent can act on (`AQAError`, and `NotImplementedError` from a
    part that has not landed) are re-raised as the SDK's `tool_error`, so the
    message reaches the agent instead of a generic failure.
    """
    hints = typing.get_type_hints(fn)
    signature = inspect.signature(fn)
    params = [
        param.replace(annotation=hints.get(name, param.annotation))
        for name, param in signature.parameters.items()
    ]

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            result = fn(*args, **kwargs)
        except (AQAError, NotImplementedError) as exc:
            raise tool_error(str(exc) or type(exc).__name__) from exc
        return json.dumps(to_plain(result), indent=2, sort_keys=True)

    # The SDK builds the input schema from the signature; the resolved hints
    # keep it independent of this module's globals.
    wrapper.__signature__ = signature.replace(parameters=params, return_annotation=str)  # type: ignore[attr-defined]
    wrapper.__annotations__ = {**{name: hints[name] for name in signature.parameters if name in hints}, "return": str}
    del wrapper.__wrapped__
    return wrapper


def build_server(
    config_path: str | Path | None = None,
    root: str | Path | None = None,
    *,
    sdk: tuple[type, type[Exception]] | None = None,
) -> Any:
    """Configure the tools and return an SDK server with every tool registered."""
    campaigns.configure(config_path=config_path, root=root)
    server_class, tool_error = sdk or load_sdk()
    server = server_class(SERVER_NAME, instructions=INSTRUCTIONS)
    for tool in TOOLS:
        server.add_tool(json_tool(tool, tool_error), name=tool.__name__, description=inspect.getdoc(tool))
    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aqa mcp", description="SwarmQA MCP server (stdio)")
    parser.add_argument("--config", default=campaigns.DEFAULT_CONFIG, help="Config file (default aqa.config.toml)")
    parser.add_argument("--root", default=None, help="Project directory (default: the current directory)")
    parser.add_argument("--version", action="version", version=f"swarmqa {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.root:
        # Relative paths in the config (report_root, intents, app.path) and in
        # verify_fix's detached runs then resolve against the project root.
        os.chdir(Path(args.root).expanduser())
    try:
        server = build_server(args.config, args.root)
    except SdkMissing:
        print(INSTALL_HINT, file=sys.stderr)
        return 2
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
