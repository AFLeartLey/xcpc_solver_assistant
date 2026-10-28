---
name: graph-tree-gen
description: 构造图论与树论特种测试数据（菊花图、退化链、森林、连通图、DAG、重边边界）。
---

# 图论 / 树论 Corner Case 数据构造指南

随机图只能覆盖"平均情况"，而 XCPC 的 WA 大多死在**退化形态**上。本技能给出可直接
使用的构造公式与本地脚本。

## 1. 树的五种必测形态

设根为 `1`，`parent[i]` 表示 `i` 的父节点：

| 形态 | 构造公式 | 专杀什么 Bug |
| :--- | :--- | :--- |
| 随机树 | `parent[i] = randint(1, i-1)` | 一般性验证，不会误杀 |
| 退化链 | `parent[i] = i - 1` | 递归爆栈、树高/深度假设、倍增未预处理 |
| 菊花图 | `parent[i] = 1` | 度数极大：子树尺寸/重儿子计算、`O(deg^2)` 合并 |
| 满二叉树 | `parent[i] = i // 2` | 完全二叉树性质的错误假设 |
| 蒲公英 | 链 + 每个点挂叶子 | 同时踩深度与度数两个极端 |

**Prufer 序列**（生成均匀随机标号树，避免 `parent[i]=randint(1,i-1)` 的偏斜）：

```python
def prufer_to_tree(seq, n):
    degree = [1] * (n + 1)
    for x in seq:
        degree[x] += 1
    edges, ptr = [], 1
    for x in seq:
        while degree[ptr] != 1:
            ptr += 1
        edges.append((ptr, x))
        degree[ptr] -= 1
        degree[x] -= 1
    u = v = None
    for i in range(1, n + 1):
        if degree[i] == 1:
            if u is None:
                u = i
            else:
                v = i
                break
    edges.append((u, v))
    return edges  # 长度 n-1
```

## 2. 图的必测形态

- **非连通 / 森林**：`k` 个连通块，检查是否只从 `1` 号点 DFS、答案是否漏算孤立点；
- **重边与自环**（题面允许时）：`u==v` 的自环、同一条边出现两次，专杀并查集/最短路计数；
- **链状 DAG / 拓扑序唯一**：`i -> i+1`，检查拓扑排序是否退化成 `1..n`；
- **二分图奇环**：长度为 3 的奇环 + 一条悬挂边，检查染色冲突处理；
- **负权边 / 负环**（题面允许时）：检查 SPFA 入队次数上界与 `dist` 初始化；
- **边权极值**：`1` 与 `1e9` 混合，检查 `int` 溢出与 `INF` 是否足够大；
- **星形网络流**：源点连 `n` 个点、每个点连汇点，检查增广路退化为 `O(n)` 次。

## 3. 使用本地脚本 `scripts/tree_gen.py`

```bash
# 退化链，n=10，输出 n 与 n-1 条边
python <skill_dir>/scripts/tree_gen.py --mode chain --n 10

# 菊花图，输出父节点数组（第 2..n 个点的父节点）
python <skill_dir>/scripts/tree_gen.py --mode star --n 10 --format parents

# 均匀随机树 + 节点权值
python <skill_dir>/scripts/tree_gen.py --mode prufer --n 8 --seed 3 --values -10 10

# 森林（3 个连通块）
python <skill_dir>/scripts/tree_gen.py --mode forest --n 9 --forest-roots 3
```

参数：`--mode star|chain|random|binary|forest|prufer`、`--n`、`--seed`、
`--format edges|parents`、`--values LO HI`、`--forest-roots K`。

## 4. 在 `gen.py` 中内联使用（推荐）

对拍生成器每次都要按 seed 变化，最稳的写法是**把上面的公式直接抄进 `gen.py`**，
只保留 30 行左右：

```python
import random, sys
seed = int(sys.argv[1]); random.seed(seed)
n = random.randint(1, 7)
mode = seed % 4
parent = [0] * (n + 1)
for i in range(2, n + 1):
    if mode == 0:   parent[i] = random.randint(1, i - 1)   # 随机
    elif mode == 1: parent[i] = i - 1                      # 链
    elif mode == 2: parent[i] = 1                          # 菊花
    else:           parent[i] = i // 2                     # 满二叉树
print(n)
print(*[random.randint(-10, 10) for _ in range(n)])
print("\n".join(f"{parent[i]} {i}" for i in range(2, n + 1)))
```

## 5. 判读顺序

先测「`N=1` / 空图 / 孤立点」-> 再测「链、菊花」-> 再测「森林、重边」-> 最后才随机。
四种形态各 30 组，比 500 组纯随机更容易在 10 秒内拍出反例。
