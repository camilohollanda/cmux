import datetime as dt
import importlib.util
import pathlib
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts/ci/cleanup-stale-runs.py"
WORKFLOW = pathlib.Path(__file__).parents[1] / ".github/workflows/ci-stale-run-janitor.yml"
MACOS_WORKFLOW = pathlib.Path(__file__).parents[1] / ".github/workflows/ci-macos.yml"
SPEC = importlib.util.spec_from_file_location("cleanup_stale_runs", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ClassifyRunTests(unittest.TestCase):
    now = dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc)
    run_data = {"created_at": "2026-09-18T00:00:00Z", "status": "queued", "event": "pull_request"}

    def test_non_pr_runs_are_preserved_at_a_merged_commit(self):
        prs = [{"state": "closed", "merged_at": "2026-09-18T12:00:00Z"}]
        for event in ("push", "schedule", "workflow_dispatch", "merge_group", "pull_request_target", None):
            with self.subTest(event=event):
                run = dict(self.run_data, event=event)
                self.assertIsNone(MODULE.classify_run(run, prs, now=self.now, min_age_seconds=3600))

    def test_completed_pr_run_is_preserved(self):
        prs = [{"state": "closed", "merged_at": "2026-09-18T12:00:00Z"}]
        run = dict(self.run_data, status="completed")
        self.assertIsNone(MODULE.classify_run(run, prs, now=self.now, min_age_seconds=3600))

    def test_merged_pr_is_eligible(self):
        prs = [{"state": "closed", "merged_at": "2026-09-18T12:00:00Z"}]
        self.assertEqual(MODULE.classify_run(self.run_data, prs, now=self.now, min_age_seconds=3600), "merged PR")

    def test_closed_pr_is_eligible(self):
        prs = [{"state": "closed", "merged_at": None}]
        self.assertEqual(MODULE.classify_run(self.run_data, prs, now=self.now, min_age_seconds=3600), "closed PR")

    def test_open_pr_is_preserved_even_when_old(self):
        prs = [{"state": "open", "merged_at": None}]
        self.assertIsNone(MODULE.classify_run(self.run_data, prs, now=self.now, min_age_seconds=3600))

    def test_no_pr_is_preserved(self):
        self.assertIsNone(MODULE.classify_run(self.run_data, [], now=self.now, min_age_seconds=3600))

    def test_recent_terminal_run_is_preserved(self):
        recent = {"created_at": "2026-09-19T00:00:00Z", "status": "queued", "event": "pull_request"}
        prs = [{"state": "closed", "merged_at": None}]
        self.assertIsNone(MODULE.classify_run(recent, prs, now=self.now, min_age_seconds=3600))


