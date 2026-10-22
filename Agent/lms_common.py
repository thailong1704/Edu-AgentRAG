#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lms_common.py — Thứ dùng CHUNG giữa agent (lich_agent) và backend (web_download).

Gồm 3 thứ:
  1) file_id(url)          — định danh 1 file từ URL. Agent dùng để dựng link /download/{fid};
                             backend dùng để tra ngược fid -> URL. PHẢI cùng một hàm để khớp.
  2) path_tai_lieu(mssv)   — đường dẫn JSON tài liệu của TỪNG sinh viên: data/{mssv}/...
  3) mssv_hien_tai         — ContextVar giữ mssv của người ĐANG đăng nhập trong lượt xử lý.
                             Lớp web set nó (từ phiên auth) TRƯỚC khi gọi team; các tool đọc
                             nó để lấy đúng dữ liệu của người đó — KHÔNG để LLM tự điền mssv.
"""
import hashlib
import json
import os
from contextvars import ContextVar
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))

# Rỗng nghĩa là chưa có ngữ cảnh người dùng (vd chạy terminal 1 người) -> fallback file env.
# LƯU Ý: biến này giữ KHÓA ĐĂNG NHẬP (tên đăng nhập) — cũng là tên thư mục data/{khoa}/.
# Neo4j lại khóa theo MSSV thật; đổi khoa -> mssv bằng doc_mssv() bên dưới.
mssv_hien_tai: ContextVar[str] = ContextVar("mssv_hien_tai", default="")


def file_id(url: str) -> str:
    """Định danh ổn định 12 ký tự cho 1 file, suy từ URL của nó."""
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:12]


def path_tai_lieu(khoa: str) -> Path:
    return DATA_DIR / khoa / "tai_lieu_lms.json"


def path_lich(khoa: str) -> Path:
    return DATA_DIR / khoa / "lich_hoc_thi.json"


def doc_ctxh(khoa: str):
    """Số ngày CTXH (công tác xã hội) đã tích lũy của người dùng, đọc từ
    data/{khoa}/lich_hoc_thi.json (do crawl_dichvu ghi ở khóa 'ctxh.tong_so_ngay').

    READER DÙNG CHUNG cho lich_agent (tra cứu) và gpa_agent (xét điều kiện lên trình độ)
    -> một đường parse, tránh lệch. Trả int, hoặc None nếu chưa có/đọc không được."""
    try:
        d = json.load(open(path_lich(khoa), encoding="utf-8"))
    except Exception:
        return None
    n = ((d or {}).get("ctxh") or {}).get("tong_so_ngay")
    try:
        return int(n) if n is not None else None
    except (ValueError, TypeError):
        return None


def doc_mssv(khoa: str) -> str:
    """Đổi KHÓA ĐĂNG NHẬP -> MSSV thật, đọc từ data/{khoa}/ket_qua.json.

    Vì thư mục data/ đặt theo tên đăng nhập (có ngay lúc login), còn Neo4j khóa theo
    MSSV (chỉ biết sau khi crawl bảng điểm). Trả '' nếu chưa có file / chưa đọc được."""
    try:
        data = json.load(open(DATA_DIR / khoa / "ket_qua.json", encoding="utf-8"))
    except Exception:
        return ""
    for block in ("chuong_trinh_dao_tao", "thong_tin_sinh_vien"):
        for line in data.get(block, []) or []:
            s = str(line)
            if s.startswith("Mã sinh viên:"):
                return s.split(":", 1)[1].strip()
    return ""