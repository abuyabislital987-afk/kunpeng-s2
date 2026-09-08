"""Exercise evidence validation and promotion without touching the working records."""
import argparse
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("experiment_under_test", REPO / "tools/experiment.py")
EXPERIMENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPERIMENT)


def official_rows(problem):
    if problem == "conv":
        path = REPO / "records/evidence/conv-1483453/benchmark.log"
    else:
        path = REPO / "records/evidence/final-1484056/benchmark.log"
    lines = [line for line in path.read_text().splitlines()
             if re.match(r"^\s*\d+\s*x", line) and re.search(r"\bPASS\s*$", line)]
    if problem != "conv":
        lines = [line for line in lines if line.count("x") == len(EXPERIMENT.CASES[problem][0]) - 1]
    return lines


def suite(problem="conv", times=None):
    rows = official_rows(problem)
    if times is not None:
        changed = []
        for row, value in zip(rows, times):
            parts = row.split()
            parts[-4] = str(value)
            changed.append(" ".join(parts))
        rows = changed
    return "\n".join(rows) + "\n"


def measured(version="C0", times=(100.0, 200.0, 300.0, 400.0)):
    result = EXPERIMENT.parse_log("conv", suite(times=times) * 3, 3)
    result.update({
        "problem": "conv", "version": version, "status": "passed", "verified": True,
        "source_hashes": {"conv2d.c": version, "bench_conv.c": EXPERIMENT.BENCH_SHA["conv"], "run.sh": "runner"},
        "environment": "cn22965-gcc10-numa2", "reference": "official internal reference",
        "machine": {"HOST": "cn22965", "ARCH": "aarch64", "NUMA_NODE": "2",
                    "ALLOWED_CPUS": "76-113", "OMP_NUM_THREADS": "38", "CPU_TARGET": "generic",
                    "compiler_banners": ["gcc (GCC) 10.3.1"]},
        "settings": {"bench_repeats": 3, "environment": {"TEST_RUNS": "3", "CC": "gcc"}},
    })
    return result


