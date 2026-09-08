# TRSM：T1-panel 待测候选

2026-09-08 建立。父版本为历史 T0；候选尚未在鲲鹏或官方 KML 环境测试，未晋级。当前最佳 `trsm/trsm.c` 保持 T0。源码位于 `.runs/trsm/T1-panel/source/trsm.c`，快照不纳入 Git；共享时必须同时提供源码和哈希。

## 单一假设与实现

大工作集分块前代的更新阶段原来从 B 读取已解右端项，k 相邻两次读取间隔为 ldb 个 double。候选将每个已解 `KB×RHS = 256×8` 块打包成连续的窄面板，一次打包后供下方所有行块复用，期望降低大步长访问引起的缓存及地址转换开销。此判断是待验证假设，不是性能结论。

- 在 `solve_blocked` 进入并行区前分配共享打包区：`ceil(n/8)×256×8` 个 double；n=512 时约 1 MiB。
- 每轮对角块求解结束后，按 8 列面板分工打包，尾列补零；原有求解 barrier 保证输入就绪，新增打包 barrier 保证所有消费者可见。
- `update4x8` 分开传入 X 步长与 C 步长，X 步长从 ldb 变成 8；写回 B 的步长仍为 ldb。标量尾部使用相同打包输入。
- 分配失败时继续原来的大步长读取，分块循环和每个点积的累计次序不变。最终释放共享打包区。
- 小工作集 `solve_panel`、64 MiB 算法选择预算、256/64 分块和 4×8 内核形状均不改变；官方 benchmark、run.sh 和兼容头未修改。

可能的代价是每个对角块新增一次并行 barrier、复制量和共享区访问，实际收益必须由鲲鹏测量决定。此轮不同时调整块尺寸、调度策略或精度。

## 源码身份与创建命令

```text
T0 trsm.c SHA-256:
6b66766196b2a6024fcbbeac45ff1b4189e19cc8ac8d75d001e4ddfea004abac
T1-panel trsm.c SHA-256:
fea6ee11cce8785ac3f13f641f9feb195a829493a27a9c902000a743545dbf73
```

```bash
python3 tools/experiment.py new trsm T1-panel --parent T0 --strategy '大工作集分块前代每个已解256x8右端项块共享打包为连续面板，使所有下方4x8更新复用连续读取，降低跨行大步长访存；其余算法与分块不变'
```

创建时继承历史 T0，仅用于准备候选；此操作不代表已经建立当前机器基线或可以晋级。

## 本机正确性检查

环境：Apple ARM64，Apple clang 17.0.0，目标 arm64-apple-darwin25.5.0，Homebrew libomp；未计时，不输出鲲鹏性能判断。最初 ASan+UBSan 编译成功，但两个程序均无测试输出，经主 Agent 通知当前 macOS 的 ASan 初始化问题后中断，退出码均为 130。因此 ASan **未完成**；空日志分别保留为 `local/check-final-asan-incomplete.log` 和 `local/check-packed-asan-incomplete.log`，没有把它们记为通过。随后改用纯 UBSan，编译参数见下文。

复用上层现成 `check-final.c`，同时链接未改的 Z0 以满足该测试程序接口；另在候选 local 目录生成独立已知解扩展测试 `check-packed.c`，只复用现成 TRSM 测试逻辑，新增 `4095×9、4096×8、4097×9、4355×65`。覆盖阈值两侧、完整 NEON 更新、8 列尾部、64 列块边界、最后一个不足 256 行的对角块、padding 与 L 不变性。两者不修改官方测试或原 `check-final.c`。

```bash
mkdir -p .runs/trsm/T1-panel/local
clang -O2 -fno-fast-math -ffp-contract=off -fsanitize=address,undefined -fno-sanitize-recover=all -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include -L/opt/homebrew/opt/libomp/lib -lomp /Users/lingsu011900/Downloads/conv/other-problems/tests/check-final.c zgemm/zgemm.c .runs/trsm/T1-panel/source/trsm.c -o .runs/trsm/T1-panel/local/check-final
clang -O2 -fno-fast-math -ffp-contract=off -fsanitize=address,undefined -fno-sanitize-recover=all -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include -L/opt/homebrew/opt/libomp/lib -lomp .runs/trsm/T1-panel/local/check-packed.c .runs/trsm/T1-panel/source/trsm.c -o .runs/trsm/T1-panel/local/check-packed
OMP_NUM_THREADS=1 .runs/trsm/T1-panel/local/check-final > .runs/trsm/T1-panel/local/check-final-t1.log 2>&1
OMP_NUM_THREADS=4 .runs/trsm/T1-panel/local/check-packed > .runs/trsm/T1-panel/local/check-packed-t4.log 2>&1
```

最初上面的 ASan+UBSan 两次运行被中断；以下是实际完成的 UBSan 编译与运行命令：

