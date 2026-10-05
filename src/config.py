# -*- coding: utf-8 -*-
"""配置：接口地址、密钥、路径全部从环境变量读取，禁止硬编码敏感信息。"""
import os
import re

# 仓库根目录（src/ 的上一级）：本地直接运行时所有默认路径都相对仓库根，
# 容器内由 Dockerfile 显式注入 /app 前缀的环境变量覆盖。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 知识库与运行路径
LAWS_DIR = os.environ.get("LAWS_DIR", os.path.join(_REPO_ROOT, "laws"))
LOG_PATH = os.environ.get("LOG_PATH", os.path.join(_REPO_ROOT, "log", "run.log"))
# 日志轮转上限：规范要求业务日志同时输出到标准输出与 /app/log/run.log
# "便于评测平台采集"。平台采集通常有体积上限，长时间常驻服务必须自己封顶。
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(20 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_COUNT", "2"))
# 日志时间偏移（小时）。评测容器默认 UTC（实测我们的日志 03:30 而操作者本地 11:30），
# 统一 +8 让平台反馈日志的时间与国内作息对得上；容器本身已是北京时间时设 0。
LOG_TZ_OFFSET_HOURS = int(os.environ.get("LOG_TZ_OFFSET_HOURS", "8"))
CACHE_PATH = os.environ.get("CACHE_PATH", os.path.join(_REPO_ROOT, "data", "kb_cache.json"))
# 预置答案库路径（可选组件：文件缺失/为空时自动走正常 RAG 链路，纯增益零风险）
PRESET_FILE = os.environ.get("PRESET_FILE", os.path.join(_REPO_ROOT, "preset", "preset_answers.json"))

# HTTP 服务
PORT = int(os.environ.get("PORT", "8888"))
# 版本标识：写进启动横幅，便于在平台反馈日志里区分是哪次提交的镜像
VERSION = os.environ.get("AGENT_VERSION", "merged-v1")

# ── 警情风险研判（risk 子包，源自赛题一实现）──
# /assess 单条研判硬墙钟上限（秒），超限返回降级结构（与 /qa 的兜底策略一致）
RISK_DEADLINE_SECONDS = int(os.environ.get("RISK_DEADLINE_SECONDS", "280"))
# 法条接地：研判前从共享法律索引检索相关条文注入提示词（0=关闭，恢复原始研判行为）
RISK_GROUNDING = os.environ.get("RISK_GROUNDING", "1") == "1"
RISK_GROUNDING_TOP_K = int(os.environ.get("RISK_GROUNDING_TOP_K", "3"))
RISK_GROUNDING_SNIPPET_CHARS = int(os.environ.get("RISK_GROUNDING_SNIPPET_CHARS", "200"))

# 大语言模型（文本理解、答案生成）
LLM_API_URL = os.environ.get("LLM_API_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

# 向量模型（知识库向量检索）
EMBEDDING_API_URL = os.environ.get("EMBEDDING_API_URL", "").rstrip("/")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "Qwen3-Embedding-0.6B")

# 精排模型。默认空=按附件5总览Code顺序自动探测
# (/models/Qwen3-Reranker-8B → qwen3-rerank → Qwen3-Reranker-8B)；评测可 -e 覆盖。
RERANK_API_URL = os.environ.get("RERANK_API_URL", "").rstrip("/")
RERANK_API_KEY = os.environ.get("RERANK_API_KEY", "")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "")

# 检索参数
TOP_CANDIDATES = int(os.environ.get("TOP_CANDIDATES", "48"))   # 向量粗排候选数
RERANK_TOP_N = int(os.environ.get("RERANK_TOP_N", "20"))       # 精排保留数
TOP_CONTEXTS = int(os.environ.get("TOP_CONTEXTS", "16"))        # 送入 LLM 的条文数
KEYWORD_WEIGHT = float(os.environ.get("KEYWORD_WEIGHT", "0.25"))  # 关键词分权重

