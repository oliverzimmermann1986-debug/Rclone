# GSD Debug Knowledge Base

Resolved debug sessions. Used by `gsd-debugger` to surface known-pattern hypotheses at the start of new investigations.

---

## rezepte-permission-denied — Recipe sync and dashboard measurement failed on protected child directories
- **Date:** 2026-08-26
- **Error patterns:** permission denied, failed to open directory, Failed to lsjson, missing local count and size, `.work`, `backups`
- **Root cause(s):** The service identity could not traverse transient `.work` or required `backups`; the pair did not exclude transient working state, and the required backup tree lacked service-group access. Independently, storage measurement did not reuse sync filters and discarded parseable partial JSON on non-zero rclone exits.
- **Fix:** Excluded `/.work/**`, granted least-privilege group access to the required backup tree, and made storage measurement apply pair filters, key its cache by those filters, and retain partial metrics with an explicit warning.
- **Files changed:** `app/routes/api_storage.py`, `tests/test_storage_sizes.py`, live pair configuration, live backup-tree metadata, hot-deployed backend module
- **Why not caught:** No test covered permission-denied descendants with partial rclone JSON, no gate asserted sync/overview filter parity, and deployment validation lacked a service-identity traversal check for newly produced backup directories.
- **Recurrence guard:** Regression tests in `tests/test_storage_sizes.py` cover partial JSON, non-caching of partial results, and effective-filter parity; the live pair excludes `/.work/**`, while setgid service-group ownership preserves future backup-file access.
---
