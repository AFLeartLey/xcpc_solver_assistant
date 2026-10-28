// case_01 正确解：每组数据彻底重置邻接表与 DFS 标记。
#include <bits/stdc++.h>
using namespace std;

const int MAXN = 100005;
int head[MAXN], nxt[MAXN * 2], to[MAXN * 2], ecnt = 0;
long long a[MAXN], subSum[MAXN];
bool vis[MAXN];

void addEdge(int u, int v) {
    to[++ecnt] = v;
    nxt[ecnt] = head[u];
    head[u] = ecnt;
}

void dfs(int u) {
    vis[u] = true;
    subSum[u] = a[u];
    for (int e = head[u]; e != 0; e = nxt[e]) {
        int v = to[e];
        if (!vis[v]) {
            dfs(v);
            subSum[u] += subSum[v];
        }
    }
}

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    int T;
    if (!(cin >> T)) return 0;
    while (T--) {
        int n;
        cin >> n;
        for (int i = 1; i <= n; i++) cin >> a[i];

        ecnt = 0;
        for (int i = 1; i <= n; i++) head[i] = 0;

        for (int i = 1; i < n; i++) {
            int u, v;
            cin >> u >> v;
            addEdge(u, v);
            addEdge(v, u);
        }

        for (int i = 1; i <= n; i++) vis[i] = false;

        dfs(1);
        for (int i = 1; i <= n; i++) cout << subSum[i] << " \n"[i == n];
    }
    return 0;
}
