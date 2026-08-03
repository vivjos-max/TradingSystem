"""Executable contracts for the pinned Windows bootstrap toolchain."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
BOOTSTRAP_SCRIPT = REPOSITORY_ROOT / "scripts" / "bootstrap.ps1"


def _fake_node(tmp_path: Path, *, node_version: str, pnpm_version: str) -> tuple[Path, Path]:
    executable = tmp_path / "node.cmd"
    executable.write_text(
        "\n".join(
            (
                "@echo off",
                f'if "%~1"=="--version" (echo v{node_version}& exit /b 0)',
                f'if "%~2"=="--version" (echo {pnpm_version}& exit /b 0)',
                "exit /b 1",
            )
        ),
        encoding="utf-8",
    )
    pnpm_entrypoint = tmp_path / "pnpm.cjs"
    pnpm_entrypoint.write_text("// fake pnpm entrypoint", encoding="utf-8")
    return executable, pnpm_entrypoint


def _validate_bootstrap(node: Path, pnpm: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BOOTSTRAP_SCRIPT),
            "-PythonExecutable",
            sys.executable,
            "-NodeExecutable",
            str(node),
            "-PnpmExecutable",
            str(pnpm),
            "-ValidateOnly",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_bootstrap_accepts_the_pinned_bundled_node_and_pnpm_versions(tmp_path: Path) -> None:
    node, pnpm = _fake_node(tmp_path, node_version="24.14.0", pnpm_version="11.9.0")

    result = _validate_bootstrap(node, pnpm)

    assert result.returncode == 0, result.stderr
    assert "Node.js 24.14.0 and pnpm 11.9.0" in result.stdout


@pytest.mark.parametrize(
    ("node_version", "pnpm_version", "expected_message"),
    [
        ("24.13.0", "11.9.0", "locked to Node.js 24.14.0"),
        ("24.14.0", "11.8.0", "locked to pnpm 11.9.0"),
    ],
)
def test_bootstrap_refuses_unpinned_node_or_pnpm(
    tmp_path: Path,
    node_version: str,
    pnpm_version: str,
    expected_message: str,
) -> None:
    node, pnpm = _fake_node(
        tmp_path,
        node_version=node_version,
        pnpm_version=pnpm_version,
    )

    result = _validate_bootstrap(node, pnpm)

    assert result.returncode != 0
    assert expected_message in result.stderr


def test_bootstrap_executes_both_frozen_locks_with_the_selected_runtimes() -> None:
    command = (
        "$tokens=$null; $errors=$null; "
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{BOOTSTRAP_SCRIPT}',"
        "[ref]$tokens,[ref]$errors); "
        "$ast.FindAll({param($node) $node -is "
        "[System.Management.Automation.Language.CommandAst]},$true) | "
        "ForEach-Object { $_.Extent.Text }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    commands = result.stdout
    assert "& $venvPython -m pip install --require-hashes --requirement $lockFile" in commands
    assert (
        "& $NodeExecutable $PnpmExecutable install --frozen-lockfile "
        "--config.confirmModulesPurge=false"
    ) in commands
