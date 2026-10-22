#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
normalize.py — CHUẨN HÓA + TÁCH 2 COLLECTION + LÀM SẠCH (một lần chạy)
---------------------------------------------------------------------
1) Gom các file chunk (schema A/B/C) về một schema thống nhất.
2) Tách thành 2 collection:
     - chunks_ctdt.json        (schema B: có ma_ctdt/nganh)  -> "ctdt"
     - chunks_hanh_chinh.json  (schema A, C)                 -> "hanh_chinh"
3) Làm sạch mỗi chunk (đúng 3 việc, KHÔNG dedup):
     (a) strikethrough: gỡ nội dung CŨ bị gạch (~~...~~), giữ nội dung mới.
     (b) bỏ MẢNH BẢNG MỒ CÔI: bị dấu '-' áp đảo, rất ít chữ (bảng vỡ).
     (c) bỏ RÁC kiểu '".': chunk gần như không có chữ (hoặc rỗng sau khi gỡ (a)).

>>> KHÔNG khử trùng lặp. Nếu chưa xử lý trùng lặp ở pipeline chunk, các bản
    trùng (nhất là CTĐT) vẫn còn — xử lý ở khâu chunk trước khi embedding.
"""

import json
import os
import re
import unicodedata
from datetime import datetime

# ----------------------------------------------------------------------------
# CẤU HÌNH
# ----------------------------------------------------------------------------
INPUT_DIR  = r"C:\Users\ADMIN\Desktop\agent_A\data\KnowledgeGraph\output\chunks"
OUTPUT_DIR = r"C:\Users\ADMIN\Desktop\agent_A\data\KnowledgeGraph\output"

OUTPUT_FILES = {
    "ctdt":       os.path.join(OUTPUT_DIR, "chunks_ctdt.json"),
    "hanh_chinh": os.path.join(OUTPUT_DIR, "chunks_hanh_chinh.json"),
}
REMOVED_FILE = os.path.join(OUTPUT_DIR, "chunks_removed.json")

# Ép route theo tên source (tùy chọn). Vd: {"quydinh": "hanh_chinh"}
SOURCE_TO_COLLECTION = {}

# REMAP tên-file -> SOURCE CHUẨN (khớp cái các tool lọc: hoc_phi/lich_hoc_vu/quydinh/
# luan_van_thuc_tap_md). Cần vì mặc định source = slugify(tên file); nếu để nguyên,
# các file lô "hỗ trợ SV" sẽ ra source lạ ("7_bang_tong_hop...") -> tool lọc-source-cứng
# KHÔNG thấy -> tri thức mới vô hình. Key = tên file (KHÔNG đuôi), đúng như trên đĩa.
# Không có trong bảng -> giữ source suy từ tên file như cũ.
FILE_TO_SOURCE = {
    "10_Học_phí": "hoc_phi",
    "2_BẢNG_LỊCH_TRÌNH_CÁC_MỐC_THỜI_GIAN_CẬP_NHẬT_CÁC_CHUẨN_ĐỂ_ĐĂNG_KÝ_MÔN_HỌC_THEO_LỊCH_HỌC_VỤ_NĂM_HỌC_2026-2027": "lich_hoc_vu",
    "1_Đăng_ký_môn_học_-_Quy_định_-_Hướng_dẫn_chung": "quydinh",
    "3_Quy_trình_đăng_ký_môn_học": "quydinh",
    "4_Hướng_dẫn_gửi_xác_minh_văn_bằng,_bảng_điểm_đến_trườngtổ_chức_nước_ngoài": "quydinh",
    "5_Thông_báo_tân_sinh_viên": "quydinh",
    "6_Ngoại_ngữ": "quydinh",
    "7_Bảng_tổng_hợp_quy_định_các_chuẩn": "quydinh",
    "8_Đánh_giá_kết_quả_học_tập": "quydinh",
    "9_Quy_chế_-_Quy_định": "quydinh",
    "11_Rút_môn_học": "quydinh",
    "12_Hoãn_thi_Giữa_kỳ_và_Cuối_kỳ": "quydinh",
    "13_Đào_tạo_song_ngành": "quydinh",
    "14_Kéo_dài_thời_gian_đào_tạo": "quydinh",
    "15_Đăng_ký_Thông_tin_văn_bằng": "quydinh",
}

STRIP_CONTEXT_PREFIX = True
NORMALIZE_UNICODE_NFC = True

# --- cấu hình làm sạch ---
CLEAN_ENABLED         = True
MIN_WORD_CHARS        = 10    # (c) rác thuần: dưới ngần này ký tự chữ-số -> bỏ
ORPHAN_MAX_WORD_CHARS = 60    # (b) mảnh bảng mồ côi: ít chữ hơn ngần này ...
ORPHAN_DASH_RUN       = 15    #     ... VÀ có chuỗi >= ngần này dấu '-' liên tiếp
SAVE_REMOVED          = True

CONTEXT_PREFIX_RE = re.compile(r"^\[NGỮ CẢNH\][^\n]*\n\n", flags=re.DOTALL)
WORD_RE     = re.compile(r"[0-9A-Za-zÀ-ỹ]")
DASH_RUN_RE = re.compile(r"-{%d,}" % ORPHAN_DASH_RUN)


# ----------------------------------------------------------------------------
# HÀM ĐẾM TOKEN — THAY bằng tokenizer THẬT của embedding model bạn dùng (BGE-M3...)
# ----------------------------------------------------------------------------
def count_tokens(text: str) -> int:
    return int(round(len(text.split()) * 1.6))


# ----------------------------------------------------------------------------
# TIỆN ÍCH
# ----------------------------------------------------------------------------
def nfc(s):
    if s is None:
        return s
    if NORMALIZE_UNICODE_NFC and isinstance(s, str):
        return unicodedata.normalize("NFC", s)
    return s


def clean_text(t: str) -> str:
    if not t:
        return ""
    t = nfc(t)
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def iso_date(raw):
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s or "unknown"


# ---- làm sạch ----
def word_chars(t: str) -> int:
    return len(WORD_RE.findall(t or ""))


def is_pure_junk(text: str) -> bool:
    return word_chars(text) < MIN_WORD_CHARS


def is_orphan_table(text: str) -> bool:
    t = text or ""
    return DASH_RUN_RE.search(t) is not None and word_chars(t) < ORPHAN_MAX_WORD_CHARS


def strip_strikethrough(text: str):
    if "~~" not in text:
        return text, False
    new = re.sub(r"~~.*?~~", "", text)   # bỏ nội dung giữa ~~ ~~ (phần cũ)
    new = new.replace("~~", "")          # bỏ dấu ~~ lẻ còn sót
    new = re.sub(r"[ \t]{2,}", " ", new)
    new = "\n".join(line.rstrip() for line in new.split("\n"))
    new = re.sub(r"\n{3,}", "\n\n", new).strip()
    return new, (new != text)


def extract_nam_hoc(records):
    """Trích 'năm học' (vd '2026-2027') từ NỘI DUNG tài liệu.
    Cần thiết vì nhiều tài liệu (lịch học vụ) có doc_title trơn ('Lịch học vụ'),
    năm học chỉ nằm trong text ('Về việc Lịch học vụ năm học 2026-2027')."""
    for rec in records[:5]:                      # năm học thường ở chunk mở đầu
        text = rec.get("text") or rec.get("content") or ""
        m = re.search(r"n\u0103m h\u1ecdc\s*(20\d{2})\s*[-\u2013]\s*(20\d{2})", text, re.I)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return None


# ---- Trích MÃ HỌC KỲ + NĂM HỌC cho từng chunk (quan trọng với lịch học vụ) ----
# Lịch học vụ chứa mốc của NHIỀU năm học với nội dung gần giống hệt nhau
# ("Đăng ký môn học (Đợt 1) | Thời gian bắt đầu: ...") -> retrieval rất dễ lấy nhầm
# mốc của năm cũ đã trôi qua. Năm học/học kỳ chỉ nằm trong section_path, KHÔNG nằm
# trong text, nên phải nâng lên metadata để lọc cứng được.
#   "... Lịch học vụ năm học 2025-2026 > HK1/2025-2026 (HK251) > I- Kế hoạch..."
#       -> ma_hoc_ky='HK251', hoc_ky=1, nam_hoc='2025-2026'
RE_MA_HK = re.compile(r"\bHK\s?(\d{3})\b")
RE_NAM_HOC_2 = re.compile(r"(20\d{2})\s*[-\u2013]\s*(20\d{2})")


def enrich_hoc_ky(chunk: dict) -> None:
    """Bổ sung ma_hoc_ky / hoc_ky / nam_hoc vào metadata (sửa tại chỗ)."""
    md = chunk.get("metadata", {}) or {}
    sec = str(md.get("section_path") or "")
    tit = str(md.get("doc_title") or "")

    ma_hk = hoc_ky = None
    m = RE_MA_HK.search(sec) or RE_MA_HK.search(tit)
    if m:
        code = m.group(1)
        ma_hk = f"HK{code}"
        hk = int(code[2])
        if hk in (1, 2, 3):          # 1=HK1, 2=HK2, 3=HK3 (hè)
            hoc_ky = hk

    # nam_hoc: ưu tiên section_path (cụ thể nhất) -> suy từ mã HK -> giữ mức tài liệu
    nam_hoc = None
    mm = RE_NAM_HOC_2.search(sec)
    if mm:
        nam_hoc = f"{mm.group(1)}-{mm.group(2)}"
    elif ma_hk:
        yy = 2000 + int(ma_hk[2:4])   # HK251 -> 2025-2026
        nam_hoc = f"{yy}-{yy + 1}"

    if ma_hk:
        md["ma_hoc_ky"] = ma_hk
    if hoc_ky:
        md["hoc_ky"] = hoc_ky
    if nam_hoc:
        md["nam_hoc"] = nam_hoc       # ghi đè nam_hoc mức tài liệu bằng mức chunk


# ---- Trích BẬC / CHƯƠNG TRÌNH / KHÓA / NGÀNH cho từng chunk (GENERIC, theo mẫu) ----
# Suy hoàn toàn từ section_path + text, KHÔNG dựa vào tên file. Chỉ set field khi nhận
# diện được mẫu -> tài liệu nào không có mẫu thì bỏ qua, không gán bừa.
#   • Học phí: "III. ĐẠI HỌC > 1. Chương trình CQ", "... > a. Khóa 2025 về sau ..."
#       -> bac='dai_hoc', chuong_trinh='CQ', khoa=['2025',...]
#   • Chuẩn tín chỉ tốt nghiệp: chunk kiểu 'row' có "Mã Khoa: BD / Ngành: ..."
#       -> nganh='Bảo dưỡng Công nghiệp', ma_nganh='BD'
RE_KHOA      = re.compile(r"[Kk]ho[áa]\s*(20\d{2})")
RE_NGANH     = re.compile(r"Ng[àa]nh\s*:\s*(.+)")
RE_MA_KHOA   = re.compile(r"M[ãa]\s*Khoa\s*:\s*([A-Za-zÀ-ỹ0-9]+)")
RE_SEC_PAREN = re.compile(r"^(.+?)\s*\(([A-Za-zÀ-ỹ0-9]{1,8})\)\s*$")


def _khong_dau(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.replace("đ", "d").replace("Đ", "d").lower()   # đ/Đ không tách bằng NFD


def enrich_phan_loai(chunk: dict) -> None:
    """Gắn bac / chuong_trinh / khoa / nganh / ma_nganh nếu suy được từ section_path+text.
       Sửa metadata tại chỗ. An toàn cho MỌI loại tài liệu (chỉ set khi khớp mẫu)."""
    md   = chunk.get("metadata", {}) or {}
    sec  = str(md.get("section_path") or "")
    text = chunk.get("text", "") or ""
    low  = _khong_dau(sec)

    # --- BẬC (đại học / sau đại học) ---
    if "sau dai hoc" in low:
        md["bac"] = "sau_dai_hoc"
    elif "dai hoc" in low:
        md["bac"] = "dai_hoc"

    # --- CHƯƠNG TRÌNH (đặt theo từ khóa trong section_path) ---
    if "chuong trinh cq" in low:
        md["chuong_trinh"] = "CQ"
    elif any(k in low for k in ("ctta", "cttt", "ctqt", "cthnb", "ctnb", "chat luong cao")):
        md["chuong_trinh"] = "CLC"
    elif ("vlvh" in low or "dttx" in low or "vua lam vua hoc" in low or "dao tao tu xa" in low):
        md["chuong_trinh"] = "VLVH_DTTX"
    elif md.get("bac") == "sau_dai_hoc":
        md["chuong_trinh"] = "sau_dai_hoc"

    # --- KHÓA ---
    # RỦI RO: học phí hay ghi "Khóa 2025 về sau / Khóa 2021 về trước" -> đây là KHOẢNG MỞ
    # (2025 trở lên / 2021 trở xuống), không phải năm rời. Lưu năm trần rồi lọc bằng '=' sẽ
    # BỎ SÓT các khóa ở giữa -> BỎ, không gán khoa cho các đoạn có 'về trước/về sau/trở...'.
    # Vẫn giữ khoa khi là năm RÕ RÀNG (vd chuẩn tín chỉ 'khoá 2019', hoặc 'Khóa 2023, 2024').
    def _cac_khoa(s):
        s = _khong_dau(s)                      # bắt cả 'khóa'(dấu trên o) lẫn 'khoá'(trên a)
        nam = []
        for grp in re.findall(r"khoa\s*((?:20\d{2}[^\d]{0,10})+)", s):
            nam += re.findall(r"20\d{2}", grp)
        return nam
    low_all = _khong_dau(sec + " " + text)
    co_khoang_mo = any(w in low_all for w in
                       ("ve truoc", "ve sau", "tro ve", "tro di", "tro xuong", "tro len"))
    if not co_khoang_mo:
        khoa = sorted(set(_cac_khoa(sec) or _cac_khoa(text)))
        if khoa:
            md["khoa"] = khoa[0] if len(khoa) == 1 else khoa

    # --- NGÀNH + MÃ NGÀNH (chỉ với DÒNG NGÀNH thật) ---
    # Dấu hiệu CHẮC CHẮN là dòng ngành của bảng 'chuẩn tín chỉ': có "Mã Khoa:" trong text.
    # KHÔNG dùng chunk_type=='row' làm điều kiện vì lịch học vụ cũng có row + section
    # dạng "Tên (MÃ)" -> dễ nhận nhầm 'Học kỳ ... (HK253)' thành ngành.
    mm = RE_MA_KHOA.search(text)
    if mm:
        mn = RE_NGANH.search(text)
        nganh = mn.group(1).strip().split("\n")[0].strip() if mn else None
        if not nganh:                      # thử từ đuôi section_path "Tên ngành (MÃ)"
            m = RE_SEC_PAREN.match(sec.split(" > ")[-1].strip())
            nganh = m.group(1).strip() if m else None
        md["ma_nganh"] = mm.group(1).strip()
        if nganh:
            md["nganh"] = nganh


def detect_schema(record: dict) -> str:
    keys = set(record.keys())
    if {"content", "heading_path", "type"} <= keys:
        return "A"
    if {"chunk_id", "text", "metadata"} <= keys:
        md = record.get("metadata", {})
        if "source_file" in md or "doc_date" in md:
            return "C"
        return "B"
    return "unknown"


def resolve_registry(path: str) -> dict:
    fname = os.path.basename(path)
    file_stem = os.path.splitext(fname)[0]
    file_slug = slugify(file_stem)
    rel = os.path.relpath(path, INPUT_DIR)
    parts = rel.split(os.sep)
    in_subfolder = len(parts) > 1

    if in_subfolder:
        source = slugify(parts[0])
        # id_prefix dùng TOÀN BỘ đường dẫn tương đối, KHÔNG chỉ tên file.
        # Lý do: nhiều ngành có file TRÙNG TÊN ở thư mục khác nhau, vd
        #   CTDT_CHUNKS/VIỆT PHÁP/Hàng không/PFIEV_CK_HK_2021_chunks.json
        #   CTDT_CHUNKS/VIỆT PHÁP/Vật liệu Polymer và Composite/PFIEV_CK_HK_2021_chunks.json
        # Nếu chỉ lấy tên file -> hai chương trình KHÁC NHAU ra cùng id_prefix -> id trùng.
        id_prefix = slugify(os.path.splitext(rel)[0])
        # doc_title kèm thư mục cha CHỈ khi có thư mục trung gian (vd ngành),
        # để phân biệt hai file trùng tên khi trích nguồn.
        # File nằm ngay trong thư mục nguồn thì giữ nguyên tên, khỏi thêm tiền tố dư.
        parent = parts[-2] if len(parts) > 2 else ""
        title = f"{parent} - {file_stem}" if parent else file_stem
    else:
        source = file_slug
        id_prefix = file_slug
        title = file_stem

    # Remap TOÀN CỤC theo tên file -> source chuẩn (áp cho CẢ file trong thư mục con:
    # lô hỗ trợ SV để trong 1 folder 'bksi_md' -> source='bksi_md' không khớp tool; cần đè
    # về hoc_phi/lich_hoc_vu/quydinh theo từng file). file_stem không đổi dù ở subfolder.
    source = FILE_TO_SOURCE.get(file_stem, source)

    return {
        "source": source,
        "id_prefix": id_prefix,
        "doc_title": nfc(title.replace("_", " ").strip()),
        "doc_date": None,
    }


def assign_collection(schema: str, source: str) -> str:
    if source in SOURCE_TO_COLLECTION:
        return SOURCE_TO_COLLECTION[source]
    return "ctdt" if schema == "B" else "hanh_chinh"


# ----------------------------------------------------------------------------
# CHUẨN HÓA THEO TỪNG SCHEMA (kèm metadata.collection)
# ----------------------------------------------------------------------------
def normalize_A(rec, reg, idx, collection):
    text = rec.get("content", "") or ""
    if STRIP_CONTEXT_PREFIX:
        text = CONTEXT_PREFIX_RE.sub("", text)
    text = clean_text(text)
    extra = {
        "original_id": rec.get("id"),
        "n_tables": rec.get("n_tables"),
        "approx_tokens_src": rec.get("approx_tokens"),
    }
    return {
        "id": f'{reg["id_prefix"]}_{idx:04d}',
        "text": text,
        "metadata": {
            "collection": collection,
            "source": reg["source"],
            "nam_hoc": reg.get("nam_hoc"),
            "doc_title": reg["doc_title"],
            "doc_date": reg["doc_date"],
            "section_path": nfc(rec.get("heading_path") or rec.get("section") or ""),
            "chunk_type": rec.get("type") or "text",
            "n_tokens": count_tokens(text),
            "has_table": bool(rec.get("has_table")),
            "extra": {k: v for k, v in extra.items() if v is not None},
        },
    }


def normalize_B(rec, reg, idx, collection):
    text = clean_text(rec.get("text", "") or "")
    md = rec.get("metadata", {}) or {}
    section = md.get("ten_nhom") or md.get("ten_mon") or reg["doc_title"]
    extra = {"original_id": rec.get("chunk_id")}
    extra.update({k: nfc(v) if isinstance(v, str) else v for k, v in md.items()})
    return {
        "id": f'{reg["id_prefix"]}_{idx:04d}',
        "text": text,
        "metadata": {
            "collection": collection,
            "source": reg["source"],
            "nam_hoc": reg.get("nam_hoc"),
            "doc_title": reg["doc_title"],
            "doc_date": reg["doc_date"],
            "section_path": nfc(str(section)),
            "chunk_type": md.get("loai_chunk") or "text",
            "n_tokens": count_tokens(text),
            "has_table": bool(md.get("has_table", False)),
            "extra": extra,
        },
    }


def normalize_C(rec, reg, idx, collection):
    text = clean_text(rec.get("text", "") or "")
    md = rec.get("metadata", {}) or {}
    doc_date = reg["doc_date"] or iso_date(md.get("doc_date"))
    extra = {
        "original_id": rec.get("chunk_id"),
        "source_file": md.get("source_file"),
        "approx_page": md.get("approx_page"),
        "doc_year": md.get("doc_year"),
        "pipeline": md.get("pipeline"),
    }
    return {
        "id": f'{reg["id_prefix"]}_{idx:04d}',
        "text": text,
        "metadata": {
            "collection": collection,
            "source": reg["source"],
            "nam_hoc": reg.get("nam_hoc"),
            "doc_title": reg["doc_title"],
            "doc_date": doc_date,
            "section_path": nfc(md.get("section_path") or md.get("heading") or ""),
            "chunk_type": "table" if md.get("has_table") else "text",
            "n_tokens": count_tokens(text),
            "has_table": bool(md.get("has_table", False)),
            "extra": {k: v for k, v in extra.items() if v is not None},
        },
    }


NORMALIZERS = {"A": normalize_A, "B": normalize_B, "C": normalize_C}


def clean_chunk(out):
    """Áp 3 bước làm sạch lên 1 chunk đã chuẩn hóa.
       Trả ('keep'|'junk'|'orphan', changed_bool)."""
    changed = False
    if CLEAN_ENABLED:
        new_text, changed = strip_strikethrough(out["text"])
        if changed:
            out["text"] = new_text
    if not out["text"].strip():
        return "junk", changed
    if CLEAN_ENABLED:
        if is_pure_junk(out["text"]):
            return "junk", changed
        if is_orphan_table(out["text"]):
            return "orphan", changed
    if changed:
        out["metadata"]["n_tokens"] = count_tokens(out["text"])
    return "keep", changed


# ----------------------------------------------------------------------------
# CHẠY
# ----------------------------------------------------------------------------
def main():
    files = []
    for root, _, names in os.walk(INPUT_DIR):
        for f in names:
            if f.lower().endswith(".json"):
                files.append(os.path.join(root, f))
    files.sort()

    buckets = {"ctdt": [], "hanh_chinh": []}
    removed = {"pure_junk": [], "orphan_table": []}
    n_strike = 0
    report = []

    for path in files:
        fname = os.path.basename(path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list) or not data:
            report.append((fname, "BỎ QUA (không phải list hoặc rỗng)", 0, "-"))
            continue

        reg = resolve_registry(path)
        reg["nam_hoc"] = extract_nam_hoc(data)      # năm học suy từ nội dung (nếu có)
        schema = detect_schema(data[0])
        if schema not in NORMALIZERS:
            report.append((fname, f"BỎ QUA (schema lạ: {schema})", 0, "-"))
            continue

        collection = assign_collection(schema, reg["source"])
        fn = NORMALIZERS[schema]
        count = 0
        for i, rec in enumerate(data, start=1):
            out = fn(rec, reg, i, collection)
            enrich_hoc_ky(out)          # gắn ma_hoc_ky / hoc_ky / nam_hoc theo chunk
            enrich_phan_loai(out)       # gắn bac / chuong_trinh / khoa / nganh / ma_nganh
            verdict, changed = clean_chunk(out)
            if verdict == "junk":
                removed["pure_junk"].append(out); continue
            if verdict == "orphan":
                removed["orphan_table"].append(out); continue
            if changed:
                n_strike += 1
            buckets[collection].append(out)
            count += 1
        report.append((fname, f"schema {schema} -> source '{reg['source']}'", count, collection))

    # ---- Kiểm tra id trùng (toàn cục) ----
    all_chunks = buckets["ctdt"] + buckets["hanh_chinh"]
    ids = [c["id"] for c in all_chunks]
    if len(ids) != len(set(ids)):
        from collections import Counter
        dups = [k for k, v in Counter(ids).items() if v > 1]
        raise AssertionError(f"LỖI: id bị trùng! ({len(dups)}) ví dụ: {dups[:5]}")
    assert all(c["metadata"]["source"] for c in all_chunks), "LỖI: thiếu source!"
    assert all(c["text"].strip() for c in all_chunks), "LỖI: text rỗng!"

    # ---- Ghi file ----
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for coll, path in OUTPUT_FILES.items():
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(buckets[coll], fh, ensure_ascii=False, indent=2)
    if SAVE_REMOVED:
        with open(REMOVED_FILE, "w", encoding="utf-8") as fh:
            json.dump(removed, fh, ensure_ascii=False, indent=2)

    # ---- Báo cáo ----
    n_removed = len(removed["pure_junk"]) + len(removed["orphan_table"])
    print("=" * 74)
    print("CHUẨN HÓA + TÁCH 2 COLLECTION + LÀM SẠCH")
    print("=" * 74)
    for fname, note, n, coll in report:
        print(f"  [{n:>5}] ({coll:<10}) {fname}  |  {note}")
    print("-" * 74)
    print(f"  Làm sạch: bỏ rác {len(removed['pure_junk'])} | bỏ mảnh bảng mồ côi "
          f"{len(removed['orphan_table'])} | xử lý strikethrough {n_strike}")
    print("-" * 74)
    for coll, path in OUTPUT_FILES.items():
        chunks = buckets[coll]
        srcs = sorted(set(c["metadata"]["source"] for c in chunks))
        print(f"  {coll:<12}: {len(chunks):>6} chunk | {len(srcs)} nguồn -> {path}")
        for s in srcs:
            print(f"                - {s}")
    print("-" * 74)
    print(f"  Tổng giữ lại: {len(all_chunks)} | id duy nhất: {len(set(ids))} | đã bỏ: {n_removed}")
    if SAVE_REMOVED:
        print(f"  Bản bị bỏ: {REMOVED_FILE}")
    print("=" * 74)


if __name__ == "__main__":
    main()