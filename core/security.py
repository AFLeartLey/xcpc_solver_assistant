"""访问安全策略（Security Policy）。

三条硬约束（可在 CLI 层用 ``/allow`` 扩权，但写权限永不放宽）：

1. **读**：只允许读应用内"工作相关"目录（``tools/skills`` / ``chat_memory`` / ``workspace``），
   或用户在**本次会话**中显式提供的文件（``/hack`` 的代码与题面、``/allow`` 的路径）。
2. **敏感文件**：``.env`` / ``*.pem`` / ``*.key`` / ``id_rsa`` / ``credentials`` 等一律拒绝，
   即使位于应用目录内部、即使被加入会话白名单也一样拒绝。
3. **写**：只能写 ``workspace/<problem_slug>/...``，不允许写到应用源码、记忆目录或工作区根目录。

说明：``bash`` 只能做静态命令审查 + 工作目录限定 + 子进程环境变量脱敏，
它**不是**操作系统级沙箱；真正的强隔离需要 Docker/Job Object（见 README 已知限制）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# 目录级黑名单：版本库、虚拟环境、云凭证目录、缓存
BLOCKED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".ssh",
    ".aws",
    ".gnupg",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".uv-cache",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
}

SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx"}

# 精确文件名（大小写不敏感）
SENSITIVE_EXACT = {
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "known_hosts",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
    "secret.json",
    ".htpasswd",
}

# 仅在"工作区之外"启用的宽泛命名规则（避免误伤 workspace 里的 normal 题面文件）
STRICT_NAME_RE = re.compile(
    r"(?i)(?:^|[._-])(api[_-]?key|apikey|access[_-]?key|private[_-]?key|secret|secrets|"
    r"credential|credentials|password|passwd|token)(?:[._-]|$)"
)

ENV_FILE_RE = re.compile(r"(?i)^\.env(?:\.[A-Za-z0-9_-]+)?$")

# ---- bash 静态审查 -------------------------------------------------------- #
COMMAND_DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(^|[^A-Za-z0-9_])\.env(\.[A-Za-z0-9_-]+)?($|[^A-Za-z0-9_])"), "命令涉及 .env（可能含 LLM 密钥）"),
    (re.compile(r"(?i)\bid_(rsa|dsa|ecdsa|ed25519)\b"), "命令涉及 SSH 私钥"),
    (re.compile(r"(?i)(\.ssh|\.aws|\.gnupg|\.git[\\/])"), "命令涉及密钥目录 / 版本库目录"),
    (re.compile(r"(?i)(credentials|\.pem\b|\.p12\b|\.pfx\b|\.keystore\b)"), "命令涉及凭证或证书文件"),
    (re.compile(r"(?i)(os\.environ|getenv\s*\(|printenv|\$env:|environ\[)"), "命令试图读取进程环境变量"),
    (re.compile(r"(?i)(api[_-]?key|access[_-]?key|private[_-]?key|openai_api_key|anthropic_api_key)"), "命令涉及密钥名称"),
    (
        re.compile(r"(?i)\b(curl|wget|nc|netcat|ssh|scp|sftp|telnet|ftp|invoke-webrequest|iwr|start-bitstransfer)\b"),
        "禁止联网 / 远程传输命令",
    ),
    (
        re.compile(r"(?i)\b(reg\s+(add|delete|query)|setx|schtasks|netsh|wmic|bcdedit|shutdown|diskpart)\b"),
        "禁止修改系统配置 / 磁盘",
    ),
    (re.compile(r"(?i)(\bmklink\b|\bln\s+-s\b)"), "禁止创建符号链接（可能绕过工作区限制）"),
]

WIN_ABS_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'|<>;&]*")
UNC_RE = re.compile(r"\\\\[^\s\"'|<>;&]+")
POSIX_ABS_RE = re.compile(r"(?<![\w:/])/(?!/)[^\s\"'|<>;&]+")
DEV_NULL_ALIASES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/zero", "nul"}
REDIRECT_RE = re.compile(r"(?<!\d)>>?\s*([^\s|;&<>]+)")
# 常见"会落盘"的命令：其路径参数必须满足写规则（防止 bash 绕过 workspace/<slug>/ 限制）
WRITE_COMMAND_RE = re.compile(
    r"(?i)^\s*(mkdir|md|copy|copy-item|xcopy|move|move-item|ren|rename|del|erase|rmdir|rd|"
    r"touch|tee|new-item|set-content|add-content|out-file|remove-item|truncate)\b"
)

# 子进程环境变量脱敏：任何 *_API_KEY / *_TOKEN / OPENAI_* 等都不下发
ENV_PREFIX_DENY = ("OPENAI_", "ANTHROPIC_", "DEEPSEEK_", "MOONSHOT_", "ZHIPU_", "GLM_", "KIMI_", "DASHSCOPE_")
ENV_SUBSTR_DENY = ("API_KEY", "APIKEY", "ACCESS_KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Decision(True)


def _norm(path: Path) -> Path:
    return Path(os.path.normcase(str(path)))


def _within(path: Path, root: Path) -> bool:
    """大小写不敏感（Windows）的包含判断。"""
    try:
        _norm(path).relative_to(_norm(root))
        return True
    except ValueError:
        return False


class SecurityPolicy:
    """读白名单 + 敏感文件黑名单 + 写工作区约束。"""

    def __init__(
        self,
        app_root: str | Path,
        *,
        workspace_dir_name: str = "workspace",
        read_roots: Iterable[str | Path] | None = None,
        session_paths: Iterable[str | Path] = (),
    ):
        self.app_root = Path(app_root).expanduser().resolve()
        self.workspace_root = (self.app_root / workspace_dir_name).resolve()
        if read_roots is None:
            read_roots = [
                self.app_root / "tools" / "skills",
                self.app_root / "chat_memory",
                self.workspace_root,
            ]
        self.read_roots = [Path(p).expanduser().resolve() for p in read_roots]
        self._session_paths: list[Path] = []
        for path in session_paths:
            self.allow_session_path(path)

    # ---------------------------- 会话授权 ---------------------------- #
    def allow_session_path(self, path: str | Path, base_dir: str | Path | None = None) -> Decision:
        """把用户在本次会话中给出的路径加入只读白名单（目录则整棵子树）。"""
        resolved = self.resolve(path, base_dir)
        reason = self._sensitive_reason(resolved)
        if reason:
            return Decision(False, f"拒绝授权：{reason}")
        if not any(_norm(p) == _norm(resolved) for p in self._session_paths):
            self._session_paths.append(resolved)
        return ALLOW

    @property
    def session_paths(self) -> list[Path]:
        return list(self._session_paths)

    # ---------------------------- 路径解析 ---------------------------- #
    def resolve(self, path: str | Path, base_dir: str | Path | None = None) -> Path:
        candidate = Path(str(path)).expanduser()
        if not candidate.is_absolute():
            candidate = (Path(base_dir) if base_dir else Path.cwd()) / candidate
        return candidate.resolve()

    def is_workspace_dir(self, path: str | Path) -> bool:
        resolved = self.resolve(path)
        return _within(resolved, self.workspace_root)

    # ---------------------------- 判定 ---------------------------- #
    def _sensitive_reason(self, path: Path) -> str | None:
        for part in path.parts:
            if part.lower() in BLOCKED_DIRS:
                return f"路径包含受保护目录 '{part}'（版本库/虚拟环境/云凭证）"

        name = path.name.lower()
        if ENV_FILE_RE.match(name) or name.endswith(".env"):
            return "禁止访问 .env / 环境变量配置文件（可能包含 LLM 密钥）"
        if name in SENSITIVE_EXACT:
            return f"禁止访问敏感文件名 '{path.name}'"
        if path.suffix.lower() in SENSITIVE_SUFFIXES:
            return f"禁止访问密钥/证书文件（{path.suffix}）"

        # 工作区之外的宽泛命名检查，降低误伤题面文件（如 secret.txt）的概率
        if not _within(path, self.workspace_root) and STRICT_NAME_RE.search(name):
            return f"禁止访问：文件名包含密钥/凭证特征（{path.name}）"
        return None

    def check_read(self, path: str | Path, base_dir: str | Path | None = None) -> Decision:
        target = self.resolve(path, base_dir)
        reason = self._sensitive_reason(target)
        if reason:
            return Decision(False, reason)

        # 会话授权优先：即使用户给的文件在应用内、但不在常规可读目录（如 evals/），也应放行
        if any(_within(target, allowed) for allowed in self._session_paths):
            return ALLOW

        if _within(target, self.app_root):
            if any(_within(target, root) for root in self.read_roots):
                return ALLOW
            return Decision(
                False,
                f"应用内该路径不在可读范围（仅允许 tools/skills、chat_memory、workspace）：{target}",
            )

        return Decision(
            False,
            f"应用目录之外且不是本会话用户提供的文件：{target}。"
            "如确需读取，请让用户执行 /allow <path> 授权，或把文件放进 workspace/<problem_slug>/。",
        )

    def check_write(self, path: str | Path, base_dir: str | Path | None = None) -> Decision:
        target = self.resolve(path, base_dir)
        reason = self._sensitive_reason(target)
        if reason:
            return Decision(False, reason)

        if not _within(target, self.workspace_root):
            return Decision(False, f"写操作只允许落在工作区 {self.workspace_root}/<problem_slug>/ 内：{target}")

        relative = target.relative_to(self.workspace_root)
        parts = relative.parts
        if len(parts) < 2:
            return Decision(False, f"写操作必须位于 workspace/<problem_slug>/ 下（不能直接写工作区根目录）：{target}")
        slug = parts[0]
        if slug.startswith(".") or slug in {"..", "."}:
            return Decision(False, f"非法的 problem_slug '{slug}'")
        return ALLOW

    def check_workspace_dir(self, path: str | Path, base_dir: str | Path | None = None) -> Decision:
        target = self.resolve(path, base_dir)
        if not _within(target, self.workspace_root):
            return Decision(False, f"沙箱目录必须位于 {self.workspace_root} 内：{target}")
        relative = target.relative_to(self.workspace_root)
        parts = relative.parts
        if len(parts) < 1 or parts == (".",):
            return Decision(False, f"沙箱目录不能是工作区根目录：{target}")
        if any(part.startswith(".") for part in parts):
            return Decision(False, f"沙箱目录名不能以 '.' 开头：{target}")
        return ALLOW

    def check_command(self, command: str, cwd: str | Path) -> Decision:
        """对 bash 命令做静态审查（命令字符串 + 绝对路径引用）。"""
        text = command or ""
        if not text.strip():
            return Decision(False, "空命令")

        for pattern, reason in COMMAND_DENY_PATTERNS:
            if pattern.search(text):
                return Decision(False, reason)

        if re.search(r"(^|[\s\\/])\.\.([\s\\/]|$)", text):
            return Decision(False, "命令包含 '..' 路径穿越，已拒绝")

        for match in UNC_RE.findall(text):
            return Decision(False, f"禁止访问网络共享路径：{match}")

        candidates = WIN_ABS_RE.findall(text)
        posix = POSIX_ABS_RE.findall(text)
        if os.name == "nt":
            # Windows 上 "/b"、"/Fe" 这类是命令开关而不是路径；只有形如 /a/b 的才当路径
            posix = [token for token in posix if token.count("/") >= 2]
        for raw in candidates + posix:
            token = raw.strip().strip("\"'").rstrip("),;")
            if not token:
                continue
            if token.replace("\\", "/").lower() in DEV_NULL_ALIASES:
                continue
            if token.startswith("//"):
                continue
            decision = self.check_read(token, base_dir=cwd)
            if not decision.allowed:
                return Decision(False, f"命令引用了不允许的路径 {token}：{decision.reason}")

        # 重定向目标：> out.txt / >> log.txt
        for raw in REDIRECT_RE.findall(text):
            target = raw.strip().strip("\"'")
            if not target or target.startswith("&"):
                continue
            if target.replace("\\", "/").lower() in DEV_NULL_ALIASES:
                continue
            decision = self.check_write(target, base_dir=cwd)
            if not decision.allowed:
                return Decision(False, f"重定向写入目标不在工作区内（{target}）：{decision.reason}")

        # 会落盘的命令：所有路径参数都按写规则检查
        if WRITE_COMMAND_RE.match(text):
            for token in re.findall(r"[^\s|;&<>]+", text):
                if not token or token.startswith(("-", "/")) or token.lower() == ">":
                    continue
                if WRITE_COMMAND_RE.match(token.lower()):
                    continue
                target = token.strip("\"'")
                if not target or target.startswith("&"):
                    continue
                decision = self.check_write(target, base_dir=cwd)
                if not decision.allowed:
                    return Decision(False, f"命令的写入目标不在工作区内（{target}）：{decision.reason}")
        return ALLOW

    # ---------------------------- 子进程环境 ---------------------------- #
    @staticmethod
    def sanitized_env(extra: dict[str, str] | None = None) -> dict[str, str]:
        """剥离密钥类环境变量，避免被测程序 / 生成器读到 LLM API Key。"""
        clean: dict[str, str] = {}
        for key, value in os.environ.items():
            upper = key.upper()
            if upper.startswith(ENV_PREFIX_DENY):
                continue
            if any(marker in upper for marker in ENV_SUBSTR_DENY):
                continue
            clean[key] = value
        if extra:
            clean.update(extra)
        return clean

    # ---------------------------- 展示 ---------------------------- #
    def describe(self) -> str:
        roots = "、".join(_display(p, self.app_root) for p in self.read_roots)
        return (
            f"读: {roots} + 本会话授权文件 | "
            f"写: {_display(self.workspace_root, self.app_root)}/<problem_slug>/ | "
            "敏感文件(.env/密钥)始终拒绝"
        )


def _display(path: Path, app_root: Path) -> str:
    try:
        return str(path.relative_to(app_root)).replace("\\", "/") or "."
    except ValueError:
        return str(path)
