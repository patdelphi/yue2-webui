"""歌曲历史页回调组（C1 拆分 app.py：第三阶段 · 历史组）。

程序说明
--------
本模块承载「歌曲历史」相关回调：分页刷新 / 选中加载 / 试听播放器同步 /
删除与清空 / 项目改名与删除项目。依赖全部由 app.py 在调用时以
:class:`HistoryDeps` 注入（本模块不 import app.py，避免循环依赖），因此 app.py
侧的猴子补丁（history_mgr / _CUR_LANG / WEBUI_ROOT 等）在调用时仍然生效。

app.py 保留同名薄封装：Gradio 的 inputs/outputs 绑定与函数签名完全不变，
既有测试与前端绑定无需改动。特别注意 refresh_history / refresh_history_full
必须保持为 app.py 模块级可赋值名字（_gen_deps 调用时读取，供测试猴子补丁）。
"""
import html
import json
from dataclasses import dataclass
from pathlib import Path

import gradio as gr

from i18n import tr

# 歌曲历史页每页条数（SQL 分页 LIMIT/OFFSET 使用）
HISTORY_PAGE_SIZE = 10


@dataclass
class HistoryDeps:
    """历史组回调的外部依赖（app.py 调用时注入当前全局值，保证猴子补丁生效）。"""
    webui_root: Path          # app.WEBUI_ROOT
    history_mgr: object       # app.history_mgr
    cur_lang: str             # app._CUR_LANG
    prefer_mp3: object        # app._prefer_mp3


def _history_page_info_text(d: HistoryDeps, page, pages, total):
    """按当前语言生成分页信息文案（整体句式，避免逐词翻译导致中英标点混杂）。"""
    if d.cur_lang == "en":
        return f"Page {page} / {pages}, {total} entries"
    return f"第 {page} / {pages} 页，共 {total} 条"


def refresh_history(d: HistoryDeps):
    """Refresh history dataframe (first page)."""
    # 歌曲历史页只展示生成记录；分离/翻唱记录在各自 Tab 的历史区查看
    # SQL 分页 + COUNT：只取本页列、不读全表（A4）
    total = d.history_mgr.count_rows(record_types=("generation",))
    pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page_rows = d.history_mgr.to_dataframe_rows(record_types=("generation",),
                                                limit=HISTORY_PAGE_SIZE, offset=0)
    return page_rows, _history_page_info_text(d, 1, pages, total)


def refresh_history_full(d: HistoryDeps):
    """Refresh history with page state reset (for buttons)."""
    rows, info = refresh_history(d)
    return rows, info, 0


def _get_history_page(d: HistoryDeps, page):
    """Get a specific page of history. Returns (rows, page_info)."""
    # 与 refresh_history 一致：仅生成记录进入歌曲历史分页（SQL LIMIT/OFFSET）
    total = d.history_mgr.count_rows(record_types=("generation",))
    pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    page_rows = d.history_mgr.to_dataframe_rows(record_types=("generation",),
                                                limit=HISTORY_PAGE_SIZE,
                                                offset=page * HISTORY_PAGE_SIZE)
    return page_rows, _history_page_info_text(d, page + 1, pages, total)


def on_history_prev_page(d: HistoryDeps, current_page):
    """Go to previous page."""
    new_page = max(0, int(current_page) - 1)
    return _get_history_page(d, new_page) + (new_page,)


def on_history_next_page(d: HistoryDeps, current_page):
    """Go to next page."""
    # 页数按过滤后的记录集计算（与 refresh_history/_get_history_page 一致），
    # 否则存在分离/翻唱记录时总页数会偏大；COUNT 走 SQL，不读全表
    total = d.history_mgr.count_rows(record_types=("generation",))
    pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    new_page = min(pages - 1, int(current_page) + 1)
    return _get_history_page(d, new_page) + (new_page,)


def _load_history_entry(d: HistoryDeps, row_index, current_state):
    """Load a history entry by row index.
    返回 state, audio, info, style, lyrics, abc, preview, lyrics_data, duration_data。

    注意：行号映射基于与表格一致的过滤记录集（generation），否则选中行会错位
    （曾导致点击生成记录实际选中 separation 记录）。
    """
    # 只按行号取该条 task_id（SQL LIMIT 1 OFFSET），不再读全表（A4）
    task_id = d.history_mgr.task_id_at(record_types=("generation",), offset=row_index)
    if row_index < 0 or not task_id:
        return current_state, None, tr(d.cur_lang, "请选择一条记录"), "", "", "", "", "", ""
    entry = d.history_mgr.get(task_id)
    if not entry:
        return current_state, None, tr(d.cur_lang, "记录不存在"), "", "", "", "", "", ""
    audio_path = Path(entry.audio_path)
    abc_score = d.history_mgr.get_abc_score(task_id) or ""
    if audio_path.exists():
        lyrics = entry.lyrics or entry.lyrics_preview or ""
        _lyrics_json = html.escape(json.dumps(lyrics, ensure_ascii=False), quote=True)
        lyrics_data = f'<div class="history-lyrics-data" style="display:none" data-lyrics=\'{_lyrics_json}\' data-duration="{entry.audio_duration_seconds}"></div>'
        abc_preview = '<div id="history-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px;"><div id="history-abc-paper"></div><div id="history-abc-audio"></div></div>'
        return [task_id], d.prefer_mp3(str(audio_path)), f"**{entry.task_id}**", entry.style, lyrics, abc_score, abc_preview, lyrics_data, f'<div class="history-duration-data" style="display:none" data-duration="{entry.audio_duration_seconds}"></div>'
    return [task_id], None, tr(d.cur_lang, "音频文件不存在"), "", "", "", "", "", ""


