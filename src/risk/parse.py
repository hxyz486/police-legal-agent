# -*- coding: utf-8 -*-
"""响应解析（三重兜底）+ 后处理规则引擎（去重/掩码保留/等级合并/枚举校验）。"""
import json
import logging
import re

log = logging.getLogger("saiti.parse")

LEVEL_RANK = {"无": 0, "低": 1, "高": 2}


def extract_json(text: str):
    """从 LLM 文本中提取 JSON 对象。直接解析 -> 剥离代码块 -> 正则截取。"""
    if text is None:
        raise ValueError("响应为空")
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 剥离 ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 截取首个 { 到最后一个 } 之间
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"无法从响应中解析 JSON：{text[:200]!r}")


def _clean(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def parse_result(content: str):
    """解析 LLM 输出为规范化结果 dict。

    返回 {"persons": [{"name","id_number","level","reason"}], "has_risk": bool}
    """
    data = extract_json(content)
    raw_persons = data.get("risk_persons") or data.get("风险人员") or []
    if isinstance(raw_persons, dict):
        raw_persons = [raw_persons]

    persons = []
    for p in raw_persons:
        if not isinstance(p, dict):
            continue
        name = _clean(p.get("name") or p.get("姓名"))
        idn = _clean(p.get("id_number") or p.get("身份证号") or p.get("id"))
        level = _clean(p.get("level") or p.get("风险等级"))
        reason = _clean(p.get("reason") or p.get("依据") or p.get("判断依据"))
        if not name and not idn:
            # 查无身份的风险人员（如不愿登记信息）：有认定理由则保留，姓名证号输出留空
            if not reason:
                continue
        if level not in LEVEL_RANK or level == "无":
            level = "低"  # 被识别为风险人员但等级缺失/非法时，按低兜底
        persons.append({"name": name, "id_number": idn, "level": level, "reason": reason})
    return {"persons": persons, "has_risk": bool(persons)}


def dedupe(persons):
    """同一警情内同一人员去重：身份证号优先，缺失按姓名。首次出现为准。"""
    seen, out = set(), []
    for p in persons:
        key = ("id", p["id_number"]) if p["id_number"] else ("name", p["name"])
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def build_row(persons):
    """按输出规范组装除编号/出警情况外的 5 个字段。

    姓名证号留空但仍认定有风险的人员（不愿登记信息）计 exists=true；
    多人员姓名、证号分别用英文逗号连接（空值跳过）。
    """
    if not persons:
        return "", "", "false", "无", "无"
    persons = dedupe(persons)
    names = ",".join(p["name"] for p in persons if p["name"])
    ids = ",".join(p["id_number"] for p in persons if p["id_number"])
    reasons = ",".join(
        f"{p['name']}：{p['reason']}" if p["name"] else f"相关人员：{p['reason']}"
        for p in persons)
    overall = max((p["level"] for p in persons), key=lambda lv: LEVEL_RANK[lv])
    return names, ids, "true", overall, reasons


def process_content(content: str):
    """LLM 响应文本 -> 5 个输出字段。"""
    return build_row(parse_result(content)["persons"])
