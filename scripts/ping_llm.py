#!/usr/bin/env python3
"""LLM 连通性自检（PRD Step 1 的 10 行验证脚本）。

用法::

    uv run scripts/ping_llm.py
    OPENAI_MODEL=deepseek-reasoner uv run scripts/ping_llm.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.llm import call_llm, current_model  # noqa: E402


def main() -> int:
    try:
        response = call_llm(
            [{"role": "user", "content": "只回复两个字：连通"}],
            stream=False,
            max_tokens=32,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001 - 自检脚本需要打印真实错误
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        print("排查：OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL 是否正确（见 .env.example）")
        return 1
    print(f"[OK] model={current_model()}")
    print(f"     reply={response.get('content', '')!r}")
    print(f"     usage={response.get('usage')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
