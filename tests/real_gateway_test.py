# -*- coding: utf-8 -*-
"""真实网关联调：MCP 服务器 + 真实 LLM + 关键词降级索引。

用法（先注入模型网关环境变量，缺省时自动跳过）：
    LLM_API_URL=http://<网关>/v1 LLM_API_KEY=<密钥> [LLM_MODEL=<模型名>] \
        python tests/real_gateway_test.py
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def send(proc, obj):
    proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
    proc.stdin.flush()

def recv(proc, want_id, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.2)
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("id") == want_id:
            return msg
    raise RuntimeError("timeout waiting id=%s" % want_id)

env = dict(os.environ)
if not env.get("LLM_API_URL") or not env.get("LLM_API_KEY"):
    print("SKIP: 未设置 LLM_API_URL / LLM_API_KEY，跳过真实网关联调")
    sys.exit(0)
env.setdefault("LLM_MODEL", "")
env.update({
    "KB_MAX_WAIT_SECONDS": "5",
    "QA_WAIT_READY_SECONDS": "5",
    "PYTHONIOENCODING": "utf-8",
})
proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "mcp_server.py")],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, env=env, cwd=ROOT)
try:
    send(proc, {"jsonrpc":"2.0","id":1,"method":"initialize",
                "params":{"protocolVersion":"2024-11-05","capabilities":{},
                          "clientInfo":{"name":"t","version":"0"}}})
    recv(proc, 1, 60)
    send(proc, {"jsonrpc":"2.0","method":"notifications/initialized"})
    print("[OK] initialize")

    t0 = time.time()
    send(proc, {"jsonrpc":"2.0","id":2,"method":"tools/call",
                "params":{"name":"law_qa",
                          "arguments":{"question":"民警在巡逻中发现有人赌博，应如何处理？"}}})
    qa = json.loads(recv(proc, 2, 280)["result"]["content"][0]["text"])
    print(f"[OK] law_qa ({time.time()-t0:.0f}s) answer={qa['answer'][:60]}... sources={[(s['law_name'],s['article']) for s in qa['sources']]}")

    t0 = time.time()
    text = ("【当事人信息】1.报警人：王XX；2.当事人：赵XX。"
            "【警情内容及处置情况】赵XX因债务纠纷扬言\"再不还钱就砍死你全家\"，"
            "并持菜刀到王XX店门口叫嚣，民警到场将其控制。")
    send(proc, {"jsonrpc":"2.0","id":3,"method":"tools/call",
                "params":{"name":"risk_assess","arguments":{"text":text}}})
    ar = json.loads(recv(proc, 3, 280)["result"]["content"][0]["text"])
    print(f"[OK] risk_assess ({time.time()-t0:.0f}s) exists={ar['exists']} level={ar['level']} "
          f"persons={[(p['name'],p['level']) for p in ar['risk_persons']]} "
          f"refs={[(r['law_name'],r['article']) for r in ar['law_references']]} degraded={ar['degraded']}")
    print("\nREAL GATEWAY TEST PASSED")
finally:
    proc.kill()
