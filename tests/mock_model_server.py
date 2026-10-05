# -*- coding: utf-8 -*-
"""独立 mock 模型服务（服务器容器联调用）：LLM / Embedding / Rerank，Python3.6 兼容。"""
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

PORT = 18765


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _vec(text, dim=64):
    v = [0.0] * dim
    s = re.sub(r'[，。、；：？！（）\s]', '', text)
    for i in range(len(s) - 1):
        g = s[i:i + 2]
        h = 0
        for ch in g:
            h = (h * 131 + ord(ch)) % dim
        v[h] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


def _overlap(query, doc):
    a = set(re.sub(r'[，。、；：？！（）\s]', '', query))
    b = set(re.sub(r'[，。、；：？！（）\s]', '', doc))
    return len(a & b) / (len(b) or 1)


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        if self.path.endswith("/embeddings"):
            texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
            self._json({"data": [{"index": i, "embedding": _vec(t)} for i, t in enumerate(texts)]})
        elif self.path.endswith("/rerank"):
            docs = body["documents"]
            scores = [{"index": i, "relevance_score": _overlap(body["query"], d)}
                      for i, d in enumerate(docs)]
            scores.sort(key=lambda x: -x["relevance_score"])
            topn = body.get("top_n") or len(docs)
            self._json({"results": scores[:topn]})
        elif self.path.endswith("/chat/completions"):
            prompt = body["messages"][-1]["content"]
            # 风险研判类请求（risk 流水线）返回标准研判 JSON，其余按问答 mock
            if "研判以下警情反馈单" in prompt or "警情筛查员" in prompt:
                if "警情筛查员" in prompt:
                    content = '{"exists": true, "names": ["赵XX"]}'
                elif "菜刀" in prompt or "砍死" in prompt:
                    content = ('{"risk_persons": [{"name": "赵XX", '
                               '"id_number": "321120XXXXXXXX0416", "level": "高", '
                               '"reason": "持械并扬言砍死他人，扬言报复且已持械实施威胁行为"}]}')
                else:
                    content = '{"risk_persons": []}'
                self._json({"choices": [{"message": {"content": content, "role": "assistant"}}]})
                return
            cited = []
            for cm in re.finditer(r'(\[\d\]) (《[^》]+》)(第[^：\n：]+)：', prompt):
                cited.append("%s%s" % (cm.group(2), cm.group(3)))
            answer = u"根据提供的法条：\n" + u"；\n".join(cited[:3]) + u"\n请依法处理。\n引用：" + u"；".join(cited[:3]) + u"。"
            self._json({"choices": [{"message": {"content": answer, "role": "assistant"}}]})
        else:
            self._json({})

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json({"data": [{"id": "mock-llm"}]})
        else:
            self._json({})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Mock)
    srv.daemon_threads = True
    print("mock model server on :%d" % PORT)
    srv.serve_forever()
