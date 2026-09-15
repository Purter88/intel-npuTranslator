from npu_translator.postprocess import clean, strip_fence, strip_prefix, strip_wrapping_quotes


def test_strip_prefix_chinese():
    assert strip_prefix("以下是翻译：今天天气不错") == "今天天气不错"


def test_strip_prefix_english():
    assert strip_prefix("Here is the translation: Hello world") == "Hello world"


def test_strip_prefix_leaves_normal_text():
    text = "今天天气不错"
    assert strip_prefix(text) == text


def test_strip_wrapping_quotes_paired():
    assert strip_wrapping_quotes('"Hello world"') == "Hello world"
    assert strip_wrapping_quotes("“你好”") == "你好"


def test_strip_wrapping_quotes_keeps_inner_quotes():
    text = '"He said "hi" to me"'
    assert strip_wrapping_quotes(text) == text


def test_strip_fence():
    assert strip_fence("```\nHello\n```") == "Hello"


def test_clean_combined():
    assert clean("  以下是翻译：\n\n今天天气不错  \n") == "今天天气不错"


def test_clean_empty():
    assert clean("") == ""
    assert clean("   ") == ""


def test_clean_preserves_multiline():
    text = "第一行\n第二行"
    assert clean(text) == text
