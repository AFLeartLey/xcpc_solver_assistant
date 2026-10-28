// case_03: 经典的"性价比贪心"假做法 —— 按 v/w 降序贪心，遇到
//   W=10, (6,11), (5,9), (5,9)
// 时返回 11，而正解是 18。样例恰好能过，随机小数据也常能过。
#include <bits/stdc++.h>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    int n, W;
    if (!(cin >> n >> W)) return 0;

    vector<pair<int, int>> items(n);  // (w, v)
    for (auto &it : items) cin >> it.first >> it.second;

    sort(items.begin(), items.end(), [](const pair<int, int> &a, const pair<int, int> &b) {
        // 按 价值/重量 降序（交叉相乘避免浮点误差）
        long long lhs = 1LL * a.second * b.first;
        long long rhs = 1LL * b.second * a.first;
        if (lhs != rhs) return lhs > rhs;
        return a.second > b.second;
    });

    int cap = W, ans = 0;
    for (auto &it : items) {
        if (it.first <= cap) {
            cap -= it.first;
            ans += it.second;
        }
    }
    cout << ans << "\n";
    return 0;
}
