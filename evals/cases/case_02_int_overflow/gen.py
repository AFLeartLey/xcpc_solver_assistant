"""case_02 数据生成器：先随机小数据，再按 seed 分阶段拉满 1e9 边界值。

约定：python gen.py <seed>，把测试数据打印到 stdout。
"""

import random
import sys

seed = int(sys.argv[1])
random.seed(seed)

LIMIT = 10**9
mode = seed % 4

if mode == 0:
    # 对照：极小规模，不会溢出（用来确认小数据已通过）
    n = random.randint(1, 2)
    values = [random.randint(-10, 10) for _ in range(n)]
elif mode == 1:
    # 全正极值：3 * 1e9 > 2^31
    n = random.randint(3, 8)
    values = [LIMIT] * n
elif mode == 2:
    # 全负极值
    n = random.randint(3, 8)
    values = [-LIMIT] * n
else:
    # 混合极值：保证 |sum| 大概率超过 2^31
    n = random.randint(3, 8)
    pool = [LIMIT, -LIMIT, LIMIT - random.randint(0, 5), random.randint(-LIMIT, LIMIT)]
    values = [random.choice(pool) for _ in range(n)]

print(n)
print(*values)
