# XCPC-Claw · 算法竞赛智能对拍与私人教练

> 过了样例交上去却 `Wrong Answer on test 54`？
> 不想手写暴力、不想调随机数生成器、也不想被题解直接剧透？
> **XCPC-Claw** 是专为 ICPC / CCPC / 蓝桥杯 / Codeforces / AtCoder 选手打造的本地命令行训练助手。

---

## ✨ 你可以用它做什么

1. **🎯 全自动真机对拍找反例（Auto-Hack）**
   把你的错误代码和题面丢给它，它会自动在本地沙箱里写暴力标程（`brute.cpp`）、造数据脚本（`gen.py`）和校验器（`spj.cpp`），调用本机 `g++` 真机编译对拍，并把拍出来的反例**缩减到一眼能手算的最小规模**（如 $N \le 4$）。
2. **💡 苏格拉底式四级点拨（防剧透教学）**
   拒绝一上来就贴 AC 代码剥夺思考乐趣。支持 **L1 ~ L4 四档提示深度**自由切换——从“只点出数据形态让你自己想”到“展示最小反例”，再到“定位错误行号”或“直接给修复补丁并重拍验证”。
3. **🧬 越用越懂你的选手画像（Contestant Profile）**
   自动记录你的算法专题掌握度与高频 Bug 指纹（如“漏开 `long long`”、“多测未清空邻接表”、“局部贪心假做法”）。下次对拍时会**优先朝你常踩的坑造数据**，点拨时也会主动提醒你犯过的同类错误。
4. **🔒 安全隔离不污染源码**
   所有对拍中间产物均隔离在 `workspace/<题名>/` 下，绝不改动你的源码文件；内置安全策略严格保护 `.env` 等密钥文件不被读取。

---

## 🔍 真实对拍效果展示

不需要面对几千行的随机乱码数据，**XCPC-Claw** 会自动把反例缩减到你可以直接口算的规模：

| 常见错因场景 | 拍出的极小反例 (`hack.in`) | 标程输出 (`hack.ans`) vs 你的输出 (`hack.out`) | 教练 L2 点拨效果 |
| :--- | :--- | :--- | :--- |
| **累加漏开 `long long`** | `4`<br>`1000000000 1000000000 1000000000 1000000000` | 标程：`4000000000`<br>你的：`-294967296` ❌ | 给出 4 个 $10^9$ 的最小反例，提问累加变量类型上界 |
| **局部贪心假做法** | `3 10`<br>`6 11`<br>`5 9`<br>`5 9` | 标程：`18`（选两个 `5 9`）<br>你的：`11`（贪心选了 `6 11`）❌ | 展示容量为 10 时的反例，引导思考性价比最高是否等于全局最优 |
| **多测未清空邻接表/数组** | `T=2`（先跑一组大图，紧接着跑一组小图） | 标程：第二组独立正确结果<br>你的：被第一组残留数据污染 ❌ | 指出“单独跑第二组是对的，跟在第一组后面就错”，提问清空范围 |

---

## 🚀 3 分钟快速上手

### 1. 环境准备