```bash
clang -O2 -fno-fast-math -ffp-contract=off -fsanitize=undefined -fno-sanitize-recover=undefined -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include -L/opt/homebrew/opt/libomp/lib -lomp /Users/lingsu011900/Downloads/conv/other-problems/tests/check-final.c zgemm/zgemm.c .runs/trsm/T1-panel/source/trsm.c -o .runs/trsm/T1-panel/local/check-final-ubsan
clang -O2 -fno-fast-math -ffp-contract=off -fsanitize=undefined -fno-sanitize-recover=undefined -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include -L/opt/homebrew/opt/libomp/lib -lomp .runs/trsm/T1-panel/local/check-packed.c .runs/trsm/T1-panel/source/trsm.c -o .runs/trsm/T1-panel/local/check-packed-ubsan
OMP_NUM_THREADS=1 .runs/trsm/T1-panel/local/check-final-ubsan > .runs/trsm/T1-panel/local/check-final-t1.log 2>&1
OMP_NUM_THREADS=4 .runs/trsm/T1-panel/local/check-final-ubsan > .runs/trsm/T1-panel/local/check-final-t4.log 2>&1
OMP_NUM_THREADS=1 .runs/trsm/T1-panel/local/check-packed-ubsan > .runs/trsm/T1-panel/local/check-packed-t1.log 2>&1
OMP_NUM_THREADS=4 .runs/trsm/T1-panel/local/check-packed-ubsan > .runs/trsm/T1-panel/local/check-packed-t4.log 2>&1
```

|检查|线程|结果|TRSM 最大绝对误差|
|---|---:|---|---:|
|原始已知解、padding、L 不变性 7 组|1|PASS，退出 0；UBSan 无诊断|2.776e-16|
|原始已知解、padding、L 不变性 7 组|4|PASS，退出 0；UBSan 无诊断|2.776e-16|
|新增打包路径 4 组|1|PASS，退出 0；UBSan 无诊断|3.886e-16|
|新增打包路径 4 组|4|PASS，退出 0；UBSan 无诊断|3.886e-16|
|强制分配失败回退，新增 4 组|4|PASS，退出 0；UBSan 无诊断|3.886e-16|

原始检查程序附带的 270 组 ZGEMM 在 1/4 线程也均 PASS，最大误差 1.814e-14；这是链接未改 Z0 的附带检查，不代表该候选优化了 ZGEMM。正常候选共有 22 次 TRSM 用例检查，另有 4 次强制分配失败回退检查。所有检查保留 B padding，且 L 逐字节不变；精度判定仍为 1e-12。未运行服务器 sanitizer。

回退检查仅在本机编译单独测试目标，将候选的 `posix_memalign` 调用重定向到 `local/fail-alloc.c` 的测试符号；它忽略参数并返回 ENOMEM，不改候选源码。构建和运行命令：

```bash
clang -O2 -fno-fast-math -ffp-contract=off -fsanitize=undefined -fno-sanitize-recover=undefined -Xpreprocessor -fopenmp -I/opt/homebrew/opt/libomp/include -Dposix_memalign=trsm_test_alloc_fail -c .runs/trsm/T1-panel/source/trsm.c -o .runs/trsm/T1-panel/local/trsm-fail-alloc.o
clang -O2 -fno-fast-math -ffp-contract=off -fsanitize=undefined -fno-sanitize-recover=undefined -L/opt/homebrew/opt/libomp/lib -lomp .runs/trsm/T1-panel/local/check-packed.c .runs/trsm/T1-panel/local/fail-alloc.c .runs/trsm/T1-panel/local/trsm-fail-alloc.o -o .runs/trsm/T1-panel/local/check-packed-fail-alloc
OMP_NUM_THREADS=4 .runs/trsm/T1-panel/local/check-packed-fail-alloc > .runs/trsm/T1-panel/local/check-packed-fail-alloc-t4.log 2>&1
```

结论：候选通过本机 UBSan 和上述数值/边界检查，具备继续鲲鹏评测的条件；没有测量或推断加速比。

## 鲲鹏待执行与晋级条件

主 Agent 统一复用用户认证和调度作业，本专职 Agent 未提交任何远程作业、比赛平台内容或 Git 推送。

1. 在当前机器用不变源码建立 T0，并按仓库流程完成三轮完整官方用例；必须确认实际加载 KML 25.2.0。历史 T0 的 OpenBLAS 0.3.28、777.85 ms 不能替代这一步。
2. 候选与基线保持相同编译器、参数、CPU/NUMA、线程、参考库、TEST_RUNS 和 wrapper 重复次数。上传前保存候选实际哈希。
3. 运行 `cluster.py submit .runs/trsm/T1-panel`，保存唯一 job ID；通过 status 查询已有作业，完成后 fetch；不得因等待重复提交。
4. 核对调度成功、wrapper 退出 0、每轮全部三组 PASS，容差仍为 1e-12。保留原始环境、源码哈希与逐组日志。
5. 使用 `experiment.py record trsm T1-panel --log .runs/trsm/T1-panel/benchmark.log --environment <实际环境ID> --reference <实际KML版本及库身份> --repeats 3` 登记，再与同环境 T0 compare。按三轮逐组中位数、波动及退化决定是否晋级；结果不明确就保留 T0。

重点观察 `17024×512`，这是官方三组中使用修改路径的一组；前两组走未改的小工作集实现。不能把这一分流理解为按公开尺寸硬编码：选择仍完全基于三角矩阵工作集预算。
