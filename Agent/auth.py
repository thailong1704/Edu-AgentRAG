#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auth.py — Phiên đăng nhập WEB (ai đang dùng app của bạn), tách bạch với phiên LMS.

Phân biệt 2 loại "phiên" trong hệ này:
  - Phiên WEB (file này): token <-> mssv. Xác định NGƯỜI DÙNG đang đăng nhập app.
  - Phiên LMS (web_download.py): cookie MoodleSession để TẢI file từ LMS.

get_mssv() là FastAPI dependency dùng chung cho /chat, /download... để lấy mssv của
người đang đăng nhập TỪ COOKIE (không nhận mssv qua URL/body -> tránh giả mạo/IDOR).

Lưu RAM cho gọn; chạy nhiều tiến trình thì thay _web_sessions bằng Redis.
"""
import secrets

from fastapi import Cookie, HTTPException

COOKIE_NAME = "sid"
_web_sessions: dict = {}          # token -> mssv


def tao_phien_web(mssv: str) -> str:
    token = secrets.token_urlsafe(24)
    _web_sessions[token] = mssv
    return token


def xoa_phien_web(token: str) -> None:
    _web_sessions.pop(token, None)


def _mssv_tu_token(token: str):
    return _web_sessions.get(token) if token else None


def get_mssv(sid: str = Cookie(default=None)) -> str:
    """Bắt buộc đăng nhập: trả mssv hoặc 401."""
    mssv = _mssv_tu_token(sid)
    if not mssv:
        raise HTTPException(status_code=401, detail="Chưa đăng nhập.")
    return mssv


def get_mssv_optional(sid: str = Cookie(default=None)):
    """Không bắt buộc: trả mssv nếu đã đăng nhập, else None (dùng cho /chat guest)."""
    return _mssv_tu_token(sid)
