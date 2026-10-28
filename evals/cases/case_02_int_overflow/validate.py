"""case_02 输入合法性校验：n 个数恰好，|a_i| <= 1e9。"""

import sys
from pathlib import Path


def invalid() -> None:
    sys.exit(1)


raw = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").split()
try:
    tok = [int(x) for x in raw]
except ValueError:
    invalid()
if not tok:
    invalid()

n = tok[0]
if not 1 <= n <= 1000:  # 题面上限 2e5，这里只要求生成器口径内合法
    invalid()
if len(tok) != n + 1:
    invalid()
for value in tok[1:]:
    if abs(value) > 10**9:
        invalid()
sys.exit(0)
