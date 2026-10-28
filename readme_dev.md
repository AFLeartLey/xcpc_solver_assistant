# XCPC-Claw（开发者与架构文档 · Developer README）

> **说明**：本文档面向开发者与面试评审，详细介绍 **XCPC-Claw** 的系统架构、设计决策、安全策略与评测细节。面向算法竞赛选手的日常使用指南请见 [README.md](README.md)。
>
> **项目定位**：AI-Native 竞争性编程（ICPC / CCPC / Codeforces）自动对拍 Hack + 个性化金牌教练 Agent。
> **核心范式**：`Node` + `Flow` + `Outer Goal Loop` + `Progressive Skill Loader` + `Subagent 上下文隔离` + `双层记忆 / 选手画像`。
> **零 LangChain / LlamaIndex / Dify 依赖**：全栈原生 Python，`core/` 运行时约 1.1k 行（含中文注释），其中 `Node`/`Flow` 状态机不到 60 行有效代码。

---

## 1. 它解决什么问题

| 痛点 | 传统做法 | XCPC-Claw |
| :--- | :--- | :--- |
| WA 但造不出反例 | 手写 `std.cpp` + `gen.py` + `checker.sh`，半小时起步 | `/hack` 一条命令，Subagent 在沙箱里写暴力、造数据、对拍、缩反例 |
| 通用大模型"盲猜"反例 | 幻觉严重，没编译没运行 | 真机 `g++ -O2 -std=c++20` 编译执行，`run_stress_test` 闭环比对 |
| Prompt 臃肿、注意力稀释 | 所有规范塞进 System Prompt | `SKILL.md` 渐进式加载：目录只占 ~40 tokens，按需注入全文 |
| 聊完即忘 | 无长期记忆 | `session.jsonl` + `MEMORY.md` + `PROFILE.json`（掌握度 / Bug 指纹） |
| 对拍日志污染教学对话 | 单体 Agent 上下文爆炸 | Subagent 隔离编译报错与上百次对拍，主对话只收 ~200 tokens 战报 |

## 2. 真实效果（本机可复现）

`uv run evals/run_eval.py` 在 3 道内置黄金错题上的**真实运行结果**（Windows 11 / g++ 14.1 / Python 3.13 / 单组超时 1.0s）：

| Case | 根因 | Hack | Verdict | 对拍组数 | 耗时 | 反例规模 |
| :--- | :--- | :---: | :---: | ---: | ---: | :--- |
| `case_01_multitest_reset` | 多测未清空：邻接表 `head/ecnt` 组间残留 | ✅ | WA | 4 | 5.1s | 22 tokens / 12 行 |
| `case_02_int_overflow` | `int` 累加溢出 | ✅ | WA | 1 | 4.6s | 5 tokens / 2 行 |
| `case_03_greedy_counterexample` | 性价比贪心假做法 | ✅ | WA | 3 | 4.8s | 8 tokens / 4 行 |

反例小到可以直接口算：

```text
# case_02: 4 个 1e9 相加
4
1000000000 1000000000 1000000000 1000000000
标准解 4000000000   vs   选手 int 解 -294967296

# case_03: 贪心取 6/11，正解取 5/9 + 5/9
3 10
6 11
5 9
5 9
标准解 18          vs   贪心 11
```

**真实 Agent 端到端**（`/hack` + DeepSeek，max_rounds=6，无人工干预）：主 Agent 读题 → 加载
`stress-test-hack` → 派发 Subagent 写 `brute.cpp`/`gen.py` → 第 6 组命中 WA → 反例最小化为
`2 3 / (1,1) / (3,2)`（正解 2 vs 贪心 1）→ L2 行号点拨 → `update_profile` 记录
`BUG_GREEDY_FALSE`（greedy 掌握度 60→57）→ `goal_complete` 收尾。产物见 `workspace/wa/`。

## 3. 快速开始

