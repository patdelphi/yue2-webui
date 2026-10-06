"""UI 构建模块（C1 拆分 app.py：Phase 2 · UI 组）。

原 app.py 的三段（_TITLE_ROW_CSS / _LOCALE_SYNC_JS / build_ui）原样搬入本模块。
build_ui 采用「调用时注入 app 模块命名空间」的方式：搬迁代码里对 app.py 顶层名字的
裸引用（tr / _CUR_LANG / on_generate / history_mgr / ...）在调用时从 app 模块注入到
本模块 globals()，因此无需逐处加前缀。本模块不 import app.py（避免循环依赖）。
"""
import gradio as gr

from i18n import normalize_lang

# 标题行紧凑样式：语言下拉框伪装为原生控件（无组件外框），配色用主题变量自动适配明暗
# 关键点：Gradio 给 .block 设了 width:100%，而 flex-basis:auto 会回退读 width，
# 因此必须显式 width:auto 才能让 hint/dd 按内容收缩，否则各自撑满整行换行堆叠。
_TITLE_ROW_CSS = """
#title-row { align-items: center; gap: 8px; }
    /* GitHub 图标：随主题色，hover 亮起；flex 收缩避免撑满整行 */
    #gh-icon { width: auto; flex: 0 0 auto; display: flex; align-items: center; margin: 0; }
    #gh-link { display: inline-flex; align-items: center; color: var(--body-text-color); opacity: .65; transition: opacity .15s; }
    #gh-link:hover { opacity: 1; }
    #lang-hint { width: auto; display: flex; align-items: center; margin: 0; flex: 0 0 auto; }
#lang-hint label { font-size: 13px; color: var(--body-text-color); opacity: .75; white-space: nowrap; }
#lang-dd { width: auto; flex: 0 0 auto; margin: 0; }
#lang-dd > div { display: flex; align-items: center; height: 26px; }
#lang-dd .wrap { border: 1px solid var(--border-color-primary); border-radius: 6px; background: var(--background-fill-primary); box-shadow: none; min-height: 26px; padding: 0 2px 0 8px; }
#lang-dd .wrap-inner, #lang-dd .secondary-wrap { min-height: 24px; }
#lang-dd input { font-size: 12px; min-height: 24px; height: 24px; width: 80px; }
#lang-dd .icon-wrap { padding: 0 4px; }
#lang-dd .icon-wrap svg { width: 12px; height: 12px; }
#lang-signal { display: none; }
    /* —— 改名/库管理行：统一结构 label | 输入框 | 主动作按钮 | 删除按钮 ——
       做法：把 Gradio Textbox 的原生 <label> 由纵向改为横向 flex，
       label 文字固定宽度，从而各行输入框/按钮起点完全对齐。 */
    #sep-rename-row, #cover-rename-row, #hist-rename-row,
    #lib-stem-row, #lib-ref-row {
        flex-wrap: nowrap !important; gap: 10px !important; align-items: center !important;
    }
    /* Textbox 外层 block 去内边距，高度贴合输入框，便于与按钮垂直居中 */
    #sep-rename-row .block, #cover-rename-row .block, #hist-rename-row .block,
    #lib-stem-row .block, #lib-ref-row .block { padding: 0 !important; }
    /* 原生 label 改为横向 flex：文字在左，输入框在右，同一行垂直居中 */
    #sep-rename-row label.container, #cover-rename-row label.container,
    #hist-rename-row label.container, #lib-stem-row label.container,
    #lib-ref-row label.container {
        display: flex !important; flex-direction: row !important;
        align-items: center !important; gap: 10px !important;
        border: none !important; padding: 0 0 0 12px !important;
    }
    /* label 文字：固定宽度，保证多行左对齐一致 */
    #sep-rename-row label.container > span[data-testid="block-info"],
    #cover-rename-row label.container > span[data-testid="block-info"],
    #hist-rename-row label.container > span[data-testid="block-info"],
    #lib-stem-row label.container > span[data-testid="block-info"],
    #lib-ref-row label.container > span[data-testid="block-info"] {
        flex: 0 0 76px !important; margin: 0 !important;
        white-space: nowrap !important; font-size: 14px !important;
    }
    /* 输入框容器占满剩余宽度，边框移到这里 */
    #sep-rename-row .input-container, #cover-rename-row .input-container,
    #hist-rename-row .input-container, #lib-stem-row .input-container,
    #lib-ref-row .input-container {
        flex: 1 1 auto !important;
        border: 1px solid var(--border-color-primary) !important;
        border-radius: 6px !important;
        background: var(--background-fill-primary) !important;
    }
    /* Textarea 单行高度 */
    #sep-rename-row textarea, #cover-rename-row textarea, #hist-rename-row textarea,
    #lib-stem-row textarea, #lib-ref-row textarea {
        min-height: 38px !important; height: 38px !important;
    }
    /* 按钮缩小 */
    #sep-rename-row button, #cover-rename-row button, #hist-rename-row button,
    #lib-stem-row button, #lib-ref-row button {
        flex: 0 0 auto !important; min-width: auto !important; padding: 4px 14px !important;
    }
    /* ==========================================================================
       YuE2 统一视觉语言（对齐多轨编辑器的设计令牌）
       作用域：全站；任何页面的区块写成 gr.Group(elem_classes=["y2-sec"]) 即成卡片
       要点：
       - 令牌统一到 Gradio 主题变量，明暗主题自动适配，禁止写死颜色；
       - 卡片：1px 描边 + 大圆角 + 微阴影；标题复用 Markdown 的 ###（渲染为 h3）；
       - 行内工具条（y2-toolrow）与动作条（y2-actions）用紧凑按钮，避免撑满整行；
       - 不改动尺寸类属性（高度/内边距）作用于播放器与上传组件，避免破坏自定义播放器。
       注：令牌必须声明在 .gradio-container（不能放 :root）——var() 在声明元素处完成
       替换后继承，:root 处解析不到 Gradio 主题变量，深色主题会整体失效。
       ========================================================================== */
    .gradio-container {
        --y2-sp-2: 8px; --y2-sp-3: 12px;
        --y2-r-lg: 14px; --y2-r-md: 10px; --y2-r-sm: 7px;
        --y2-h-ctl: 30px;
        --y2-line: var(--border-color-primary);
        /* 卡片底色用 secondary：深色主题下 primary 与页面底色相同（#0f0f11），
           卡片会失去层次，故改用略亮的 secondary 形成"浮起"效果 */
        --y2-card: var(--background-fill-secondary);
        --y2-muted: color-mix(in srgb, var(--body-text-color) 55%, transparent);
        --y2-shadow-sm: var(--shadow-drop);
        --y2-ring: 0 0 0 3px color-mix(in srgb, var(--color-accent) 24%, transparent);
    }
    /* —— 区块卡片 —— */
    .y2-sec {
        border: 1px solid var(--y2-line) !important;
        border-radius: var(--y2-r-lg) !important;
        background: var(--y2-card) !important;
        padding: 14px 16px !important;
        box-shadow: var(--y2-shadow-sm) !important;
        display: flex !important; flex-direction: column !important;
        gap: var(--y2-sp-3) !important; margin-bottom: var(--y2-sp-3) !important;
    }
    /* gr.Group 在 Gradio 6 中渲染为两层嵌套 div 且两层都带 elem_classes，
       若不在内层清零会出现"卡片套卡片"（边框/内边距翻倍）。此处把内层还原为透明容器 */
    .y2-sec .y2-sec {
        border: 0 !important; border-radius: 0 !important; background: transparent !important;
        padding: 0 !important; box-shadow: none !important; margin-bottom: 0 !important;
    }
    /* —— 卡片内层容器的间距与底色（关键修复）——
       Gradio 6 的 gr.Group 渲染为：外层 gr-group.y2-sec（卡片）→ 内层 gr-group.y2-sec
       → .styler（真正的内容容器）→ 各组件。该 .styler 上 Gradio 原生规则为
       `background: var(--border-color-primary); gap: var(--form-gap-width)`，同时
       gr.Group 还会向内联注入 `--layout-gap: 1px; --form-gap-width: 1px`。后果：
       1) 深色主题 --border-color-primary = #3f3f46，整张卡片内部被铺成灰底，组件之间的
          负空间露出一条条灰带（"使用上一次"那一整条灰带即由此而来）；
       2) 容器子项间距、行/列间距被压到 1px（组件贴死、文字贴边）。
       内联值只能被带 !important 的样式表规则覆盖，故在此显式重声明。 */
    .y2-sec .styler {
        background: transparent !important;        /* 去掉灰底，露出卡片自身底色 */
        gap: var(--y2-sp-3) !important;            /* 卡片内子项间距 */
        --layout-gap: var(--y2-sp-3) !important;   /* 卡片内行/列间距 */
        --form-gap-width: 0px !important;          /* label 与控件贴紧，由控件自身内边距撑开 */
    }
    /* 窄窗口下 Gradio 给列设了 min-width: min(320px,100%)（内联），
       卡片内两列会因此撑破卡片右边界，故允许其收缩 */
    .y2-sec .row > .column { min-width: 0 !important; }
    /* "使用上一次"按钮行：右对齐、按钮按内容宽度，不再贴边 */
    .y2-sec .last-btn-row { justify-content: flex-end !important; margin: 0 !important; }
    /* —— 卡片标题（Markdown ### -> h3） —— */
    .y2-sec h3 {
        font-size: 13px !important; font-weight: 600 !important; line-height: 1.4 !important;
        margin: 0 !important; padding: 0 0 8px !important; letter-spacing: .01em !important;
        border-bottom: 1px solid var(--y2-line) !important;
        color: var(--body-text-color) !important;
    }
    /* —— 卡片内竖向留白收紧，避免出现大片空白 —— */
    .y2-sec > .form { margin: 0 !important; }
    .y2-sec .block.padded { padding-top: 6px !important; padding-bottom: 6px !important; }
    /* 单选组保留横向内边距，避免选项贴边 */
    .y2-sec fieldset.block.padded { padding: 6px 10px !important; }
    .y2-sec .prose > * + * { margin-top: 0 !important; }
    /* —— 控件圆角与聚焦光圈统一（仅几何与光圈，不改尺寸） —— */
    .y2-sec :is(input, textarea, .wrap, .input-container, fieldset) {
        border-radius: var(--y2-r-sm) !important;
    }
    .y2-sec :is(.wrap, .input-container, label.container):focus-within {
        border-color: var(--color-accent) !important; box-shadow: var(--y2-ring) !important;
    }
    /* —— 行内工具条：翻页等，按钮紧凑、信息居中 —— */
    .y2-toolrow {
        flex-wrap: nowrap !important; align-items: center !important; gap: var(--y2-sp-2) !important;
    }
    .y2-toolrow > .block:not(button) {
        flex: 1 1 auto !important; min-width: 0 !important; margin: 0 !important; padding: 0 !important;
        border: 0 !important; background: transparent !important; box-shadow: none !important;
        text-align: center !important; color: var(--y2-muted) !important; font-size: 12.5px !important;
    }
    /* 工具条内的按钮同样紧凑（去掉 Gradio 默认 min-width:320px 造成的拉伸） */
    .y2-toolrow button {
        flex: 0 0 auto !important; width: auto !important; min-width: auto !important;
        height: var(--y2-h-ctl) !important; min-height: var(--y2-h-ctl) !important;
        padding: 0 14px !important; font-size: 12.5px !important;
        border-radius: var(--y2-r-sm) !important;
    }
    /* —— 动作条：紧凑按钮，按内容宽度排布 —— */
    .y2-actions { flex-wrap: wrap !important; align-items: center !important; gap: var(--y2-sp-2) !important; }
    /* "使用上一次"这类行尾按钮与动作条按钮统一规格（高度/字号/内边距/圆角） */
    .y2-actions button,
    .y2-sec .last-btn-row button {
        flex: 0 0 auto !important; width: auto !important; min-width: auto !important;
        height: var(--y2-h-ctl) !important; min-height: var(--y2-h-ctl) !important;
        padding: 0 14px !important; font-size: 12.5px !important;
        border-radius: var(--y2-r-sm) !important;
        transition: background .15s, border-color .15s, box-shadow .15s;
    }
    .y2-actions button:hover:not(.primary):not(.stop),
    .y2-sec .last-btn-row button:hover:not(.primary):not(.stop) {
        border-color: var(--color-accent) !important; background: var(--color-accent-soft) !important;
    }
    .y2-actions button:focus-visible,
    .y2-sec .last-btn-row button:focus-visible { outline: none !important; box-shadow: var(--y2-ring) !important; }
    /* —— 历史表格：外框 + 圆角，与卡片层次统一 —— */
    #history-table { border: 1px solid var(--y2-line) !important; border-radius: var(--y2-r-md) !important; overflow: hidden !important; }
    /* —— Gradio 6 块重置规则踩坑 ——
       Gradio 自带规则 `div.styler > :not(.absolute)` 把子块写成
       border-width: medium(3px) + border-color: currentColor（本意是配合 border-style: none 做重置）。
       但 File 组件会内联 `border-style: dashed`，于是宽度回落到 3px、颜色取 currentColor，
       亮色主题下露出一圈 3px 近黑虚线框。这里把虚线拖拽区压回 1px 主题边框色。 */
    div.styler > :not(.absolute)[style*="dashed"] {
        border-width: 1px !important; border-color: var(--border-color-primary) !important;
    }
"""

