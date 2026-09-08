"""SSH is mocked: these tests never connect or submit a real cluster job."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("cluster", Path(__file__).resolve().parents[1] / "tools" / "cluster.py")
cluster = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cluster)


def result(code=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def archive_bytes(files):
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return blob.getvalue()


class ClusterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "cluster.json"
        self.config_path.write_text(json.dumps({"host": "cluster.example.org", "user": "alice",
                                              "remote_root": "/home/alice/experiments"}))
        self.cfg = cluster.load_config(self.config_path)
        self.run = self.root / "C0 with spaces"
        (self.run / "source" / "compat").mkdir(parents=True)
        (self.run / "source" / "run.sh").write_text("#!/bin/bash\necho benchmark\n")
        (self.run / "source" / "conv.c").write_text("int test = 1;\n")
        (self.run / "source" / "compat" / "header.h").write_text("#define TEST 1\n")
        (self.run / "experiment.json").write_text(json.dumps({"settings": {}}))

    def manifest(self, **changes):
        data = {"state": "submitted", "job_id": "1234", "host": "cluster.example.org",
                "user": "alice", "port": 22, "remote_dir": "/home/alice/experiments/run-1234"}
        data.update(changes)
        (self.run / "cluster.json").write_text(json.dumps(data))
        return data

    def read_manifest(self):
        return json.loads((self.run / "cluster.json").read_text())

    def test_submit_snapshot_settings_and_no_password(self):
        observed = []
        def fake(argv, **kwargs):
            observed.append((argv, kwargs))
            if "stdin" in kwargs:
                with tarfile.open(fileobj=kwargs["stdin"], mode="r:gz") as archive:
                    self.assertEqual(set(archive.getnames()), {"source", "source/conv.c", "source/run.sh",
                                     "source/compat/header.h", "remote_job.sh", "remote-settings.json"})
                    settings = json.load(archive.extractfile("remote-settings.json"))
                    self.assertEqual(settings["environment"]["CPU_TARGET"], "generic")
                    self.assertEqual(settings["bench_repeats"], 3)
                return result()
            self.assertEqual(self.read_manifest()["state"], "submit_unknown")
            return result(stdout=b"Job <1234> is submitted to queue <q_kunpeng>.\n")
        (self.run / "source" / "conv.c").write_text("int changed_after_new = 2;\n")
        with patch.object(cluster.subprocess, "run", side_effect=fake):
            self.assertEqual(cluster.submit(self.cfg, self.run), 0)
        manifest = self.read_manifest()
        self.assertEqual(manifest["state"], "submitted")
        self.assertEqual(manifest["job_id"], "1234")
        self.assertEqual(manifest["source_hashes"], cluster.source_hashes(self.run / "source"))
        self.assertIn("StrictHostKeyChecking=yes", observed[0][0])
        self.assertIn("BatchMode=yes", observed[0][0])
        self.assertIn("ConnectTimeout=10", observed[0][0])
        self.assertNotIn("shell", observed[0][1])
        command = shlex.split(observed[-1][0][-1])
        self.assertEqual(command[command.index("-R") + 1], "cpu=38,mem=24576")
        self.assertEqual(command[command.index("-a") + 1], "numa[count=1,distribution=pack]")
        self.assertEqual(command[command.index("-T") + 1], "1800")
        self.assertTrue(command[command.index("-o") + 1].endswith("wrapper.stdout.log"))

    def test_duplicate_and_unknown_submissions_never_call_ssh(self):
        for state in ("submitted", "submit_unknown", "preparing", "upload_failed"):
            self.manifest(state=state)
            with patch.object(cluster.subprocess, "run") as remote:
                with self.assertRaises(cluster.ClusterError):
                    cluster.submit(self.cfg, self.run)
                remote.assert_not_called()

    def test_submit_timeout_guards_against_retry(self):
        with patch.object(cluster.subprocess, "run", side_effect=[result(), subprocess.TimeoutExpired("ssh", 45)]):
            with self.assertRaises(cluster.ClusterError):
                cluster.submit(self.cfg, self.run)
        self.assertEqual(self.read_manifest()["state"], "submit_unknown")
        with patch.object(cluster.subprocess, "run") as remote:
            with self.assertRaises(cluster.ClusterError):
                cluster.submit(self.cfg, self.run)
            remote.assert_not_called()

    def test_upload_uses_snapshot_even_if_live_candidate_changes(self):
        expected = cluster.source_hashes(self.run / "source")
        def fake(argv, **kwargs):
            if "stdin" in kwargs:
                (self.run / "source" / "conv.c").write_text("changed while uploading\n")
                with tarfile.open(fileobj=kwargs["stdin"], mode="r:gz") as archive:
                    self.assertEqual(archive.extractfile("source/conv.c").read(), b"int test = 1;\n")
                return result()
            return result(stdout=b"Job <1234> is submitted\n")
        with patch.object(cluster.subprocess, "run", side_effect=fake):
            cluster.submit(self.cfg, self.run)
        self.assertEqual(self.read_manifest()["source_hashes"], expected)
        self.assertNotEqual(self.read_manifest()["source_hashes"], cluster.source_hashes(self.run / "source"))

    def test_unrecognized_or_nonzero_submit_never_assumes_success(self):
        for reply in (result(stdout=b"Submission received"), result(255, b"Job <1234> submitted"),
                      result(stdout=b"Job <1234> submitted\nJob <4567> submitted")):
            with self.subTest(reply=reply):
                (self.run / "cluster.json").unlink(missing_ok=True)
                with patch.object(cluster.subprocess, "run", side_effect=[result(), reply]):
                    with self.assertRaises(cluster.ClusterError):
                        cluster.submit(self.cfg, self.run)
                self.assertEqual(self.read_manifest()["state"], "submit_unknown")

    def test_upload_failure_does_not_call_dsub(self):
        with patch.object(cluster.subprocess, "run", return_value=result(255, stderr=b"connection failed")) as remote:
            with self.assertRaises(cluster.ClusterError):
                cluster.submit(self.cfg, self.run)
            self.assertEqual(remote.call_count, 1)
        self.assertEqual(self.read_manifest()["state"], "upload_failed")

    def test_shell_metacharacters_are_quoted_as_one_argument(self):
        self.cfg["scheduler"]["queue"] = "queue'; touch /tmp/SHOULD_NOT_EXIST; '"
        command = cluster.submit_command(self.cfg, "/home/alice/experiments/run", "name")
        parsed = shlex.split(command)
        self.assertEqual(parsed[parsed.index("-q") + 1], self.cfg["scheduler"]["queue"])
        self.assertNotIn("touch", parsed)

    def test_status_requires_explicit_syntax_and_records_json(self):
        self.manifest()
        with patch.object(cluster.subprocess, "run") as remote:
            with self.assertRaises(cluster.ClusterError):
                cluster.status(self.cfg, self.run)
            remote.assert_not_called()
        self.cfg["scheduler"]["status_argv"] = ["djob", "--site-verified-json", "{job_id}"]
        payload = {"jobs": [{"jobId": "1234", "status": "SUCCEEDED", "jobExitCode": 0, "systemExitCode": 0}]}
        with patch.object(cluster.subprocess, "run", return_value=result(stdout=json.dumps(payload).encode())) as remote:
            self.assertEqual(cluster.status(self.cfg, self.run), 0)
            self.assertEqual(remote.call_count, 1)
            self.assertEqual(shlex.split(remote.call_args.args[0][-1]), ["djob", "--site-verified-json", "1234"])
        self.assertEqual(self.read_manifest()["scheduler_status"]["status"], "SUCCEEDED")
        self.assertEqual(json.loads((self.run / "scheduler-status.txt").read_text()), payload)

    def test_status_failure_clears_stale_success(self):
        self.manifest(scheduler_status={"status": "SUCCEEDED", "jobExitCode": 0, "systemExitCode": 0})
        self.cfg["scheduler"]["status_argv"] = ["djob", "{job_id}"]
        with patch.object(cluster.subprocess, "run", return_value=result(255, stderr=b"connection lost")):
            with self.assertRaises(cluster.ClusterError):
                cluster.status(self.cfg, self.run)
        self.assertIsNone(self.read_manifest()["scheduler_status"])

    def test_plain_text_or_wrong_job_does_not_verify_completion(self):
        self.assertIsNone(cluster.parse_scheduler_status("SUCCEEDED jobExitCode=0", "1234"))
        self.assertIsNone(cluster.parse_scheduler_status('{"status":"SUCCEEDED","jobId":"9999"}', "1234"))

    def test_observed_djob_ll_status_is_saved_with_job_and_exit_codes(self):
        self.manifest(job_id="1485173")
        self.cfg["scheduler"]["status_argv"] = ["djob", "-ll", "{job_id}"]
        output = ("Basic Details:\n    jobId       \t1485173   \t\n"
                  "    state       \tFAILED    \t\nRuntime Details:\n"
                  "    jobExitCode \t1         \t\n    systemExitCode \t10001 \t\n")
        with patch.object(cluster.subprocess, "run", return_value=result(stdout=output.encode())):
            self.assertEqual(cluster.status(self.cfg, self.run), 0)
        self.assertEqual(self.read_manifest()["scheduler_status"], {
            "jobId": "1485173", "state": "FAILED", "status": "FAILED",
            "jobExitCode": 1, "systemExitCode": 10001})
        self.assertEqual((self.run / "scheduler-status.txt").read_text(), output)
        self.assertIsNone(cluster.parse_scheduler_status(output, "9999"))

    def test_djob_ll_multiple_job_ids_are_not_merged(self):
        self.assertIsNone(cluster.parse_scheduler_status("jobId 1234\njobId 5678\nstate SUCCEEDED\n", "1234"))

    def test_fetch_complete_keeps_source_immutable(self):
        self.manifest()
        hashes = cluster.source_hashes(self.run / "source")
        files = {"benchmark.log": b"PASS\n", "environment.log": b"HOST=compute\n",
                 "source-sha256.txt": b"abc  conv.c\n", "exit-code.txt": b"0\n",
                 "source/results/round1/case-1.log": b"PASS case 1\n"}
        def fake(argv, **kwargs):
            self.assertIn("&& (set --;", argv[-1])
            kwargs["stdout"].write(archive_bytes(files))
            return result(stdout=None)
        with patch.object(cluster.subprocess, "run", side_effect=fake):
            self.assertEqual(cluster.fetch(self.cfg, self.run), 0)
        self.assertEqual(self.read_manifest()["fetch_state"], "complete")
        self.assertEqual(set(self.read_manifest()["artifacts_sha256"]), set(cluster.ARTIFACTS[:4]))
        self.assertTrue((self.run / "remote-results/round1/case-1.log").is_file())
        self.assertEqual(cluster.source_hashes(self.run / "source"), hashes)

    def test_fetch_failure_and_partial_fetch_cannot_be_complete(self):
        self.manifest()
        with patch.object(cluster.subprocess, "run", return_value=result(255, stdout=None, stderr=b"timeout")):
            with self.assertRaises(cluster.ClusterError):
                cluster.fetch(self.cfg, self.run)
        self.assertEqual(self.read_manifest()["fetch_state"], "failed")
        def partial(argv, **kwargs):
            kwargs["stdout"].write(archive_bytes({"benchmark.log": b"running"}))
            return result(stdout=None)
        with patch.object(cluster.subprocess, "run", side_effect=partial):
            self.assertEqual(cluster.fetch(self.cfg, self.run), 2)
        self.assertEqual(self.read_manifest()["fetch_state"], "incomplete")

    def test_fetch_archive_traversal_and_symlinks_rejected(self):
        for name in ("../escape", "/tmp/escape", "source/results/../../../escape", "source/run.sh"):
            with self.subTest(name=name):
                archive_path = self.root / "bad.tar.gz"
                archive_path.write_bytes(archive_bytes({name: b"bad"}))
                with self.assertRaises(cluster.ClusterError):
                    cluster.extract_artifacts(archive_path, self.root / "staging")
        archive_path = self.root / "symlink.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            link = tarfile.TarInfo("source/results/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/tmp"
            archive.addfile(link)
        with self.assertRaises(cluster.ClusterError):
            cluster.extract_artifacts(archive_path, self.root / "staging")

    def test_remote_paths_and_competition_resources_validated(self):
        for path in ("relative/root", "/home/alice/../other", "/home/alice/a b", "/tmp/$(touch-x)", "/"):
            self.config_path.write_text(json.dumps({"host": "cluster", "remote_root": path}))
            with self.assertRaises(cluster.ClusterError):
                cluster.load_config(self.config_path)
        self.config_path.write_text(json.dumps({"host": "cluster", "remote_root": "/home/alice",
                                               "scheduler": {"cpus": 39}}))
        with self.assertRaises(cluster.ClusterError):
            cluster.load_config(self.config_path)

    def test_known_hosts_path_and_environment_overrides(self):
        known = self.root / "known hosts"
        known.write_text("trusted-key-placeholder\n")
        self.config_path.write_text(json.dumps({"host": "cluster", "remote_root": "/home/alice",
                                               "known_hosts": "known hosts", "environment": {"CONV_BLOCK": "32"}}))
        cfg = cluster.load_config(self.config_path)
        self.assertIn("UserKnownHostsFile=" + str(known.resolve()), cluster.ssh_argv(cfg, "hostname"))
        settings = cluster.effective_settings(cfg, {"settings": {"bench_repeats": 5, "environment": {"CONV_BLOCK": 64}}})
        self.assertEqual(settings["bench_repeats"], 5)
        self.assertEqual(settings["environment"]["CONV_BLOCK"], "64")
        inherited = cluster.effective_settings(cfg, {"settings": settings})
        self.assertEqual(inherited, settings)
        with self.assertRaises(cluster.ClusterError):
            cluster.effective_settings(cfg, {"settings": {"environment": {"OMP_NUM_THREADS": "200"}}})

    def test_optional_control_socket_reuses_local_interactive_login(self):
        socket = str((self.root / "ssh-control.socket").resolve())
        self.config_path.write_text(json.dumps({"host": "cluster", "remote_root": "/home/alice",
                                               "control_path": socket}))
        cfg = cluster.load_config(self.config_path)
        argv = cluster.ssh_argv(cfg, "hostname")
        self.assertEqual(argv[argv.index("-S") + 1], socket)
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ControlMaster=no", argv)
        self.config_path.write_text(json.dumps({"host": "cluster", "remote_root": "/home/alice",
                                               "control_path": "relative/socket"}))
        with self.assertRaises(cluster.ClusterError):
            cluster.load_config(self.config_path)

    def test_doctor_is_read_only(self):
        with patch.object(cluster.subprocess, "run", return_value=result(stdout=b"host\nhelp\n")) as remote:
            self.assertEqual(cluster.doctor(self.cfg), 0)
        command = remote.call_args.args[0][-1]
        self.assertIn("dsub --help", command)
        self.assertIn("djob --help", command)
        self.assertNotIn("dlogin", command)
        self.assertNotIn("-n ", command)

    def run_wrapper_validation(self, allowed, node_cpus=("0-37", "38-75")):
        wrapper = (cluster.ROOT / "tools/remote_job.sh").read_text()
        code = wrapper.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        (self.run / "remote-settings.json").write_text(json.dumps(cluster.effective_settings(self.cfg, {})))
        (self.run / "environment.log").write_text("HOST=test\n")
        nodes = []
        for number, cpus in enumerate(node_cpus):
            path = self.root / "sys" / f"node{number}" / "cpulist"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(cpus)
            nodes.append(path)
        original_glob = Path.glob
        def fake_glob(path, pattern):
            if str(path) == "/sys/devices/system/node":
                return iter(nodes)
            return original_glob(path, pattern)
        with patch.object(os, "sched_getaffinity", return_value=allowed, create=True), \
                patch.object(Path, "glob", fake_glob), patch.object(sys, "argv", ["-", str(self.run)]):
            exec(compile(code, "remote_job.sh:python", "exec"), {})

    def test_wrapper_checks_exact_cpu_count(self):
        for count in (37, 39):
            with self.subTest(count=count), self.assertRaisesRegex(SystemExit, "exactly 38"):
                self.run_wrapper_validation(set(range(count)))

    def test_wrapper_rejects_allocation_across_numa_nodes(self):
        with self.assertRaisesRegex(SystemExit, "exactly one NUMA"):
            self.run_wrapper_validation(set(range(19)) | set(range(38, 57)))

    def test_wrapper_ignores_memory_only_numa_nodes_with_empty_cpulist(self):
        self.run_wrapper_validation(set(range(38, 76)), ("0-37", "38-75", "", "\n"))
        self.assertIn("NUMA_NODE=1\n", (self.run / "environment.log").read_text())

    def test_wrapper_records_actual_allocation_and_source_hashes(self):
        self.run_wrapper_validation(set(range(38, 76)))
        environment = (self.run / "environment.log").read_text()
        self.assertIn("NUMA_NODE=1\n", environment)
        self.assertIn("ALLOWED_CPUS=38,39,40", environment)
        settings = (self.run / "settings.env").read_text()
        self.assertIn("export NUMA_NODE=1\n", settings)
        self.assertIn("export OMP_NUM_THREADS=38\n", settings)
        parsed = {line.split("  ", 1)[1]: line.split("  ", 1)[0]
                  for line in (self.run / "source-sha256.txt").read_text().splitlines()}
        self.assertEqual(parsed, cluster.source_hashes(self.run / "source"))


if __name__ == "__main__":
    unittest.main()