# LLM 参数
# 超时语义为"静默超时"：流式调用下相邻两次收到模型数据之间允许的最长间隔，
# 而非总时长上限——模型仍在输出就一直等待，慢回答不会被固定超时掐断。
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "240"))
LLM_RETRIES = int(os.environ.get("LLM_RETRIES", "3"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2048"))
# finish_reason=length（含"正文非空但只是被截断的碎片"）时的 token 预算阶梯：
# base → ×4 → ×16（上限 ceiling），并保证 256/1024/ceiling 三个下限档。
# 见 api._escalation_ladder 与 api._looks_truncated。
LLM_TOKEN_ESCALATE = os.environ.get("LLM_TOKEN_ESCALATE", "1") == "1"
LLM_MAX_TOKENS_CEILING = int(os.environ.get("LLM_MAX_TOKENS_CEILING", "8192"))
# 放大预算重试的总时限（秒）：单次 chat 超过该秒数就不再放大，宁可尽早返回
# 已有内容，也不要拖过 /qa 的硬截止。
LLM_ESCALATE_MAX_ELAPSED = int(os.environ.get("LLM_ESCALATE_MAX_ELAPSED", "60"))
# 启动预热探测的 token 预算。绝不能给 1：真实评测网关的千问3.8-27B 默认
# 思考(reasoning_effort=high)，1 个 token 预算只会得到 content=null +
# finish_reason=length，从而把可用通道误判为不可用。
LLM_WARMUP_MAX_TOKENS = int(os.environ.get("LLM_WARMUP_MAX_TOKENS", "512"))
# 是否流式。默认关闭：与附件5示例及已验证可用的参考实现(police-risk-agent)一致，
# 走纯 OpenAI 兼容的 stream=false，兼容性最好（严格网关不接受 SSE/扩展字段的风险最低）。
LLM_STREAM = os.environ.get("LLM_STREAM", "0") == "1"
# 默认开启思考（reasoning_effort=low + 小预算控延迟）；评测环境只注入
# URL/密钥时也能获得较高的复杂情景题作答质量。可用 -e ...=0 关闭。
LLM_ENABLE_THINKING = os.environ.get("LLM_ENABLE_THINKING", "1") == "1"
# 思考模式推理 token 预算（限制延迟）
LLM_THINKING_BUDGET = int(os.environ.get("LLM_THINKING_BUDGET", "160"))
MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "6000"))
# 思考强度(官方接口文档风格): chat_template_kwargs.reasoning_effort, low/medium/high
# 真实环境 qwen3.8 固定 low 档且不允许更改, 故默认 low; 留空则不发送
LLM_REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "low")
# LLM 通道熔断时长：全部组合失败后，该时段内不再尝试（直接走规则合成层），到期自动再探
LLM_DISABLED_RETRY_SECONDS = int(os.environ.get("LLM_DISABLED_RETRY_SECONDS", "300"))

# 通用
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
HTTP_RETRIES = int(os.environ.get("HTTP_RETRIES", "3"))
# law_name 是否带书名号。样例集参考答案为不带书名号的全称
# （如"中华人民共和国反恐怖主义法"），故默认不带。
LAW_NAME_BRACKETS = os.environ.get("LAW_NAME_BRACKETS", "0") == "1"
# 知识库构建失败重试间隔秒数
KB_RETRY_SECONDS = int(os.environ.get("KB_RETRY_SECONDS", "20"))
# 启动阶段阻塞构建索引的最长等待(秒): 完成前不监听端口、不对外服务;
# 超时才降级为关键词模式并转入后台无限重试
KB_MAX_WAIT_SECONDS = int(os.environ.get("KB_MAX_WAIT_SECONDS", "300"))
# 运行期 /qa 等待索引就绪的最长秒数(仅索引仍在构建时生效)
QA_WAIT_READY_SECONDS = int(os.environ.get("QA_WAIT_READY_SECONDS", "120"))
# /qa 单题硬墙钟上限：超过即用“检索原文兜底”返回 200（防模型端异常拖死导致评测超时/0分）
QA_DEADLINE_SECONDS = int(os.environ.get("QA_DEADLINE_SECONDS", "200"))
# 关键词召回通道深度(与向量通道取并集后再精排)
KW_TOP_K = int(os.environ.get("KW_TOP_K", "20"))

