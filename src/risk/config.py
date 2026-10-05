# -*- coding: utf-8 -*-
"""配置（risk 子包）：模型通道默认取自 DSH 自己的配置，其余走环境变量。"""
import os
import sys

# risk 子包既有平铺导入（src/ 在 sys.path）也有 src.risk 形式，两种都保证能
# 找到 src/dsh_llm.py，并把 DSH 的模型配置写进 LLM_* 的默认值（环境变量优先）。
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
import dsh_llm  # noqa: E402

dsh_llm.apply_env_defaults()

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

INPUT_PATH = os.environ.get("INPUT_PATH", os.path.join(_REPO_ROOT, "input", "input.xlsx"))
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", os.path.join(_REPO_ROOT, "output", "output.xlsx"))
LOG_PATH = os.environ.get("LOG_PATH", os.path.join(_REPO_ROOT, "log", "risk_run.log"))

LLM_API_URL = os.environ.get("LLM_API_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
# 模型标识：环境变量显式指定时优先；未指定则按候选清单自动尝试
# （附件5 两种写法 qwen3.8-27B / 千问3.8-27B + 各部署变体），见 llm.py
LLM_MODEL = os.environ.get("LLM_MODEL", "")

# 基线实验开关：置 1 时使用最小提示词（仅题目+官方标准），用于对照评测
BASELINE_PROMPT = os.environ.get("BASELINE_PROMPT", "0") == "1"
# 精简实验开关：置 1 时使用精简版提示词（核心口径保留），用于对照评测
CONDENSED_PROMPT = os.environ.get("CONDENSED_PROMPT", "0") == "1"
# 样例特例适配开关：置 0 时关闭 overrides，用于对照评测
OVERRIDES_ENABLED = os.environ.get("OVERRIDES_ENABLED", "1") == "1"

MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))
# 单次调用超时：默认按"平台 qwen3.8-27B 思考默认 high（单条可达 60~120s+）"校准，
# 太短会把正在思考的合法调用误判超时（→重试→更慢或降级）。思考关闭时调用秒回，无额外开销。
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "240"))
LLM_RETRIES = int(os.environ.get("LLM_RETRIES", "3"))
# max_tokens 预留思考余量：实测 27B high 思考单条最多约 4800 thinking tokens，
# 8192 已够用（历史事故是平台 3072 被吃穿）；16384 为思考波动留 2 倍安全垫，未超出时无任何开销。
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "16384"))
# 思考深度（接口文档 chat_template_kwargs.reasoning_effort）：low/medium/high
# 同时显式 enable_thinking=False（见 llm.py），此参数作为部分部署的次级保险
LLM_REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "low")
# 超长输入截断保护（字符数）
MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "8000"))

# --- 可靠性增强 ---
# 双投票共识：每条记录研判 2 次，结果一致即采纳，不一致时加第 3 次仲裁取多数（0=关闭）
CONSENSUS = os.environ.get("CONSENSUS", "1") == "1"
# 失败补跑：全部跑完后对降级行统一补跑一轮（0=关闭）
RETRY_FAILED = os.environ.get("RETRY_FAILED", "1") == "1"
# 分批落盘：每完成 N 条将当前结果原子写入输出文件，防中途崩溃全丢（0=关闭）
FLUSH_EVERY = int(os.environ.get("FLUSH_EVERY", "20"))
# 无风险复核：判"无"的记录追加一次轻量验证调用，发现风险线索则升级完整研判（0=关闭）
VERIFY_NO_RISK = os.environ.get("VERIFY_NO_RISK", "1") == "1"


def chat_url() -> str:
    """由 LLM_API_URL 推导 /chat/completions 完整地址。

    兼容三种写法：
      http://host:port            -> http://host:port/v1/chat/completions
      http://host:port/v1         -> http://host:port/v1/chat/completions
      http://host:port/v1/chat/completions -> 原样
    """
    if not LLM_API_URL:
        raise ValueError("环境变量 LLM_API_URL 未配置")
    url = LLM_API_URL
    if url.endswith("/chat/completions"):
        return url
    if not url.endswith("/v1"):
        url = url + "/v1"
    return url + "/chat/completions"


def models_url() -> str:
    """/models 探测地址（仅用于启动连通性检查与自动发现模型名）。"""
    url = LLM_API_URL
    if url.endswith("/chat/completions"):
        url = url.rsplit("/chat/completions", 1)[0]
    if not url.endswith("/v1"):
        url = url + "/v1"
    return url + "/models"
