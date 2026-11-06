---
description: Format, then run the full JMIP verification gate and fix anything that fails
---
1. Run `.venv/Scripts/python.exe -m ruff format .`
2. Run `.venv/Scripts/python.exe scripts/verify_project.py`.
3. If any check fails (ruff, mypy, pytest, node --check), fix the cause (not the test,
   unless the test is wrong), and rerun until it prints `All checks passed.`
4. Report what failed and what you changed.