```bash
# 依赖（Python >= 3.11，本机需 g++；Docker 可选）
uv sync

# 配置模型（任意 OpenAI 兼容端点：DeepSeek / Kimi / 智谱 / OpenAI）
cp .env.example .env      # 填入 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL

# 连通性自检
uv run scripts/ping_llm.py

# 启动交互式 CLI
uv run main.py
```

**没有 API Key 也能验证核心引擎**（确定性、不经过大模型）：

```bash
uv run evals/run_eval.py                  # 3 道错题端到端对拍 + 生成 evals/eval_report.md
uv run python -m unittest discover -s tests   # 114 个单元/集成测试（含本地 HTTP 桩验证 LLM 协议）
```

## 4. CLI 命令

| 命令 | 说明 |
| :--- | :--- |
| `/hack <code_file> [problem_file]` | 一键对拍：自动开启 `/goal`，派发 Hacker Subagent 在 `workspace/<slug>/` 写暴力/造数据/对拍/缩反例，再按画像分层点拨并更新画像；同时把这两个文件加入**本会话只读白名单** |
| `/allow <path>` | 把本会话要用的外部文件/目录加入只读白名单（不带参数则查看已授权）；`.env`/密钥即使授权也拒绝 |
| `/goal <objective>` | 开启通用长程自治循环（模型中途停下会被自动督促继续） |
| `/goal status` / `/goal clear` | 查看 / 清除 Goal |
| `/profile` | 算法掌握度表 + 高频 Bug 指纹排行榜 |
| `/skills` | 已加载的可插拔技能 |
| `/mode L1\|L2\|L3\|L4` | 切换默认点拨深度（L1 只提问 → L4 给修复补丁） |
| `/display compact\|verbose` | 切换工具细节粒度：简略（状态+耗时）/ 详细（额外展开工具参数与结果），持久化到 `display_mode` |
| `/think` | 展开 / 收起思考过程（等价于 `Ctrl+O` / `Ctrl+Shift+O`），持久化到 `show_thinking` |
| `/log` / `/log tail N` | 查看本次运行日志路径 / 最近 N 行 Markdown 归档 |
| `/clear` | 清空短期会话（保留 `MEMORY.md` 与 `PROFILE.json`） |

## 5. 架构

```text
main.py  (rich CLI: /hack /goal /profile /skills /mode /clear)
   │
   ├── core/goal.py          外层 Goal Loop：最多 15 轮，goal_complete / goal_pause 退出
   │      └── core/agent_nodes.py   ChatNode ⇄ ToolCallNode（Flow 状态机，emit 事件流）
   │             ├── core/llm.py        OpenAI 兼容 + 指数退避重试 + 流式 tool_calls 聚合
   │             ├── core/memory.py     session.jsonl 追加写 / 崩溃残尾自愈 / 90% 阈值安全压缩
   │             └── core/profile.py    PROFILE.json：掌握度 + Bug 指纹 → 注入 System Prompt
   │
   ├── core/logbook.py       运行日志：事件流 → JSONL（机器）+ Markdown（人类归档）
   ├── core/hotkeys.py       CLI 快捷键：Ctrl+O 即时展开/收起流式思考
   │
   └── tools/
          ├── executor.py        tool_calls 解析 + 异常隔离（永不炸断 Flow）
          ├── skill_loader.py    YAML Frontmatter → Catalog 摘要 / 按需全文 / 本地脚本路径
          ├── subagent.py        独立 messages 的 Hacker Subagent（≤12 轮工具调用）
          ├── builtins/
          │     ├── file_tools.py   read / write / edit (+ ls / grep / find 供 Subagent)
          │     ├── bash_tool.py    超时强杀进程树 + 输出首尾截断
          │     └── stress_runner.py 编译 → 造数据 → 对拍 → 固化 hack.in/ans/out
          └── skills/            5 个可热插拔 XCPC SKILL.md（含 2 个本地脚本）
```

