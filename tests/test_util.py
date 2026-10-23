"""swarmqa.util helpers."""

from __future__ import annotations


def test_child_env_puts_the_package_folder_first_once():
    import os
    from pathlib import Path

    import swarmqa
    from swarmqa.util import child_env

    folder = str(Path(swarmqa.__file__).resolve().parents[1])
    env = child_env({"PYTHONPATH": "/elsewhere", "HOME": "/h"})
    assert env["PYTHONPATH"].split(os.pathsep) == [folder, "/elsewhere"]
    assert env["HOME"] == "/h"
    assert child_env(env)["PYTHONPATH"] == env["PYTHONPATH"]
