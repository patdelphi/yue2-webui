# -*- coding: utf-8 -*-
"""测试用「app bundle」读取助手（C1 拆分 app.py 后同步源码级断言）。

C1 把 app.py 的回调/UI 构建拆分到 src/callbacks_*.py 与 src/ui_tabs.py，但大量
测试用 `read_text("app.py")` 做源码级字符串断言。为在不丢失断言覆盖的前提下适配
拆分，统一改为读取「bundle」= app.py + src/ui_tabs.py + src/callbacks_*.py 的拼接。

用法：
    from _app_bundle import app_bundle
    src = app_bundle()          # 等价于拆分前的 app.py 源码
"""

from pathlib import Path

_WEBUI_DIR = Path(__file__).resolve().parent.parent
_SRC_DIR = _WEBUI_DIR / "src"


def app_bundle() -> str:
    """返回 app.py 与其拆分模块的源码拼接（模拟拆分前的单体 app.py）。

    按固定顺序拼接：app.py 本体、src/ui_tabs.py、以及 src/callbacks_*.py（排序稳定），
    使既有源码级断言（子串/计数）在拆分后仍成立。
    """
    parts = [( _WEBUI_DIR / "app.py").read_text(encoding="utf-8-sig")]
    ui_tabs = _SRC_DIR / "ui_tabs.py"
    if ui_tabs.exists():
        parts.append(ui_tabs.read_text(encoding="utf-8-sig"))
    for cb in sorted(_SRC_DIR.glob("callbacks_*.py")):
        parts.append(cb.read_text(encoding="utf-8-sig"))
    return "\n".join(parts)
