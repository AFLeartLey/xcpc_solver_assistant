"""case_01 输入合法性校验（供 delta_reduce.py --validator 使用）。

退出码 0 = 输入完全符合题面约束（T/ n 范围、权值范围、恰好 n-1 条合法树边、无环且连通）。
"""

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

pos = 0
T = tok[pos]
pos += 1
if not 1 <= T <= 5:
    invalid()

for _ in range(T):
    if pos >= len(tok):
        invalid()
    n = tok[pos]
    pos += 1
    if not 1 <= n <= 8:
        invalid()
    if pos + n > len(tok):
        invalid()
    for k in range(n):
        if not -10 <= tok[pos + k] <= 10:
            invalid()
    pos += n

    parent = list(range(n + 1))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _ in range(n - 1):
        if pos + 1 >= len(tok):
            invalid()
        u, v = tok[pos], tok[pos + 1]
        pos += 2
        if not (1 <= u <= n and 1 <= v <= n) or u == v:
            invalid()
        ru, rv = find(u), find(v)
        if ru == rv:
            invalid()  # 成环 -> 不是树
        parent[ru] = rv
    if n > 1 and len({find(i) for i in range(1, n + 1)}) != 1:
        invalid()  # 非连通

if pos != len(tok):
    invalid()
sys.exit(0)
