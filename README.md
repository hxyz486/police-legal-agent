# police-legal-agent 公安法律智能体

一个面向公安业务场景的统一智能体，把两个经过完整评测的能力合并进同一套引擎、共享同一个法律知识库：

| 能力 | 来源 | 形态 |
|---|---|---|
| **法律知识问答** | 赛题三实现（公安法律知识智能问答） | RAG：混合检索 → 精排 → LLM 生成 → 引用全库校验，答案与引用可溯源、防幻觉 |
| **警情风险研判** | 赛题一实现（警情反馈单风险人员识别研判） | LLM 研判 + 双投票共识 + 失败补跑 + 原子落盘 + 输出自校验 |
| **法条接地（整合点）** | 本次合并新增 | 研判前从共享法律索引检索相关条文注入提示词，研判结论可附带可溯源的法律依据 |

法律知识库内置 **38 部现行法律法规**（含 2025 修订《治安管理处罚法》、2020《刑法》及修正案十二等），解析为"条"级索引（数千条）。

## 三种使用方式

### 1. 统一 HTTP 服务

```bash
pip install -r requirements.txt   # 仅 openpyxl
export LLM_API_URL=http://<模型网关>/v1
export LLM_API_KEY=<密钥>
python src/service.py             # 默认监听 8888
```

```text
GET  /health   {"status":"ok","kb_ready":true,...}
POST /qa       {"question":"民警在巡逻中发现有人赌博，如何处理？"}
               -> {"answer":"根据《中华人民共和国治安管理处罚法》第七十条…",
                   "sources":[{"law_name":"中华人民共和国治安管理处罚法","article":"第七十条","snippet":"…"}]}
POST /assess   {"text":"【当事人信息】…【警情内容及处置情况】…"}
               -> {"exists":true,"level":"高",
                   "risk_persons":[{"name":"赵XX","id_number":"…","level":"高","reason":"…"}],
                   "law_references":[{"law_name":"…","article":"第…条","snippet":"…"}],
                   "degraded":false,"reason":"…"}
```

任何异常都返回 200 + 合法 JSON（服务永不 5xx，研判失败自动降级）。

### 2. dsh 插件（一键安装，MCP 接入）

本仓库声明了 `dsh.bundle.patch`，是标准的 **dsh（DeepSeek Harness）组合包插件**。dsh 桌面版：

**插件 → 安装插件 → 填入 `https://github.com/hxyz486/police-legal-agent` → 安装**，重启后 dsh 会话中出现三个 MCP 工具：

- `law_qa(question)` — 法律问答
- `risk_assess(text)` — 单条警情研判
- `risk_assess_batch(input_xlsx, output_xlsx)` — 批量研判

同一个组合包还会注册一个 **Agent 预设「警务助手」**（`dsh.bundle.patch` 的第二个文件 `dsh-preset-jingwu/cordis.patch.yml`）：新建任务时在 **设置 → Agent 预设** 里选中它，Agent 就按执法辅助模式工作——法条问题一律先调 `law_qa`、警情研判先调 `risk_assess`、xlsx 批量走 `risk_assess_batch`，回答保持带 `sources` 引用的一句话金标形态。

模型通道**不用单独配置**：MCP 服务启动时自己读 dsh 的配置——密钥取 `$DSH_HOME/.credentials.yaml` 的 refs，接口地址与模型名取 profile 里声明的 provider（`src/dsh_llm.py`）。只有显式设置 `LLM_API_URL` / `LLM_API_KEY` / `LLM_MODEL` 环境变量时才覆盖（容器、命令行或非 dsh 客户端用）。

在 dsh 里取不到可用通道时插件仍会启动，工具自动降级（问答走确定性规则合成层，研判返回降级结构）。其他 MCP 客户端（Claude Desktop、Cline 等）可显式设置上述环境变量后 stdio 接入 `mcp_server.py`。

### 3. 批量研判 CLI

```bash
python src/assess_batch.py input.xlsx output.xlsx
```

输入 xlsx：Sheet「警情数据」，列「反馈单编号」「出警情况」；输出 xlsx：Sheet「风险研判结果」7 列，格式逐字合规（含掩码身份证原样保留、人员去重、等级取最高）。批量模式保留赛题一全套可靠性设计：4 线程并发、失败重试（指数退避）、**双投票共识**（不一致时第 3 次仲裁）、**分批原子落盘**（每 20 条，中途崩溃不丢）、失败补跑、写后自校验。

