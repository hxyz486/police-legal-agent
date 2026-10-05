# -*- coding: utf-8 -*-
"""模型 API 客户端：LLM / Embedding / Reranker，OpenAI 兼容接口，标准库实现。

模型选择策略：环境变量显式注入的模型名最优先；未注入或不可用时，
按 config 中的候选清单（官方接口文档模型在前，备用模型在后）探测并
自动切换。embedding 模型在索引构建前一次性锁定，运行期不切换，
避免不同模型的向量空间混用；LLM 与 rerank 无状态，可运行期切换。
"""
import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.request

import config

log = logging.getLogger("sait3.api")

_discovered_llm_model = None
_model_lock = threading.Lock()
_chosen = {"embedding": None, "rerank": None, "llm": None}
# rerank 失败组合负面缓存: (model,url,style) -> 失败时间戳, 10 分钟内不再尝试
_rerank_bad = {}
# embedding 白名单模型是否已确认不可用（禁止改调名单外模型）
_embedding_blocked = False
# LLM 通道熔断：启动探测/多次失败后，一段时间内不再尝试（直接走规则合成层）
_llm_disabled_until = 0.0


def disable_llm(seconds=None):
    """熔断 LLM 通道：期间 chat() 立即失败，避免每题都耗尽重试时间。"""
    global _llm_disabled_until
    secs = config.LLM_DISABLED_RETRY_SECONDS if seconds is None else seconds
    _llm_disabled_until = time.time() + max(0, secs)
    log.warning("LLM 通道熔断 %ds（期间走规则合成层，到期自动再探）", secs)


def enable_llm():
    global _llm_disabled_until
    _llm_disabled_until = 0.0


def llm_disabled():
    return time.time() < _llm_disabled_until


class EmptyContentError(RuntimeError):
    """上游 200 但 choices[0].message.content 为空（思考型模型常见）。

    这类失败说明"通道是通的、只是没产出正文"，绝不能据此熔断模型通道。
    """


def _is_content_blocked(e):
    """是否命中上游内容安全拦截（400 data_inspection_failed 类）。

    此类错误重试/换模型/改参数均无效，应整轮短路，避免把一次快速拒绝
    拖成数分钟（评测客户端可能因此超时）。
    """
    s = str(e)
    return any(t in s for t in (
        "data_inspection_failed", "DataInspectionFailed",
        "Algo.DataInspectionFailed", "inappropriate content"))


def _http_err_detail(e):
    """把 HTTPError 的响应体读出来（urllib 默认丢弃），便于定位"参数被拒"的具体原因。"""
    try:
        if isinstance(e, urllib.error.HTTPError):
            body = e.read().decode("utf-8", "replace")
            return "HTTP %s: %s" % (e.code, body[:500])
    except Exception:  # noqa: BLE001
        pass
    return "%s: %s" % (type(e).__name__, e)


def _http_json(url, payload=None, headers=None, method=None, timeout=None,
               retries=None):
    """POST/GET JSON，带重试与指数退避。payload=None 时为 GET。

    每次调用的完整请求/响应(截断预览)与失败原因都记入日志，
    便于评测环境联调时定位模型接口的兼容性问题。
    """
    timeout = timeout or config.HTTP_TIMEOUT
    retries = config.HTTP_RETRIES if retries is None else retries
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    _m = method or ("POST" if data else "GET")
    if payload is not None:
        _pj = json.dumps(payload, ensure_ascii=False)
        log.info("HTTP %s %s 请求(%dB): %s", _m, url, len(_pj),
                 _pj[:600] + ("...(截断)" if len(_pj) > 600 else ""))
    else:
        log.info("HTTP %s %s", _m, url)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {},
                                         method=_m)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                log.info("HTTP %s %s -> %s, %dB: %s", _m, url, resp.status, len(body),
                         body[:600] + ("...(截断)" if len(body) > 600 else ""))
                return json.loads(body)
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("HTTP %s %s 第%d次失败: %s", _m, url, attempt,
                        _http_err_detail(e))
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(_http_err_detail(last_err))


