#!/usr/bin/env python3
"""Submit immutable benchmark snapshots through SSH; never retry uncertain jobs."""

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
ENV_KEYS = {
    "COMPILER", "CC", "MODULE_ROOT", "CONV_BLOCK", "CONV_KERNEL_UNROLL",
    "TEST_RUNS", "KBLAS_LIB",
}
FIXED_ENV = {
    "OMP_NUM_THREADS": "38", "OMP_DYNAMIC": "FALSE", "OMP_PROC_BIND": "close",
    "OMP_PLACES": "cores", "CPU_TARGET": "generic",
}
ARTIFACTS = ("benchmark.log", "environment.log", "source-sha256.txt", "exit-code.txt", "wrapper.stdout.log")
SOURCE_SUFFIXES = {".c", ".h", ".sh", ".md", ".cc", ".cpp", ".hpp", ".f", ".f90", ".txt"}


class ClusterError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def read_json(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ClusterError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ClusterError(f"Expected a JSON object: {path}")
    return data


def positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ClusterError(f"{name} must be a positive integer")
    return value


def load_config(path):
    path = Path(path).expanduser().resolve()
    cfg = read_json(path)
    host = cfg.get("host", "")
    user = cfg.get("user", "")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ClusterError("Set host to the real SSH hostname or IPv4 address")
    if user and (not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", user)):
        raise ClusterError("Invalid SSH user")
    remote_root = cfg.get("remote_root", "")
    if (not isinstance(remote_root, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", remote_root)
            or any(p in (".", "..") for p in remote_root.split("/")) or remote_root.rstrip("/") == ""):
        raise ClusterError("remote_root must be an absolute path without spaces, '.' or '..' components")
    cfg["remote_root"] = remote_root.rstrip("/")
    cfg["port"] = positive_int(cfg.get("port", 22), "port")
    if cfg["port"] > 65535:
        raise ClusterError("port must be <= 65535")
    cfg["connect_timeout"] = positive_int(cfg.get("connect_timeout", 10), "connect_timeout")
    cfg["command_timeout"] = positive_int(cfg.get("command_timeout", 45), "command_timeout")
    cfg["transfer_timeout"] = positive_int(cfg.get("transfer_timeout", 120), "transfer_timeout")
    if cfg.get("known_hosts"):
        known = Path(cfg["known_hosts"]).expanduser()
        if not known.is_absolute():
            known = path.parent / known
        if not known.is_file():
            raise ClusterError(f"known_hosts file does not exist: {known}")
        cfg["known_hosts"] = str(known.resolve())
    if cfg.get("control_path"):
        control_path = cfg["control_path"]
        if not isinstance(control_path, str) or "\x00" in control_path or not Path(control_path).is_absolute():
            raise ClusterError("control_path must be an absolute local SSH control socket path")
        cfg["control_path"] = str(Path(control_path).resolve())
    scheduler = cfg.setdefault("scheduler", {})
    if not isinstance(scheduler, dict):
        raise ClusterError("scheduler must be an object")
    defaults = {"queue": "q_kunpeng", "cpus": 38, "memory_mb": 24576,
                "numa_count": 1, "numa_distribution": "pack", "walltime_seconds": 1800}
    for key, value in defaults.items():
        scheduler.setdefault(key, value)
    for key in ("cpus", "memory_mb", "numa_count", "walltime_seconds"):
        positive_int(scheduler[key], key)
    if scheduler["cpus"] != 38 or scheduler["numa_count"] != 1 or scheduler["numa_distribution"] != "pack":
        raise ClusterError("Competition runner requires 38 CPUs on one packed NUMA node")
    if not isinstance(scheduler["queue"], str) or not scheduler["queue"] or "\x00" in scheduler["queue"]:
        raise ClusterError("queue must be a nonempty string")
    status_argv = scheduler.get("status_argv")
    if status_argv is not None and (not isinstance(status_argv, list) or not status_argv
            or not all(isinstance(x, str) and x and "\x00" not in x for x in status_argv)
            or not any("{job_id}" in x for x in status_argv)):
        raise ClusterError("status_argv must be an argv array containing {job_id}, or null until verified")
    pattern = scheduler.setdefault("job_id_pattern", r"(?im)\bjob\s*(?:<|id\s*[:=]?\s*)([0-9]+)>?")
    try:
        if re.compile(pattern).groups != 1:
            raise ValueError("exactly one capture group is required")
    except (TypeError, ValueError, re.error) as exc:
        raise ClusterError(f"Invalid job_id_pattern: {exc}") from exc
    return cfg


def ssh_argv(cfg, command):
    result = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
              "-o", "NumberOfPasswordPrompts=0", "-o", f"ConnectTimeout={cfg['connect_timeout']}",
              "-p", str(cfg["port"])]
    if cfg.get("known_hosts"):
        result += ["-o", "UserKnownHostsFile=" + cfg["known_hosts"]]
    if cfg.get("control_path"):
        result += ["-o", "ControlMaster=no", "-S", cfg["control_path"]]
    destination = (cfg["user"] + "@" if cfg.get("user") else "") + cfg["host"]
    return result + [destination, command]


def remote(cfg, command, **kwargs):
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    kwargs.setdefault("timeout", cfg["command_timeout"])
    return subprocess.run(ssh_argv(cfg, command), check=False, **kwargs)


def output_text(result):
    def decode(value):
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")
    return decode(result.stdout) + decode(result.stderr)


def source_hashes(source):
    if not source.is_dir() or source.is_symlink():
        raise ClusterError("source must be a real directory, not a symlink")
    files = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ClusterError(f"Source symlinks are not allowed: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(source).as_posix()
        if any(ord(c) < 32 for c in relative) or "\\" in relative:
            raise ClusterError(f"Unsupported source filename: {relative!r}")
        if (not path.is_file() or (path.suffix.lower() not in SOURCE_SUFFIXES
                and path.name not in ("Makefile", "CMakeLists.txt"))):
            raise ClusterError(f"Source snapshot contains a non-source file: {relative}")
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files or "run.sh" not in files:
        raise ClusterError("source/run.sh and benchmark sources are required")
    return files


def effective_settings(cfg, experiment):
    requested = experiment.get("settings", {})
    if not isinstance(requested, dict):
        raise ClusterError("experiment.settings must be an object")
    if not isinstance(cfg.get("environment", {}), dict) or not isinstance(requested.get("environment", {}), dict):
        raise ClusterError("environment must be an object")
    environment = dict(cfg.get("environment", {}))
    environment.update(requested.get("environment", {}))
    for key, value in environment.items():
        if key not in ENV_KEYS | FIXED_ENV.keys() or not isinstance(value, (str, int)) or "\x00" in str(value):
            raise ClusterError(f"Unsupported environment setting: {key}")
        if key in FIXED_ENV and str(value) != FIXED_ENV[key]:
            raise ClusterError(f"Competition setting {key} must remain {FIXED_ENV[key]}")
    environment = {key: str(value) for key, value in environment.items()}
    environment.update(FIXED_ENV)
    repeats = positive_int(requested.get("bench_repeats", cfg.get("bench_repeats", 3)), "bench_repeats")
    if repeats > 100:
        raise ClusterError("bench_repeats must be <= 100")
    return {"bench_repeats": repeats, "environment": environment,
            "scheduler_resources": {k: cfg["scheduler"][k] for k in
                                    ("queue", "cpus", "memory_mb", "numa_count", "numa_distribution", "walltime_seconds")},
            "numa_policy": "38 allowed CPUs within one NUMA node"}


def archive_source(run, hashes, settings, destination):
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(run / "source", arcname="source", recursive=False)
        for relative in hashes:
            archive.add(run / "source" / relative, arcname="source/" + relative, recursive=False)
        archive.add(ROOT / "tools" / "remote_job.sh", arcname="remote_job.sh", recursive=False)
        blob = (json.dumps(settings, indent=2) + "\n").encode()
        info = tarfile.TarInfo("remote-settings.json")
        info.size = len(blob)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(blob))


def submit_command(cfg, remote_dir, name):
    s = cfg["scheduler"]
    return shlex.join(["dsub", "-n", name, "-q", s["queue"], "-R",
                       f"cpu={s['cpus']},mem={s['memory_mb']}", "-a",
                       f"numa[count={s['numa_count']},distribution={s['numa_distribution']}]",
                       "-T", str(s["walltime_seconds"]), "-o", remote_dir + "/wrapper.stdout.log",
                       "/bin/bash", "-l", remote_dir + "/remote_job.sh"])


def doctor(cfg):
    command = ('failed=0; hostname || failed=1; uname -a || failed=1; '
               'for tool in python3 dsub djob; do command -v "$tool" || failed=1; done; '
               'dsub --help; djob --help; exit "$failed"')
    result = remote(cfg, command)
    print(output_text(result), end="")
    if not cfg["scheduler"].get("status_argv"):
        print("\nstatus_argv is unconfigured. Verify djob help, then configure its read-only verbose/JSON status command with {job_id}.")
    return result.returncode


def submit(cfg, run):
    manifest_path = run / "cluster.json"
    if manifest_path.exists():
        raise ClusterError("cluster.json already exists: refusing duplicate submission. Inspect status or use a new experiment.")
    experiment = read_json(run / "experiment.json")
    hashes = source_hashes(run / "source")
    settings = effective_settings(cfg, experiment)
    token = uuid4().hex[:12]
    name = "kp-" + re.sub(r"[^A-Za-z0-9_-]", "-", run.name)[:35] + "-" + token
    remote_dir = cfg["remote_root"] + "/" + name
    manifest = {"state": "preparing", "created_at": now(), "remote_dir": remote_dir,
                "host": cfg["host"], "user": cfg.get("user", ""), "port": cfg["port"],
                "settings": settings, "source_hashes": hashes, "job_id": None}
    # Exclusive creation also prevents two local agents from submitting the same run.
    try:
        with manifest_path.open("x") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
    except FileExistsError as exc:
        raise ClusterError("Another process already started this submission") from exc
    try:
        with tempfile.TemporaryDirectory(prefix="kunpeng-upload-") as temporary:
            temporary = Path(temporary)
            payload = temporary / "payload"
            # Hash and tar the same private snapshot; edits to the live candidate
            # while SSH is running cannot change the submitted payload.
            shutil.copytree(run / "source", payload / "source", symlinks=True)
            hashes = source_hashes(payload / "source")
            manifest["source_hashes"] = hashes
            write_json(manifest_path, manifest)
            archive_path = temporary / "source.tar.gz"
            archive_source(payload, hashes, settings, archive_path)
            # Never merge with an existing remote run, even if a token ever collides.
            command = (shlex.join(["mkdir", "-p", cfg["remote_root"]]) + " && "
                       + shlex.join(["mkdir", remote_dir]) + " && "
                       + shlex.join(["tar", "-xzf", "-", "-C", remote_dir]))
            with archive_path.open("rb") as handle:
                uploaded = remote(cfg, command, stdin=handle, timeout=cfg["transfer_timeout"])
            (run / "upload.log").write_text(output_text(uploaded))
            if uploaded.returncode:
                raise ClusterError("Upload failed; see upload.log. No scheduler submission was attempted.")
        # A disconnect after this point is ambiguous: persist the guard before dsub.
        manifest["state"] = "submit_unknown"
        manifest["submission_started_at"] = now()
        manifest["submit_command"] = submit_command(cfg, remote_dir, name)
        write_json(manifest_path, manifest)
        result = remote(cfg, manifest["submit_command"])
        output = output_text(result)
        (run / "submit.log").write_text(output)
        matches = re.findall(cfg["scheduler"]["job_id_pattern"], output)
        if result.returncode or len(set(matches)) != 1 or not re.fullmatch(r"[0-9]+", matches[0]):
            raise ClusterError("Submission outcome is uncertain. Do not resubmit; inspect submit.log and the scheduler.")
        manifest.update(state="submitted", job_id=matches[0], submitted_at=now())
        write_json(manifest_path, manifest)
        print(f"Submitted job {manifest['job_id']}: {remote_dir}")
        return 0
    except (OSError, subprocess.SubprocessError, ClusterError) as exc:
        if manifest["state"] == "preparing":
            manifest["state"] = "upload_failed"
        manifest["error"] = str(exc)
        write_json(manifest_path, manifest)
        raise ClusterError(str(exc)) from exc


def read_manifest(cfg, run):
    manifest = read_json(run / "cluster.json")
    if (manifest.get("host"), manifest.get("user", ""), manifest.get("port")) != (
            cfg["host"], cfg.get("user", ""), cfg["port"]):
        raise ClusterError("Config SSH destination differs from this run's submission")
    remote_dir = manifest.get("remote_dir", "")
    if not isinstance(remote_dir, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", remote_dir) or ".." in PurePosixPath(remote_dir).parts:
        raise ClusterError("Invalid remote_dir in cluster.json")
    return manifest


def parse_scheduler_status(output, job_id):
    """Accept explicit JSON or the verified Donau djob -ll field layout."""
    try:
        data = json.loads(output)
    except ValueError:
        data = {}
        for key in ("jobId", "job_id", "state", "status", "jobExitCode", "systemExitCode"):
            values = re.findall(r"^[ \t]*" + key + r"[ \t]*(?:[:=][ \t]*|[ \t]+)(\S+)[ \t]*$", output, re.M)
            if len(values) > 1:
                return None
            if values:
                data[key] = values[0]
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        data = data["jobs"]
    if isinstance(data, list):
        matching = [row for row in data if isinstance(row, dict)
                    and str(row.get("jobId", row.get("job_id", ""))) == job_id]
        data = matching[0] if len(matching) == 1 else None
    if not isinstance(data, dict):
        return None
    state = data.get("state", data.get("status"))
    if not isinstance(state, str) or ("state" in data and "status" in data and data["state"] != data["status"]):
        return None
    returned_id = data.get("jobId", data.get("job_id"))
    if returned_id is None or str(returned_id) != job_id:
        return None
    parsed = {"status": state, "state": state, "jobId": returned_id}
    for key in ("jobExitCode", "systemExitCode"):
        value = str(data.get(key))
        parsed[key] = int(value) if re.fullmatch(r"-?[0-9]+", value) else None
    return parsed


def status(cfg, run):
    manifest = read_manifest(cfg, run)
    job_id = manifest.get("job_id")
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9]+", job_id):
        raise ClusterError("No confirmed job ID; inspect submit.log and reconcile the uncertain submission manually")
    argv = cfg["scheduler"].get("status_argv")
    if not argv:
        raise ClusterError("status_argv is unconfigured; run doctor and verify djob's read-only verbose/JSON status syntax first")
    command = shlex.join([part.replace("{job_id}", job_id) for part in argv])
    try:
        result = remote(cfg, command)
        output = output_text(result)
        (run / "scheduler-status.txt").write_text(output)
        manifest["status_checked_at"] = now()
        manifest["status_returncode"] = result.returncode
        # Clear stale successful evidence on every failed/unparseable query.
        manifest["scheduler_status"] = parse_scheduler_status(result.stdout.decode("utf-8", errors="replace"), job_id) if result.returncode == 0 else None
        write_json(run / "cluster.json", manifest)
        print(output, end="" if output.endswith("\n") else "\n")
        if result.returncode:
            raise ClusterError("Scheduler status query failed; see scheduler-status.txt")
        if manifest["scheduler_status"] is None:
            print("Status text saved; scheduler job/status/exit-code fields could not be verified.")
        return 0
    except (OSError, subprocess.SubprocessError) as exc:
        manifest.update(scheduler_status=None, status_checked_at=now(), status_error=str(exc))
        write_json(run / "cluster.json", manifest)
        (run / "scheduler-status.txt").write_text(str(exc) + "\n")
        raise ClusterError(f"Status query failed: {exc}") from exc


def extract_artifacts(archive_path, target):
    total = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            parts = PurePosixPath(name).parts
            if (not parts or name.startswith("/") or ".." in parts
                    or not (name in ARTIFACTS or name == "source/results" or name.startswith("source/results/"))
                    or not (member.isfile() or member.isdir())):
                raise ClusterError(f"Unsafe artifact archive entry: {member.name}")
            total += member.size
            if total > 2 * 1024 ** 3:
                raise ClusterError("Artifact archive exceeds the 2 GiB extraction limit")
            relative = "remote-results" + name[len("source/results"):] if name.startswith("source/results") else name
            destination = target / relative
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)


def fetch(cfg, run):
    manifest = read_manifest(cfg, run)
    command = ("cd " + shlex.quote(manifest["remote_dir"]) + " && (set --; "
               "for f in " + shlex.join(list(ARTIFACTS) + ["source/results"]) + "; do "
               'if [ -e "$f" ]; then set -- "$@" "$f"; fi; done; '
               '[ "$#" -gt 0 ] && tar -czf - "$@")')
    try:
        with tempfile.TemporaryDirectory(prefix="kunpeng-fetch-") as temporary:
            temporary = Path(temporary)
            archive_path = temporary / "artifacts.tar.gz"
            with archive_path.open("wb") as handle:
                result = remote(cfg, command, stdout=handle, timeout=cfg["transfer_timeout"])
            (run / "fetch.log").write_text(output_text(result))
            if result.returncode:
                raise ClusterError("Artifact transfer failed; see fetch.log")
            staging = temporary / "extracted"
            staging.mkdir()
            extract_artifacts(archive_path, staging)
            for path in staging.iterdir():
                destination = run / path.name
                if destination.is_symlink():
                    raise ClusterError(f"Refusing to overwrite a symlink: {destination}")
                if path.is_dir():
                    if destination.exists():
                        # Existing fetched artifacts are disposable; source/ is never touched.
                        shutil.rmtree(destination)
                    shutil.copytree(path, destination)
                else:
                    shutil.copyfile(path, destination)
            complete = all((staging / name).is_file() for name in ARTIFACTS[:4])
            artifact_hashes = {name: hashlib.sha256((staging / name).read_bytes()).hexdigest()
                               for name in ARTIFACTS[:4] if (staging / name).is_file()}
        manifest.update(fetch_state="complete" if complete else "incomplete", fetched_at=now(), artifacts_sha256=artifact_hashes)
        manifest.pop("fetch_error", None)
        write_json(run / "cluster.json", manifest)
        print(f"Fetched artifacts to {run}; {'wrapper finished' if complete else 'job artifacts are incomplete'}. Benchmark PASS still requires log validation.")
        return 0 if complete else 2
    except (OSError, subprocess.SubprocessError, tarfile.TarError, ClusterError) as exc:
        manifest.update(fetch_state="failed", fetch_error=str(exc), fetched_at=now(), artifacts_sha256={})
        write_json(run / "cluster.json", manifest)
        raise ClusterError(str(exc)) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "cluster.local.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Read-only SSH and scheduler help checks")
    for name in ("submit", "status", "fetch"):
        command = commands.add_parser(name)
        command.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == "doctor":
            return doctor(cfg)
        run = args.run_dir.expanduser().resolve()
        if not run.is_dir():
            raise ClusterError(f"Run directory does not exist: {run}")
        return {"submit": submit, "status": status, "fetch": fetch}[args.command](cfg, run)
    except (ClusterError, OSError, subprocess.SubprocessError) as exc:
        print(f"cluster: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
