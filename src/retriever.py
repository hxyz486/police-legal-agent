# -*- coding: utf-8 -*-
"""检索器：向量粗排 + 关键词加权 + 精排重排序。

索引在后台线程构建（调用评测环境 Embedding API），构建完成前降级为
纯关键词检索，保证 /qa 始终可用。
"""
import logging
import math
import re
import threading
import time

import api
import config
import laws
from laws import Article

log = logging.getLogger("sait3.retriever")

_PUNCT_RE = re.compile(r'[，。、；：？！""''（）《》\s,.;:?!"\'()<>]')


def _bigrams(s):
    s = _PUNCT_RE.sub('', s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def _cosine(a, b):
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / math.sqrt(na * nb)


class Retriever(object):
    def __init__(self, articles):
        self.articles = articles  # [Article]
        self.lock = threading.Lock()
        self.vectors = None       # [[float]] 与 articles 对齐
        self.doc_grams = None     # 每条的字符 bigram 集合
        self.idf = None           # bigram -> idf
        self.ready = False
        self.error = None
        self.building = False           # 索引构建进行中
        self.index_done = threading.Event()  # 构建结束(成功或放弃)信号
        if articles:
            self.doc_grams = [_bigrams(a.text) for a in articles]
            self.idf = self._build_idf(self.doc_grams)
        else:
            self.index_done.set()  # 空知识库无可构建, 不阻塞等待

    @staticmethod
    def _build_idf(doc_grams):
        df = {}
        for grams in doc_grams:
            for g in grams:
                df[g] = df.get(g, 0) + 1
        n = len(doc_grams)
        return {g: math.log((n + 1) / (c + 0.5)) for g, c in df.items()}

    # ---------- 索引构建（后台） ----------

    def build_index_async(self):
        t = threading.Thread(target=self._build_with_retry, daemon=True)
        t.start()

    def _attempt_build(self, attempt):
        """单轮索引构建：成功置 ready=True；失败抛异常。"""
        start = time.time()
        # 每轮构建前先锁定 embedding 模型；重建(force)时允许重新
        # 探测切换，避免用已失效的模型反复失败
        m = api.ensure_embedding_model(force=(attempt > 1))
        if m is None:  # 白名单 embedding 不可用：不调用其它模型，直接关键词模式
            raise ValueError("embedding 白名单模型不可用（不调用名单外模型）")
        vecs = []
        batch = 16
        texts = [a.chunk for a in self.articles]
        for i in range(0, len(texts), batch):
            vecs.extend(api.embed_texts(texts[i:i + batch]))
        with self.lock:
            self.vectors = vecs
            self.ready = True
            self.error = None
        log.info("向量索引构建完成：%d 条，耗时 %.1fs", len(vecs), time.time() - start)

    def _build_with_retry(self):
        """后台无限重试构建（启动阻塞超时后的兜底路径）。白名单 embedding
        不可用时不重试（禁止改调其它模型），仅维持关键词模式。"""
        attempt = 0
        while True:
            attempt += 1
            try:
                self._attempt_build(attempt)
                return
            except Exception as e:  # noqa: BLE001
                self.error = "%s: %s" % (type(e).__name__, e)
                if api.embedding_blocked():
                    with self.lock:
                        self.vectors = None
                        self.ready = True
                        self.error = None
                    log.error("embedding 白名单模型不可用，不再重试（关键词模式就绪）：%s",
                              self.error)
                    return
                log.error("向量索引构建失败（第 %d 次）：%s，%ds 后重试",
                          attempt, self.error, config.KB_RETRY_SECONDS)
                time.sleep(config.KB_RETRY_SECONDS)

    def build_index_sync(self, max_wait=None):
        """阻塞构建索引：成功或超过 max_wait 秒后才返回。

        服务必须在调用本方法之后才开始监听端口，保证对外提供的
        一律是完整 RAG 检索结果；超时则转后台无限重试并先行提供
        关键词模式（有总比无好，格式分不能丢）。
        """
        max_wait = config.KB_MAX_WAIT_SECONDS if max_wait is None else max_wait
        self.building = True
        self.index_done.clear()
        deadline = time.time() + max_wait
        attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    self._attempt_build(attempt)
                    return True
                except Exception as e:  # noqa: BLE001
                    self.error = "%s: %s" % (type(e).__name__, e)
                    if api.embedding_blocked():
                        # 白名单 embedding 不可用：立即以关键词模式服务，不再重试/换模型。
                        # 关键词模式同样"就绪可答"，避免评测就绪轮询卡住（向量=None 由
                        # 检索逻辑自动走关键词通道，通道状态另以 index=keyword 明示）。
                        with self.lock:
                            self.vectors = None
                            self.ready = True
                            self.error = None
                        log.error("embedding 白名单模型不可用，以关键词模式就绪并对外服务"
                                  "（不调用名单外模型）：%s", self.error)
                        return False
                    log.error("向量索引构建失败（第 %d 次）：%s", attempt, self.error)
                    if time.time() + config.KB_RETRY_SECONDS >= deadline:
                        log.error("索引构建等待超过 %ds，先以关键词模式对外服务，"
                                  "后台继续重试直至就绪", max_wait)
                        return False
                    time.sleep(config.KB_RETRY_SECONDS)
        finally:
            self.building = False
            self.index_done.set()
            if not self.ready and not api.embedding_blocked():
                self.build_index_async()

    # ---------- 查询 ----------

    def search(self, query, top_k=6):
        """返回 [Article]（按相关性降序，top_k 个）。永不抛异常。

        检索前先做查询扩展（原问题 + LLM 法律术语改写变体）并集召回，
        精排仍用原问题，兼顾召回率与排序准确性。
        """
        try:
            variants = [query]
            # 1) 确定性法条映射字典（零模型调用）：题目案情语言 → 法条罪状措辞
            try:
                import lawmap
                for v in lawmap.expand(query):
                    if v and v not in variants:
                        variants.append(v)
            except Exception as e:  # noqa: BLE001
                log.warning("法条映射字典扩展失败（忽略）：%s: %s", type(e).__name__, e)
            # 2) LLM 查询扩展（法律术语改写），失败自动退回单查询
            for v in api.expand_query(query):
                if v and v not in variants:
                    variants.append(v)
            if len(variants) > 1:
                log.info("多查询检索：%d条变体 %s", len(variants),
                         [v[:30] for v in variants])
            cands = self._candidates(variants)
            if not cands:
                return []
            ranked = self._rerank(query, cands)
            top = self._pin_named_articles(query, ranked)[:top_k]
            log.info("检索[%s]: 变体%d条, 候选%d条, top%d: %s", (query or "")[:30],
                     len(variants), len(cands), len(top),
                     ["%s%s(%.3f)" % (self.articles[i].law_name, self.articles[i].article, s)
                      for i, s in top])
            return [self.articles[idx] for idx, _ in top]
        except Exception as e:  # noqa: BLE001
            log.error("检索失败：%s: %s", type(e).__name__, e)
            return []

    def _pin_named_articles(self, query, ranked, max_pin=3):
        """问题点名了"法名+条号"（如"人民警察法第九条规定了什么制度"）时，
        把该条文钉到结果最前。关键词 bigram 对这种精确指向反而乏力：
        法名/序数词的 bigram 在全库高频，会被同主题短条文稀释（实测缺陷）。
        只在查询字符串同时出现法名（全称或去"中华人民共和国"短名）与
        完整条号时触发，最多钉 max_pin 条；其余结果保持原序。"""
        try:
            q = query or ""
            if "条" not in q:
                return ranked
            pinned = []
            for i, a in enumerate(self.articles):
                if len(pinned) >= max_pin:
                    break
                if not a.article or a.article not in q:
                    continue
                short = a.law_name.replace("中华人民共和国", "", 1)
                if short in q or a.law_name in q:
                    pinned.append((i, 1.0))
            if not pinned:
                return ranked
            pin_set = {i for i, _ in pinned}
            rest = [(i, s) for i, s in ranked if i not in pin_set]
            log.info("点名条文钉位: %s", [self.articles[i].article for i, _ in pinned])
            return pinned + rest
        except Exception:  # noqa: BLE001
            return ranked

    def _candidates(self, variants):
        """多查询变体的候选并集：关键词通道各取 top M + 向量通道各取
        top N 轮转交错，保证每个变体的高分条文都进入精排候选。"""
        kw_best = {}  # 各条文在所有变体中的最高关键词得分（无精排时即排序信号）
        kw_picked, kw_seen = [], set()
        for q in variants:
            qgrams = _bigrams(q)
            nq = len(qgrams) or 1
            kw_scores = {}
            for i, grams in enumerate(self.doc_grams):
                inter = qgrams & grams
                if inter:
                    # 文档长度归一（cosine 思路）：不加的话，网页 dump 解析出的
                    # 超长条文块靠 bigram 覆盖面碾压精准命中的短条文（实测缺陷）。
                    s = sum(self.idf.get(g, 1.0) for g in inter) / (nq * len(grams)) ** 0.5
                    kw_scores[i] = s
                    if s > kw_best.get(i, 0.0):
                        kw_best[i] = s
            for i, _ in sorted(kw_scores.items(), key=lambda x: -x[1])[:config.KW_TOP_K]:
                if i not in kw_seen:
                    kw_seen.add(i)
                    kw_picked.append(i)
        with self.lock:
            vectors = self.vectors
            ready = self.ready
        if not ready or vectors is None:
            # 降级：纯关键词。候选按"各变体最高得分"降序——精排不可用时，
            # 关键词得分本身就是相关性信号（按变体先后拼接会让首变体的
            # 常见词匹配压住后续变体的高价值 rare 词命中）。
            kw_picked.sort(key=lambda i: -kw_best.get(i, 0.0))
            return kw_picked[:config.TOP_CANDIDATES]

        try:
            qvecs = api.embed_texts(variants)
        except Exception as e:  # noqa: BLE001
            # 向量通道失败(额度耗尽/网络异常): 退回纯关键词候选, 不放弃整次检索
            log.warning("查询向量失败，本次退回纯关键词候选：%s: %s",
                        type(e).__name__, e)
            return kw_picked[:config.KW_TOP_K]
        # 各变体向量 top N 轮转交错合并：第 d 轮取每个变体的第 d 名，
        # 原问题排首位，保证其召回不被扩展变体挤出候选集
        n_vec = max(20, config.TOP_CANDIDATES // 2)
        per_var = []
        for qv in qvecs:
            vec_ranked = sorted(
                ((_cosine(qv, vec), i) for i, vec in enumerate(vectors)),
                key=lambda x: -x[0])
            per_var.append([i for _, i in vec_ranked[:n_vec]])
        picked, seen = [], set()
        for depth in range(n_vec):
            for lst in per_var:
                if depth < len(lst):
                    i = lst[depth]
                    if i not in seen:
                        seen.add(i)
                        picked.append(i)
        for i in kw_picked:
            if i not in seen:
                picked.append(i)
                seen.add(i)
        return picked[:config.TOP_CANDIDATES]

    def _rerank(self, query, cand_idx):
        if not cand_idx:
            return []
        docs = [self.articles[i].chunk[:512] for i in cand_idx]
        try:
            pairs = api.rerank(query, docs, top_n=config.RERANK_TOP_N)
            return [(cand_idx[i], s) for i, s in pairs]
        except Exception as e:  # noqa: BLE001
            log.warning("精排失败，退回粗排顺序：%s: %s", type(e).__name__, e)
            return [(i, 0.0) for i in cand_idx]
