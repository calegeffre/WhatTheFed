from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from unittest.mock import call, patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build-static-site.py"


def _script_namespace() -> dict[str, object]:
    return runpy.run_path(str(SCRIPT_PATH), run_name="build_static_site")


def test_ingestion_command_retries_then_succeeds() -> None:
    run_ingestion_command = _script_namespace()["run_ingestion_command"]
    failure = subprocess.CalledProcessError(1, ["python", "-m", "example"])

    with (
        patch("subprocess.run", side_effect=[failure, failure, None]) as run,
        patch("time.sleep") as sleep,
    ):
        run_ingestion_command(
            label="Example",
            module="example",
            arguments=["--value", "1"],
        )

    assert run.call_count == 3
    assert sleep.call_args_list == [call(10), call(30)]


def test_ingestion_command_raises_after_last_attempt() -> None:
    run_ingestion_command = _script_namespace()["run_ingestion_command"]
    failure = subprocess.CalledProcessError(1, ["python", "-m", "example"])

    with (
        patch("subprocess.run", side_effect=failure) as run,
        patch("time.sleep") as sleep,
    ):
        try:
            run_ingestion_command(
                label="Example",
                module="example",
                arguments=[],
            )
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("Expected the final ingestion failure to be raised.")

    assert run.call_count == 3
    assert sleep.call_args_list == [call(10), call(30)]
