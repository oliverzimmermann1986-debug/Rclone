# Rclone opt-in for the shared Prüfdienst controller

The base controller belongs to `Schoenmackers/deploy/ci-runner`. The deployed
HappySrv copy is the operational baseline; its four modified source digests are
recorded in `patch-manifest.json`. This directory carries only Rclone's reviewed
extension and exact-match integration patches. It does not replace or rewrite
the other project's sources or grant additional token permissions.

The host configuration enables pull-request discovery explicitly:

```json
"pullRequestRepos": ["oliverzimmermann1986-debug/Rclone"]
```

Without this setting, existing repositories retain default-branch/tag discovery.
The opt-in adds open PR heads against the default branch from the same repository;
forks, closed PRs, removed heads and unrelated base repositories are excluded.
The existing paginated GitHub API client is reused. Invalid commits and API
failures fail closed. No GitHub token is copied into test containers.

Queue results still belong to one exact SHA. Replacing a sole PR cancels its old
work; replacing one ref on a job still shared by another PR/main ref leaves that
job running. A new head gets its own result. A later tag can reuse a completed
successful check of the same SHA, while genuine failures and cleanup errors stay
blocking. Closing a PR does not abort an already queued/running check; that bounded
extra work may finish on its old SHA and cannot make a different SHA green.

The `rclone` image profile additionally requires a valid nonempty controller
report. A successful shell exit without a report cannot produce a green status,
including direct runs with a custom command. Other image profiles are unchanged.

## Isolated regression check

The Rclone image contains the pristine, root-owned `.cjs` controller snapshot and
its existing regression files under `/opt/rclone-ci/controller`, without secrets.
Inside the disposable container as its unprivileged user:

```sh
python infra/pruefdienst/central/stage.py \
  --source /opt/rclone-ci/controller \
  --destination /work/controller-tests
node --test /work/controller-tests/*.unit.test.cjs
```

The destination must be new. Staging verifies every recorded source hash and
requires each replacement anchor to occur exactly once before creating a copy.
A changed base, missing original tests, symlinks, a writable baseline, or a missing
controller module fails the gate. Existing controller tests run alongside the new
configuration, discovery, API failure, cancellation, SHA sharing and missing-report
regressions. The staging script is not a host deployment command.

## Host update and future controller updates

The coordinator applies the reviewed exact replacements to a private controller
backup/staging directory, checks syntax and the isolated test result, then deploys
only `pull-requests.cjs` and the four changed controller modules. Credentials,
firewall, Docker restrictions and unrelated repository configuration stay intact.
Restart the existing service only after its active queue is safe to restart.

A future canonical controller update must preserve or consciously reapply this
opt-in. Review any changed base and refresh the manifest and image snapshot
together; never remove a hash guard just to get a passing build. Keep the
`pruefdienst/release` status and native macOS check required on Rclone's protected
branch after the new exact-SHA result has passed.