def _http_chat_stream(url, payload, headers, timeout, retries):
    """调用 chat/completions，兼容 SSE 流式与整段 JSON 两种返回。

    默认走 stream=false（与附件5示例/参考实现一致，兼容性最好）；
    设 LLM_STREAM=1 时走 SSE，timeout 语义为"静默超时"（相邻数据块间隔），
    模型持续输出则不掐断。两种返回都会归一成非流式响应结构。
    """
    payload = dict(payload)
    stream_on = bool(payload.get("stream", config.LLM_STREAM))
    payload["stream"] = stream_on
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = dict(headers or {})
    hdrs.setdefault("Accept", "text/event-stream" if stream_on else "application/json")
    _pj = data.decode("utf-8")
    log.info("HTTP POST %s 请求(%dB, stream=%s): %s", url, len(_pj), stream_on,
             _pj[:600] + ("...(截断)" if len(_pj) > 600 else ""))
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs,
                                         method="POST")
            t0 = time.time()
            parts, reasoning, usage, finish = [], [], None, None
            saw_sse, raw_body = False, []
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        raw_body.append(line)  # 网关忽略 stream，整段 JSON
                        continue
                    saw_sse = True
                    pl = line[5:].strip()
                    if pl == "[DONE]":
                        break
                    try:
                        chunk = json.loads(pl)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        raise RuntimeError("流式返回错误：%s" % json.dumps(
                            chunk["error"], ensure_ascii=False)[:300])
                    usage = chunk.get("usage") or usage
                    chs = chunk.get("choices") or []
                    if not chs:
                        continue
                    d = chs[0].get("delta") or {}
                    if d.get("content"):
                        parts.append(d["content"])
                    if d.get("reasoning_content"):
                        reasoning.append(d["reasoning_content"])
                    if chs[0].get("finish_reason"):
                        finish = chs[0]["finish_reason"]
            if not saw_sse:
                body = "\n".join(raw_body).strip()
                log.info("HTTP POST %s -> 非流式响应 %dB, 耗时%.1fs",
                         url, len(body), time.time() - t0)
                return json.loads(body)
            content = "".join(parts)
            _rc = "".join(reasoning)
            log.info("HTTP POST %s -> 流式完成: 回答%d字/思考%d字 finish=%s "
                     "usage=%s, 耗时%.1fs", url, len(content), len(_rc), finish,
                     json.dumps(usage, ensure_ascii=False) if usage else "无",
                     time.time() - t0)
            return {"choices": [{"message": {"content": content,
                                             "reasoning_content": _rc or None},
                                 "finish_reason": finish}],
                    "usage": usage or {}}
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("HTTP POST %s 第%d次失败: %s (静默超时=%ss)",
                        url, attempt, _http_err_detail(e), timeout)
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(_http_err_detail(last_err))


# ---------------- LLM ----------------

def _list_models(url, headers, timeout=10):
    """GET {base}/models，返回通告的模型 id 列表（失败抛异常）。"""
    data = _http_json(url, headers=headers, timeout=timeout, retries=1)
    return [m.get("id") for m in (data.get("data") or []) if m.get("id")]


def probe_llm():
    """启动连通性探测：GET /models，并在可用模型中优先匹配官方文档模型。"""
    global _discovered_llm_model
    try:
        ids = _list_models(config.llm_models_url(), config.llm_headers(), timeout=10)
        if ids:
            log.info("LLM /models 全部可用模型(%d个): %s", len(ids), ", ".join(ids))
            # 白名单家族优先（按归一化名匹配，兼容 models/、/data/models/ 等前缀写法）
            hit = config.match_model_id(ids, config.llm_model_candidates())
            _discovered_llm_model = hit or ids[0]
            if hit:
                log.info("LLM /models 中命中白名单模型：%s", hit)
            log.info("LLM 连通性探测成功，可用模型 %d 个，实际调用模型：%s",
                     len(ids), llm_model_name())
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("LLM 连通性探测失败（不阻断服务）：%s: %s", type(e).__name__, e)
        return False



def llm_model_name():
    if _chosen["llm"]:
        return _chosen["llm"]
    if config.LLM_MODEL:
        return config.LLM_MODEL
    # /models 探测失败且未显式注入时，回退到官方文档模型候选首位
    # （千问3.8-27B），而不是无意义的 "qwen"
    if _discovered_llm_model:
        return _discovered_llm_model
    cands = config.llm_model_candidates()
    return cands[0] if cands else "qwen"


def _extract_choice(data):
    """取 (content, reasoning, finish_reason)；无 choices 抛 ValueError。"""
    chs = data.get("choices") or []
    if not chs:
        raise ValueError("LLM 响应无 choices：%s"
                         % json.dumps(data, ensure_ascii=False)[:300])
    ch = chs[0]
    msg = ch.get("message") or ch.get("delta") or {}
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    return str(content), str(reasoning), ch.get("finish_reason")


# 思考文本里"正文起点"的常见标记（取最后一次出现位置之后的内容）
_ANS_MARKERS = ("最终答案：", "最终答案:", "答案：", "答案:", "结论：", "结论:",
                "综上，", "综上:", "因此，")


