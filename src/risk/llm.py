# -*- coding: utf-8 -*-
"""LLM 客户端：OpenAI 兼容 /chat/completions，标准库实现。

兼容策略（针对评测环境模型 Code / 参数形态不明的部署）：
1. 模型名自动降级梯：按候选清单逐个尝试，直到请求成功；
2. 载荷自动降级：先带 chat_template_kwargs，被拒(400/422)则去掉重试；
3. 成功后缓存生效的 模型+载荷 组合，后续调用直连，无额外开销。
"""
import json
import logging
import os
import time
import urllib.error
import urllib.request

from . import config

log = logging.getLogger("saiti.llm")

_discovered_model = None
_WORKING = {"model": None, "with_ctk": True}


def _http_json(req: urllib.request.Request, timeout: int):
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _auth_headers():
    # 按《附件5》3.3：请求/响应均为 application/json; charset=utf-8，UTF-8 编码
    h = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    if config.LLM_API_KEY:
        key = config.LLM_API_KEY
        if not key.lower().startswith("bearer "):
            key = "Bearer " + key
        h["Authorization"] = key
    return h


def probe() -> bool:
    """启动连通性探测：GET /models。成功返回 True（失败不阻断主流程）。"""
    global _discovered_model
    try:
        req = urllib.request.Request(config.models_url(), headers=_auth_headers(), method="GET")
        data = _http_json(req, timeout=10)
        ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        if ids:
            _discovered_model = ids[0]
            log.info("连通性探测成功，可用模型 %d 个，首个：%s", len(ids), ids[0])
        else:
            log.warning("连通性探测成功但未返回模型列表")
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("连通性探测失败（不阻断主流程）：%s: %s", type(e).__name__, e)
        return False


def _candidate_models() -> list:
    """模型名候选：显式 LLM_MODEL > 附件5 的两种写法 > 探测发现 > 兜底。"""
    cands = []
    if config.LLM_MODEL:
        cands.append(config.LLM_MODEL)
    else:
        # 附件5 总览表模型Code 为中文「千问3.8-27B」，请求示例为 qwen3.8-27B，
        # DashScope 实测为小写 qwen3.8-27b——三者都要覆盖
        cands += ["qwen3.8-27B", "千问3.8-27B", "qwen3.8-27b", "qwen"]
    if _discovered_model and _discovered_model not in cands:
        cands.append(_discovered_model)
    if _WORKING["model"] and _WORKING["model"] not in cands:
        cands.insert(0, _WORKING["model"])  # 已打通的放最前
    return cands


def _combo_payload(model: str, messages, temperature: float, max_tokens, with_ctk: bool):
    p = {"model": model, "messages": messages,
         "temperature": temperature,
         "max_tokens": max_tokens or config.LLM_MAX_TOKENS}
    if with_ctk:
        # 附件5：思考深度经 chat_template_kwargs 控制；LLM_THINKING=high 为诊断开关
        if os.environ.get("LLM_THINKING", "off") == "high":
            p["chat_template_kwargs"] = {"reasoning_effort": "high"}
        else:
            ctk = {"enable_thinking": False}
            if config.LLM_REASONING_EFFORT:
                ctk["reasoning_effort"] = config.LLM_REASONING_EFFORT
            p["chat_template_kwargs"] = ctk
    return p


def _extract_content(data: str, model: str):
    model_used = data.get("model")
    if model_used and model_used != "default" and model_used != model:
        log.warning("响应模型 %s 与请求模型 %s 不一致", model_used, model)
    choice = data["choices"][0]
    msg = choice.get("message") or choice.get("delta") or {}
    content = msg.get("content")
    if content is None:
        raise KeyError("content is null")
    return content


def _err_body(e) -> str:
    if isinstance(e, urllib.error.HTTPError):
        try:
            return e.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _classify(e, body: str):
    """返回 'model' / 'auth' / 'param' / 'transient' / 'other'。"""
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403):
            return "auth"
        if e.code in (400, 404, 422):
            low = body.lower()
            if "model" in low and any(k in low for k in ("not", "invalid", "no access",
                                                         "does not exist", "not_exist", "model_not")):
                return "model"
            return "param"
        if e.code == 429 or e.code >= 500:
            return "transient"
        return "other"
    return "transient"  # 超时/连接类


def chat(messages, temperature: float = 0.0, max_tokens: int = None,
         timeout: int = None, retries: int = None) -> str:
    """调用 chat/completions，自动尝试模型名与载荷组合。全部失败抛异常。"""
    timeout = timeout or config.LLM_TIMEOUT
    retries = config.LLM_RETRIES if retries is None else retries
    models = _candidate_models()
    errors = []
    attempts = 0

    for model in models:
        # 载荷形态：优先带 chat_template_kwargs；参数被拒时退化到基础请求
        ctk_order = [True, False]
        if _WORKING["model"] == model:
            ctk_order = [_WORKING["with_ctk"], not _WORKING["with_ctk"]]
        move_next_model = False
        for with_ctk in ctk_order:
            n_retry = retries if attempts == 0 else 1  # 完整重试只给第一组，避免超时风暴
            for attempt in range(1, n_retry + 1):
                attempts += 1
                payload = _combo_payload(model, messages, temperature, max_tokens, with_ctk)
                req = urllib.request.Request(
                    config.chat_url(), data=json.dumps(payload).encode("utf-8"),
                    headers=_auth_headers(), method="POST")
                try:
                    data = _http_json(req, timeout=timeout)
                    content = _extract_content(data, model)
                    if _WORKING["model"] != model or _WORKING["with_ctk"] != with_ctk:
                        _WORKING["model"] = model
                        _WORKING["with_ctk"] = with_ctk
                        log.info("模型通道打通：model=%s with_chat_template_kwargs=%s",
                                 model, with_ctk)
                    return content
                except Exception as e:  # noqa: BLE001
                    body = _err_body(e)
                    kind = _classify(e, body)
                    errors.append(f"{model}/ctk={with_ctk}: {body[:120] or e}")
                    if kind == "model":
                        log.warning("模型 %s 不被识别(%s)，尝试下一个模型名", model, errors[-1])
                        move_next_model = True
                        break
                    if kind == "auth":
                        log.error("认证失败(401/403)：请检查 LLM_API_KEY 注入是否正确")
                        raise RuntimeError(f"认证失败：{e}") from e
                    if kind == "param":
                        log.warning("参数形态被拒，退化载荷重试：%s", errors[-1])
                        break  # 换载荷形态
                    wait = min(2 ** (attempt - 1), 8)
                    log.warning("LLM 调用失败(第 %d/%d 次)：%s，%ds 后重试",
                                attempt, n_retry, errors[-1], wait)
                    if attempt < n_retry:
                        time.sleep(wait)
            if move_next_model:
                break
    raise RuntimeError(f"LLM 调用最终失败，已尝试 {attempts} 次组合：{errors[:6]}")
