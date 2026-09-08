# CONV2D 比赛计算节点实测

2026-09-07，当前 `conv2d.c` V2 在比赛 Donau 队列 `q_kunpeng` 完成四组公开用例，作业 **1483453** 状态为 **SUCCEEDED**。四组均 PASS，最大绝对误差均为 0。

| 输入尺寸 | 卷积核 | 耗时（ms） | GFLOPS | 校验 |
|---|---|---:|---:|---|
| 4096 × 6144 | 39 × 39 | 224.42 | 335.8663 | PASS |
| 6144 × 4096 | 41 × 41 | 241.21 | 345.0793 | PASS |
| 4256 × 6390 | 55 × 55 | 449.97 | 357.9701 | PASS |
| 6390 × 4256 | 81 × 81 | 966.07 | 357.9154 | PASS |

四组打印耗时合计 **1881.67 ms**。每组正式计时一次，另有参考校验和一次预热；作业运行时间为 18:36:13–18:36:43。耗时总和只包含算子计时，不包含编译、参考计算和队列等待。

## 运行环境

- 登录：`huzhenghong@10.44.9.4`；计算节点：`cn22965`，AArch64。
- 资源：单个 NUMA 节点 38 核，调度器分配 CPU `190-227`，对应 NUMA node 5。使用分配到的节点，而不是固定绑定示例中的 node 1。
- 编译器：系统 GCC 10.3.1。原脚本预设的 `/home/HPC/HPCKit/latest/modulefiles` 在此环境不存在，未使用预设 GCC 12.3.1。
- 编译：`-O3 -std=c11 -Wall -Wextra -fno-fast-math -ffp-contract=off -mcpu=generic -fopenmp -DCONV_BLOCK=32 -DCONV_KERNEL_UNROLL=2`，链接 `-lm`。
- OpenMP：38 线程，`OMP_DYNAMIC=FALSE`、`OMP_PROC_BIND=close`、`OMP_PLACES=cores`。
- 远端目录：`/home/share/huzhenghong/conv-20260907-1835`。
- 本地及上传的算子、benchmark、脚本 SHA-256 一致，记录于 `source-sha256.txt`。

## 复现

在远端目录保留 `conv2d.c`、`bench_conv.c`、`run.sh` 与 `remote/run_donau.sh` 的副本（远端文件名为 `run_donau.sh`）。登录服务器并完成 `dlogin` 后，从该目录提交：

```bash
dsub -n conv-v2-four-cases -q q_kunpeng \
  -R 'cpu=38,mem=4096' -a 'numa[count=1,distribution=pack]' \
  -T 1800 -o "$PWD/benchmark-repeat.log" \
  /bin/bash -l "$PWD/run_donau.sh"
```

## 结果范围

这是一次当前版本的服务器实测；未运行 baseline 对照，不能据此给出加速比或排名。未向比赛网站提交成绩。用户提供的官方比赛链接是动态页面，本轮未成功取得详细规则或榜单，因此上述参数依据项目内 README 和实际资源分配。

官方 `bench_conv.c` 原有的未使用变量、自引用未初始化变量警告仍存在；本次保持 benchmark 和校验容差不变。PASS 是该 benchmark 的实际输出，不代表消除了其原有代码问题。

`benchmark.log` 为完整编译和运行日志；`run-20260907-183616-oHTJkh/` 包含编译产物、环境、四组原始日志与汇总；`job-status.txt` 为调度器完成记录。