def _answer_from_reasoning(text):
    """思考型模型只回 reasoning_content 时，从中提取可作答的正文。

    评测网关的千问3.8-27B 默认思考，且网关可能只回 reasoning_content
    （content=null），此时把思考尾巴当答案总好过整题兜底。
    """
    if not text:
        return ""
    t = re.sub(r"</?think>", "", str(text)).strip()
    best = ""
    for m in _ANS_MARKERS:
        i = t.rfind(m)
        if i >= 0:
            cand = t[i + len(m):].strip()
            if len(cand) > len(best):
                best = cand
    if best:
        return best
    paras = [p.strip() for p in re.split(r"\n+", t) if p.strip()]
    for p in reversed(paras):
        if len(p) >= 30:
            return p
    # 没有明确"答案"标记时，过短的思考碎片（如被 length 截断后的"用户"）
    # 不足以当作答，返回空串让上层按失败处理，避免把垃圾当答案。
    return t if len(t) >= 30 else ""


def _payload_variants(base, enable_thinking):
    """按兼容性从高到低生成 (描述, 请求体) 变体列表，供正式调用与启动诊断共用。

    首个变体固定为附件5 官方文档参数：chat_template_kwargs.reasoning_effort
    （文档写明默认 high，建议 medium 及以下）。真实网关认这个字段，而
    enable_thinking/thinking_budget 属 dashscope 风格扩展，网关会忽略
    （表现为"发了 enable_thinking=false 仍然思考"）。
    """
    effort = config.LLM_REASONING_EFFORT or ("low" if not enable_thinking else "medium")
    doc_style = dict(base)
    doc_style["chat_template_kwargs"] = {"reasoning_effort": effort}
    if enable_thinking:
        # 首选：官方 reasoning_effort + dashscope 思考预算双写。
        # 双写是刻意的——dashscope 类网关认 enable_thinking/thinking_budget 并据此
        # 把思考截到预算内（实测把单题从 50s+ 压到 5s 级）；评测内网网关忽略这两个
        # 未知字段、只认 chat_template_kwargs，因此对它是无害的。
        full = dict(doc_style)
        full["enable_thinking"] = True
        full["thinking_budget"] = config.LLM_THINKING_BUDGET
        top_effort = dict(base)
        top_effort["reasoning_effort"] = effort
        lean = {k: v for k, v in full.items()
                if k not in ("top_p", "presence_penalty")}
        variants = [
            ("官方reasoning_effort=%s+思考预算%d" % (effort, config.LLM_THINKING_BUDGET), full),
            ("仅官方chat_template_kwargs.reasoning_effort=%s" % effort, doc_style),
            ("精简采样参数", lean),
            ("顶层reasoning_effort=%s" % effort, top_effort),
            ("纯OpenAI裸参数", dict(base)),
        ]
    else:
        off = dict(doc_style)
        off["enable_thinking"] = False
        off["thinking_budget"] = config.LLM_THINKING_BUDGET
        bare_off = dict(base)
        bare_off["enable_thinking"] = False
        top_effort = dict(base)
        top_effort["reasoning_effort"] = "low"
        variants = [
            ("官方reasoning_effort=low+关闭思考", off),
            ("仅官方chat_template_kwargs.reasoning_effort=low", doc_style),
            ("enable_thinking=false", bare_off),
            ("顶层reasoning_effort=low", top_effort),
            ("纯OpenAI裸参数", dict(base)),
        ]
    uniq, seen = [], set()
    for desc, p in variants:
        k = json.dumps(p, ensure_ascii=False, sort_keys=True)
        if k not in seen:
            seen.add(k)
            uniq.append((desc, p))
    return uniq


def _looks_truncated(content, finish):
    """响应是否被 token 预算截断（不能当作完整答案）。

    实测两个真实网关在小预算下的表现：
      评测网关 max_tokens=1 → content=null, reasoning_content="用户", finish=length
      dev 网关  max_tokens=1 → content="民警"(2字), reasoning=279字, finish=length
    后者说明**不能只看"正文是否为空"**：非空的碎片（"用户"/"民警"）一样是截断产物，
    被当成正式答案就会得到"调用 3.8 只输出了用户两个字"这种结果。
    """
    if finish != "length":
        return False
    if not content:
        return True
    # 我们的作答要求 ≤400 字；撞 length 却短于该上限，基本可判截断
    # （哪怕是长正文，末尾没有句终标点也说明被切断，例如缺了"引用："行）
    if len(content) < 400:
        return True
    return not re.search(r"[。！？；…”』」）\)]\s*$", content)


