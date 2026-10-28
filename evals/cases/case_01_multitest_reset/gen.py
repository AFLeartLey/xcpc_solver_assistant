"""case_01 数据生成器：多组树数据，且倾向让后一组规模不小于前一组（专攻多测残留）。

约定：python gen.py <seed>，把测试数据打印到 stdout。
"""

import random
import sys

seed = int(sys.argv[1])
random.seed(seed)

T = 2 if seed % 4 else 3
prev_n = 2
out = [str(T)]

for t in range(T):
    if t == 0:
        n = random.randint(2, 4)
    else:
        if random.random() < 0.75:
            n = min(7, prev_n + random.randint(0, 2))  # 规模不减，残留边才可能被 DFS 走到
        else:
            n = random.randint(2, 6)
    prev_n = n

    values = [random.randint(-10, 10) for _ in range(n)]
    shape = random.randint(0, 3)
    edges = []
    for i in range(2, n + 1):
        if shape == 0:
            parent = random.randint(1, i - 1)          # 随机树
        elif shape == 1:
            parent = i - 1                             # 退化链
        elif shape == 2:
            parent = 1                                 # 菊花图
        else:
            parent = i // 2                            # 满二叉树
        edges.append((parent, i))

    random.shuffle(values)
    out.append(str(n))
    out.append(" ".join(map(str, values)))
    for u, v in edges:
        if random.random() < 0.5:
            u, v = v, u
        out.append(f"{u} {v}")

print("\n".join(out))
