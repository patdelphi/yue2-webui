"""Lyrics templates for YuE2 music generation."""

LYRICS_TEMPLATES = {
    "流行 (Verse+Chorus)": """[Verse]
第一段的歌词
第二行歌词
第三行歌词
第四行歌词

[Chorus]
副歌歌词，更加朗朗上口
第二行副歌
第三行副歌
第四行副歌

[Verse]
第二段歌词
新的内容和情感
继续讲故事
推进情节

[Chorus]
重复副歌
强化记忆点
让听众跟唱
最后一句收尾""",

    "民谣 (Verse×3)": """[Verse]
第一段歌词
讲述一个故事
描绘场景和人物
奠定叙事基调

[Verse]
第二段歌词
故事继续发展
新的视角或转折
保持韵脚一致

[Verse]
第三段歌词
故事收尾
留下余韵
让听众回味""",

    "完整 (Intro+Verse+Chorus+Bridge+Outro)": """[Intro]
(前奏，可以是器乐或轻声吟唱)

[Verse]
第一段歌词
建立场景和情绪
描绘画面
引入主题

[Pre-Chorus]
预副歌，制造期待感
情绪逐渐上升

[Chorus]
副歌，全曲最抓耳的部分
核心情感表达
让人记住的旋律
重复的歌名或主题

[Verse]
第二段歌词
深化主题
新的视角或故事发展
保持韵脚一致

[Chorus]
重复副歌
加强印象

[Bridge]
桥段，打破重复
情感转折或高潮
新的和声进行
为最后的高潮铺垫

[Chorus]
最终副歌
最强烈的情感表达
可以略微变化歌词
完美的收尾

[Outro]
尾声
渐渐远去
最后一句话
留白""",
}


def get_template_names() -> list[str]:
    """Get list of template names."""
    return list(LYRICS_TEMPLATES.keys())


def get_lyrics_template(name: str) -> str | None:
    """Get lyrics text by template name."""
    return LYRICS_TEMPLATES.get(name)