def _escalation_ladder(base_tokens):
    """预算阶梯：base → ×4 → ×16（上限 ceiling），并保证有 256/1024 两个下限档。

    下限档是给"调用方传了极小预算"（如预热探针误传 1）兜底的：只按倍率放大时
    1→4→16 仍然远不够思考型模型用完思考再输出正文。
    """
    ceiling = config.LLM_MAX_TOKENS_CEILING
    rungs = [base_tokens]
    for f in (4, 16):
        v = min(base_tokens * f, ceiling)
        if v > rungs[-1]:
            rungs.append(v)
    for floor in (256, 1024, ceiling):
        if floor > rungs[-1]:
            rungs.append(floor)
    return [r for r in rungs if 0 < r <= ceiling] or [base_tokens]


def diagnose_llm():
    """启动诊断：逐一尝试 模型×参数变体，日志给出每个组合的成败与拒绝原因。

    成功判据放宽为"HTTP 200 且返回 choices"：思考型模型在小预算下正文为空
    但通道完全可用，不能判为失败（否则会误熔断整条模型通道）。
    """
    log.info("开始 LLM 参数兼容性诊断 ...")
    for model in config.llm_model_candidates()[:3]:
        base = {"model": model,
                "messages": [{"role": "user", "content": "1加1等于几？只回答数字。"}],
                "stream": bool(config.LLM_STREAM),
                "temperature": 0.0, "top_p": 1.0, "presence_penalty": 0.0,
                "max_tokens": max(64, min(config.LLM_MAX_TOKENS, 512))}
        for desc, payload in _payload_variants(base, config.LLM_ENABLE_THINKING):
            try:
                data = _http_chat_stream(config.llm_chat_url(), payload=payload,
                                         headers=config.llm_headers(), timeout=60,
                                         retries=1)
                content, reasoning, finish = _extract_choice(data)
                log.info("诊断成功：model=%s 参数=%s（通道可用，正文%d字/思考%d字 "
                         "finish=%s）", model, desc, len(content), len(reasoning), finish)
                return True
            except Exception as e:  # noqa: BLE001
                log.warning("诊断失败：model=%s 参数=%s -> %s", model, desc,
                            _http_err_detail(e)[:300])
    log.error("诊断结论：所有 模型×参数 组合均被拒；请核对 dsh 配置（或 LLM_* 环境变量）提供的通道与模型 Code")
    return False


