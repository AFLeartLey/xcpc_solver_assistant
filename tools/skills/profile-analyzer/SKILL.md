---
name: profile-analyzer
description: 分析选手错因并更新长期记忆 MEMORY.md 与结构化画像 PROFILE.json。
---

# 选手画像提取与长期记忆更新规范

画像的价值在于**下一次**：如果这次记录的标签下一次用不上，这次记录就是噪音。

## 1. 什么时候更新

- 一次对拍闭环结束（拿到最小反例 + 完成点拨）后**必须**更新；
- 选手自己报告"改完 AC 了"时更新一次（记录修复方式）；
- 一轮训练结束（多题）时做一次汇总复盘；
- 不要每轮工具调用都写，噪音会污染 MEMORY.md。

## 2. 怎么打标签

调用 `update_profile(problem_id, bug_id, bug_summary, topic, mastery_delta)`：

| 参数 | 规则 |
| :--- | :--- |
| `problem_id` | 用题号/文件名 slug（`cf1920c`、`case_01_multitest_reset`） |
| `bug_id` | 优先复用既有指纹（见下表）；确实无法归类时用 `BUG_<大写短语>` 新建 |
| `bug_summary` | **必须含行号与变量名**，如 `wa.cpp:23 只清 diff[1..n]，diff[n+1] 残留污染下一组` |
| `topic` | 用小写专题键：`greedy` / `dp` / `graph` / `data_structures` / `math` / `string` / `geometry` |
| `mastery_delta` | 同类 Bug 复发用 `-3 ~ -5`；首次踩坑用 `-2`；已修复并重拍通过用 `0` 或 `+2` |

### 既有 Bug 指纹表

| bug_id | 含义 |
| :--- | :--- |
| `BUG_INT_OVERFLOW` | 32 位整数溢出（漏 `long long` / `1LL`） |
| `BUG_MULTITEST_DIRTY` | 多测未彻底清空全局状态 |
| `BUG_BOUNDARY_N1` | 极小边界（`N=1`、`K=0`、空集） |
| `BUG_GREEDY_FALSE` | 局部最优贪心假做法 |
| `BUG_ARRAY_OVERFLOW` | 数组开小 / 下标越界 |
| `BUG_DFS_STACK` | 深递归爆栈 |
| `BUG_DISCONNECTED` | 非连通图只从一个点遍历 |
| `BUG_MOD_NEGATIVE` | 取模出现负数 |
| `BUG_FLOAT_EPS` | 浮点精度 / 比较未设 eps |
| `BUG_LAZY_PUSHDOWN` | 线段树懒标记下传错误 |

## 3. 怎么写 MEMORY.md

`update_profile` 会自动追加一行训练记录，但你仍应在对话里补一条**可复用结论**，例如：

```
- [2026-10-27] cf1920c：多测差分数组只清到 n 导致 T=2 污染；已提醒"清空范围必须覆盖所有可能被写的下标"。
  下次遇到差分/前缀和题，第一件事检查清空上界。
```

写作要求：

1. 一条记录只讲一个结论，不要写流水账；
2. 含题号 + 根因标签 + 下次可执行的检查动作；
3. 不要抄代码；不要写"选手很努力"这类无信息量内容。

## 4. 怎么用画像反哺教学

- **薄弱专题**（score < 65）：讲解降步长，先给结论再给推导，必要时 ASCII 图解；
- **较强专题**（score >= 75）：跳过基础概念，直接讨论实现细节与常数优化；
- **高频 Bug Top3**：造数据与静态审计时优先攻击；点拨时显式引用历史（"这是第 3 次同类问题"）；
- **默认点拨层级**：`L1/L2` 保持苏格拉底式；连续 3 次同题失败可临时升到 `L3/L4`。

## 5. 复盘输出模板（多题汇总时）

```
本周训练复盘（5 题 / 3 次 WA / 2 次 AC）
- 高频根因: BUG_MULTITEST_DIRTY x2（均为清空范围不足）、BUG_INT_OVERFLOW x1
- 掌握度变化: data_structures 60 -> 57（线段树懒标记仍不稳）
- 下阶段重点: 多测题先写"清空清单"；累加类变量一律 long long
- 建议练习: 2 道多测 + 差分边界题（难度 1400~1600）
```

## 6. 禁止事项

1. 不要为了让画像"好看"而虚增计数或分数；
2. 不要把未验证的猜测写成根因（例如"可能是溢出"却记为 `BUG_INT_OVERFLOW`）；
3. 不要覆盖 `solved_or_hacked_history`，只能追加；
4. 不要写入与训练无关的内容（闲聊、情绪评价）。
