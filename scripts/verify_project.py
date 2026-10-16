"""Run the local quality gate: Ruff, strict mypy, pytest, and dashboard JS syntax."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TYPED_PACKAGES = [
    "analytics",
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


def main() -> int:
    """Run each check from the project root and return nonzero if any fail."""
    failed: list[str] = []
    for name, command in _checks():
        print(f"[run ] {name}: {' '.join(command[1:])}", flush=True)
        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)  # noqa: S603
        status = "pass" if result.returncode == 0 else "FAIL"
        print(f"[{status}] {name}", flush=True)
        if result.returncode != 0:
            failed.append(name)
    if failed:
        print(f"Verification failed: {', '.join(failed)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