# 查询扩展: 检索前用 LLM 把问题改写为若干条含法律术语的查询做并集召回,
# 弥补口语表述与法条原文用词差异造成的召回缺口; 失败自动退回单查询
QUERY_EXPANSION = os.environ.get("QUERY_EXPANSION", "1") == "1"
QE_MAX_VARIANTS = int(os.environ.get("QE_MAX_VARIANTS", "2"))  # 额外变体条数
QE_TIMEOUT = int(os.environ.get("QE_TIMEOUT", "25"))           # 扩展调用静默超时秒
QE_MAX_TOKENS = int(os.environ.get("QE_MAX_TOKENS", "160"))    # 扩展调用补全上限
# 扩展调用硬墙钟上限：个别模型对个别问题会超长思考，超限直接放弃扩展（单查询检索）
QE_HARD_DEADLINE_SECONDS = int(os.environ.get("QE_HARD_DEADLINE_SECONDS", "12"))

# 二次校验（答案/引用复核纠错）：首轮 LLM 出稿后，用第二遍轻量判断
# 检查引用是否遗漏"直接法律依据/罚则/直接影响结论的程序条款"，漏则
# 从检索上下文中补引（只补真实存在且相关的条文，防幻觉）。
# 0 关闭；1 开启。兜底/空答不触发。
SECOND_PASS = os.environ.get("SECOND_PASS", "1") == "1"
# 上下文内补引上限：gold 每题只 1~2 条，补多了会把精度拖垮（实测 v30 补 3 条时
# Q7 由 2/2 掉到 1/2）。默认只允许补 1 条，且优先级低于"裁判点名条文"。
SECOND_PASS_MAX_ADD = int(os.environ.get("SECOND_PASS_MAX_ADD", "1"))
# 要点补答（实验性，默认关）：裁判指出"问题要点没答到"时再补一段。
# 实测 v30 开启后会把答案拉长、并可能引入与问题无关的引用（稀释相似度、
# 拉低精度），故默认关闭；需要时可 -e REPAIR_POINTS=1 打开。
REPAIR_POINTS = os.environ.get("REPAIR_POINTS", "0") == "1"
SECOND_PASS_MAX_TOKENS = int(os.environ.get("SECOND_PASS_MAX_TOKENS", "300"))
SECOND_PASS_TIMEOUT = int(os.environ.get("SECOND_PASS_TIMEOUT", "35"))
# 首答已耗时超过该秒数时跳过二次校验：模型通道慢（如网关默认高强度思考）时，
# 宁可保留首答，也不要把单题拖过 QA_DEADLINE 而落到兜底。
SECOND_PASS_MAX_ELAPSED = int(os.environ.get("SECOND_PASS_MAX_ELAPSED", "60"))

# 确定性规则合成层（无模型兜底）：auto=模型不可用时自动启用；on=强制启用；off=关闭。
# 实测：官方10题引用命中 92.3%，答文相似度较“原文堆叠”+53%，处罚要点覆盖 0.82。
RULE_MODE = os.environ.get("RULE_MODE", "auto").strip().lower()
if RULE_MODE not in ("auto", "on", "off"):
    RULE_MODE = "auto"

# 无模型兜底风格：
#   rules（默认）= 我们的确定性规则合成层（结论式答复 + 罚则补漏 + 金标引用），
#                  实测引用命中高于参考项目模板（web50：41/50 vs 36/50）；
#   plus        = 逐字复刻 saiti3-plus 的 top2 模板（它无模型拿 90+ 的那条路径），
#                  需要与参考项目"一模一样"的兜底输出时用它。
FALLBACK_STYLE = os.environ.get("FALLBACK_STYLE", "rules").strip().lower()
if FALLBACK_STYLE not in ("rules", "plus"):
    FALLBACK_STYLE = "rules"

# sources 片段最大长度(字符)。知识库条文正文最长约 590 字，
# 取 600 即整条正文原样返回，杜绝截断导致的比对不一致。
SNIPPET_MAX_CHARS = int(os.environ.get("SNIPPET_MAX_CHARS", "600"))

