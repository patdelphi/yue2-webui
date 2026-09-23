# -*- coding: utf-8 -*-
"""预设参数系统与最新功能字段匹配测试。

背景 bug：预设保存/加载只覆盖 17 个旧字段，后来新增的 CFG 引导强度、
批量生成数量、4 个音频后处理开关未纳入，导致预设恢复不完整。

验证点：
1. save→load roundtrip 含全部 23 个字段（17 旧 + cfg/batch + 4 后处理）
2. 旧格式 json（缺新字段）load 时不崩，缺失字段返回 gr.update()（保持当前值）
3. 内置预设 load：声明字段恢复、未声明字段保持当前值
4. 空名称保存被拒绝
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # app.py 在 webui 根目录

import gradio as gr
import app


def _with_temp_presets(fn):
    """把 presets 目录指到临时目录后执行 fn（测试隔离，不碰真实文件）。"""
    with tempfile.TemporaryDirectory() as td:
        orig = app.WEBUI_ROOT
        app.WEBUI_ROOT = Path(td)
        try:
            fn()
        finally:
            app.WEBUI_ROOT = orig


def test_save_load_roundtrip():
    """保存当前参数 → 加载恢复：25 个字段全部一致。"""
    # 模拟创作页当前值（含新功能字段的非默认值）
    cur = dict(
        cot="melody", steps=16, out_format="pcm24",
        abc_temp=0.5, abc_top_p=0.8, abc_top_k=20, abc_rep=1.1, abc_pen=80, abc_min=16, abc_max=2048,
        sem_temp=1.2, sem_top_p=0.9, sem_top_k=64, sem_rep=1.3, sem_pen=40, sem_min=100, sem_max=8000,
        cfg=2.5, batch=3, pp_norm=False, pp_fade=False, pp_trim=True, pp_meta=False,
    )

    def run():
        app.on_preset_save("RoundTrip", cur["cot"], cur["steps"], cur["out_format"],
                           cur["abc_temp"], cur["abc_top_p"], cur["abc_top_k"], cur["abc_rep"], cur["abc_pen"], cur["abc_min"], cur["abc_max"],
                           cur["sem_temp"], cur["sem_top_p"], cur["sem_top_k"], cur["sem_rep"], cur["sem_pen"], cur["sem_min"], cur["sem_max"],
                           cur["cfg"], cur["batch"], cur["pp_norm"], cur["pp_fade"], cur["pp_trim"], cur["pp_meta"])
        got = app.on_preset_load("RoundTrip")
        assert len(got) == 23, f"load 应返回 23 个字段，实际 {len(got)}"
        keys = ["cot", "steps", "out_format",
                "abc_temp", "abc_top_p", "abc_top_k", "abc_rep", "abc_pen", "abc_min", "abc_max",
                "sem_temp", "sem_top_p", "sem_top_k", "sem_rep", "sem_pen", "sem_min", "sem_max",
                "cfg", "batch", "pp_norm", "pp_fade", "pp_trim", "pp_meta"]
        # 逐字段比对保存值与恢复值
        for i, k in enumerate(keys):
            assert got[i] == cur[k], f"字段 {k} 不一致: save={cur[k]} load={got[i]}"

    _with_temp_presets(run)


def test_old_format_json_missing_fields():
    """旧格式预设（缺 cfg/batch/后处理字段）：缺失字段返回 gr.update() 而非默认值覆盖。"""

    def run():
        pdir = app.WEBUI_ROOT / "presets"
        pdir.mkdir(exist_ok=True)
        # 旧版格式：只有 17 个字段
        (pdir / "Old.json").write_text(
            '{"name": "Old", "params": {"cot": "off", "num_inference_steps": 4}}',
            encoding="utf-8",
        )
        got = app.on_preset_load("Old")
        assert len(got) == 23, f"应返回 23 个字段，实际 {len(got)}"
        assert got[0] == "off" and got[1] == 4, "声明字段应恢复"
        # 新字段缺失 → gr.update()（前端保持当前值，不覆盖）
        for i in (17, 18, 19, 20, 21, 22):
            assert isinstance(got[i], dict) and got[i].get("__type__") == "update", \
                f"缺失字段 {i} 应为 gr.update()，实际 {got[i]!r}"

    _with_temp_presets(run)


def test_builtin_preset_partial_restore():
    """内置预设：params 声明的字段恢复，未声明字段保持当前值。"""
    got = app.on_preset_load("快速demo")
    assert len(got) == 23
    assert got[0] == "off" and got[1] == 4, "快速demo 声明字段"
    assert isinstance(got[17], dict) and got[17].get("__type__") == "update", "cfg 未声明应保持当前值"


def test_empty_name_rejected():
    """空名称保存被拒绝并返回提示。"""

    def run():
        msg = app.on_preset_save("  ", *([None] * 23))
        assert "请输入预设名称" in msg or "preset name" in msg.lower(), msg

    _with_temp_presets(run)


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
