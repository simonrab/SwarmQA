"""Providers that call a coding-agent CLI instead of a model API.

`claude_cli` runs `claude -p` (Claude Code) and `codex_cli` runs `codex exec`
(Codex), one process per call, with the call's JSON schema as structured
output. They use the CLI's own sign-in, so a campaign can run on a Claude or
ChatGPT plan with no API key; per-call cost is reported as 0 because the plan,
not the token count, pays for it. Each call starts a process, so expect a few
seconds per step: fine for one or two devices, slow for a large swarm.

A screenshot, when the call has one, is written to a temporary directory the
CLI may read (`Read` tool for Claude Code, `--image` for Codex).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from swarmqa.llm.base import BaseProvider, BilledError, Completion, ModelCall, int_field
from swarmqa.llm.protocol import ModelError, ModelRefused, Usage
from swarmqa.llm.transport import TransportTimeout

Runner = Callable[..., subprocess.CompletedProcess]
# The model setting that means "the CLI's own default model".
DEFAULT_MODEL = "default"
_IMAGE_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def _default_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    from swarmqa.driver.runner_client import bounded_runner

    return bounded_runner(args, **kwargs)


class CLITransport:
    """Runs one CLI call per `send`. `request` is what `CLIProvider.build_request` returns."""

    def __init__(self, cli: str, *, executable: str | None = None, runner: Runner | None = None):
        self.cli = cli
        self.executable = executable or cli_name(cli)
        self.runner = runner or _default_runner

    def send(self, request: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        if shutil.which(self.executable) is None and self.runner is _default_runner:
            raise ModelError(f"`{self.executable}` is not on PATH; install it and sign in first")
        with tempfile.TemporaryDirectory(prefix="aqa-llm-") as tmp:
            work = Path(tmp)
            image = None
            if request.get("image") is not None:
                data, media_type = request["image"]
                image = work / f"screen{_IMAGE_SUFFIX.get(media_type, '.png')}"
                image.write_bytes(data)
            if self.cli == "claude":
                args, stdin = self._claude(request, work, image)
            else:
                args, stdin = self._codex(request, work, image)
            try:
                done = self.runner(args, timeout=timeout_s, input=stdin, cwd=str(work))
            except subprocess.TimeoutExpired as exc:
                raise TransportTimeout(f"{self.executable} did not answer in {timeout_s:.0f}s") from exc
            if self.cli == "claude":
                return _claude_result(done)
            return _codex_result(done, work, request)

    def _claude(self, request: dict[str, Any], work: Path, image: Path | None) -> tuple[list[str], str]:
        args = [
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(request["schema"]),
            "--no-session-persistence",
            "--strict-mcp-config",
            "--append-system-prompt",
            request["system"],
        ]
        if request["model"] and request["model"] != DEFAULT_MODEL:
            args += ["--model", request["model"]]
        prompt = request["prompt"]
        if image is not None:
            args += ["--tools", "Read", "--allowedTools", "Read", "--add-dir", str(work)]
            prompt += f"\n\nThe screenshot is the image file {image}. Read it before answering."
        else:
            args += ["--tools", ""]
        return args, prompt

    def _codex(self, request: dict[str, Any], work: Path, image: Path | None) -> tuple[list[str], str]:
        schema = work / "schema.json"
        schema.write_text(json.dumps(request["schema"]), encoding="utf-8")
        args = [
            self.executable,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(work / "answer.json"),
            "--cd",
            str(work),
        ]
        if request["model"] and request["model"] != DEFAULT_MODEL:
            args += ["--model", request["model"]]
        if image is not None:
            args += ["--image", str(image)]
        args.append("-")  # the prompt comes on stdin
        return args, f"{request['system']}\n\n{request['prompt']}"


def cli_name(cli: str) -> str:
    return {"claude": "claude", "codex": "codex"}[cli]


def _claude_result(done: subprocess.CompletedProcess) -> dict[str, Any]:
    text = (done.stdout or "").strip()
    try:
        envelope = json.loads(text) if text else None
    except json.JSONDecodeError:
        envelope = None
    if not isinstance(envelope, dict):
        detail = (done.stderr or text or "no output").strip().splitlines()
        raise ModelError(f"claude -p failed (exit {done.returncode}): {detail[-1] if detail else ''}")
    return envelope


def _codex_result(done: subprocess.CompletedProcess, work: Path, request: dict[str, Any]) -> dict[str, Any]:
    answer = work / "answer.json"
    if done.returncode != 0 or not answer.is_file():
        detail = (done.stderr or done.stdout or "no output").strip().splitlines()
        raise ModelError(f"codex exec failed (exit {done.returncode}): {detail[-1] if detail else ''}")
    return {"text": answer.read_text(encoding="utf-8"), "model": request["model"]}


class CLIProvider(BaseProvider):
    """`BaseProvider` over a CLI: the prompt is text, the schema is the CLI's structured output."""

    cli = ""

    def build_request(self, call: ModelCall) -> dict[str, Any]:
        image = (call.image.data, call.image.media_type) if call.image is not None else None
        return {
            "model": call.model,
            "system": call.system,
            "prompt": call.text,
            "schema": call.schema,
            "schema_name": call.schema_name,
            "image": image,
        }

    def _usage(self, completion: Completion, latency: float) -> Usage:
        # The CLI's plan pays for the call; tokens are kept for reference.
        return Usage(
            provider=self.name,
            model=completion.model,
            input_tokens=completion.input_tokens + completion.cache_write_tokens,
            output_tokens=completion.output_tokens,
            cache_read_tokens=completion.cache_read_tokens,
            cost=0.0,
            currency=self.settings.currency,
            latency_s=round(latency, 3),
        )


class ClaudeCLIProvider(CLIProvider):
    name = "claude_cli"
    cli = "claude"

    def parse_response(self, raw: dict[str, Any], call: ModelCall) -> Completion:
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        models = raw.get("modelUsage") if isinstance(raw.get("modelUsage"), dict) else {}
        completion = Completion(
            text="",
            model=next(iter(models), call.model),
            input_tokens=int_field(usage, "input_tokens"),
            output_tokens=int_field(usage, "output_tokens"),
            cache_read_tokens=int_field(usage, "cache_read_input_tokens"),
            cache_write_tokens=int_field(usage, "cache_creation_input_tokens"),
        )
        if raw.get("is_error"):
            message = str(raw.get("result") or raw.get("subtype") or "error")
            raise BilledError(completion, ModelRefused(f"claude -p: {message[:300]}"))
        structured = raw.get("structured_output")
        if isinstance(structured, dict):
            completion.text = json.dumps(structured)
        else:
            completion.text = str(raw.get("result") or "")
        if not completion.text.strip():
            raise BilledError(completion, ModelRefused("claude -p returned no answer"))
        return completion


class CodexCLIProvider(CLIProvider):
    name = "codex_cli"
    cli = "codex"

    def parse_response(self, raw: dict[str, Any], call: ModelCall) -> Completion:
        text = str(raw.get("text") or "").strip()
        model = str(raw.get("model") or call.model)
        completion = Completion(text=text, model=f"codex:{model}")
        if not text:
            raise BilledError(completion, ModelRefused("codex exec returned no answer"))
        return completion
