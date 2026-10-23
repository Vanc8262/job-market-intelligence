---
description: Pull the latest collected data, rebuild Gold, and rerun the application comparison
---
1. Run `.venv/Scripts/python.exe scripts/sync_data.py` and read its JSON output.
2. Summarize: data commit, records analyzed, failures, profile source, and comparison
   band counts (strong/moderate/limited/unknown). Do not print resume content.
3. If it fails, diagnose from the error (missing `JMIP_PIPELINE_DATA_DIR`, git errors in
   `..\jmip-data`, validation errors) and propose the fix.
4. Remind the owner to restart the dashboard (`-m dashboard.app`) and hard-refresh.
