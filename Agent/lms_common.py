
import hashlib
import json
import os
from contextvars import ContextVar
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))


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