# TRSM 优化提交包

在已申请的鲲鹏计算资源内运行 `bash run.sh`，自动编译并执行三组官方测试。默认使用题目指定的 KML/kblas 参考库；需要 Linux、GCC/OpenMP、numactl。

默认 38 线程、单 NUMA，自动使用当前调度分配中的第一个 NUMA 节点。可设置 `OMP_NUM_THREADS`（1–38）、`NUMA_NODE`、`CC`、`TEST_RUNS`。不要在登录节点运行大尺寸测试。

## 实现说明

`l_trsm` 求解行主序、非单位对角的下三角方程 `L X = B`；B 原地变成 X，L 保持不变。

- 三角矩阵工作集较小时，使用 4 行 × 8 列的窄面板前代，打包右端项并复用已解数据。
- 工作集估计超过 64 MiB 时，使用 256 行对角块前代和 64×64 输出块更新；更新内核为 4×8 NEON FMA。
- 64 MiB 是通用算法选择阈值，不是对硬件缓存大小的承诺；没有对公开测试尺寸作等值分支。
- 支持任意尾部与 lda/ldb 填充。内存不足时窄面板路径回退通用前代。
- 不同线程只写各自的列或输出矩形。分块路径通过屏障保持前代依赖。
- FMA 和分块会改变舍入，以官方绝对误差 `1e-12` 检查。
- 优化算子本身不调用 BLAS 库；参考库仅用于官方 benchmark 生成输入和复制矩阵。

## 参考库

默认 `-lkblas`，优先加载官方 HPCkit modules。

若测试环境未安装 KML，可显式指定兼容的参考 BLAS，例如：

```bash
KBLAS_LIB=/absolute/path/to/libopenblas.a bash run.sh
```

此时 `compat/kblas.h` 只提供 benchmark 所需的标准 CBLAS 声明，脚本明确打印参考库差异。交付时的鲲鹏验证使用 OpenBLAS 0.3.28 静态参考库，不能视作已在官方 KML 25.2.0 环境复验。

## 验证与文件

`bench_trsm.c` 与官方 ZIP 中的测试程序逐字节一致。`run.sh` 执行：

- 512 × 19968
- 2432 × 17024
- 17024 × 512

脚本检查真实 PASS/FAIL 文本并保存独立 `results/run-*` 日志。实测成绩及 ZIP 校验和见随交付提供的结果报告。此包未自动上传 OBS。
