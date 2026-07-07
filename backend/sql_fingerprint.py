# -*- coding: utf-8 -*-
"""
SQL 指纹归一化 (Percona pt-query-digest 风格)

把 SQL 归一化为稳定模板，等价 SQL 共享同一 fingerprint，便于聚合统计。

算法：
    1. 剥离注释 (--, #, /* */)
    2. 字符串字面量 → '?'     （'..' ".." `..` N'..' 0x.. b'..'）
    3. 数字字面量 → ?
    4. IN (?, ?, ...) → IN (?)
    5. 连续 3+ 个占位符 → ?, ?, ?
    6. 折叠空白
    7. 首关键字小写
    8. MD5 → fingerprint_id
"""

import hashlib
import re
from typing import Tuple


# 1. 注释剥离（保留换行）
_RE_LINE_COMMENT = re.compile(r"(?:--[^\n]*|#[^\n]*)")
_RE_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# 2. 字符串字面量（含反引号标识符 / N'..' / 0x.. / b'..' / B'..'）
#     用非贪婪匹配到下一个未转义的结束符；容忍 \' \" \\
_RE_STRING = re.compile(
    r"""
    (?:
        N?'(?:\\.|[^'\\])*'           |   # N'..' 或 '..'
        N?"(?:\\.|[^"\\])*"           |   # N".." 或 ".."
        `(?:\\.|[^`\\])*`             |   # 反引号标识符
        0x[0-9A-Fa-f]+                |   # 十六进制字面量
        [bB]'(?:\\.|[^'\\])*'         |   # b'..'
        [bB]"(?:\\.|[^"\\])*"             # b".."
    )
    """,
    re.VERBOSE | re.DOTALL,
)

# 3. 数字字面量：整数、浮点、指数、可选负号（不在标识符后）
#     用 lookbehind 避免吃掉 "col-1" 这种减号
_RE_NUMBER = re.compile(r"(?<![A-Za-z_])-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")

# 4. IN (?, ?, ...) → IN (?)
_RE_IN_LIST = re.compile(r"IN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", re.IGNORECASE)

# 5. 连续 3+ 个 ?, ?, ? → ?, ?, ? （保留 3 个以便阅读）
_RE_PLACEHOLDER_RUN = re.compile(r"(\?(?:\s*,\s*\?){3,})")


def _strip_comments(sql: str) -> str:
    sql = _RE_BLOCK_COMMENT.sub(" ", sql)
    sql = _RE_LINE_COMMENT.sub(" ", sql)
    return sql


def _replace_strings(sql: str) -> str:
    return _RE_STRING.sub("?", sql)


def _replace_numbers(sql: str) -> str:
    return _RE_NUMBER.sub("?", sql)


def _collapse_in_lists(sql: str) -> str:
    return _RE_IN_LIST.sub("IN (?)", sql)


def _collapse_placeholder_runs(sql: str) -> str:
    return _RE_PLACEHOLDER_RUN.sub("?, ?, ?", sql)


def _collapse_whitespace(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _lowercase_leading_keyword(sql: str) -> str:
    """只把首关键字小写，避免破坏标识符大小写。"""
    match = re.match(r"\s*([A-Za-z_]+)", sql)
    if not match:
        return sql
    keyword = match.group(1)
    rest = sql[match.end():]
    return keyword.lower() + rest


def normalize(sql: str) -> Tuple[str, str]:
    """归一化 SQL，返回 (template, md5_fingerprint)。"""
    if sql is None:
        return "", hashlib.md5(b"").hexdigest()

    template = _strip_comments(sql)
    template = _replace_strings(template)
    template = _replace_numbers(template)
    template = _collapse_in_lists(template)
    template = _collapse_placeholder_runs(template)
    template = _collapse_whitespace(template)
    template = _lowercase_leading_keyword(template)

    fingerprint = hashlib.md5(template.encode("utf-8")).hexdigest()
    return template, fingerprint


# 兼容旧调用风格
def fingerprint(sql: str) -> str:
    """仅返回 md5 fingerprint。"""
    return normalize(sql)[1]