"""XCPC-Claw CLI 主入口。

交互命令：``/hack`` ``/goal`` ``/profile`` ``/skills`` ``/mode`` ``/clear`` ``/help`` ``/exit``。
支持自然语言对话；无 API Key 时仍可用 ``/profile`` ``/skills`` ``/mode`` 等本地命令。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.agent_nodes import AgentSession, SessionState
from core.goal import GoalState, run_goal
from core.hotkeys import HotkeyListener
from core.llm import configure, current_model, has_api_key
from core.logbook import RunLogger
from core.memory import Memory, project_root
from core.profile import DISPLAY_MODE_LABEL, DISPLAY_MODES, HINT_LEVEL_LABEL, Profile
from core.security import SecurityPolicy
from tools import DEFAULT_SKILLS_DIR, get_skill_catalog, get_tools, list_skill_summaries

BANNER = """[bold cyan]XCPC-Claw[/bold cyan] · AI-Native 对拍 Hack 与个性化教练 Agent
[dim]Node + Flow + Outer Goal Loop + Progressive Skills + Subagent Isolation[/dim]"""

# 工具 -> 终端状态标签（简略模式下只显示这些 + 耗时）
TOOL_STATUS_LABELS = {
    "read": "[读取中]",
    "write": "[写入中]",
    "edit": "[修改中]",
    "ls": "[列目录]",
    "grep": "[搜索中]",
    "find": "[搜索中]",
    "bash": "[运行中]",
    "run_stress_test": "[测试中]",
    "spawn_subagent": "[测试中]",
    "load_skill": "[加载技能]",
    "update_profile": "[更新画像]",
    "goal_complete": "[收尾]",
    "goal_pause": "[等待输入]",
}

HELP_TEXT = """[bold]可用命令[/bold]
  [cyan]/hack <code_file> [problem_file][/cyan]  一键对拍：自动开启 Goal，派发 Hacker Subagent 找最小反例，再分层点拨
  [cyan]/allow <path>[/cyan]                     把本次会话要用的外部文件/目录加入只读白名单（不带参数则查看已授权）
  [cyan]/goal <objective>[/cyan]                 开启通用长程自治循环
  [cyan]/goal status[/cyan] | [cyan]/goal clear[/cyan]   查看 / 清除当前 Goal
  [cyan]/profile[/cyan]                          打印算法掌握度与高频 Bug 指纹
  [cyan]/skills[/cyan]                           列出已加载的可插拔技能
  [cyan]/mode <L1|L2|L3|L4>[/cyan]               切换默认点拨深度
  [cyan]/think[/cyan]                            展开 / 收起流式思考过程（等价于 Ctrl+O / Ctrl+Shift+O）
  [cyan]/display <compact|verbose>[/cyan]        切换终端输出：简略（状态+耗时）/ 详细（额外展开工具参数与结果）
  [cyan]/log [tail [n]][/cyan]                   查看本次运行日志路径 / 查看最近 n 行 Markdown 日志
  [cyan]/clear[/cyan]                            清空短期会话（保留 MEMORY.md 与 PROFILE.json）
  [cyan]/help[/cyan] | [cyan]/exit[/cyan]

