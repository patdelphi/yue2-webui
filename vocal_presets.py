"""Vocal and instrument presets for quick style selection."""


VOCAL_PRESETS = {
    "女声": "female voice",
    "男声": "male voice",
    "女高音": "soprano female voice",
    "女低音": "alto female voice",
    "男高音": "tenor male voice",
    "男中音": "baritone male voice",
    "男低音": "bass male voice",
    "温柔女声": "gentle female voice",
    "温柔男声": "gentle male voice",
    "力量女声": "powerful female voice",
    "力量男声": "powerful male voice",
    "甜美女声": "sweet female voice",
    "沙哑男声": "raspy male voice",
    "丝滑女声": "smooth female voice",
    "深情女声": "soulful female voice",
    "清澈女声": "clear female voice",
    "气声女声": "breathy female voice",
    "歌剧男高音": "operatic tenor",
    "年轻女声": "young female voice",
    "年轻男声": "young male voice",
    "成熟女声": "mature female voice",
    "成熟男声": "mature male voice",
}


INSTRUMENT_PRESETS = {
    "钢琴": "piano",
    "原声吉他": "acoustic guitar",
    "电吉他": "electric guitar",
    "贝斯": "bass",
    "鼓": "drums",
    "弦乐": "strings",
    "小提琴": "violin",
    "大提琴": "cello",
    "长笛": "flute",
    "萨克斯": "saxophone",
    "小号": "trumpet",
    "合成器": "synthesizer",
    "电子琴": "electric piano",
    "口琴": "harmonica",
    "尤克里里": "ukulele",
    "手风琴": "accordion",
    "竖琴": "harp",
    "管风琴": "organ",
}


MOOD_PRESETS = {
    "欢快": "happy, upbeat, cheerful",
    "悲伤": "sad, melancholic, emotional",
    "浪漫": "romantic, loving, tender",
    "激情": "passionate, intense, dramatic",
    "平静": "calm, peaceful, relaxing",
    "神秘": "mysterious, atmospheric, ethereal",
    "史诗": "epic, grand, cinematic",
    "怀旧": "nostalgic, warm, sentimental",
    "活力": "energetic, dynamic, lively",
    "忧郁": "melancholic, moody, introspective",
    "甜美": "sweet, cute, playful",
    "酷炫": "cool, edgy, stylish",
}


def get_vocal_preset_names() -> list[str]:
    """Get list of vocal preset names."""
    return list(VOCAL_PRESETS.keys())


def get_vocal_preset(name: str) -> str | None:
    """Get vocal preset text by name."""
    return VOCAL_PRESETS.get(name)


def get_instrument_preset_names() -> list[str]:
    """Get list of instrument preset names."""
    return list(INSTRUMENT_PRESETS.keys())


def get_instrument_preset(name: str) -> str | None:
    """Get instrument preset text by name."""
    return INSTRUMENT_PRESETS.get(name)


def get_mood_preset_names() -> list[str]:
    """Get list of mood preset names."""
    return list(MOOD_PRESETS.keys())


def get_mood_preset(name: str) -> str | None:
    """Get mood preset text by name."""
    return MOOD_PRESETS.get(name)
