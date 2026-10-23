"""Run the local quality gate: Ruff, strict mypy, pytest, and dashboard JS syntax."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TYPED_PACKAGES = [
    "analytics",
    "bayesian",
    "comparison",
    "dashboard",
    "enrichment",
    "extractors",
    "models",
    "pipeline",
    "processors",
    "profiles",
    "storage",
    "utils",
]


def _checks() -> list[tuple[str, list[str]]]:
    python = sys.executable
    checks = [
        ("ruff", [python, "-m", "ruff", "check", "."]),
        ("mypy", [python, "-m", "mypy", *TYPED_PACKAGES]),
        ("pytest", [python, "-m", "pytest", "-q"]),
    ]
    node = shutil.which("node")
    if node:
        for script in ("app.js", "resume-review.js"):
            checks.append(
                (f"dashboard-js:{script}", [node, "--check", f"dashboard/static/{script}"])
            )
    else:
        print("[skip] dashboard-js: Node.js is not installed")
    return checks


# Generous limits: a hung tool (for example a Node.js shim waiting on a prompt) fails
# the check instead of blocking the whole gate forever.
TIMEOUT_SECONDS = {"ruff": 300, "mypy": 900, "pytest": 900}
DEFAULT_TIMEOUT_SECONDS = 60


def main() -> int:
    """Run each check from the project root and return nonzero if any fail."""
    failed: list[str] = []
    try:
        for name, command in _checks():
            print(f"[run ] {name}: {' '.join(command[1:])}", flush=True)
            timeout = TIMEOUT_SECONDS.get(name, DEFAULT_TIMEOUT_SECONDS)
            try:
                result = subprocess.run(  # noqa: S603
                    command, cwd=PROJECT_ROOT, check=False, timeout=timeout
                )
                passed = result.returncode == 0
            except subprocess.TimeoutExpired:
                print(f"[FAIL] {name}: no result after {timeout} seconds", flush=True)
                failed.append(name)
                continue
            except OSError as exc:
                print(f"[FAIL] {name}: could not start ({exc})", flush=True)
                failed.append(name)
                continue
            print(f"[{'pass' if passed else 'FAIL'}] {name}", flush=True)
            if not passed:
                failed.append(name)
    except KeyboardInterrupt:
        print("\nVerification interrupted.", flush=True)
        return 130
    if failed:
        print(f"Verification failed: {', '.join(failed)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