# Gradio 内置文案（上传组件"将音频拖放到此处/点击上传"、页脚等）跟随浏览器 locale，
# 后端无参数可控制。此处通过隐藏信号组件 #lang-signal（值=zh/en，CSS display:none
# 隐藏而非 visible=False——后者不渲染 DOM，前端将无法监听）+ 本脚本联动：
# 轮询等待信号组件渲染 -> 调用 Gradio 前端内部 changeLocale 切换其内置文案语言。
# core-*.js 文件名带构建 hash，运行时从 <script> 标签或 performance 资源记录动态
# 获取（Gradio 模块多为动态 import，不一定存在于 script 标签），避免硬编码。
# 注意：Gradio 5.x 的 Blocks 级 js= 会被包装为 await (js)(); 要求「函数表达式」，
# 禁止写成 IIFE (function(){...})(); ——尾部分号会导致前端 SyntaxError 并中断组件挂载。
_LOCALE_SYNC_JS = """
() => {
    var GRADIO_LOCALE = { zh: "zh-CN", en: "en" };
    function findCoreModuleUrl() {
        var s = document.querySelector('script[src*="/core-"]');
        if (s) return s.src;
        var res = performance.getEntriesByType('resource').map(function (e) { return e.name; });
        for (var i = 0; i < res.length; i++) {
            if (/\\/core-[^/]+\\.js$/.test(res[i])) return res[i];
        }
        return null;
    }
    function applyGradioLocale(node) {
        var lang = (node.textContent || "").trim();
        var locale = GRADIO_LOCALE[lang];
        if (!locale) return;
        var coreUrl = findCoreModuleUrl();
        if (!coreUrl) return;
        import(coreUrl).then(function (m) {
            if (m && m.changeLocale) m.changeLocale(locale);
        }).catch(function () {});
    }
    // 轮询等待信号组件渲染（最长约 30 秒），出现后先做初始同步，再持续监听语言变化
    var tries = 0;
    var timer = setInterval(function () {
        var node = document.querySelector("#lang-signal");
        tries += 1;
        if (node) {
            clearInterval(timer);
            applyGradioLocale(node);
            new MutationObserver(function () { applyGradioLocale(node); })
                .observe(node, { childList: true, characterData: true, subtree: true });
        } else if (tries > 150) {
            clearInterval(timer);
        }
    }, 200);
}
"""


