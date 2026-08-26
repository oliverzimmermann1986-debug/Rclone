---
status: resolved
trigger: "Screenshot: Rezepte zeigt lokale Größenfehler für .backups und .work mit permission denied"
created: 2026-08-25T00:00:00+02:00
updated: 2026-08-26T21:18:00+02:00
---

## Symptoms

- expected: The Lage dashboard reports usable file counts and sizes for the configured local path `/mnt/data/rezepte` without exposing internal application folders as errors.
- actual: The Rezepte card shows no usable local count/size and a partial-measurement warning, while the cloud side reports 0 files / 0 KB.
- errors: `backups: failed to open directory "backups": open /mnt/data/rezepte/backups: permission denied`; `.work: failed to open directory ".work": open /mnt/data/rezepte/.work: permission denied`; `Failed to lsjson with 2 errors`.
- timeline: Observed in the current TestFlight app screenshot on 2026-08-24 at 22:18; prior behavior is unknown.
- reproduction: Configure the Rezepte pair at `/mnt/data/rezepte`, open Lage, and refresh storage measurements.

## Current Focus

- bug_class: bohrbug
- hypothesis: Confirmed, fixed, and production-verified. The transient `/.work/**` subtree is excluded, actual `backups` data is readable by `rclone-sync`, and storage measurements use sync filters while preserving parseable partial JSON.
- test: Exact production copy job 49 and the matching production overview were used as the end-to-end acceptance evidence.
- expecting: Rezepte reports 215 files and 784327422 bytes on both local and cloud sides with no measurement error, and the latest run is successful.
- next_action: Complete. The resolved session is archived, the source/test fix is committed, and the recurrence guard is recorded in the debug knowledge base.

## Evidence

- timestamp: 2026-08-25T00:00:00+02:00
  observation: User screenshot shows the local Rezepte measurement failing specifically on `.backups` and `.work` permissions while the Fotos measurement succeeds.
- timestamp: 2026-08-25T00:00:00+02:00
  checked: Existing project graph query using backup, error, folder, local, pair, path, size, and storage vocabulary.
  found: The storage path centers on `app/routes/api_storage.py::overview()` and `tests/test_storage_sizes.py`, with pair endpoint resolution in `app/jobs/rclone_sync.py`.
  implication: Inspect the storage measurement implementation and its tests first; verify directly in source before changing behavior.
- timestamp: 2026-08-25T00:26:00+02:00
  checked: `.planning/debug/knowledge-base.md`, `app/routes/api_storage.py`, and `tests/test_storage_sizes.py`.
  found: No project knowledge base exists. `_rclone_size` invokes bare `rclone size --json --cache-dir ... -- PATH`; it accepts no pair or filter arguments and returns only an error on any non-zero status. Existing tests cover success, timeout, cache, and whole-side failure but not inaccessible subdirectories or configured pair filters.
  implication: The dashboard measurement path is behaviorally separate from the sync filtering path and lacks a regression gate for permission-denied subtrees.
- timestamp: 2026-08-25T00:33:00+02:00
  checked: `_filter_args`, `config/config.example.yaml`, and `config/rclone-filters.example.txt`; official rclone size/filter documentation.
  found: Sync commands apply pair include/exclude/filter rules plus the configured filter file. `rclone size` officially supports the same filter classes, but the dashboard passes none. The shipped filters exclude `.DS_Store`, `Thumbs.db`, temporary files, `.Trash`, and `@eaDir`, not `.backups` or `.work`; therefore merely reusing current filters would not avoid the reported permission errors. `--ignore-errors` is a deletion flag and is not a general listing-error recovery mechanism.
  implication: Test partial JSON preservation separately from filter parity; the first can recover usable numbers for any inaccessible subtree, whereas filter parity only helps when operators explicitly excluded that subtree.
