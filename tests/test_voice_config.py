# -*- coding: utf-8 -*-
"""voice_config 模块单元测试（config.cfg [voice] 段解析）。

验证点（对齐 load_model_config 的既有测试覆盖风格）：
1. 无 cfg 文件时回退默认值（enabled=true / port=8190 / seedvc=None / max=8）
2. 部分配置缺失时逐项回退默认
3. seedvc_dir 支持相对路径（基于系统根）与绝对路径解析
4. seedvc_dir 指向不存在目录时返回 None（UI 据此显示安装指引）
5. 段缺失 / 文件损坏时回退默认，不抛异常
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from voice_config import load_voice_config  # noqa: E402

# 系统根模拟：test 运行时把临时目录当 project_root，config.cfg 放其 yue2-webui/ 子目录


def _make_roots():
    tmp = Path(tempfile.mkdtemp())
    webui = tmp / "yue2-webui"
    webui.mkdir()
    return tmp, webui


def _write_cfg(webui: Path, text: str):
    f = webui / "config.cfg"
    f.write_text(text, encoding="utf-8")
    return f


def test_missing_cfg_returns_defaults():
    tmp, webui = _make_roots()
    cfg = load_voice_config(tmp)
    assert cfg["enabled"] is True
    assert cfg["worker_port"] == 8190
    assert cfg["seedvc_dir"] is None
    assert cfg["max_input_minutes"] == 8


def test_partial_section_fills_defaults():
    tmp, webui = _make_roots()
    _write_cfg(webui, "\n[voice]\nenabled = false\nworker_port = 9001\n")
    cfg = load_voice_config(tmp)
    assert cfg["enabled"] is False
    assert cfg["worker_port"] == 9001
    # 未填的项回退默认
    assert cfg["max_input_minutes"] == 8
    assert cfg["seedvc_dir"] is None


def test_relative_seedvc_dir_resolves_from_system_root():
    tmp, webui = _make_roots()
    svc = tmp / "seed-vc"
    svc.mkdir()
    _write_cfg(webui, "\n[voice]\nseedvc_dir = seed-vc\n")
    cfg = load_voice_config(tmp)
    assert cfg["seedvc_dir"] == svc


def test_absolute_seedvc_dir():
    tmp, webui = _make_roots()
    svc = tmp / "abs-seedvc"
    svc.mkdir()
    _write_cfg(webui, f"\n[voice]\nseedvc_dir = {svc}\n")
    cfg = load_voice_config(tmp)
    assert cfg["seedvc_dir"] == svc


def test_nonexistent_seedvc_dir_returns_none():
    tmp, webui = _make_roots()
    _write_cfg(webui, "\n[voice]\nseedvc_dir = not_there\n")
    cfg = load_voice_config(tmp)
    assert cfg["seedvc_dir"] is None


def test_corrupt_cfg_falls_back():
    tmp, webui = _make_roots()
    _write_cfg(webui, "\n[voice]<<< malformed \n  = = =\n")
    cfg = load_voice_config(tmp)
    assert cfg["enabled"] is True
    assert cfg["worker_port"] == 8190
    assert cfg["max_input_minutes"] == 8


def test_other_sections_ignored():
    tmp, webui = _make_roots()
    _write_cfg(webui, "\n[models]\nmain_model = x.gguf\n")
    cfg = load_voice_config(tmp)
    # 仅 [voice] 段影响，[models] 不被误读
    assert cfg["enabled"] is True