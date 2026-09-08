#!/usr/bin/env bash
# dsub starts this wrapper through /bin/bash -l to preserve the cluster environment.
set -euo pipefail
TASK_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$TASK_DIR"
exec 3>&1 4>&2
exec > >(tee "$TASK_DIR/benchmark.log") 2>&1
LOG_PID=$!
finish() {
    local code=$? log_code=0
    trap - EXIT
    # Flush the complete benchmark log before declaring the wrapper finished.
    exec 1>&3 2>&4 3>&- 4>&-
    wait "$LOG_PID" || log_code=$?
    if (( code == 0 && log_code != 0 )); then code=$log_code; fi
    printf '%s\n' "$code" > "$TASK_DIR/exit-code.txt"
    exit "$code"
}
trap finish EXIT
printf 'BENCH_JOB_BEGIN %s\n' "$(date -u +%FT%TZ)"
{
    printf 'HOST=%s\n' "$(hostname)"
    printf 'ARCH=%s\n' "$(uname -m)"
    printf 'OMP_NUM_THREADS=38\nCPU_TARGET=generic\n'
} > "$TASK_DIR/environment.log"
[[ "$(uname -s)" == Linux ]] || { echo 'Linux compute node required'; exit 2; }
command -v python3 >/dev/null || { echo 'python3 required for allocation and snapshot checks'; exit 2; }

# JSON is data. shlex.quote prevents environment values from becoming shell code.
python3 - "$TASK_DIR" <<'PY'
import hashlib, json, os, pathlib, shlex, sys
root = pathlib.Path(sys.argv[1])
settings = json.loads((root / 'remote-settings.json').read_text())
environment = settings['environment']
repeats = settings['bench_repeats']
if type(repeats) is not int or not 1 <= repeats <= 100:
    raise SystemExit('Invalid benchmark repeat count')
allowed_keys = {'COMPILER', 'CC', 'MODULE_ROOT', 'CONV_BLOCK', 'CONV_KERNEL_UNROLL',
                'TEST_RUNS', 'KBLAS_LIB', 'OMP_NUM_THREADS', 'OMP_DYNAMIC',
                'OMP_PROC_BIND', 'OMP_PLACES', 'CPU_TARGET'}
if set(environment) - allowed_keys:
    raise SystemExit('Unsupported environment variable')
fixed = {'OMP_NUM_THREADS': '38', 'OMP_DYNAMIC': 'FALSE', 'OMP_PROC_BIND': 'close',
         'OMP_PLACES': 'cores', 'CPU_TARGET': 'generic'}
if any(environment.get(k) != v for k, v in fixed.items()):
    raise SystemExit('Invalid competition environment settings')
with (root / 'source-sha256.txt').open('w') as out:
    for path in sorted((root / 'source').rglob('*')):
        if path.is_symlink():
            raise SystemExit('Source symlinks are forbidden')
        if path.is_file():
            relative = path.relative_to(root / 'source').as_posix()
            out.write(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n')

def cpus(text):
    answer = set()
    if not text.strip():
        return answer  # Kunpeng also exposes memory-only NUMA nodes.
    for part in text.strip().split(','):
        ends = part.split('-')
        answer.update(range(int(ends[0]), int(ends[-1]) + 1))
    return answer

allowed = set(os.sched_getaffinity(0))
with (root / 'environment.log').open('a') as out:
    out.write('ALLOWED_CPUS=' + ','.join(map(str, sorted(allowed))) + '\n')
if len(allowed) != 38:
    raise SystemExit(f'Expected exactly 38 allowed CPUs, got {len(allowed)}: {sorted(allowed)}')
nodes = [p.parent.name[4:] for p in pathlib.Path('/sys/devices/system/node').glob('node[0-9]*/cpulist')
         if allowed <= cpus(p.read_text())]
if len(nodes) != 1:
    raise SystemExit(f'Allowed CPUs must lie in exactly one NUMA node; matching nodes: {nodes}')
environment['NUMA_NODE'] = nodes[0]
environment['BENCH_REPEATS'] = str(repeats)
with (root / 'settings.env').open('w') as out:
    for key, value in sorted(environment.items()):
        out.write(f'export {key}={shlex.quote(str(value))}\n')
with (root / 'environment.log').open('a') as out:
    out.write('NUMA_NODE=' + nodes[0] + '\n')
    out.write(json.dumps(settings, indent=2, sort_keys=True) + '\n')
PY
source "$TASK_DIR/settings.env"
{
    printf 'UTC: %s\n' "$(date -u +%FT%TZ)"
    hostname
    uname -a
    if command -v lscpu >/dev/null; then lscpu || true; fi
    if command -v numactl >/dev/null; then numactl --show || true; fi
    if type module >/dev/null 2>&1; then module list 2>&1 || true; fi
    printf 'Compiler from initial PATH (run.sh also records its actual selected compiler):\n'
    for compiler in gcc clang; do
        if command -v "$compiler" >/dev/null; then "$compiler" --version || true; fi
    done
} >> "$TASK_DIR/environment.log" 2>&1
cat "$TASK_DIR/environment.log"
cd "$TASK_DIR/source"
for ((repeat=1; repeat<=BENCH_REPEATS; repeat++)); do
    printf '\nBENCH_REPEAT %d/%d BEGIN\n' "$repeat" "$BENCH_REPEATS"
    bash ./run.sh
    printf 'BENCH_REPEAT %d/%d END\n' "$repeat" "$BENCH_REPEATS"
done
printf 'BENCH_JOB_END %s\n' "$(date -u +%FT%TZ)"