def chat(messages, temperature=0.0, max_tokens=None, timeout=None, thinking=None,
         retries=None):
    """调用 chat/completions，返回 assistant 文本。失败抛异常。

    - 兼容非流式整段 JSON 与 SSE 流式两种返回（默认 stream=false）。
    - **截断即重试**：finish_reason=length（含"正文非空但只是碎片"的情况）视为
      不完整答案，按预算阶梯放大 max_tokens 重试；阶梯见 _escalation_ladder。
    - 正文为空但 reasoning_content 非空（思考型模型）：先放大预算重试，仍无正文
      则从思考文本提取作答；过短的思考碎片（如"用户"）判为无正文。
    - 该路径不会熔断通道——通道是通的，只是预算不够。
    - 首选模型调用失败时按候选清单自动切换（官方文档模型优先），
      切换成功后锁定，后续调用直接使用。
    - thinking=None 按全局配置；False 走 reasoning_effort=low（省思考延迟）。
    """
    order = config.unique_models([llm_model_name()] + config.llm_model_candidates())
    last_err = None
    if llm_disabled():
        raise RuntimeError("LLM_DISABLED（通道熔断中，走规则合成层）")
    retries = config.LLM_RETRIES if retries is None else retries
    enable_thinking = config.LLM_ENABLE_THINKING if thinking is None else bool(thinking)
    base_tokens = int(max_tokens or config.LLM_MAX_TOKENS)
    ladder = _escalation_ladder(base_tokens) if config.LLM_TOKEN_ESCALATE \
        else [base_tokens]
    _call_t0 = time.time()
    saw_transport_error = False

    for i, model in enumerate(order):
        for ti, tok in enumerate(ladder):
            base = {
                "model": model,
                "messages": messages,
                "stream": bool(config.LLM_STREAM),
                "temperature": temperature,
                "top_p": 1.0,
                "presence_penalty": 0.0,
                "max_tokens": tok,
            }
            payloads = _payload_variants(base, enable_thinking)
            log.info("LLM[%s] 请求: %d条消息/%d字, thinking=%s, budget=%s, "
                     "effort=%s, max_tokens=%d",
                     model, len(messages),
                     sum(len(m.get("content") or "") for m in messages),
                     enable_thinking, config.LLM_THINKING_BUDGET,
                     config.LLM_REASONING_EFFORT if enable_thinking else "low", tok)
            data = None
            for pi, (desc, pl) in enumerate(payloads):
                if pi:
                    log.warning("LLM[%s] 上一变体被拒，改用参数变体：%s (%d/%d)",
                                model, desc, pi + 1, len(payloads))
                try:
                    data = _http_chat_stream(config.llm_chat_url(), payload=pl,
                                             headers=config.llm_headers(),
                                             timeout=timeout or config.LLM_TIMEOUT,
                                             retries=retries if i == 0 and pi == 0 else 1)
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if _is_content_blocked(e):
                        # 内容安全拦截：换模型/换参数/重试均无意义，立即短路（走检索兜底）
                        log.error("LLM 输出被内容安全拦截(%s)，立即放弃重试与换模型：%s",
                                  model, str(e)[:160])
                        raise e
                    saw_transport_error = True
                    log.warning("LLM 模型[%s]调用失败：%s: %s", model, type(e).__name__, e)
            if data is None:
                break  # 参数被拒与预算无关，换下一个模型
            next_budget = False
            try:
                content, reasoning, finish = _extract_choice(data)
                if reasoning:
                    log.info("LLM[%s] 思考过程(%d字): %s", model, len(reasoning),
                             json.dumps(reasoning[:800], ensure_ascii=False))
                truncated = _looks_truncated(content, finish)
                more_budget = ti + 1 < len(ladder)
                in_time = (time.time() - _call_t0) < config.LLM_ESCALATE_MAX_ELAPSED
                if truncated and more_budget and in_time:
                    log.warning("LLM[%s] 响应被长度截断(finish=length, max_tokens=%d, "
                                "正文%d字)，放大预算重试 -> %d",
                                model, tok, len(content), ladder[ti + 1])
                    next_budget = True
                elif truncated:
                    # 预算已到顶或已耗时过久：只能用现有内容，但绝不用碎片当答案
                    log.warning("LLM[%s] 响应仍被截断(finish=length, 正文%d字, "
                                "预算已到顶=%s, 超时放弃放大=%s)",
                                model, len(content), not more_budget, not in_time)
                    if len(content) < 40 and len(reasoning) >= 40:
                        alt = _answer_from_reasoning(reasoning)
                        if len(alt) >= 30 and len(alt) > len(content) * 3:
                            log.warning("LLM[%s] 正文是碎片(%d字)，改用思考文本作答(%d字)",
                                        model, len(content), len(alt))
                            content = alt
                if not content:
                    if reasoning:
                        content = _answer_from_reasoning(reasoning)
                        log.warning("LLM[%s] 正文为空，改用 reasoning_content "
                                    "提取作答(%d字)", model, len(content))
                    if not content:
                        raise EmptyContentError(
                            "LLM content为空! finish_reason=%s 原始响应: %s"
                            % (finish, json.dumps(data, ensure_ascii=False)[:1000]))
                if not next_budget:
                    log.info("LLM[%s] 回答(%d字) finish_reason=%s usage=%s",
                             model, len(content), finish,
                             json.dumps(data.get("usage") or {}, ensure_ascii=False))
                    if model != order[0]:
                        log.warning("LLM 模型自动切换：%s -> %s", order[0], model)
                        with _model_lock:
                            _chosen["llm"] = model
                    enable_llm()
                    return content
            except EmptyContentError as e:
                last_err = e
                log.warning("LLM 模型[%s]响应无正文：%s", model, str(e)[:400])
                next_budget = (ti + 1 < len(ladder)
                               and (time.time() - _call_t0) < config.LLM_ESCALATE_MAX_ELAPSED)
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("LLM 模型[%s]响应解析失败：%s: %s", model, type(e).__name__, e)
            if next_budget:
                continue  # 换更大的 token 预算再试


    # 全部组合失败：只有"网络/HTTP 层"失败才熔断；正文为空说明通道可达，
    # 不熔断（否则会把一次预算不足放大成整场评测静默降级）
    if last_err is None:
        last_err = EmptyContentError("LLM 所有模型×参数组合均未返回可用正文")
    if saw_transport_error and not _is_content_blocked(last_err):
        disable_llm()
    raise last_err


