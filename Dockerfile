# 统一智能体镜像：法律问答（RAG）+ 警情风险研判，共享同一法律知识库
FROM python:3.11-slim

WORKDIR /app

# 仅 openpyxl（批量 xlsx 读写需要）；问答链路是纯标准库实现
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
COPY laws/ /app/laws/

# 预建挂载点目录
RUN mkdir -p /app/log /app/data /app/input /app/output

ENV LAWS_DIR=/app/laws \
    LOG_PATH=/app/log/run.log \
    CACHE_PATH=/app/data/kb_cache.json \
    INPUT_PATH=/app/input/input.xlsx \
    OUTPUT_PATH=/app/output/output.xlsx

EXPOSE 8888

# 默认启动统一服务（/qa + /assess）；批量模式见 README：
#   docker run ... police-legal-agent python src/assess_batch.py
CMD ["python", "src/service.py"]
