#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh 插件：police-legal-agent 的 MCP stdio 服务器。

向 dsh（DeepSeek Harness）等 MCP 客户端暴露三个工具：
- law_qa(question)                    法律知识问答（RAG，答案与引用可溯源）
- risk_assess(text)                   警情反馈单风险研判（人员/等级/法律依据）
- risk_assess_batch(input, output)    批量研判 xlsx（共识投票/原子落盘/失败补跑）

配置全部走环境变量（LLM_API_URL / LLM_API_KEY / EMBEDDING_API_URL / ...），
与统一 HTTP 服务共用同一套引擎模块；知识库索引在首次工具调用时懒构建，
Embedding 端点不可用时自动降级为关键词检索。

dsh 注册示例（~/.dsh/profiles/desktop/cordis.patch.yml 的 insert 列表）：
    - id: mcp-police-legal-agent
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: police-legal-agent
        transport: stdio
        command: <python 解释器路径>
        args: [<本文件路径>]
        env:
          LLM_API_URL: <模型网关地址>
          LLM_API_KEY: <密钥>
        toolCallTimeoutMs: 600000
工具在 dsh 会话中出现为 mcp__police-legal-agent__law_qa 等名字。
"""
import asyncio
import json
import logging
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

# 引擎模块必须在主线程导入完成：FastMCP 的工具调用跑在 anyio worker 线程里，
# 在那种线程里懒加载 openpyxl 等重型依赖会触发 CPython 导入锁死锁（实测复现）。
import api  # noqa: E402
import config as app_config  # noqa: E402
import laws as laws_mod  # noqa: E402
import qa as qa_mod  # noqa: E402
from retriever import Retriever  # noqa: E402
from risk import assess as risk_assess_mod  # noqa: E402
from risk import reader as risk_reader  # noqa: E402

mcp = FastMCP("police-legal-agent")

_STATE = {"retriever": None, "lock": threading.Lock()}
log = logging.getLogger("police-legal-agent.mcp")


def _get_retriever():
    """懒构建共享法律索引（线程安全）。构建失败返回 None，工具自动降级。"""
    if _STATE["retriever"] is not None:
        return _STATE["retriever"]
    with _STATE["lock"]:
        if _STATE["retriever"] is not None:
            return _STATE["retriever"]
        try:
            articles = laws_mod.load_kb(app_config.LAWS_DIR)
            if not articles:
                log.error("知识库为空：%s", app_config.LAWS_DIR)
                return None
            retriever = Retriever(articles)
            retriever.build_index_sync()
            _STATE["retriever"] = retriever
            log.info("法律索引就绪：%d 条", len(articles))
            return retriever
        except Exception as e:  # noqa: BLE001
            log.warning("法律索引构建失败，工具将降级运行：%s: %s", type(e).__name__, e)
            return None


def _ensure_logging():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")


@mcp.tool()
def law_qa(question: str) -> str:
    """公安法律知识问答：输入自然语言问题（如"民警在巡逻中发现有人赌博，如何处理？"），
    返回基于现行法律法规知识库的答案，附可溯源的 law_name/article/snippet 引用。"""
    _ensure_logging()
    retriever = _get_retriever()
    result = api._call_with_wallclock(
        lambda: qa_mod.answer(question, retriever),
        app_config.QA_DEADLINE_SECONDS, default=None)
    if result is None:
        result = qa_mod.fallback_answer(question, retriever)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def risk_assess(text: str) -> str:
    """警情反馈单风险研判：输入警情反馈单全文（含【当事人信息】与【警情内容及处置情况】），
    识别风险人员并评定风险等级（高/低/无），返回结构化结果；法条接地开启时附法律依据。"""
    _ensure_logging()
    retriever = _get_retriever()
    return json.dumps(risk_assess_mod.assess_one(text, retriever), ensure_ascii=False)


@mcp.tool()
def risk_assess_batch(input_xlsx: str, output_xlsx: str) -> str:
    """批量警情研判：读取输入 xlsx（Sheet「警情数据」，列：反馈单编号/出警情况），
    并发研判后写出规范输出 xlsx（Sheet「风险研判结果」7 列）。返回处理摘要。"""
    _ensure_logging()
    retriever = _get_retriever()
    records = risk_reader.read_records(input_xlsx)
    code = risk_assess_mod.run_batch(input_xlsx, output_xlsx, retriever)
    done = 0
    if code == 0 and os.path.exists(output_xlsx):
        done = max(0, len(records))
    return json.dumps({"ok": code == 0, "records": len(records), "written": done,
                       "output": output_xlsx}, ensure_ascii=False)


if __name__ == "__main__":
    _ensure_logging()
    # 调试开关：MCP_DEBUG_DUMP=<秒> 时周期性向 stderr 转储全部线程栈（定位卡点用）
    if os.environ.get("MCP_DEBUG_DUMP"):
        import faulthandler
        faulthandler.dump_traceback_later(
            int(os.environ["MCP_DEBUG_DUMP"]), repeat=True)
    mcp.run(transport="stdio")