使用前请确保本机已安装：
- **Python `>= 3.11`** 与包管理器 **[`uv`](https://docs.astral.sh/uv/)**
- **C++ 编译器 `g++`**（需支持 C++17/C++20，Windows 推荐 MinGW-w64 并加入 `PATH`，Linux/macOS 直接安装 GCC）
- 一个兼容 OpenAI 接口的大模型 API Key（推荐使用 **DeepSeek**、Kimi、智谱 GLM 或 OpenAI）

安装项目依赖：

```bash
uv sync
```

### 2. 配置大模型 API Key

复制环境变量模板并填入你的 API Key：

```bash
# Linux / macOS
cp .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

编辑 `.env` 文件（完整可选配置见文末配置说明）：

```ini
OPENAI_API_KEY=sk-你的密钥
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
```

（可选）测试大模型连通性：

```bash
uv run scripts/ping_llm.py
```

### 3. 启动助手

```bash
uv run main.py
```

也支持通过命令行参数临时覆盖配置：

```bash
uv run main.py --model deepseek-reasoner --max-rounds 15
```

---

## 🎮 核心玩法与命令速查

启动进入 `» ` 交互提示符后，你可以直接用自然语言和教练对话（讨论思路、询问复杂度、请求造特殊图数据），或使用以下快捷命令：

| 命令 | 用途说明 | 示例 |
| :--- | :--- | :--- |
| **`/hack <代码> [题面]`** | **一键自动对拍 + 查错点拨**（最常用！） | `/hack ./my_sol.cpp ./problem.md` |
| **`/mode <L1\|L2\|L3\|L4>`** | 切换教练的默认点拨深度（防剧透等级） | `/mode L2` |
| **`/display <compact\|verbose>`** | 切换工具细节粒度：**简略**（默认，只显示状态与耗时）/ **详细**（额外展开工具参数与结果） | `/display verbose` |
| **`/think`** | 展开 / 收起思考过程（等价于运行中按 `Ctrl+O` 或 `Ctrl+Shift+O`） | `/think` |
| **`/log`** | 查看本次运行日志路径；`/log tail 50` 查看最近 50 行归档日志 | `/log tail 80` |
| **`/profile`** | 查看你的算法掌握度雷达表与高频 Bug 排行榜 | `/profile` |
| **`/goal <目标>`** | 开启长链路自主任务（如整题从零补题、验证思路） | `/goal 帮我验证树形DP换根思路并写标程对拍` |
| **`/goal status`** / **`clear`** | 查看或终止当前正在进行的自主任务 | `/goal status` |
| **`/allow [路径]`** | 授权助手在本会话中只读访问指定外部文件/目录（不带参数查看已授权列表） | `/allow D:/contest/round_950/` |
| **`/skills`** | 查看当前已装载的 5 个算法竞赛专项技能包 | `/skills` |
| **`/clear`** | 清空当前对话上下文（保留你的长期画像与训练记忆） | `/clear` |
| **`/help`** / **`/exit`** | 查看帮助 / 退出程序（也可用 `/quit` 或 `/q`） | `/exit` |

---

## 🖥️ 终端输出、思考开关与运行日志

**正文是流式输出的**（边生成边显示），但**模型的思考过程默认隐藏**。终端上始终只有一行行状态与耗时，状态行打印在输入提示上方，正文在其后自然滚动：

```text
» /hack my_sol.cpp problem.md
[目标] Hack · my_sol.cpp · 最多 6 轮
[轮次] 1/6
[思考中] 上下文 8 条 / 工具 9 个
[思考完成] 3.2s · 思考已隐藏（Ctrl+O 展开）
（教练的正文在这里流式滚动……）
[测试中] spawn_subagent
[完成] spawn_subagent 45.1s
[本轮完成] 50.9s
```

### 随时偷看 / 收起思考：`Ctrl+O` 或 `/think`

Agent 正在思考或输出时，直接按 **`Ctrl+O`**（多数终端下 **`Ctrl+Shift+O`** 与它是同一个键码）：

```text
» [思考过程] 已展开 —— Ctrl+O / Ctrl+Shift+O 或 /think 可切换
» [思考中] 上下文 6 条 / 工具 9 个
🧠 思考过程
（灰色流式推理……）
[思考完成] 5.5s
（教练的正文）
```

再按一次即可收起。注意快捷键只在 **Agent 运行期间** 生效（此时命令行不在读输入，不会和你打字抢字符）；
如果你在等待输入的空闲状态，或者终端把 `Ctrl+Shift+O` 拦截了，用 `/think` 命令效果完全一样。
展开状态会被记住（写入 `PROFILE.json.preferences.show_thinking`），下次启动保持不变。

### 工具细节：`/display verbose`

`/display verbose` 会额外展示工具入参、结果面板与上下文规模；它**不影响思考的显示**，思考仍由 `Ctrl+O / /think` 控制。

### 日志：终端安静，档案完整

无论思考是否展开，**每一轮完整推理、工具调用、Goal 轮次和耗时都会落盘**到 `chat_memory/logs/`：

- `session_<时间>.md`：人类可读归档（含完整思考过程、工具调用、Goal 轮次与耗时），适合赛后复盘；
- `session_<时间>.jsonl`：机器可读事件流，**后续 Agent 可直接读取**（该目录在 Agent 的可读白名单内），用于提取错因、生成训练周报或做回归分析。

终端里拼错命令？`/log` 看路径，`/log tail 50` 直接翻最后 50 行，不用离开 CLI。
启动参数：`--no-stream` 关闭流式（仍会打印完整正文）、`--no-hotkeys` 关闭快捷键、`--no-log` 关闭日志。

---

## 📖 典型使用场景详解

### 场景一：样例全过但提交 WA，一键找反例 (`/hack`)

1. 把你的代码保存为 `my_sol.cpp`，把题目描述复制保存为 `problem.md`（支持 Markdown 或纯文本；若不传题面文件，教练会先做静态审计或向你确认关键约束）。
2. 在终端中输入：
   ```text
   » /hack my_sol.cpp problem.md
   ```
3. **XCPC-Claw 会自动为你完成**：
   - 读取题意与你的代码，并参考你的历史画像；
   - 在后台沙箱 `workspace/my_sol/` 中编写绝对正确的朴素暴力解与随机数据生成器（多解或浮点题会自动补充 `spj.cpp`）；
   - 自动用 `g++` 编译并高速循环跑数百组测试数据；
   - 一旦抓到 WA / TLE / RE，立即把反例缩减到极小规模，保存至 `workspace/my_sol/hack.in`（标准答案在 `hack.ans`，你的输出在 `hack.out`）；
   - 按你当前的点拨档位（默认 `L2`）给出最小反例和引导问题，帮你自己想明白错在哪，并把本次 Bug 记入你的选手画像。

> 💡 **体验内置示例**：你可以直接拿仓库自带的 3 道典型错题试试手：
> ```text
> » /hack evals/cases/case_01_multitest_reset/wa.cpp evals/cases/case_01_multitest_reset/problem.md
> » /hack evals/cases/case_02_int_overflow/wa.cpp evals/cases/case_02_int_overflow/problem.md
> » /hack evals/cases/case_03_greedy_counterexample/wa.cpp evals/cases/case_03_greedy_counterexample/problem.md
> ```

---

### 场景二：按需调节提示深度 (`/mode`)

不同训练阶段需要不同程度的提示，随时用 `/mode` 切换：

- **`/mode L1`（纯思维点拨）**：不给反例数据也不报行号，只提醒你“想一想当图不连通或者有重边时会发生什么？”——适合赛前高强度独立训练。
- **`/mode L2`（最小反例点拨 · 默认）**：给你展示极小反例 `hack.in`、标准输出 `hack.ans` 和你的输出 `hack.out`，附带一个针对性思考问题，让你自己对照手算调试。
- **`/mode L3`（精确行号与变量追踪）**：看了反例还是找不到 Bug？切到 `L3`，教练会直接指出你的代码在第几行、哪个变量在哪一步开始算偏。
- **`/mode L4`（补丁修复 + 千组重拍）**：彻底卡死或赛后赶时间补题时使用，直接给出最小修改补丁（diff），并在沙箱里重跑 500+ 组对拍确认修复有效。

---

### 场景三：追踪长期训练画像 (`/profile`) 与专项技能 (`/skills`)

每次抓出 Bug 后，助手都会把错因归档到你的本地画像库中。输入 `/profile` 即可查看：

- **算法掌握度**：直观展示你在 `dp`、`greedy`、`graph`、`data_structures`、`math`、`string` 等专题上的当前评分与薄弱备注。
- **高频 Bug 指纹榜**：统计你最容易犯的错误类型（如 `BUG_INT_OVERFLOW` 漏开 `long long`、`BUG_MULTITEST_DIRTY` 多测未清空、`BUG_GREEDY_FALSE` 假贪心、`BUG_BOUNDARY_N1` 边界漏判等）。

同时，你可以输入 `/skills` 查看内置的 5 大专项能力包（对话中教练会自动按需调用）：
- `stress-test-hack`：全自动对拍与反例最小化标准流程
- `socratic-xcpc-tutor`：苏格拉底式四级防剧透点拨规范
- `graph-tree-gen`：树与图论特殊拓扑生成器（链、菊花图、森林、DAG、重边自环）
- `complexity-edge-audit`：时间/空间复杂度与常见边界静态审计清单
- `profile-analyzer`：选手画像分析与针对性训练建议

---

## ⚙️ 常用配置项（`.env`）

除了必填的 API Key 外，你还可以在 `.env` 中按需调整以下运行参数：

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `OPENAI_API_KEY` | （必填） | 大模型 API 密钥 |
| `OPENAI_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容接口地址（支持 DeepSeek / Kimi / 智谱 / OpenAI 等） |
| `OPENAI_MODEL` | `deepseek-chat` | 模型名称（如 `deepseek-chat`、`deepseek-reasoner`） |
| `XCPC_TIMEOUT_SEC` | `2.0` | 对拍时单组测试数据的运行超时时间（秒） |
| `CXX` | 自动探测 `g++` | 显式指定 C++ 编译器路径 |
| `XCPC_STREAM` | `1` | 是否流式输出正文（思考过程仍默认隐藏；`0` 则整段返回） |
| `XCPC_MAX_CONTEXT` | `128000` | 触发自动摘要压缩的上下文窗口大小 |
| `XCPC_LLM_TIMEOUT_SEC` | `180` | 单次大模型请求超时时间（秒） |

---

## 🛠️ 常见问题（FAQ）

**Q1：对拍生成的文件保存在哪里？会弄乱我的代码目录吗？**
不会。所有生成的暴力程序、造数据脚本和反例数据都严格保存在项目下的 `workspace/<你的代码文件名>/` 目录中：
- `hack.in`：触发错误的最小反例输入
- `hack.ans`：暴力标程跑出的正确输出
- `hack.out`：你的代码跑出的实际输出
- `brute.cpp` / `gen.py` / `spj.cpp`：本次对拍生成的暴力标程、数据生成器与校验器

**Q2：为什么提示“读取被安全策略拒绝”？**
为了保护你的电脑隐私与 API 密钥安全，助手默认只能读取项目工作区以及你在 `/hack` 命令中显式传入的文件。如果你在对话中临时想让它看另一个路径下的代码或题面，只需先执行一次：
```text
» /allow 你的文件路径.cpp
```

**Q3：没有配置 API Key 可以体验对拍引擎吗？**
可以！仓库内置了离线对拍基准测试，无需配置任何大模型密钥，直接运行以下命令即可体验真机 `g++` 编译与自动对拍（同时在无 API Key 时你也可以启动 `uv run main.py` 使用 `/profile`、`/skills`、`/mode` 等本地命令）：
```bash
uv run evals/run_eval.py
```

**Q4：为什么终端里看不到模型的思考过程？**
思考默认隐藏，避免长篇推理刷屏。三种看法任选：
- Agent 运行中按 **`Ctrl+O`**（多数终端下 `Ctrl+Shift+O` 同码）即时展开，再按一次收起；
- 输入 **`/think`** 切换（空闲时也能用，效果相同）；
- `/log tail 100` 或直接打开 `chat_memory/logs/*.md` 查阅归档。

快捷键只会在 Agent 运行期间监听（不会抢你打字）；个别终端会拦截 `Ctrl+Shift+O`，此时用 `/think` 即可。

**Q5：为什么状态行有时候出现在正文前后？**
状态行（`[思考中]` / `[思考完成]` / `[测试中]`）总是在输入提示上方打印，正文与工具输出在它之后继续滚动，这是预期行为。`[思考完成]` 会在第一个正文字符到达时收束，所以它可能正好夹在思考与正文之间。

**Q6：我想重置自己的选手画像或历史记忆怎么办？**
- 只想开启新话题、保留历史画像：在程序内输入 `/clear`。
- 想彻底重置画像重新开始：直接删除 `chat_memory/PROFILE.json` 和 `chat_memory/MEMORY.md`，下次启动时会自动生成全新初始画像。
- 想清理历史运行日志：删除 `chat_memory/logs/` 即可（不影响画像与记忆）。

---

## 📚 开发者与架构文档

如果你对 **XCPC-Claw** 的底层轻框架实现（`Node/Flow` 状态机、外层 `/goal` 循环、Subagent 上下文隔离、渐进式 `SKILL.md` 体系、安全沙箱策略、终端事件流与运行日志、单元测试）感兴趣，请查阅开发者文档：

👉 **[readme_dev.md](readme_dev.md)**