def probe_chat(timeout=None):
    """启动预热探测：返回 (ok, kind, detail)。

    ok=True 表示模型通道可用——产出正文(content)或只产出思考
    (reasoning_only) 都算可用；只有网络/HTTP 层失败才 ok=False。
    """
    try:
        txt = chat([{"role": "user", "content": "1加1等于几？只回答数字。"}],
                   temperature=0.0, max_tokens=config.LLM_WARMUP_MAX_TOKENS,
                   timeout=timeout or 60, thinking=False, retries=1)
        return True, "content", (txt or "")[:120]
    except EmptyContentError as e:
        return True, "reasoning_only", str(e)[:200]
    except Exception as e:  # noqa: BLE001
        return False, "error", _http_err_detail(e)[:300]



# ---------------- Embedding ----------------

def _probe_embedding(model):
    """小请求探测 embedding 模型是否可用，失败抛异常。"""
    data = _http_json(config.embedding_url(),
                      payload={"model": model, "input": ["连通性测试"]},
                      headers=config.embedding_headers(), timeout=10, retries=1)
    if not (data.get("data") or []):
        raise ValueError("embedding 响应无数据")


def ensure_embedding_model(force=False):
    """选定 embedding 模型并锁定，返回模型名；白名单模型全部不可用时返回 None。

    赛题硬约束：只允许调用规定模型（Qwen3-Embedding-0.6B 及文档别名），
    任何候选不可用时绝不自动改调其它模型——全部探测失败即判定 embedding
    不可用并置 blocked 标记，上层转入关键词模式（服务保持可用，不崩溃）。
    force=True 允许重新探测（上层在阻塞等待超时后可能再试一次）。
    """
    global _embedding_blocked
    with _model_lock:
        if _chosen["embedding"] and not force:
            return _chosen["embedding"]
        if _embedding_blocked and not force:
            return None
        cands = config.embedding_model_candidates()
        chosen, first_err = None, None
        for m in cands:
            try:
                _probe_embedding(m)
                chosen = m
                break
            except Exception as e:  # noqa: BLE001
                if first_err is None:
                    first_err = e
                log.warning("embedding 模型[%s]不可用：%s: %s", m, type(e).__name__, e)
        if chosen is None:
            # 白名单写法全被拒时，问一次该端点的 /models：若通告的 id 归一化后
            # 仍属于白名单家族（如 models/Qwen3-Embedding-0.6B），就用通告的
            # 原始 id 再试一次。名单外的模型一律不碰。
            try:
                _ids = _list_models(config.embedding_models_url(),
                                    config.embedding_headers(), timeout=10)
                _hit = config.match_model_id(_ids, cands)
                if _hit:
                    log.info("embedding /models 通告 id：%s（命中白名单家族）", _hit)
                    _probe_embedding(_hit)
                    chosen = _hit
            except Exception as e:  # noqa: BLE001
                log.warning("embedding /models 探测未成功：%s: %s", type(e).__name__, e)
        if chosen is None:
            # 白名单内无可用模型：不调用其它模型，进入关键词降级
            _embedding_blocked = True
            _chosen["embedding"] = None
            log.error("embedding 白名单模型全部不可用（仅限 Qwen3-Embedding-0.6B 及"
                      "文档别名），服务转入关键词模式（不调用其它模型）：%s",
                      first_err)
            return None
        _embedding_blocked = False
        if first_err is not None:
            log.warning("embedding 模型自动切换：%s -> %s", cands[0], chosen)
        else:
            log.info("embedding 模型选定：%s", chosen)
        _chosen["embedding"] = chosen
        return chosen


def embedding_blocked():
    """embedding 白名单模型是否已确认全部不可用（此时不得调用任何其它模型）。"""
    return _embedding_blocked


def embed_texts(texts, timeout=None):
    """批量向量：texts -> [[float, ...], ...]，与输入顺序一致。

    使用已锁定的模型（ensure_embedding_model），失败抛异常由上层处理，
    不在运行期自动换模型。
    """
    if _embedding_blocked:
        raise ValueError("embedding 白名单模型不可用，已转入关键词模式（禁止调用其它模型）")
    model = _chosen["embedding"] or config.EMBEDDING_MODEL
    if not model:
        raise ValueError("embedding 模型未就绪")
    payload = {"model": model, "input": texts}
    t0 = time.time()
    data = _http_json(config.embedding_url(), payload=payload,
                      headers=config.embedding_headers(), timeout=timeout)
    items = data.get("data") or []
    if len(items) != len(texts):
        raise ValueError("embedding 返回数量不符：%d != %d" % (len(items), len(texts)))
    items.sort(key=lambda x: x.get("index", 0))
    vecs = [it["embedding"] for it in items]
    log.info("embedding[%s] %d条 -> %d维, 耗时%.2fs", model, len(texts),
             len(vecs[0]) if vecs and vecs[0] else 0, time.time() - t0)
    return vecs


