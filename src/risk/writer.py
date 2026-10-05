# -*- coding: utf-8 -*-
"""写出 output.xlsx：Sheet「风险研判结果」，7 列表头与规范逐字一致。"""
import logging
import os

from openpyxl import Workbook

from . import config

log = logging.getLogger("saiti.writer")

HEADERS = ["反馈单编号", "出警情况", "风险人员姓名", "风险人员身份证号",
           "是否存在风险人员", "风险等级", "判断依据"]


def write_rows(rows, path: str = None, atomic: bool = True) -> str:
    """rows: [(编号, 出警情况, 姓名, 证号, 是否存在, 等级, 依据), ...]

    是否存在风险人员列写入布尔值（与官方样例集一致）。
    atomic=True 时先写临时文件再原子替换，保证任何时刻落盘的都是完整文件。
    """
    path = path or config.OUTPUT_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "风险研判结果"
    ws.append(HEADERS)
    for r in rows:
        if len(r) != len(HEADERS):
            raise ValueError(f"行字段数不符：{len(r)} != {len(HEADERS)}")
        rid, text, names, ids, exists, level, reason = r
        exists_bool = exists if isinstance(exists, bool) else str(exists).strip().lower() == "true"
        ws.append([rid, text, names, ids, exists_bool, level, reason])
        # 身份证号列强制文本格式，防止被 Excel 当数值显示
        for idx in (3, 4):
            ws.cell(row=ws.max_row, column=idx).number_format = "@"
    if atomic:
        tmp = path + ".tmp"
        wb.save(tmp)
        os.replace(tmp, path)
    else:
        wb.save(path)
    log.info("输出已写入：%s（%d 行）", path, len(rows))
    return path


def write_degraded(records, path: str = None) -> str:
    """全量降级版：编号+出警情况原样，其余按无风险输出。"""
    rows = [(rid, text, "", "", "false", "无", "无") for rid, text in records]
    return write_rows(rows, path)
