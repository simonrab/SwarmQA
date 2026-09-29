"""aqa doctor with fake runners: macOS and Linux paths, no host tools touched."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from swarmqa.cli import main
from swarmqa.doctor import (
    NOT_MACOS,
    DoctorEnv,
    exit_code,
    parse_gh_scopes,
    render_json,
    render_text,
    run_doctor,
)

_SECRET = "sk-ant-super-secret-value-123"

_RUNTIMES = {
    "runtimes": [
        {
            "name": "iOS 26.0",
            "platform": "iOS",
            "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-0",
            "isAvailable": True,
        },
        {
            "name": "watchOS 26.0",
            "platform": "watchOS",
            "identifier": "com.apple.CoreSimulator.SimRuntime.watchOS-26-0",
            "isAvailable": True,
        },
    ]
}
_DEVICES = {
    "devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
            {"name": "iPhone 17", "udid": "AAAA-1111", "isAvailable": True},
            {"name": "iPhone 17 Pro", "udid": "BBBB-2222", "isAvailable": True},
        ]
    }
}
_GH_OK = """github.com
  ✓ Logged in to github.com account octo (keyring)
  - Active account: true
  - Git operations protocol: https
  - Token: gho_************************************
  - Token scopes: 'checks:write', 'pull-requests:write', 'read:org'