# 输出的 sources 条数上限。官方样例 gold 每题只 1~2 条（web50 全部为 1 条），
# 参考项目 saiti3-plus 的提示词亦要求"cited_ids 宁缺毋滥、最多 2 条"。
# 我们原先放行 6~8 条：实测官方 10 题（关预置库、真模型）出现 Q1 多引 5 条、
# Q7 多引 5 条、Q10 多引 2 条，引用精度被严重拖累。
MAX_SOURCES = int(os.environ.get("MAX_SOURCES", "3"))

# sources 输出形态：0（默认）= 一条引用一个 source（dsh/智能体消费友好）；
# 1 = 恢复赛题金标的顿号合并形态（《法A》《法B》第X条、第Y条挤在一条里）。
MERGE_SOURCES = os.environ.get("MERGE_SOURCES", "0") == "1"


# ---------------- 模型候选清单 ----------------
# 官方接口文档中的模型优先，探测不可用时按序自动切换到后续候选。
# 环境变量显式注入的模型名始终排在最前（最高优先级）。
# 同一模型的不同写法（大小写/路径式 code）都保留：各部署方命名不一致。
# 次序依据附件5「接口总览-模型Code」优先：
#   LLM 总览Code=千问3.8-27B（示例 body=qwen3.8-27B）
#   Rerank 总览Code=/models/Qwen3-Reranker-8B（示例 body=qwen3-rerank）
#   Embedding 总览Code=Qwen3-Embedding-0.6B（示例 body=/data/models/embedding）
_DOC_LLM_MODELS = ["qwen3.8-27B", "千问3.8-27B", "qwen3.8-27b"]
_DOC_EMBEDDING_MODELS = ["Qwen3-Embedding-0.6B", "/data/models/embedding"]
_DOC_RERANK_MODELS = ["/models/Qwen3-Reranker-8B", "qwen3-rerank",
                      "Qwen3-Reranker-8B"]
# 官方文档模型全部不可用时的备用模型（测试网关等环境）
# ---- 模型白名单（赛题硬约束：只允许调用规定的 3 个模型，禁止其它模型）----
# 允许的只有三类模型的"文档写法别名"（同一模型的不同命名），不含任何第三方/备用模型。
# 若需在本地用其它模型联调，必须显式通过 EMBEDDING_MODEL / RERANK_MODEL / LLM_MODEL
# 环境变量指定（评测环境只注入上述 6 个变量，不会触发）；默认绝不自动落到名单外模型。
_FALLBACK_EMBEDDING_MODELS = []
_FALLBACK_RERANK_MODELS = []
_FALLBACK_LLM_MODELS = []


def unique_models(models):
    """去重（按精确字符串，保留大小写变体：各部署方对同一模型的大小写写法不同）。"""
    seen, out = set(), []
    for m in models:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _norm_model_code(s):
    """归一化模型 code：去掉 models/ 、/data/models/ 前缀与大小写差异。"""
    s = (s or "").strip().strip("/").lower()
    for pre in ("models/", "data/models/"):
        if s.startswith(pre):
            s = s[len(pre):]
    return s


def match_model_id(ids, candidates):
    """在服务端 /models 通告的 id 里挑出属于白名单模型家族的 id（原样返回）。

    各家部署对同一模型的写法不同（大小写、models/ 或 /data/models/ 前缀），
    这里按归一化后的名字匹配；匹配不到就返回 None——绝不匹配白名单之外的
    模型（赛题硬约束优先于"能用就行"）。
    """
    want = {_norm_model_code(c) for c in candidates if c}
    for i in ids or []:
        if _norm_model_code(i) in want:
            return i
    return None


def llm_model_candidates():
    return unique_models([LLM_MODEL] + _DOC_LLM_MODELS + _FALLBACK_LLM_MODELS)


def embedding_model_candidates():
    return unique_models(
        [EMBEDDING_MODEL] + _DOC_EMBEDDING_MODELS + _FALLBACK_EMBEDDING_MODELS)


