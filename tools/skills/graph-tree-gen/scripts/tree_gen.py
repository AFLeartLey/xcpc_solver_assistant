#!/usr/bin/env python3
"""参数化树 / 图数据生成器（供 ``graph-tree-gen`` 技能调用）。

输出格式::

    edges   : 第 1 行 n；随后 n-1 行 "u v"（1-indexed）
    parents : 第 1 行 n；第 2 行 p2 p3 ... pn（1-indexed，根为 0 时表示森林）

``--values LO HI`` 时在 n 之后额外输出一行 n 个节点权值。

示例::

    python tree_gen.py --mode star --n 8
    python tree_gen.py --mode chain --n 6 --format parents --values -10 10
    python tree_gen.py --mode prufer --n 9 --seed 42
    python tree_gen.py --mode forest --n 9 --forest-roots 3
"""

from __future__ import annotations

import argparse
import random
import sys

MODES = ("star", "chain", "random", "binary", "forest", "prufer")


def prufer_to_tree(seq: list[int], n: int) -> list[tuple[int, int]]:
    """Prufer 序列 -> 树边（均匀随机标号树）。

    使用小根堆维护"当前可用的叶子"，O(n log n)；朴素单调指针在 ``x < ptr``
    时会把已越过的叶子漏掉，产生越界。
    """
    if n < 2:
        return []
    degree = [1] * (n + 1)
    for x in seq:
        degree[x] += 1

    import heapq

    heap = [i for i in range(1, n + 1) if degree[i] == 1]
    heapq.heapify(heap)
    edges: list[tuple[int, int]] = []
    for x in seq:
        while heap and degree[heap[0]] != 1:
            heapq.heappop(heap)
        leaf = heapq.heappop(heap)
        edges.append((leaf, x))
        degree[leaf] -= 1
        degree[x] -= 1
        if degree[x] == 1:
            heapq.heappush(heap, x)

    leaves = [i for i in range(1, n + 1) if degree[i] == 1]
    if len(leaves) >= 2:
        edges.append((leaves[0], leaves[1]))
    return edges


def build_parents(mode: str, n: int, rng: random.Random, forest_roots: int) -> list[int]:
    parent = [0] * (n + 1)
    if mode == "star":
        for i in range(2, n + 1):
            parent[i] = 1
    elif mode == "chain":
        for i in range(2, n + 1):
            parent[i] = i - 1
    elif mode == "binary":
        for i in range(2, n + 1):
            parent[i] = i // 2
    elif mode == "random":
        for i in range(2, n + 1):
            parent[i] = rng.randint(1, i - 1)
    elif mode == "forest":
        roots = max(2, min(forest_roots, n))
        root_set = set(rng.sample(range(1, n + 1), roots))
        for i in range(1, n + 1):
            if i in root_set:
                parent[i] = 0
            else:
                candidates = [j for j in range(1, i) if j not in root_set] or [j for j in range(1, i)]
                parent[i] = rng.choice(candidates) if candidates else 0
    elif mode == "prufer":
        seq = [rng.randint(1, n) for _ in range(max(0, n - 2))]
        edges = prufer_to_tree(seq, n)
        # 统一以 1 为根重定向，保证 parents 格式合法
        adjacency: dict[int, list[int]] = {i: [] for i in range(1, n + 1)}
        for u, v in edges:
            adjacency[u].append(v)
            adjacency[v].append(u)
        seen = [False] * (n + 1)
        stack = [1]
        seen[1] = True
        while stack:
            u = stack.pop()
            for v in adjacency[u]:
                if not seen[v]:
                    seen[v] = True
                    parent[v] = u
                    stack.append(v)
    else:  # pragma: no cover - argparse 已限制
        raise ValueError(mode)
    return parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="XCPC 树/图 Corner Case 生成器")
    parser.add_argument("--mode", choices=MODES, default="random")
    parser.add_argument("--n", type=int, default=8, help="节点数（1-indexed，根为 1）")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--format", choices=("edges", "parents"), default="edges")
    parser.add_argument("--values", nargs=2, type=int, metavar=("LO", "HI"), default=None,
                        help="额外输出一行节点权值，范围 [LO, HI]")
    parser.add_argument("--forest-roots", type=int, default=3, help="forest 模式的连通块数量")
    args = parser.parse_args(argv)

    n = max(1, args.n)
    rng = random.Random(args.seed)
    parent = build_parents(args.mode, n, rng, args.forest_roots)

    out: list[str] = [str(n)]
    if args.values:
        lo, hi = sorted(args.values)
        out.append(" ".join(str(rng.randint(lo, hi)) for _ in range(n)))
    if args.format == "parents":
        out.append(" ".join(str(parent[i]) for i in range(2, n + 1)) if n >= 2 else "")
    else:
        for i in range(2, n + 1):
            p = parent[i] if parent[i] else 1
            out.append(f"{p} {i}")
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
