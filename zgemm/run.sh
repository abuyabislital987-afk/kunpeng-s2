#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# 官方环境可用时加载；也允许在已配置好的计算节点直接运行。
if ! type module >/dev/null 2>&1 && [[ -f /etc/profile.d/modules.sh ]]; then
    source /etc/profile.d/modules.sh
fi
if type module >/dev/null 2>&1 && [[ -d /home/HPC/HPCKit/latest/modulefiles ]]; then
    module use /home/HPC/HPCKit/latest/modulefiles
    module load gcc/compiler12.3.1/gccmodule
fi
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-38}"
[[ "$OMP_NUM_THREADS" =~ ^[0-9]+$ ]] && (( OMP_NUM_THREADS>=1 && OMP_NUM_THREADS<=38 )) || { echo '需要 1–38 线程'; exit 2; }
export OMP_PROC_BIND=close
export OMP_PLACES=cores
# 根据调度器实际分配的 CPU 选择 NUMA，不能固定占用其他人的节点。
allowed=$(awk '/^Cpus_allowed_list:/ {print $2}' /proc/self/status)
first=${allowed%%,*}; first=${first%%-*}
nodes=(/sys/devices/system/cpu/cpu"$first"/node[0-9]*)
[[ -d "${nodes[0]}" ]] || { echo '无法识别 NUMA'; exit 2; }
node="${NUMA_NODE:-${nodes[0]##*/node}}"
command -v numactl >/dev/null || { echo '需要 numactl'; exit 2; }
mkdir -p results
logdir=$(mktemp -d "results/run-$(date +%Y%m%d-%H%M%S)-XXXXXX")
exec > >(tee "$logdir/summary.log") 2>&1
cc="${CC:-gcc}"
flags=(-O3 -ffp-contract=off -fopenmp)
[[ $(uname -m) != aarch64 ]] || flags+=(-mcpu="${CPU_TARGET:-generic}")
printf 'Compiler: '; "$cc" --version | head -1
printf 'CPUs=%s NUMA=%s threads=%s\n' "$allowed" "$node" "$OMP_NUM_THREADS"
"$cc" "${flags[@]}" bench_zgemm.c zgemm.c -o zgemm_test -lm
runs="${TEST_RUNS:-1}"
[[ "$runs" =~ ^[1-9][0-9]*$ ]] || exit 2
cases=(
  "7427 7427 256"
  "14848 14848 256"
  "37360 8192 512"
)
for i in "${!cases[@]}"; do
    read -r -a dims <<< "${cases[$i]}"
    numactl --cpunodebind="$node" --membind="$node" ./zgemm_test "${dims[@]}" "$runs" | tee "$logdir/case-$i.log"
    # 官方程序 FAIL 也可能退出 0，因此必须检查打印结果。
    if grep -q FAIL "$logdir/case-$i.log" || ! grep -q PASS "$logdir/case-$i.log"; then
        echo "校验失败或没有有效成绩，停止"; exit 1
    fi
done
printf 'All three official cases PASS. Logs: %s\n' "$logdir"