class IsolatedRoot(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kunpeng-experiment-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = mock.patch.object(EXPERIMENT, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def install_source(self, problem="conv"):
        shutil.copytree(REPO / problem, self.root / problem)

    def install_history(self):
        target = self.root / "records/history.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / "records/history.json", target)

    def new(self, version="C0", parent=None):
        EXPERIMENT.new(argparse.Namespace(problem="conv", version=version, parent=parent,
                                         strategy="Test an independent blocking change"))
        return EXPERIMENT.run_dir("conv", version)


class ParseLogTests(IsolatedRoot):
    def test_three_real_official_formats_and_case_totals(self):
        expected = {"conv": 1881.67, "zgemm": 4566.78, "trsm": 777.85}
        for problem, total in expected.items():
            with self.subTest(problem=problem):
                result = EXPERIMENT.parse_log(problem, suite(problem) * 3, 3)
                self.assertAlmostEqual(result["total_median_ms"], total, places=7)
                self.assertEqual([r["dims"] for r in result["cases"]],
                                 [list(d) for d in EXPERIMENT.CASES[problem]])
                self.assertTrue(all(len(r["times_ms"]) == 3 for r in result["cases"]))
                self.assertIsNone(result["official_score"])
                self.assertIsNone(result["ranking"])

    def test_rejects_fail_even_when_all_pass_rows_are_present(self):
        with self.assertRaises(ValueError):
            EXPERIMENT.parse_log("conv", suite() + "validation FAIL\n", 1)

    def test_rejects_nonfinite_or_nonpositive_metrics(self):
        for column, value in [(-4, "NaN"), (-4, "Inf"), (-4, "-Inf"), (-4, "0"),
                              (-4, "-1"), (-3, "NaN"), (-3, "Inf"), (-3, "0"),
                              (-2, "NaN"), (-2, "Inf"), (-2, "-1")]:
            with self.subTest(column=column, value=value):
                rows = official_rows("conv")
                parts = rows[0].split()
                parts[column] = value
                rows[0] = " ".join(parts)
                with self.assertRaises(ValueError):
                    EXPERIMENT.parse_log("conv", "\n".join(rows), 1)

    def test_rejects_missing_duplicate_reordered_or_wrong_dimensions(self):
        rows = official_rows("conv")
        alternatives = {
            "missing": rows[:-1], "duplicate": rows + [rows[-1]],
            "wrong_order": [rows[1], rows[0], *rows[2:]],
            "duplicate_replaces_case": [rows[0], rows[0], *rows[2:]],
            "wrong_dimensions": [rows[0].replace("4096", "4095", 1), *rows[1:]],
        }
        for label, changed in alternatives.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                EXPERIMENT.parse_log("conv", "\n".join(changed), 1)

    def test_requires_independent_complete_rounds(self):
        with self.assertRaises(ValueError):
            EXPERIMENT.parse_log("conv", suite(), 3)
        rows = official_rows("conv")
        grouped_by_case = "\n".join(row for row in rows for _ in range(3))
        with self.assertRaises(ValueError):
            EXPERIMENT.parse_log("conv", grouped_by_case, 3)
        with self.assertRaises(ValueError):
            EXPERIMENT.parse_log("conv", suite(), 0)

    def test_median_uses_repeated_rounds_instead_of_fastest_sample(self):
        text = suite(times=(90, 180, 270, 360)) + suite(times=(100, 200, 300, 400)) + suite(times=(150, 300, 450, 600))
        result = EXPERIMENT.parse_log("conv", text, 3)
        self.assertEqual(result["total_median_ms"], 1000)
        self.assertEqual(result["cases"][0]["times_ms"], [90, 100, 150])
        self.assertEqual(result["cases"][0]["spread_pct"], 60)


class SchedulerTests(IsolatedRoot):
    def test_historical_donau_success_and_exit_codes(self):
        for name in ("conv-1483453", "final-1484056"):
            text = (REPO / "records/evidence" / name / "job-status.txt").read_text()
            with self.subTest(name=name):
                self.assertTrue(EXPERIMENT.scheduler_ok(text))
                for field in ("jobExitCode", "systemExitCode"):
                    failed = re.sub(r"(^\s*" + field + r"\s+)0\b", r"\g<1>1", text, flags=re.M)
                    self.assertNotEqual(failed, text)
                    self.assertFalse(EXPERIMENT.scheduler_ok(failed))

    def test_normalized_status_requires_explicit_success_and_zero_exits(self):
        valid = {"state": "SUCCEEDED", "jobExitCode": 0, "systemExitCode": "0", "jobId": 42}
        self.assertTrue(EXPERIMENT.scheduler_ok(json.dumps(valid)))
        self.assertTrue(EXPERIMENT.scheduler_ok(json.dumps({"jobs": [valid]})))
        for bad in ({}, [], {**valid, "state": "RUNNING"},
                    {"state": "SUCCEEDED", "jobExitCode": 0},
                    {"jobs": [valid, valid]}, {**valid, "jobExitCode": 1}):
            with self.subTest(status=bad):
                self.assertFalse(EXPERIMENT.scheduler_ok(json.dumps(bad)))


class ComparisonTests(IsolatedRoot):
    def setUp(self):
        super().setUp()
        self.base = measured()
        self.candidate = measured("C1", (80, 160, 240, 320))

    def test_accepts_clear_same_environment_speedup_with_source_change(self):
        verdict = EXPERIMENT.comparison(self.base, self.candidate)
        self.assertTrue(verdict["eligible"], verdict)
        self.assertEqual(len(verdict["cases"]), 4)
        self.assertTrue(all(r["gain_pct"] == 20 for r in verdict["cases"]))

    def test_rejects_missing_evidence_or_fewer_than_three_rounds_on_either_side(self):
        for side in ("base", "candidate"):
            for field, value in (("verified", False), ("repeats", 2)):
                a, b = copy.deepcopy(self.base), copy.deepcopy(self.candidate)
                (a if side == "base" else b)[field] = value
                with self.subTest(side=side, field=field):
                    self.assertFalse(EXPERIMENT.comparison(a, b)["eligible"])

    def test_rejects_environment_reference_machine_or_benchmark_changes(self):
        for field in ("environment", "reference", "machine"):
            changed = copy.deepcopy(self.candidate)
            changed[field] = "different"
            with self.subTest(field=field):
                self.assertFalse(EXPERIMENT.comparison(self.base, changed)["eligible"])
        changed = copy.deepcopy(self.candidate)
        changed["source_hashes"]["bench_conv.c"] = "modified benchmark"
        self.assertFalse(EXPERIMENT.comparison(self.base, changed)["eligible"])

    def test_rejects_evaluation_settings_changes(self):
        for key in ("TEST_RUNS", "KBLAS_LIB", "CC", "COMPILER"):
            changed = copy.deepcopy(self.candidate)
            changed["settings"]["environment"][key] = "changed"
            with self.subTest(key=key):
                self.assertFalse(EXPERIMENT.comparison(self.base, changed)["eligible"])

    def test_rejects_gain_inside_noise_or_one_percent_floor(self):
        tiny = measured("C1", (99.5, 199, 298.5, 398))
        self.assertFalse(EXPERIMENT.comparison(self.base, tiny)["eligible"])
        noisy = copy.deepcopy(self.candidate)
        noisy["cases"][0]["spread_pct"] = 25
        self.assertFalse(EXPERIMENT.comparison(self.base, noisy)["eligible"])

    def test_rejects_single_case_regression_even_if_total_is_faster(self):
        mixed = measured("C1", (102, 150, 200, 250))
        self.assertFalse(EXPERIMENT.comparison(self.base, mixed)["eligible"])

    def test_same_source_and_settings_is_a_retest_not_a_promotion(self):
        self.candidate["source_hashes"] = copy.deepcopy(self.base["source_hashes"])
        self.assertFalse(EXPERIMENT.comparison(self.base, self.candidate)["eligible"])


class SnapshotAndPromotionTests(IsolatedRoot):
    def setUp(self):
        super().setUp()
        self.install_source()

    def test_new_accepts_historical_parent_without_local_runs(self):
        self.install_history()
        self.assertFalse((self.root / ".runs").exists())
        folder = self.new("C1-test", "C0")
        metadata = EXPERIMENT.read_json(folder / "experiment.json")
        self.assertEqual(metadata["parent"], "C0")
        self.assertEqual(EXPERIMENT.source_files(folder / "source"),
                         EXPERIMENT.source_files(self.root / "conv"))

    def test_new_records_planned_and_checkpoint_refreshes_prepared_source_hashes(self):
        folder = self.new()
        path = EXPERIMENT.record_path("conv", "C0")
        planned = EXPERIMENT.read_json(path)
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(planned, EXPERIMENT.read_json(folder / "experiment.json"))
        with (folder / "source/conv2d.c").open("a") as output:
            output.write("\n/* locally checked candidate adjustment */\n")
        EXPERIMENT.checkpoint(argparse.Namespace(problem="conv", version="C0",
                                                note="Local correctness check passed; remote timing pending."))
        prepared = EXPERIMENT.read_json(path)
        self.assertEqual(prepared["status"], "prepared")
        self.assertFalse(prepared["verified"])
        self.assertNotEqual(prepared["source_hashes"]["conv2d.c"], planned["source_hashes"]["conv2d.c"])
        self.assertEqual(prepared["source_hashes"], EXPERIMENT.source_files(folder / "source"))
        self.assertEqual(prepared["strategy"], planned["strategy"])
        self.assertIn("remote timing pending", prepared["note"])

    def test_historical_parent_rejects_a_changed_operator(self):
        self.install_history()
        with (self.root / "conv/conv2d.c").open("a") as output:
            output.write("\n/* different historical source */\n")
        with self.assertRaises(ValueError):
            self.new("C1-test", "C0")

    def test_new_rejects_modified_official_benchmark(self):
        with (self.root / "conv/bench_conv.c").open("a") as output:
            output.write("\n/* changed benchmark */\n")
        with self.assertRaises(ValueError):
            self.new()

    def test_clone_with_best_and_records_restores_parent_from_problem_directory(self):
        parent = measured()
        parent["source_hashes"] = EXPERIMENT.source_files(self.root / "conv")
        EXPERIMENT.write_json(EXPERIMENT.record_path("conv", "C0"), parent)
        EXPERIMENT.write_json(self.root / "records/best.json", {"conv": "C0"})
        self.assertFalse((self.root / ".runs").exists())
        folder = self.new("C1-test", "C0")
        metadata = EXPERIMENT.read_json(folder / "experiment.json")
        self.assertEqual(metadata["source_hashes"], parent["source_hashes"])
        self.assertEqual(metadata["settings"], parent["settings"])

    def test_clone_rejects_problem_source_that_differs_from_parent_record(self):
        parent = measured()
        parent["source_hashes"] = EXPERIMENT.source_files(self.root / "conv")
        EXPERIMENT.write_json(EXPERIMENT.record_path("conv", "C0"), parent)
        with (self.root / "conv/conv2d.c").open("a") as output:
            output.write("\n/* unrelated change */\n")
        with self.assertRaises(ValueError):
            self.new("C1-test", "C0")

    def test_promote_rejects_source_changed_after_measurement(self):
        folder = self.new()
        original = EXPERIMENT.source_files(self.root / "conv")
        record = measured()
        record["source_hashes"] = EXPERIMENT.source_files(folder / "source")
        record["parent"] = None
        EXPERIMENT.write_json(EXPERIMENT.record_path("conv", "C0"), record)
        with (folder / "source/conv2d.c").open("a") as output:
            output.write("\n/* not the measured source */\n")
        with self.assertRaises(ValueError):
            EXPERIMENT.promote(argparse.Namespace(problem="conv", version="C0"))
        self.assertEqual(EXPERIMENT.source_files(self.root / "conv"), original)
        self.assertFalse((self.root / "records/best.json").exists())


class RecordEvidenceTests(IsolatedRoot):
    def setUp(self):
        super().setUp()
        self.install_source()
        self.folder = self.new()
        self.hashes = EXPERIMENT.source_files(self.folder / "source")
        (self.folder / "benchmark.log").write_text("gcc (GCC) 10.3.1\n" + suite() * 3)
        (self.folder / "environment.log").write_text(
            "HOST=cn22965\nARCH=aarch64\nNUMA_NODE=5\nALLOWED_CPUS=190-227\n"
            "OMP_NUM_THREADS=38\nCPU_TARGET=generic\n")
        (self.folder / "exit-code.txt").write_text("0\n")
        (self.folder / "source-sha256.txt").write_text(
            "".join(f"{value}  source/{name}\n" for name, value in self.hashes.items()))
        shutil.copyfile(REPO / "records/evidence/conv-1483453/job-status.txt",
                        self.folder / "scheduler-status.txt")
        self.cluster = {"job_id": 1483453, "source_hashes": self.hashes,
                        "settings": {"bench_repeats": 3, "environment": {"TEST_RUNS": "1"}},
                        "artifacts_sha256": {name: EXPERIMENT.digest(self.folder / name) for name in
                                              ("benchmark.log", "environment.log", "exit-code.txt", "source-sha256.txt")}}
        self.save_cluster()

    def save_cluster(self):
        EXPERIMENT.write_json(self.folder / "cluster.json", self.cluster)

    def record(self, log=None):
        args = argparse.Namespace(problem="conv", version="C0", log=str(log or self.folder / "benchmark.log"),
                                  environment="cn22965-gcc10", reference="official internal reference",
                                  repeats=3, scheduler_status=None, failure=None)
        EXPERIMENT.record(args)
        return EXPERIMENT.read_json(EXPERIMENT.record_path("conv", "C0"))

    def test_accepts_complete_artifacts_bound_to_same_job_and_source(self):
        record = self.record()
        self.assertTrue(record["verified"], record["checks"])
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["job_id"], 1483453)
        self.assertTrue(all(record["checks"].values()))

    def test_prepared_can_be_measured_but_completed_measurement_cannot_be_overwritten(self):
        EXPERIMENT.checkpoint(argparse.Namespace(problem="conv", version="C0", note="Prepared for timing."))
        self.assertEqual(EXPERIMENT.read_json(EXPERIMENT.record_path("conv", "C0"))["status"], "prepared")
        record = self.record()
        self.assertEqual(record["status"], "passed")
        with self.assertRaises(ValueError):
            EXPERIMENT.checkpoint(argparse.Namespace(problem="conv", version="C0", note="Late edit."))
        with self.assertRaises(ValueError):
            self.record()
        self.assertEqual(EXPERIMENT.read_json(EXPERIMENT.record_path("conv", "C0")), record)

    def test_unverified_allows_same_source_and_log_to_gain_missing_job_evidence(self):
        self.cluster["job_id"] = 9999999
        self.save_cluster()
        unverified = self.record()
        self.assertEqual(unverified["status"], "unverified")
        self.cluster["job_id"] = 1483453
        self.save_cluster()
        completed = self.record()
        self.assertEqual(completed["status"], "passed")
        self.assertTrue(completed["verified"])
        self.assertEqual(completed["log_sha256"], unverified["log_sha256"])
        self.assertEqual(completed["source_hashes"], unverified["source_hashes"])

    def test_unverified_rejects_source_or_log_changes_and_preserves_prior_record(self):
        self.cluster["job_id"] = 9999999
        self.save_cluster()
        original_record = self.record()
        self.assertEqual(original_record["status"], "unverified")
        for relative in ("source/conv2d.c", "benchmark.log"):
            target = self.folder / relative
            original = target.read_bytes()
            with self.subTest(changed=relative):
                try:
                    target.write_bytes(original + b"\n/* changed after measurement */\n")
                    with self.assertRaises(ValueError):
                        self.record()
                    self.assertEqual(EXPERIMENT.read_json(EXPERIMENT.record_path("conv", "C0")), original_record)
                finally:
                    target.write_bytes(original)

    def test_rejects_success_status_from_a_different_job(self):
        self.cluster["job_id"] = 9999999
        self.save_cluster()
        record = self.record()
        self.assertFalse(record["verified"])
        self.assertFalse(record["checks"]["scheduler"])

    def test_identical_external_log_is_not_bound_to_fetched_artifacts(self):
        external = self.root / "unrelated-benchmark.log"
        shutil.copyfile(self.folder / "benchmark.log", external)
        record = self.record(external)
        self.assertFalse(record["verified"])
        self.assertFalse(record["checks"]["fetched_log"])

    def test_valid_replacement_benchmark_with_stale_artifact_hash_is_rejected(self):
        (self.folder / "benchmark.log").write_text("gcc (GCC) 10.3.1\n" + suite(times=(10, 20, 30, 40)) * 3)
        record = self.record()
        self.assertFalse(record["verified"])
        self.assertFalse(record["checks"]["fetched_log"])

    def test_changed_environment_artifact_is_rejected(self):
        with (self.folder / "environment.log").open("a") as output:
            output.write("CHANGED_AFTER_FETCH=1\n")
        record = self.record()
        self.assertFalse(record["verified"])
        self.assertFalse(record["checks"]["fetched_log"])

    def test_missing_remote_source_hash_is_rejected(self):
        (self.folder / "source-sha256.txt").write_text("")
        self.cluster["artifacts_sha256"]["source-sha256.txt"] = EXPERIMENT.digest(self.folder / "source-sha256.txt")
        self.save_cluster()
        record = self.record()
        self.assertFalse(record["verified"])
        self.assertFalse(record["checks"]["source_hashes"])


if __name__ == "__main__":
    unittest.main()
