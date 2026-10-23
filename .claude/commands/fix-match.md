---
description: Investigate and fix a wrong ✓/◐/✗ call in the application comparison
argument-hint: <posting title / employer> — <requirement line> — <expected status>
---
The owner reports a mismatched requirement in the Phase 6.3 comparison: $ARGUMENTS

1. Find the posting in Silver (under `JMIP_PIPELINE_DATA_DIR`) and the stored result in
   `data/private/comparisons/latest.json`. Read only the item for that requirement; do
   not paste resume text into the conversation beyond the matched words.
2. Explain why it was classified and scored that way (kind, concepts, named products,
   matched/missing terms, field match).
3. Fix it in `config/comparison.yaml` (concepts, synonyms, traits, fields) when possible,
   otherwise in `comparison/`. Bump the comparison config `version`.
4. Add a regression test in `tests/test_comparison.py` using the synthetic resume only.
5. Run `/verify`, then `scripts/compare_applications.py`, and confirm the line now has
   the expected status. Commit and push to master.
