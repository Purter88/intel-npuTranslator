"""HY-MT1.5 官方 prompt 模板（Prompt 与语言，严格照抄官方）。

注意：{target_language} 必须用英文语言名（English / Japanese），不要用语言代码。
"""
from __future__ import annotations

# 中 -> 外
ZH_XX = "将以下文本翻译为{target_language},注意只需要输出翻译后的结果,不要额外解释:\n\n{source_text}"

# 外 -> 外（源非中文）
XX_XX = "Translate the following segment into {target_language}, without additional explanation.\n\n{source_text}"

# 术语干预
TERM = (
    "参考下面的翻译:{terminology} 翻译成 {terminology_target_language}\n"
    "将以下文本翻译为{target_language},注意只需要输出翻译后的结果,不要额外解释:\n{source_text}"
)

# 上下文感知
CTX = (
    "{context}\n参考上面的信息,把下面的文本翻译成{target_language},"
    "注意不需要翻译上文,也不要额外解释:\n{source_text}"
)


def build(source_text: str, target_language: str, source_lang: str = "auto",
          terminology: str | None = None, context: str | None = None) -> str:
    """构造 prompt。

    :param source_text: 待翻译文本
    :param target_language: 目标语言英文名，如 English
    :param source_lang: 源语言代码，zh 时走中文模板
    :param terminology: 术语表（可选）
    :param context: 上下文（可选）
    """
    if context:
        return CTX.format(
            context=context,
            target_language=target_language,
            source_text=source_text,
        )
    if terminology:
        return TERM.format(
            terminology=terminology,
            terminology_target_language=target_language,
            target_language=target_language,
            source_text=source_text,
        )
    if source_lang.lower() in {"zh", "zh-cn", "zh-tw", "yue", "bo", "ug", "kk", "mn", "zh-hans", "zh-hant"}:
        return ZH_XX.format(target_language=target_language, source_text=source_text)
    return XX_XX.format(target_language=target_language, source_text=source_text)