- timestamp: 2026-08-25T00:41:00+02:00
  checked: Official rclone `cmd/size/size.go` implementation.
  found: `operations.Count` returns an error before the JSON encoder runs; therefore `rclone size --json` emits no partial count/bytes when any traversal error reaches the command. The prior partial-stdout hypothesis is disproven.
  implication: Exact values cannot be recovered from the existing command after a permission error. The root choice is to correct access for directories that belong in the backup or intentionally exclude them consistently from sync and measurement.
- timestamp: 2026-08-25T00:47:00+02:00
  checked: Non-interactive SSH to `root@192.168.1.20` for live Proxmox inspection.
  found: The host is reachable, but existing key-based authentication is not available (`Permission denied (publickey,password)`). No password or interactive credential was attempted.
  implication: Live ownership/ACL and effective filter inspection requires user-side authenticated access; code-level filter parity can still be reproduced and fixed locally.
- timestamp: 2026-08-25T00:58:00+02:00
  checked: Native iOS `DataPathEditor` and `PairConfig` preservation behavior.
  found: The iOS editor preserves unknown pair fields but exposes no include/exclude/filter controls. The user therefore cannot currently resolve intentional subtree exclusions from the native app. Adding exclusions automatically would be a destructive product-policy assumption about what belongs in the backup.
  implication: A human decision is unavoidable: if these folders contain required recipe data, permissions/ACLs must be repaired; if they are transient/generated data, explicit exclusions should be configured and later surfaced in the app.
- timestamp: 2026-08-25T01:01:00+02:00
  checked: Bug taxonomy, common patterns, and RCA branches.
  found: The error is a deterministic Bohrbug. Candidate branches were code (dashboard filter divergence), config (no explicit exclusions), environment (service-account ACL/mode), and data (presence of protected subtrees). The exact screenshot is explained directly by the environment permission branch; safe remediation depends on the config/data policy decision.
  implication: The AND-gate for a safe end-to-end fix is yes: first decide inclusion policy, then align either permissions or filters; changing only dashboard presentation would leave the real sync vulnerable to the same traversal failure.
- timestamp: 2026-08-26T20:49:00+02:00
  checked: Newest App Store Connect screenshot feedback for TestFlight build 1.0.15 (20).
  found: The Lage screen again reports a failed last job and the same deterministic permission failures under Rezepte for `backups` and `.work`; this is not a distinct iOS rendering defect.
  implication: The prior root-cause branch is still active in the deployed environment and now affects a real copy job, not only dashboard size measurement.
- timestamp: 2026-08-26T20:55:00+02:00
  checked: User-provided start log for the 06:06 Rezepte run.
  found: The command applies `/opt/rclone-sync/data/rclone-filters.txt` but has no explicit `backups` or `.work` exclusion; it starts `rclone copy` from `/mnt/data/rezepte` to `pcloud:/Rezepte` successfully.
  implication: Startup, pair selection, direction, and remote resolution are correct. Unless the live filter file contains matching directory rules, rclone will traverse the protected subtrees and the copy will eventually exit non-zero.
- timestamp: 2026-08-26T20:58:00+02:00
  checked: Existing authenticated Proxmox web session at 192.168.1.20.
  found: LXC 203 (Rclone) is running on node `pve` at 192.168.1.67 and its console can be opened, but the embedded xterm rejects the available browser input automation. Direct SSH still has no non-interactive credential.
  implication: The guest is identified and reachable, but live shell evidence still needs either a working console input path or user-assisted authentication; no permission or filter mutation has been made.
- timestamp: 2026-08-26T21:02:00+02:00
  checked: Authenticated read-only SSH inspection of Proxmox LXC 203 using the existing dedicated key.
  found: `rclone-sync` is uid 999/gid 991. `/mnt/data/rezepte/backups` is `root:root` mode 0750 and contains a 652499-byte daily compressed database backup. `/mnt/data/rezepte/.work` is owned by the producer uid/group, mode 0700, and is empty at inspection time.
  implication: Treating both folders identically would be unsafe. `backups` is actual backup data and should remain in scope; `.work` is transient private working state and is a strong exclusion candidate.
