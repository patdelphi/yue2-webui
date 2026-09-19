"""Style presets for YuE2 music generation."""

STYLE_PRESETS = {
    "钢琴流行": "English, warm piano pop, expressive female voice, acoustic piano, rounded bass and light drums, lyrical memorable melody, unhurried phrasing, 88 BPM",
    "民谣": "English, indie folk, gentle male vocal, acoustic guitar, harmonica, soft percussion, nostalgic, warm, 96 BPM",
    "摇滚": "English, alternative rock, powerful male vocal, electric guitar, bass, drums, energetic, driving rhythm, 128 BPM",
    "爵士": "English, jazz, smooth female vocal, piano, upright bass, brushed drums, saxophone solo, sophisticated harmony, 110 BPM",
    "电子": "English, EDM, synth lead, four-on-the-floor kick, energetic build-up, drop, female vocal chops, 128 BPM",
    "中文流行": "Mandarin, C-pop, sweet female voice, piano, strings, gentle percussion, emotional ballad, 72 BPM",
    "R&B": "English, R&B, soulful female vocal, electric piano, smooth bass, snap beats, groovy, 90 BPM",
    "古典跨界": "English, classical crossover, operatic tenor, orchestra, strings, timpani, epic, 100 BPM",
}


def get_style_preset_names() -> list[str]:
    """Get list of preset names."""
    return list(STYLE_PRESETS.keys())


def get_style_preset(name: str) -> str | None:
    """Get style text by preset name."""
    return STYLE_PRESETS.get(name)
