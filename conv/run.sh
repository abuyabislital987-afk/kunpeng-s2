#!/usr/bin/env bash
set -euo pipefail

# 无论从哪个目录启动脚本，都切到脚本所在的提交目录。
cd "$(dirname "$0")"

# 可切换两套比赛编译器；尚未实测，默认 gcc 不代表它已经被证明更快。
COMPILER="${COMPILER:-gcc}"
case "$COMPILER" in
    gcc) COMPILER_MODULE=gcc/compiler12.3.1/gccmodule; DEFAULT_CC=gcc ;;
    bisheng) COMPILER_MODULE=bisheng/compiler5.0.0.2/bishengmodule; DEFAULT_CC=clang ;;
    *) echo "COMPILER must be gcc or bisheng" >&2; exit 2 ;;
esac
MODULE_ROOT="${MODULE_ROOT:-/home/HPC/HPCKit/latest/modulefiles}"
if type module >/dev/null 2>&1 && [[ -d "$MODULE_ROOT" ]]; then
    module use "$MODULE_ROOT"
    module load "$COMPILER_MODULE"
elif [[ "$(uname -s)" == Linux ]]; then
    printf 'Compiler modules unavailable at %s; using compiler from PATH.\n' "$MODULE_ROOT" >&2
fi

CC="${CC:-$DEFAULT_CC}"
CONV_BLOCK="${CONV_BLOCK:-32}"
CONV_KERNEL_UNROLL="${CONV_KERNEL_UNROLL:-2}"
[[ "$CONV_BLOCK" =~ ^[1-9][0-9]{0,5}$ ]] || {
    echo "CONV_BLOCK must be a positive integer <= 999999" >&2; exit 2;
}
case "$CONV_KERNEL_UNROLL" in
    1|2) ;;
    *) echo "CONV_KERNEL_UNROLL must be 1 or 2" >&2; exit 2 ;;
esac

# 官方上限为 38 线程。允许调用者减少线程数，但不允许脚本意外超配。
THREADS="${OMP_NUM_THREADS:-38}"
if ! [[ "$THREADS" =~ ^[1-9][0-9]{0,5}$ ]]; then
    echo "OMP_NUM_THREADS must be a positive integer" >&2
    exit 2
fi
if (( THREADS > 38 )); then
    THREADS=38
fi

ARCH_FLAGS=()
case "$(uname -m)" in
    # native 让正式 Kunpeng 编译器选择本机可用的 ARM/NEON 指令；非 ARM 不添加。
    aarch64|arm64) ARCH_FLAGS=("-mcpu=${CPU_TARGET:-generic}") ;;
esac

# Linux 必须绑定单个 NUMA 节点，缺少工具时不能静默降级为跨节点运行。
RUN_PREFIX=()
if [[ "$(uname -s)" == Linux ]]; then
    command -v numactl >/dev/null || {
        echo "numactl is required for the competition run" >&2; exit 2;
    }
    # Pick a NUMA node inside the scheduler's CPU allocation when no override is set.
    if [[ -z "${NUMA_NODE:-}" ]]; then
        allowed_cpus="$(awk '/^Cpus_allowed_list:/ {print $2}' /proc/self/status)"
        first_cpu="${allowed_cpus%%,*}"
        first_cpu="${first_cpu%%-*}"
        node_paths=(/sys/devices/system/cpu/cpu"$first_cpu"/node[0-9]*)
        [[ -d "${node_paths[0]}" ]] || { echo 'Cannot identify allocated NUMA node' >&2; exit 2; }
        NUMA_NODE="${node_paths[0]##*/node}"
    fi
    [[ "$NUMA_NODE" =~ ^[0-9]+$ ]] || exit 2
    node_cpus="$(numactl --hardware | awk -v node="$NUMA_NODE" \
        '$1 == "node" && $2 == node && $3 == "cpus:" {print NF - 3}')"
    [[ "$node_cpus" =~ ^[1-9][0-9]*$ ]] || {
        echo "NUMA node $NUMA_NODE has no CPU list" >&2; exit 2;
    }
    (( THREADS <= node_cpus )) || THREADS="$node_cpus"
    RUN_PREFIX=(numactl -N "$NUMA_NODE")