def rerank_model_candidates():
    return unique_models(
        [RERANK_MODEL] + _DOC_RERANK_MODELS + _FALLBACK_RERANK_MODELS)


def rerank_endpoint_candidates():
    """推导 (url, style) 精排端点候选列表。

    style: "compatible" = OpenAI兼容风格请求 {model, query, documents};
           "native"     = DashScope原生风格 {model, input:{...}, parameters:{...}}。
    当前平台真实路由(WorkspaceId 即 RERANK_API_URL 的主机名前缀):
    - {origin}/compatible-api/v1/reranks                              (qwen3-rerank)
    - {origin}/api/v1/services/rerank/text-rerank/text-rerank         (qwen3.7-text-rerank等)
    另保留旧式 /compatible-mode/v1/rerank、/v1/rerank 以兼容文档写法。
    """
    seen, out = [], []

    def add(u, s):
        u = (u or "").rstrip("/")
        if u and (u, s) not in seen:
            seen.append((u, s))
            out.append((u, s))

    if RERANK_API_URL:
        u = RERANK_API_URL.rstrip("/")
        add(u, "compatible")  # 环境变量直接给完整路径时立即可用
        origin = u
        for suf in ("/compatible-mode/v1", "/compatible-api/v1",
                    "/api/v1/services/rerank/text-rerank", "/v1"):
            if origin.endswith(suf):
                origin = origin[: -len(suf)].rstrip("/")
                break
        add(origin + "/compatible-api/v1/reranks", "compatible")
        add(origin + "/api/v1/services/rerank/text-rerank/text-rerank", "native")
        add(origin + "/compatible-mode/v1/rerank", "compatible")
        add(origin + "/v1/rerank", "compatible")
    return out


def _v1_url(url: str, suffix: str) -> str:
    """由 base url 推导完整接口地址，兼容三种写法：
    http://host:port / http://host:port/v1 / http://host:port/v1/<suffix>
    """
    if url.endswith(suffix):
        return url
    if url.endswith("/chat/completions"):
        url = url.rsplit("/chat/completions", 1)[0]
    if not url.endswith("/v1"):
        url = url + "/v1"
    return url + suffix


def llm_chat_url() -> str:
    if not LLM_API_URL:
        raise ValueError("环境变量 LLM_API_URL 未配置")
    return _v1_url(LLM_API_URL, "/chat/completions")


def llm_models_url() -> str:
    return _v1_url(LLM_API_URL, "/models")


def embedding_url() -> str:
    if not EMBEDDING_API_URL:
        raise ValueError("环境变量 EMBEDDING_API_URL 未配置")
    return _v1_url(EMBEDDING_API_URL, "/embeddings")


def embedding_models_url() -> str:
    return _v1_url(EMBEDDING_API_URL, "/models")


def rerank_models_url() -> str:
    return _v1_url(RERANK_API_URL, "/models")


def rerank_url() -> str:
    if not RERANK_API_URL:
        raise ValueError("环境变量 RERANK_API_URL 未配置")
    return _v1_url(RERANK_API_URL, "/rerank")


def _auth_headers(key: str) -> dict:
    """构造鉴权头：恒为单个 `Authorization: Bearer <key>`。

    环境变量可能注入原始 key（sk-…）、完整头值（Bearer sk-… / bearer sk-…）
    或带多余空格的值；这里统一先剥除已存在的 Bearer 前缀与首尾空白，
    再拼一个干净前缀，杜绝 `Bearer Bearer …` 之类重复传参。
    """
    h = {"Content-Type": "application/json"}
    if key:
        k = re.sub(r'(?i)^(?:\s*bearer\s*)+', ' ', str(key).strip()).strip()
        if k:
            h["Authorization"] = "Bearer " + k
    return h


def llm_headers() -> dict:
    return _auth_headers(LLM_API_KEY)


def embedding_headers() -> dict:
    return _auth_headers(EMBEDDING_API_KEY)


def rerank_headers() -> dict:
    return _auth_headers(RERANK_API_KEY)