# ---------------- Rerank ----------------

def _rerank_payload(style, model, query, documents, top_n):
    """按端点风格构造请求体。"""
    if style == "native":
        return {"model": model,
                "input": {"query": query, "documents": documents},
                "parameters": {"return_documents": False, "top_n": top_n}}
    return {"model": model, "query": query, "documents": documents,
            "top_n": top_n}


def _parse_rerank(data):
    """统一解析两种响应: OpenAI兼容(results/data) 与 DashScope原生(output.results)。"""
    items = data.get("results") or data.get("data")
    if not items and isinstance(data.get("output"), dict):
        items = data["output"].get("results")
    out = []
    for it in items or []:
        idx = it.get("index")
        score = it.get("relevance_score", it.get("score", 0.0))
        if idx is not None:
            out.append((int(idx), float(score)))
    return out


def _call_rerank(url, style, model, query, documents, top_n, timeout, retries):
    data = _http_json(url, payload=_rerank_payload(style, model, query, documents, top_n),
                      headers=config.rerank_headers(), timeout=timeout, retries=retries)
    out = _parse_rerank(data)
    if not out:
        raise ValueError("rerank 响应无结果：%s" % json.dumps(data, ensure_ascii=False)[:200])
    out.sort(key=lambda x: -x[1])
    return out


def _rerank_combos(models):
    """生成 (model, url, style) 组合：模型与端点风格按名字亲和排序。

    qwen3.7-text-rerank/gte-rerank 系走 DashScope原生路由优先;
    qwen3-rerank/Qwen3-Reranker 系走 OpenAI兼容路由优先。
    """
    eps = config.rerank_endpoint_candidates()
    combos = []
    for m in models:
        ml = m.lower()
        if "text-rerank" in ml or "gte-rerank" in ml:
            order = sorted(eps, key=lambda x: 0 if x[1] == "native" else 1)
        elif "rerank" in ml:
            order = sorted(eps, key=lambda x: 0 if x[1] == "compatible" else 1)
        else:
            order = eps
        for u, s in order:
            combos.append((m, u, s))
    return combos


def rerank(query, documents, top_n=None, timeout=None):
    """精排：返回 [(idx, score), ...] 按分数降序；全部组合失败抛异常。

    自动兼容当前平台的三类路由与两种请求/响应格式:
    - {origin}/compatible-api/v1/reranks      qwen3-rerank, OpenAI兼容风格
    - {origin}/api/v1/services/rerank/text-rerank/text-rerank
                                              qwen3.7-text-rerank等, DashScope原生风格
    - {origin}/compatible-mode/v1/rerank、/v1/rerank  旧式兼容路由
    首个成功的 (模型, 端点, 风格) 组合会被锁定，失败自动遍历其余组合。
    """
    chosen = _chosen["rerank"]
    models = config.unique_models(
        [chosen["model"] if chosen else None] + config.rerank_model_candidates())
    combos = []
    if chosen:
        combos.append((chosen["model"], chosen["url"], chosen["style"]))
    for m, u, s in _rerank_combos(models):
        if (m, u, s) not in combos:
            combos.append((m, u, s))
    # 负面缓存：端点/风格 4xx 或连接类失败短时间内不再重试；
    # 若所有组合都已被缓存，直接放弃精排（上层退回粗排顺序），不再逐个重试。
    now = time.time()
    fresh = [(m, u, s) for (m, u, s) in combos
             if (m, u, s) not in _rerank_bad
             or now - _rerank_bad[(m, u, s)] > 600]
    if not fresh:
        log.warning("rerank 全部组合均处于负面缓存，直接退回粗排（不再重试）")
        raise RuntimeError("RERANK_ALL_CACHED")
    combos = fresh
    last_err = None
    for i, (model, url, style) in enumerate(combos):
        try:
            out = _call_rerank(url, style, model, query, documents,
                               top_n or len(documents), timeout,
                               config.HTTP_RETRIES if i == 0 else 1)
        except urllib.error.HTTPError as e:  # 4xx 客户端错误缓存；429/5xx 等瞬态除外
            last_err = e
            if e.code in (400, 401, 403, 404, 405, 415, 422):
                with _model_lock:
                    _rerank_bad[(model, url, style)] = time.time()
            log.warning("rerank[%s@%s:%s]调用失败：%s: %s", model, style, url,
                        type(e).__name__, e)
            continue
        except Exception as e:  # noqa: BLE001
            last_err = e
            # 连接类/协议类失败也缓存（否则每个请求都会重试全部注定失败的组合）
            if not isinstance(e, urllib.error.HTTPError):
                with _model_lock:
                    _rerank_bad[(model, url, style)] = time.time()
            log.warning("rerank[%s@%s:%s]调用失败：%s: %s", model, style, url,
                        type(e).__name__, e)
            continue
        if combos[0] != (model, url, style):
            log.warning("rerank 自动切换: %s@%s -> %s@%s[%s]",
                        combos[0][0], combos[0][2], model, url, style)
        with _model_lock:
            _chosen["rerank"] = {"model": model, "url": url, "style": style}
        log.info("rerank[%s@%s] %d文档重排结果: %s", model, style, len(documents),
                 [(idx, round(s, 4)) for idx, s in out])
        return out
    raise last_err


