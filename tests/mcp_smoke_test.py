# -*- coding: utf-8 -*-
"""MCP stdio 插件冒烟测试：握手 -> tools/list -> law_qa / risk_assess 实调。

运行：python tests/mcp_smoke_test.py（内部自动起 mock 模型服务）
"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import mock_model_server  # noqa: E402


def send(proc, obj):
    proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
    proc.stdin.flush()


def recv(proc, want_id=None, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.1)
            continue
        try:
            msg = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if want_id is None or msg.get("id") == want_id:
            return msg
    raise RuntimeError("MCP 响应超时")


def main():
    import http.server
    from socketserver import ThreadingMixIn

    class Srv(ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    mock = Srv(("127.0.0.1", mock_model_server.PORT), mock_model_server.Mock)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    mu = f"http://127.0.0.1:{mock_model_server.PORT}"
    env = dict(os.environ)
    env.update({
        "LLM_API_URL": mu, "EMBEDDING_API_URL": mu, "RERANK_API_URL": mu,
        "LLM_MODEL": "mock-llm",
        "LAWS_DIR": os.path.join(ROOT, "laws"),
        "LOG_PATH": os.path.join(ROOT, "log", "mcp_test.log"),
        "CACHE_PATH": os.path.join(ROOT, "data", "mcp_cache.json"),
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "mcp_server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=open(os.path.join(ROOT, "log", "mcp_srv_err.log"), "w", encoding="utf-8"), env=env, cwd=ROOT)
    try:
        send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "smoke", "version": "0"}}})
        init = recv(proc, 1)
        name = init["result"]["serverInfo"]["name"]
        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        print(f"[OK] initialize: server={name}")

        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = recv(proc, 2)["result"]["tools"]
        names = sorted(t["name"] for t in tools)
        assert names == ["law_qa", "risk_assess", "risk_assess_batch"], names
        print(f"[OK] tools/list: {names}")

        send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "law_qa",
                               "arguments": {"question": "民警在巡逻中发现有人赌博，应如何处理？"}}})
        r3 = recv(proc, 3)
        qa_text = r3["result"]["content"][0]["text"]
        qa = json.loads(qa_text)
        assert qa.get("answer") and isinstance(qa.get("sources"), list), qa
        print(f"[OK] law_qa: sources={len(qa['sources'])}")

        text = ("【当事人信息】1.报警人：王XX；2.当事人：赵XX。"
                "【警情内容及处置情况】赵XX因债务纠纷扬言\"再不还钱就砍死你全家\"，"
                "并持菜刀到王XX店门口叫嚣，民警到场将其控制。")
        send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "risk_assess", "arguments": {"text": text}}})
        r4 = recv(proc, 4)
        ar = json.loads(r4["result"]["content"][0]["text"])
        assert ar["exists"] is True and ar["level"] == "高", ar
        assert ar["risk_persons"][0]["name"] == "赵XX", ar
        assert isinstance(ar["law_references"], list), ar
        print(f"[OK] risk_assess: level={ar['level']} refs={len(ar['law_references'])}")
    finally:
        proc.kill()
        mock.shutdown()

    print("\nMCP SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
