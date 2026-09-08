# 已验证的起点：C0 / Z0 / T0

本仓库的三个初始版本来自现有提交包。历史记录于 2026-09-08 导入；没有为本次导入重新运行远程作业。它们是已有正确性和性能证据的起点，尚未证明是全局最快实现，也没有排行榜名次或前百分位成绩。

机器可读台账见 [records/history.json](../records/history.json)。版本保存源码快照，运行记录保存每次测量：同一源码的复测只增加 run，不因一次跑得更快就产生 C1、Z1 或 T1。

## 基线结果

|版本|实现|参考作业|官方尺寸数|每组计时次数|打印耗时合计|正确性|
|---|---|---|---:|---:|---:|---|
|C0|CONV V2，32 路输出分块、固定尾块、kernel 两步展开|1483453|4|1|1881.67 ms|4/4 PASS，最大误差 0|
|Z0|3×4 NEON 三乘法复数矩阵乘，24 行缓存块|1484056|3|3|4566.78 ms|3/3 PASS，最大误差 6.04e-12|
|T0|4×8 打包前代与 256 行分块更新结合|1484056|3|3|777.85 ms|3/3 PASS，最大误差 1.11e-15|

合计是官方程序打印的各组平均毫秒数之和，每项已舍入到 0.01 ms。它用于同题性能比较，不是官方得分；不同题的合计不可相互排名。日志没有保存单次采样时间或方差。

### C0：CONV

|输入 H×W|卷积核 H×W|平均耗时 ms|GFLOPS|最大误差|
|---|---|---:|---:|---:|
|4096×6144|39×39|224.42|335.8663|0|
|6144×4096|41×41|241.21|345.0793|0|
|4256×6390|55×55|449.97|357.9701|0|
|6390×4256|81×81|966.07|357.9154|0|

参考作业运行于 cn22965、q_kunpeng、单 NUMA 5，CPU 190–227，38 线程；GCC 10.3.1，`-O3 -fno-fast-math -ffp-contract=off -mcpu=generic -fopenmp`，`CONV_BLOCK=32`、`CONV_KERNEL_UNROLL=2`。每组正式计时一次，另有一次预热。校验容差为 `1e-5`。

证据：[完整运行日志](../records/evidence/conv-1483453/benchmark.log)、[调度器状态](../records/evidence/conv-1483453/job-status.txt)、[历史源文件哈希](../records/evidence/conv-1483453/source-sha256.txt)、[环境](../records/evidence/conv-1483453/environment.log)、[原始报告](../records/evidence/conv-1483453/REPORT.md)。

同一算子后来从提交包解压复测，作业 1483483 记录为四组 PASS，耗时分别为 213.59、239.25、452.73、964.26 ms，合计 **1869.83 ms**；最大误差均为 0。该结果作为 C0 的附加 run 保留，证据为 [包验证日志](../records/evidence/conv-1483483/benchmark.log) 和 [原始状态说明](../records/evidence/conv-1483483/STATUS.md)。它使用 NUMA 6。本地未留存该次独立调度器状态或 contemporaneous 源文件哈希，因此选证据链更完整的 1483453 为主参考。

**runner 有历史差异。** 当前 `conv/run.sh` 来自提交包，默认 CPU target 为 `generic`，并选择调度器分配的 NUMA；1483453 的旧 runner 默认 target 为 `native`、默认 NUMA 为 1，但当时由远程包装脚本显式设置 `generic` 和实际分配的 NUMA 5。两次使用的算子和 benchmark 相同，runner 哈希不同：

|文件身份|SHA-256|
|---|---|
|C0 算子 `conv/conv2d.c`|`78a336ca08eb6a47b02a452ae0bea5ff1c2e516c4b9a5dbd33f428bf41e2933e`|
|当前提交 runner `conv/run.sh`|`6aa53c3f5d3fc8ddf8ae6a898043f4f1c40f8778db5d71bb7be6087983349868`|
|参考作业 1483453 的旧 runner|`f4a7110d93ee32315967ab49409b69c6f53d36988ad86a00269fa5918fb67b75`|

没有 CONV 原版或 V1 的目标机器性能对照，不能给 C0 填写相对它们的加速比。官方 benchmark 的原有未使用变量及自引用未初始化变量警告仍然存在；本次未改 benchmark。

### Z0：ZGEMM

|M×N×K|平均耗时 ms|有效 GFLOPS|最大误差|
|---|---:|---:|---:|
|7427×7427×256|300.60|375.8078|3.05e-12|
|14848×14848×256|1179.34|382.8496|3.51e-12|
|37360×8192×512|3086.84|406.1089|6.04e-12|

