# -*- coding: utf-8 -*-
"""中文数字与阿拉伯数字互转（法条编号用，支持到千位及"之一/之二"后缀）。"""
import re

_DIG = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
        '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_UNIT = {'十': 10, '百': 100, '千': 1000}
CN_DIGITS = '零一二三四五六七八九'

# 第X条（含"之一"式后缀）匹配
ARTICLE_RE = re.compile(r'第([零一二三四五六七八九十百千两]+)条(之[一二三四五六七八九十]+)?')


def cn2int(s: str) -> int:
    s = s.strip()
    if not s:
        raise ValueError('empty cn num')
    total, num = 0, 0
    for ch in s:
        if ch in _DIG:
            num = _DIG[ch]
        elif ch in _UNIT:
            if num == 0:
                num = 1
            total += num * _UNIT[ch]
            num = 0
        else:
            raise ValueError('bad cn num: %r' % s)
    return total + num


def suffix2int(suf: str) -> int:
    """"之一" -> 1；空串 -> 0。"""
    if not suf:
        return 0
    n = 0
    for ch in suf[1:]:
        n = n * 10 + CN_DIGITS.index(ch)
    return n


def int2cn(n: int) -> str:
    if n < 0:
        raise ValueError('negative')
    if n < 10:
        return CN_DIGITS[n]
    parts = []
    for unit in (1000, 100, 10):
        if n >= unit:
            d = n // unit
            lead = '一' if (unit == 10 and d == 1 and not parts) else CN_DIGITS[d]
            parts.append(lead + {10: '十', 100: '百', 1000: '千'}[unit])
            n %= unit
        elif parts and n:
            parts.append('零')
    if n:
        parts.append(CN_DIGITS[n])
    return ''.join(parts).rstrip('零')


def article_key(article: str) -> tuple:
    """'第二百六十条之二' -> (260, 2)，用于排序与索引。"""
    m = ARTICLE_RE.fullmatch(article)
    if not m:
        raise ValueError('bad article: %r' % article)
    return (cn2int(m.group(1)), suffix2int(m.group(2) or ''))