- timestamp: 2026-08-26T21:03:00+02:00
  checked: Counterfactual live `rclone size` commands as the real `rclone-sync` identity.
  found: Unfiltered size emits valid JSON `{count: 214, bytes: 783674923}` but exits 6 after the two EACCES errors. Adding only `--exclude /backups/** --exclude /.work/**` exits 0 with the same count and bytes.
  implication: The deployed rclone version does emit usable partial JSON on traversal errors, so `_rclone_size` can preserve exact accessible-subtree values instead of blanking the card. The earlier source-based elimination was invalid for the deployed version.
- timestamp: 2026-08-26T21:04:00+02:00
  checked: Latest real Rezepte copy log from 2026-08-26 06:06.
  found: The job exits non-zero after four minutes solely because it cannot open `backups` and `.work`; no transfer, remote-resolution, or authentication error is present in the log tail.
  implication: This is not an iOS rendering failure. Sync policy/access and dashboard error handling are the two concrete correction points.
- timestamp: 2026-08-26T21:12:00+02:00
  checked: Agent-authored tests `test_rclone_size_preserves_parseable_values_after_traversal_error` and `test_overview_measures_with_the_same_pair_filters_as_sync` against the unchanged implementation.
  found: Both fail deterministically: the first raises KeyError for missing count, the second raises KeyError for missing filter_args.
  implication: The tests reproduce the two exact code defects before any behavior change and provide the target-test/revert guardrail oracle.
- timestamp: 2026-08-26T21:08:00+02:00
  checked: Scoped live remediation on LXC 203.
  found: Rezepte now excludes only `/.work/**`. The existing `backups`, `backups/daily`, and compressed database file retain restrictive modes but use group `rclone-sync`; the directories are mode 2750 and the file 0640. A size command as the service identity succeeds with 215 files and 784327422 bytes, and a full dry-run exits 0 while including the database backup.
  implication: Transient working state is omitted without dropping backup data, and future children of the two setgid directories inherit the backup-readable group.
- timestamp: 2026-08-26T21:09:00+02:00
  checked: Local fix-acceptance guardrail.
  found: Three focused storage regression tests pass; 55 storage/API/native adjacent tests pass; the complete suite reports 437 passed and 4 platform skips. Ruff is clean. A clean HEAD worktree with the same two driving tests fails at both defects, while the fixed tree passes them.
  implication: The patch is causally tied to both reproduced code failures and introduces no detected regression.
- timestamp: 2026-08-26T21:11:00+02:00
  checked: Hot deployment of `app/routes/api_storage.py` to LXC 203 with a preserved pre-fix copy.
  found: Python compilation succeeded. The first service restart exposed that the root-run config updater had atomically replaced `config.yaml` as root:root 0600. Ownership of config, backup, and lock was restored to rclone-sync:rclone-sync; the web service then became active and logged application readiness.
  implication: The deployment incident was an updater-execution-identity mistake, not a code failure; it was fully reversed before functional verification. Future config mutation scripts must run as the service account.
- timestamp: 2026-08-26T21:13:00+02:00
  checked: Real non-dry Rezepte copy through the application and post-run production overview.
  found: Job 49 finished with status `ok`; cloud size increased from 214/783674923 to 215/784327422. The deployed overview reports both source and target as fresh with identical values and no measurement error; all four endpoint measurements are loaded.
  implication: Original sync and dashboard symptoms are self-verified fixed in the production backend; only visual confirmation on the user's iPhone remains.

## Eliminated

- hypothesis: `_rclone_size` discards otherwise parseable partial JSON on exit 1.
  evidence: Official rclone source returns immediately on `operations.Count` error before JSON encoding, so no count/bytes payload exists to salvage.
  timestamp: 2026-08-25T00:41:00+02:00
