#!/usr/bin/env python3
"""Report or remove Actions runs that cannot produce a useful verdict.

Two rules, one report and one action budget:

* stale runs -- a queued or in-progress run whose pull requests are all closed
  or merged, older than the age limit.
* doomed runs -- an in-progress CI run whose required `ci-status` check is
  already decided against it, while sibling macOS jobs still hold scarce
  macOS concurrency.

The scheduled workflow is deliberately report-only.  Destructive cleanup is
available only from an explicit workflow_dispatch and is bounded by a small
action limit.  Runs with no pull request, or with any open pull request for
their commit, are always ignored by the stale rule.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, NamedTuple


API = "https://api.github.com"

# The doomed rule only understands one workflow: the one that owns the required
# `ci-status` check.  Naming the file rather than the display name keeps a
# renamed workflow from silently widening the rule.
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"

# `.github/workflows/ci-macos.yml` shards this job six ways; the reusable-call
# prefix makes the API name "macos / app-host unit tests (3/6)".  Substring
# matching covers both the prefix and the shard suffix.
DOOMED_JOB_NAME = "app-host unit tests"

# Blacksmith macOS pools, the Tart pools and the AWS M4 Pro builders are the
# capacity this rule reclaims.  Linux jobs in the same run are not scarce and
# are cancelled only as a side effect of cancelling the run.
MACOS_RUNNER_LABEL_MARKERS = ("macos", "tart", "m4pro")

# The `app-host unit tests` shards' own inputs, read off the job block in
# .github/workflows/ci-macos.yml: the XCTest sources it runs, the scripts that
# shard, compile, isolate and grade them, the known-failure quarantine list and
# the workflow that defines the job.  A run that changes any of these is an
# attempt to change what the shard does, so its remaining shards are the
# result someone is waiting for -- exactly the run that must not be cancelled.
#
# Product code under Sources/ is deliberately absent.  The suite exercises it,
# but nearly every pull request changes it, and a set that matches every pull
# request is not a rule.  The residual case -- a fix that lives entirely in
# product code -- is covered by JANITOR_OPT_OUT_LABEL instead.
APP_HOST_INPUT_PREFIXES = ("cmuxTests/", "scripts/ci/workloads/")
APP_HOST_INPUT_MARKERS = ("app-host", "app_host")
APP_HOST_INPUT_FILES = (".github/workflows/ci-macos.yml", "scripts/ci/cmux_unit_test_shard.py")

# Applied to the pull request, this exempts its runs from the doomed rule.  It
# exists for the fix whose diff the path list above cannot recognise.
JANITOR_OPT_OUT_LABEL = "no-janitor"

# A job that still holds its runner.  `completed` jobs have already released
# theirs, and an unknown status is not evidence that anything is held.
HOLDING_STATUSES = {"queued", "in_progress"}


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def classify_run(
    run: dict[str, Any], pull_requests: list[dict[str, Any]], *, now: dt.datetime, min_age_seconds: int
) -> str | None:
    """Return an eligible reason, or None when the run must be preserved."""
    # Commit-to-PR association also exists for main pushes and scheduled runs.
    # Only ordinary PR runs are eligible; unknown event/state facts fail closed.
    if run.get("event") != "pull_request" or run.get("status") not in {"queued", "in_progress"}:
        return None
    if not pull_requests or any(pr.get("state") != "closed" for pr in pull_requests):
        return None
    created_at = run.get("created_at")
    if not created_at:
        return None
    if (now - parse_time(created_at)).total_seconds() < min_age_seconds:
        return None
    if any(pr.get("merged_at") for pr in pull_requests):
        return "merged PR"
    if any(pr.get("state") == "closed" for pr in pull_requests):
        return "closed PR"
    return None


class Doomed(NamedTuple):
    """Why a run is unsalvageable, and what cancelling it gives back."""

    trigger: str
    holding: int

    @property
    def reason(self) -> str:
        return f"doomed by {self.trigger!r}; {self.holding} macOS job(s) still holding a runner"


def touches_app_host_inputs(changed_files: list[str]) -> bool:
    for name in changed_files:
        if name in APP_HOST_INPUT_FILES or name.startswith(APP_HOST_INPUT_PREFIXES):
            return True
        if any(marker in name for marker in APP_HOST_INPUT_MARKERS):
            return True
    return False


def is_macos_job(job: dict[str, Any]) -> bool:
    labels = job.get("labels") or []
    return any(marker in str(label).lower() for label in labels for marker in MACOS_RUNNER_LABEL_MARKERS)


def classify_doomed_run(
    run: dict[str, Any],
    jobs: list[dict[str, Any]],
    *,
    now: dt.datetime,
    grace_seconds: int,
    default_branch: str,
    changed_files: list[str] | None,
    labels: list[str],
) -> Doomed | None:
    """Return why a run is already decided against, or None to preserve it.

    `ci-status` needs the `macos` reusable-workflow call and accepts only
    `success` or `skipped` from it, so one `app-host unit tests` shard that
    concluded `failure` fails `ci-status` for the whole run.  Nothing later in
    the run can take that back: there is no retry above the job and no
    tolerated-failure list.  Every sibling macOS job still running is therefore
    occupying a macOS slot it cannot use to change the verdict.

    Cancelling reclaims only test shards, never a compile.  `app-host-unit-tests`
    needs `macos-compile-admission` to have concluded `success`, so the compiled
    app-host product has already been packaged, uploaded and seeded before any
    shard can fail: there is no in-flight compile to lose, whatever the state of
    compiled-product reuse.  That is why this rule names one job rather than
    reading the whole needs list.  A Linux guard failure decides `ci-status`
    just as firmly, but it lands while the macOS compile is still running, so a
    rule built on it would be discarding compiles -- safe only for as long as
    cross-run reuse stays broken (#13709), and destructive the moment #13718
    lands.

    Step-level `continue-on-error` needs no special case.  It is absorbed by the
    job conclusion the API reports -- a job whose only failed steps tolerate
    failure concludes `success` -- so requiring `conclusion == "failure"` at the
    job level already excludes it.  Job-level `continue-on-error` would not be
    absorbed, which is why tests/test_cleanup_stale_runs.py pins its absence
    from the app-host job.

    The run repairing the failing job is the one exception that matters.  Its
    remaining shards are the whole point of the run: someone needs to see which
    shards the fix moved, and cancelling after the first failure destroys
    exactly that.  So a run whose diff touches the shard's own inputs is
    preserved, as is one whose pull request carries the opt-out label.

    Every other fact fails closed: a different workflow, a re-run, a
    non-pull_request event, a default-branch run, a missing timestamp, an
    unknown diff, or a run with nothing left to reclaim is preserved.
    """
    if run.get("event") != "pull_request" or run.get("status") != "in_progress":
        return None
    # Only the workflow whose required check this argument is about.  A
    # merge_group run is already handled in real time by merge-group-fail-fast.
    if run.get("path") != CI_WORKFLOW_PATH:
        return None
    # A re-run replays a subset of jobs, so an older attempt's failure is not
    # evidence about the current one.
    if run.get("run_attempt") != 1:
        return None
    # Cancelling a default-branch run would destroy the only signal main gets
    # for that commit, and no evidence was gathered that it is safe.
    if not default_branch or run.get("head_branch") == default_branch:
        return None
    # An unknown diff is not evidence that the run is not the fix.
    if changed_files is None or touches_app_host_inputs(changed_files):
        return None
    if JANITOR_OPT_OUT_LABEL in labels:
        return None

    decided: tuple[str, dt.datetime] | None = None
    for job in jobs:
        if DOOMED_JOB_NAME not in str(job.get("name", "")):
            continue
        if job.get("status") != "completed" or job.get("conclusion") != "failure":
            continue
        completed_at = job.get("completed_at")
        if not completed_at:
            return None
        finished = parse_time(completed_at)
        # A grace window keeps the janitor away from a run that just turned red,
        # so a human still has a moment to look at it before the logs go.
        if (now - finished).total_seconds() < grace_seconds:
            continue
        if decided is None or finished < decided[1]:
            decided = (str(job.get("name")), finished)
    if decided is None:
        return None

    holding = sum(1 for job in jobs if job.get("status") in HOLDING_STATUSES and is_macos_job(job))
    if not holding:
        return None
    return Doomed(decided[0], holding)


class GitHub:
    def __init__(self, token: str, repo: str) -> None:
        self.repo = repo
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cmux-stale-run-janitor",
        }

    def request(self, method: str, path: str, *, missing_is_empty: bool = False) -> Any:
        request = urllib.request.Request(API + path, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status == 204:
                    return {}
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_is_empty:
                return []
            raise RuntimeError(f"GitHub API request failed ({error.code})") from error
        except urllib.error.URLError as error:
            raise RuntimeError("GitHub API request failed") from error

    def runs(self, status: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for page in range(1, 11):
            query = urllib.parse.urlencode({"status": status, "per_page": 100, "page": page})
            payload = self.request("GET", f"/repos/{self.repo}/actions/runs?{query}")
            page_runs = payload.get("workflow_runs", [])
            result.extend(page_runs)
            if len(page_runs) < 100:
                break
        return result

    def jobs(self, run_id: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for page in range(1, 11):
            query = urllib.parse.urlencode({"filter": "latest", "per_page": 100, "page": page})
            payload = self.request("GET", f"/repos/{self.repo}/actions/runs/{run_id}/jobs?{query}")
            page_jobs = payload.get("jobs", [])
            result.extend(page_jobs)
            if len(page_jobs) < 100:
                break
        return result

    def pull_request_files(self, number: Any) -> list[str] | None:
        """Changed paths, or None when the diff is too large to be summarised."""
        result: list[str] = []
        for page in range(1, 11):
            query = urllib.parse.urlencode({"per_page": 100, "page": page})
            payload = self.request("GET", f"/repos/{self.repo}/pulls/{number}/files?{query}")
            result.extend(str(entry.get("filename")) for entry in payload)
            if len(payload) < 100:
                return result
        # The files endpoint stops at 3000 files; a truncated list cannot show
        # that a path is absent, so report the diff as unknown.
        return None

    def default_branch(self) -> str:
        return str(self.request("GET", f"/repos/{self.repo}").get("default_branch") or "")

    def pull_requests_for_commit(self, sha: str) -> list[dict[str, Any]]:
        path = f"/repos/{self.repo}/commits/{urllib.parse.quote(sha, safe='')}/pulls"
        return self.request("GET", path, missing_is_empty=True)


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "true" if default else "false").lower() in {"1", "true", "yes"}


def evaluate_run(
    janitor: "GitHub",
    run: dict[str, Any],
    *,
    now: dt.datetime,
    min_age_seconds: int,
    grace_seconds: int,
    default_branch: str,
) -> tuple[str, bool] | None:
    """Return (reason, is_doomed) for an eligible run, or None to preserve it."""
    # A jobs request per run would triple this workflow's API cost, so fetch
    # them only where the doomed rule could apply.  classify_doomed_run repeats
    # these checks and adds the rest; this is a budget filter, not the policy.
    prs = janitor.pull_requests_for_commit(run.get("head_sha", ""))
    if (
        run.get("path") == CI_WORKFLOW_PATH
        and run.get("status") == "in_progress"
        and run.get("event") == "pull_request"
        and run.get("run_attempt") == 1
    ):
        # Exactly one pull request, or the diff the doomed rule must inspect is
        # ambiguous and the run is preserved by classify_doomed_run.
        number = prs[0].get("number") if len(prs) == 1 else None
        doomed = classify_doomed_run(
            run,
            janitor.jobs(run.get("id")),
            now=now,
            grace_seconds=grace_seconds,
            default_branch=default_branch,
            changed_files=janitor.pull_request_files(number) if number else None,
            labels=[str(label.get("name")) for label in (prs[0].get("labels") or [])] if number else [],
        )
        if doomed:
            return doomed.reason, True
    reason = classify_run(run, prs, now=now, min_age_seconds=min_age_seconds)
    return (reason, False) if reason else None


def describe(run: dict[str, Any], reason: str) -> str:
    return f"run {run.get('id')} on {run.get('head_branch')!r} ({run.get('status')}, {reason})"


def main() -> int:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("stale-run-janitor: GH_TOKEN and GH_REPO are required", file=sys.stderr)
        return 2

    try:
        min_age_minutes = int(os.environ.get("MIN_AGE_MINUTES", "1440"))
        max_actions = int(os.environ.get("MAX_ACTIONS", "25"))
        doomed_grace_minutes = int(os.environ.get("DOOMED_GRACE_MINUTES", "10"))
    except ValueError:
        print("stale-run-janitor: MIN_AGE_MINUTES, MAX_ACTIONS and DOOMED_GRACE_MINUTES must be integers",
              file=sys.stderr)
        return 2
    if min_age_minutes < 1 or max_actions < 1 or doomed_grace_minutes < 1:
        print("stale-run-janitor: age, grace and action limits must be positive", file=sys.stderr)
        return 2
    if max_actions > 25:
        print("stale-run-janitor: MAX_ACTIONS must not exceed 25", file=sys.stderr)
        return 2

    cleanup_requested = env_bool("CLEANUP") and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    janitor = GitHub(token, repo)
    now = dt.datetime.now(dt.timezone.utc)
    doomed: list[tuple[dict[str, Any], str]] = []
    stale: list[tuple[dict[str, Any], str]] = []
    try:
        default_branch = janitor.default_branch()
        for status in ("queued", "in_progress"):
            for run in janitor.runs(status):
                verdict = evaluate_run(janitor, run, now=now, min_age_seconds=min_age_minutes * 60,
                                       grace_seconds=doomed_grace_minutes * 60, default_branch=default_branch)
                if verdict:
                    (doomed if verdict[1] else stale).append((run, verdict[0]))
    except RuntimeError as error:
        print(f"stale-run-janitor: {error}", file=sys.stderr)
        return 1

    # Doomed runs are holding macOS slots right now; a stale run has been
    # stranded for at least a day and keeps until the next cycle.  Spend the
    # shared action budget on the urgent ones first.
    eligible = doomed + stale
    mode = "CLEANUP" if cleanup_requested else "DRY-RUN"
    print(f"stale-run-janitor: {mode}; doomed={len(doomed)}; stale={len(stale)}; limit={max_actions}")
    if not cleanup_requested:
        for run, reason in eligible[:max_actions]:
            print(f"would process {describe(run, reason)}")
        return 0

    processed = 0
    failures = 0
    for run, reason in eligible[:max_actions]:
        run_id = run.get("id")
        endpoint = f"/repos/{repo}/actions/runs/{run_id}"
        try:
            # Runs can finish, jobs can turn green and PRs can reopen after
            # inventory collection, so both rules are re-decided from fresh
            # state immediately before the request that cannot be undone.
            current = janitor.request("GET", endpoint)
            verdict = evaluate_run(janitor, current, now=dt.datetime.now(dt.timezone.utc),
                                   min_age_seconds=min_age_minutes * 60,
                                   grace_seconds=doomed_grace_minutes * 60, default_branch=default_branch)
            if not verdict:
                print(f"preserved run {run_id}: no longer eligible")
                continue
            method = "DELETE" if current.get("status") == "queued" else "POST"
            janitor.request(method, endpoint + ("/cancel" if method == "POST" else ""))
            processed += 1
            print(f"processed {describe(current, verdict[0])}")
        except RuntimeError as error:
            failures += 1
            print(f"failed run {run_id}: {error}", file=sys.stderr)
    print(f"stale-run-janitor: processed={processed}; failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