**双层循环**：内层 `ChatNode ⇄ ToolCallNode` 处理单轮工具调用；外层 `/goal` 在模型"汇报完就停"时注入督促消息，直到 `goal_complete`、`goal_pause` 或轮数熔断。

**工具面**：常规对话恰好 9 个原子工具（`read/write/edit/bash/run_stress_test/load_skill/spawn_subagent/update_profile/goal_complete`），`goal_pause` 仅在 Goal 运行期间暴露；`ls/grep/find` 只给 Subagent，避免主 Agent 工具面膨胀。

## 6. 目录职责

```text
core/                极简运行时内核（LLM / Node-Flow / Memory / Profile / Goal）
core/security.py     读白名单 / 敏感文件黑名单 / 写工作区约束 / bash 静态审查 / 子进程环境脱敏
core/logbook.py      运行日志（JSONL + Markdown）
core/hotkeys.py      Ctrl+O 思考展开快捷键（Windows msvcrt / POSIX select+cbreak）
tools/               原子工具、Subagent、Skill 加载器与 5 个 XCPC 技能包
workspace/           对拍沙箱（按题目 slug 分目录，产物不污染源码）
chat_memory/         session.jsonl（短期）+ MEMORY.md（长期）+ PROFILE.json（画像）+ logs/（运行日志）
evals/               3 个黄金错题 + run_eval.py + eval_report.md
tests/               114 个测试（核心/执行器/记忆/画像/对拍引擎/Agent Loop/LLM 协议/CLI/安全策略/输出与日志）
scripts/ping_llm.py  LLM 连通性自检
```

## 7. 关键设计决策

1. **为什么不用 LangChain/Dify**：`Node`+`Flow` 不到 60 行就能表达"有分支、可重试、可组合"的 Agent 循环；上下文拼接、KV Cache 前缀、异常恢复全部可见可控，避免框架黑盒。
2. **为什么是 9 个工具 + Skill**：每多一个工具 Schema 都会常驻 Prompt 并稀释注意力。造数据规范、点拨话术、静态审计清单本质是**知识**而非**接口**，放进 `SKILL.md` 按需加载，5 个技能目录仅占目录区约 200 tokens。
3. **为什么必须有 Subagent**：一题对拍会产生编译报错、生成器调试、上百次运行日志。Subagent 用独立 `messages` 吃掉这些噪声，只回传 ~200 tokens 结构化战报（最小反例 + 触发条件 + 文件路径）。
4. **记忆怎么兼顾成本**：`session.jsonl` 追加写保护 Prefix KV Cache；`usage.total_tokens` 超过窗口 90% 才压缩，且切分点严格避开 `assistant(tool_calls)` 与 `tool` 的配对边界；压缩失败自动降级为机械摘要，绝不中断会话。
5. **画像如何真正影响行为**：`get_profile_prompt_snippet()` 把"薄弱专题 + 高频 Bug Top3 + 最近错题"压成 ~150 tokens 注入 System Prompt，于是造数据会优先攻击历史 Bug、讲解会自动跳过已掌握概念。
6. **对拍器的工程细节**：编译一次复用、每轮独立进程、超时强杀进程树（Windows `taskkill /T /F`、POSIX `killpg`）、stdout 重定向临时文件防死锁/防打爆内存、`normalize_output` 忽略行末空格与文末空行、多解题走 SPJ。
7. **反例最小化的坑**：通用文本 ddmin 会删掉格式关键行，缩出"两个程序都读入失败但输出不同"的**非法假反例**。因此 `delta_reduce.py` 支持 `--validator` 强制校验输入合法性（多测/树结构/值域），checker 也要求两边都正常退出。

## 8. 安全边界（文件访问控制）

实现在 [`core/security.py`](core/security.py)，所有文件类工具（`read` / `write` / `edit` / `ls` / `grep` / `find`）、
`run_stress_test`、`spawn_subagent`、`bash` 都走同一个 `SecurityPolicy`：

