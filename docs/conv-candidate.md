# CONV 候选 C1-block

父版本 C0；当前最佳 `conv/` 未改。候选在 `.runs/conv/C1-block/source/conv2d.c`。

假设：32 列 NEON 块在处理相邻两列 kernel 时，输入加载大量重叠。保留 8 个累加向量，用 7 次 `vext` 复用前一列的输入，将每两列 kernel 的向量加载从 16 次减为 9 次。最后一组从偏移 29 直接加载，避免从偏移 32 加载导致越界。新指令与加载端口之间的收益必须由鲲鹏实测决定。

保持每个输出的原始累加次序，使用分离的 `vmulq_f32` 和 `vaddq_f32`，保留 `-ffp-contract=off`。宽度不足 32 的尾块使用原实现；非 NEON 或自定义其他主块宽度也走原实现。官方 benchmark 和 run.sh 未改。

## 已做检查

- Apple ARM64、Clang、OpenMP、UBSan（遇错停止）。
- 1 与 4 线程各 418 组，与独立标量 float 参考逐位一致。覆盖输出宽度 1–97，31/32/33、63/64/65 等边界，kernel 宽度包括 39/41/55/81，奇偶 kernel 和地址偏移。
- 为输入与输出添加不可读写保护页后，1 与 4 线程各 418 组通过；完整向量与尾块未触碰保护页。
- ASan 在本机运行库初始化、进入 main 之前自旋，已停止；不记为 ASan 通过。UBSan 与保护页检查已完成。
- 本地测试与日志：`.runs/conv/C1-block/local/`；`guard-1thread.log`、`guard-4thread.log`、`ubsan-1thread.log`、`ubsan-4thread.log`。

## 待做

超算登录后先运行 C0 三轮，再在同节点、NUMA、编译器、线程配置下运行 C1-block 三轮。以所有官方用例 PASS、耗时中位数和波动判断；检查目标 GCC 汇编是否确实减少加载、是否引入寄存器溢出。没有鲲鹏成绩前不晋级，不声称提速。