容器批量形态：

```bash
docker run --rm \
  -e LLM_API_URL=http://<网关>/v1 -e LLM_API_KEY=<密钥> \
  -v $PWD/input:/app/input -v $PWD/output:/app/output -v $PWD/log:/app/log \
  police-legal-agent python src/assess_batch.py
```

## Docker

```bash
bash docker_build.sh [镜像名] [tag]   # 构建 + docker save 导出 + 体积校验(≤1GB)
docker run -d -p 8888:8888 \
  -e LLM_API_URL=http://<网关>/v1 -e LLM_API_KEY=<密钥> \
  police-legal-agent                  # 常驻服务模式
```

## 环境变量

模型三件套（URL/KEY/MODEL）全部环境变量注入，无任何硬编码；未注入 KEY 时服务仍可启动（问答走确定性规则合成层兜底，研判返回降级结构）。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_API_URL` / `LLM_API_KEY` / `LLM_MODEL` | - | 文本模型（OpenAI 兼容，模型名可自动发现） |
| `EMBEDDING_API_URL` / `EMBEDDING_API_KEY` | - | 向量模型（不可用时自动降级关键词检索） |
| `RERANK_API_URL` / `RERANK_API_KEY` | - | 精排模型（不可用时降级粗排） |
| `LAWS_DIR` | `<仓库>/laws` | 法律 TXT 目录 |
| `PORT` | 8888 | HTTP 服务端口 |
| `RISK_GROUNDING` | 1 | 研判法条接地开关（0=恢复原始研判行为） |
| `RISK_GROUNDING_TOP_K` | 3 | 接地条文数 |
| `CONSENSUS` / `VERIFY_NO_RISK` / `RETRY_FAILED` / `FLUSH_EVERY` | 1/1/1/20 | 批量研判可靠性开关 |
| `MAX_WORKERS` | 4 | 批量并发线程数 |
| `LLM_TIMEOUT` / `LLM_RETRIES` / `LLM_MAX_TOKENS` | 240/3/2048 | LLM 调用参数 |
| `RULE_MODE` | auto | 无模型兜底层：auto/on/off |
| `PRESET_FILE` | `<仓库>/preset/preset_answers.json` | 可选预置答案库（文件缺失自动走正常 RAG） |

## 目录结构

```text
police-legal-agent/
├── mcp_server.py          # MCP stdio 插件服务器（dsh/Claude 等客户端接入）
├── src/
│   ├── service.py         # 统一 HTTP 服务（/qa + /assess + /health）
│   ├── assess_batch.py    # 批量研判 CLI
│   ├── api.py             # LLM/Embedding/Rerank 客户端（模型自动发现/降级）
│   ├── qa.py              # 问答编排：prompt 构造、引用解析与全库校验
│   ├── retriever.py       # 混合检索：向量∪关键词并集召回 + 精排
│   ├── laws.py cnnum.py lawmap.py rules.py sources.py preset.py
│   ├── config.py          # 统一配置（全部环境变量注入）
│   └── risk/              # 警情风险研判子包
│       ├── assess.py      # assess_one（单条）+ run_batch（批量）+ 法条接地
│       ├── llm.py         # OpenAI 兼容客户端（模型名/载荷自动降级）
│       ├── prompt.py      # 研判提示词（官方标准固化 + few-shot + 法条接地注入）
│       ├── parse.py overrides.py reader.py writer.py validate.py
│       └── config.py
├── laws/                  # 38 部现行法律法规 TXT（"条"级知识库源）
└── tests/
    ├── smoke_test.py      # 端到端冒烟：mock 模型 -> 统一服务三接口 + 批量链路
    ├── mcp_smoke_test.py  # MCP 插件冒烟：握手 -> tools/list -> 工具实调
    └── mock_model_server.py
```

## 测试

```bash
python tests/smoke_test.py       # HTTP 服务 + 批量链路（mock 模型，无需真实网关）
python tests/mcp_smoke_test.py   # MCP 插件链路
```

## 许可证

MIT
