# -*- coding: utf-8 -*-
"""知识库解析：从 /app/laws/*.txt 载入法律法规，切成以"条"为单位的碎片。

文件格式约定：
  第 1 行：法律名称（如"治安管理处罚法"）
  第 2 行：版本说明
  正文：编/章/节标题独立成行；每条以"第X条"开头，条文内可多行。
"""
import logging
import os
import re

import cnnum
import config

log = logging.getLogger("sait3.laws")


class Article(object):
    __slots__ = ("law_name", "article", "text", "body", "chunk")

    def __init__(self, law_name, article, text):
        self.law_name = law_name
        self.article = article
        self.text = text  # 含"第X条"开头的条文原文（知识库原文）
        # 条文正文（不含"第X条"前缀），snippet 从这里截取，与样例集参考格式一致
        self.body = _strip_article_prefix(text, article)
        self.chunk = "%s%s %s" % (law_name, article, text)  # 送入向量模型的文本

    def to_dict(self):
        return {"law_name": self.law_name, "article": self.article, "text": self.text}


def _strip_article_prefix(text, article):
    body = text
    for sep in ("　", " ", "  "):
        if body.startswith(article):
            body = body[len(article):]
            if body.startswith(sep):
                body = body[len(sep):]
            break
    return body or text


_STRUCT_RE = re.compile(r'^(第[零一二三四五六七八九十百千]+[编章节]|附则)$')
_ART_LINE_RE = re.compile(r'^(第[零一二三四五六七八九十百千两]+条(之[一二三四五六七八九十]+)?)')


def parse_law_file(path):
    """解析单个法律 TXT，返回 (law_name, [Article, ...])。"""
    with open(path, encoding="utf-8") as f:
        lines = [ln.rstrip("\n").strip() for ln in f]
    lines = [ln for ln in lines if ln]
    if len(lines) < 3:
        log.warning("法律文件内容过短，跳过：%s", path)
        return None, []

    law_name = lines[0].strip("《》 ")
    # 第 2 行为版本说明；正文从第 3 行起
    articles = []
    cur_no, cur_buf = None, []

    def flush():
        if cur_no and cur_buf:
            text = "\n".join(cur_buf).strip()
            if text:
                articles.append(Article(law_name, cur_no, text))

    for ln in lines[2:]:
        if _STRUCT_RE.match(ln):
            continue  # 编/章/节标题不进入条文
        m = _ART_LINE_RE.match(ln)
        if m:
            flush()
            cur_no, cur_buf = m.group(1), [ln]
        elif cur_no:
            cur_buf.append(ln)
        # 条文开始前的散行丢弃
    flush()
    return law_name, articles


def load_kb(laws_dir):
    """载入目录下全部法律文件。返回按文件名排序的 [Article, ...]。"""
    articles = []
    if not os.path.isdir(laws_dir):
        log.error("知识库目录不存在：%s", laws_dir)
        return articles
    for fn in sorted(os.listdir(laws_dir)):
        if not fn.endswith(".txt"):
            continue
        path = os.path.join(laws_dir, fn)
        try:
            law_name, arts = parse_law_file(path)
        except Exception as e:  # noqa: BLE001 - 单文件损坏不拖垮整体
            log.error("解析失败 %s：%s: %s", fn, type(e).__name__, e)
            continue
        if law_name and arts:
            log.info("载入《%s》：%d 条（%s）", law_name, len(arts), arts[0].article)
            articles.extend(arts)
    log.info("知识库构建完成：%d 个文件，共 %d 条法条", len(articles), len(articles))
    return articles


def extract_snippet(article: Article, query: str, max_chars: int = None) -> str:
    """从条文正文（不含条号前缀）中截取与问题最相关的连续片段。

    片段为知识库原文的连续子串，保证与知识库逐字一致。以连续句子窗口打分。
    首尾空白（含全角空格）剥除——参考片段均从正文首字开始，不去掉会
    子串匹配失败。默认长度取 config.SNIPPET_MAX_CHARS。
    """
    if max_chars is None:
        max_chars = config.SNIPPET_MAX_CHARS
    text = article.body.replace("\n", "").strip()
    if len(text) <= max_chars:
        return text
    sents = re.split(r'(?<=[。；？！])', text)
    sents = [s for s in sents if s]
    if not sents:
        return text[:max_chars]
    qgrams = _bigrams(query)
    n = len(sents)
    best, best_score = text[:max_chars], -1.0
    for start in range(n):
        total, grams = 0, set()
        for end in range(start, n):
            total += len(sents[end])
            if total > max_chars:
                break
            grams |= _bigrams(sents[end])
            score = len(qgrams & grams) / (len(qgrams) or 1)
            if score > best_score:
                best_score = score
                best = "".join(sents[start:end + 1])
    return best.strip()


def _bigrams(s):
    s = re.sub(r'[，。、；：？！""''（）《》\s,.;:?!"\'()<>]', '', s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}
