# -*- coding: utf-8 -*-
"""「使用上一次」乐谱恢复逻辑测试。

背景 bug：_save_last_inputs 只在任务开始时存外部 ABC 输入框内容（正常生成时为空），
导致生成后点「使用上一次」乐谱恢复为空。修复：生成成功后用产出的 abc_score 更新。

验证点：
1. _update_last_abc 只更新 abc 字段，style/lyrics 保留
2. on_restore_last 能按 kind 取回更新后的值
3. last_inputs.json 不存在时恢复空串不报错
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # app.py 在 webui 根目录

import app


def _with_temp_state(fn):
    """把 LANG/LAST_INPUTS 状态文件指向临时目录后执行 fn。"""
    with tempfile.TemporaryDirectory() as td:
        orig_last = app.LAST_INPUTS_FILE
        app.LAST_INPUTS_FILE = Path(td) / "last_inputs.json"
        try:
            fn()
        finally:
            app.LAST_INPUTS_FILE = orig_last


def test_update_last_abc_preserves_others():
    """_update_last_abc 只改 abc，保留 style/lyrics。"""
    def run():
        app._save_last_inputs("pop, female vocal", "[Verse]\nla", "")
        assert app._load_last_inputs().get("abc") == ""
        app._update_last_abc("X:1\nT:Test")
        data = app._load_last_inputs()
        assert data["abc"] == "X:1\nT:Test"
        assert data["style"] == "pop, female vocal"
        assert data["lyrics"] == "[Verse]\nla"
    _with_temp_state(run)


def test_restore_last_returns_updated_abc():
    """on_restore_last('abc') 取回生成产出更新后的乐谱。"""
    def run():
        app._save_last_inputs("style", "lyrics", "")
        app._update_last_abc("X:1\nT:Generated")
        assert app.on_restore_last("abc", "current") == "X:1\nT:Generated"
    _with_temp_state(run)


def test_restore_missing_file():
    """状态文件不存在时点恢复 → 保留当前输入（设计为不清空，避免误覆盖）。"""
    def run():
        assert app.on_restore_last("abc", "old") == "old"
    _with_temp_state(run)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}")
        except Exception as e:
            print(f"ERROR: {t.__name__}: {e}")
    print(f"\n结果: {passed} 通过, {len(tests) - passed} 失败")
    sys.exit(0 if passed == len(tests) else 1)
