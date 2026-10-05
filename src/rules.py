# -*- coding: utf-8 -*-
"""确定性规则合成层：模型不可用时，用检索到的法条原文拼出“结论+处罚幅度+引用”。

设计原则：
- 只用知识库原文片段（抽取/截取），不做任何推演式改写，杜绝编造；
- 结论先行：从条文首句给出“根据《X法》第Y条规定，……”；
- 命中情形项：把条文中的（一）（二）…与问题最相关的一项摘出；
- 处罚/处理幅度：抽取含拘留/罚款/有期徒刑等要件的句子；
- 输出仍带“引用：”行，sources 取整条正文（与 LLM 模式一致）。
"""
import re

_PENALTY_WORDS = ("拘留", "罚款", "警告", "有期徒刑", "拘役", "管制", "罚金",
                  "没收", "吊销", "责令", "收缴", "追缴", "赔偿", "不予处罚",
                  "从轻", "减轻", "从重", "期限", "时限", "日内", "小时内")
_TIME_RE = re.compile(r'[一二三四五六七八九十百千万零两\d]+(?:年|个月|月|日|小时|分钟)')
_ITEM_RE = re.compile(r'（[一二三四五六七八九十]+）')


def _bigrams(s):
    s = re.sub(r'[^\w\u4e00-\u9fff]', '', s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else set()


def _overlap(question, text, idf=None):
    """问题词覆盖率；传入 idf 时按 IDF 加权（稀有词权重高，如“赌博”）。"""
    gq, gd = _bigrams(question), _bigrams(text)
    if not gq:
        return 0.0
    if idf:
        denom = sum(idf.get(g, 1.0) for g in gq) or 1.0
        num = sum(idf.get(g, 1.0) for g in gq if g in gd)
        return num / denom
    return len(gq & gd) / len(gq)


def _sentences(text):
    return [s.strip() for s in re.split(r'(?<=[。；！？])', text or "") if s.strip()]


def _first_sentence(text, cap=150):
    sents = _sentences(text)
    if not sents:
        return (text or "")[:cap]
    s = sents[0]
    return s if len(s) <= cap else s[:cap] + "……"


def _penalty_sentences(text, cap=220):
    out = []
    for s in _sentences(text):
        if any(w in s for w in _PENALTY_WORDS) or _TIME_RE.search(s):
            out.append(s)
        if sum(len(x) for x in out) >= cap:
            break
    joined = "".join(out)
    return joined[:cap] + ("……" if len(joined) > cap else "")


def _best_item(text, question, idf=None):
    """从条文的（一）（二）…各项中，挑与问题重合度最高的一项。"""
    parts = _ITEM_RE.split(text)
    heads = _ITEM_RE.findall(text)
    if not heads:
        return ""
    best, best_score = "", 0.0
    for head, seg in zip(heads, parts[1:]):
        seg = seg.strip()
        if not seg:
            continue
        score = _overlap(question, seg, idf)
        if score > best_score:
            best, best_score = "%s%s" % (head, seg), score
    if best_score < 0.08:
        return ""
    return best if len(best) <= 180 else best[:180] + "……"


def _law_name(a):
    return a.law_name


def compose(question, arts, max_articles=3, max_chars=700, idf=None):
    """把检索到的法条合成一段“结论式”答复；返回 (answer, used_articles)。

    idf：可选，检索器的 bigram IDF 表；传入后按 IDF 加权选条（稀有行为词优先）。
    """
    if not arts:
        return "", []
    ranked = sorted(arts, key=lambda a: -_overlap(question, (a.body or a.text), idf))
    used, clauses = [], []
    for a in ranked:
        if len(used) >= max_articles:
            break
        body = (a.body or a.text).replace("\n", "")
        if not body:
            continue
        law = _law_name(a)
        clause = "根据《%s》%s规定，%s" % (law, a.article, _first_sentence(body))
        item = _best_item(body, question, idf)
        if item:
            clause += "其中%s" % item
        pen = _penalty_sentences(body)
        if pen and pen not in clause:
            clause += "处罚与处理：%s" % pen
        clauses.append(clause)
        used.append(a)
    if not clauses:
        return "", []
    answer = "；".join(clauses)
    if not answer.endswith(("。", "！", "？")):
        answer += "。"
    answer += "\n引用：" + "；".join("《%s》%s" % (a.law_name, a.article) for a in used)
    if len(answer) > max_chars:
        # 优先保留引用行
        cite = "\n引用：" + "；".join("《%s》%s" % (a.law_name, a.article) for a in used)
        answer = answer[:max(0, max_chars - len(cite))] + cite
    return answer, used
