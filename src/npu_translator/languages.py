"""HY-MT1.5 支持的语种表（33 主流 + 5 民族语/方言）。

英文名用于 prompt 里的 {target_language} —— **必须用英文语言名**（见 SPEC.md · Prompt 与语言）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str
    zh_name: str
    en_name: str  # 送进 prompt 的名字
    native: str = ""
    group: str = "main"  # main | minority
    # ★ 实测（2026-09-11）：少数语种用英文反而不生效，需显式指定送进 prompt 的名字。
    #   zh-Hant 用 "Traditional Chinese" 会让模型直接回吐英文原文，只有 "繁体中文" 有效。
    #   留空表示沿用 en_name。
    prompt_name: str = ""

    @property
    def target_name(self) -> str:
        """实际送进 prompt 的目标语言名。"""
        return self.prompt_name or self.en_name


LANGUAGES: list[Language] = [
    # ---------------- 主流 33 语种 ----------------
    Language("zh", "中文", "Chinese", "中文"),
    # 实测：英文名 Traditional Chinese 无效（模型回吐原文），必须用中文名
    Language("zh-Hant", "繁体中文", "Traditional Chinese", "繁體中文", prompt_name="繁体中文"),
    Language("en", "英语", "English", "English"),
    Language("ja", "日语", "Japanese", "日本語"),
    Language("ko", "韩语", "Korean", "한국어"),
    Language("fr", "法语", "French", "Français"),
    Language("de", "德语", "German", "Deutsch"),
    Language("es", "西班牙语", "Spanish", "Español"),
    Language("pt", "葡萄牙语", "Portuguese", "Português"),
    Language("ru", "俄语", "Russian", "Русский"),
    Language("ar", "阿拉伯语", "Arabic", "العربية"),
    Language("it", "意大利语", "Italian", "Italiano"),
    Language("nl", "荷兰语", "Dutch", "Nederlands"),
    Language("pl", "波兰语", "Polish", "Polski"),
    Language("cs", "捷克语", "Czech", "Čeština"),
    Language("tr", "土耳其语", "Turkish", "Türkçe"),
    Language("th", "泰语", "Thai", "ไทย"),
    Language("vi", "越南语", "Vietnamese", "Tiếng Việt"),
    Language("id", "印尼语", "Indonesian", "Bahasa Indonesia"),
    Language("ms", "马来语", "Malay", "Bahasa Melayu"),
    Language("tl", "菲律宾语", "Tagalog", "Filipino"),
    Language("hi", "印地语", "Hindi", "हिन्दी"),
    Language("bn", "孟加拉语", "Bengali", "বাংলা"),
    Language("ta", "泰米尔语", "Tamil", "தமிழ்"),
    Language("te", "泰卢固语", "Telugu", "తెలుగు"),
    Language("mr", "马拉地语", "Marathi", "मराठी"),
    Language("gu", "古吉拉特语", "Gujarati", "ગુજરાતી"),
    Language("ur", "乌尔都语", "Urdu", "اردو"),
    Language("fa", "波斯语", "Persian", "فارسی"),
    Language("he", "希伯来语", "Hebrew", "עברית"),
    Language("km", "高棉语", "Khmer", "ខ្មែរ"),
    Language("my", "缅甸语", "Burmese", "ဗမာ"),
    Language("uk", "乌克兰语", "Ukrainian", "Українська"),
    # ---------------- 民族语言 / 方言 ----------------
    Language("bo", "藏语", "Tibetan", "བོད་སྐད་", group="minority"),
    Language("kk", "哈萨克语", "Kazakh", "Қазақша", group="minority"),
    Language("mn", "蒙古语", "Mongolian", "Монгол", group="minority"),
    Language("ug", "维吾尔语", "Uyghur", "ئۇيغۇرچە", group="minority"),
    Language("yue", "粤语", "Cantonese", "粵語", group="minority"),
]

# UI 默认展示的常用语种（其余折叠到「更多语言」）
COMMON_CODES = [
    "zh", "zh-Hant", "en", "ja", "ko", "fr", "de", "es", "ru",
    "pt", "it", "ar", "th", "vi", "id", "hi", "tr",
    "nl", "pl", "uk", "yue",
]

# key 统一小写：zh-Hant 这类含大写代码要能用 "zh-hant" 查到
_BY_CODE = {lang.code.lower(): lang for lang in LANGUAGES}


def get(code: str) -> Language | None:
    return _BY_CODE.get(code.lower())


def en_name(code: str) -> str:
    """取英文语言名；未知代码原样返回。"""
    lang = get(code)
    return lang.en_name if lang else code


def zh_name(code: str) -> str:
    lang = get(code)
    return lang.zh_name if lang else code


def target_name(code: str) -> str:
    """取**送进 prompt** 的目标语言名（可能与 en_name 不同，见 Language.prompt_name）。"""
    lang = get(code)
    return lang.target_name if lang else code


def all_codes() -> list[str]:
    return [lang.code for lang in LANGUAGES]


def common() -> list[Language]:
    return [lang for lang in LANGUAGES if lang.code in COMMON_CODES]


def others() -> list[Language]:
    return [lang for lang in LANGUAGES if lang.code not in COMMON_CODES]


def is_supported(code: str) -> bool:
    return code.lower() in _BY_CODE
