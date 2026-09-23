# -*- coding: utf-8 -*-
"""语言状态持久化测试。

验证点：
1. 无 lang_state.json 时默认恢复 zh
2. 保存后读取 roundtrip（en / zh）
3. 文件内容非法时回退 zh 不抛异常
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # app.py 在 webui 根目录

import app


def test_load_default():
    """无状态文件时默认 zh。"""
    with tempfile.TemporaryDirectory() as td:
        # monkeypatch 状态文件路径到临时目录
        orig = app.LANG_STATE_FILE
        app.LANG_STATE_FILE = Path(td) / "lang_state.json"
        try:
            assert app._load_lang_state() == "zh"
        finally:
            app.LANG_STATE_FILE = orig


def test_roundtrip():
    """保存 en 后读取恢复 en；再存 zh 恢复 zh。"""
    with tempfile.TemporaryDirectory() as td:
        orig = app.LANG_STATE_FILE
        app.LANG_STATE_FILE = Path(td) / "lang_state.json"
        try:
            app._save_lang_state("en")
            assert app._load_lang_state() == "en"
            app._save_lang_state("zh")
            assert app._load_lang_state() == "zh"
        finally:
            app.LANG_STATE_FILE = orig


def test_invalid_content_fallback():
    """文件内容非法（非 zh/en）时回退 zh。"""
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "lang_state.json"
        f.write_text('{"lang": "fr"}', encoding="utf-8")
        orig = app.LANG_STATE_FILE
        app.LANG_STATE_FILE = f
        try:
            assert app._load_lang_state() == "zh"
        finally:
            app.LANG_STATE_FILE = orig


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
