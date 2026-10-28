"""case_03 输入合法性校验：n/W 范围与恰好 n 组 (w, v)，权值 1..20。"""

import sys
from pathlib import Path


def invalid() -> None:
    sys.exit(1)


raw = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").split()
try:
    tok = [int(x) for x in raw]
except ValueError:
    invalid()
if len(tok) < 2:
    invalid()

n, W = tok[0], tok[1]
if not 1 <= n <= 6:
    invalid()
if not 1 <= W <= 20:
    invalid()
if len(tok) != 2 + 2 * n:
    invalid()
for k in range(n):
    w, v = tok[2 + 2 * k], tok[3 + 2 * k]
    if not 1 <= w <= 20 or not 1 <= v <= 20:
        invalid()
sys.exit(0)