class CleanupRevalidationTests(unittest.TestCase):
    def test_cleanup_uses_refreshed_run_status(self):
        original = dict(ClassifyRunTests.run_data, id=123, head_sha="old-head")
        closed = [{"state": "closed", "merged_at": "2026-09-18T12:00:00Z"}]
        for status, method, suffix in [("queued", "DELETE", ""), ("in_progress", "POST", "/cancel")]:
            with self.subTest(status=status):
                api = mock.Mock()
                api.runs.side_effect = [[original], []]
                api.pull_requests_for_commit.return_value = closed
                api.request.side_effect = [dict(original, status=status), {}]
                environment = {"GH_TOKEN": "test-token", "GH_REPO": "test/repo",
                               "CLEANUP": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"}
                with mock.patch.dict(MODULE.os.environ, environment, clear=True), mock.patch.object(MODULE, "GitHub", return_value=api):
                    self.assertEqual(MODULE.main(), 0)
                self.assertEqual(api.request.call_args_list, [
                    mock.call("GET", "/repos/test/repo/actions/runs/123"),
                    mock.call(method, "/repos/test/repo/actions/runs/123" + suffix),
                ])

    def test_changed_run_or_reopened_pr_is_preserved(self):
        original = dict(ClassifyRunTests.run_data, id=123, head_sha="old-head")
        closed = [{"state": "closed", "merged_at": "2026-09-18T12:00:00Z"}]
        scenarios = [
            (dict(original, status="completed"), closed),
            (dict(original, event="push"), closed),
            (original, [{"state": "open"}]),
            (original, [{}]),
        ]
        for current, current_prs in scenarios:
            with self.subTest(current=current, current_prs=current_prs):
                api = mock.Mock()
                api.runs.side_effect = [[original], []]
                api.pull_requests_for_commit.side_effect = [closed, current_prs]
                api.request.return_value = current
                environment = {"GH_TOKEN": "test-token", "GH_REPO": "test/repo",
                               "CLEANUP": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"}
                with mock.patch.dict(MODULE.os.environ, environment, clear=True), mock.patch.object(MODULE, "GitHub", return_value=api):
                    self.assertEqual(MODULE.main(), 0)
                api.request.assert_called_once_with("GET", "/repos/test/repo/actions/runs/123")


class DoomedRunTests(unittest.TestCase):
    """The doomed rule cancels a run whose required check is already decided.

    One `app-host unit tests` shard concluding `failure` fails the `macos`
    reusable-workflow call, which `ci-status` accepts only as `success` or
    `skipped`, so the required check is decided by construction.  Across the
    299 CI runs created between 2026-09-22T06:05Z and 17:00Z, 21 runs had such
    a shard failure and `ci-status` concluded `failure` in all 21.
    """

    now = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc)
    run_data = {
        "id": 900,
        "event": "pull_request",
        "status": "in_progress",
        "path": ".github/workflows/ci.yml",
        "run_attempt": 1,
        "head_branch": "fix/app-host-cloud-displays",
    }
    failed_shard = {
        "name": "macos / app-host unit tests (3/6)",
        "status": "completed",
        "conclusion": "failure",
        "completed_at": "2026-09-22T11:00:00Z",
        "labels": ["blacksmith-6vcpu-macos-15"],
    }
    running_shard = {
        "name": "macos / app-host unit tests (4/6)",
        "status": "in_progress",
        "conclusion": None,
        "completed_at": None,
        "labels": ["blacksmith-6vcpu-macos-15"],
    }
    linux_job = {
        "name": "guards / workflow-guard-tests",
        "status": "in_progress",
        "conclusion": None,
        "completed_at": None,
        "labels": ["blacksmith-4vcpu-ubuntu-2404"],
    }

    def classify(self, jobs, changed_files=("web/app/page.tsx",), labels=(), **overrides):
        run = dict(self.run_data, **overrides)
        return MODULE.classify_doomed_run(
            run, jobs, now=self.now, grace_seconds=600, default_branch="main",
            changed_files=None if changed_files is None else list(changed_files),
            labels=list(labels),
        )

    def test_failed_shard_with_macos_jobs_still_running_is_doomed(self):
        verdict = self.classify([self.failed_shard, self.running_shard, self.linux_job])
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.trigger, "macos / app-host unit tests (3/6)")
        self.assertEqual(verdict.holding, 1)
        self.assertIn("app-host unit tests (3/6)", verdict.reason)
        self.assertIn("1 macOS job(s) still holding a runner", verdict.reason)

    def test_every_macos_runner_family_counts_as_reclaimed_capacity(self):
        for label in ("blacksmith-6vcpu-macos-15", "tart-macos-15", "aws-m4pro-3", "MACOS-26"):
            with self.subTest(label=label):
                sibling = dict(self.running_shard, labels=[label])
                self.assertEqual(self.classify([self.failed_shard, sibling]).holding, 1)

    def test_failure_with_no_macos_jobs_still_running_is_preserved(self):
        # Nothing left to reclaim: cancelling would only destroy the Linux
        # jobs' results without freeing a macOS slot.
        finished = dict(self.running_shard, status="completed", conclusion="success",
                        completed_at="2026-09-22T11:30:00Z")
        self.assertIsNone(self.classify([self.failed_shard, finished, self.linux_job]))

    def test_macos_jobs_running_without_a_failure_are_preserved(self):
        for conclusion in ("success", "skipped", "cancelled", None):
            with self.subTest(conclusion=conclusion):
                shard = dict(self.failed_shard, conclusion=conclusion,
                             status="completed" if conclusion else "in_progress")
                self.assertIsNone(self.classify([shard, self.running_shard]))

    def test_continue_on_error_step_failure_is_preserved(self):
        # The jobs API does not report continue-on-error, but it does not need
        # to: a job whose only failed steps tolerate failure concludes
        # `success`, and ci-status reads the job, not the step.
        tolerated = dict(
            self.failed_shard,
            conclusion="success",
            steps=[{"name": "Upload xcresults", "status": "completed", "conclusion": "failure"}],
        )
        self.assertIsNone(self.classify([tolerated, self.running_shard]))

    def test_app_host_job_has_no_job_level_continue_on_error(self):
        # Job-level continue-on-error would not be absorbed by the conclusion
        # the previous test relies on, so the predicate's reading of a
        # `failure` conclusion would stop being sound.
        workflow = MACOS_WORKFLOW.read_text(encoding="utf-8")
        job = workflow.split("\n  app-host-unit-tests:\n", 1)[1].split("\n  ", 1)[0]
        self.assertNotIn("\n    continue-on-error", "\n" + job)

    def test_recent_failure_is_preserved_until_the_grace_window_passes(self):
        fresh = dict(self.failed_shard, completed_at="2026-09-22T11:55:00Z")
        self.assertIsNone(self.classify([fresh, self.running_shard]))

    def test_unsalvageable_facts_must_be_present_and_current(self):
        scenarios = {
            "other workflow": {"path": ".github/workflows/ci-macos.yml"},
            "re-run": {"run_attempt": 2},
            "merge group": {"event": "merge_group"},
            "push": {"event": "push"},
            "completed run": {"status": "completed"},
            "queued run": {"status": "queued"},
            "default branch": {"head_branch": "main"},
        }
        for name, override in scenarios.items():
            with self.subTest(name=name):
                self.assertIsNone(self.classify([self.failed_shard, self.running_shard], **override))

    def test_missing_completion_time_fails_closed(self):
        undated = dict(self.failed_shard, completed_at=None)
        self.assertIsNone(self.classify([undated, self.running_shard]))

    def test_unknown_default_branch_fails_closed(self):
        self.assertIsNone(MODULE.classify_doomed_run(
            dict(self.run_data), [self.failed_shard, self.running_shard], now=self.now,
            grace_seconds=600, default_branch="", changed_files=[], labels=[],
        ))

    def test_a_run_fixing_the_failing_job_is_preserved(self):
        """The run repairing app-host is the one that most needs its shards.

        Each path below is a real diff from a run this rule flagged in the
        24h window: PR #13643 (fix/app-host-green and its sibling branches),
        #13579, #13427, #13414 and #13615 were all repairing the app-host lane
        when a shard of their own run failed.
        """
        repairs = {
            "PR #13643 app-host test sources": "cmuxTests/AgentSessionAutoResumeSettingsTests.swift",
            "PR #13579 shard splitter": "scripts/ci/cmux_unit_test_shard.py",
            "PR #13579 shard workload": "scripts/ci/workloads/macos-app-host-test-shard.sh",
            "PR #13427 job definition": ".github/workflows/ci-macos.yml",
            "PR #13414 test product build": "scripts/ci/compile-app-host-test-product.sh",
            "PR #13414 product accounting": "scripts/ci/app_host_test_products.py",
            "quarantine list": "scripts/ci/app-host-known-failures.json",
        }
        for name, changed in repairs.items():
            with self.subTest(name=name):
                self.assertIsNone(self.classify(
                    [self.failed_shard, self.running_shard],
                    changed_files=["Sources/AppDelegate.swift", changed],
                ))

    def test_an_unrelated_diff_is_still_doomed(self):
        # PR #13218 (tact-79-lane-p-lossless-jsonc) changed nothing the shard
        # consumes, so its remaining macOS jobs are still only burning capacity.
        verdict = self.classify(
            [self.failed_shard, self.running_shard],
            changed_files=["Sources/JSONC.swift", "web/app/page.tsx", "tests/test_jsonc.py"],
        )
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.holding, 1)

    def test_unknown_diff_fails_closed(self):
        # No pull request, several pull requests, or a diff past the 3000-file
        # API ceiling: an absent path cannot be shown to be absent.
        self.assertIsNone(self.classify([self.failed_shard, self.running_shard], changed_files=None))

    def test_opt_out_label_preserves_the_run(self):
        # The escape hatch for a fix that lives entirely in product code, which
        # no path list can distinguish from an ordinary change.
        self.assertIsNone(self.classify(
            [self.failed_shard, self.running_shard], labels=["full-ci", "no-janitor"],
        ))

    def test_stale_rule_is_unaffected_by_a_doomed_run(self):
        # A doomed run with an open pull request is not stale, and a stale run
        # is not required to be doomed; the two rules stay independent.
        self.assertIsNone(MODULE.classify_run(
            dict(self.run_data, created_at="2026-09-22T11:00:00Z"),
            [{"state": "open", "merged_at": None}], now=self.now, min_age_seconds=3600,
        ))


