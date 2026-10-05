# -*- coding: utf-8 -*-
"""统一智能体服务：法律问答（RAG）+ 警情风险研判。

容器内固定监听 8888 端口：
- POST /qa     法律知识问答：{"question": "..."} -> {"answer", "sources"}（引用可溯源）
- POST /assess 警情风险研判：{"text": "警情反馈单全文"} -> 风险人员/等级/法律依据
- GET  /health 健康检查（含知识库/模型通道状态）

启动流程：载入 laws/ 法律 TXT -> 构建向量索引（失败自动重试，未就绪期间
/qa 降级为关键词检索，保证服务始终可用）。法律索引由两个能力共享：
/qa 用于混合检索召回，/assess 用于研判前的法条接地。
"""
import json
import logging
import logging.handlers   # 必须显式导入子模块，否则 RotatingFileHandler 取不到
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import api     # noqa: E402
import laws    # noqa: E402
from retriever import Retriever  # noqa: E402
import qa as qa_mod  # noqa: E402
from risk import assess as risk_assess  # noqa: E402


class _CSTFormatter(logging.Formatter):
    """日志时间固定为北京时间（UTC+8），免装 tzdata，离线镜像也能用。"""

    def formatTime(self, record, datefmt=None):
        ct = time.localtime(record.created + config.LOG_TZ_OFFSET_HOURS * 3600)
        return time.strftime(datefmt or "%Y-%m-%d %H:%M:%S", ct)


LOG_FILE_STATUS = "未初始化"