def on_history_select(d: HistoryDeps, evt: gr.SelectData, current_state: list, current_page):
    """Handle history row selection via Dataframe.select (fallback)."""
    actual_index = evt.index[0] + int(current_page) * HISTORY_PAGE_SIZE
    return _load_history_entry(d, actual_index, current_state)


def _hist_player_keep():
    """历史页试听播放器：保持现状（未选中/操作失败时使用）。"""
    return gr.update()


def _hist_player_clear():
    """历史页试听播放器：清空（删除/清空成功后使用，避免仍指向已删文件）。"""
    return gr.update(value=None)


def on_history_delete(d: HistoryDeps, selected_state):
    """Delete the currently selected history entry.

    返回末尾的试听播放器同步刷新，避免仍指向已删除文件
    （与分离/翻唱页 on_voice_task_delete 清空播放器的行为一致）。
    """
    if not selected_state:
        rows, info = refresh_history(d)
        return rows, info, tr(d.cur_lang, "请先点击选择要删除的记录"), selected_state, 0, *_hist_player_keep()
    task_id = selected_state[0]
    if d.history_mgr.delete(task_id):
        rows, info = refresh_history(d)
        return rows, info, f"{tr(d.cur_lang, '已删除')} {task_id}", [], 0, *_hist_player_clear()
    rows, info = refresh_history(d)
    return rows, info, tr(d.cur_lang, "删除失败"), selected_state, 0, *_hist_player_keep()


def on_history_clear(d: HistoryDeps):
    """Clear all history. 清空后播放器一并清空。"""
    d.history_mgr.clear()
    rows, info = refresh_history(d)
    return rows, info, tr(d.cur_lang, "已清空所有历史"), [], 0, *_hist_player_clear()


def on_history_rename_project(d: HistoryDeps, selected_state, new_name):
    """改项目名（文件管理重构）：重命名选中记录所在项目目录的全部文件。

    只替换文件名中的项目名段，保留时间戳与后缀（_varN 等）；目录名不动。
    输入留空 = 清除项目名（文件回退为时间戳开头）。
    返回末尾的试听播放器按记录新路径重填（旧路径已随重命名失效）。
    """
    if not selected_state:
        rows, info = refresh_history(d)
        return rows, info, tr(d.cur_lang, "请先点击选择要修改的记录"), selected_state, 0, *_hist_player_keep()
    entry = d.history_mgr.get(selected_state[0])
    if not entry or not getattr(entry, "output_dir", ""):
        rows, info = refresh_history(d)
        return rows, info, tr(d.cur_lang, "该记录没有产物目录"), selected_state, 0, *_hist_player_keep()
    n = d.history_mgr.rename_project(entry.output_dir, new_name or "")
    rows, info = refresh_history(d)
    # rename_project 已同步 db 路径：重新取记录，按新路径重填播放器
    entry = d.history_mgr.get(selected_state[0]) or entry
    audio = entry.audio_path if entry.audio_path and Path(entry.audio_path).exists() else None
    return rows, info, f"{tr(d.cur_lang, '已重命名')} {n} {tr(d.cur_lang, '个文件')}", [], 0, audio


def on_history_delete_project(d: HistoryDeps, selected_state):
    """删除项目（文件管理重构）：选中记录所在的整个产物目录移入回收站。

    同目录的批量变体等多条记录一并移除（目录整体回收，文件可从回收站还原）。
    返回末尾三元组清空播放器，避免仍指向已删除目录。
    """
    if not selected_state:
        rows, info = refresh_history(d)
        return rows, info, tr(d.cur_lang, "请先点击选择要删除的记录"), selected_state, 0, *_hist_player_keep()
    entry = d.history_mgr.get(selected_state[0])
    if not entry or not getattr(entry, "output_dir", ""):
        rows, info = refresh_history(d)
        return rows, info, tr(d.cur_lang, "该记录没有产物目录"), selected_state, 0, *_hist_player_keep()
    n = d.history_mgr.delete_project(entry.output_dir)
    rows, info = refresh_history(d)
    if n <= 0:
        return rows, info, tr(d.cur_lang, "删除失败"), selected_state, 0, *_hist_player_keep()
    return rows, info, f"{tr(d.cur_lang, '已删除项目')} · {n} {tr(d.cur_lang, '条记录')}", [], 0, *_hist_player_clear()
