"""渐进式 Skill 加载器：Catalog 轻索引 -> 按需加载全文 -> 本地脚本路径。

阶段一：``get_skill_catalog`` 只注入 name + 一句话 description（每个约 40 tokens）。
阶段二：模型调用 ``load_skill(name)`` 时才读取 Markdown 正文。
阶段三：正文里附带 ``scripts/`` 下脚本的绝对路径，模型可直接 ``bash`` 调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
_CACHE: dict[str, tuple[tuple[Any, ...], list["Skill"]]] = {}


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str = ""
    scripts: list[Path] = field(default_factory=list)

    @property
    def directory(self) -> Path:
        return self.path.parent


def default_skills_dir() -> Path:
    return Path(__file__).resolve().parent / "skills"


def _parse(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8", errors="replace")
    meta: dict[str, Any] = {}
    body = text
    match = FRONTMATTER_RE.match(text)
    if match:
        try:
            loaded = yaml.safe_load(match.group(1)) or {}
            if isinstance(loaded, dict):
                meta = loaded
        except yaml.YAMLError:
            meta = {}
        body = match.group(2)
    name = str(meta.get("name") or path.parent.name).strip()
    description = " ".join(str(meta.get("description") or "").split())
    if not description:
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                description = line[:160]
                break
    scripts: list[Path] = []
    scripts_dir = path.parent / "scripts"
    if scripts_dir.is_dir():
        scripts = sorted(p for p in scripts_dir.rglob("*") if p.is_file())
    return Skill(name=name, description=description, path=path, body=body.strip(), scripts=scripts)


def scan_skills(skills_dir: str | Path | None = None) -> list[Skill]:
    directory = Path(skills_dir) if skills_dir else default_skills_dir()
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*/SKILL.md"))
    signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files if p.is_file())
    key = str(directory.resolve())
    cached = _CACHE.get(key)
    if cached and cached[0] == signature:
        return cached[1]
    skills = [_parse(path) for path in files if path.is_file()]
    _CACHE[key] = (signature, skills)
    return skills


def get_skill_catalog(skills_dir: str | Path | None = None) -> str:
    """生成注入 System Prompt 的 ``<available_skills>`` 段。"""
    skills = scan_skills(skills_dir)
    if not skills:
        return "<available_skills>\n(当前未安装任何 Skill)\n</available_skills>"
    lines = ["<available_skills>"]
    for skill in skills:
        lines.append(f"- `{skill.name}`: {skill.description}")
    lines.append("</available_skills>")
    return "\n".join(lines)


def find_skill(name: str, skills_dir: str | Path | None = None) -> Skill | None:
    target = (name or "").strip().strip("`")
    for skill in scan_skills(skills_dir):
        if skill.name == target or skill.directory.name == target:
            return skill
    return None


def load_skill_content(name: str, skills_dir: str | Path | None = None) -> str:
    """返回 SKILL.md 正文 + 该 Skill 附带的本地脚本绝对路径。"""
    skill = find_skill(name, skills_dir)
    if skill is None:
        available = ", ".join(s.name for s in scan_skills(skills_dir)) or "(空)"
        return f"ERROR: 未找到 skill '{name}'。可用 skills: {available}"
    parts = [skill.body]
    if skill.scripts:
        parts.append("---\n本地辅助脚本（可直接用 bash 调用绝对路径）:")
        parts.extend(f"- {script}" for script in skill.scripts)
    else:
        parts.append("---\n（该 Skill 未附带本地脚本）")
    return "\n\n".join(parts)


def list_skill_summaries(skills_dir: str | Path | None = None) -> list[dict[str, Any]]:
    return [
        {
            "name": skill.name,
            "description": skill.description,
            "path": str(skill.path),
            "scripts": [str(s) for s in skill.scripts],
        }
        for skill in scan_skills(skills_dir)
    ]
