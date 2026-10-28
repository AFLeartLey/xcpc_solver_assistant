"""case_03 数据生成器：小规模 0/1 背包，混入"性价比贪心必翻车"的经典权值组合。

约定：python gen.py <seed>，把测试数据打印到 stdout。
"""

import random
import sys

seed = int(sys.argv[1])
random.seed(seed)

# 每 3 组插一个经典反例（贪心取 6/11，正解取 5/9 + 5/9）
if seed % 3 == 0:
    W = 10
    items = [(6, 11), (5, 9), (5, 9)]
else:
    n = random.randint(2, 6)
    W = random.randint(3, 12)
    items = [(random.randint(1, W), random.randint(1, 20)) for _ in range(n)]

print(len(items), W)
for w, v in items:
    print(w, v)