| 维度 | 规则 |
| :--- | :--- |
| **可读目录** | `tools/skills/`、`chat_memory/`、`workspace/` |
| **可读文件（会话级）** | 用户在**本次会话**显式给出的文件：`/hack <code> <problem>` 自动授权，`/allow <path>` 手动授权（仅本次会话有效） |
| **敏感文件（始终拒绝）** | `.env` / `.env.*` / `*.pem` / `*.key` / `*.p12` / `id_rsa` / `credentials` 等；即使位于应用目录内、即使被 `/allow` 授权也拒绝 |
| **受保护目录** | `.git` / `.ssh` / `.aws` / `.gnupg` / `.venv` / `__pycache__` 等不可读、不可写 |
| **可写范围** | 仅 `workspace/<problem_slug>/...`；不能写工作区根目录、应用源码、`chat_memory/` 或应用外路径 |
| **bash** | 工作目录必须在工作区内；命令禁止 `..` 穿越、应用外绝对路径、`.env`/密钥名、环境变量读取、联网与系统配置命令、符号链接创建 |
| **子进程环境** | 编译/运行/`gen.py` 的子进程统一剥离 `OPENAI_*` / `*_API_KEY` / `*_TOKEN` 等变量，被测程序拿不到 LLM 密钥 |

**实测**：真实 `/hack` 端到端跑通时，策略精确拦截了越界命令（并暴露了一个 `dir /b` 被误判为 Unix 路径的 bug，
已修复为 Windows 命令开关白名单）。`tests/test_security.py` 覆盖读越权、写越权、敏感文件、路径穿越、
符号链接逃逸、bash 审查、环境脱敏等 20+ 用例。

**已知限制（必须知道）**：`bash` 是**静态审查 + 工作目录限定**，不是操作系统级沙箱。
用户/模型若在工作区里写一个脚本并让解释器执行，脚本本身仍可能读到工作区外的文件。
要做到硬隔离，需要 Docker/Job Object（本版本未实现，见第 11 节）。

## 9. 终端输出、思考开关与运行日志（"终端安静、档案完整"）

### 9.1 设计目标

Agent 的思考过程动辄数千 token，直接刷到终端会淹没真正的结论；但完全不记录又无法复盘。
因此拆成三条独立通道：

1. **正文流式输出**：教练回答边生成边滚屏，状态行始终打印在输入提示上方。
2. **思考默认隐藏、可即时展开**：`on_reasoning` 收到的流式推理默认丢弃，按 `Ctrl+O`
   （多数终端下 `Ctrl+Shift+O` 同码）或 `/think` 即时展开/收起，状态持久化到
   `PROFILE.json.preferences.show_thinking`。
3. **细节全量落盘**：`core/logbook.py` 的 `RunLogger` 把每个事件同时写成
   `chat_memory/logs/session_<时间>.jsonl`（机器可读）与 `.md`（人类归档）——**无论思考是否展开**。

### 9.2 事件流（谁产生、谁消费）

```text
ChatNode ──emit("llm_start")──────┐
        └─emit("llm_end", content, reasoning, reasoning_streamed, usage, elapsed, final)
ToolCallNode ─emit("tool_start")──┤──> AgentSession.on_event ──> Runtime.on_event
             └emit("tool_end", ok, result, elapsed)                ├── RunLogger.log()  （JSONL + Markdown）
                                                                    └── _render_event()  （compact / verbose 渲染）

call_llm ──on_delta────> Runtime.on_text ──（首个正文字符时收束 [思考完成] 状态行）
         └─on_reasoning> Runtime.on_reasoning ──（show_thinking 时才打印 dim 推理）
```

