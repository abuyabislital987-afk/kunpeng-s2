# 鲲鹏 S2：三题协作与实验记录

当前仓库：[abuyabislital987-afk/kunpeng-s2](https://github.com/abuyabislital987-afk/kunpeng-s2)，上游：[Palm0palM/kunpeng-s2](https://github.com/Palm0palM/kunpeng-s2)。目标是让 Agent 完成“提出方案 → 修改候选 → 超算测试 → 留存结果”，两人从共同基线尝试不同方法；比赛平台的正式提交由本人负责。

`conv/`、`zgemm/`、`trsm/` 保存当前晋级版本。现有版本的验证范围与环境见 [基线说明](docs/BASELINES.md)，不要把历史测试直接当作当前排行榜成绩。

## 先把环境跑通

1. 安装 Python 3，并确保本机可以使用 Git、SSH。本机配置位于 `config/cluster.local.json`；服务器主机密钥已按用户确认更新到 `config/known_hosts.local`。不要把密码、私钥、令牌写进仓库。
2. 本轮已恢复 SSH 认证并建立约一小时的复用会话，Agent 可以直接检查连接、提交与取回作业，无需用户操作终端：
   ```bash
   python3 tools/cluster.py --config config/cluster.local.json doctor
   ```
   目标服务器为 `10.44.9.4`，本次登录节点为 login08。会话失效时先检查 VPN，再按用户授权的认证方式恢复，不把密码写入仓库。`tools/connect.py` 是可选的交互登录辅助工具。
3. 给 Agent 使用 [启动提示词](prompts/optimize.md)。最新分工为本任务负责 CONV，用户另开任务负责 [ZGEMM](prompts/zgemm.md) 和 [TRSM](prompts/trsm.md)；每题使用独立实验目录与作业 ID，禁止取消他人作业。

本地工作分支为 `setup/agent-workflow`；`origin` 保留上游仓库，`personal` 指向当前账号的仓库。已验证成果发布到个人仓库的 `main` 分支。

CONV 本轮保留 C0-r2，四组用例各三轮耗时中位数合计 1867.77 ms，三个候选均未晋级。完整结果见 [CONV 实测记录](docs/CONV_RESULTS.md)，提交包对应的源码校验见 [包清单](outputs/conv-best.json)。

## 先重测当前基线

历史 C0/Z0/T0 仅作起点，必须先在当下机器完成三轮测量。第一次创建 C0 **不加 `--parent`**，保持源码不变：

以下是新环境示例，已有目录应直接恢复。当前 CONV 首次运行 C0 因运行器解析空 CPU 列表失败，已修复并保留失败记录，重试编号为 C0-r1；候选 C1-block 基于这次重试比较。

```bash
python3 tools/experiment.py new conv C0 --strategy "当前代码基线复测"
python3 tools/cluster.py --config config/cluster.local.json submit .runs/conv/C0
python3 tools/cluster.py --config config/cluster.local.json status .runs/conv/C0
python3 tools/cluster.py --config config/cluster.local.json fetch .runs/conv/C0
```

`status` 每次查询一次，可中断后用已有作业 ID 恢复查询；成功完成后再 `fetch`。不要因为等待或断线重复提交。wrapper 独立执行三轮完整用例，`--repeats 3` 指这三轮，**不等于 benchmark 内部的 `TEST_RUNS`**。

登记固定的下载日志并建立当下机器基线。下面的环境 ID 仅适用于实际记录确为该节点和编译配置的情况，否则如实替换：

```bash
python3 tools/experiment.py record conv C0 --log .runs/conv/C0/benchmark.log --environment cn22965-gcc10-generic-38 --reference "官方内置参考" --repeats 3
python3 tools/experiment.py promote conv C0
```

ZGEMM 的 Z0、TRSM 的 T0 同理；参考库必须填写实际版本，尤其不能把 OpenBLAS 写成官方 KML。

## 再开始优化候选

```bash
git switch -c conv/c1-alice
python3 tools/experiment.py new conv C1-alice --parent C0 --strategy "调整分块尺寸，减少工作集占用；其余条件保持与 C0 一致"
```

只改 `.runs/conv/C1-alice/source/`，然后按基线相同流程对该目录执行 `submit`、`status`、`fetch`，完成三轮后登记与比较：

本地正确性检查完成后，可执行 `python3 tools/experiment.py checkpoint conv C1-alice --note "校验结果与待测事项"` 保存待测候选；创建时记录 planned，准备完成记录 prepared，失败也保留。

```bash
python3 tools/experiment.py record conv C1-alice --log .runs/conv/C1-alice/benchmark.log --environment cn22965-gcc10-generic-38 --reference "官方内置参考" --repeats 3
python3 tools/experiment.py compare conv C0 C1-alice
python3 tools/experiment.py report
```

所有用例正确且同环境复测确认提速后，再执行 `python3 tools/experiment.py promote conv C1-alice`。晋级保留候选来源；下一轮两人共同从该版本出发。正式编号与协作约定见流程文档。

## 文件放在哪里

| 位置 | 内容 |
| --- | --- |
| 三个题目目录 | 当前已晋级的题解与运行文件 |
| `.runs/题目/版本/source/` | 本地候选源码；实验完成后保留快照，不纳入 Git |
| `.runs/题目/版本/` | 作业信息、下载日志与临时产物；不纳入 Git |
| `records/history.json` | 版本关系与实验索引 |
| `records/experiments/` | 每个版本的策略、环境、测量与判定 |
| `AGENTS.md` | Agent 必须遵守的优化与记录规则 |

GFLOPS、耗时和正确性来自测量；官方评分与排名算法待确认。总毫秒数仅作内部比较，不标成官方分数。
