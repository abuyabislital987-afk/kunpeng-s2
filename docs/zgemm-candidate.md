# ZGEMM 候选 Z1-pack

当前状态：**候选已编码，本机正确性通过，尚未在鲲鹏测量，未晋级。** 2026-09-08 创建。当前共享题解仍为 Z0；历史成绩不能替代当前环境基线复测。

## 单一假设与代码依据

Z0 使用 3×4 NEON 三实乘（3M）微内核，MB=24。A 原来按行保存 `[real, imag, real+imag]`，内核每个 K 从相隔 `K×24` 字节的三行读取九个标量。Z1-pack 只改变 A 的微面板打包及对应读取：每三行按 K 连续存放 `[r0,r1,r2,i0,i1,i2,t0,t1,t2]`，配合向量 lane FMA。目标是减少 A 的加载指令、地址计算和独立访存流。MR、NR、MB、B 布局、线程任务划分、3M 累加顺序及输出缩放保持一致。

分配、尾行补零和非 NEON/尾行通用路径同步适配新布局；分配溢出检查使用 `ceil(M/3)×K×9`，以覆盖最多两行补零。总 A 容量只增加尾部补齐部分。

硬件证据来自 `records/evidence/final-1484056/environment.txt`：aarch64、HiSilicon，608 个 L1D/L2 实例合计 19/456 MiB，折算约 32 KiB L1D 和 768 KiB L2/实例。K=512 时三行 A 为 36 KiB，MB24 的 A 为 288 KiB，单 B 微面板为 48 KiB。**本候选没有缩小这些工作集，也没有解决整个 B 被每个行块重新遍历的问题**；它检验的是加载路径是否限制内核，而非声称解决缓存容量瓶颈。

Apple clang17 本地发布配置汇编提供了实现证据：Z0 K 循环加载 A 用 3 条 `ld1r` 和 6 条 `ldr`；Z1 用 2 条 `ldp q` 和 1 条 `ldr d`，两者均保留 18 条向量 FMA。候选 K 循环未出现寄存器溢出到栈。GCC10 在目标服务器的指令选择仍待核实；Mac 汇编不是鲲鹏速度证据。

## 源码与留存文件

- 候选：`.runs/zgemm/Z1-pack/source/zgemm.c`
- 候选 SHA-256：`3a26f10b2080d057cace983db2fa26c375e654e219991e23e2e61eb58baefba9`
- Z0 `zgemm/zgemm.c` SHA-256：`1bf7a5630cbb5c75c21ff3b1407829ab3d26f0ea007510f819ec4a41fb9d1690`
- 创建命令、构建命令：`.runs/zgemm/Z1-pack/local/commands.txt`
- 本机测试：`local/check-zgemm.c`、`local/build.log`、`local/check-1t.log`、`local/check-4t.log`
- 本机汇编：`local/zgemm-native.s`、`local/zgemm-z0-native.s`

以上 `local/` 均位于 `.runs/zgemm/Z1-pack/` 下。`.runs/` 不纳入 Git，跨机器共享候选时须显式交换源文件和哈希。

## 改动与测试记录

1. 阅读 AGENTS、README、BASELINES、PLAYBOOK、Z0 源码、硬件证据；工作分支为 `setup/agent-workflow`，已有大量协作者未跟踪文件，未覆盖他人文件。
2. 执行下列命令创建历史 Z0 子快照，工具成功；由于尚无当前测量，不视为可晋级基线。

   ```bash
   python3 tools/experiment.py new zgemm Z1-pack --parent Z0 --strategy '仅重排A为3行K优先的连续微面板并用NEON向量lane读取9个分量，减少内核A的独立加载流和load指令；保留3x4形状、MB24及3M运算顺序'
   ```

3. 一次性修改候选内核：A packing、向量 lane 读取、尾部读取、大小检查。没有修改 benchmark、run.sh、工具或当前最佳。
4. 从上层 `other-problems/tests/check-final.c` 派生仅含 ZGEMM 的本机检查到候选 `local/`，未修改原测试。保留 5 组原尺寸，增加 `(23,8,511)`、`(24,8,512)`、`(25,9,513)`、`(47,13,512)`、`(48,12,257)`、`(49,7,256)`。共 594 组，覆盖行/列主序、N/T/C、alpha=0、beta=0、K=0、padding、MR/MB 与 K 边界。
5. 在 Mac ARM64 使用 Apple clang 17.0.0、OpenMP、`-O3 -ffp-contract=off` 与 UBSan 构建。构建退出 0，无输出；1 和 4 线程分别通过 594 组，最大 long-double 参考差均为 `3.076e-14`，低于 `1e-10`。Mac 的 long double 精度由平台实现决定；这是本机回归，不替代官方参考库校验。完整命令如下（仓库根目录执行）：

   ```bash
   clang -O3 -ffp-contract=off -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include -L/opt/homebrew/opt/libomp/lib -Wl,-rpath,/opt/homebrew/opt/libomp/lib -lomp -fsanitize=undefined -fno-sanitize-recover=undefined .runs/zgemm/Z1-pack/source/zgemm.c .runs/zgemm/Z1-pack/local/check-zgemm.c -o .runs/zgemm/Z1-pack/local/check-zgemm
   OMP_NUM_THREADS=1 .runs/zgemm/Z1-pack/local/check-zgemm
   OMP_NUM_THREADS=4 .runs/zgemm/Z1-pack/local/check-zgemm
   clang -O3 -ffp-contract=off -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include -S .runs/zgemm/Z1-pack/source/zgemm.c -o .runs/zgemm/Z1-pack/local/zgemm-native.s
   clang -O3 -ffp-contract=off -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include -S zgemm/zgemm.c -o .runs/zgemm/Z1-pack/local/zgemm-z0-native.s
   ```

6. 只做正确性与汇编检查，没有跑 Mac 官方大尺寸性能，没有取得候选 GFLOPS、平台分数或排名。没有失败/退化性能样本可以登记；待测状态如实保留。

## 统一排队后的待办

由主协调 Agent 排队，其他题不得同时占用同一测量资源。先对不变 Z0 建立当前环境三轮完整基线并登记/晋级，再用完全相同节点、38 线程、NUMA/绑核、GCC/flags、参考库与 `TEST_RUNS` 测 Z1-pack 三轮完整套件。wrapper 三轮独立套件不等于 benchmark 内部 `TEST_RUNS`。

候选路径已可交给 `python3 tools/cluster.py --config config/cluster.local.json submit .runs/zgemm/Z1-pack`；提交后保存 job ID，只查询该作业，再取回原始日志、状态、环境、源码哈希和退出码。检查三种官方尺寸每轮全部 PASS，不能只看退出码。使用 `experiment.py record` 登记实际环境/参考库，再 `compare zgemm Z0 Z1-pack`。只有同环境三轮结果支持且逐组没有不允许的退化才晋级。

风险与下一步：目标 GCC 的寄存器分配、A 打包成本增加、B 带宽限制可能抵消加载收益。若性能退化或处于噪声内，保留 Z0 并记录结果；下一候选应再独立验证二维 cache blocking 等思路，不混在 Z1-pack 里。
