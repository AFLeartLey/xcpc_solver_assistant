// case_02: 隐蔽的 32 位整数溢出 —— 累加器用了 int，值域 1e9 * 2e5 时溢出为负数。
// 样例（1..5）能过；一旦出现 3 个 1e9 就 WA。
#include <bits/stdc++.h>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    int n;
    if (!(cin >> n)) return 0;

    int sum = 0;  // BUG: 应为 long long
    for (int i = 0; i < n; i++) {
        int x;
        cin >> x;
        sum += x;
    }
    cout << sum << "\n";
    return 0;
}
