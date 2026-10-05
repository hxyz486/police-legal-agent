# -*- coding: utf-8 -*-
"""sources 金标形态合并：严格复刻官方样例的 sources 结构。

官方样例（Q7/Q9/Q10）实证形态：
- 单条引用：一个条目，snippet = 条文正文（去“第X条”条头）；
- 同一法律多条：一个条目，article 用顿号连接，snippet 按「条号 正文」分段；
- 跨法且每法恰好一条：一个条目，law_name/article 逐法顿号连接，
  snippet 按「短法名条号：正文」分段；
- 跨法且存在同法多条：每法一个条目（各自落在上面两种形态上）。
"""
import re

_HEAD_RE = re.compile(r'^第[零〇一二三四五六七八九十百千万0-9０-９]+条(?:之[一二三四五六七八九十]+)?\s*')


def article_body(text):
    """条文正文（去条头、去内部换行）——与官方金标单条 snippet 形态一致。"""
    body = _HEAD_RE.sub("", (text or "").strip())
    return re.sub(r'\s*\n\s*', "", body)


def _short_law(law_name):
    return (law_name or "").replace("中华人民共和国", "")


def merge_sources(entries):
    """entries: [{"law_name","article","text"}...]（按引用顺序）→ 金标 sources 列表。"""
    entries = [e for e in entries if e and e.get("law_name") and e.get("article")]
    if not entries:
        return []
    if len(entries) == 1:
        e = entries[0]
        return [{"law_name": e["law_name"], "article": e["article"],
                 "snippet": article_body(e.get("text", ""))}]
    laws = []
    for e in entries:
        if e["law_name"] not in laws:
            laws.append(e["law_name"])
    if len(laws) == 1:
        return [{"law_name": laws[0],
                 "article": "、".join(e["article"] for e in entries),
                 "snippet": "\n".join("%s %s" % (e["article"], article_body(e.get("text", "")))
                                      for e in entries)}]
    groups = [(law, [e for e in entries if e["law_name"] == law]) for law in laws]
    if all(len(g) == 1 for _, g in groups):
        return [{"law_name": "、".join(laws),
                 "article": "、".join(g[0]["article"] for _, g in groups),
                 "snippet": "\n".join("%s%s：%s" % (_short_law(law), g[0]["article"],
                                                   article_body(g[0].get("text", "")))
                                      for law, g in groups)}]
    out = []
    for law, group in groups:
        if len(group) == 1:
            out.append({"law_name": law, "article": group[0]["article"],
                        "snippet": article_body(group[0].get("text", ""))})
        else:
            out.append({"law_name": law,
                        "article": "、".join(e["article"] for e in group),
                        "snippet": "\n".join("%s %s" % (e["article"], article_body(e.get("text", "")))
                                             for e in group)})
    return out