直接输入自然语言即可与教练对话（Goal 运行中则继续推进当前目标）。
快捷键：Agent 运行中按 [bold]Ctrl+O[/bold]（多数终端下 Ctrl+Shift+O 同码）即时展开/收起思考过程。"""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", name).strip("_.")
    return slug or "problem"


@dataclass
class Runtime:
    console: Console
    memory: Memory
    profile: Profile
    goal: GoalState
    session: AgentSession
    registry: object
    policy: SecurityPolicy
    model: str | None = None
    stream: bool = True
    display: str = "compact"
    show_thinking: bool = False
    logger: RunLogger | None = None
    hotkeys: HotkeyListener | None = None
    _streamed: bool = False
    _started: float = 0.0
    _llm_started: float = 0.0
    _thinking_status_open: bool = False
    _reasoning_started: bool = False
    _thinking_hint_shown: bool = False

    # ---------------------------- 输出策略 ---------------------------- #
    def on_event(self, kind: str, data: dict[str, Any]) -> None:
        """Agent 事件 -> 落盘日志 + 按展示模式渲染。"""
        if self.logger is not None:
            try:
                payload = dict(data)
                if kind == "llm_end":
                    payload["model"] = self.model or current_model()
                self.logger.log(kind, **payload)
            except OSError:
                pass
        try:
            self._render_event(kind, data)
        except Exception:  # noqa: BLE001 - 渲染失败不应影响 Agent
            pass

    def on_text(self, chunk: str) -> None:
        """流式正文：第一个正文字符到达即说明思考阶段结束。"""
        self._close_thinking_status()
        self._streamed = True
        self.console.print(chunk, end="", markup=False, highlight=False, soft_wrap=True)

    def on_reasoning(self, chunk: str) -> None:
        """流式思考：默认丢弃（日志仍完整记录），Ctrl+O 展开时才实时显示。"""
        if not self.show_thinking:
            return
        if not self._reasoning_started:
            self.console.print(Text("🧠 思考过程", style="dim italic"))
            self._reasoning_started = True
        self.console.print(chunk, end="", markup=False, highlight=False, style="dim italic")

    def _close_thinking_status(self, elapsed: float | None = None) -> None:
        """把「[思考中]」收束为一行带耗时的状态（在正文开始前打印，保证状态行在上）。"""
        if not self._thinking_status_open:
            return
        self._thinking_status_open = False
        if self._reasoning_started:
            self.console.print()  # 结束流式 dim 思考行，状态另起一行
            self._reasoning_started = False
        if elapsed is None:
            elapsed = time.perf_counter() - self._llm_started if self._llm_started else 0.0
        line = Text()
        line.append("[思考完成]", style="dim")
        line.append(f" {elapsed:.1f}s", style="dim")
        if not self.show_thinking:
            line.append(" · 思考已隐藏（Ctrl+O 展开）", style="dim")
        self.console.print(line)

    def toggle_thinking(self) -> None:
        """Ctrl+O / Ctrl+Shift+O / /think：即时展开或收起思考过程。"""
        self.show_thinking = not self.show_thinking
        if self._reasoning_started and not self.show_thinking:
            self.console.print()  # 结束当前 dim 思考行
        try:
            self.profile.set_show_thinking(self.show_thinking)
        except OSError:
            pass
        state = "展开" if self.show_thinking else "隐藏"
        self.console.print()
        self.console.print(
            Text(f"[思考过程] 已{state} —— Ctrl+O / Ctrl+Shift+O 或 /think 可切换", style="yellow")
        )

    def _status(self, label: str, detail: str = "", style: str = "dim") -> None:
        line = Text()
        line.append(label, style=style)
        if detail:
            line.append(" " + detail, style="dim")
        self.console.print(line)

    def _render_event(self, kind: str, data: dict[str, Any]) -> None:
        compact = self.display == "compact"
        if kind == "llm_start":
            self._llm_started = time.perf_counter()
            self._thinking_status_open = True
            self._reasoning_started = False
            if compact:
                if not self.show_thinking and not self._thinking_hint_shown:
                    self._thinking_hint_shown = True
                    self._status("[思考中]", "思考内容默认隐藏，Ctrl+O / Ctrl+Shift+O 可展开")
                else:
                    self._status("[思考中]", "等待模型响应…")
            else:
                self._status(
                    "[思考中]",
                    f"上下文 {data.get('message_count', 0)} 条 / 工具 {data.get('tool_count', 0)} 个",
                )
            return

        if kind == "llm_end":
            self._close_thinking_status(float(data.get("elapsed") or 0.0))
            reasoning = str(data.get("reasoning") or "").strip()
            # 非流式返回的思考（部分 provider 不推 reasoning delta）在展开时补一张面板
            if reasoning and self.show_thinking and not data.get("reasoning_streamed"):
                self.console.print(Panel(Text(reasoning), title="🧠 思考过程", border_style="dim", expand=False))
            content = str(data.get("content") or "")
            if content and not data.get("streamed") and (not compact or data.get("final")):
                self.console.print()
                self.console.print(Text(content))
            return

        if kind == "tool_start":
            name = str(data.get("name") or "?")
            if compact:
                self._status(TOOL_STATUS_LABELS.get(name, "[运行中]"), name)
            else:
                args_text = _clip(json.dumps(data.get("args") or {}, ensure_ascii=False), 400)
                line = Text()
                line.append("[工具]", style="magenta")
                line.append(f" {name} ", style="bold")
                line.append(args_text, style="dim")
                self.console.print(line)
            return

        if kind == "tool_end":
            name = str(data.get("name") or "?")
            elapsed = float(data.get("elapsed") or 0)
            ok = bool(data.get("ok"))
            if compact:
                self._status("[完成]" if ok else "[失败]", f"{name} {elapsed:.1f}s", style="green" if ok else "red")
            else:
                body = Text(_clip(str(data.get("result") or ""), 1200), style="dim")
                self.console.print(
                    Panel(body, title=f"{name} · {elapsed:.2f}s", border_style="blue" if ok else "red", expand=False)
                )
            return

        if kind == "turn_end":
            self._status("[本轮完成]", f"{float(data.get('elapsed') or 0):.1f}s")
        return

    @contextmanager
    def _hotkey_window(self) -> Iterator[None]:
        """仅在 Agent 运行期间接管 Ctrl+O（此时没有 input() 在读 stdin）。"""
        listener = self.hotkeys
        started = bool(listener and listener.start())
        try:
            yield
        finally:
            if started and listener is not None:
                listener.stop()

    def end_stream(self) -> None:
        if self._streamed:
            self.console.print()
            self._streamed = False

    # ---------------------------- 组装 ---------------------------- #
    @classmethod
    def build(
        cls,
        *,
        console: Console | None = None,
        model: str | None = None,
        stream: bool = True,
        memory_dir: str | Path | None = None,
        max_rounds: int = 15,
        base_dir: str | Path | None = None,
        skills_dir: str | Path | None = None,
        policy: SecurityPolicy | None = None,
        display: str | None = None,
        enable_log: bool = True,
        enable_hotkeys: bool = True,
    ) -> "Runtime":
        console = console or Console()
        memory = Memory(memory_dir)
        profile = Profile(memory.memory_dir)
        goal = GoalState(max_rounds=max_rounds)
        skills = Path(skills_dir) if skills_dir else DEFAULT_SKILLS_DIR
        app_root = Path(base_dir) if base_dir else project_root()
        policy = policy or SecurityPolicy(app_root)
        display_mode = str(display or profile.display_mode or "compact").strip().lower()
        if display_mode not in DISPLAY_MODES:
            display_mode = "compact"
        logger = RunLogger(memory.memory_dir / "logs") if enable_log else None
        registry = get_tools(
            base_dir=app_root,
            profile=profile,
            memory=memory,
            goal=goal,
            skills_dir=skills,
            policy=policy,
        )
        runtime = cls(
            console=console,
            memory=memory,
            profile=profile,
            goal=goal,
            session=None,  # type: ignore[arg-type]
            registry=registry,
            policy=policy,
            model=model,
            stream=stream,
            display=display_mode,
            show_thinking=profile.show_thinking,
            logger=logger,
            _started=time.perf_counter(),
        )
        runtime.session = AgentSession(
            memory=memory,
            profile=profile,
            registry=registry,
            skill_catalog=get_skill_catalog(skills),
            state=SessionState(goal=goal),
            on_text=runtime.on_text,
            on_reasoning=runtime.on_reasoning,
            on_event=runtime.on_event,
            model=model,
            stream=stream,
        )
        if enable_hotkeys:
            runtime.hotkeys = HotkeyListener(runtime.toggle_thinking)
        if logger is not None:
            logger.log(
                "session_start",
                model=model or current_model(),
                display=display_mode,
                show_thinking=runtime.show_thinking,
                memory_dir=str(memory.memory_dir),
                workspace=str(app_root / "workspace"),
                hotkeys=bool(runtime.hotkeys and runtime.hotkeys.available),
            )
        return runtime

    # ---------------------------- 命令实现 ---------------------------- #
    def print_profile(self) -> None:
        console = self.console
        prefs = self.profile.data.get("preferences", {})
        console.print(
            Panel(
                f"选手: [bold]{self.profile.data.get('contestant_id', 'default_player')}[/bold]    "
                f"语言: {prefs.get('language', 'C++20')}    讲解模式: {prefs.get('coaching_mode', 'socratic')}    "
                f"默认点拨: [cyan]{HINT_LEVEL_LABEL[self.profile.hint_level]}[/cyan]    "
                f"输出: [cyan]{self.display}[/cyan]",
                title="选手画像",
                border_style="cyan",
            )
        )
        topic_table = Table(title="算法掌握度", header_style="bold cyan", expand=False)
        topic_table.add_column("专题", style="bold")
        topic_table.add_column("分数", justify="right")
        topic_table.add_column("等级")
        topic_table.add_column("备注", overflow="fold")
        for row in self.profile.topic_rows():
            bar = "█" * int(row["score"] / 10) + "░" * (10 - int(row["score"] / 10))
            topic_table.add_row(row["topic"], f"{row['score']:>3} {bar}", row["level"], str(row["notes"])[:80])
        console.print(topic_table)

        bug_table = Table(title="高频 Bug 指纹", header_style="bold red", expand=False)
        bug_table.add_column("ID", style="bold")
        bug_table.add_column("名称", overflow="fold")
        bug_table.add_column("次数", justify="right")
        bug_table.add_column("最近题目")
        for row in self.profile.bug_rows():
            bug_table.add_row(row["id"], row["name"], str(row["count"]), row["last"] or "-")
        console.print(bug_table)
        console.print(f"[dim]记忆文件: {self.memory.session_path} / {self.memory.memory_path} / {self.profile.path}[/dim]")

    def print_skills(self) -> None:
        rows = list_skill_summaries(DEFAULT_SKILLS_DIR)
        table = Table(title="可插拔技能（SKILL.md）", header_style="bold green", expand=False)
        table.add_column("Skill", style="bold")
        table.add_column("描述", overflow="fold")
        table.add_column("脚本", justify="right")
        for row in rows:
            table.add_row(row["name"], row["description"], str(len(row["scripts"])))
        self.console.print(table)
        self.console.print("[dim]新增技能：在 tools/skills/ 下创建 <name>/SKILL.md 即可，无需改代码。[/dim]")

    def set_mode(self, raw: str) -> None:
        try:
            level = self.profile.set_hint_level(raw)
        except ValueError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        self.console.print(f"[green]已切换默认点拨层级 -> {HINT_LEVEL_LABEL[level]}[/green]")

    def set_display(self, raw: str) -> None:
        try:
            value = self.profile.set_display_mode(raw)
        except ValueError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        self.display = value
        self.console.print(f"[green]已切换输出模式 -> {value}：{DISPLAY_MODE_LABEL[value]}[/green]")
        if value == "verbose":
            self.console.print("[dim]思考过程仍默认隐藏：Ctrl+O / Ctrl+Shift+O 或 /think 可随时展开。[/dim]")
        self.console.print("[dim]无论哪种模式，思考过程与工具细节都会完整写入运行日志（/log 查看）。[/dim]")

    def print_log(self, rest: str) -> None:
        if self.logger is None:
            self.console.print("[yellow]本次运行未启用日志（--no-log）。[/yellow]")
            return
        args = rest.split()
        if args and args[0].lower() in {"tail", "last"}:
            count = 30
            if len(args) > 1 and args[1].isdigit():
                count = max(1, min(500, int(args[1])))
            self.console.print(Text(self.logger.markdown_tail(count)))
            return
        self.console.print(Text(self.logger.summary()))
        self.console.print("[dim]用法: /log 查看路径 · /log tail 50 查看最近 50 行[/dim]")

    def allow_path(self, raw: str) -> None:
        if not raw:
            paths = self.policy.session_paths
            if not paths:
                self.console.print("[yellow]本次会话尚无额外授权文件。用法: /allow <file_or_dir>[/yellow]")
            else:
                for item in paths:
                    self.console.print(f"[dim]· 只读已授权:[/dim] {item}")
            return
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            self.console.print(f"[red]路径不存在: {path}[/red]")
            return
        decision = self.policy.allow_session_path(path)
        if not decision.allowed:
            self.console.print(f"[red]{decision.reason}[/red]")
            return
        kind = "目录（含子目录）" if path.is_dir() else "文件"
        self.console.print(f"[green]已授权本次会话只读{kind}: {path.resolve()}[/green]")
        self.console.print("[dim]仅本会话有效；敏感文件（.env / 密钥）即使是应用内文件也始终拒绝。[/dim]")

    def start_goal(self, objective: str, title: str = "Goal 已启动") -> None:
        if not self._require_api():
            return
        self.goal.start(objective)
        if self.logger is not None:
            self.logger.log("goal_start", title=title, objective=objective, max_rounds=self.goal.max_rounds)
        if self.display == "compact":
            self._status("[目标]", f"{title} · 最多 {self.goal.max_rounds} 轮", style="yellow")
        else:
            self.console.print(
                Panel(
                    Text(objective, style="white"),
                    title=f"[bold yellow]{title}[/bold yellow] · 最多 {self.goal.max_rounds} 轮",
                    border_style="yellow",
                )
            )
        outcome = self._drive_goal()
        if outcome is not None:
            self._report_goal(outcome)

    def _drive_goal(self) -> str | None:
        """驱动 Goal 循环；API/工具异常不应让 CLI 退出。"""
        try:
            with self._hotkey_window():
                outcome = run_goal(self.session, self.goal, on_round=self._on_round)
        except Exception as exc:  # noqa: BLE001 - 交互式 CLI 必须兜住
            self.end_stream()
            self.goal.active = False
            self.console.print(f"[red]Goal 执行中断: {type(exc).__name__}: {exc}[/red]")
            return None
        self.end_stream()
        return outcome

    def _on_round(self, goal: GoalState) -> None:
        if self.logger is not None:
            self.logger.log("goal_round", n=goal.current_round, max=goal.max_rounds)
        if self.display == "compact":
            self._status("[轮次]", f"{goal.current_round}/{goal.max_rounds}")
        else:
            self.console.rule(f"[dim]Goal round {goal.current_round}/{goal.max_rounds}[/dim]", style="dim")

    def _report_goal(self, outcome: str) -> None:
        summary = self.goal.last_summary or "(无总结)"
        question = self.goal.pending_question if outcome == "PAUSED" else ""
        if self.logger is not None:
            self.logger.log("goal_end", outcome=outcome, summary=summary, question=question)
        if self.display == "compact":
            label, style = {
                "COMPLETED": ("[完成] 目标已完成", "green"),
                "PAUSED": ("[等待] Goal 已挂起", "yellow"),
                "ROUND_LIMIT": ("[中断] 达到轮数上限", "red"),
                "INACTIVE": ("[空闲] 没有活跃的 Goal", "dim"),
            }.get(outcome, (f"[结束] {outcome}", "white"))
            self._status(label, style=style)
            self.console.print(Text(summary))
            if question:
                self.console.print(Text("需要你回答：" + question, style="bold"))
            return

        label = {
            "COMPLETED": ("[green]目标已完成[/green]", "green"),
            "PAUSED": ("[yellow]Goal 已挂起，等待选手回答[/yellow]", "yellow"),
            "ROUND_LIMIT": ("[red]达到轮数上限，Goal 已自动结束[/red]", "red"),
            "INACTIVE": ("[dim]没有活跃的 Goal[/dim]", "dim"),
        }.get(outcome, (outcome, "white"))
        body = Text(summary)
        if question:
            body.append("\n\n")
            body.append("需要你回答：", style="bold")
            body.append(question)
        self.console.print(Panel(body, title=label[0], border_style=label[1]))

    def hack(self, code_path: str, problem_path: str | None) -> None:
        code = Path(code_path).expanduser()
        if not code.is_absolute():
            code = Path.cwd() / code
        if not code.is_file():
            self.console.print(f"[red]代码文件不存在: {code}[/red]")
            return
        problem_note = "（未提供题面，请先向选手确认关键约束）"
        authorized: list[Path] = [code]
        if problem_path:
            problem = Path(problem_path).expanduser()
            if not problem.is_absolute():
                problem = Path.cwd() / problem
            if not problem.is_file():
                self.console.print(f"[red]题面文件不存在: {problem}[/red]")
                return
            problem_note = f"题面路径: {problem}"
            authorized.append(problem)

        # 用户在本会话显式给出的文件 -> 只读白名单（.env 等敏感文件仍会被拒绝）
        granted: list[str] = []
        for path in authorized:
            decision = self.policy.allow_session_path(path)
            if not decision.allowed:
                self.console.print(f"[red]无法授权 {path}：{decision.reason}[/red]")
                return
            granted.append(str(path.resolve()))
        self.console.print("[dim]已授权本次会话只读:[/dim] " + " , ".join(granted))

        slug = _slugify(code.stem)
        workspace = project_root() / "workspace" / slug
        objective = (
            f"[一键对拍 /hack]\n"
            f"选手代码: {code}\n"
            f"{problem_note}\n"
            f"沙箱工作目录: {workspace}\n"
            f"选手默认点拨层级: L{self.profile.hint_level}\n\n"
            "请按以下闭环推进：\n"
            "1) 读取题面与选手代码，用 1~3 句话复述题意、数据范围与输出要求；若题面缺失，先做静态审计并说明假设。\n"
            '2) 调用 load_skill("stress-test-hack") 加载对拍 SOP。\n'
            f"3) 调用 spawn_subagent 把对拍任务派发到 {workspace}：要求它写 brute.cpp + gen.py（多解/浮点题补 spj.cpp），"
            "调用 run_stress_test 捕获反例并做最小化。\n"
            "4) 读取 Subagent 战报中的 hack.in/hack.ans/hack.out，反查选手代码具体行号，"
            f"按 L{self.profile.hint_level} 层级给出点拨（默认不直接贴完整 AC 代码）。\n"
            "5) 调用 update_profile 记录本次 Bug 指纹（结合画像中的历史高频项）。\n"
            "6) 全部完成后调用 goal_complete 结束。"
        )
        self.start_goal(objective, title=f"Hack · {code.name}")

    # ---------------------------- 会话 ---------------------------- #
    def _require_api(self) -> bool:
        if has_api_key():
            return True
        self.console.print(
            Panel(
                "未检测到 [bold]OPENAI_API_KEY[/bold]，无法调用大模型。\n"
                "请复制 .env.example 为 .env 并填入 key（支持 DeepSeek / Kimi / 智谱 / OpenAI 兼容端点），"
                "或 `export OPENAI_API_KEY=sk-xxx` 后重启。\n\n"
                "离线可验证的部分：`uv run evals/run_eval.py`、`python -m unittest discover -s tests`、"
                "`/profile`、`/skills`、`/mode`。",
                title="[red]缺少 API Key[/red]",
                border_style="red",
            )
        )
        return False

    def handle_input(self, line: str) -> bool:
        """处理一行输入；返回 False 表示退出。"""
        text = line.strip()
        if not text:
            return True
        started = time.perf_counter()
        if not text.startswith("/"):
            if self.goal.active:
                if not self._require_api():
                    return True
                if self.goal.paused:
                    self.goal.resume()
                self.session.memory.append({"role": "user", "content": text})
                outcome = self._drive_goal()
                if outcome is not None:
                    self._report_goal(outcome)
            else:
                if not self._require_api():
                    return True
                try:
                    with self._hotkey_window():
                        self.session.chat(text)
                except Exception as exc:  # noqa: BLE001 - CLI 不应因 API 异常退出
                    self.console.print(f"[red]调用模型失败: {type(exc).__name__}: {exc}[/red]")
            self._finish_turn(started)
            return True

        command, _, rest = text.partition(" ")
        rest = rest.strip()
        command = command.lower()

        if command in ("/exit", "/quit", "/q"):
            return False
        if command == "/help":
            self.console.print(HELP_TEXT)
        elif command == "/profile":
            self.print_profile()
        elif command == "/skills":
            self.print_skills()
        elif command == "/mode":
            if not rest:
                self.console.print(f"[yellow]当前层级: {HINT_LEVEL_LABEL[self.profile.hint_level]}；用法 /mode L1[/yellow]")
            else:
                self.set_mode(rest)
        elif command == "/think":
            self.toggle_thinking()
        elif command == "/display":
            if not rest:
                self.console.print(Text(f"当前输出模式: {self.display} —— {DISPLAY_MODE_LABEL[self.display]}"))
                state = "展开" if self.show_thinking else "隐藏"
                self.console.print(Text(f"思考过程当前: {state}（Ctrl+O / Ctrl+Shift+O 或 /think 切换）"))
                self.console.print("[dim]用法: /display compact（简略） · /display verbose（额外展开工具细节）[/dim]")
            else:
                self.set_display(rest)
        elif command == "/log":
            self.print_log(rest)
        elif command == "/allow":
            self.allow_path(rest)
        elif command == "/clear":
            self.memory.clear()
            self.profile.reload()
            self.console.print("[green]短期会话已清空（MEMORY.md 与 PROFILE.json 保留）[/green]")
        elif command == "/goal":
            if rest in ("", "status"):
                self.console.print(Panel(Text(self.goal.status_text()), title="Goal 状态", border_style="yellow"))
            elif rest == "clear":
                self.goal.complete("")
                self.goal.text = None
                self.goal.current_round = 0
                self.console.print("[green]Goal 已清除[/green]")
            else:
                self.start_goal(rest)
        elif command == "/hack":
            parts = rest.split(maxsplit=1)
            if not parts:
                self.console.print("[yellow]用法: /hack <code_file> [problem_file][/yellow]")
            else:
                self.hack(parts[0], parts[1] if len(parts) > 1 else None)
        else:
            self.console.print(f"[red]未知命令: {command}[/red]（输入 /help 查看帮助）")
        return True

    # ---------------------------- 主循环 ---------------------------- #
    def _finish_turn(self, started: float) -> None:
        """一轮对话结束：收尾流式输出并广播耗时（终端 + 日志）。"""
        elapsed = time.perf_counter() - started
        self.end_stream()
        self.session.emit("turn_end", elapsed=round(elapsed, 3))

    def repl(self) -> None:
        self.console.print(BANNER)
        self.console.print(
            f"[dim]模型: {current_model()} | 技能: {len(list_skill_summaries(DEFAULT_SKILLS_DIR))} 个 | "
            f"记忆: {self.memory.memory_dir} | 输出: {self.display}[/dim]"
        )
        if self.memory.recovery_note:
            self.console.print(f"[yellow]⚠ {self.memory.recovery_note}[/yellow]")
        self.console.print(f"[dim]安全策略: {self.policy.describe()}[/dim]")
        if self.logger is not None:
            self.console.print(f"[dim]运行日志: {self.logger.md_path}[/dim]")
        thinking = "展开" if self.show_thinking else "隐藏"
        hotkey_note = (
            "Ctrl+O / Ctrl+Shift+O 随时切换"
            if self.hotkeys is not None and self.hotkeys.available
            else "当前非交互终端，仅可用 /think 切换"
        )
        self.console.print(f"[dim]思考过程: {thinking}（{hotkey_note}；完整内容始终写入日志）[/dim]")
        if not has_api_key():
            self.console.print("[yellow]⚠ 未配置 OPENAI_API_KEY：仅本地命令可用（/profile /skills /mode /clear）。[/yellow]")
        self.console.print("[dim]输入 /help 查看命令，/exit 退出。[/dim]\n")

        while True:
            try:
                line = input("» ")
            except (EOFError, KeyboardInterrupt):
                self._shutdown()
                return
            if not self.handle_input(line):
                self._shutdown()
                return

    def _shutdown(self) -> None:
        self.end_stream()
        if self.hotkeys is not None:
            self.hotkeys.stop()
        if self.logger is not None:
            try:
                self.logger.log("session_end", elapsed=round(time.perf_counter() - self._started, 3))
            except OSError:
                pass
        self.console.print("[dim]已退出。[/dim]")


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + " …"


def _force_utf8_stdio() -> None:
    """Windows 控制台默认 GBK，统一改成 UTF-8 避免中文/emoji 输出乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def build_runtime(**kwargs) -> Runtime:
    """供 evals / 测试复用的运行时构造函数。"""
    return Runtime.build(**kwargs)


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(prog="xcpc-claw", description="AI-Native XCPC 对拍 Hack 与个性化教练 Agent")
    parser.add_argument("--model", default=None, help="覆盖 OPENAI_MODEL")
    parser.add_argument("--base-url", default=None, help="覆盖 OPENAI_BASE_URL")
    parser.add_argument("--api-key", default=None, help="覆盖 OPENAI_API_KEY")
    parser.add_argument("--no-stream", action="store_true", help="关闭流式输出（默认流式；思考过程仍默认隐藏）")
    parser.add_argument("--memory-dir", default=None, help="记忆目录（默认 ./chat_memory）")
    parser.add_argument("--max-rounds", type=int, default=15, help="外层 /goal 最大轮数")
    parser.add_argument(
        "--display",
        choices=list(DISPLAY_MODES),
        default=None,
        help="终端输出模式：compact=状态+耗时（默认）；verbose=额外展开工具参数与结果",
    )
    parser.add_argument("--no-log", action="store_true", help="不写运行日志（默认写入 chat_memory/logs/）")
    parser.add_argument("--no-hotkeys", action="store_true", help="禁用 Ctrl+O 思考展开快捷键（仍可用 /think）")
    args = parser.parse_args(argv)

    configure(api_key=args.api_key, base_url=args.base_url, model=args.model)
    console = Console()
    try:
        runtime = Runtime.build(
            console=console,
            model=args.model,
            stream=not args.no_stream,
            memory_dir=args.memory_dir,
            max_rounds=args.max_rounds,
            display=args.display,
            enable_log=not args.no_log,
            enable_hotkeys=not args.no_hotkeys,
        )
    except Exception as exc:  # noqa: BLE001 - 启动期失败给出可读提示
        console.print(f"[red]启动失败: {type(exc).__name__}: {exc}[/red]")
        return 1
    try:
        runtime.repl()
    except KeyboardInterrupt:
        console.print("\n[dim]已退出。[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