def setup_logging():
    """日志双写：标准输出 + 文件（带轮转上限），文件开不起来回退 /tmp 并留证据。"""
    global LOG_FILE_STATUS
    handlers = [logging.StreamHandler(sys.stdout)]
    path = config.LOG_PATH
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass  # 显式验证可写（失败要留证据，不能被 except 吞掉）
        handlers.append(logging.handlers.RotatingFileHandler(
            path, maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUP_COUNT, encoding="utf-8"))
        LOG_FILE_STATUS = "已启用 %s（上限%dMB×%d）" % (
            path, config.LOG_MAX_BYTES // (1024 * 1024), config.LOG_BACKUP_COUNT + 1)
    except Exception as e:  # noqa: BLE001
        LOG_FILE_STATUS = "不可用（%s: %s）" % (type(e).__name__, e)
        print("[WARN] 日志文件 %s 不可用：%s: %s" % (path, type(e).__name__, e),
              file=sys.stderr, flush=True)
        try:
            alt = "/tmp/run.log"
            handlers.append(logging.handlers.RotatingFileHandler(
                alt, maxBytes=config.LOG_MAX_BYTES,
                backupCount=config.LOG_BACKUP_COUNT, encoding="utf-8"))
            LOG_FILE_STATUS += "；已回退 /tmp/run.log"
        except Exception as e2:  # noqa: BLE001
            LOG_FILE_STATUS += "；回退也失败（%s），仅标准输出" % type(e2).__name__
            print("[WARN] 日志回退 /tmp/run.log 也失败：%s，仅标准输出"
                  % type(e2).__name__, file=sys.stderr, flush=True)
    fmt = _CSTFormatter("%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    for h in logging.root.handlers:
        h.setFormatter(fmt)
    for name in ("urllib3", "requests"):
        logging.getLogger(name).setLevel(logging.WARNING)


log = logging.getLogger("agent.main")

STATE = {"retriever": None}


def normalize_qa_response(result):
    """严格消毒 /qa 响应：顶层只允许 {answer, sources}，sources 每项只允许
    {law_name, article, snippet} 三个字符串键，绝不流出多余字段/非法类型。"""
    answer = result.get("answer") if isinstance(result, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        answer = "根据检索到的相关法条，暂无法给出完整结论，请结合具体案情核实后适用。"
    sources = []
    raw_sources = result.get("sources") if isinstance(result, dict) else None
    if isinstance(raw_sources, list):
        for s in raw_sources:
            if not isinstance(s, dict):
                continue
            law = s.get("law_name")
            art = s.get("article")
            snip = s.get("snippet")
            law = law if isinstance(law, str) else None
            art = art if isinstance(art, str) else None
            snip = snip if isinstance(snip, str) else None
            if law and art:
                sources.append({
                    "law_name": law.strip(),
                    "article": art.strip(),
                    "snippet": (snip or "").strip(),
                })
    return {"answer": answer.strip(), "sources": sources}


def normalize_assess_response(result):
    """消毒 /assess 响应：固定键集合 + 字符串化，任何异常输入规整为合法结构。"""
    if not isinstance(result, dict):
        result = {}
    exists = bool(result.get("exists"))
    level = result.get("level")
    level = level if isinstance(level, str) and level in ("高", "低", "无") else "无"
    persons = []
    for p in result.get("risk_persons") or []:
        if isinstance(p, dict):
            persons.append({
                "name": str(p.get("name") or ""),
                "id_number": str(p.get("id_number") or ""),
                "level": str(p.get("level") or ""),
                "reason": str(p.get("reason") or ""),
            })
    refs = []
    for r in result.get("law_references") or []:
        if isinstance(r, dict) and r.get("law_name") and r.get("article"):
            refs.append({
                "law_name": str(r["law_name"]).strip(),
                "article": str(r["article"]).strip(),
                "snippet": str(r.get("snippet") or "").strip(),
            })
    return {
        "exists": exists,
        "level": level,
        "risk_persons": persons,
        "law_references": refs,
        "degraded": bool(result.get("degraded")),
        "reason": str(result.get("reason") or ""),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "police-legal-agent/1.0"

    def log_message(self, fmt, *args):  # 走 logging，统一输出
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_GET(self):
        if self.path in ("/health", "/healthz", "/"):
            # 通道状态仅供核对，不影响业务出参
            self._send_json({
                "status": "ok",
                "kb_ready": bool(STATE["retriever"] and STATE["retriever"].ready),
                "llm": STATE.get("llm_ok") is True and "ok" or "degraded",
                "index": STATE.get("index_mode", "unknown"),
                "risk_grounding": config.RISK_GROUNDING,
            })
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        if self.path == "/qa":
            self._handle_qa()
        elif self.path == "/assess":
            self._handle_assess()
        else:
            self._send_json({"error": "not found"}, status=404)

    def _wait_index(self):
        _r = STATE["retriever"]
        if _r is not None and not _r.ready and not _r.index_done.is_set():
            log.warning("索引构建中，本请求等待就绪(最长 %ds)...",
                        config.QA_WAIT_READY_SECONDS)
            _r.index_done.wait(config.QA_WAIT_READY_SECONDS)

    def _handle_qa(self):
        try:
            body = self._read_body()
            question = body.get("question") if isinstance(body, dict) else None
        except Exception as e:  # noqa: BLE001
            log.warning("请求体解析失败：%s: %s", type(e).__name__, e)
            self._send_json({"answer": "请求格式错误，应为 {\"question\": \"...\"}。", "sources": []})
            return

        start = time.time()
        self._wait_index()
        try:
            # 单题硬墙钟：模型端异常/超长思考不会拖死请求，超限即检索原文兜底（不编造）
            result = api._call_with_wallclock(
                lambda: qa_mod.answer(question, STATE["retriever"]),
                config.QA_DEADLINE_SECONDS, default=None)
            if result is None:
                log.error("问答处理超过硬时限 %ds，改用检索原文兜底返回",
                          config.QA_DEADLINE_SECONDS)
                result = qa_mod.fallback_answer(question, STATE["retriever"])
        except Exception as e:  # noqa: BLE001 - 任何异常都不允许中断服务
            log.error("问答处理异常：%s: %s", type(e).__name__, e)
            result = qa_mod.fallback_answer(question, STATE["retriever"])
        # 出参前严格白名单消毒：杜绝多余字段/类型错误
        result = normalize_qa_response(result)
        _secs = time.time() - start
        _srcs = result["sources"]
        _first = ("%s%s" % (_srcs[0]["law_name"], _srcs[0]["article"])) if _srcs else "无"
        log.info("[QA] secs=%.2f route=%s sources=%d first=%s q=%s",
                 _secs, getattr(qa_mod, "LAST_ROUTE", "?"), len(_srcs),
                 _first[-28:], (question or "")[:60].replace("\n", " "))
        self._send_json(result)

    def _handle_assess(self):
        try:
            body = self._read_body()
            text = body.get("text") if isinstance(body, dict) else None
        except Exception as e:  # noqa: BLE001
            log.warning("请求体解析失败：%s: %s", type(e).__name__, e)
            self._send_json(normalize_assess_response(
                {"degraded": True, "reason": "请求格式错误，应为 {\"text\": \"警情反馈单全文\"}"}))
            return
        if not isinstance(text, str) or not text.strip():
            self._send_json(normalize_assess_response(
                {"degraded": True, "reason": "缺少 text 字段（警情反馈单全文）"}))
            return

        start = time.time()
        self._wait_index()
        try:
            # 单条硬墙钟：与 /qa 同策略，超限返回降级结构（assess_one 本身也永不抛异常）
            result = api._call_with_wallclock(
                lambda: risk_assess.assess_one(text, STATE["retriever"]),
                config.RISK_DEADLINE_SECONDS, default=None)
            if result is None:
                result = {"exists": False, "level": "无", "risk_persons": [],
                          "law_references": [], "degraded": True,
                          "reason": "研判超过硬时限 %ds" % config.RISK_DEADLINE_SECONDS}
        except Exception as e:  # noqa: BLE001
            log.error("研判处理异常：%s: %s", type(e).__name__, e)
            result = {"exists": False, "level": "无", "risk_persons": [],
                      "law_references": [], "degraded": True,
                      "reason": "研判异常：%s" % type(e).__name__}
        result = normalize_assess_response(result)
        log.info("[ASSESS] secs=%.2f exists=%s level=%s persons=%d refs=%d degraded=%s",
                 time.time() - start, result["exists"], result["level"],
                 len(result["risk_persons"]), len(result["law_references"]),
                 result["degraded"])
        self._send_json(result)


def main():
    setup_logging()
    log.info("[BOOT] 统一智能体服务启动（法律问答 + 警情风险研判）| version=%s | "
             "port=%d | laws_dir=%s | grounding=%s | log_path=%s | 日志文件=%s | python=%s",
             config.VERSION, config.PORT, config.LAWS_DIR, config.RISK_GROUNDING,
             config.LOG_PATH, LOG_FILE_STATUS, sys.version.split()[0])
    api.probe_llm()
    # 微型真实调用：唯一可信的"模型通道是否打通"判据。
    _llm_ok, _probe_kind, _probe_detail = api.probe_chat()
    if _llm_ok:
        log.info("LLM 预热完成，模型通道=真实可用（%s）：%s", _probe_kind, _probe_detail)
    else:
        log.warning("LLM 预热失败（模型通道不可用，/qa 将走规则合成层，/assess 将降级）：%s",
                    _probe_detail)

    articles = laws.load_kb(config.LAWS_DIR)
    if not articles:
        log.error("知识库为空：请检查 %s 下的法律 TXT 文件", config.LAWS_DIR)
        retriever = Retriever([])
    else:
        log.info("知识库加载: %d 条, %d 部法律: %s", len(articles),
                 len(set(a.law_name for a in articles)),
                 ",".join(sorted(set(a.law_name for a in articles))[:8]))
        retriever = Retriever(articles)
        # 同步构建: 完成前不监听端口, 对外提供的回答一律是完整RAG结果
        retriever.build_index_sync()
    STATE["retriever"] = retriever
    # 服务自检/全链路预热：真跑一遍短问答，避免第一个请求撞上冷启动超时
    _warm = None
    try:
        _t1 = time.time()
        _warm = qa_mod.answer("民警在巡逻中发现有人赌博，应如何处理？", retriever)
        log.info("服务自检预热完成(%.1fs, sources=%d)", time.time() - _t1,
                 len(_warm.get("sources") or []))
    except Exception as _e:  # noqa: BLE001
        log.warning("服务自检预热失败（不影响对外服务）：%s", _e)
    if not _llm_ok:
        api.diagnose_llm()
        api.disable_llm()
    STATE["llm_ok"] = _llm_ok
    STATE["index_mode"] = "vector" if (retriever.vectors is not None) else "keyword"
    log.info("通道状态总览: LLM=%s | 向量索引=%s | 精排=%s | 二次校验=%s | 规则层=%s | 法条接地=%s",
             "真实可用" if _llm_ok else "不可用(兜底层)",
             STATE["index_mode"],
             "已启用" if api._chosen.get("rerank") else "未启用/降级粗排",
             "开" if config.SECOND_PASS else "关",
             config.RULE_MODE, config.RISK_GROUNDING)
    log.info("RAG 索引就绪=%s，开始对外提供服务(端口 %d)", retriever.ready, config.PORT)

    server = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    server.daemon_threads = True
    log.info("HTTP 服务就绪：POST /qa | POST /assess | GET /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("收到退出信号，服务关闭")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