fi

# Apple Clang 的 OpenMP 运行库需要额外搜索路径；比赛 Linux 使用普通 -fopenmp。
OMP_FLAGS=(-fopenmp)
LINK_FLAGS=(-lm)
if [[ "$(uname -s)" == Darwin ]]; then
    LIBOMP_PREFIX="${LIBOMP_PREFIX:-/opt/homebrew/opt/libomp}"
    OMP_FLAGS=(-Xpreprocessor -fopenmp "-I$LIBOMP_PREFIX/include")
    LINK_FLAGS+=("-L$LIBOMP_PREFIX/lib" "-Wl,-rpath,$LIBOMP_PREFIX/lib" -lomp)
    local_cpus="$(/usr/sbin/sysctl -n hw.physicalcpu)"
    (( THREADS <= local_cpus )) || THREADS="$local_cpus"
fi
export OMP_NUM_THREADS="$THREADS"
export OMP_DYNAMIC=FALSE
export OMP_PROC_BIND="${OMP_PROC_BIND:-close}"
export OMP_PLACES="${OMP_PLACES:-cores}"

# 独立日志目录避免覆盖既有实验；参数也一起保留，便于之后比较。
mkdir -p results
RUN_DIR="$(mktemp -d "$PWD/results/run-$(date +%Y%m%d-%H%M%S)-XXXXXX")"
{
    "$CC" --version
    printf 'Host=%s CPU target=%s\n' "$(hostname)" "${CPU_TARGET:-generic}"
    printf 'Block=%s Unroll=%s Threads=%s Bind=%s Places=%s Node=%s\n' \
        "$CONV_BLOCK" "$CONV_KERNEL_UNROLL" "$THREADS" \
        "$OMP_PROC_BIND" "$OMP_PLACES" "${NUMA_NODE:-none}"
} | tee "$RUN_DIR/environment.log"

# benchmark 和优化代码统一关闭 FMA contraction；不能用 *.c 混入备份版本。
"$CC" -O3 -std=c11 -Wall -Wextra -fno-fast-math -ffp-contract=off \
    "${ARCH_FLAGS[@]}" "${OMP_FLAGS[@]}" \
    "-DCONV_BLOCK=$CONV_BLOCK" "-DCONV_KERNEL_UNROLL=$CONV_KERNEL_UNROLL" \
    bench_conv.c conv2d.c -o "$RUN_DIR/conv2d_test" "${LINK_FLAGS[@]}" \
    2>&1 | tee "$RUN_DIR/build.log"

run_case() {
    local number="$1"
    shift
    "${RUN_PREFIX[@]}" "$RUN_DIR/conv2d_test" "$@" 1 | tee "$RUN_DIR/case-$number.log"
    # 官方 main 在 FAIL 时也可能返回 0，仅靠 set -e 无法识别失败。
    # 检查输出文本，不改变 reference、计时区或成绩。
    if ! awk '$NF == "PASS" {pass++} $NF == "FAIL" {fail++}
              END {exit !(pass == 1 && fail == 0)}' "$RUN_DIR/case-$number.log"; then
        echo "Case $number did not report PASS; see $RUN_DIR" >&2
        exit 1
    fi
}
run_case 1 4096 6144 39 39
run_case 2 6144 4096 41 41
run_case 3 4256 6390 55 55
run_case 4 6390 4256 81 81

# 官方每行第七字段是时间。求和的是已舍入至 0.01ms 的打印值。
awk '$NF == "PASS" {total += $7; count++}
     END {printf "PASS cases: %d; total of printed times: %.2f ms\n", count, total}' \
    "$RUN_DIR"/case-*.log | tee "$RUN_DIR/summary.log"
printf 'Results: %s\n' "$RUN_DIR"
