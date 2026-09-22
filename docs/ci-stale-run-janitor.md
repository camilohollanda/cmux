# Actions run janitor

`.github/workflows/ci-stale-run-janitor.yml` reports runs that cannot produce a
useful verdict every 30 minutes, under two rules that share one action budget.

## Stale runs

Runs stranded by a closed pull request. It associates each run's commit with pull requests using
the GitHub API, then considers only runs older than 24 hours whose associated
pull requests are closed or merged. Only `pull_request` event runs qualify;
push, scheduled, merge-group, manually dispatched, and unknown events are
preserved even when their commit is associated with a merged PR. Runs with
no pull request or with any open or unknown-state pull request are preserved.
Cleanup refreshes the run and PR states immediately before each action;
completed runs and reopened PRs are skipped.

## Doomed runs

An in-progress `ci.yml` run whose required `ci-status` check is already decided
against it, while sibling macOS jobs still hold macOS concurrency. This is
structural, not statistical: `ci-status` declares `needs: [changes,
static-preflight, guards, ghosttykit-release-check, cli, web, linux-preflight,
macos-debounce, macos, tests]` and accepts only `success` or `skipped` from
each, so a failed `app-host unit tests` shard fails the `macos`
reusable-workflow call and the required check by construction. A census of the
299 CI runs created between 2026-09-22T06:05Z and 17:00Z confirms the
construction behaves as written: 21 runs had such a shard failure, `ci-status`
concluded `failure` in all 21, and their sibling macOS jobs ran on for 1,522
macOS runner-minutes after the verdict was already fixed.

Cancelling reclaims only test shards, never a compile. `app-host-unit-tests`
needs `macos-compile-admission` to have concluded `success`, so the compiled
app-host product has already been packaged, uploaded and seeded before any
shard can fail; there is no in-flight compile to lose. That is why the rule
names one job instead of reading the whole `needs` list. A Linux guard failure
decides `ci-status` just as firmly, but it lands while the macOS compile is
still running, so a rule built on it would be discarding compiles. That is safe
today only because cross-run compiled-product reuse never hits on a pull
request (#13709), and it stops being safe when #13718 lands.

The rule reads job conclusions, not step conclusions, so a step-level
`continue-on-error` failure is already excluded; `tests/test_cleanup_stale_runs.py`
pins the absence of a job-level `continue-on-error` that the conclusion would
not absorb. Re-runs, non-`pull_request` events, other workflows, default-branch
runs, runs with no macOS job left to reclaim, and runs whose failure is newer
than `doomed_grace_minutes` are all preserved.

So is the run that is trying to repair the failing job. A pull request whose
diff touches the shards' own inputs -- `cmuxTests/`, the shard, isolation,
compile and grading scripts under `scripts/ci/`, the known-failure quarantine
list, or `ci-macos.yml` -- is exactly the run whose remaining shards someone is
waiting on, so it is never cancelled. A diff that cannot be read (no pull
request, several pull requests, or more than 3000 files) preserves the run too.
For a fix that lives entirely in product code, which no path list can pick out,
apply the `no-janitor` label to the pull request.

## Operating it

Scheduled executions are always dry runs. To perform cleanup, start the
workflow manually, leave the age and action limits conservative, and set
`cleanup` to true. Queued runs are deleted; in-progress runs are cancelled.
Doomed runs are always in progress, so they are cancelled, never deleted, and
`ci-status` then reports the failure the run was already headed for rather than
staying pending. Doomed runs are offered the shared action budget first,
because a stale run keeps until the next cycle and a doomed run is holding a
macOS slot now. The workflow caps each invocation at 25 actions and grants
`actions: write` only to this trusted control-plane workflow.
