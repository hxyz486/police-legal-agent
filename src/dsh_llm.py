# -*- coding: utf-8 -*-
"""从 DSH（DeepSeek Harness）自己的配置里解析模型通道，无需单独配置 URL/密钥。

DSH 侧已经把模型通道配好了（profile 的 cordis.patch.yml / cordis.yml 里声明
provider，密钥存在 $DSH_HOME/.credentials.yaml 的 refs 里）。本模块把这些信息
解析出来并写入环境变量默认值，于是：

    LLM_API_URL / LLM_API_KEY / LLM_MODEL

在**未显式设置**时自动等于 DSH 正在使用的那套配置；显式设置的环境变量依旧
最高优先（容器/命令行/比赛评测环境仍可完全覆盖）。

解析顺序（只认 DSH 里 Python 侧真正能直连的 API-key 通道）：
1. profile 里 ``@deepseek-ai/dsh-llm-deepseek-api-key`` 声明的官方通道
   （baseURL 固定 https://api.deepseek.com，密钥 ref 默认 DEEPSEEK_API_KEY）；
2. profile 里 ``@deepseek-ai/dsh-llm-pi-ai`` 声明的自定义网关（baseURL/apiKeyEnv/models）；
3. 都取不到时保持为空——由调用方决定降级行为。

``deepseek-account``（账号令牌，走 x-dsh-auth-token）不在候选内：那是 DSH 进程内
的账号通道，子进程无法复用。

模型名优先取 ``agent-default-model`` 指定的那个（若它落在选中通道的 models 里），
否则取通道 models 列表的第一个。
"""
from __future__ import annotations

import os
import re

_OFFICIAL_BASE_URL = "https://api.deepseek.com"
_DEFAULT_KEY_REF = "DEEPSEEK_API_KEY"
_FALLBACK_KEY_REFS = ("DEEPSEEK_API_KEY", "DS_API_KEY", "NEWAPI_CPPU_ISA_KEY")

# 注意：行内一律用 [ \t] 而不是 \s —— \s 会跨行吞掉换行，把下一行当成上一行的值。
_ROW_ID = re.compile(r"^(?P<indent>[ \t]*)-[ \t]*id:[ \t]*(?P<id>[\w.-]+)[ \t]*$", re.M)
# loader 条目里 name 与 id 同级（``- id: x`` 后跟 ``  name: '...'``），取块内第一个。
_ROW_NAME = re.compile(r"^[ \t]+name:[ \t]*['\"]?(?P<name>[^'\"\s]+)['\"]?[ \t]*$", re.M)
_KV = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][\w.-]*):[ \t]*(?P<value>.*?)[ \t]*$", re.M)
_LIST_ID = re.compile(r"^[ \t]*-[ \t]*id:[ \t]*(?P<id>.+?)[ \t]*$", re.M)


def dsh_home() -> str:
    """$DSH_HOME，或 %USERPROFILE%/.dsh。"""
    home = os.environ.get("DSH_HOME")
    if home:
        return home
    return os.path.join(os.path.expanduser("~"), ".dsh")