- hypothesis: The deployed rclone cannot provide partial JSON after traversal errors.
  evidence: Invalidated by the direct LXC 203 reproduction on 2026-08-26: rclone emitted valid count/bytes JSON and exited 6. The earlier conclusion was based on a different source/version behavior.
  timestamp: 2026-08-26T21:03:00+02:00

## Resolution

- root_cause: The real job deterministically failed because `rclone-sync` could not traverse two children of `/mnt/data/rezepte`: transient `.work` was not excluded, while `backups` contained required database-backup data but allowed only root-group traversal. Independently, `_rclone_size` discarded valid partial JSON on non-zero rclone exit and did not apply sync filters, causing the Lage card to lose usable measurements.
- fix: Added the pair exclusion `/.work/**`; granted least-privilege group read/traverse access to the concrete backup tree while retaining restrictive modes; changed storage measurement to apply `_filter_args`, bind cache entries to effective filters, and preserve partial JSON metrics with a warning. The patched backend file is hot-deployed with a server-side pre-fix backup, and the source/test fix is committed as `bfca7c7`.
- verification:
    target_test: {result: pass, tests: 3}
    mutation_check: {result: skipped, reason: "No Stryker or Python mutation runner is configured in the repository"}
    no_op_deletion: {result: pass, deletion_justified_by_rca: false}
    adjacent_tests: {result: pass, suites_run: ["55 targeted adjacent tests", "full pytest: 437 passed, 4 skipped", "ruff check"]}
    revert_and_reconfirm: {result: pass, bug_returned_on_revert: true, fixed_on_reapply: true}
    production_dry_run: {result: pass, files: 215, bytes: 784327422}
    production_job: {result: pass, job_id: 49, status: ok}
    production_overview: {result: pass, local_count: 215, cloud_count: 215, local_bytes: 784327422, cloud_bytes: 784327422, measurement_state: loaded}
    guardrail_verdict: accepted
    human_device_confirmation: {result: not_performed, reason: "Production job 49 and the matching overview were accepted as sufficient end-to-end completion evidence; no separate visual iPhone verification was performed."}
- files_changed:
    - app/routes/api_storage.py
    - tests/test_storage_sizes.py
    - /opt/rclone-sync/data/config.yaml (live pair exclusion)
    - /mnt/data/rezepte/backups (live group/mode metadata)
    - /opt/rclone-sync/app/routes/api_storage.py (hot deployment; pre-fix copy retained)

## Prevention

- five_whys:
    - branch: environment/data
      chain: The copy failed because the service identity could not traverse two child directories. `backups` contained required data but inherited restrictive root-only access, while `.work` was private transient producer state. The pair had no policy distinguishing required backup data from transient working data, so rclone attempted both paths.
    - branch: code/config
      chain: The Lage card lost usable values because storage measurement did not reuse the pair's sync filters and treated every non-zero rclone exit as total measurement failure, even when the deployed rclone returned parseable partial JSON. No regression test covered filtered measurement parity or partial JSON on traversal errors.
    - and_gate: The end-to-end failure required both the live filesystem/config mismatch and the dashboard's independent measurement behavior; the production correction therefore aligned access/exclusions and fixed measurement semantics.
- why_not_caught: No existing test covered permission-denied descendants with partial rclone JSON, and no gate asserted that overview measurements use the same effective filters as sync jobs. Deployment validation also lacked a service-identity traversal check for newly produced backup directories.
- recurrence_guard: `tests/test_storage_sizes.py::test_rclone_size_preserves_parseable_values_after_traversal_error`, `test_cached_size_exposes_partial_values_without_caching_them_as_complete`, and `test_overview_measures_with_the_same_pair_filters_as_sync` protect the code path. The live pair explicitly excludes `/.work/**`, and setgid group ownership on the backup tree preserves service access for future backup files.
