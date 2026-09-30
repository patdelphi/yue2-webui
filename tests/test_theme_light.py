"""亮色主题适配回归测试（源码级断言）

覆盖内容：
- static/js/app.js 播放器选择器必须用 :is() 包裹：直接写逗号列表再拼 ".timestamps"
  时，只有列表最后一项带后代限定，前几项会命中播放器根节点，把整块播放器染成
  半透明黑（亮色主题下尤为明显）
- 播放器时间码条 / 缩放工具条 / 波形底 / 段落卡改用 Gradio 主题变量，明暗自动切换
- app.py 三处 ABC 预览容器不再使用亮色下不可见的白色虚线边框
- app.py：File 虚线拖拽区不再因 Gradio 重置规则（border-width: medium + currentColor）
  露出 3px 近黑虚线框
- 多轨编辑页主题判定：优先读父页面 body.dark，回退时读 Gradio 主题变量
  --body-background-fill / --background-fill-primary（而非父页 body 背景色），
  并为原生控件（复选框/滚动条）声明 color-scheme

运行方式：pytest tests/test_theme_light.py
"""
from pathlib import Path

WEBUI_DIR = Path(__file__).parent.parent

JS = (WEBUI_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
APP = (WEBUI_DIR / "app.py").read_text(encoding="utf-8")
PAGE = (WEBUI_DIR / "static" / "multitrack" / "index.html").read_text(encoding="utf-8")


def test_player_selectors_are_wrapped_in_is():
    """播放器选择器必须整体包在 :is() 里，否则后代选择器只作用于最后一个选择器。"""
    assert "var SEL = ':is(' + PLAYERS + ')';" in JS
    # 固定 id 的播放器必须与 initPlayerZoom 的 PLAYER_IDS 对齐，漏掉会残留近黑内联边框
    for pid in ("#gen-audio", "#history-audio", "#history-stem-audio", "#lib-stem-preview",
                "#lib-ref-preview", "#cover-ref-preview", "#cover-acc-preview"):
        assert pid in JS
    # 回归根因：这条直接拼逗号列表的写法会让 #gen-audio 等根节点吃下 .timestamps 样式
    assert "var SEL = '#gen-audio, #history-audio" not in JS


def test_player_time_bar_and_toolbar_use_theme_vars():
    """时间码条 / 缩放工具条 / 段落卡不再硬编码深色，改用主题变量。"""
    assert "var(--background-fill-secondary, rgba(0,0,0,0.7))" in JS   # 时间码条底色
    assert "var(--button-secondary-background-fill" in JS               # 缩放工具条按钮
    assert "var(--body-text-color, #fff)" in JS                         # 时间码条文字
    assert "body.dark" in JS                                            # 暗色下单独覆盖读数绿色
    assert "background:rgba(255,255,255,0.05)" not in JS                # 段落卡不再直接用白 5% 底
    assert "background:var(--background-fill-secondary" in JS           # 改走主题变量（兜底值不影响）
    assert "l.style.color = '#fff'" not in JS                           # 逐句高亮不写死白字
    # 播放器外框：Gradio 音频块内联 border-style 无宽度 → 回落 3px currentColor（亮色下近黑框）
    assert "SEL + ' { border: 1px solid var(--border-color-primary, transparent) !important; }'" in JS


def test_player_waveform_colors_follow_theme():
    """缩放播放器的波形配色随主题切换（亮色下白光标在白底不可见）。"""
    assert "cursorColor: isDark ? '#ffffff' : '#1f2328'" in JS
    assert "progressColor: isDark ? '#4ade80' : '#15803d'" in JS
    assert "var isDark = document.body.classList.contains('dark');" in JS


def test_abc_preview_border_uses_theme_var():
    """ABC 预览容器的虚线边框改用主题边框色（亮色下白色虚线不可见）。"""
    assert "rgba(255,255,255,0.15)" not in APP
    assert "border: 1px dashed var(--border-color-primary)" in APP


def test_file_dropzone_border_is_normalized():
    """File 虚线拖拽区：Gradio 重置规则给 medium(3px)+currentColor，需压回 1px 主题边框色。"""
    assert 'div.styler > :not(.absolute)[style*="dashed"]' in APP
    assert "border-width: 1px !important; border-color: var(--border-color-primary) !important;" in APP


def test_multitrack_theme_detection_is_robust():
    """多轨编辑页主题判定：body.dark 优先 + 主题变量亮度（不用父页 body 背景色）。"""
    assert 'pbody.classList.contains("dark")' in PAGE
    assert 'varOf("--body-background-fill")' in PAGE     # 真正跟随主题的变量
    assert 'varOf("--background-fill-primary")' in PAGE
    assert "const lumOfColor" in PAGE                     # 色值统一交给 canvas 归一化
    assert "const sentinel = \"#010203\";" in PAGE        # 非法色值需判无效，不能当作纯黑
    assert "color-scheme: dark" in PAGE and "color-scheme: light" in PAGE
