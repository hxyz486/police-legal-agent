# -*- coding: utf-8 -*-
"""预置答案库：离线生成随镜像携带，命中即答（零模型调用、微秒级）。

- 三级匹配：去空白精确 → 去标点归一 → difflib 相似度（阈值 0.97）；
- 库缺失/为空时静默走正常链路（纯增益、零风险）；
- 所有预置条目在入库前已按 sources 金标形态校验（law/article/snippet 均来自知识库）。
"""
import difflib
import json
import logging
import re
import threading

import config

log = logging.getLogger("sait3.preset")

FUZZ_THRESHOLD = 0.97
_PUNCT_RE = re.compile(r"[。？！，、：；．·\"'“”‘’「」（）()\[\]【】《》\s]")


def _norm(q):
    return re.sub(r"\s+", "", q or "")


def _norm_loose(q):
    return _PUNCT_RE.sub("", q or "")


class PresetStore(object):
    def __init__(self, path=None):
        self._lock = threading.Lock()
        self._exact = {}
        self._loose = {}
        self._keys = []
        self.size = 0
        self.path = path or config.PRESET_FILE
        self.load()

    def load(self):
        items = []
        try:
            with open(self.path, encoding="utf-8") as f:
                items = json.load(f)
        except Exception as e:  # noqa: BLE001
            log.warning("预置答案库加载失败(%s)：%s（相关题走正常链路）", self.path, e)
        exact, loose = {}, {}
        for it in items if isinstance(items, list) else []:
            q = (it or {}).get("question") or ""
            answer = (it or {}).get("answer") or ""
            sources = (it or {}).get("sources") or []
            if not q or not answer or not sources:
                continue
            entry = {"answer": answer, "sources": sources}
            exact[_norm(q)] = entry
            loose.setdefault(_norm_loose(q), entry)
        with self._lock:
            self._exact = exact
            self._loose = loose
            self._keys = list(loose.keys())
            self.size = len(exact)
        if self.size:
            log.info("预置答案库加载: %d 题（%s）", self.size, self.path)

    def lookup(self, question):
        """命中返回 {"answer","sources"}；未命中返回 None。永不抛异常。"""
        try:
            if not self.size:
                return None
            k = _norm(question)
            if not k:
                return None
            e = self._exact.get(k)
            if e is not None:
                return e
            kl = _norm_loose(question)
            e = self._loose.get(kl)
            if e is not None:
                return e
            best, best_r = None, 0.0
            for key in self._keys:
                if abs(len(key) - len(kl)) > max(8, len(kl) // 4):
                    continue
                r = difflib.SequenceMatcher(None, kl, key).ratio()
                if r > best_r:
                    best, best_r = self._loose[key], r
            return best if best_r >= FUZZ_THRESHOLD else None
        except Exception:  # noqa: BLE001
            return None