采用三个实数点积恢复复数结果；表中 GFLOPS 仍按官方 `8MNK` 公式计算，不是实际指令执行量。正确性容差为 `1e-10`。另有 270 组布局、转置/共轭、尾部、leading dimension 和缩放边界用例通过，服务器 long-double 参考最大差 `1.847e-14`。

### T0：TRSM

|M×N|平均耗时 ms|GFLOPS|最大误差|
|---|---:|---:|---:|
|512×19968|22.21|235.6548|1.67e-16|
|2432×17024|253.85|396.6549|2.22e-16|
|17024×512|501.79|295.7150|1.11e-15|

正确性容差为 `1e-12`。另有 7 组独立已知解检查通过，包含算法切换边界、窄列尾部、padding 保护和 L 不变性；最大差 `2.776e-16`。64 MiB 是三角矩阵工作集的算法选择预算，不是机器缓存容量声明。

**TRSM 尚未在官方 KML 25.2.0 配置下复验。** 历史测试使用自行构建的 OpenBLAS 0.3.28 静态库，仅供官方 benchmark 生成输入和复制矩阵；算子本身不调用 BLAS。没有留存这份参考库二进制的哈希，不能把该环境记作官方 KML 环境。

### Z0 / T0 的共同验证环境

最终作业 1484056 为 SUCCEEDED，运行于 cn22965、q_kunpeng、单 NUMA 2，CPU 76–113，38 线程。GCC 10.3.1，`-O3 -mcpu=generic -ffp-contract=off -fopenmp`，未启用 fast-math。每组耗时为三次计时的均值，包含算子内部打包、分配和计算；benchmark 在计时外进行的数据准备不计入算子时间。

原最终验证脚本显式设置 `TEST_RUNS=3`；题目目录的 `run.sh` 默认 `TEST_RUNS=1`。复现三次均值时需要显式保持该设置，并记录实际编译器、绑定、参考库和源码哈希。

证据：[最终原包验证日志](../records/evidence/final-1484056/benchmark.log)、[调度器状态](../records/evidence/final-1484056/job-status.txt)、[源文件哈希](../records/evidence/final-1484056/source-sha256.txt)、[硬件环境](../records/evidence/final-1484056/environment.txt)、[277 组边界检查](../records/evidence/final-1484056/edge-check.log)、[服务器 sanitizer 状态](../records/evidence/final-1484056/sanitizer.log)。

服务器缺少 libasan/libubsan，最终流程明确记录 **sanitizer 未运行**。原工作区另有 Mac ARM64 上 1 线程和 4 线程各通过 277 组 UBSan 的日志；这些本机检查不替代服务器 sanitizer，也不作为鲲鹏性能结果。本次精选证据未重复打包本机日志。

## 源码对应关系与证据保存

导入时逐字节确认三个题目录的算子、benchmark、runner 和 TRSM 兼容头与现有提交包展开文件一致；每个文件的 SHA-256 存于台账的 `source_files`。ZGEMM/TRSM 还与最终服务器哈希清单完全匹配，两个 benchmark 与原工作区的官方副本一致。C0 的 runner 差异已单独记录，不能将其旧 runner 哈希套用到当前文件。

原 ZIP 的身份留档如下；仓库仅保留源码和文本证据，不保存 ZIP 或二进制：

|题目|原 ZIP 字节数|原 ZIP SHA-256|
|---|---:|---|
|CONV|8946|`f038f604eb62fb8fd2029530a39b32d34e532c3a95d797830c0f45ec14fde045`|
|ZGEMM|8342|`37809fd6e17133f443f9c22e79e84326c92b432eb22ed75cb132e8f98c7a59c3`|
|TRSM|8369|`cecda928735e1198697cc5b4858ef5c47d91da71090b01e5691b7aa1baa296bb`|

[证据清单](../records/evidence/manifest.json) 保存每份复制文件的来源、字节数及 SHA-256。原始日志和报告按原样复制，其内部链接、远程路径及时间保持原有内容；当前可读结论以本页和结构化台账为准。

原工作区还有同一最终 ZIP 的较早测试，ZGEMM 合计 4520.41 ms、TRSM 合计 775.35 ms，但整轮随后因 sanitizer 链接失败终止。本台账选最终完整成功的 1484056，不择取较早更快数字作为新版本。原第一版 ZGEMM/TRSM 的 10030.43/3620.97 ms 来自不同重复次数和部分不同 NUMA，且第一版本身已经优化，不能当作官方原版的严格对照。

后续晋级应同时保留：父版本、不同的优化思路、完整源码哈希、实际运行环境、逐 case 指标与正确性、原始日志、与父版本的可比性。排名和正式分数在实际取得前保持空值。