"""


class FakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], object] | None = None):
        self.responses = dict(responses or {})
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        for prefix, response in self.responses.items():
            if tuple(args[: len(prefix)]) == prefix:
                if isinstance(response, Exception):
                    raise response
                code, out, err = response
                return subprocess.CompletedProcess(args, code, out, err)
        raise FileNotFoundError(2, "not found", args[0])


def _mac_runner(overrides: dict | None = None) -> FakeRunner:
    responses: dict[tuple[str, ...], object] = {
        ("xcode-select", "-p"): (0, "/Applications/Xcode.app/Contents/Developer\n", ""),
        ("xcodebuild", "-version"): (0, "Xcode 26.0\nBuild version 17A100\n", ""),
        ("xcrun", "simctl", "list", "runtimes"): (0, json.dumps(_RUNTIMES), ""),
        ("xcrun", "simctl", "list", "devices"): (0, json.dumps(_DEVICES), ""),
        ("osascript",): (0, "true\n", ""),
        ("tart", "--version"): (0, "2.20.0\n", ""),
        ("gh", "auth", "status"): (0, _GH_OK, ""),
    }
    responses.update(overrides or {})
    return FakeRunner(responses)


def _env(
    *,
    platform: str = "darwin",
    machine: str = "arm64",
    runner: FakeRunner | None = None,
    tools: tuple[str, ...] = ("idb", "ffmpeg", "tart", "gh"),
    environ: dict[str, str] | None = None,
    modules: tuple[str, ...] = ("anthropic", "openai", "mcp"),
    python_version: tuple[int, ...] = (3, 12, 4),
) -> DoctorEnv:
    return DoctorEnv(
        runner=runner or _mac_runner(),
        which=lambda name: f"/opt/homebrew/bin/{name}" if name in tools else None,
        platform=platform,
        machine=machine,
        environ=environ if environ is not None else {},
        find_spec=lambda name: object() if name in modules else None,
        python_version=python_version,
    )


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "aqa.config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _by_name(checks):
    return {check.name: check for check in checks}


def test_macos_all_ok(tmp_path: Path):
    config = _write(
        tmp_path,
        '[app]\nplatform = "ios"\nsimulators = ["iPhone 17", "BBBB-2222"]\n',
    )
    checks = run_doctor(config, github=True, env=_env())
    statuses = {check.name: check.status for check in checks}
    assert set(statuses.values()) == {"ok", "skip"}, statuses
    assert statuses["llm keys"] == "skip"
    found = _by_name(checks)
    assert "Xcode 26.0" in found["xcode"].message
    assert "iOS 26.0" in found["simulators"].message
    assert "watchOS" not in found["simulators"].message
    assert "2 available iOS devices" in found["simulators"].message
    assert found["github"].status == "ok"
    assert "octo" in found["github"].message
    assert exit_code(checks) == 0


def test_linux_skips_mac_checks(tmp_path: Path):
    runner = FakeRunner()
    checks = run_doctor(
        tmp_path / "missing.toml",
        env=_env(platform="linux", machine="x86_64", runner=runner, tools=()),
    )
    found = _by_name(checks)
    for name in ("xcode", "simulators", "idb", "accessibility", "tart"):
        assert found[name].status == "skip"
        assert found[name].message == NOT_MACOS
    assert found["ffmpeg"].status == "warn"
    assert found["config"].status == "warn"
    assert "aqa init" in found["config"].fix
    assert "github" not in found
    assert runner.calls == []
    assert exit_code(checks) == 0


def test_missing_xcode_and_tools(tmp_path: Path):
    runner = FakeRunner({("osascript",): (0, "false\n", "")})
    checks = run_doctor(
        tmp_path / "none.toml", env=_env(runner=runner, tools=(), machine="x86_64")
    )
    found = _by_name(checks)
    assert found["xcode"].status == "fail"
    assert "xcode-select" in found["xcode"].fix
    assert found["simulators"].status == "warn"  # macOS project: iOS runtimes optional
    assert found["idb"].status == "warn"
    assert "facebook/fb/idb" in found["idb"].fix
    assert found["accessibility"].status == "warn"
    assert "Privacy & Security" in found["accessibility"].fix
    assert found["ffmpeg"].status == "warn"
    assert found["tart"].status == "warn"
    assert "Apple Silicon" in found["tart"].message
    assert exit_code(checks) == 1


def test_command_line_tools_only_is_a_fail(tmp_path: Path):
    runner = _mac_runner(
        {
            ("xcode-select", "-p"): (0, "/Library/Developer/CommandLineTools\n", ""),
            ("xcodebuild", "-version"): (1, "", "requires Xcode"),
        }
    )
    found = _by_name(run_doctor(None, env=_env(runner=runner)))
    assert found["xcode"].status == "fail"
    assert "Command Line Tools" in found["xcode"].fix


def test_ios_project_without_runtime_or_device_fails(tmp_path: Path):
    config = _write(tmp_path, '[app]\nplatform = "ios"\nsimulator = "iPhone 99"\n')
    empty = _mac_runner({("xcrun", "simctl", "list", "runtimes"): (0, '{"runtimes": []}', "")})
    assert _by_name(run_doctor(config, env=_env(runner=empty)))["simulators"].status == "fail"
    missing = _by_name(run_doctor(config, env=_env()))["simulators"]
    assert missing.status == "fail"
    assert "iPhone 99" in missing.message


def test_vm_backend_without_tart_fails(tmp_path: Path):
    config = _write(tmp_path, '[campaign]\nbackend = "vm"\n')
    found = _by_name(run_doctor(config, env=_env(tools=("idb", "ffmpeg"))))
    assert found["tart"].status == "fail"
    assert "cirruslabs" in found["tart"].fix


def test_accessibility_unknown_is_warn():
    runner = _mac_runner({("osascript",): (1, "", "execution error")})
    found = _by_name(run_doctor(None, env=_env(runner=runner)))
    assert found["accessibility"].status == "warn"
    assert "could not determine" in found["accessibility"].message


def test_api_key_present_never_printed(tmp_path: Path):
    config = _write(tmp_path, '[llm]\nenabled = true\nprovider = "anthropic"\n')
    checks = run_doctor(config, env=_env(environ={"ANTHROPIC_API_KEY": _SECRET}))
    found = _by_name(checks)
    assert found["llm keys"].status == "ok"
    assert "ANTHROPIC_API_KEY" in found["llm keys"].message
    assert _SECRET not in render_text(checks)
    assert _SECRET not in render_json(checks)


def test_api_key_absent_fails_and_custom_env_is_named(tmp_path: Path):
    config = _write(
        tmp_path, '[llm]\nenabled = true\nprovider = "openai"\napi_key_env = "MY_OPENAI"\n'
    )
    found = _by_name(run_doctor(config, env=_env(environ={"OPENAI_API_KEY": _SECRET})))
    assert found["llm keys"].status == "fail"
    assert "MY_OPENAI" in found["llm keys"].message
    assert _SECRET not in found["llm keys"].message + found["llm keys"].fix


def test_missing_provider_extra_fails_others_warn(tmp_path: Path):
    config = _write(tmp_path, '[llm]\nenabled = true\nprovider = "anthropic"\n')
    found = _by_name(
        run_doctor(config, env=_env(environ={"ANTHROPIC_API_KEY": "x"}, modules=()))
    )
    assert found["extra: anthropic"].status == "fail"
    assert found["extra: openai"].status == "warn"
    assert found["extra: mcp"].status == "warn"
    assert "swarmqa[mcp]" in found["extra: mcp"].fix


def test_config_invalid_and_deprecated_pr(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    bad = _write(tmp_path, "[campaign]\nworkers = 0\n")
    found = _by_name(run_doctor(bad, env=_env()))
    assert found["config"].status == "fail"
    assert "workers" in found["config"].message

    old = _write(tmp_path, '[pr]\nmode = "human"\n')
    found = _by_name(run_doctor(old, env=_env()))
    assert found["config"].status == "warn"
    assert "[pr]" in found["config"].message
    assert "deprecated" not in capsys.readouterr().err  # doctor reports it as a check instead


def test_old_python_fails():
    found = _by_name(run_doctor(None, env=_env(python_version=(3, 10, 2))))
    assert found["python"].status == "fail"


@pytest.mark.parametrize(
    ("output", "code", "tools", "status"),
    [
        (_GH_OK, 0, ("gh",), "ok"),
        ("✓ Logged in to github.com as octo (oauth_token)\n✓ Token scopes: repo, read:org\n", 0, ("gh",), "warn"),
        ("✓ Logged in to github.com account octo (keyring)\n- Token: github_pat_***\n", 0, ("gh",), "warn"),
        ("- Token scopes: 'read:org', 'gist'\n", 0, ("gh",), "fail"),
        ("You are not logged into any GitHub hosts.", 1, ("gh",), "fail"),
        ("", 0, (), "fail"),
    ],
)
def test_github_scopes(output, code, tools, status):
    runner = _mac_runner({("gh", "auth", "status"): (code, "", output)})
    env = _env(platform="linux", runner=runner, tools=tools)
    found = _by_name(run_doctor(None, github=True, env=env))
    assert found["github"].status == status
    assert "gho_" not in found["github"].message


def test_parse_gh_scopes():
    assert parse_gh_scopes(_GH_OK) == ["checks:write", "pull-requests:write", "read:org"]
    assert parse_gh_scopes("Token scopes: none") == []
    assert parse_gh_scopes("no scopes here") is None


def test_json_output_and_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch):
    import swarmqa.doctor as doctor

    def fake_run_doctor(config_path, *, github=False, env=None):
        return run_doctor(config_path, github=github, env=_env(platform="linux", tools=()))

    monkeypatch.setattr(doctor, "run_doctor", fake_run_doctor)
    assert main(["doctor", "--json", "--config", str(tmp_path / "none.toml")]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    names = [check["name"] for check in payload["checks"]]
    assert names[0] == "python"
    assert set(payload["checks"][0]) == {"name", "status", "message", "fix"}

    def failing(config_path, *, github=False, env=None):
        return run_doctor(config_path, github=github, env=_env(python_version=(3, 9, 0), platform="linux"))

    monkeypatch.setattr(doctor, "run_doctor", failing)
    assert main(["doctor", "--config", str(tmp_path / "none.toml")]) == 1
    text = capsys.readouterr().out
    assert "[fail] python" in text
    assert "fix:" in text