def build_ui(app_module=None):
    """Build the Gradio UI.

    本函数体原样来自 app.py。搬迁代码引用了大量 app.py 顶层名字，这里在调用时把
    app 模块命名空间注入本模块 globals()，使裸名字引用继续可用。app_module 由
    app.py 薄封装传入(sys.modules[__name__])；缺省时回退从 sys.modules 取 "app"。
    注意：必须排除 build_ui 自身，否则注入会覆盖本函数导致递归。
    """
    if app_module is None:
        import sys as _sys
        app_module = _sys.modules.get("app")
    _app_module = app_module
    globals().update({k: v for k, v in vars(app_module).items()
                      if not k.startswith("__") and k != "build_ui"})
    with gr.Blocks(title="YuE2 Music Studio",
                    css=_TITLE_ROW_CSS, js=_LOCALE_SYNC_JS) as demo:

        def _t(s: str) -> str:
            """按当前界面语言翻译单条文案（zh 直接返回原文）。"""
            return tr(_CUR_LANG, s)

        # 标题行：主标题+副标题 Markdown，右侧原生风格 "Lang/语言" 说明 + 紧凑下拉框
        with gr.Row(elem_id="title-row"):
            title_md = gr.Markdown("### YuE2 Music Studio · " + _t("AI音乐创作 — 输入歌词和风格，生成完整歌曲"))
            # GitHub 图标：点击新窗口打开项目仓库（无文案，语言切换无需注册 updater）
            gr.HTML(
                '<a id="gh-link" href="https://github.com/patdelphi/yue2-webui" target="_blank" rel="noopener" title="GitHub">'
                '<svg viewBox="0 0 16 16" width="17" height="17" aria-hidden="true"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>'
                '</a>',
                elem_id="gh-icon", container=False,
            )
            # 说明文字用原生 HTML label（无 Gradio block 底色）
            gr.HTML('<label>Lang/语言</label>', elem_id="lang-hint", container=False)
            lang_select = gr.Dropdown(
                choices=["中文", "English"], value=("English" if _CUR_LANG == "en" else "中文"),
                show_label=False, container=False,
                elem_id="lang-dd", scale=0, min_width=90,
            )
        # 语言即时切换控件：下拉框选择中文/English，State 保存归一化语言标识。
        # translatables/_updaters 一一对应（同一组件各占一位），保证 apply_lang
        # 返回值数量与 outputs=[lang_state]+translatables 完全一致。
        # 初始值取自 lang_state.json 恢复的 _CUR_LANG，重启后界面语言不回退。
        lang_state = gr.State(_CUR_LANG)
        translatables: list = []
        _updaters: list = []

        def _reg(comp, updater):
            """注册一个可切换语言组件：comp 进 outputs，updater 生成对应 gr.update。"""
            translatables.append(comp)
            _updaters.append(updater)

        def apply_lang(lang):
            """语言切换回调：全局更新 _CUR_LANG 并为每个 translatable 生成 gr.update。"""
            global _CUR_LANG
            nlang = normalize_lang(lang)
            _CUR_LANG = nlang
            # C1 拆分适配：_CUR_LANG 的真实宿主是 app.py 模块全局（其余回调经
            # app._CUR_LANG 读取），故同步写回 app 模块，保持 UI 与后端语言一致。
            if _app_module is not None:
                _app_module._CUR_LANG = nlang
            _save_lang_state(nlang)  # 持久化，服务重启后恢复
            return [nlang] + [u(nlang) for u in _updaters]

        _reg(title_md, lambda lang: gr.update(value="### YuE2 Music Studio · " + tr(lang, "AI音乐创作 — 输入歌词和风格，生成完整歌曲")))

        # 语言信号组件（CSS display:none 隐藏，visible=False 不渲染 DOM 会导致前端无法监听）：
        # 值=当前语言(zh/en)。前端 _LOCALE_SYNC_JS 脚本监听其变化并调用 Gradio 内部
        # changeLocale，同步上传组件/页脚等 Gradio 内置文案语言。
        lang_signal = gr.HTML(value=_CUR_LANG, elem_id="lang-signal")
        _reg(lang_signal, lambda lang: gr.update(value=lang))

        with gr.Tabs():
            tab_create = gr.Tab(_t("歌曲创作"))
            _reg(tab_create, lambda lang: gr.update(label=tr(lang, "歌曲创作")))
            with tab_create:
                with gr.Row():
                    with gr.Column(scale=1):
                        # —— 卡片 1：风格与歌词 ——
                        with gr.Group(elem_classes=["y2-sec"]):
                            style_sec_md = gr.Markdown(_t("### 风格与歌词"))
                            _reg(style_sec_md, lambda lang: gr.update(value=tr(lang, "### 风格与歌词")))
                            style_input = gr.Textbox(
                                label=_t("风格描述"),
                                placeholder="English, warm piano pop, expressive female voice, acoustic piano, 88 BPM",
                                lines=2,
                                info=_t("语言 + 流派 + 乐器 + 人声 + 速度"),
                            )
                            _reg(style_input, lambda lang: gr.update(label=tr(lang, "风格描述"), info=tr(lang, "语言 + 流派 + 乐器 + 人声 + 速度")))
                            with gr.Row(elem_classes="last-btn-row"):
                                last_style_btn = gr.Button(_t("使用上一次"), size="sm", scale=0, min_width=110)
                                _reg(last_style_btn, lambda lang: gr.update(value=tr(lang, "使用上一次")))

                            style_tag_acc = gr.Accordion(_t("风格标签"), open=False)
                            _reg(style_tag_acc, lambda lang: gr.update(label=tr(lang, "风格标签")))
                            with style_tag_acc:
                                quick_tags_md = gr.Markdown(_t("#### 风格快捷标签"))
                                _reg(quick_tags_md, lambda lang: gr.update(value=tr(lang, "#### 风格快捷标签")))
                                preset_names = list(STYLE_PRESETS.keys())
                                half = len(preset_names) // 2
                                with gr.Row():
                                    for name in preset_names[:half]:
                                        btn = gr.Button(name, size="sm")
                                        btn.click(fn=lambda n=name: on_style_preset(n), outputs=style_input)
                                with gr.Row():
                                    for name in preset_names[half:]:
                                        btn = gr.Button(name, size="sm")
                                        btn.click(fn=lambda n=name: on_style_preset(n), outputs=style_input)

                                vocal_acc = gr.Accordion(_t("人声标签"), open=False)
                                _reg(vocal_acc, lambda lang: gr.update(label=tr(lang, "人声标签")))
                                with vocal_acc:
                                    vocal_names = list(VOCAL_PRESETS.keys())
                                    half_vocal = len(vocal_names) // 2
                                    with gr.Row():
                                        for name in vocal_names[:half_vocal]:
                                            btn = gr.Button(name, size="sm")
                                            btn.click(fn=lambda current, n=name: on_vocal_preset(current, n), inputs=style_input, outputs=style_input)
                                    with gr.Row():
                                        for name in vocal_names[half_vocal:]:
                                            btn = gr.Button(name, size="sm")
                                            btn.click(fn=lambda current, n=name: on_vocal_preset(current, n), inputs=style_input, outputs=style_input)

                                inst_acc = gr.Accordion(_t("乐器标签"), open=False)
                                _reg(inst_acc, lambda lang: gr.update(label=tr(lang, "乐器标签")))
                                with inst_acc:
                                    inst_names = list(INSTRUMENT_PRESETS.keys())
                                    half_inst = len(inst_names) // 2
                                    with gr.Row():
                                        for name in inst_names[:half_inst]:
                                            btn = gr.Button(name, size="sm")
                                            btn.click(fn=lambda current, n=name: on_instrument_preset(current, n), inputs=style_input, outputs=style_input)
                                    with gr.Row():
                                        for name in inst_names[half_inst:]:
                                            btn = gr.Button(name, size="sm")
                                            btn.click(fn=lambda current, n=name: on_instrument_preset(current, n), inputs=style_input, outputs=style_input)

                                mood_acc = gr.Accordion(_t("情绪标签"), open=False)
                                _reg(mood_acc, lambda lang: gr.update(label=tr(lang, "情绪标签")))
                                with mood_acc:
                                    mood_names = list(MOOD_PRESETS.keys())
                                    half_mood = len(mood_names) // 2
                                    with gr.Row():
                                        for name in mood_names[:half_mood]:
                                            btn = gr.Button(name, size="sm")
                                            btn.click(fn=lambda current, n=name: on_mood_preset(current, n), inputs=style_input, outputs=style_input)
                                    with gr.Row():
                                        for name in mood_names[half_mood:]:
                                            btn = gr.Button(name, size="sm")
                                            btn.click(fn=lambda current, n=name: on_mood_preset(current, n), inputs=style_input, outputs=style_input)

                                lang_tag_acc = gr.Accordion(_t("语言标签"), open=False)
                                _reg(lang_tag_acc, lambda lang: gr.update(label=tr(lang, "语言标签")))
                                with lang_tag_acc:
                                    lang_names = list(LANGUAGE_PRESETS.keys())
                                    half_lang = len(lang_names) // 2
                                    with gr.Row():
                                        for name in lang_names[:half_lang]:
                                            btn = gr.Button(name, size="sm")
                                            btn.click(fn=lambda current, n=name: on_language_preset(current, n), inputs=style_input, outputs=style_input)
                                    with gr.Row():
                                        for name in lang_names[half_lang:]:
                                            btn = gr.Button(name, size="sm")
                                            btn.click(fn=lambda current, n=name: on_language_preset(current, n), inputs=style_input, outputs=style_input)

                                genre_acc = gr.Accordion(_t("流派标签"), open=False)
                                _reg(genre_acc, lambda lang: gr.update(label=tr(lang, "流派标签")))
                                with genre_acc:
                                    genre_names = list(GENRE_PRESETS.keys())
                                    half_genre = len(genre_names) // 2
                                    with gr.Row():
                                        for name in genre_names[:half_genre]:
                                            btn = gr.Button(name, size="sm")
                                            btn.click(fn=lambda current, n=name: on_genre_preset(current, n), inputs=style_input, outputs=style_input)
                                    with gr.Row():
                                        for name in genre_names[half_genre:]:
                                            btn = gr.Button(name, size="sm")
                                            btn.click(fn=lambda current, n=name: on_genre_preset(current, n), inputs=style_input, outputs=style_input)

                            lyrics_input = gr.Textbox(
                                label=_t("歌词"),
                                placeholder=f"[Verse]\n{_t('在这里输入歌词...')}\n\n[Chorus]\n{_t('副歌歌词...')}",
                                lines=10,
                                info=_t("支持 [Verse] [Chorus] [Bridge] 段落标记，可拖拽排序"),
                            )
                            _reg(lyrics_input, lambda lang: gr.update(label=tr(lang, "歌词"), placeholder=f"[Verse]\n{tr(lang, '在这里输入歌词...')}\n\n[Chorus]\n{tr(lang, '副歌歌词...')}", info=tr(lang, "支持 [Verse] [Chorus] [Bridge] 段落标记，可拖拽排序")))
                            with gr.Row(elem_classes="last-btn-row"):
                                last_lyrics_btn = gr.Button(_t("使用上一次"), size="sm", scale=0, min_width=110)
                                _reg(last_lyrics_btn, lambda lang: gr.update(value=tr(lang, "使用上一次")))

                            lyrics_tools_acc = gr.Accordion(_t("歌词工具"), open=False)
                            _reg(lyrics_tools_acc, lambda lang: gr.update(label=tr(lang, "歌词工具")))
                            with lyrics_tools_acc:
                                with gr.Row(elem_classes=["y2-actions"]):
                                    gr.Button("+ Verse", size="sm")
                                    gr.Button("+ Chorus", size="sm")
                                    gr.Button("+ Bridge", size="sm")
                                    gr.Button("+ Intro", size="sm")
                                    gr.Button("+ Outro", size="sm")
                                    gr.Button("+ Pre-Chorus", size="sm")

                                # 段落标记说明表格：按语言组装，切语言时由 _reg 重新生成
                                def _section_notes_md(t):
                                    return (
                                        f"{t('#### 段落标记说明')}\n"
                                        f"| {t('标记')} | {t('用途')} |\n"
                                        "| --- | --- |\n"
                                        f"| [Intro] | {t('前奏/器乐引入')} |\n"
                                        f"| [Verse] | {t('主歌段落')} |\n"
                                        f"| [Pre-Chorus] | {t('预副歌，制造期待感')} |\n"
                                        f"| [Chorus] | {t('副歌，全曲最抓耳的部分')} |\n"
                                        f"| [Bridge] | {t('桥段，打破重复，情感转折')} |\n"
                                        f"| [Outro] | {t('尾声/渐弱收尾')} |\n\n"
                                        f"{t('注释行：以 `//` 或 `**` 开头的行视为注释，不会送入模型生成。')}"
                                    )

                                section_notes_md = gr.Markdown(_section_notes_md(_t))
                                _reg(section_notes_md, lambda lang: gr.update(value=_section_notes_md(lambda k: tr(lang, k))))

                                structure_tpl_md = gr.Markdown(_t("#### 歌曲结构模板"))
                                _reg(structure_tpl_md, lambda lang: gr.update(value=tr(lang, "#### 歌曲结构模板")))
                                with gr.Row():
                                    gr.Button("Verse-Chorus", size="sm")
                                    gr.Button("V-C-V-C", size="sm")
                                    gr.Button("V-C-V-C-B-C", size="sm")
                                    gr.Button("V-V-C", size="sm")
                                    gr.Button("A-A-B-A", size="sm")

                                segment_cards = gr.HTML(
                                    label=_t("段落拖拽排序"),
                                    value='<div id="segment-cards" style="padding:4px 0;"></div>',
                                )
                                structure_analysis = gr.HTML(
                                    label=_t("结构分析"),
                                    value=f'<div id="lyrics-structure" style="padding:4px 8px;color:#888;">{_t("输入歌词后显示结构分析")}</div>',
                                )
                                _reg(structure_analysis, lambda lang: gr.update(value=f'<div id="lyrics-structure" style="padding:4px 8px;color:#888;">{tr(lang, "输入歌词后显示结构分析")}</div>'))

                                template_dropdown = gr.Dropdown(
                                    label=_t("歌词模板 (内容)"),
                                    choices=list(LYRICS_TEMPLATES.keys()),
                                    value=None,
                                    info=_t("选择模板将填充歌词内容（覆盖现有内容）"),
                                )
                                _reg(template_dropdown, lambda lang: gr.update(label=tr(lang, "歌词模板 (内容)"), info=tr(lang, "选择模板将填充歌词内容（覆盖现有内容）")))
                                template_dropdown.change(fn=on_lyrics_template, inputs=template_dropdown, outputs=lyrics_input)

                        # —— 卡片 2：工作模式 ——
                        with gr.Group(elem_classes=["y2-sec"]):
                            workmode_md = gr.Markdown(_t("### 工作模式"))
                            _reg(workmode_md, lambda lang: gr.update(value=tr(lang, "### 工作模式")))
                            cot_input = gr.Radio(
                                label=_t("模式"),
                                choices=[
                                    (_t("完整创作 (生成乐谱+和弦)"), "full"),
                                    (_t("旋律创作 (仅旋律，适合翻唱)"), "melody"),
                                    (_t("直接生成 (跳过乐谱，最快)"), "off"),
                                ],
                                value="full",
                            )
                            _reg(cot_input, lambda lang: gr.update(label=tr(lang, "模式"), choices=[(tr(lang, "完整创作 (生成乐谱+和弦)"), "full"), (tr(lang, "旋律创作 (仅旋律，适合翻唱)"), "melody"), (tr(lang, "直接生成 (跳过乐谱，最快)"), "off")]))

                            abc_input = gr.Textbox(
                                label=_t("ABC 乐谱 (外部输入)"),
                                placeholder="X:1\nM:4/4\nL:1/16\nK:C\n...",
                                lines=8,
                                visible=True,
                                info=_t("提供外部ABC乐谱文本。仅在 full/melody 模式下生效。留空则自动生成。"),
                            )
                            _reg(abc_input, lambda lang: gr.update(label=tr(lang, "ABC 乐谱 (外部输入)"), info=tr(lang, "提供外部ABC乐谱文本。仅在 full/melody 模式下生效。留空则自动生成。")))
                            with gr.Row(elem_classes="last-btn-row"):
                                last_abc_btn = gr.Button(_t("使用上一次"), size="sm", scale=0, min_width=110)
                                _reg(last_abc_btn, lambda lang: gr.update(value=tr(lang, "使用上一次")))
                            cot_input.change(fn=on_cot_change, inputs=cot_input, outputs=[abc_input, last_abc_btn])

                            last_style_btn.click(fn=lambda cur: on_restore_last("style", cur), inputs=style_input, outputs=style_input)
                            last_lyrics_btn.click(fn=lambda cur: on_restore_last("lyrics", cur), inputs=lyrics_input, outputs=lyrics_input)
                            # 恢复上次乐谱：off（直接生成）模式下 ABC 输入框隐藏，
                            # 恢复到乐谱时自动切回 full 以显示输入框（cot 值变化触发 on_cot_change 联动显示）
                            def _restore_last_abc(cur_abc, cur_cot):
                                value = on_restore_last("abc", cur_abc)
                                return value, ("full" if (value and cur_cot == "off") else cur_cot)
                            last_abc_btn.click(fn=_restore_last_abc, inputs=[abc_input, cot_input], outputs=[abc_input, cot_input])

                        # —— 卡片 3：生成参数 ——
                        with gr.Group(elem_classes=["y2-sec"]):
                            genparam_md = gr.Markdown(_t("### 生成参数"))
                            _reg(genparam_md, lambda lang: gr.update(value=tr(lang, "### 生成参数")))
                            # 项目名（文件管理重构）：产物文件名前缀 <项目名>_<时间戳>，留空则仅时间戳
                            project_input = gr.Textbox(
                                label=_t("项目名 (可选)"),
                                placeholder=_t("例如: 夜曲demo"),
                                info=_t("产物文件名 = 项目名_时间戳；留空则仅用时间戳"),
                            )
                            _reg(project_input, lambda lang: gr.update(
                                label=tr(lang, "项目名 (可选)"),
                                placeholder=tr(lang, "例如: 夜曲demo"),
                                info=tr(lang, "产物文件名 = 项目名_时间戳；留空则仅用时间戳")))

                            with gr.Row():
                                seed_input = gr.Number(label=_t("随机种子"), value=831001, precision=0, info=_t("勾选「随机种子变化」时每次生成自动换新，此处显示实际使用的种子"))
                                _reg(seed_input, lambda lang: gr.update(label=tr(lang, "随机种子"), info=tr(lang, "勾选「随机种子变化」时每次生成自动换新，此处显示实际使用的种子")))
                                random_seed_btn = gr.Button(_t("🎲 随机"), size="sm")
                                _reg(random_seed_btn, lambda lang: gr.update(value=tr(lang, "🎲 随机")))
                            random_seed_checkbox = gr.Checkbox(label=_t("随机种子变化"), value=True, info=_t("勾选: 每次点击「生成歌曲」自动换新种子; 取消勾选: 使用上方固定种子"))
                            _reg(random_seed_checkbox, lambda lang: gr.update(label=tr(lang, "随机种子变化"), info=tr(lang, "勾选: 每次点击「生成歌曲」自动换新种子; 取消勾选: 使用上方固定种子")))

                            cfg_input = gr.Slider(
                                label=_t("CFG 引导强度"),
                                minimum=0, maximum=20, step=0.1, value=0,
                                info=_t("0=Auto (off模式=1.01, 其他=1.0)"),
                            )
                            _reg(cfg_input, lambda lang: gr.update(label=tr(lang, "CFG 引导强度"), info=tr(lang, "0=Auto (off模式=1.01, 其他=1.0)")))

                            steps_input = gr.Slider(
                                label=_t("ODE 求解步数"),
                                minimum=1, maximum=64, step=1, value=8,
                                info=_t("8=快速, 16=标准, 32=高质量"),
                            )
                            _reg(steps_input, lambda lang: gr.update(label=tr(lang, "ODE 求解步数"), info=tr(lang, "8=快速, 16=标准, 32=高质量")))

                            out_format_input = gr.Dropdown(
                                label=_t("输出格式"),
                                choices=[
                                    (_t("PCM 16-bit (标准)"), "pcm16"),
                                    (_t("PCM 24-bit (高动态)"), "pcm24"),
                                    (_t("Float 32-bit (最大动态)"), "float32"),
                                ],
                                value="pcm16",
                                info=_t("PCM16=标准质量, PCM24=更高动态范围, Float32=最大动态范围(文件更大)"),
                            )
                            _reg(out_format_input, lambda lang: gr.update(label=tr(lang, "输出格式"), choices=[(tr(lang, "PCM 16-bit (标准)"), "pcm16"), (tr(lang, "PCM 24-bit (高动态)"), "pcm24"), (tr(lang, "Float 32-bit (最大动态)"), "float32")], info=tr(lang, "PCM16=标准质量, PCM24=更高动态范围, Float32=最大动态范围(文件更大)")))

                            batch_count_input = gr.Slider(
                                label=_t("批量生成数量"),
                                minimum=1, maximum=10, step=1, value=1,
                                info=_t("一次生成多个变体 (每个变体使用独立随机种子)"),
                            )
                            _reg(batch_count_input, lambda lang: gr.update(label=tr(lang, "批量生成数量"), info=tr(lang, "一次生成多个变体 (每个变体使用独立随机种子)")))

                            postprocess_acc = gr.Accordion(_t("音频后处理"), open=False)
                            _reg(postprocess_acc, lambda lang: gr.update(label=tr(lang, "音频后处理")))
                            with postprocess_acc:
                                postopt_md = gr.Markdown(_t("#### 后处理选项"))
                                _reg(postopt_md, lambda lang: gr.update(value=tr(lang, "#### 后处理选项")))
                                with gr.Row():
                                    normalize_checkbox = gr.Checkbox(label=_t("音量标准化"), value=True, info=_t("归一化到 -1dB"))
                                    _reg(normalize_checkbox, lambda lang: gr.update(label=tr(lang, "音量标准化"), info=tr(lang, "归一化到 -1dB")))
                                    fade_checkbox = gr.Checkbox(label=_t("淡入淡出"), value=True, info=_t("首尾各 0.5 秒"))
                                    _reg(fade_checkbox, lambda lang: gr.update(label=tr(lang, "淡入淡出"), info=tr(lang, "首尾各 0.5 秒")))
                                    trim_checkbox = gr.Checkbox(label=_t("裁剪静音"), value=False, info=_t("移除首尾静音 (< -40dB)"))
                                    _reg(trim_checkbox, lambda lang: gr.update(label=tr(lang, "裁剪静音"), info=tr(lang, "移除首尾静音 (< -40dB)")))
                                with gr.Row():
                                    metadata_checkbox = gr.Checkbox(label=_t("嵌入元数据"), value=True, info=_t("标题/风格/种子"))
                                    _reg(metadata_checkbox, lambda lang: gr.update(label=tr(lang, "嵌入元数据"), info=tr(lang, "标题/风格/种子")))

                            advanced_acc = gr.Accordion(_t("高级采样参数"), open=True)
                            _reg(advanced_acc, lambda lang: gr.update(label=tr(lang, "高级采样参数")))
                            with advanced_acc:
                                abc_stage1_md = gr.Markdown(_t("#### ABC 乐谱采样 (Stage 1)"))
                                _reg(abc_stage1_md, lambda lang: gr.update(value=tr(lang, "#### ABC 乐谱采样 (Stage 1)")))
                                with gr.Row():
                                    abc_temp_input = gr.Slider(label=_t("ABC 温度"), minimum=0, maximum=5, step=0.1, value=0.7)
                                    abc_top_p_input = gr.Slider(label="ABC Top-P", minimum=0, maximum=1, step=0.01, value=0.9)
                                    abc_top_k_input = gr.Slider(label="ABC Top-K", minimum=1, maximum=500, step=1, value=30)
                                with gr.Row():
                                    abc_rep_input = gr.Slider(label=_t("ABC 重复惩罚"), minimum=0.001, maximum=3, step=0.001, value=1.005)
                                    abc_pen_window_input = gr.Slider(label=_t("ABC 惩罚窗口"), minimum=1, maximum=100, step=1, value=100)
                                with gr.Row():
                                    abc_min_tok_input = gr.Slider(label="ABC Min Tokens", minimum=0, maximum=8192, step=1, value=32)
                                    abc_max_tok_input = gr.Slider(label="ABC Max Tokens", minimum=1, maximum=8192, step=1, value=4096)
                                _reg(abc_temp_input, lambda lang: gr.update(label=tr(lang, "ABC 温度")))
                                _reg(abc_rep_input, lambda lang: gr.update(label=tr(lang, "ABC 重复惩罚")))
                                _reg(abc_pen_window_input, lambda lang: gr.update(label=tr(lang, "ABC 惩罚窗口")))

                                sem_stage2_md = gr.Markdown(_t("#### 语义 Token 采样 (Stage 2)"))
                                _reg(sem_stage2_md, lambda lang: gr.update(value=tr(lang, "#### 语义 Token 采样 (Stage 2)")))
                                with gr.Row():
                                    sem_temp_input = gr.Slider(label=_t("语义 温度"), minimum=0, maximum=5, step=0.1, value=1.0)
                                    sem_top_p_input = gr.Slider(label=_t("语义 Top-P"), minimum=0, maximum=1, step=0.01, value=0.95)
                                    sem_top_k_input = gr.Slider(label=_t("语义 Top-K"), minimum=1, maximum=500, step=1, value=100)
                                with gr.Row():
                                    sem_rep_input = gr.Slider(label=_t("语义 重复惩罚"), minimum=0.001, maximum=3, step=0.01, value=1.2)
                                    sem_pen_window_input = gr.Slider(label=_t("语义 惩罚窗口"), minimum=1, maximum=100, step=1, value=50)
                                with gr.Row():
                                    sem_min_tok_input = gr.Slider(label=_t("语义 Min Tokens"), minimum=0, maximum=9000, step=1, value=200)
                                    sem_max_tok_input = gr.Slider(label=_t("语义 Max Tokens"), minimum=1, maximum=9000, step=1, value=9000)
                                _reg(sem_temp_input, lambda lang: gr.update(label=tr(lang, "语义 温度")))
                                _reg(sem_top_p_input, lambda lang: gr.update(label=tr(lang, "语义 Top-P")))
                                _reg(sem_top_k_input, lambda lang: gr.update(label=tr(lang, "语义 Top-K")))
                                _reg(sem_rep_input, lambda lang: gr.update(label=tr(lang, "语义 重复惩罚")))
                                _reg(sem_pen_window_input, lambda lang: gr.update(label=tr(lang, "语义 惩罚窗口")))
                                _reg(sem_min_tok_input, lambda lang: gr.update(label=tr(lang, "语义 Min Tokens")))
                                _reg(sem_max_tok_input, lambda lang: gr.update(label=tr(lang, "语义 Max Tokens")))

                    with gr.Column(scale=1):
                        # —— 卡片 4：输出 ——
                        with gr.Group(elem_classes=["y2-sec"]):
                            output_md = gr.Markdown(_t("### 输出"))
                            _reg(output_md, lambda lang: gr.update(value=tr(lang, "### 输出")))
                            # 主 CTA 保留大号样式：本行不加 y2-actions（避免被统一压到 30px）
                            with gr.Row():
                                generate_btn = gr.Button(_t("🎵 生成歌曲"), variant="primary", size="lg")
                                _reg(generate_btn, lambda lang: gr.update(value=tr(lang, "🎵 生成歌曲")))
                                cancel_btn = gr.Button(_t("取消"), size="lg")
                                _reg(cancel_btn, lambda lang: gr.update(value=tr(lang, "取消")))
                            audio_output = gr.Audio(label=_t("生成的歌曲"), type="filepath", elem_id="gen-audio")
                            _reg(audio_output, lambda lang: gr.update(label=tr(lang, "生成的歌曲")))
                            info_output = gr.Markdown()

                            with gr.Group(visible=False) as variant_group:
                                variant_selector = gr.Radio(label=_t("批量变体选择"), choices=[], interactive=True)
                                _reg(variant_selector, lambda lang: gr.update(label=tr(lang, "批量变体选择")))
                                with gr.Row(elem_classes=["y2-actions"]):
                                    variant_finalize_btn = gr.Button(_t("✅ 选定为最终版"), variant="primary", size="sm")
                                    _reg(variant_finalize_btn, lambda lang: gr.update(value=tr(lang, "✅ 选定为最终版")))
                                    variant_keep_btn = gr.Button(_t("保留全部变体"), size="sm")
                                    _reg(variant_keep_btn, lambda lang: gr.update(value=tr(lang, "保留全部变体")))
                            variant_state = gr.State([])

                        # —— 卡片 5：ABC 乐谱 ——
                        with gr.Group(elem_classes=["y2-sec"]):
                            abc_md = gr.Markdown(_t("### ABC 乐谱"))
                            _reg(abc_md, lambda lang: gr.update(value=tr(lang, "### ABC 乐谱")))
                            gen_abc_acc = gr.Accordion(_t("生成的乐谱 (可编辑)"), open=False)
                            _reg(gen_abc_acc, lambda lang: gr.update(label=tr(lang, "生成的乐谱 (可编辑)")))
                            with gen_abc_acc:
                                abc_output = gr.Textbox(
                                    label=_t("ABC 乐谱文本"),
                                    placeholder="X:1",
                                    lines=10,
                                    interactive=True,
                                    elem_id="gen-abc-output",
                                    info=_t("生成后可编辑乐谱，点击「重新合成」使用修改后的乐谱生成新音频"),
                                )
                                _reg(abc_output, lambda lang: gr.update(label=tr(lang, "ABC 乐谱文本"), info=tr(lang, "生成后可编辑乐谱，点击「重新合成」使用修改后的乐谱生成新音频")))
                            preview_md = gr.Markdown(_t("#### 乐谱预览"))
                            _reg(preview_md, lambda lang: gr.update(value=tr(lang, "#### 乐谱预览")))
                            # 乐谱预览占位容器：提示文案按语言翻译，切语言时更新
                            def _abc_preview_html(container_id, paper_id, audio_id, msg):
                                return (
                                    f'<div id="{container_id}" style="padding: 20px; border-radius: 8px; min-height: 200px; '
                                    f'border: 1px dashed var(--border-color-primary);">'
                                    f'<div style="text-align:center;color:#666;margin-bottom:12px;">{msg}</div>'
                                    f'<div id="{paper_id}"></div><div id="{audio_id}"></div></div>'
                                )

                            gen_abc_preview = gr.HTML(
                                value=_abc_preview_html("abc-preview-container", "abc-paper", "abc-audio", _t("生成歌曲后乐谱将在此处渲染")),
                            )
                            _reg(gen_abc_preview, lambda lang: gr.update(value=_abc_preview_html("abc-preview-container", "abc-paper", "abc-audio", tr(lang, "生成歌曲后乐谱将在此处渲染"))))
                            with gr.Row(elem_classes=["y2-actions"]):
                                export_midi_btn = gr.Button(_t("导出 MIDI"), size="sm")
                                _reg(export_midi_btn, lambda lang: gr.update(value=tr(lang, "导出 MIDI")))
                                export_png_btn = gr.Button(_t("导出 PNG"), size="sm")
                                _reg(export_png_btn, lambda lang: gr.update(value=tr(lang, "导出 PNG")))
                            abc_file_output = gr.File(label=_t("下载乐谱"))
                            _reg(abc_file_output, lambda lang: gr.update(label=tr(lang, "下载乐谱")))
                            flac_file_output = gr.File(label=_t("下载 MP3"))
                            _reg(flac_file_output, lambda lang: gr.update(label=tr(lang, "下载 MP3")))
                            lyrics_sync_data = gr.HTML(value="", visible=False)
                            with gr.Row(elem_classes=["y2-actions"]):
                                resynthesize_btn = gr.Button(_t("重新合成"), variant="secondary")
                                _reg(resynthesize_btn, lambda lang: gr.update(value=tr(lang, "重新合成")))

            with gr.Tab(_t("歌曲历史"), elem_id="tab-history") as tab_history:
                _reg(tab_history, lambda lang: gr.update(label=tr(lang, "歌曲历史")))
                # —— 区块 1：生成历史（标题 + 表格 + 翻页） ——
                with gr.Group(elem_classes=["y2-sec"]):
                    history_md = gr.Markdown(_t("### 生成历史"))
                    _reg(history_md, lambda lang: gr.update(value=tr(lang, "### 生成历史")))
                    history_state = gr.State(value=[])
                    history_page = gr.State(value=0)
                    history_df = gr.Dataframe(
                        # 列头采用中英双语（Gradio 静态表格的 headers 不支持运行时切换）
                        headers=["时间 Time", "项目名 Project", "风格 Style", "模式 Mode", "音频时长 Duration", "生成耗时 Elapsed", "Task ID"],
                        datatype=["str", "str", "str", "str", "str", "str", "str"],
                        col_count=7,
                        max_height=500,
                        interactive=False,
                        value=refresh_history()[0],
                        elem_id="history-table",
                    )
                    with gr.Row(elem_classes=["y2-toolrow"]):
                        history_prev_btn = gr.Button(_t("上一页"), size="sm")
                        _reg(history_prev_btn, lambda lang: gr.update(value=tr(lang, "上一页")))
                        history_page_info = gr.Markdown(value=refresh_history()[1], elem_id="history-page-info")
                        history_next_btn = gr.Button(_t("下一页"), size="sm")
                        _reg(history_next_btn, lambda lang: gr.update(value=tr(lang, "下一页")))
                # —— 区块 2：记录详情（试听 + 歌词/乐谱） ——
                # 注：本页表格只列 generation 记录（见 to_dataframe_rows 过滤），而轨道回放只对
                # separation/cover 有意义，故此处不再放「轨道回放」下拉与播放器（分离/翻唱的
                # 逐轨回放分别在「音轨分离」「音色翻唱」两页各自的任务历史区）。
                with gr.Group(elem_classes=["y2-sec"]):
                    history_detail_md = gr.Markdown(_t("### 记录详情"))
                    _reg(history_detail_md, lambda lang: gr.update(value=tr(lang, "### 记录详情")))
                    history_audio = gr.Audio(label=_t("试听"), type="filepath", elem_id="history-audio")
                    _reg(history_audio, lambda lang: gr.update(label=tr(lang, "试听")))
                    history_info = gr.Markdown()
                    with gr.Row():
                        with gr.Column(scale=1):
                            history_lyrics = gr.Textbox(label=_t("歌词"), lines=10, interactive=False)
                            _reg(history_lyrics, lambda lang: gr.update(label=tr(lang, "歌词")))
                            history_lyric_sync = gr.HTML(
                                label=_t("歌词同步"),
                                value='<div id="history-lyric-sync" style="padding: 12px; min-height: 100px; border-radius: 8px;"></div>',
                            )
                            _reg(history_lyric_sync, lambda lang: gr.update(label=tr(lang, "歌词同步")))
                        with gr.Column(scale=1):
                            history_abc_preview = gr.HTML(
                                label=_t("乐谱预览"),
                                value=f'<div id="history-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px; border: 1px dashed var(--border-color-primary);"><div style="text-align:center;color:#666;margin-bottom:12px;">{_t("点击历史记录后乐谱将在此处渲染")}</div><div id="history-abc-paper"></div><div id="history-abc-audio"></div></div>',
                            )
                            _reg(history_abc_preview, lambda lang: gr.update(label=tr(lang, "乐谱预览"), value=f'<div id="history-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px; border: 1px dashed var(--border-color-primary);"><div style="text-align:center;color:#666;margin-bottom:12px;">{tr(lang, "点击历史记录后乐谱将在此处渲染")}</div><div id="history-abc-paper"></div><div id="history-abc-audio"></div></div>'))
                            history_abc = gr.Textbox(label=_t("ABC 乐谱文本"), lines=6, interactive=False, elem_id="history-abc")
                            _reg(history_abc, lambda lang: gr.update(label=tr(lang, "ABC 乐谱文本")))
                    history_lyrics_data = gr.HTML(value="", visible=False)
                    history_duration_data = gr.HTML(value="", visible=False)
                    history_style = gr.Markdown(label=_t("风格描述"))
                    _reg(history_style, lambda lang: gr.update(label=tr(lang, "风格描述")))
                # —— 区块 3：项目操作（刷新/删除/清空 + 改名/删除项目） ——
                with gr.Group(elem_classes=["y2-sec"]):
                    history_ops_md = gr.Markdown(_t("### 项目操作"))
                    _reg(history_ops_md, lambda lang: gr.update(value=tr(lang, "### 项目操作")))
                    with gr.Row(elem_classes=["y2-actions"]):
                        history_refresh_btn = gr.Button(_t("刷新"))
                        _reg(history_refresh_btn, lambda lang: gr.update(value=tr(lang, "刷新")))
                        history_delete_btn = gr.Button(_t("删除选中"))
                        _reg(history_delete_btn, lambda lang: gr.update(value=tr(lang, "删除选中")))
                        history_clear_btn = gr.Button(_t("清空历史"))
                        _reg(history_clear_btn, lambda lang: gr.update(value=tr(lang, "清空历史")))
                # 项目管理（文件管理重构）：改项目名只改文件名段（保留时间戳）；
                # 删除项目 = 整个产物目录移入回收站 + 移除该目录全部记录
                with gr.Row(elem_id="hist-rename-row"):
                    history_project_input = gr.Textbox(
                        label=_t("新项目名"), placeholder=_t("留空则清除项目名"), scale=3, lines=1)
                    _reg(history_project_input, lambda lang: gr.update(
                        label=tr(lang, "新项目名"), placeholder=tr(lang, "留空则清除项目名")))
                    history_rename_btn = gr.Button(_t("改项目名"), size="sm", scale=1)
                    _reg(history_rename_btn, lambda lang: gr.update(value=tr(lang, "改项目名")))
                    history_del_project_btn = gr.Button(_t("删除项目"), variant="stop", size="sm", scale=1)
                    _reg(history_del_project_btn, lambda lang: gr.update(value=tr(lang, "删除项目")))

                history_df.select(fn=on_history_select, inputs=[history_state, history_page], outputs=[history_state, history_audio, history_info, history_style, history_lyrics, history_abc, history_abc_preview, history_lyrics_data, history_duration_data])
                history_refresh_btn.click(fn=refresh_history_full, outputs=[history_df, history_page_info, history_page])
                # outputs 末尾为试听播放器，与回调返回值对应（删除/清空后需清空，避免指向已删文件）
                history_delete_btn.click(fn=on_history_delete, inputs=history_state,
                                         outputs=[history_df, history_page_info, history_info, history_state, history_page, history_audio])
                history_clear_btn.click(fn=on_history_clear,
                                        outputs=[history_df, history_page_info, history_info, history_state, history_page, history_audio])
                # 项目管理：改项目名（文件级重命名，播放器按新路径重填）/ 删除项目（整目录入回收站，播放器清空）
                history_rename_btn.click(fn=on_history_rename_project, inputs=[history_state, history_project_input],
                                         outputs=[history_df, history_page_info, history_info, history_state, history_page, history_audio])
                # 删除项目确认弹窗（前端 js，取消则中止回调不触发 Python 端删除）。
                # 注意：Gradio 5.x 中 js 返回 false 不能阻止 fn 执行（实测），
                # 必须在用户取消时 throw 中断；弹窗文案中英双语以兼容两种界面语言。
                _DEL_PROJECT_CONFIRM_JS = (
                    "() => { if (!confirm("
                    "'确定删除整个项目目录？文件将移入回收站。\\n"
                    "Delete the whole project folder? Files will be moved to the Recycle Bin.'"
                    ")) throw new Error('cancelled'); }"
                )
                history_del_project_btn.click(fn=on_history_delete_project, inputs=history_state,
                                              js=_DEL_PROJECT_CONFIRM_JS,
                                              outputs=[history_df, history_page_info, history_info, history_state, history_page, history_audio])
                history_prev_btn.click(fn=on_history_prev_page, inputs=history_page, outputs=[history_df, history_page_info, history_page])
                history_next_btn.click(fn=on_history_next_page, inputs=history_page, outputs=[history_df, history_page_info, history_page])
                demo.load(fn=refresh_history_full, outputs=[history_df, history_page_info, history_page])

            with gr.Tab(_t("音频转谱")) as tab_transcribe:
                _reg(tab_transcribe, lambda lang: gr.update(label=tr(lang, "音频转谱")))
                # 整页统一卡片容器（对齐全站视觉语言）
                with gr.Group(elem_classes=["y2-sec"]):
                    transcribe_md = gr.Markdown(_t("### 音频转乐谱"))
                    _reg(transcribe_md, lambda lang: gr.update(value=tr(lang, "### 音频转乐谱")))
                    transcribe_intro_md = gr.Markdown(_t("上传音频文件，使用 SheetSage2 模型自动转写为 ABC 乐谱"))
                    _reg(transcribe_intro_md, lambda lang: gr.update(value=tr(lang, "上传音频文件，使用 SheetSage2 模型自动转写为 ABC 乐谱")))

                    with gr.Row():
                        with gr.Column():
                            transcribe_audio_input = gr.Audio(label=_t("上传音频 (支持 WAV/MP3/FLAC/OGG/M4A 等)"), type="filepath", elem_id="transcribe-audio-input")
                            _reg(transcribe_audio_input, lambda lang: gr.update(label=tr(lang, "上传音频 (支持 WAV/MP3/FLAC/OGG/M4A 等)")))
                            with gr.Row(elem_classes=["y2-actions"]):
                                transcribe_btn = gr.Button(_t("开始转谱"), variant="primary")
                                _reg(transcribe_btn, lambda lang: gr.update(value=tr(lang, "开始转谱")))
                                transcribe_send_btn = gr.Button(_t("→ 发送到生成页"), variant="secondary")
                                _reg(transcribe_send_btn, lambda lang: gr.update(value=tr(lang, "→ 发送到生成页")))
                            transcribe_info = gr.Markdown()

                        with gr.Column():
                            transcribe_abc_output = gr.Textbox(
                                label=_t("ABC 乐谱 (可编辑)"),
                                placeholder=_t("转谱完成后乐谱将显示在这里..."),
                                lines=10,
                            )
                            _reg(transcribe_abc_output, lambda lang: gr.update(label=tr(lang, "ABC 乐谱 (可编辑)"), placeholder=tr(lang, "转谱完成后乐谱将显示在这里...")))
                            transcribe_abc_preview = gr.HTML(
                                label=_t("乐谱预览"),
                                value=f'<div id="transcribe-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px; border: 1px dashed var(--border-color-primary);"><div style="text-align:center;color:#666;">{_t("转谱后乐谱预览将在此处显示")}</div><div id="transcribe-abc-paper"></div><div id="transcribe-abc-audio"></div></div>',
                            )
                            _reg(transcribe_abc_preview, lambda lang: gr.update(label=tr(lang, "乐谱预览"), value=f'<div id="transcribe-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px; border: 1px dashed var(--border-color-primary);"><div style="text-align:center;color:#666;">{tr(lang, "转谱后乐谱预览将在此处显示")}</div><div id="transcribe-abc-paper"></div><div id="transcribe-abc-audio"></div></div>'))

                            with gr.Row(elem_classes=["y2-actions"]):
                                transcribe_abc_download = gr.File(label=_t("下载 ABC"))
                                _reg(transcribe_abc_download, lambda lang: gr.update(label=tr(lang, "下载 ABC")))
                                transcribe_midi_download = gr.File(label=_t("下载 MIDI"))
                                _reg(transcribe_midi_download, lambda lang: gr.update(label=tr(lang, "下载 MIDI")))

                transcribe_task_id = gr.State(value="")
                transcribe_abc_bridge = gr.Textbox(elem_id="abc-bridge", label="")

            with gr.Tab(_t("音轨分离"), elem_id="tab-sep") as tab_sep:
                _reg(tab_sep, lambda lang: gr.update(label=tr(lang, "音轨分离")))
                # —— 左右分栏：左=源音频+分离参数+执行输出，右=任务历史+库管理 ——
                with gr.Row():
                    with gr.Column(scale=5):
                        # 卡片1：源音频（历史记录 / 上传）
                        with gr.Group(elem_classes=["y2-sec"]):
                            sep_src_md = gr.Markdown(_t("### 源音频"))
                            _reg(sep_src_md, lambda lang: gr.update(value=tr(lang, "### 源音频")))

                            # 源音频：历史记录 或 上传（value 固定 history/upload）
                            sep_src_mode = gr.Radio(choices=[
                                (_t("从历史记录选择"), "history"), (_t("上传音频"), "upload"),
                            ], value="history", label=_t("当前源"))
                            _reg(sep_src_mode, lambda lang: gr.update(
                                choices=[(tr(lang, "从历史记录选择"), "history"), (tr(lang, "上传音频"), "upload")],
                                label=tr(lang, "当前源")))

                            sep_src_history = gr.Dropdown(
                                choices=_voice_source_history_choices(_CUR_LANG),
                                label=_t("从历史记录选择"), interactive=True)
                            _reg(sep_src_history, lambda lang: gr.update(
                                choices=_voice_source_history_choices(lang),
                                label=tr(lang, "从历史记录选择")))

                            sep_src_upload = gr.Audio(label=_t("上传音频"), type="filepath",
                                                      elem_id="sep-src-upload", visible=False)
                            _reg(sep_src_upload, lambda lang: gr.update(label=tr(lang, "上传音频")))

                        # 卡片2：分离参数
                        with gr.Group(elem_classes=["y2-sec"]):
                            sep_param_md = gr.Markdown(_t("### 音轨分离"))
                            _reg(sep_param_md, lambda lang: gr.update(value=tr(lang, "### 音轨分离")))
                            sep_stem_mode = gr.Radio(choices=[
                                (_t("人声/伴奏"), "vocals"), (_t("人声/鼓/贝斯/其他"), "full"),
                            ], value="vocals", label=_t("分离模式"))
                            _reg(sep_stem_mode, lambda lang: gr.update(
                                choices=[(tr(lang, "人声/伴奏"), "vocals"), (tr(lang, "人声/鼓/贝斯/其他"), "full")],
                                label=tr(lang, "分离模式")))
                            sep_denoise = gr.Checkbox(label=_t("降噪"), value=False,
                                                      info=_t("开启后对输出人声降噪"))
                            _reg(sep_denoise, lambda lang: gr.update(
                                label=tr(lang, "降噪"), info=tr(lang, "开启后对输出人声降噪")))

                        # 卡片3：执行与输出（开始分离 / 取消 / 结果播放器组）
                        with gr.Group(elem_classes=["y2-sec"]):
                            sep_run_md = gr.Markdown(_t("### 执行与输出"))
                            _reg(sep_run_md, lambda lang: gr.update(value=tr(lang, "### 执行与输出")))
                            with gr.Row(elem_classes=["y2-actions"]):
                                sep_btn = gr.Button(_t("开始分离"), variant="primary", scale=3)
                                _reg(sep_btn, lambda lang: gr.update(value=tr(lang, "开始分离")))
                                sep_cancel_btn = gr.Button(_t("取消任务"), variant="stop", scale=2)
                                _reg(sep_cancel_btn, lambda lang: gr.update(value=tr(lang, "取消任务")))
                            sep_info = gr.Markdown()
                            # 输出产物播放器组：按产物数量逐个显示（与其他 Tab 播放器同组件，
                            # PlayerZoom 个性化定制按 elem_id 前缀 sep-audio- 统一接管）；label 动态为轨道名
                            sep_audios = [
                                gr.Audio(type="filepath", label="", elem_id=f"sep-audio-{i}",
                                         visible=False)
                                for i in range(VOICE_PLAYER_COUNT)
                            ]

                    # —— 右栏：分离任务历史（按文件夹选择，整组播放器回放全部轨道） ——
                    with gr.Column(scale=4):
                        # 卡片1：分离任务历史（选择任务 + 回放 + 改名/删除）
                        with gr.Group(elem_classes=["y2-sec"]):
                            sep_hist_md = gr.Markdown(_t("### 分离任务历史"))
                            _reg(sep_hist_md, lambda lang: gr.update(value=tr(lang, "### 分离任务历史")))
                            sep_history_dd = gr.Dropdown(
                                choices=_voice_task_history_choices("separation"),
                                label=_t("选择分离任务"), interactive=True)
                            _reg(sep_history_dd, lambda lang: gr.update(
                                choices=_voice_task_history_choices("separation", lang),
                                label=tr(lang, "选择分离任务")))
                            # 隐藏 State 组件：保存当前选中的 task_id，供删除/改名按钮正确读取
                            # （Gradio 6 中 Tab 切换更新 choices 会把 Dropdown 的 value 重置为 None，
                            # 必须用 State 组件显式保存用户选中的值，与历史页 history_state 模式一致）
                            sep_selected_task = gr.State(value=None)
                            # 历史回放播放器组：选中任务后按文件夹填充全部轨道（每轨可下载）
                            sep_hist_audios = [
                                gr.Audio(type="filepath", label="", elem_id=f"sep-history-audio-{i}",
                                         visible=False)
                                for i in range(VOICE_PLAYER_COUNT)
                            ]
                            # 项目管理（文件管理重构）：改项目名（保留时间戳）/ 删除项目（整目录入回收站）
                            # 统一结构：label | 输入框 | 主动作按钮 | 删除按钮（label 由 CSS 压在输入框同一行）
                            with gr.Row(elem_id="sep-rename-row"):
                                sep_rename_input = gr.Textbox(
                                    label=_t("新项目名"), placeholder=_t("留空则清除项目名"),
                                    scale=3, lines=1)
                                _reg(sep_rename_input, lambda lang: gr.update(
                                    label=tr(lang, "新项目名"), placeholder=tr(lang, "留空则清除项目名")))
                                sep_rename_btn = gr.Button(_t("改项目名"), size="sm", scale=1)
                                _reg(sep_rename_btn, lambda lang: gr.update(value=tr(lang, "改项目名")))
                                sep_del_btn = gr.Button(_t("删除项目"), variant="stop", size="sm", scale=1)
                                _reg(sep_del_btn, lambda lang: gr.update(value=tr(lang, "删除项目")))

                        # 卡片2：库管理（素材库=乐器轨 / 音色库=参考干声）
                        # （翻唱页仅保留选择+试听；删除/重命名集中到分离页）
                        with gr.Group(elem_classes=["y2-sec"]):
                            lib_md = gr.Markdown(_t("### 库管理"))
                            _reg(lib_md, lambda lang: gr.update(value=tr(lang, "### 库管理")))

                            # —— 保存到素材库：把本次分离的乐器/伴奏轨入库 ——
                            # 这是素材库的唯一写入入口（此前 save_stem 无 UI 调用方，
                            # 导致素材库下拉与本页/翻唱页「自定义伴奏」永远为空）
                            with gr.Row(elem_id="lib-stem-save-row"):
                                lib_stem_pick = gr.Dropdown(
                                    choices=[], label=_t("待入库轨道"), interactive=True, scale=3)
                                _reg(lib_stem_pick, lambda lang: gr.update(label=tr(lang, "待入库轨道")))
                                lib_stem_save_name = gr.Textbox(
                                    label=_t("素材名称"), placeholder=_t("留空则用轨道名"),
                                    scale=3, lines=1)
                                _reg(lib_stem_save_name, lambda lang: gr.update(
                                    label=tr(lang, "素材名称"), placeholder=tr(lang, "留空则用轨道名")))
                                lib_stem_save_btn = gr.Button(_t("保存到素材库"), size="sm", scale=1)
                                _reg(lib_stem_save_btn, lambda lang: gr.update(value=tr(lang, "保存到素材库")))
                            # 本次分离的 stems 缓存（保存时回查轨道类型，供「名__类型」命名）
                            sep_stems_state = gr.State([])

                            lib_stem_dd = gr.Dropdown(
                                choices=_voice_stem_choices(_CUR_LANG),
                                label=_t("素材库(乐器轨)"), interactive=True)
                            _reg(lib_stem_dd, lambda lang: gr.update(
                                choices=_voice_stem_choices(lang), label=tr(lang, "素材库(乐器轨)")))
                            lib_stem_preview = gr.Audio(
                                label=_t("试听"), type="filepath",
                                elem_id="lib-stem-preview", visible=False,
                               )
                            _reg(lib_stem_preview, lambda lang: gr.update(label=tr(lang, "试听")))
                            with gr.Row(elem_id="lib-stem-row"):
                                lib_stem_rename_input = gr.Textbox(
                                    label=_t("重命名为"), placeholder=_t("新名字"), elem_id="lib-stem-rename",
                                    scale=3, lines=1)
                                _reg(lib_stem_rename_input, lambda lang: gr.update(
                                    label=tr(lang, "重命名为"), placeholder=tr(lang, "新名字")))
                                lib_stem_rename_btn = gr.Button(_t("重命名"), size="sm", scale=1)
                                _reg(lib_stem_rename_btn, lambda lang: gr.update(value=tr(lang, "重命名")))
                                lib_stem_del_btn = gr.Button(_t("删除选中"), size="sm", variant="stop", scale=1)
                                _reg(lib_stem_del_btn, lambda lang: gr.update(value=tr(lang, "删除选中")))

                            lib_ref_dd = gr.Dropdown(
                                choices=_voice_ref_choices(_CUR_LANG),
                                label=_t("音色库(参考干声)"), interactive=True)
                            _reg(lib_ref_dd, lambda lang: gr.update(
                                choices=_voice_ref_choices(lang), label=tr(lang, "音色库(参考干声)")))
                            lib_ref_preview = gr.Audio(
                                label=_t("试听"), type="filepath",
                                elem_id="lib-ref-preview", visible=False,
                               )
                            _reg(lib_ref_preview, lambda lang: gr.update(label=tr(lang, "试听")))
                            with gr.Row(elem_id="lib-ref-row"):
                                lib_ref_rename_input = gr.Textbox(
                                    label=_t("重命名为"), placeholder=_t("新名字"), elem_id="lib-ref-rename",
                                    scale=3, lines=1)
                                _reg(lib_ref_rename_input, lambda lang: gr.update(
                                    label=tr(lang, "重命名为"), placeholder=tr(lang, "新名字")))
                                lib_ref_rename_btn = gr.Button(_t("重命名"), size="sm", scale=1)
                                _reg(lib_ref_rename_btn, lambda lang: gr.update(value=tr(lang, "重命名")))
                                lib_ref_del_btn = gr.Button(_t("删除选中"), size="sm", variant="stop", scale=1)
                                _reg(lib_ref_del_btn, lambda lang: gr.update(value=tr(lang, "删除选中")))

                # 事件绑定
                sep_src_mode.change(fn=on_voice_src_mode, inputs=sep_src_mode,
                                    outputs=[sep_src_history, sep_src_upload])
                sep_btn.click(fn=on_voice_separate,
                              inputs=[sep_src_history, sep_src_upload, sep_stem_mode, sep_denoise],
                              outputs=[*sep_audios, sep_info, sep_btn, sep_history_dd,
                                       lib_stem_pick, sep_stems_state])
                # 取消按钮：协作式取消本 Tab 排队中/运行中的任务（info 区反馈结果）
                sep_cancel_btn.click(fn=lambda: on_voice_cancel("separation"),
                                     outputs=[sep_info])

            with gr.Tab(_t("音色翻唱"), elem_id="tab-cover") as tab_cover:
                _reg(tab_cover, lambda lang: gr.update(label=tr(lang, "音色翻唱")))
                # —— 左右分栏：左=被翻唱歌曲+参考音色+翻唱参数，右=执行与输出+任务历史 ——
                with gr.Row():
                    with gr.Column(scale=5):
                        # 卡片1：被翻唱歌曲（历史记录 / 上传）
                        with gr.Group(elem_classes=["y2-sec"]):
                            cover_src_md = gr.Markdown(_t("### 被翻唱歌曲"))
                            _reg(cover_src_md, lambda lang: gr.update(value=tr(lang, "### 被翻唱歌曲")))

                            # 被翻唱歌曲：历史记录 或 上传（value 固定 history/upload）
                            cover_src_mode = gr.Radio(choices=[
                                (_t("从历史记录选择"), "history"), (_t("上传音频"), "upload"),
                            ], value="history", label=_t("当前源"))
                            _reg(cover_src_mode, lambda lang: gr.update(
                                choices=[(tr(lang, "从历史记录选择"), "history"), (tr(lang, "上传音频"), "upload")],
                                label=tr(lang, "当前源")))

                            cover_src_history = gr.Dropdown(
                                choices=_voice_cover_source_choices(_CUR_LANG),
                                label=_t("从历史记录选择"), interactive=True)
                            _reg(cover_src_history, lambda lang: gr.update(
                                choices=_voice_cover_source_choices(lang),
                                label=tr(lang, "从历史记录选择")))

                            cover_src_upload = gr.Audio(label=_t("上传音频"), type="filepath",
                                                        elem_id="cover-src-upload", visible=False)
                            _reg(cover_src_upload, lambda lang: gr.update(label=tr(lang, "上传音频")))

                        # 卡片2：参考音色（音色库 / 干声历史 / 上传）+ 自定义伴奏
                        with gr.Group(elem_classes=["y2-sec"]):
                            # —— 参考音色三入口：音色库 / 干声历史 / 上传 ——
                            cover_ref_md = gr.Markdown(_t("### 参考音色"))
                            _reg(cover_ref_md, lambda lang: gr.update(value=tr(lang, "### 参考音色")))

                            cover_ref_mode = gr.Radio(choices=[
                                (_t("从音色库选择"), "library"), (_t("从干声历史选择"), "dry"),
                                (_t("上传参考干声(1-30秒)"), "upload"),
                            ], value="library", label=_t("参考音色"))
                            _reg(cover_ref_mode, lambda lang: gr.update(
                                choices=[(tr(lang, "从音色库选择"), "library"),
                                         (tr(lang, "从干声历史选择"), "dry"),
                                         (tr(lang, "上传参考干声(1-30秒)"), "upload")],
                                label=tr(lang, "参考音色")))

                            # 音色库入口（默认可见）
                            cover_ref_dropdown = gr.Dropdown(
                                choices=_voice_ref_choices(_CUR_LANG), label=_t("音色库选择"),
                                interactive=True)
                            _reg(cover_ref_dropdown, lambda lang: gr.update(
                                choices=_voice_ref_choices(lang), label=tr(lang, "音色库选择")))
                            # —— 音色库入口：仅选择；选中即试听（管理功能在分离页库管理区） ——
                            cover_ref_preview = gr.Audio(
                                label=_t("试听"), type="filepath",
                                elem_id="cover-ref-preview", visible=False,
                               )
                            _reg(cover_ref_preview, lambda lang: gr.update(label=tr(lang, "试听")))

                            # 干声历史入口：radio 选来源（分离人声/上传干声），再在下拉选文件（默认隐藏）
                            with gr.Column(visible=False) as cover_ref_dry_panel:
                                cover_ref_dry_src = gr.Radio(
                                    choices=[(_t("从分离人声选择"), "sep"), (_t("从上传干声选择"), "upload")],
                                    value="sep", label=_t("干声来源"))
                                _reg(cover_ref_dry_src, lambda lang: gr.update(
                                    choices=[(tr(lang, "从分离人声选择"), "sep"),
                                             (tr(lang, "从上传干声选择"), "upload")],
                                    label=tr(lang, "干声来源")))
                                cover_ref_dry_sep = gr.Dropdown(
                                    choices=_voice_dry_sep_choices(_CUR_LANG),
                                    label=_t("分离人声"), interactive=True)
                                _reg(cover_ref_dry_sep, lambda lang: gr.update(
                                    choices=_voice_dry_sep_choices(lang),
                                    label=tr(lang, "分离人声")))
                                cover_ref_dry_upload = gr.Dropdown(
                                    choices=_voice_dry_upload_choices(_CUR_LANG),
                                    label=_t("上传干声"), interactive=True, visible=False)
                                _reg(cover_ref_dry_upload, lambda lang: gr.update(
                                    choices=_voice_dry_upload_choices(lang),
                                    label=tr(lang, "上传干声")))

                            # 上传入口（含命名保存，仅 upload 模式可见）
                            with gr.Column(visible=False) as cover_ref_upload_panel:
                                cover_ref_upload = gr.Audio(label=_t("上传参考干声(1-30秒)"), type="filepath",
                                                            elem_id="cover-ref-upload")
                                _reg(cover_ref_upload, lambda lang: gr.update(label=tr(lang, "上传参考干声(1-30秒)")))
                                # 轻量人声检测结果提示（上传后自动判定，仅提示不拦截）
                                cover_ref_check_md = gr.Markdown()
                                with gr.Row():
                                    cover_ref_name = gr.Textbox(label=_t("输入音色库名称"), elem_id="cover-ref-name")
                                    _reg(cover_ref_name, lambda lang: gr.update(label=tr(lang, "输入音色库名称")))
                                    cover_ref_save_btn = gr.Button(_t("保存到音色库"), size="sm")
                                    _reg(cover_ref_save_btn, lambda lang: gr.update(value=tr(lang, "保存到音色库")))
                                cover_ref_info = gr.Markdown(_t("保存参考音色提示"))
                                _reg(cover_ref_info, lambda lang: gr.update(value=tr(lang, "保存参考音色提示")))

                            # —— 自定义伴奏（可选）：从素材库选乐器轨替换原曲伴奏 ——
                            cover_acc_dd = gr.Dropdown(
                                choices=_voice_stem_choices(_CUR_LANG),
                                label=_t("自定义伴奏(可选)"), interactive=True,
                                info=_t("留空自动使用源伴奏"))
                            _reg(cover_acc_dd, lambda lang: gr.update(
                                choices=_voice_stem_choices(lang),
                                label=tr(lang, "自定义伴奏(可选)"),
                                info=tr(lang, "留空自动使用源伴奏")))
                            # —— 素材库选择：仅选择；选中即试听（管理功能在分离页库管理区） ——
                            cover_acc_preview = gr.Audio(
                                label=_t("试听"), type="filepath",
                                elem_id="cover-acc-preview", visible=False,
                               )
                            _reg(cover_acc_preview, lambda lang: gr.update(label=tr(lang, "试听")))

                        # 卡片3：翻唱参数
                        with gr.Group(elem_classes=["y2-sec"]):
                            cover_param_md = gr.Markdown(_t("### 翻唱参数"))
                            _reg(cover_param_md, lambda lang: gr.update(value=tr(lang, "### 翻唱参数")))
                            cover_semi = gr.Slider(-12, 12, value=0, step=1, label=_t("半音偏移"))
                            _reg(cover_semi, lambda lang: gr.update(label=tr(lang, "半音偏移")))
                            with gr.Row(elem_classes=["y2-actions"]):
                                cover_semi_orig = gr.Button(_t("半音快捷原调"), size="sm")
                                _reg(cover_semi_orig, lambda lang: gr.update(value=tr(lang, "半音快捷原调")))
                                cover_semi_m12 = gr.Button(_t("−12"), size="sm")
                                cover_semi_p12 = gr.Button(_t("+12"), size="sm")
                            # 默认 40：Seed-VC 官方称质量最佳区为 30-50（30 是质量区下限），
                            # 默认取 40 兼顾质量与耗时
                            cover_steps = gr.Slider(10, 50, value=40, step=1, label=_t("扩散步数"))
                            _reg(cover_steps, lambda lang: gr.update(label=tr(lang, "扩散步数")))
                            cover_gain = gr.Slider(-6, 6, value=0, step=0.5, label=_t("伴奏增益(dB)"))
                            _reg(cover_gain, lambda lang: gr.update(label=tr(lang, "伴奏增益(dB)")))
                            # 参考段策略（P5C 盲听验证）：默认「智能」——参考干声来自分离记录时
                            # 用配对伴奏挑"人声主导度最高 10s"（串音最少）作音色参考；拿不到配对
                            # 伴奏（上传干声/音色库）时自动回退「能量最高段」（旧行为）。
                            # 「整曲不裁剪」等价关闭该优化。
                            cover_ref_seg = gr.Dropdown(choices=[
                                (_t("智能 (推荐)"), "smart"),
                                (_t("能量最高段"), "energy"),
                                (_t("整曲不裁剪"), "full"),
                            ], value="smart", label=_t("参考段"),
                                info=_t("智能：取人声最干净的 10 秒作参考；整曲不裁剪可能音色漂移"))
                            _reg(cover_ref_seg, lambda lang: gr.update(
                                choices=[(tr(lang, "智能 (推荐)"), "smart"),
                                         (tr(lang, "能量最高段"), "energy"),
                                         (tr(lang, "整曲不裁剪"), "full")],
                                label=tr(lang, "参考段"),
                                info=tr(lang, "智能：取人声最干净的 10 秒作参考；整曲不裁剪可能音色漂移")))

                    # —— 右栏：执行与输出 + 翻唱任务历史 ——
                    with gr.Column(scale=4):
                        # 卡片1：执行与输出
                        with gr.Group(elem_classes=["y2-sec"]):
                            # 补齐卡片标题，与分离页的同名卡片保持一致
                            cover_run_md = gr.Markdown(_t("### 执行与输出"))
                            _reg(cover_run_md, lambda lang: gr.update(value=tr(lang, "### 执行与输出")))
                            cover_denoise = gr.Checkbox(label=_t("降噪"), value=False,
                                                        info=_t("开启后对输出人声降噪"))
                            _reg(cover_denoise, lambda lang: gr.update(
                                label=tr(lang, "降噪"), info=tr(lang, "开启后对输出人声降噪")))
                            with gr.Row(elem_classes=["y2-actions"]):
                                cover_btn = gr.Button(_t("开始翻唱"), variant="primary", scale=3)
                                _reg(cover_btn, lambda lang: gr.update(value=tr(lang, "开始翻唱")))
                                cover_cancel_btn = gr.Button(_t("取消任务"), variant="stop", scale=2)
                                _reg(cover_cancel_btn, lambda lang: gr.update(value=tr(lang, "取消任务")))
                            cover_info = gr.Markdown()
                            # 输出产物播放器组：按产物数量逐个显示（与其他 Tab 播放器同组件，
                            # PlayerZoom 按 elem_id 前缀 cover-audio- 接管）；label 动态为轨道名
                            cover_audios = [
                                gr.Audio(type="filepath", label="", elem_id=f"cover-audio-{i}",
                                         visible=False)
                                for i in range(VOICE_PLAYER_COUNT)
                            ]

                        # 卡片2：翻唱任务历史（选择任务 + 回放 + 改名/删除）
                        with gr.Group(elem_classes=["y2-sec"]):
                            # —— 翻唱任务历史：按文件夹选择，整组播放器回放全部轨道 ——
                            cover_hist_md = gr.Markdown(_t("### 翻唱任务历史"))
                            _reg(cover_hist_md, lambda lang: gr.update(value=tr(lang, "### 翻唱任务历史")))
                            cover_history_dd = gr.Dropdown(
                                choices=_voice_task_history_choices("cover"),
                                label=_t("选择翻唱任务"), interactive=True)
                            _reg(cover_history_dd, lambda lang: gr.update(
                                choices=_voice_task_history_choices("cover", lang),
                                label=tr(lang, "选择翻唱任务")))
                            cover_selected_task = gr.State(value=None)
                            # 历史回放播放器组：选中任务后按文件夹填充全部轨道（每轨可下载）
                            cover_hist_audios = [
                                gr.Audio(type="filepath", label="", elem_id=f"cover-history-audio-{i}",
                                         visible=False)
                                for i in range(VOICE_PLAYER_COUNT)
                            ]
                            # 项目管理（文件管理重构）：改项目名（保留时间戳）/ 删除项目（整目录入回收站）
                            with gr.Row(elem_id="cover-rename-row"):
                                cover_rename_input = gr.Textbox(
                                    label=_t("新项目名"), placeholder=_t("留空则清除项目名"),
                                    scale=3, lines=1)
                                _reg(cover_rename_input, lambda lang: gr.update(
                                    label=tr(lang, "新项目名"), placeholder=tr(lang, "留空则清除项目名")))
                                cover_rename_btn = gr.Button(_t("改项目名"), size="sm", scale=1)
                                _reg(cover_rename_btn, lambda lang: gr.update(value=tr(lang, "改项目名")))
                                cover_del_btn = gr.Button(_t("删除项目"), variant="stop", size="sm", scale=1)
                                _reg(cover_del_btn, lambda lang: gr.update(value=tr(lang, "删除项目")))

                # 事件绑定
                cover_src_mode.change(fn=on_voice_src_mode, inputs=cover_src_mode,
                                      outputs=[cover_src_history, cover_src_upload])
                cover_ref_mode.change(fn=on_voice_ref_mode, inputs=cover_ref_mode,
                                    outputs=[cover_ref_dropdown, cover_ref_dry_panel,
                                             cover_ref_upload_panel])
                cover_ref_dry_src.change(fn=on_voice_dry_src_mode, inputs=cover_ref_dry_src,
                                       outputs=[cover_ref_dry_upload, cover_ref_dry_sep])
                cover_ref_save_btn.click(fn=on_voice_save_ref, inputs=[cover_ref_upload, cover_ref_name],
                                         outputs=[cover_ref_info, cover_ref_dropdown])
                # 上传参考干声后自动轻量人声检测（方案B，仅提示）+ 通过者留存并入干声历史
                cover_ref_upload.change(fn=on_voice_ref_upload_check,
                                        inputs=cover_ref_upload,
                                        outputs=[cover_ref_check_md, cover_ref_dry_upload])
                cover_semi_orig.click(fn=lambda: 0, outputs=cover_semi)
                cover_semi_m12.click(fn=lambda: -12, outputs=cover_semi)
                cover_semi_p12.click(fn=lambda: 12, outputs=cover_semi)
                cover_btn.click(fn=on_voice_cover,
                                inputs=[cover_src_history, cover_src_upload,
                                        cover_ref_dropdown, cover_ref_dry_upload,
                                        cover_ref_dry_sep,
                                        cover_ref_upload,
                                        cover_semi, cover_steps, cover_gain, cover_acc_dd,
                                        cover_denoise, cover_ref_seg],
                                outputs=[*cover_audios, cover_info, cover_btn, cover_history_dd])
                # 取消按钮：协作式取消本 Tab 排队中/运行中的任务（info 区反馈结果）
                cover_cancel_btn.click(fn=lambda: on_voice_cancel("cover"),
                                       outputs=[cover_info])
                # 音色库/素材库选择：选中即试听（管理功能在分离页库管理区）
                cover_ref_dropdown.change(fn=lambda p: gr.update(value=_preview_for_library(p), visible=bool(p)),
                                          inputs=cover_ref_dropdown,
                                          outputs=[cover_ref_preview])
                cover_acc_dd.change(fn=lambda p: gr.update(value=_preview_for_library(p), visible=bool(p)),
                                    inputs=cover_acc_dd, outputs=[cover_acc_preview])

                # 库管理（分离页）：选中即试听；删除/重命名（回收站）后刷新下拉
                lib_stem_dd.change(fn=lambda p: gr.update(value=_preview_for_library(p), visible=bool(p)),
                                   inputs=lib_stem_dd, outputs=[lib_stem_preview])
                # 保存到素材库：把本次分离的乐器/伴奏轨写入素材库（唯一写入入口）
                lib_stem_save_btn.click(fn=on_voice_save_stem_to_lib,
                                        inputs=[lib_stem_pick, lib_stem_save_name, sep_stems_state],
                                        outputs=[lib_stem_dd, lib_stem_save_name])
                lib_stem_del_btn.click(fn=on_voice_stem_delete, inputs=[lib_stem_dd],
                                       outputs=[lib_stem_dd])
                lib_stem_rename_btn.click(fn=on_voice_stem_rename,
                                          inputs=[lib_stem_dd, lib_stem_rename_input],
                                          outputs=[lib_stem_dd])
                lib_ref_dd.change(fn=lambda p: gr.update(value=_preview_for_library(p), visible=bool(p)),
                                  inputs=lib_ref_dd, outputs=[lib_ref_preview])
                lib_ref_del_btn.click(fn=on_voice_ref_delete, inputs=[lib_ref_dd],
                                      outputs=[lib_ref_preview, lib_ref_dd])
                lib_ref_rename_btn.click(fn=on_voice_ref_rename,
                                         inputs=[lib_ref_dd, lib_ref_rename_input],
                                         outputs=[lib_ref_preview, lib_ref_dd])

                # 任务历史回放（按文件夹）：选任务 → 整组播放器填充全部轨道
                sep_history_dd.change(fn=on_voice_task_history_pick, inputs=sep_history_dd,
                                      outputs=[*sep_hist_audios])
                # 额外绑定：用户手动选下拉时，同步更新隐藏 State 组件
                # （后续删除/改名按钮从 State 读 task_id，避免 Dropdown 被重置）
                sep_history_dd.change(fn=lambda tid: tid, inputs=sep_history_dd,
                                      outputs=sep_selected_task)
                cover_history_dd.change(fn=on_voice_task_history_pick, inputs=cover_history_dd,
                                        outputs=[*cover_hist_audios])
                cover_history_dd.change(fn=lambda tid: tid, inputs=cover_history_dd,
                                        outputs=cover_selected_task)

                # 项目管理（文件管理重构）：改项目名（重命名文件保留时间戳）/ 删除项目（整目录入回收站）
                # 关键修复：inputs 用隐藏 State 组件而非 Dropdown，
                # 因为 Gradio 6 中 Tab 切换更新 choices 会把 Dropdown.value 重置为 None
                sep_rename_btn.click(fn=lambda tid, name: on_voice_task_rename(tid, name, "separation"),
                                     inputs=[sep_selected_task, sep_rename_input],
                                     outputs=[sep_history_dd, sep_selected_task, *sep_hist_audios, sep_rename_input])
                sep_del_btn.click(fn=lambda tid: on_voice_task_delete(tid, "separation"),
                                  inputs=sep_selected_task,
                                  js=_DEL_PROJECT_CONFIRM_JS,
                                  outputs=[sep_history_dd, sep_selected_task, *sep_hist_audios])
                cover_rename_btn.click(fn=lambda tid, name: on_voice_task_rename(tid, name, "cover"),
                                       inputs=[cover_selected_task, cover_rename_input],
                                       outputs=[cover_history_dd, cover_selected_task, *cover_hist_audios, cover_rename_input])
                cover_del_btn.click(fn=lambda tid: on_voice_task_delete(tid, "cover"),
                                    inputs=cover_selected_task,
                                    js=_DEL_PROJECT_CONFIRM_JS,
                                    outputs=[cover_history_dd, cover_selected_task, *cover_hist_audios])

                # 每次切到分离 Tab 时刷新源下拉 + 分离任务历史 + 库管理两下拉
                # （翻唱页删除后保持同步；新生成的歌曲也要能立即作为分离源，无需刷新页面）
                # 注意：Gradio 6 中 gr.update(choices=...) 不传 value 会把 Dropdown 值重置，
                # 必须用 _dd_update 同时传递 value=首项值。
                # 同时更新隐藏 State：保存第一条任务记录的 task_id，供删除/改名按钮正确读取
                tab_sep.select(fn=lambda: (
                    _dd_update(_voice_source_history_choices(_CUR_LANG)),
                    _dd_update(_voice_task_history_choices("separation")),
                    _dd_update(_voice_stem_choices(_CUR_LANG)),
                    _dd_update(_voice_ref_choices(_CUR_LANG)),
                    _preview_first_update(_voice_stem_choices(_CUR_LANG)),
                    _preview_first_update(_voice_ref_choices(_CUR_LANG)),
                    (_voice_task_history_choices("separation")[0][1]
                     if _voice_task_history_choices("separation") else None),
                    *_voice_task_first_players("separation")),
                               outputs=[sep_src_history, sep_history_dd, lib_stem_dd, lib_ref_dd,
                                        lib_stem_preview, lib_ref_preview,
                                        sep_selected_task, *sep_hist_audios])
                # 每次切到翻唱 Tab 时刷新翻唱源/音色库/伴奏/干声两来源/翻唱历史下拉 + 两处试听，
                # 并按历史首条任务回填回放播放器（否则下拉显示着任务名、播放器却是空的）
                tab_cover.select(fn=lambda: (
                    _dd_update(_voice_cover_source_choices(_CUR_LANG)),
                    _dd_update(_voice_ref_choices(_CUR_LANG)),
                    _dd_update(_voice_stem_choices(_CUR_LANG)),
                    _dd_update(_voice_dry_sep_choices(_CUR_LANG)),
                    _dd_update(_voice_dry_upload_choices(_CUR_LANG)),
                    _dd_update(_voice_task_history_choices("cover")),
                    _preview_first_update(_voice_ref_choices(_CUR_LANG)),
                    _preview_first_update(_voice_stem_choices(_CUR_LANG)),
                    (_voice_task_history_choices("cover")[0][1]
                     if _voice_task_history_choices("cover") else None),
                    *_voice_task_first_players("cover")),
                                 outputs=[cover_src_history, cover_ref_dropdown, cover_acc_dd,
                                          cover_ref_dry_sep, cover_ref_dry_upload,
                                          cover_history_dd, cover_ref_preview, cover_acc_preview,
                                          cover_selected_task, *cover_hist_audios])

            with gr.Tab(_t("多轨编辑")) as tab_mix:
                _reg(tab_mix, lambda lang: gr.update(label=tr(lang, "多轨编辑")))
                # 多轨编辑器：同一独立编辑页以 iframe 内嵌（不做 Gradio 重渲染耦合）
                # embed=1 时页面收紧内边距并跟随父页面明暗主题（同源可读父窗口样式）
                gr.HTML(
                    '<div style="margin-top:0;">'
                    '<iframe src="/static/multitrack/?embed=1" title="multitrack"'
                    ' style="width:100%;height:760px;border:1px solid var(--y2-line, #d8dee4);'
                    'border-radius:var(--y2-r-lg, 10px);box-shadow:var(--y2-shadow-sm, none);'
                    'background:transparent;display:block;"></iframe>'
                    '</div>',
                    elem_id="mix-editor-embed",
                )

            with gr.Tab(_t("系统设置")) as tab_settings:
                _reg(tab_settings, lambda lang: gr.update(label=tr(lang, "系统设置")))
                # 左右分栏：左列系统状态（模型检查），右列当前队列；下方参数预设整行
                with gr.Row():
                    with gr.Column(scale=3, elem_classes=["y2-sec"]):
                        # 标题与「检查模型」按钮同行，压缩纵向占用
                        with gr.Row():
                            sysstatus_md = gr.Markdown(_t("### 系统状态"))
                            _reg(sysstatus_md, lambda lang: gr.update(value=tr(lang, "### 系统状态")))
                            check_models_btn = gr.Button(_t("检查模型"), scale=0, min_width=110, size="sm")
                            _reg(check_models_btn, lambda lang: gr.update(value=tr(lang, "检查模型")))
                        model_status = gr.Markdown(value=on_check_models())
                        # 切语言时重新渲染模型状态（apply_lang 先更新 _CUR_LANG 再执行 updater）
                        _reg(model_status, lambda lang: gr.update(value=on_check_models()))
                        check_models_btn.click(fn=on_check_models, outputs=model_status)

                    with gr.Column(scale=2, elem_classes=["y2-sec"]):
                        # 当前队列状态窗口：Timer 每 2s 轮询只读快照（不影响任务进度流）
                        queue_md = gr.Markdown(_t("### 当前队列"))
                        _reg(queue_md, lambda lang: gr.update(value=tr(lang, "### 当前队列")))
                        queue_status_md = gr.Markdown(value=_queue_status_html())
                        # 切语言即时重渲染（apply_lang 先更新 _CUR_LANG 再执行 updater）
                        _reg(queue_status_md, lambda lang: gr.update(value=_queue_status_html()))
                        queue_timer = gr.Timer(2.0)
                        queue_timer.tick(fn=_queue_status_html, outputs=[queue_status_md])

                # 参数预设区：Group 承载卡片外框（与上方两列同样式）
                with gr.Group(elem_classes=["y2-sec"]):
                    presets_md = gr.Markdown(_t("### 参数预设"))
                    _reg(presets_md, lambda lang: gr.update(value=tr(lang, "### 参数预设")))
                    # 下拉与「加载」按钮同行、名称输入与「保存当前参数」按钮同行（紧凑化）
                    with gr.Row():
                        preset_dropdown = gr.Dropdown(
                            label=_t("加载预设"),
                            choices=_preset_display_names(_CUR_LANG),
                            value=None,
                            scale=4,
                        )
                        _reg(preset_dropdown, lambda lang: gr.update(label=tr(lang, "加载预设"), choices=_preset_display_names(lang)))
                        preset_load_btn = gr.Button(_t("加载"), scale=1)
                        _reg(preset_load_btn, lambda lang: gr.update(value=tr(lang, "加载")))
                    with gr.Row():
                        preset_name_input = gr.Textbox(label=_t("保存预设名称"), placeholder=_t("我的预设"), scale=4)
                        _reg(preset_name_input, lambda lang: gr.update(label=tr(lang, "保存预设名称"), placeholder=tr(lang, "我的预设")))
                        preset_save_btn = gr.Button(_t("保存当前参数"), scale=1)
                        _reg(preset_save_btn, lambda lang: gr.update(value=tr(lang, "保存当前参数")))
                    preset_info = gr.Markdown()

        lyrics_input.change(fn=on_lyrics_change, inputs=lyrics_input, outputs=structure_analysis)

        random_seed_btn.click(fn=on_random_seed, outputs=seed_input)

        generate_btn.click(
            fn=on_generate,
            inputs=[
                project_input,
                style_input, lyrics_input, cot_input, seed_input, random_seed_checkbox, cfg_input, steps_input, out_format_input, batch_count_input,
                normalize_checkbox, fade_checkbox, trim_checkbox, metadata_checkbox,
                abc_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
            ],
            outputs=[audio_output, info_output, abc_output, abc_file_output, flac_file_output, lyrics_sync_data, history_df, history_page_info, history_page, variant_group, variant_selector, variant_state, seed_input],
        )

        variant_selector.change(
            fn=on_variant_select,
            inputs=[variant_selector, variant_state],
            outputs=[audio_output, abc_output, abc_file_output, flac_file_output],
        )
        variant_finalize_btn.click(
            fn=on_variant_finalize,
            inputs=[variant_selector, variant_state],
            outputs=[info_output, history_df, history_page_info, history_page, variant_group, variant_selector, variant_state],
        )
        variant_keep_btn.click(
            fn=on_variant_keep_all,
            inputs=[variant_selector, variant_state],
            outputs=[info_output, history_df, history_page_info, history_page, variant_group, variant_selector, variant_state],
        )

        cancel_btn.click(fn=on_cancel, outputs=info_output)

        resynthesize_btn.click(
            fn=on_resynthesize,
            inputs=[
                abc_output, style_input, lyrics_input, seed_input, cfg_input, steps_input, out_format_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
            ],
            outputs=[audio_output, info_output, abc_file_output, flac_file_output, lyrics_sync_data],
        )

        transcribe_btn.click(
            fn=on_transcribe,
            inputs=[transcribe_audio_input],
            outputs=[transcribe_abc_output, transcribe_info, transcribe_abc_download, transcribe_midi_download, transcribe_task_id],
        )

        transcribe_send_btn.click(
            fn=on_send_to_generate,
            inputs=[transcribe_abc_output],
            outputs=[transcribe_abc_bridge],
        )

        preset_load_btn.click(
            fn=on_preset_load,
            inputs=preset_dropdown,
            outputs=[
                cot_input, steps_input, out_format_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
                cfg_input, batch_count_input,
                normalize_checkbox, fade_checkbox, trim_checkbox, metadata_checkbox,
            ],
        )
        preset_save_btn.click(
            fn=on_preset_save,
            inputs=[
                preset_name_input, cot_input, steps_input, out_format_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
                cfg_input, batch_count_input,
                normalize_checkbox, fade_checkbox, trim_checkbox, metadata_checkbox,
            ],
            outputs=preset_info,
        )

        # 语言即时切换：lang_select.change -> apply_lang -> 更新 lang_state + 所有 translatable 组件
        lang_select.change(
            fn=apply_lang,
            inputs=lang_select,
            outputs=[lang_state] + translatables,
        )
        # 历史页翻页信息是动态文案（含当前页码），无法静态注册 updater；
        # 追加第二个 change 绑定，按当前页重新生成。本绑定注册在 apply_lang 之后，
        # 执行时 _CUR_LANG 已更新为新语言，故直接复用 _get_history_page。
        lang_select.change(
            fn=lambda page: _get_history_page(page)[1],
            inputs=history_page,
            outputs=history_page_info,
        )

    return demo
