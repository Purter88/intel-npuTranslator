from npu_translator.languages import LANGUAGES, all_codes, en_name, get, is_supported, zh_name
from npu_translator.prompt import build


def test_language_count():
    # 33 主流（含繁体中文 zh-Hant）+ 5 民族语/方言
    assert len(LANGUAGES) == 38


def test_traditional_chinese_supported():
    assert is_supported("zh-Hant")
    assert is_supported("zh-hant")  # 大小写不敏感
    assert en_name("zh-Hant") == "Traditional Chinese"


def test_unique_codes():
    assert len(all_codes()) == len(set(all_codes()))


def test_en_name():
    assert en_name("en") == "English"
    assert en_name("ja") == "Japanese"
    assert en_name("zh") == "Chinese"


def test_unknown_code_falls_back():
    assert en_name("xx") == "xx"
    assert get("xx") is None
    assert not is_supported("xx")


def test_zh_name():
    assert zh_name("ko") == "韩语"


def test_prompt_zh_source():
    p = build("你好", "English", source_lang="zh")
    assert "将以下文本翻译为English" in p
    assert "你好" in p


def test_prompt_non_zh_source():
    p = build("Hello", "Chinese", source_lang="en")
    assert p.startswith("Translate the following segment into Chinese")


def test_prompt_with_terminology():
    p = build("GPU", "Chinese", source_lang="en", terminology="GPU -> 图形处理器")
    assert "参考下面的翻译" in p


def test_target_name_resolution():
    """语言代码必须查表转换，不能用 islower() 判断（zh-Hant 含大写会漏判）。"""
    from npu_translator.engine import TranslateEngine

    # zh-Hant 必须出中文名：实测英文名 Traditional Chinese 会让模型回吐原文
    assert TranslateEngine._target_name("zh-Hant") == "繁体中文"
    assert TranslateEngine._target_name("zh-hant") == "繁体中文"
    assert TranslateEngine._target_name("en") == "English"
    assert TranslateEngine._target_name("Japanese") == "Japanese"  # 已是语言名则原样


def test_target_name_property_falls_back_to_en_name():
    from npu_translator.languages import get, target_name

    assert get("zh-Hant").target_name == "繁体中文"
    assert target_name("ja") == "Japanese"  # 未设 prompt_name 时沿用 en_name


def test_prompt_with_context():
    p = build("it", "Chinese", source_lang="en", context="上文提到 GPU")
    assert "参考上面的信息" in p
