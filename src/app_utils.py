"""生成/设置页的纯常量与无状态工具（C1 拆分 app.py：第一阶段）。

本模块只收纳「无状态」内容，供 app.py 直接导入复用，不依赖 app.py 的任何
全局状态（界面语言、路径、Gradio 组件），因此可以独立导入与测试：

- 内置参数预设 BUILTIN_PRESETS、预设参数字段顺序 PRESET_PARAM_KEYS
- 输出格式标签 FORMAT_LABELS、歌词注释前缀 COMMENT_PREFIXES
- 歌词去注释 strip_comment_lines()
"""

# 内置参数预设：名称 → {description: 说明, params: 参数键值}
BUILTIN_PRESETS = {
    "默认": {
        "description": "标准质量",
        "params": {"cot": "full", "num_inference_steps": 8},
    },
    "快速demo": {
        "description": "最快出结果",
        "params": {"cot": "off", "num_inference_steps": 4},
    },
    "高质量": {
        "description": "最佳质量",
        "params": {"cot": "full", "num_inference_steps": 32, "out_format": "pcm24"},
    },
    "创意模式": {
        "description": "更多样化",
        "params": {"cot": "full", "num_inference_steps": 8, "sem_temp": 1.5, "sem_top_p": 0.98},
    },
    "保守模式": {
        "description": "最稳定",
        "params": {"cot": "full", "num_inference_steps": 8, "sem_temp": 0.3, "sem_rep_penalty": 1.5},
    },
}

# 预设参数字段顺序（单一来源）：与预设保存/加载的 Gradio 组件绑定顺序严格一致。
# 保存时按此顺序 zip 输入值，加载时按此顺序返回，避免位置参数错位。
# 前 3 项（cot/num_inference_steps/out_format）加载时缺失走具体默认值，
# 其余字段缺失返回 gr.update()（前端保持当前值）。
PRESET_PARAM_KEYS = (
    "cot", "num_inference_steps", "out_format",
    "abc_temp", "abc_top_p", "abc_top_k",
    "abc_rep_penalty", "abc_pen_window", "abc_min_tok", "abc_max_tok",
    "sem_temp", "sem_top_p", "sem_top_k",
    "sem_rep_penalty", "sem_pen_window", "sem_min_tok", "sem_max_tok",
    "cfg_scale", "batch_count",
    "pp_normalize", "pp_fade", "pp_trim", "pp_metadata",
)

FORMAT_LABELS = {"pcm16": "PCM 16-bit", "pcm24": "PCM 24-bit", "float32": "Float 32-bit"}

COMMENT_PREFIXES = ("//", "**")


def strip_comment_lines(lyrics: str) -> str:
    """移除歌词中的注释行（以 // 或 ** 开头的行）。"""
    if not lyrics:
        return lyrics
    return "\n".join(
        line for line in lyrics.split("\n")
        if not line.strip().startswith(COMMENT_PREFIXES)
    )
