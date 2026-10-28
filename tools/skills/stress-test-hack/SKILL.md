---
name: stress-test-hack
description: >-
  针对 XCPC 题目和代码自动设计暴力标程、数据生成器与 SPJ，在沙箱中对拍寻找极小反例。
---

# Stress-Test & Hack 标准作业程序（SOP）

目标：在**不污染主对话**的沙箱目录里，用真机编译执行的方式，把选手代码的隐蔽 Bug 逼出来，
并交付一个人类一眼能看懂的**最小反例**。

## 0. 工作区隔离（硬规则）

- 所有产物必须放在 `workspace/<problem_slug>/`（Subagent 的工作目录就是这个目录，直接用相对路径即可）：
  `brute.cpp`、`gen.py`、`spj.cpp`、`hack.in`、`hack.ans`、`hack.out`。
- 严禁修改选手提交的源码文件；需要插桩时把代码**复制**成 `target_debug.cpp` 再改。

## 1. 先读题，锁定四件事

1. **输入格式**（是否多测 `T`、多测是否共享全局状态）；
2. **数据范围**（`N` 上限、值域上下界、是否允许负数/重边/自环/不连通）；
3. **输出格式**（是否多解 -> 需要 SPJ；浮点题 -> 误差容忍）；
4. **暴力可行性**（`N<=8` 时能否 `O(2^N)` / `O(N!)` / `O(N^3)` 秒过）。

## 2. 写 `brute.cpp` 的铁律

1. **绝对正确优先**：宁可 `O(2^N)` 子集枚举、`std::next_permutation`、朴素 DFS 回溯、
   `O(N^3)` 区间模拟，也不要复用选手代码里的贪心、剪枝或"看似显然"的性质。
2. **独立实现**：不要包含、不要复制选手代码的任何函数。
3. **必须自测**：写完后先用题面样例喂给 `brute.cpp`，输出与样例一致才进入对拍；
   不一致先修暴力，不要拿一个错标程去对拍。
4. 多测题在暴力里用 `while (T--)` 且每轮重新读入，所有状态都是局部变量，天然不受残留污染。

## 3. 写 `gen.py` 的铁律

1. 约定：`python gen.py <seed>`，把测试数据**打印到 stdout**，不要写文件、不要读 stdin。
2. 用 `random.seed(int(sys.argv[1]))` 保证可复现；同一 seed 必须产生相同数据。
3. **默认极小规模**：`N in [1, 6]`、值域 `[-10, 10]`，让反例短到能口算；
   只有确认小数据拍不出来时，才放大到 `N<=12`。
4. **分阶段策略**（用 seed 取模切换，而不是每次随机都靠运气）：

```python
import random, sys
seed = int(sys.argv[1]); random.seed(seed)
stage = seed % 5
if stage == 0:      n = random.randint(1, 2)          # 极小边界
elif stage == 1:    n = random.randint(2, 4)          # 手算规模
elif stage == 2:    n = 6; values = [10, -10, 0]      # 极值/零
elif stage == 3:    n = random.randint(1, 6)          # 常规随机
else:               n = 5; values = list(range(n))    # 单调/重复
```

5. **按画像偏置**（看到 `<contestant_profile>` 的高频 Bug 指纹就针对性造数据）：
   - `BUG_INT_OVERFLOW` -> 把值域拉到 `±1e9`、`n` 取能让 `sum` 越 `int` 的规模；
   - `BUG_MULTITEST_DIRTY` -> `T>=2`，且**先大后小 / 先小后大**各来一遍，制造上一轮残留；
   - `BUG_BOUNDARY_N1` -> 专门生成 `N=1`、空区间、`K=0`；
   - `BUG_GREEDY_FALSE` -> `N=3..5`，权值/容量精心组合，让"性价比贪心"翻车；
   - 树/图题加载 `graph-tree-gen` 技能，生成菊花图、退化链、森林、重边。

## 4. 需要 SPJ 吗？

题面出现「输出任意一种合法方案」「误差不超过 `1e-6`」「多解」时必须写 `spj.cpp`：