class DoomedCancellationTests(unittest.TestCase):
    environment = {"GH_TOKEN": "test-token", "GH_REPO": "test/repo",
                   "CLEANUP": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"}

    def run_main(self, api):
        with mock.patch.dict(MODULE.os.environ, self.environment, clear=True), \
                mock.patch.object(MODULE, "GitHub", return_value=api):
            return MODULE.main()

    def api_for(self, jobs, *, current=None):
        run = dict(DoomedRunTests.run_data, head_sha="doomed-head",
                   created_at="2026-09-22T11:00:00Z")
        api = mock.Mock()
        api.default_branch.return_value = "main"
        api.runs.side_effect = [[], [run]]
        api.jobs.return_value = jobs
        api.pull_requests_for_commit.return_value = [
            {"state": "open", "merged_at": None, "number": 13218, "labels": [{"name": "full-ci"}]}
        ]
        api.pull_request_files.return_value = ["Sources/JSONC.swift"]
        api.request.side_effect = [current if current is not None else run, {}]
        return api

    def test_doomed_run_is_cancelled_not_deleted(self):
        api = self.api_for([DoomedRunTests.failed_shard, DoomedRunTests.running_shard])
        with mock.patch.object(MODULE.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = DoomedRunTests.now
            self.assertEqual(self.run_main(api), 0)
        self.assertEqual(api.request.call_args_list, [
            mock.call("GET", "/repos/test/repo/actions/runs/900"),
            mock.call("POST", "/repos/test/repo/actions/runs/900/cancel"),
        ])

    def test_a_fix_branch_is_never_cancelled_end_to_end(self):
        api = self.api_for([DoomedRunTests.failed_shard, DoomedRunTests.running_shard])
        api.pull_request_files.return_value = [
            "Sources/AppDelegate.swift", "cmuxTests/AgentSessionAutoResumeSettingsTests.swift",
        ]
        with mock.patch.object(MODULE.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = DoomedRunTests.now
            self.assertEqual(self.run_main(api), 0)
        api.request.assert_not_called()

    def test_a_run_that_turned_green_before_the_request_is_preserved(self):
        recovered = [dict(DoomedRunTests.failed_shard, conclusion="success"),
                     DoomedRunTests.running_shard]
        api = self.api_for([DoomedRunTests.failed_shard, DoomedRunTests.running_shard])
        api.jobs.side_effect = [[DoomedRunTests.failed_shard, DoomedRunTests.running_shard], recovered]
        with mock.patch.object(MODULE.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = DoomedRunTests.now
            self.assertEqual(self.run_main(api), 0)
        api.request.assert_called_once_with("GET", "/repos/test/repo/actions/runs/900")


class WorkflowSafetyTests(unittest.TestCase):
    def test_scheduled_and_manual_janitors_are_serialized(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("concurrency:\n  group: ci-stale-run-janitor\n  cancel-in-progress: false", workflow)

    def test_scheduled_executions_cannot_cancel_anything(self):
        # The schedule has no inputs, so CLEANUP falls back to 'false'; the
        # script additionally requires the workflow_dispatch event.
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("CLEANUP: ${{ inputs.cleanup || 'false' }}", workflow)
        self.assertIn("DOOMED_GRACE_MINUTES: ${{ inputs.doomed_grace_minutes || '10' }}", workflow)
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'env_bool("CLEANUP") and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
