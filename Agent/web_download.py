#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_download.py — Tải tài liệu LMS ON-DEMAND qua web (hướng B).

Ý tưởng:
  - Agent CHỈ trả về LINK tải (vd '/download/ab12cd34'), KHÔNG nhét bytes vào câu trả lời.
  - Backend giữ PHIÊN LMS (cookie MoodleSession) của từng sinh viên, và khi người dùng
    bấm link thì dùng phiên đó tải file từ LMS rồi STREAM về trình duyệt.

Vì sao phải có phiên: URL pluginfile.php của Moodle cần cookie đăng nhập mới tải được;
gọi thẳng sẽ bị đá về trang login. Nên mỗi mssv cần 1 requests.Session đã auth.

CHỖ DUY NHẤT phụ thuộc cách login của web là hàm dang_nhap_lms() — xem chú thích (*).
"""
import json
import os
import re
import threading
import time
from urllib.parse import urljoin

import requests
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse

from lms_common import file_id, path_tai_lieu   # cùng file_id với agent -> fid khớp
from auth import get_mssv                        # lấy mssv từ phiên web (không qua URL)

SESSION_TTL = int(os.getenv("LMS_SESSION_TTL", str(30 * 60)))   # coi cookie sống 30 phút

LMS_LOGIN_URL = ("https://sso.hcmut.edu.vn/cas/login?service="
                 "https%3A%2F%2Flms.hcmut.edu.vn%2Flogin%2Findex.php%3FauthCAS%3DCAS")


# ============================================================================
#  KHO PHIÊN LMS theo mssv  (để RAM; chạy nhiều tiến trình thì thay bằng Redis)
# ============================================================================
_sessions: dict = {}                 # mssv -> (requests.Session, thoi_diem_luu)
_lock = threading.Lock()


def luu_phien(mssv: str, sess: requests.Session) -> None:
    with _lock:
        _sessions[mssv] = (sess, time.time())


def lay_phien(mssv: str):
    """Trả requests.Session còn hạn, hoặc None nếu chưa có / đã hết hạn."""
    with _lock:
        item = _sessions.get(mssv)
    if not item:
        return None
    sess, ts = item
    if time.time() - ts > SESSION_TTL:
        with _lock:
            _sessions.pop(mssv, None)
        return None
    return sess


# ============================================================================
#  (*) ĐĂNG NHẬP LMS phía server
# ============================================================================
def _field_an(html: str) -> dict:
    """Bóc TẤT CẢ <input type=hidden> (name -> value). CAS trường có lt, execution,
    _eventId — gom hết rồi gửi lại thì bền, không vỡ khi trường đổi giao diện."""
    out = {}
    for m in re.finditer(r"<input\b[^>]*>", html, re.I):
        tag = m.group(0)
        if not re.search(r'type=["\']hidden["\']', tag, re.I):
            continue
        name = re.search(r'\bname=["\']([^"\']+)["\']', tag, re.I)
        val = re.search(r'\bvalue=["\']([^"\']*)["\']', tag, re.I)
        if name:
            out[name.group(1)] = val.group(1) if val else ""
    return out


def dang_nhap_lms(mssv: str, password: str) -> requests.Session:
    """Đăng nhập LMS bằng CAS, trả requests.Session đã có cookie MoodleSession.

    Gọi 1 lần khi có credential (lúc user đăng nhập web / job crawl), rồi luu_phien(mssv, sess).
    Sau đó /download tái dùng phiên, KHÔNG cần mật khẩu nữa cho tới khi hết hạn.

    Cách làm: lấy trang login CAS -> GOM HẾT field ẩn (lt, execution, _eventId) -> POST
    kèm username/password. Gom hết thay vì bóc riêng 'execution' vì CAS của trường còn đòi
    'lt' (login ticket); thiếu là bị từ chối.
    """
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"

    r = s.get(LMS_LOGIN_URL, timeout=30)
    fields = _field_an(r.text)
    if "execution" not in fields:
        raise RuntimeError("Không đọc được form CAS (thiếu 'execution') — kiểm tra trang login.")

    data = dict(fields)                 # giữ nguyên lt + execution + _eventId CAS vừa phát
    data["username"] = mssv
    data["password"] = password
    data.setdefault("_eventId", "submit")
    action = urljoin(r.url, re.search(r'<form\b[^>]*\baction=["\']([^"\']+)["\']',
                                      r.text, re.I).group(1)) if re.search(
        r'<form\b[^>]*\baction=', r.text, re.I) else r.url

    s.post(action, data=data, timeout=30)   # theo redirect CAS -> Moodle set MoodleSession

    if not any(c.name == "MoodleSession" for c in s.cookies):
        raise RuntimeError("Đăng nhập LMS thất bại (không có cookie MoodleSession).")
    return s


# ============================================================================
#  ĐỌC DANH MỤC TÀI LIỆU + ĐỊNH DANH FILE
# ============================================================================
def _doc_tai_lieu(mssv: str) -> list:
    try:
        return json.load(open(path_tai_lieu(mssv), encoding="utf-8"))
    except Exception:
        return []


# ============================================================================
#  ROUTER  (main.py gắn vào app chính bằng app.include_router)
# ============================================================================
router = APIRouter()


@router.get("/download/{fid}")
def download(fid: str, mssv: str = Depends(get_mssv)):
    # mssv đến từ phiên web đã xác thực (auth.get_mssv), KHÔNG nhận qua URL -> tránh IDOR
    rec = next((it for it in _doc_tai_lieu(mssv)
                if it.get("url") and file_id(it["url"]) == fid), None)
    if not rec:
        raise HTTPException(404, "Không tìm thấy tài liệu.")

    sess = lay_phien(mssv)
    if sess is None:
        raise HTTPException(401, "Phiên LMS đã hết hạn — vui lòng đăng nhập lại.")

    try:
        r = sess.get(rec["url"], stream=True, timeout=60)
    except requests.RequestException:
        raise HTTPException(502, "Không kết nối được LMS.")

    # Bị đá về trang login -> phiên chết -> báo re-auth, ĐỪNG trả HTML rác
    if r.status_code != 200 or "text/html" in r.headers.get("content-type", ""):
        with _lock:
            _sessions.pop(mssv, None)
        raise HTTPException(401, "Phiên LMS không còn hợp lệ — vui lòng đăng nhập lại.")

    ten = rec.get("ten_file") or "tai_lieu"
    return StreamingResponse(
        r.iter_content(chunk_size=8192),
        media_type=r.headers.get("content-type", "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{ten}"'},
    )