#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量警情风险研判命令行入口。

用法：
    python src/assess_batch.py                       # 用 INPUT_PATH/OUTPUT_PATH 环境变量
    python src/assess_batch.py input.xlsx out.xlsx   # 显式指定输入输出

容器内评测形态：
    docker run --rm -e LLM_API_URL=... -e LLM_API_KEY=... \
        -v $PWD/input:/app/input -v $PWD/output:/app/output \
        -v $PWD/log:/app/log police-legal-agent \
        python src/assess_batch.py /app/input/input.xlsx /app/output/output.xlsx
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from risk import assess  # noqa: E402

if __name__ == "__main__":
    assess.setup_logging()
    code = assess.main(sys.argv[1:])
    sys.stdout.flush()
    sys.exit(code)