- 事件：`user_message` / `llm_start` / `llm_end` / `tool_start` / `tool_end` / `turn_end` / `goal_round` / `goal_end`。
- `llm_end.reasoning_streamed` 区分"推理已流式展示"与"provider 只在完整响应里带 reasoning"，后者在展开时会补一张面板。
- `AgentSession.emit()` 对展示层异常做隔离——日志或渲染失败绝不影响 Agent 主流程。
- `ChatNode` **不直接 print 正文**，避免"正文先出、状态行粘在正文末尾"的重复渲染 bug
  （`tests/test_display_log.py` 有回归用例）。

### 9.3 快捷键与命令

| 操作 | 效果 | 生效时机 |
| :--- | :--- | :--- |
| `Ctrl+O` / `Ctrl+Shift+O` | 展开/收起流式思考 | 仅 Agent 运行期间（此时 `input()` 不读 stdin，不会抢字符） |
| `/think` | 同上，静默切换 | 任意时刻（空闲也能用） |
| `/display compact\|verbose` | 工具参数/结果/上下文规模的详细程度 | 任意时刻，持久化到 `display_mode` |

实现见 [`core/hotkeys.py`](core/hotkeys.py)：Agent 运行前 `start()`、结束后 `stop()`；
Windows 用 `msvcrt.kbhit()` 轮询，Linux/macOS 用 `select + cbreak`；`Ctrl+C` 会通过
`_thread.interrupt_main()` 转成主线程 `KeyboardInterrupt`，不会被监听线程吞掉。

**终端兼容性**：`Ctrl+Shift+O` 的键码由终端决定，部分终端会转成 `Ctrl+O`（0x0F），
也有终端直接拦截——因此 `/think` 是稳定后备。非 TTY（管道/CI）下快捷键自动禁用。

### 9.4 为什么状态行这样排

- 状态行只在 Agent 运行期间打印，而输入提示 `» ` 只在 `input()` 时出现，
  因此所有状态行天然位于提示上方；正文与工具输出在其后滚动属于预期。
- `[思考完成] X.Xs` 由 **第一个正文字符** 触发收束（time-to-first-token），
  所以它可能正好夹在"思考"与"正文"之间；若模型只返回工具调用，则在 `llm_end` 用真实耗时收束。
- 展开思考时，推理以 dim 流式打印，收束前补一个换行，避免状态行粘在推理末尾。

### 9.5 日志为什么放在 `chat_memory/`

`chat_memory/` 在安全策略的可读白名单内，因此**后续 Agent 可以直接读日志做复盘/画像提炼/回归**；
同时它被 `.gitignore` 忽略，不会把用户的训练对话误提交。Markdown 归档可直接给用户存档查阅。

## 10. 评测（`evals/`）

```bash
uv run evals/run_eval.py              # 离线引擎模式：真实编译 + 对拍，不调用大模型
uv run evals/run_eval.py --minimize   # 额外执行合法性强校验的 ddmin 缩减
uv run evals/run_eval.py --agent      # 额外用真实 Agent 跑一遍（需 API Key，统计主上下文 token）
```

输出指标：**Hack Success（二次复现校验）/ Verdict / Iterations / Latency / 反例规模 / Main Context Tokens / Profile Updated**，同时写入 `evals/eval_report.md`。画像写入隔离目录 `evals/.eval_memory/`，不污染真实训练数据。

## 11. 已知限制

- 离线评测衡量的是**对拍引擎与画像闭环**的确定性能力，不衡量 LLM 决策质量；后者需要 `--agent` 模式或人工 `/hack` 观察。
- `XCPC_SANDBOX_MODE` 目前只实现了进程级受限沙箱（超时熔断 + 输出限额 + 目录隔离）；Docker 容器隔离是预留扩展点，不做假实现。
- `bash` 不是安全沙箱（见第 8 节）：静态规则能拦住绝大多数越界访问，但无法阻止"工作区内脚本自行读文件"。
- 通用 ddmin 无法重排多测结构（改 `T` 需同时增删整组数据），多测类反例通常只能收缩数值；真实 Agent 会按题面写更强的结构化 reducer（`--validator` 保证不会缩出非法数据）。
