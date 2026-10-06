# -*- coding: utf-8 -*-
"""文件管理重构（项目名 / 目录命名规范 / 上传统一留存）单元测试。

验证点：
1. sanitize_project：非法字符清洗 / 长度限制 / 空值 / 尾部空白与点；
2. rename_project：仅替换文件名中的项目名段（保留时间戳与 _varN 等后缀），
   同步记录 project / audio_path / abc_path / stems[].path（绝对与相对两种形态）；
   新项目名为空则剥离项目名段；不匹配命名规范的文件不动；
3. delete_project：仅允许 outputs 下单层 song_/separations_/cover_ 前缀目录，
   整目录移回收站（mock，避免污染真实回收站）并移除同目录全部记录（含批量变体多条）；
4. app.py / voice_ui_handlers.py 源码断言：目录前缀命名、项目名输入框、
   改名/删除项目回调、上传统一留存（uploads/ + sep_src/cover_src/dry_ref 类别）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import history as H  # noqa: E402

# 统一测试时间戳（文件名规范中的 <时间戳> 段）
TS = "20260925_120000"


def _mgr(tmp_path):
    """构造指向 tmp 的历史管理器（outputs_root = tmp/outputs）。"""
    return H.HistoryManager(db_file=tmp_path / "history.db",
                            outputs_root=tmp_path / "outputs")


def _make_song_project(mgr, tmp_path, project="夜曲"):
    """造一个 song_<ts> 项目目录 + 一条生成记录（audio/abc 用绝对路径，与真实记录一致）。"""
    d = tmp_path / "outputs" / f"song_{TS}"
    d.mkdir(parents=True, exist_ok=True)
    wav = d / f"{project}_{TS}.wav"
    var1 = d / f"{project}_{TS}_var1.wav"
    abc = d / f"{project}_{TS}.abc"
    for f in (wav, var1, abc):
        f.write_bytes(b"x")
    rec = H.HistoryRecord(
        task_id=f"song_{TS}_var1", created_at="2026-09-25T12:00:00",
        style="pop", cot="full",
        audio_path=str(var1), abc_path=str(abc),
        output_dir=f"outputs/song_{TS}", project=project)
    mgr.append(rec)
    return d, rec


# ---------------------------------------------------------------- sanitize_project
def test_sanitize_project():
    assert H.sanitize_project("夜曲demo") == "夜曲demo"
    # 非法字符（文件系统禁用）全部替换为下划线
    assert H.sanitize_project('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    # 空 / 仅空白 -> 空串
    assert H.sanitize_project("") == ""
    assert H.sanitize_project("   ") == ""
    # 尾部空白与点去除
    assert H.sanitize_project("abc. ") == "abc"
    # 超长截断到 60 字符
    assert len(H.sanitize_project("x" * 100)) == 60
    # 控制字符替换为下划线
    assert H.sanitize_project("a\tb") == "a_b"


# ---------------------------------------------------------------- rename_project
def test_rename_project_replaces_project_segment(tmp_path):
    """改名仅替换项目名段：时间戳与 _varN 后缀保留，记录路径同步。"""
    mgr = _mgr(tmp_path)
    d, rec = _make_song_project(mgr, tmp_path, project="夜曲")
    n = mgr.rename_project(f"outputs/song_{TS}", "新名字")
    assert n == 3  # wav + var1.wav + abc 三个文件重命名
    names = sorted(p.name for p in d.iterdir())
    # 注意排序："." < "_"，故 .wav 在 _var1.wav 之前
    assert names == [f"新名字_{TS}.abc", f"新名字_{TS}.wav", f"新名字_{TS}_var1.wav"]
    assert not (d / f"夜曲_{TS}.wav").exists()  # 旧文件已不存在
    # 记录同步：project 字段 + audio_path/abc_path（绝对路径形态）
    r = mgr.get(rec.task_id)
    assert r.project == "新名字"
    assert r.audio_path.endswith(f"新名字_{TS}_var1.wav")
    assert r.abc_path.endswith(f"新名字_{TS}.abc")


def test_rename_project_empty_strips_project(tmp_path):
    """新项目名为空：剥离项目名段，文件以时间戳开头。"""
    mgr = _mgr(tmp_path)
    d, rec = _make_song_project(mgr, tmp_path, project="夜曲")
    assert mgr.rename_project(f"outputs/song_{TS}", "") == 3
    names = sorted(p.name for p in d.iterdir())
    assert names == [f"{TS}.abc", f"{TS}.wav", f"{TS}_var1.wav"]
    assert mgr.get(rec.task_id).project == ""


def test_rename_project_updates_all_records_in_dir(tmp_path):
    """同目录多条记录（批量变体）：改项目名后全部记录的 project/路径同步更新。

    对应歌曲历史页场景：选中一首歌改项目名，同项目其他变体在列表中也应更新。
    """
    mgr = _mgr(tmp_path)
    d, _ = _make_song_project(mgr, tmp_path, project="夜曲")
    # 再补一条同目录的主变体记录（_make_song_project 只登记了 var1）
    rec0 = H.HistoryRecord(
        task_id=f"song_{TS}", created_at="2026-09-25T12:00:00",
        style="pop", cot="full",
        audio_path=str(d / f"夜曲_{TS}.wav"), abc_path=str(d / f"夜曲_{TS}.abc"),
        output_dir=f"outputs/song_{TS}", project="夜曲")
    mgr.append(rec0)
    assert mgr.rename_project(f"outputs/song_{TS}", "新夜曲") == 3
    # 两条记录的 project 与 audio_path 全部指向新文件名
    r0, r1 = mgr.get(f"song_{TS}"), mgr.get(f"song_{TS}_var1")
    assert r0.project == "新夜曲" and r1.project == "新夜曲"
    assert r0.audio_path.endswith(f"新夜曲_{TS}.wav")
    assert r1.audio_path.endswith(f"新夜曲_{TS}_var1.wav")
    assert Path(r0.audio_path).exists() and Path(r1.audio_path).exists()
    mgr.close()


def test_rename_project_updates_stems_and_relative_paths(tmp_path):
    """分离记录改名：stems 内相对/绝对两种路径形态都正确映射。"""
    mgr = _mgr(tmp_path)
    d = tmp_path / "outputs" / f"separations_{TS}"
    d.mkdir(parents=True)
    (d / f"源曲_{TS}_vocals.wav").write_bytes(b"x")
    (d / f"源曲_{TS}_accompaniment.wav").write_bytes(b"x")
    rec = H.HistoryRecord(
        task_id="sep001", created_at="2026-09-25T12:00:00", style="[separation]",
        audio_path=str(d / f"源曲_{TS}_vocals.wav"),
        output_dir=f"outputs/separations_{TS}",
        record_type="separation",
        stems=[{"label": "人声", "type": "vocals",
                "path": f"outputs/separations_{TS}/源曲_{TS}_vocals.wav"},  # 相对路径形态
               {"label": "伴奏", "type": "accompaniment",
                "path": str(d / f"源曲_{TS}_accompaniment.wav")}])  # 绝对路径形态
    mgr.append(rec)
    assert mgr.rename_project(f"outputs/separations_{TS}", "改名曲") == 2
    r = mgr.get("sep001")
    assert r.project == "改名曲"
    # 相对分支：仍是相对路径，且指向新文件名
    assert r.stems[0]["path"].replace("\\", "/") == \
        f"outputs/separations_{TS}/改名曲_{TS}_vocals.wav"
    # 绝对分支：指向新文件名
    assert r.stems[1]["path"].endswith(f"改名曲_{TS}_accompaniment.wav")
    assert r.audio_path.endswith(f"改名曲_{TS}_vocals.wav")


def test_rename_project_ignores_nonconforming_files(tmp_path):
    """不符合 <项目名>_<时间戳> 规范的文件（readme/_progress.json 等）不动。"""
    mgr = _mgr(tmp_path)
    d = tmp_path / "outputs" / f"song_{TS}"
    d.mkdir(parents=True)
    (d / f"夜曲_{TS}.wav").write_bytes(b"x")
    (d / "readme.txt").write_bytes(b"x")
    (d / "_progress.json").write_bytes(b"x")
    assert mgr.rename_project(f"outputs/song_{TS}", "新名") == 1
    assert (d / "readme.txt").exists()
    assert (d / "_progress.json").exists()
    assert (d / f"新名_{TS}.wav").exists()


# ---------------------------------------------------------------- delete_project
def test_delete_project_removes_records_and_dir(tmp_path, monkeypatch):
    """删除项目：整目录入回收站，同目录全部记录（批量变体多条）一并移除。"""
    mgr = _mgr(tmp_path)
    d = tmp_path / "outputs" / f"song_{TS}"
    d.mkdir(parents=True)
    (d / f"夜曲_{TS}.wav").write_bytes(b"x")
    for i in (1, 2):  # 批量变体：同目录两条记录
        mgr.append(H.HistoryRecord(
            task_id=f"song_{TS}_var{i}", created_at="2026-09-25T12:00:00",
            style="pop", audio_path=str(d / f"夜曲_{TS}.wav"),
            output_dir=f"outputs/song_{TS}", project="夜曲"))
    # mock 回收站删除，避免测试污染真实回收站
    deleted = []
    monkeypatch.setattr(H, "_delete_to_recycle",
                        lambda p: (deleted.append(str(p)), True)[1])
    assert mgr.delete_project(f"outputs/song_{TS}") == 2
    assert mgr.list_all() == []  # 两条记录一并移除
    assert deleted and deleted[0].endswith(f"song_{TS}")  # 整目录入回收站
    assert d.exists()  # mock 未真删磁盘；tmp_path 兜底清理


def test_delete_project_rejects_non_project_dir(tmp_path, monkeypatch):
    """安全判定：非 song_/separations_/cover_ 前缀目录拒绝删除，记录保留。"""
    mgr = _mgr(tmp_path)
    d = tmp_path / "outputs" / "random_dir"
    d.mkdir(parents=True)
    (d / "x.wav").write_bytes(b"x")
    mgr.append(H.HistoryRecord(
        task_id="r1", created_at="2026-09-25T12:00:00", style="s",
        audio_path=str(d / "x.wav"), output_dir="outputs/random_dir"))
    called = []
    monkeypatch.setattr(H, "_delete_to_recycle",
                        lambda p: called.append(str(p)) or True)
    assert mgr.delete_project("outputs/random_dir") == 0  # 拒绝
    assert not called  # 未触发回收站
    assert mgr.get("r1") is not None  # 记录保留
    assert (d / "x.wav").exists()


# ---------------------------------------------------------------- 源码断言（app / handlers）
def test_app_uses_new_naming_and_project_ui():
    from _app_bundle import app_bundle  # C1 拆分后源码级断言读 app bundle
    src = app_bundle()
    # 目录命名：song_<ts>（生成/重新合成）
    assert 'base_task_id = f"song_{timestamp}"' in src
    assert 'task_id = f"song_{timestamp}"' in src
    # 生成页项目名输入框 + 历史页改名/删除项目回调
    assert "project_input = gr.Textbox" in src
    assert "on_history_rename_project" in src
    assert "on_history_delete_project" in src
    # 分离/翻唱项目名解析与任务级改名/删除回调
    assert "_project_from_source" in src
    assert "on_voice_task_rename" in src
    assert "on_voice_task_delete" in src


def test_voice_handlers_new_derived_dir_and_uploads():
    src = (Path(__file__).resolve().parent.parent / "src" / "voice_ui_handlers.py") \
        .read_text(encoding="utf-8-sig")
    # 衍生目录命名：outputs/separations_<ts> 与 outputs/cover_<ts>
    assert '"separations_" if kind == "sep" else "cover_"' in src
    # 上传统一留存：uploads/ + 四类类别标识
    assert "persist_upload" in src
    for cat in ("sep_src", "cover_src", "dry_ref", "transcribe"):
        assert f'"{cat}"' in src
    # 产物命名前缀 = <项目名>_<时间戳>（项目名空则时间戳开头）
    assert 'prefix = f"{project}_{ts}" if project else ts' in src
