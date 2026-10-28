# XCPC-Claw 评测报告（XCPC-Hack-Bench）

- 生成时间：2026-10-27 20:09:41
- 运行模式：确定性离线引擎（真实 g++ 编译 + 造数据 + 对拍，不调用大模型）
- 反例最小化：关闭（--minimize 可开启）
- 环境：Python 3.13.7 / 平台 win32 / 对拍上限 300 组 / 单组超时 1.0s

| Case | 期望捕获的根因 | Hack Success | Verdict | Iterations | Latency (s) | 反例 tokens | 反例行数 | Main Tokens | Profile Updated | 备注 |
| :--- | :--- | :---: | :---: | ---: | ---: | :---: | ---: | ---: | :---: | :--- |
| case_01_multitest_reset | 多测未清空：邻接表 head/ecnt 组间残留污染下一棵树 | ✅ | WA | 4 | 4.75 | 22 | 12 | 0 | Yes | ans='-8 -7 -3 -10 -9 -2 -2 -10' out='-8 -7 -3 -10 -9 -2 -2 -12' |
| case_02_int_overflow | int 累加溢出：极值数据下符号翻转 | ✅ | WA | 1 | 4.32 | 5 | 2 | 0 | Yes | ans='4000000000' out='-294967296' |
| case_03_greedy_counterexample | 性价比贪心假做法：小规模权值组合下失效 | ✅ | WA | 3 | 4.65 | 8 | 4 | 0 | Yes | ans='18' out='11' |

## 汇总

- **反例捕获成功率 (HSR)**：3/3 = 100%
- **平均单题耗时**：4.57s（含 g++ 编译，整体 13.94s）
- **平均反例规模**：11.7 tokens（生成器一开始就只造极小数据，便于人类手算）
- **画像闭环**：3/3 个 case 成功写入 Bug 指纹
- **Main Context Tokens**：离线引擎模式不经过主 Agent，故为 0；Subagent 隔离收益请在 `uv run main.py` 的 `/hack` 流程中观察（或使用 `--agent`）。

## 复现方式

```bash
uv run evals/run_eval.py                       # 离线引擎模式
uv run evals/run_eval.py --minimize             # 额外执行反例最小化
uv run evals/run_eval.py --agent                # 额外跑真实 Agent（需 API Key）
```

## 说明

- 每个 case 的 `wa.cpp` 均为真实可编译的错解，`ac.cpp` 作为对拍标程，`gen.py` 按 seed 分阶段造数据；
- `Hack Success` 要求反例经**二次重跑**仍能稳定复现差异，避免把偶发/假阳性算作成功；
- 画像更新写入隔离目录 `evals/.eval_memory/PROFILE.json`，不会污染真实选手画像；
- `--minimize` 会调用与 Agent 相同的 `delta_reduce.py`（ddmin + 数值收缩 + 合法性 validator）。注意：通用文本 ddmin 无法重排多测结构（改 `T` 必须同时增删整组数据），因此多测类反例通常只能收缩数值；真实 Agent 会按题面写更强的结构化 reducer。