- 调用约定：`spj <in.txt> <out.txt> <ans.txt>`，返回 `0` 表示选手输出合法，非 0 表示 WA；
- SPJ 只校验**合法性/最优性**，不要假设输出格式唯一；
- 用 `run_stress_test(..., spj_cpp="spj.cpp")` 传入即可自动启用。

## 5. 执行对拍

```
run_stress_test(
  workspace_dir = "workspace/<slug>",
  target_cpp   = "<选手代码>.cpp",
  brute_cpp    = "brute.cpp",
  gen_py       = "gen.py",
  spj_cpp      = "",            # 需要时填 "spj.cpp"
  iterations   = 200,
  time_limit   = 2.0,
)
```

返回值判读：

| status | 含义 | 下一步 |
| :--- | :--- | :--- |
| `HACK_SUCCESS` | 捕获到 `WA/TLE/RE`，已固化 `hack.in/ans/out` | 做第 6 步最小化 |
| `ALL_PASSED` | 200 组全部一致 | 换生成策略（边界/极值/特殊图）加到 300~500 组 |
| `BRUTE_FAILED` | 暴力超时或崩溃 | 缩小 `gen.py` 规模或修暴力 |
| `GEN_FAILED` | 生成器报错/无输出 | 修 `gen.py`，确认打印到 stdout |
| `COMPILE_ERROR` | 编译失败 | 按返回的 stderr 修代码 |

## 6. 反例最小化（必做）

随机拍出的反例可能 `N=15`，对选手没有教学价值。按下面顺序缩：

1. **重复验证**：把 `hack.in` 再喂给 target/brute 各跑一次，确认差异稳定复现（不是偶发）；
2. **缩减多测**：`T>1` 时先试 `T=1`（只留第二组/第一组），确认是否只有多测组合才挂；
3. **缩减规模**：手写更小的 `N=1/2/3/4` 输入，逐个喂进去，找能触发的最小 `N`；
4. **自动缩减**：调用本技能自带脚本

```bash
python <skills_dir>/stress-test-hack/scripts/delta_reduce.py \
  --input workspace/<slug>/hack.in \
  --output workspace/<slug>/hack.min.in \
  --checker "python workspace/<slug>/check.py" \
  --validator "python workspace/<slug>/validate.py"
```

其中：

- `check.py <candidate>`：读候选输入 -> 跑 brute 与 target -> **两边都正常退出且输出仍有差异**才退出码 0；
- `validate.py <candidate>`：读候选输入并校验题面约束（`T`/`n` 范围、恰好 `n-1` 条边、值域等），
  合法才退出码 0。**没有 validator 会缩出"删掉关键行导致读入失败"的非法假反例**，务必写一个
  （十几行 union-find / token 计数即可）。
5. 最终把最小反例另存为 `hack.in`（覆盖原文件），并重新确认 `hack.ans/hack.out` 与之对应。

## 7. 回传战报（只回传结论，不回传日志）

必须输出如下 JSON（不要夹带编译日志）：

```json
{
  "status": "COUNTEREXAMPLE_FOUND",
  "failure_type": "WA",
  "iterations_run": 37,
  "minimal_input": "2\n3\n1 2 3\n...",
  "expected_output": "...",
  "actual_output": "...",
  "divergence_observation": "当 T=2 且第二组 N 大于第一组时，第一组的 diff[n+1] 残留污染了第二组",
  "files": {"in": "...hack.in", "ans": "...hack.ans", "out": "...hack.out"},
  "brute_source_path": "...brute.cpp",
  "gen_source_path": "...gen.py",
  "note": "主 Agent 请结合 BUG_MULTITEST_DIRTY 指纹做 L2 点拨，并重点看 diff 清空范围"
}
```

## 8. 常见翻车点

- 生成器忘了 `seed` 参数 -> 每次数据一样，永远拍不出；
- 暴力用了 `int` 存答案 -> 自己也溢出，反例无效；
- 用了 `diff` 比对多解题 -> 把合法解当 WA，浪费时间；
- 只测随机不测边界 -> 溢出/多测类 Bug 永远抓不到；
- 反例没最小化就回传 -> 选手看不懂，等于没帮上忙。