def profile_dir() -> str:
    """DSH 启动时注入的 $DSH_PROFILE_DIR，否则按 profile 名拼。"""
    explicit = os.environ.get("DSH_PROFILE_DIR")
    if explicit:
        return explicit
    name = os.environ.get("DSH_PROFILE") or "desktop"
    return os.path.join(dsh_home(), "profiles", name)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _unquote(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    # 行尾 YAML 注释（密钥里不会有 ' #'，安全）
    return re.sub(r"\s+#.*$", "", value).strip()


def credential_refs(home: str | None = None) -> dict:
    """读取 .credentials.yaml 的 refs 区（环境变量名 -> 值）。"""
    text = _read(os.path.join(home or dsh_home(), ".credentials.yaml"))
    refs: dict[str, str] = {}
    in_refs = False
    for line in text.splitlines():
        if re.match(r"^refs:\s*$", line):
            in_refs = True
            continue
        if not in_refs:
            continue
        if line.strip() and not line.startswith((" ", "\t")):
            break
        m = _KV.match(line)
        if m and m.group("value"):
            refs[m.group("key")] = _unquote(m.group("value"))
    return refs


def _blocks(text: str) -> list[dict]:
    """把顶层 ``- id:`` 行切成块（含其后缩进更深的行）。"""
    lines = text.splitlines()
    starts: list[tuple[int, str, int]] = []
    for idx, line in enumerate(lines):
        m = _ROW_ID.match(line)
        if m:
            starts.append((idx, m.group("id"), len(m.group("indent"))))
    out: list[dict] = []
    for pos, (idx, row_id, indent) in enumerate(starts):
        end = len(lines)
        for nxt_idx, _, nxt_indent in starts[pos + 1:]:
            if nxt_indent <= indent:
                end = nxt_idx
                break
        body = "\n".join(lines[idx:end])
        name_m = _ROW_NAME.search(body)
        out.append({
            "id": row_id,
            "name": name_m.group("name") if name_m else None,
            "body": body,
        })
    return out


def _models_in(body: str) -> list[str]:
    """取 models: 段里的 ``- id:`` 列表（只扫 models: 之后，避免把行 id 当模型名）。"""
    section = body
    m = re.search(r"^[ \t]*models:[ \t]*$", body, re.M)
    if m:
        section = body[m.end():]
    return [x.group("id").strip("'\"") for x in re.finditer(_LIST_ID.pattern, section, re.M)]


def _kv(body: str, key: str, indent: int | None = None) -> str | None:
    """取块内某键的值（可限定缩进层数，避免误取子块同名键）。"""
    for m in re.finditer(_KV.pattern, body, re.M):
        if m.group("key") != key:
            continue
        if indent is not None and len(m.group("indent")) != indent:
            continue
        value = _unquote(m.group("value"))
        if value:
            return value
    return None


def _pi_ai_routes(body: str) -> list[dict]:
    """解析 llm-pi-ai 的 providers 映射，返回 [{provider,url,key_ref,models}]。"""
    lines = body.splitlines()
    start = None
    for idx, line in enumerate(lines):
        m = _KV.match(line)
        if m and m.group("key") == "providers" and not m.group("value"):
            start = idx
            base_indent = len(m.group("indent"))
            break
    if start is None:
        return []
    provider_indent = None
    providers: list[tuple[str, list[str]]] = []
    for line in lines[start + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= base_indent:
            break
        m = re.match(r"^(?P<indent>\s+)(?P<key>[\w.-]+):\s*$", line)
        if m and (provider_indent is None or len(m.group("indent")) <= provider_indent):
            provider_indent = len(m.group("indent"))
            providers.append((m.group("key"), []))
            continue
        if providers:
            providers[-1][1].append(line)
    routes = []
    for name, body_lines in providers:
        sub = "\n".join(body_lines)
        url = _kv(sub, "baseURL")
        if not url:
            continue
        routes.append({
            "provider": name,
            "url": url.rstrip("/"),
            "key_ref": _kv(sub, "apiKeyEnv") or _DEFAULT_KEY_REF,
            "models": _models_in(sub),
        })
    return routes


def default_model_choice(config_text: str) -> tuple[str | None, str | None]:
    """profile 里 agent-default-model 的 (provider, model)。"""
    for block in _blocks(config_text):
        if block["id"] == "agent-default-model":
            return _kv(block["body"], "provider"), _kv(block["body"], "model")
    return None, None


def _pick_key(refs: dict, ref: str | None) -> tuple[str, str]:
    """返回 (密钥, 命中来源)。环境变量 > .credentials.yaml refs > 同类兜底 ref。"""
    for candidate in [ref] + [r for r in _FALLBACK_KEY_REFS if r != ref]:
        if not candidate:
            continue
        value = os.environ.get(candidate)
        if value:
            return value, f"env:{candidate}"
        if refs.get(candidate):
            return refs[candidate], f"credentials:{candidate}"
    return "", ""


def resolve(config_text: str, refs: dict) -> dict:
    """从 DSH 配置文本 + 凭据表里挑一条 Python 侧可用的模型通道。"""
    provider_pref, model_pref = default_model_choice(config_text)
    routes: list[dict] = []
    for block in _blocks(config_text):
        name = (block["name"] or "").split("?")[0]
        if name == "@deepseek-ai/dsh-llm-deepseek-api-key":
            routes.append({
                "provider": "deepseek-official",
                "url": _OFFICIAL_BASE_URL,
                "key_ref": _kv(block["body"], "apiKeyEnv") or _DEFAULT_KEY_REF,
                "models": _models_in(block["body"]),
                "origin": "profile:llm-deepseek",
            })
        elif name == "@deepseek-ai/dsh-llm-pi-ai":
            for route in _pi_ai_routes(block["body"]):
                route["origin"] = "profile:llm-pi-ai"
                routes.append(route)

    def usable(route: dict) -> bool:
        key, _ = _pick_key(refs, route["key_ref"])
        return bool(route["url"] and key)

    chosen = None
    if provider_pref:
        chosen = next((r for r in routes if r["provider"] == provider_pref and usable(r)), None)
    if chosen is None:
        chosen = next((r for r in routes if r["provider"] == "deepseek-official" and usable(r)), None)
    if chosen is None:
        chosen = next((r for r in routes if usable(r)), None)
    if chosen is None and routes:
        chosen = routes[0]
    if chosen is None:
        return {}

    key, key_source = _pick_key(refs, chosen["key_ref"])
    models = chosen.get("models") or []
    model = model_pref if model_pref in models else (models[0] if models else (model_pref or ""))
    return {
        "url": chosen["url"],
        "key": key,
        "model": model,
        "provider": chosen["provider"],
        "origin": chosen.get("origin", "?"),
        "key_source": key_source,
        "configured_model": model_pref or "",
    }


def _config_text(home: str, profile: str) -> str:
    """profile 的用户 patch 优先，其后拼接组合后的 cordis.yml 作为补充。"""
    return "\n".join([
        _read(os.path.join(profile, "cordis.patch.yml")),
        _read(os.path.join(profile, "cordis.yml")),
    ])


def resolve_for_dsh(home: str | None = None, profile: str | None = None) -> dict:
    home = home or dsh_home()
    profile = profile or profile_dir()
    return resolve(_config_text(home, profile), credential_refs(home))


def apply_env_defaults(home: str | None = None, profile: str | None = None) -> dict:
    """把解析结果写入 LLM_API_URL / LLM_API_KEY / LLM_MODEL 的默认值。

    仅 setdefault：显式配置的环境变量保持最高优先级。返回解析摘要（不含密钥）。
    """
    found = resolve_for_dsh(home, profile)
    if found.get("url"):
        os.environ.setdefault("LLM_API_URL", found["url"])
    if found.get("key"):
        os.environ.setdefault("LLM_API_KEY", found["key"])
    if found.get("model"):
        os.environ.setdefault("LLM_MODEL", found["model"])
    return {
        "source": found.get("origin"),
        "provider": found.get("provider"),
        "url": found.get("url"),
        "model": found.get("model"),
        "key": found.get("key_source") or None,
    }
