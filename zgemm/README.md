# ZGEMM 优化提交包

在已申请的鲲鹏计算资源内运行 `bash run.sh`，自动编译并执行三组官方测试。需要 Linux、GCC/OpenMP、numactl；默认 38 线程、单 NUMA，自动使用当前调度分配中的第一个 NUMA 节点。

可设置 `OMP_NUM_THREADS`（1–38）、`NUMA_NODE`、`CC`、`TEST_RUNS`。不要在登录节点运行大尺寸测试。

## 实现说明

`cblas_zgemm` 计算 `C = alpha * op(A) * op(B) + beta * C`。

- 支持行/列主序，普通、转置、共轭转置，及有填充的 leading dimension。
- 打包 A/B 的实部、虚部与两者之和；3×4 NEON 微内核用三个实点积恢复复数结果。
- 24 行缓存块；累加器保留在寄存器中，OpenMP 静态分配输出矩形。
- 处理任意行列尾部、alpha=0、beta=0、K=0；beta=0 时不读取旧 C。
- 打包内存不足时回退到通用三重循环。非 AArch64 平台有普通 C 路径。
- 三乘法与显式 FMA 会改变浮点舍入，以官方绝对误差 `1e-10` 检查，不要求逐位相同。

## 验证与文件

`bench_zgemm.c` 与官方 ZIP 中的测试程序逐字节一致。编译禁用 fast-math 和隐式 FMA 合并；微内核使用的显式 FMA 保留。

`run.sh` 运行以下尺寸，并检查真实的 PASS/FAIL 文本；即使测试程序退出码为 0，出现 FAIL 或没有 PASS 也会中止：

- 7427 × 7427 × 256
- 14848 × 14848 × 256
- 37360 × 8192 × 512

每次运行的原始日志保存在独立 `results/run-*` 目录。实测成绩、环境及 ZIP 校验和见随交付提供的结果报告。此包未自动上传 OBS。
