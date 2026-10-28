"""XCPC 选手画像：`PROFILE.json` 的读写、Bug 指纹统计与 Prompt 注入。

画像不是静态档案，而是**反哺 Agent 行为的控制面**：
高频 Bug 指纹 -> 造数据偏置；掌握度 -> 讲解深度；偏好 -> 点拨层级。
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

from .memory import default_memory_dir

DEFAULT_PROFILE: dict[str, Any] = {
    "contestant_id": "default_player",
    "preferences": {
        "language": "C++20",
        "coaching_mode": "socratic",
        "default_hint_level": 2,
        "display_mode": "compact",
        "show_thinking": False,
    },
    "topic_mastery": {
        "greedy": {"level": "medium", "score": 60, "notes": "容易直觉贪心漏掉反例"},
        "dp": {"level": "medium", "score": 65, "notes": "边界初始化偶发错误"},
        "graph": {"level": "strong", "score": 75, "notes": "基础图论扎实，需注意非连通图"},
        "data_structures": {"level": "medium", "score": 60, "notes": "线段树懒标记下传顺序"},
    },
    "bug_fingerprints": [
        {"id": "BUG_INT_OVERFLOW", "name": "32位整数溢出 (漏开 long long / 1LL)", "count": 0, "examples": []},
        {"id": "BUG_MULTITEST_DIRTY", "name": "多组测试数据未彻底清空全局数组/邻接表", "count": 0, "examples": []},
        {"id": "BUG_BOUNDARY_N1", "name": "极小边界 (N=1, K=0, 空集) 越界或特判错误", "count": 0, "examples": []},
        {"id": "BUG_GREEDY_FALSE", "name": "局部最优贪心假做法", "count": 0, "examples": []},
    ],
    "solved_or_hacked_history": [],
}

# 常见 Bug 指纹库：`update_profile` 传入未知 bug_id 时据此补全名称/分类
BUG_LIBRARY: dict[str, dict[str, str]] = {
    "BUG_INT_OVERFLOW": {"name": "32位整数溢出 (漏开 long long / 1LL)", "category": "Arithmetic"},
    "BUG_MULTITEST_DIRTY": {"name": "多组测试数据未彻底清空全局数组/邻接表", "category": "Initialization"},
    "BUG_BOUNDARY_N1": {"name": "极小边界 (N=1, K=0, 空集) 越界或特判错误", "category": "Boundary"},
    "BUG_GREEDY_FALSE": {"name": "局部最优贪心假做法", "category": "Algorithmic Thinking"},
    "BUG_ARRAY_OVERFLOW": {"name": "数组下标越界 / 开小 (N 或 N+1 边界)", "category": "Boundary"},
    "BUG_DFS_STACK": {"name": "深递归爆栈 / 退化为链未改迭代", "category": "Implementation"},
    "BUG_DISCONNECTED": {"name": "非连通图/森林只从一个点开始遍历", "category": "Graph"},
    "BUG_MOD_NEGATIVE": {"name": "取模后出现负数未 +MOD", "category": "Arithmetic"},
    "BUG_FLOAT_EPS": {"name": "浮点比较未设 eps / 精度丢失", "category": "Precision"},
    "BUG_LAZY_PUSHDOWN": {"name": "线段树懒标记下传顺序或范围错误", "category": "Data Structure"},
}

HINT_LEVEL_LABEL = {
    1: "L1 纯思维点拨（只指出数据形态）",
    2: "L2 反例数据点拨（展示 hack.in/ans/out）",
    3: "L3 精确行号 + 变量快照",
    4: "L4 完整修复补丁 + 重拍验证",
}

DISPLAY_MODES = ("compact", "verbose")
DISPLAY_MODE_LABEL = {
    "compact": "简略模式（状态+耗时+最终答复；思考默认隐藏）",
    "verbose": "详细模式（额外展开工具参数/结果/用量；思考仍由 Ctrl+O 控制）",
}


def _level_of(score: int) -> str:
    if score >= 75:
        return "strong"
    if score >= 60:
        return "medium"
    return "weak"


class Profile:
    """`chat_memory/PROFILE.json` 的读写与统计。"""

    def __init__(self, memory_dir: str | Path | None = None):
        self.memory_dir = Path(memory_dir) if memory_dir else default_memory_dir()
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.memory_dir / "PROFILE.json"
        self.data: dict[str, Any] = self._load()

    # ---------------------------- 持久化 ---------------------------- #
    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            data = copy.deepcopy(DEFAULT_PROFILE)
            data["solved_or_hacked_history"] = []
            self._write(data)
            return data
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("PROFILE.json 根节点必须是对象")
        except (json.JSONDecodeError, ValueError, OSError):
            backup = self.path.with_suffix(".json.bak")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            data = copy.deepcopy(DEFAULT_PROFILE)
            self._write(data)
            return data
        return self._migrate(data)

    def _migrate(self, data: dict[str, Any]) -> dict[str, Any]:
        changed = False
        for key, value in DEFAULT_PROFILE.items():
            if key not in data:
                data[key] = copy.deepcopy(value)
                changed = True
        data.setdefault("preferences", {})
        for key, value in DEFAULT_PROFILE["preferences"].items():
            if key not in data["preferences"]:
                data["preferences"][key] = value
                changed = True
        if not isinstance(data.get("bug_fingerprints"), list):
            data["bug_fingerprints"] = []
            changed = True
        if not isinstance(data.get("solved_or_hacked_history"), list):
            data["solved_or_hacked_history"] = []
            changed = True
        if not isinstance(data.get("topic_mastery"), dict):
            data["topic_mastery"] = {}
            changed = True
        if changed:
            self._write(data)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def save(self) -> None:
        self._write(self.data)

    def reload(self) -> dict[str, Any]:
        self.data = self._load()
        return self.data

    # ---------------------------- 偏好 ---------------------------- #
    @property
    def hint_level(self) -> int:
        try:
            level = int(self.data.get("preferences", {}).get("default_hint_level", 2))
        except (TypeError, ValueError):
            level = 2
        return min(4, max(1, level))

    def set_hint_level(self, level: int | str) -> int:
        raw = str(level).strip().upper()
        if raw.startswith("L"):
            raw = raw[1:]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"非法点拨层级: {level!r}（合法值 L1~L4）") from None
        if not 1 <= value <= 4:
            raise ValueError(f"非法点拨层级: {level!r}（合法值 L1~L4）")
        self.data.setdefault("preferences", {})["default_hint_level"] = value
        self.save()
        return value

    # ---------------------------- 展示模式 ---------------------------- #
    @property
    def display_mode(self) -> str:
        raw = str(self.data.get("preferences", {}).get("display_mode", "compact")).strip().lower()
        return raw if raw in DISPLAY_MODES else "compact"

    def set_display_mode(self, mode: str) -> str:
        value = str(mode).strip().lower()
        aliases = {"quiet": "compact", "brief": "compact", "simple": "compact", "minimal": "compact",
                   "full": "verbose", "detail": "verbose", "debug": "verbose", "expand": "verbose"}
        value = aliases.get(value, value)
        if value not in DISPLAY_MODES:
            raise ValueError(f"非法展示模式: {mode!r}（合法值 compact / verbose）")
        self.data.setdefault("preferences", {})["display_mode"] = value
        self.save()
        return value

    # ---------------------------- 思考过程展示 ---------------------------- #
    @property
    def show_thinking(self) -> bool:
        return bool(self.data.get("preferences", {}).get("show_thinking", False))

    def set_show_thinking(self, visible: bool) -> bool:
        self.data.setdefault("preferences", {})["show_thinking"] = bool(visible)
        self.save()
        return bool(visible)

    # ---------------------------- Bug 指纹 ---------------------------- #
    def _find_bug(self, bug_id: str) -> dict[str, Any] | None:
        for item in self.data.get("bug_fingerprints", []):
            if item.get("id") == bug_id:
                return item
        return None

    def record_count(self, bug_id: str) -> int:
        """当前 Bug 指纹的累计命中次数（不存在则为 0），供评测对比前后差异。"""
        entry = self._find_bug(bug_id)
        return int(entry.get("count") or 0) if entry else 0

    def record_bug_occurrence(
        self,
        bug_id: str,
        problem_name: str,
        detail: str,
        topic: str = "",
        mastery_delta: int | None = None,
        solution_status: str = "hacked",
    ) -> dict[str, Any]:
        """累加 Bug 指纹、追加案例、微调专题掌握度，并写入错题历史。"""
        bug_id = (bug_id or "BUG_UNKNOWN").strip() or "BUG_UNKNOWN"
        entry = self._find_bug(bug_id)
        if entry is None:
            meta = BUG_LIBRARY.get(bug_id, {"name": bug_id, "category": "Unclassified"})
            entry = {"id": bug_id, "name": meta["name"], "count": 0, "examples": []}
            self.data.setdefault("bug_fingerprints", []).append(entry)
        entry["count"] = int(entry.get("count") or 0) + 1
        examples = entry.setdefault("examples", [])
        examples.append(
            {
                "problem": problem_name or "unknown",
                "detail": (detail or "")[:400],
                "date": time.strftime("%Y-%m-%d"),
            }
        )
        del examples[:-8]  # 只保留最近 8 条，避免画像无限膨胀

        topic = (topic or "").strip()
        if topic:
            mastery = self.data.setdefault("topic_mastery", {}).setdefault(
                topic, {"level": "medium", "score": 65, "notes": ""}
            )
            delta = -3 if mastery_delta is None else int(mastery_delta)
            if delta:
                score = min(100, max(0, int(mastery.get("score", 60)) + delta))
                mastery["score"] = score
                mastery["level"] = _level_of(score)
            note = f"{time.strftime('%Y-%m-%d')} {problem_name} 命中 {bug_id}"
            mastery["notes"] = (str(mastery.get("notes") or "").strip() + "；" + note).strip("；")[-240:]

        self.data.setdefault("solved_or_hacked_history", []).append(
            {
                "problem": problem_name or "unknown",
                "bug_id": bug_id,
                "topic": topic,
                "status": solution_status,
                "detail": (detail or "")[:400],
                "date": time.strftime("%Y-%m-%d %H:%M"),
            }
        )
        del self.data["solved_or_hacked_history"][:-200]
        self.save()
        return entry

    # ---------------------------- Prompt 注入 ---------------------------- #
    def top_bugs(self, limit: int = 3) -> list[dict[str, Any]]:
        bugs = [b for b in self.data.get("bug_fingerprints", []) if int(b.get("count") or 0) > 0]
        return sorted(bugs, key=lambda b: (-int(b.get("count") or 0), b.get("id", "")))[:limit]

    def weak_topics(self, limit: int = 3) -> list[tuple[str, dict[str, Any]]]:
        items = [(name, meta) for name, meta in self.data.get("topic_mastery", {}).items() if isinstance(meta, dict)]
        return sorted(items, key=lambda kv: int(kv[1].get("score", 0)))[:limit]

    def strong_topics(self, limit: int = 2) -> list[tuple[str, dict[str, Any]]]:
        items = [(name, meta) for name, meta in self.data.get("topic_mastery", {}).items() if isinstance(meta, dict)]
        return sorted(items, key=lambda kv: -int(kv[1].get("score", 0)))[:limit]

    def get_profile_prompt_snippet(self) -> str:
        """紧凑的 <contestant_profile> 文本块（约 150 tokens），注入 System Prompt。"""
        prefs = self.data.get("preferences", {})
        lines = [
            "<contestant_profile>",
            f"- 选手ID: {self.data.get('contestant_id', 'default_player')} | "
            f"语言: {prefs.get('language', 'C++20')} | 讲解模式: {prefs.get('coaching_mode', 'socratic')} | "
            f"默认点拨: {HINT_LEVEL_LABEL[self.hint_level]}",
        ]
        weak = self.weak_topics()
        if weak:
            lines.append("- 薄弱专题（优先引导）: " + ", ".join(f"{name}({meta.get('score', 0)})" for name, meta in weak))
        strong = self.strong_topics()
        if strong:
            lines.append("- 较强专题（可跳过基础概念）: " + ", ".join(f"{name}({meta.get('score', 0)})" for name, meta in strong))
        bugs = self.top_bugs()
        if bugs:
            lines.append("- 高频 Bug 指纹 Top3（造数据/查错时优先攻击）:")
            for rank, bug in enumerate(bugs, 1):
                lines.append(f"  {rank}. {bug.get('id')} {bug.get('name')} ×{bug.get('count')}")
        else:
            lines.append("- 高频 Bug 指纹: 暂无记录（首次训练，先按通用边界策略造数据）")
        history = self.data.get("solved_or_hacked_history", [])[-3:]
        if history:
            lines.append("- 最近训练记录:")
            for item in history:
                lines.append(f"  · {item.get('date', '')} {item.get('problem')} -> {item.get('bug_id')} ({item.get('status')})")
        lines.append("- 硬性要求: 默认不直接贴出完整 AC 代码；优先反例驱动 + 结合上述 Bug 指纹提问。")
        lines.append("</contestant_profile>")
        return "\n".join(lines)

    # ---------------------------- 展示 ---------------------------- #
    def topic_rows(self) -> list[dict[str, Any]]:
        rows = []
        for name, meta in self.data.get("topic_mastery", {}).items():
            if not isinstance(meta, dict):
                continue
            score = int(meta.get("score", 0))
            rows.append({"topic": name, "score": score, "level": meta.get("level") or _level_of(score), "notes": meta.get("notes", "")})
        return sorted(rows, key=lambda r: r["score"])

    def bug_rows(self) -> list[dict[str, Any]]:
        rows = []
        for bug in self.data.get("bug_fingerprints", []):
            rows.append(
                {
                    "id": bug.get("id", "?"),
                    "name": bug.get("name", ""),
                    "count": int(bug.get("count") or 0),
                    "last": (bug.get("examples") or [{}])[-1].get("problem", ""),
                }
            )
        return sorted(rows, key=lambda r: (-r["count"], r["id"]))

    def to_json(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=2)