_qe_deadline = None


def _call_with_wallclock(fn, deadline_secs, default=None):
    """给不可中断的长调用加硬墙钟时限：超时返回 default（线程仍在后台自然结束）。

    流式"静默超时"不限制总时长；个别模型对个别问题会超长思考（已观察到最长数分钟），
    对查询扩展/二次校验等非关键环节必须设总时限，避免单题被拖到客户端超时。
    """
    if deadline_secs is None or deadline_secs <= 0:
        try:
            return fn()
        except BaseException:  # noqa: BLE001
            return default
    q = queue.Queue(maxsize=1)

    def _run():
        try:
            q.put(("ok", fn()))
        except BaseException as e:  # noqa: BLE001
            q.put(("err", e))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    try:
        status, val = q.get(timeout=deadline_secs)
    except queue.Empty:
        log.warning("调用超过硬时限 %ss，放弃等待并按默认处理（后台线程自动结束）",
                    deadline_secs)
        return default
    if status == "ok":
        return val
    raise val


# ---------------- 查询扩展 ----------------

_QE_SYSTEM = "你是法律条文检索的查询扩展器，只输出改写后的检索查询，不回答问题。"

_QE_PROMPT = (
    "把民警的问题改写为%d条用于法律条文库检索的查询，用于召回相关法条：\n"
    "1. 把口语表述替换为规范法律术语并补充同义关键词"
    "（如\"吸毒\"应写作\"吸食、注射毒品 治安管理处罚\"）；\n"
    "2. 补充问题隐含的法律要点关键词（行为定性、处罚种类与幅度、办案程序等）；\n"
    "3. 问题包含多个子问题的，每个子问题单独写一条。\n"
    "只输出查询本身，每行一条，不要编号、不要解释、不要书名号。"
)


def expand_query(question):
    """用 LLM 把问题改写为若干条检索查询（法律术语化）。

    返回额外变体列表（不含原问题）；任何失败/超时返回 []，上层退回单查询检索。
    思考关闭并施加硬墙钟时限，防止个别问题触发超长思考拖垮单题延迟。
    """
    if not config.QUERY_EXPANSION:
        return []
    messages = [
        {"role": "system", "content": _QE_SYSTEM},
        {"role": "user", "content": _QE_PROMPT % config.QE_MAX_VARIANTS
         + "\n\n民警的问题：" + question},
    ]
    try:
        t0 = time.time()
        raw = _call_with_wallclock(
            lambda: chat(messages, temperature=0.0,
                         max_tokens=config.QE_MAX_TOKENS,
                         timeout=config.QE_TIMEOUT, thinking=False, retries=1),
            config.QE_HARD_DEADLINE_SECONDS)
        if raw is None:
            log.warning("查询扩展超硬时限(%.1fs)，退回单查询检索",
                        time.time() - t0)
            return []
        out = []
        for ln in raw.splitlines():
            ln = ln.strip()
            # 去掉 LLM 可能自带的行首编号/项目符号
            ln = re.sub(r'^\s*(?:[\d①-⑩]{1,2}|[一二三四五六七八九十]{1,3})'
                        r'\s*[、.．:：,，)）]\s*', '', ln)
            ln = re.sub(r'^[\-\*—·]+\s*', '', ln)
            ln = ln.strip("。；;，, 　\"“”")
            # 关键词串往往较长（"醉酒的人 保护性约束措施 约束至酒醒 …"），
            # 上限放到 80 字；过长的说明性文字仍会被挡掉。
            if 2 <= len(ln) <= 80 and ln != question and ln not in out:
                out.append(ln)
        out = out[:config.QE_MAX_VARIANTS]
        log.info("查询扩展(%.1fs): %s -> %s", time.time() - t0,
                 question[:40], out)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("查询扩展失败，退回单查询检索：%s: %s", type(e).__name__, e)
        return []
