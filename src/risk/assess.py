# -*- coding: utf-8 -*-
"""警情风险研判（源自赛题一实现，合并进统一智能体）。

三种使用方式：
- HTTP 单条：src/service.py 的 POST /assess -> assess_one(text, retriever)
- 批量流水线：run_batch(input.xlsx, output.xlsx, retriever)（保留赛题一全套算法）
- 命令行：python src/assess_batch.py [input.xlsx output.xlsx]

流程（批量）：读 input.xlsx -> 并发研判（双投票共识）-> 解析+规则后处理
-> 失败补跑 -> 写 output.xlsx（分批原子落盘）-> 自校验。
整合点：RISK_GROUNDING=1 时，研判前先从共享法律索引（与 /qa 同一套）
检索相关法条注入提示词，让研判结论可附带可溯源的法律依据；检索失败
静默降级，不影响原研判行为。
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from . import llm
from . import overrides
from . import parse as parse_mod
from . import prompt as prompt_mod
from . import reader
from . import validate as validate_mod
from . import writer

DEGRADED_REASON = "模型调用失败，降级输出"


def setup_logging():
    import os
    import sys
    os.makedirs(os.path.dirname(config.LOG_PATH) or ".", exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    try:
        fh = logging.FileHandler(config.LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        print(f"[WARN] 无法写日志文件 {config.LOG_PATH}: {e}", file=sys.stderr)


def _retrieval_query(text: str) -> str:
    """接地检索的查询串：优先取【警情内容及处置情况】段落——动作与情节都在
    这里；【当事人信息】的登记表头（姓名/性别/出生年月/身份证号）会稀释
    关键词得分，把检索带偏到程序规定尾部条文（实测缺陷）。缺失时回退全文。"""
    if "【警情内容及处置情况】" in text:
        seg = text.split("【警情内容及处置情况】", 1)[1]
        for stop in ("【", "\n\n"):
            idx = seg.find(stop)
            if idx > 0:
                seg = seg[:idx]
        seg = seg.strip()
        if seg:
            return seg[:1000]
    return text[:1000]


def legal_context(text: str, retriever):
    """从共享法律索引检索与警情相关的法条，生成提示词接地文本块。

    任何失败（索引未就绪/检索异常）都返回 None——接地是纯增益，
    绝不能因为它影响研判主链路。
    """
    # 顶层统一配置（src/config.py，含 RISK_GROUNDING 开关）。
    # 注意：src 目录在 sys.path 上，绝对导入 `import config` 解析到顶层配置；
    # `from . import config` 解析到 risk 包自己的 config（LLM/批量参数），两者职责不同。
    import config as app_config
    if retriever is None or not getattr(retriever, "ready", False):
        return None
    if not app_config.RISK_GROUNDING:
        return None
    try:
        arts = retriever.search(_retrieval_query(text), top_k=app_config.RISK_GROUNDING_TOP_K)
    except Exception:  # noqa: BLE001
        return None
    if not arts:
        return None
    n = app_config.RISK_GROUNDING_SNIPPET_CHARS
    lines = []
    for a in arts:
        body = (a.body or a.text or "")[:n]
        lines.append(f"- 《{a.law_name}》{a.article}：{body}")
    return ("【相关法律条文参考】以下为系统中检索到的可能与本警情相关的条文，"
            "仅供判定理由引用法律依据时参考，判定标准以上文为准：\n" + "\n".join(lines))


def _judge_once(text: str, retriever=None):
    """单次研判：LLM -> 解析 -> 规则后处理 -> 特例适配，返回 (5元组, 原始人员列表)。"""
    content = llm.chat(prompt_mod.build_messages(
        text[:config.MAX_INPUT_CHARS], legal_context(text, retriever)))
    row5 = parse_mod.process_content(content)
    if config.OVERRIDES_ENABLED:
        row5 = overrides.apply(text, *row5)
    persons = _persons_from_content(content)
    return row5, persons


def _persons_from_content(content: str):
    """从模型原始输出提取结构化人员列表（提取失败返回 None，不影响 5 元组主链路）。"""
    try:
        data = parse_mod.extract_json(content)
        persons = data.get("risk_persons")
        if isinstance(persons, list):
            out = []
            for p in persons:
                if not isinstance(p, dict):
                    continue
                out.append({
                    "name": str(p.get("name") or ""),
                    "id_number": str(p.get("id_number") or ""),
                    "level": str(p.get("level") or ""),
                    "reason": str(p.get("reason") or ""),
                })
            return out
    except Exception:  # noqa: BLE001
        pass
    return None


def _vote_key(row5):
    """投票键：姓名/证号/等级一致即视为同一研判结果（依据文字允许措辞差异）。"""
    names, ids, _exists, level, _reasons = row5
    return (names, ids, level)


def _is_no_risk(row5) -> bool:
    """首次研判结论为无风险（无人员、无等级）时无需二次投票。"""
    names, ids, _exists, level, _reasons = row5
    return names == "" and ids == "" and level == "无"


def _verify_no_risk(text: str, rid: str) -> bool:
    """无风险复核器：轻量验证调用，返回 True 表示发现风险线索（需升级研判）。

    复核调用失败时按"未发现"处理（fail-open），维持原判不阻断流程。
    """
    try:
        content = llm.chat(prompt_mod.build_verify_messages(text[:config.MAX_INPUT_CHARS]),
                           max_tokens=2048, timeout=30, retries=2)
        data = parse_mod.extract_json(content)
        exists = bool(data.get("exists"))
        if exists:
            logging.getLogger("risk.assess").info(
                "记录 %s 复核发现风险线索: %s", rid, data.get("names"))
        return exists
    except Exception as e:  # noqa: BLE001
        logging.getLogger("risk.assess").warning(
            "记录 %s 复核不可用，维持原判：%s: %s", rid, type(e).__name__, e)
        return False


def _judge_consensus(text: str, rid: str, retriever=None):
    """共识投票：研判出风险人员的记录才做第二次投票（边界案例才值得花双倍成本），
    两次一致即采纳；不一致时加一轮仲裁取多数。无风险结论单次即采纳。

    任一次调用异常不致命：有一次成功就用成功的那次，全部失败返回 None。
    """
    log = logging.getLogger("risk.assess")
    r1 = None
    p1 = None
    try:
        r1, p1 = _judge_once(text, retriever)
    except Exception as e:  # noqa: BLE001
        log.warning("记录 %s 第 1 次研判失败：%s: %s", rid, type(e).__name__, e)
    if not config.CONSENSUS:
        return r1, p1
    if r1 is not None and _is_no_risk(r1):
        # 无风险复核：轻量验证发现风险线索时，升级为第二次完整研判
        if config.VERIFY_NO_RISK and _verify_no_risk(text, rid):
            try:
                r2, p2 = _judge_once(text, retriever)
            except Exception as e:  # noqa: BLE001
                log.warning("记录 %s 升级研判失败，维持原判：%s: %s", rid, type(e).__name__, e)
                return r1, p1
            if r2 is not None and not _is_no_risk(r2):
                log.info("记录 %s 经复核升级为风险人员", rid)
                return r2, p2
        return r1, p1
    r2 = None  # 先初始化：第 2 次调用抛异常时下方引用不炸，保留 r1 成功结果
    p2 = None
    try:
        r2, p2 = _judge_once(text, retriever)
    except Exception as e:  # noqa: BLE001
        log.warning("记录 %s 第 2 次研判失败：%s: %s", rid, type(e).__name__, e)
    if r1 is None or r2 is None:
        return r1 or r2, p1 or p2
    k1, k2 = _vote_key(r1), _vote_key(r2)
    if k1 == k2:
        return r1, p1 or p2
    log.info("记录 %s 两次研判不一致，启动第 3 次仲裁", rid)
    try:
        r3, p3 = _judge_once(text, retriever)
    except Exception as e:  # noqa: BLE001
        log.warning("记录 %s 仲裁失败：%s: %s", rid, type(e).__name__, e)
        return r1, p1
    votes = [_vote_key(x) for x in (r1, r2, r3) if x is not None]
    if votes.count(k2) > votes.count(k1):
        return r2, p2 or p1
    return r1, p1


def process_record(rid: str, text: str, retriever=None):
    """单条记录研判，返回 7 元组。任何异常降级为无风险行。"""
    try:
        row5, _persons = _judge_consensus(text, rid, retriever)
        if row5 is None:
            raise RuntimeError("全部研判尝试均失败")
        return rid, text, *row5
    except Exception as e:  # noqa: BLE001 - 单条失败绝不拖垮整体
        logging.getLogger("risk.assess").error(
            "记录 %s 研判失败，降级为无风险：%s: %s", rid, type(e).__name__, e)
        return rid, text, "", "", "false", "无", DEGRADED_REASON


def assess_one(text: str, retriever=None):
    """单条警情研判（HTTP /assess 用）：返回结构化 dict，永不抛异常。

    返回：{exists, level, risk_persons[], law_references[], degraded, reason}
    """
    import json as _json
    log = logging.getLogger("risk.assess")
    refs = []
    try:
        ctx = legal_context(text, retriever)
        if ctx is None:
            refs = []
        else:
            # legal_context 里的行首就是 "- 《法名》第X条：..."，解析回结构便于程序消费
            for line in ctx.splitlines():
                line = line.strip()
                if line.startswith("- 《") and "》" in line:
                    body = line[3:]
                    law = body.split("》", 1)[0]
                    rest = body.split("》", 1)[1]
                    if "：" in rest:
                        art, snip = rest.split("：", 1)
                        refs.append({"law_name": law, "article": art, "snippet": snip})
    except Exception:  # noqa: BLE001
        refs = []
    try:
        row5, persons = _judge_consensus(text, "HTTP", retriever)
        if row5 is None:
            raise RuntimeError("全部研判尝试均失败")
        names, ids, exists, level, reasons = row5
        # 只保留研判理由/人员依据里实际援引的条文：接地检索是宽召回的"参考"，
        # 把模型没采用的无关条文原样列进 law_references 会误导上层智能体（实测缺陷）。
        hay = " ".join([reasons or ""] +
                       [p.get("reason", "") for p in (persons or []) if isinstance(p, dict)])

        def _cited(r):
            law = r.get("law_name", "")
            short = law.replace("中华人民共和国", "", 1)
            return (short and short in hay and r.get("article", "") in hay) or \
                   (law in hay and r.get("article", "") in hay)
        refs = [r for r in refs if _cited(r)]
        out = {
            "exists": str(exists).strip().lower() == "true",
            "level": level,
            "risk_persons": persons if persons else [],
            "law_references": refs,
            "degraded": False,
            "reason": reasons,
            "summary": {"names": names, "id_numbers": ids},
        }
        log.info("[ASSESS] exists=%s level=%s persons=%d refs=%d",
                 out["exists"], level, len(out["risk_persons"]), len(refs))
        return out
    except Exception as e:  # noqa: BLE001 - 与 /qa 同策略：任何异常都返回合法 JSON
        log.error("研判失败，返回降级结构：%s: %s", type(e).__name__, e)
        return {
            "exists": False,
            "level": "无",
            "risk_persons": [],
            "law_references": refs,
            "degraded": True,
            "reason": f"{DEGRADED_REASON}（{_json.dumps(str(e), ensure_ascii=False)[:200]}）",
            "summary": {"names": "", "id_numbers": ""},
        }


def run_batch(input_path: str, output_path: str, retriever=None):
    """批量流水线（赛题一原样）：并发研判 -> 补跑 -> 原子落盘 -> 自校验。"""
    log = logging.getLogger("risk.assess")
    log.info("批量研判启动，输入=%s 输出=%s 共识=%s 补跑=%s 落盘间隔=%s 接地=%s",
             input_path, output_path, config.CONSENSUS, config.RETRY_FAILED,
             config.FLUSH_EVERY, bool(retriever and app_config.RISK_GROUNDING))
    llm.probe()
    records = reader.read_records(input_path)
    if not records:
        log.error("无有效输入记录，写出空表头输出文件")
        writer.write_rows([], output_path)
        return 0

    results = [None] * len(records)
    done = 0
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        futures = {pool.submit(process_record, rid, text, retriever): i
                   for i, (rid, text) in enumerate(records)}
        for fut in as_completed(futures):
            idx = futures[fut]
            results[idx] = fut.result()
            done += 1
            # 分批落盘：未完成的位置先以无风险占位，保证任何时刻文件都是完整格式
            if config.FLUSH_EVERY and done % config.FLUSH_EVERY == 0 and done < len(records):
                snapshot = [r if r is not None else (records[i][0], records[i][1],
                                                     "", "", "false", "无", "处理中")
                            for i, r in enumerate(results)]
                writer.write_rows(snapshot, output_path, atomic=True)

    # 失败补跑：对降级行统一再试一轮
    if config.RETRY_FAILED:
        failed = [i for i, r in enumerate(results) if r is not None and r[6] == DEGRADED_REASON]
        if failed:
            log.info("补跑 %d 条降级记录", len(failed))
            for i in failed:
                rid, text = records[i]
                results[i] = process_record(rid, text, retriever)
            log.info("补跑完成")

    writer.write_rows(results, output_path)
    issues = validate_mod.validate(output_path, records)
    if issues:
        log.warning("自检发现 %d 个问题：%s", len(issues), issues)
        # 重写一次（内容本身已兜底，重写主要排除磁盘瞬时问题）
        writer.write_rows(results, output_path)
        issues = validate_mod.validate(output_path, records)
        if issues:
            log.error("自检仍未通过（保留已写文件）：%s", issues)
    else:
        log.info("自检通过")
    log.info("处理完成，共 %d 条", len(results))
    return 0


def main(argv=None):
    """命令行入口：python -m src.assess_batch [input.xlsx output.xlsx]。"""
    import os
    import sys
    setup_logging()
    log = logging.getLogger("risk.assess")
    argv = list(sys.argv[1:] if argv is None else argv)
    input_path = argv[0] if len(argv) > 0 else config.INPUT_PATH
    output_path = argv[1] if len(argv) > 1 else config.OUTPUT_PATH

    retriever = None
    try:
        # 法条接地（可选）：共享知识库构建失败时批量研判照常进行
        import config as app_config
        import laws as laws_mod
        from retriever import Retriever
        articles = laws_mod.load_kb(app_config.LAWS_DIR)
        if articles:
            retriever = Retriever(articles)
            retriever.build_index_sync()
            log.info("法条接地索引就绪：%d 条", len(articles))
    except Exception as e:  # noqa: BLE001
        log.warning("法条接地不可用，按原始研判行为执行：%s: %s", type(e).__name__, e)
        retriever = None

    try:
        return run_batch(input_path, output_path, retriever)
    except Exception as e:  # noqa: BLE001 - 顶层兜底：宁可降级输出也不崩
        log.exception("主流程异常，尝试全量降级输出：%s: %s", type(e).__name__, e)
        try:
            records = reader.read_records(input_path)
        except Exception:  # noqa: BLE001
            records = []
        try:
            writer.write_degraded(records, output_path)
            log.info("降级输出已写出（%d 条）", len(records))
        except Exception:  # noqa: BLE001
            log.critical("降级输出也失败，无法生成输出文件")
            return 1
        return 0
