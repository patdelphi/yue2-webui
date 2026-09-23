# -*- coding: utf-8 -*-
"""外置 config.cfg 模型路径配置测试。

验证点：
1. cfg 不存在时回退默认路径（保持旧行为）
2. cfg 存在时覆盖模型目录/文件名/SheetSage2 路径
3. 相对路径基于项目根解析，绝对路径直接使用
4. cfg 损坏时不崩溃，回退默认值
5. check_models/check_sheetsage2 按 cfg 路径检查
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from backend_gguf import GGUFBackend, load_model_config, DEFAULT_MODEL_CONFIG


def _write_cfg(root: Path, content: str) -> None:
    """在模拟项目根的 yue2-webui 子目录写测试用 config.cfg（与实际布局一致）。"""
    webui_dir = root / "yue2-webui"
    webui_dir.mkdir(parents=True, exist_ok=True)
    (webui_dir / "config.cfg").write_text(content, encoding="utf-8")


def test_default_when_no_cfg():
    """cfg 不存在时使用默认值。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = load_model_config(root)
        assert cfg["models_dir"] == root / "models", cfg
        assert cfg["main_model"] == "yue2-3b-q8_0.gguf", cfg
        assert cfg["vae_model"] == "yue2-vae-f16.gguf", cfg
        assert cfg["sheetsage2_path"] == root / "audio-cpp" / "models" / "SheetSage2-GGUF" / "sheetsage2-orig.gguf", cfg


def test_cfg_override():
    """cfg 存在时覆盖默认值（相对路径基于项目根解析）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_cfg(root, """
[models]
models_dir = my_models
main_model = yue2-3b-q4_0.gguf
vae_model = my-vae.gguf
sheetsage2_path = ss2/model.gguf
""")
        cfg = load_model_config(root)
        assert cfg["models_dir"] == root / "my_models", cfg
        assert cfg["main_model"] == "yue2-3b-q4_0.gguf", cfg
        assert cfg["vae_model"] == "my-vae.gguf", cfg
        assert cfg["sheetsage2_path"] == root / "ss2" / "model.gguf", cfg


def test_absolute_path():
    """cfg 中绝对路径直接使用，不拼接项目根。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_cfg(root, f"""
[models]
models_dir = {td}\\abs_models
""")
        cfg = load_model_config(root)
        assert cfg["models_dir"] == Path(td) / "abs_models", cfg


def test_partial_cfg_keeps_defaults():
    """cfg 只写部分配置项时，其余项回退默认值。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_cfg(root, """
[models]
main_model = other.gguf
""")
        cfg = load_model_config(root)
        assert cfg["main_model"] == "other.gguf", cfg
        assert cfg["models_dir"] == root / "models", cfg
        assert cfg["vae_model"] == "yue2-vae-f16.gguf", cfg


def test_broken_cfg_fallback():
    """cfg 语法损坏时不抛异常，回退全部默认值。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_cfg_bytes(root, b"\xff\xfe\x00binary garbage")
        cfg = load_model_config(root)
        assert cfg == load_model_config(Path(tempfile.mkdtemp())) or cfg["main_model"] == "yue2-3b-q8_0.gguf"


def _write_cfg_bytes(root: Path, content: bytes) -> None:
    """在 yue2-webui 子目录写二进制损坏 cfg（模拟文件损坏场景）。"""
    webui_dir = root / "yue2-webui"
    webui_dir.mkdir(parents=True, exist_ok=True)
    (webui_dir / "config.cfg").write_bytes(content)


def test_backend_uses_cfg_paths():
    """GGUFBackend 按 cfg 路径检查模型存在性（造假文件验证 available）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # cfg 指向临时目录，并创建假模型文件
        model_dir = root / "mm"
        model_dir.mkdir()
        (model_dir / "main.gguf").write_bytes(b"x")
        (model_dir / "vae.gguf").write_bytes(b"x")
        ss2 = root / "ss2.gguf"
        ss2.write_bytes(b"x")
        _write_cfg(root, f"""
[models]
models_dir = {model_dir}
main_model = main.gguf
vae_model = vae.gguf
sheetsage2_path = {ss2}
""")
        backend = GGUFBackend(root)
        checks = backend.check_models()
        assert checks["available"] is True, checks
        assert checks["model_gguf"]["path"].endswith("main.gguf"), checks
        assert checks["vae_gguf"]["path"].endswith("vae.gguf"), checks
        ss2_check = backend.check_sheetsage2()
        assert ss2_check["available"] is True, ss2_check


def test_default_config_table_complete():
    """默认配置表包含全部必需键。"""
    assert set(DEFAULT_MODEL_CONFIG.keys()) == {"models_dir", "main_model", "vae_model", "sheetsage2_path"}


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
