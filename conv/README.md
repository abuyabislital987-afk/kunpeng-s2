# CONV 优化赛题提交

运行 `bash run.sh`（或 `./run.sh`）自动编译并运行四组官方测试。

- `conv2d.c`：经过四组服务器校验的 V2 卷积实现。
- `bench_conv.c`：官方测试程序，逐字节保持原样。
- `run.sh`：编译、单 NUMA 节点运行、检查 PASS 并保存本次日志。

默认 GCC，ARM 使用 `-mcpu=generic`；禁用 fast-math 和 FMA contraction。具备官方 HPCkit module 路径时加载 GCC 12.3.1，否则使用 PATH 中的 gcc。最多 38 线程，默认使用当前 CPU 分配中的第一个 NUMA 节点；可用 `NUMA_NODE` 和 `OMP_NUM_THREADS` 指定资源。

输出包含四组官方 benchmark 的原始性能及校验结果，完整日志保存于新建的 `results/run-*` 目录。
