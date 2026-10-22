"""
BUILD CTĐT JSON TỪ PDF — HỢP NHẤT 3 FORMAT (ĐH Bách Khoa)
=========================================================
 
Tự nhận diện định dạng PDF rồi chọn parser phù hợp, xuất cùng một schema JSON:
 
  Format A — "Cấu trúc CTĐT", 6 cột CÓ tiên quyết, nhóm đánh số "1. Khối..." +
             tên nhóm tiếng Việt + "chọn N tín chỉ", honors 'x', mỗi môn 1 dòng.
             (VD: 2022_DD_DVT — Điện tử Viễn thông 2022)
 
  Format B — 5 cột, KHÔNG có cột tiên quyết, có cột "Khối kiến thức", nhóm theo
             số La Mã "I./II./III.", chọn-N ghi "(Tối thiểu N TC)".
             (VD: 2019_BD_BDG — Bảo dưỡng Công nghiệp 2019)
 
  Format C — "Cấu trúc CTĐT", 6 cột CÓ tiên quyết, nhóm theo mã "A1./A52./B2./C1."
             + tag [BB]/[TC] + tổng TC ở cột 4, mỗi môn 2 dòng (VN/EN),
             có nhóm cha-con (A5 = A51+A52+A53).
             (VD: 2024_BD_BDG — Bảo dưỡng Công nghiệp 2024, Khoa Cơ khí)
 
Schema xuất (mọi format):
  {nganh, khoa, khoa_vien, ma_ctdt, tong_tin_chi, format_pdf,
   chu_thich_ky_hieu, chu_thich_loai_nhom,
   cau_truc:[{ma_nhom, ten, khoi, loai, [so_tin_chi_yeu_cau],
             mon:[{ma, ten, tin_chi, tien_quyet_song_hanh}]}]}
 
loai nhóm: bat_buoc | chon_tin_chi | tu_do | chung_chi
 
Cách dùng:
    python build_ctdt.py <file.pdf> [output.json]
"""
 
import re
import sys
import json
import unicodedata
from pathlib import Path
 
import pdfplumber
 
 
# --------------------------------------------------------------------------- #
# Regex & hằng dùng chung
# --------------------------------------------------------------------------- #
MA_MON_FULL = re.compile(r"^[A-Z]{2}\d{4}$")
NUM_LINE = re.compile(r"^(?:(\d+(?:\.\d+)?)\s+)?([A-Z]{2}\d{4})\s+(\d+)\b(.*)$")
PREREQ = re.compile(r"([A-Z]{2}\d{4})\((TQ|SHT|SH|KN)\)")
CHON_TIN_CHI = re.compile(r"chọn\s+(\d+)\s+tín chỉ", re.IGNORECASE)
TOI_THIEU = re.compile(r"Tối thiểu\s+(\d+)\s*TC", re.IGNORECASE)
TIN_CHI_TU_DO = re.compile(r"(\d+)\s+tín chỉ", re.IGNORECASE)
KHOI_LINE = re.compile(r"^\d+\.\s+\S")                      # "1. Khối..."  (format A)
ROMAN_HEAD = re.compile(r"^(I|II|III|IV|V|VI)(\.\d+)?\.\s", re.IGNORECASE)  # format B
HONORS_X = re.compile(r"(?<![A-Za-z])x(?![A-Za-z])")
 
CHU_THICH_LOAI = {
    "TQ": "Tiên quyết (Prerequisite)",
    "SH": "Song hành (Co-requisite)",
    "KN": "Khuyến nghị (Recommended)",
}
 
CHU_THICH_KY_HIEU = {
    "TQ": "Tiên quyết (Prerequisite)",
    "SH": "Song hành (Co-requisite)",
    "KN": "Khuyến nghị (Recommended)",
}
CHU_THICH_LOAI_NHOM = {
    "bat_buoc": "Phải học hết mọi môn trong nhóm",
    "chon_tin_chi": "Chỉ cần tích luỹ đủ 'so_tin_chi_yeu_cau'; các môn còn lại tuỳ chọn",
    "tu_do": "Chọn môn bất kỳ cho đủ 'so_tin_chi_yeu_cau'",
    "chung_chi": "Điều kiện xét tốt nghiệp, KHÔNG tính vào tổng tín chỉ tích luỹ",
}
 
 
# --------------------------------------------------------------------------- #
# Tiện ích dùng chung
# --------------------------------------------------------------------------- #
def bo_dau(s: str) -> str:
    s = s.replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")
 
 
def co_tieng_viet(s: str) -> bool:
    return any(ord(c) > 127 for c in s)
 
 
def initials(name: str) -> str:
    return "".join(bo_dau(w[0]).upper() for w in name.split() if w[:1].isalpha())
 
 
def vn_part(cell: str) -> str:
    """Ô song ngữ 'Tiếng Việt\nEnglish' -> lấy phần tiếng Việt (dòng đầu có dấu)."""
    for l in (cell or "").split("\n"):
        if l.strip():
            return l.strip()
    return ""
 
 
def vn_only(cell: str) -> str:
    """Trả về dòng tiếng Việt (ưu tiên dòng có ký tự phi-ASCII)."""
    lines = [l.strip() for l in (cell or "").split("\n") if l.strip()]
    for l in lines:
        if co_tieng_viet(l):
            return l
    return lines[0] if lines else ""
 
 
def strip_en_paren(s: str) -> str:
    s = s.strip()
    m = re.search(r"\s*\(([^()]*)\)\s*$", s)
    if m and not co_tieng_viet(m.group(1)):
        return s[:m.start()].strip()
    return s
 
 
def parse_prereq(text: str):
    """Trả về danh sách quan hệ. Ký hiệu 'SHT' (bản 2024) được quy về 'SH'."""
    ra = []
    for ma, loai in PREREQ.findall(text or ""):
        loai = "SH" if loai == "SHT" else loai
        ra.append({"ma": ma, "loai": loai, "loai_mo_ta": CHU_THICH_LOAI[loai]})
    return ra
 
 
def phan_tich_dong_so(line: str):
    m = NUM_LINE.match(line.strip())
    if not m:
        return None
    stt, ma, tc, rest = m.groups()
    return stt, ma, int(tc), parse_prereq(rest), bool(HONORS_X.search(rest))
 
 
def mon_moi(ma, ten, tc, prereq=None, honors=False):
    return {"ma": ma, "ten": ten, "tin_chi": tc,
            "tien_quyet_song_hanh": prereq or []}
 
# --- BỘ CHUẨN HOÁ XÔ LỆCH BẢNG ---
def _chuan_hoa_dong(cells, fmt=None):
    """Xử lý lỗi xô lệch cột (vỡ bảng) áp dụng chung cho mọi format."""
    if not any(cells):
        return cells
        
    # 1. Mã môn học văng ra cột 0 (do mất cột STT) -> đẩy lùi vào cột 1
    if MA_MON_FULL.match(cells[0].strip()):
        cells.insert(0, "")
        
    # 2. Tiêu đề văng sang cột 1 (do ô STT rỗng) -> kéo về cột 0
    elif not cells[0].strip() and len(cells) > 1 and cells[1].strip():
        c1 = cells[1].replace("\n", " ").strip()
        if fmt == "B" and ROMAN_HEAD.match(c1):
            cells.pop(0)
        elif fmt == "C" and re.match(r"^([A-Z][A-Z0-9_]*)\.(?:\s|$)", c1):
            cells.pop(0)
        elif fmt in ("P1", "P2", "P3") and (HK_P3.search(c1) or re.match(r"^Các\s", c1, re.I)):
            cells.pop(0)
            
    # Đảm bảo mảng luôn có tối thiểu 6 phần tử để truy xuất c0-c5 an toàn
    while len(cells) < 6:
        cells.append("")
        
    return cells

# --------------------------------------------------------------------------- #
# Metadata (dùng chung cả 3 format)
# --------------------------------------------------------------------------- #
def doc_metadata(pdf):
    txt = pdf.pages[0].extract_text() or ""
    md = {"nganh": "", "khoa": None, "tong_tin_chi": None, "khoa_vien": ""}
    m = re.search(r"Ngành:\s*(.+?)\s*-\s*([\d.]+)\s*Tín chỉ", txt)
    if m:
        md["nganh"] = m.group(1).strip()
        md["tong_tin_chi"] = int(round(float(m.group(2))))
    else:
        m = re.search(r"Ngành:\s*([^\n]+)", txt)
        if m:
            ten = m.group(1).strip()
            ten = re.sub(r"\s*\([^()]*\)\s*$", "", ten).strip()
            md["nganh"] = ten
        m = re.search(r"Tổng số tín chỉ:\s*([\d.]+)", txt)
        if m:
            md["tong_tin_chi"] = int(round(float(m.group(1))))
    m = re.search(r"KH[ÓO]A\s+(\d{4})", txt) or re.search(r"YEAR\s+(\d{4})", txt)
    if m:
        md["khoa"] = int(m.group(1))
    m = (re.search(r"KHOA:\s*(.+)", txt)
         or re.search(r"(TRUNG TÂM[^\n]+)", txt))
    if m:
        md["khoa_vien"] = re.sub(r"\s*Ngày\s+[\d/]+\s*$", "", m.group(1).strip()).strip()
    return md
 
 
def dong_goi(md, cau_truc, fmt):
    nganh = md["nganh"] or "Không rõ"
    khoa = md["khoa"]
    ma_ctdt = f"{initials(nganh)}_{khoa}" if khoa else initials(nganh)
    return {
        "loai_tai_lieu": "ctdt_cau_truc",
        "nganh": nganh,
        "khoa": khoa,
        "khoa_vien": md["khoa_vien"],
        "ma_ctdt": ma_ctdt,
        "ma_ctdt_ghi_chu": "Mã tự sinh từ tên ngành + khoá (PDF không chứa mã CTĐT chính thức).",
        "tong_tin_chi": md["tong_tin_chi"],
        "format_pdf": fmt,
        "chu_thich_ky_hieu": CHU_THICH_KY_HIEU,
        "chu_thich_loai_nhom": CHU_THICH_LOAI_NHOM,
        "chu_thich_truong_nhom": {
            "tong_tin_chi_nhom": "Tổng tín chỉ nhóm đóng góp vào CTĐT "
                                 "(bắt buộc: tổng TC các môn; chọn/tự do: số TC yêu cầu). "
                                 "Cộng mọi nhóm (trừ chung_chi) = tổng tín chỉ toàn CTĐT.",
            "so_tin_chi_yeu_cau": "Số TC tối thiểu phải tích luỹ (chỉ nhóm chọn_tin_chi/tu_do).",
            "tin_chi_kha_dung": "Tổng TC của toàn bộ môn được phép chọn trong nhóm "
                                "(chỉ nhóm chọn_tin_chi/tu_do; thường lớn hơn số yêu cầu).",
        },
        "cau_truc": cau_truc,
    }
 
 
# --------------------------------------------------------------------------- #
# NHẬN DIỆN FORMAT
# --------------------------------------------------------------------------- #
def detect_format(pdf) -> str:
    header_cells = []
    group_texts = []
    for page in pdf.pages:
        for tb in page.extract_tables():
            for r in tb[:2]:
                header_cells += [(c or "") for c in r]
            for r in tb:
                c0 = (r[0] or "").replace("\n", " ")
                if c0.strip():
                    group_texts.append(c0)
    head_join = " ".join(header_cells)
 
    if ("Khối kiến thức" in head_join or "Subject Group" in head_join) \
            and "tiên quyết" not in head_join and "Prerequisite" not in head_join:
        return "B"
    if any(("[BB]" in g or "[TC]" in g) for g in group_texts):
        return "C"
    return "A"
 
 
# =========================================================================== #
# FORMAT A  
# =========================================================================== #
class _BuilderA:
    def __init__(self):
        self.nhom = []; self.cur = None; self.cur_khoi = ""; self.canh_bao = []
 
    def _slug(self, ten, khoi):
        base = re.sub(r"[^a-z0-9]+", "_", bo_dau(f"{khoi}_{ten}").lower()).strip("_")
        return base[:60] or f"nhom_{len(self.nhom)}"
 
    def _new_group(self, ten, khoi, loai="bat_buoc", so_tin=None):
        if self.cur and self.cur["loai"] in ("bat_buoc", "chon_tin_chi") and not self.cur["mon"]:
            if self.nhom and self.nhom[-1] is self.cur:
                self.nhom.pop()
        g = {"ma_nhom": self._slug(ten, khoi), "ten": ten, "khoi": khoi, "loai": loai, "mon": []}
        if so_tin is not None:
            g["so_tin_chi_yeu_cau"] = so_tin
        self.nhom.append(g); self.cur = g; return g
 
    def header_line(self, line):
        line = line.strip()
        if not line:
            return
        if re.search(r"^(?:\d+(?:\.\d+)?\s+)?([A-Z]{2}\d{4})", line):
            return
        if KHOI_LINE.match(line):
            ten = strip_en_paren(re.sub(r"^\d+\.\s*", "", line))
            if "Tự chọn tự do" in ten:
                so = TIN_CHI_TU_DO.search(ten)
                self._new_group("Tự chọn tự do", ten, "tu_do", int(so.group(1)) if so else 0)
            elif "Chứng chỉ" in ten or "Certification" in line:
                self._new_group("Chứng chỉ", "Chứng chỉ", "chung_chi")
            else:
                self.cur_khoi = ten
            return
        if not co_tieng_viet(line):
            return
        so = CHON_TIN_CHI.search(line); ten = strip_en_paren(line)
        self._new_group(ten, self.cur_khoi, "chon_tin_chi" if so else "bat_buoc",
                        int(so.group(1)) if so else None)
 
    def them_mon(self, ma, ten, tc, prereq, honors):
        if self.cur is None:
            self._new_group("(Chưa phân nhóm)", self.cur_khoi or "", "bat_buoc")
        m = mon_moi(ma, ten, tc, prereq, honors); self.cur["mon"].append(m)
 
    def them_cert(self, ten):
        if self.cur is None or self.cur["loai"] != "chung_chi":
            self._new_group("Chứng chỉ", "Chứng chỉ", "chung_chi")
        self.cur["mon"].append(mon_moi(initials(ten), ten, 0))
 
 
def _rowA(cells, b: _BuilderA):
    text_col_early = " ".join(cells[:2])
    if "Tốt nghiệp" in text_col_early and re.search(r"[A-Z]{2}\d{4}", text_col_early):
        b.header_line("Tốt nghiệp")
        ma_mon = re.search(r"([A-Z]{2}\d{4})", text_col_early).group(1)
        ten_mon = ""
        if cells[2]:
            ten_lines = [l.strip() for l in cells[2].split("\n") if co_tieng_viet(l)]
            if ten_lines:
                ten_mon = ten_lines[0]
        tc = int(cells[3]) if cells[3].isdigit() else 0
        prereq = parse_prereq(cells[4])
        honors = bool(HONORS_X.search(cells[5]))
        b.them_mon(ma_mon, ten_mon, tc, prereq, honors)
        return
        
    if MA_MON_FULL.match(cells[1]) and cells[3].isdigit():
        b.them_mon(cells[1], vn_part(cells[2]), int(cells[3]),
                   parse_prereq(cells[4]),
                   bool(HONORS_X.search(cells[5])))
        return
        
    if b.cur and b.cur["loai"] == "chung_chi" and cells[2]:
        b.them_cert(vn_part(cells[2])); return
        
    big = max(cells, key=len)
    lines = [l.strip() for l in big.split("\n") if l.strip()]
    k = next((i for i, l in enumerate(lines) if NUM_LINE.match(l)), None)
    if k is None:
        for l in lines:
            if KHOI_LINE.match(l) or (co_tieng_viet(l)
                    and re.search(r"[()]|Nhóm|Ngoại ngữ|Tốt nghiệp", l)):
                b.header_line(l)
            elif b.cur and b.cur["loai"] == "chung_chi" and co_tieng_viet(l):
                b.them_cert(l)
        return
    for l in lines[:max(0, k - 1)]:
        b.header_line(l)
    ten = lines[k - 1] if k >= 1 and co_tieng_viet(lines[k - 1]) else ""
    parsed = phan_tich_dong_so(lines[k])
    if parsed:
        _, ma, tc, prereq, honors = parsed
        b.them_mon(ma, ten, tc, prereq, honors)
 
 
def _coord_rows(page, ytol=3):
    ws = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    ws.sort(key=lambda w: (round(w["top"] / ytol), w["x0"]))
    out, cur, ct = [], [], None
    for w in ws:
        if ct is None or abs(w["top"] - ct) <= ytol:
            cur.append(w); ct = w["top"] if ct is None else ct
        else:
            out.append(" ".join(x["text"] for x in sorted(cur, key=lambda z: z["x0"]))); cur, ct = [w], w["top"]
    if cur:
        out.append(" ".join(x["text"] for x in sorted(cur, key=lambda z: z["x0"])))
    return out
 
 
def _bu_hang_roiA(pdf, b: _BuilderA):
    order = []
    for page in pdf.pages:
        rows = _coord_rows(page)
        for i, line in enumerate(rows):
            p = phan_tich_dong_so(line)
            if p:
                _, ma, tc, prereq, honors = p
                ten = rows[i - 1].strip() if i > 0 and co_tieng_viet(rows[i - 1]) else ""
                if i + 1 < len(rows) and PREREQ.search(rows[i + 1]) and not NUM_LINE.match(rows[i + 1]):
                    prereq = prereq + parse_prereq(rows[i + 1])
                order.append({"ma": ma, "ten": ten, "tc": tc, "prereq": prereq, "honors": honors})
    da_co = {m["ma"] for g in b.nhom for m in g["mon"]}
    ma_to_group = {m["ma"]: g for g in b.nhom for m in g["mon"]}
    for idx, o in enumerate(order):
        if o["ma"] in da_co:
            continue
        g = next((ma_to_group[order[j]["ma"]] for j in range(idx - 1, -1, -1)
                  if order[j]["ma"] in ma_to_group), b.nhom[0] if b.nhom else None)
        if g is None:
            continue
        pos = len(g["mon"])
        for j, m in enumerate(g["mon"]):
            if idx > 0 and m["ma"] == order[idx - 1]["ma"]:
                pos = j + 1; break
        g["mon"].insert(pos, mon_moi(o["ma"], o["ten"], o["tc"], o["prereq"], o["honors"]))
        da_co.add(o["ma"]); ma_to_group[o["ma"]] = g
    info = {}
    for o in order:
        info.setdefault(o["ma"], (o["prereq"], o["honors"], o["tc"]))
    for g in b.nhom:
        for m in g["mon"]:
            if m["ma"] in info:
                pq, hn, tc = info[m["ma"]]
                if not m["tien_quyet_song_hanh"] and pq:
                    m["tien_quyet_song_hanh"] = pq
                if m["tin_chi"] == 0 and tc:
                    m["tin_chi"] = tc
 
 
def parse_format_A(pdf, md):
    b = _BuilderA()
    for page in pdf.pages:
        for tb in page.extract_tables():
            for row in tb:
                cells = [(c or "").strip() for c in row]
                cells = _chuan_hoa_dong(cells, "A")
                if not any(cells):
                    continue
                if cells[0].startswith("STT") or cells[0].startswith("No."):
                    continue
                _rowA(cells, b)
    _bu_hang_roiA(pdf, b)
    nhom = [g for g in b.nhom if g["mon"] or g["loai"] == "tu_do"]
    return nhom, b.canh_bao
 
 
# =========================================================================== #
# FORMAT B  
# =========================================================================== #
def parse_format_B(pdf, md):
    canh_bao = []
    groups = {}          
    order = []           
    cert = None
    che_do_cert = False
 
    def get_group(khoi_vn):
        key = ("bb", khoi_vn)
        if key not in groups:
            g = {"ma_nhom": re.sub(r"[^a-z0-9]+", "_", bo_dau(khoi_vn).lower()).strip("_")[:60] or f"nhom_{len(order)}",
                 "ten": khoi_vn, "khoi": khoi_vn, "loai": "bat_buoc", "mon": []}
            groups[key] = g; order.append(g)
        return groups[key]
 
    DECL = re.compile(r"Group\)\s*([A-Z])\s*\(Tối thiểu\s*(\d+)\s*TC\)\s*\d+\s*(.*)$")
    TUDO = re.compile(r"Tự chọn tự do.*Tối thiểu\s*(\d+)\s*TC")
    elect = {}           
    tu_do_g = None
 
    def them_elect(lt, so, khoi):
        if lt not in elect:
            elect[lt] = {"ma_nhom": f"tu_chon_{lt.lower()}",
                         "ten": f"Nhóm tự chọn {lt}" + (f" ({khoi})" if khoi else ""),
                         "khoi": khoi or f"Nhóm tự chọn {lt}", "loai": "chon_tin_chi",
                         "so_tin_chi_yeu_cau": so, "mon": []}
 
    def dat_tu_do(so):
        nonlocal tu_do_g
        if tu_do_g is None:
            tu_do_g = {"ma_nhom": "tu_chon_tu_do", "ten": "Tự chọn tự do",
                       "khoi": "Tự chọn tự do", "loai": "tu_do",
                       "so_tin_chi_yeu_cau": so, "mon": []}
 
    for page in pdf.pages:
        for line in _coord_rows(page):
            md_ = DECL.search(line)
            if md_:
                them_elect(md_.group(1), int(md_.group(2)), md_.group(3).strip())
            mt = TUDO.search(line)
            if mt:
                dat_tu_do(int(mt.group(1)))
 
    for page in pdf.pages:
        for tb in page.extract_tables():
            for row in tb:
                cells = [(c or "").strip() for c in row]
                cells = _chuan_hoa_dong(cells, "B")
                if not any(cells):
                    continue
                c1, c2, c3, c4 = cells[1], cells[2], cells[3], cells[4]
                if MA_MON_FULL.match(c1) or not c2:
                    continue
                if "Group)" in c2 or "tự chọn nhóm" in c2.lower():
                    ml = re.search(r"Group\)\s*([A-Z])", c2)
                    mreq = TOI_THIEU.search(c2)
                    req = int(mreq.group(1)) if mreq else (int(c3) if c3.isdigit() else None)
                    if ml and req is not None:
                        them_elect(ml.group(1), req, vn_part(c4))
                elif "Tự chọn tự do" in c2 or "Free Elective" in c2:
                    mreq = TOI_THIEU.search(c2)
                    dat_tu_do(int(mreq.group(1)) if mreq else (int(c3) if c3.isdigit() else 0))
 
    dinh_kem_tu_chon = False   
    for page in pdf.pages:
        for tb in page.extract_tables():
            for row in tb:
                cells = [(c or "").strip() for c in row]
                cells = _chuan_hoa_dong(cells, "B")
                if not any(cells):
                    continue
                if cells[0].startswith("STT") or cells[0].startswith("No."):
                    continue
                c0, c1, c2, c3, c4 = cells[0], cells[1], cells[2], cells[3], cells[4]
 
                if ROMAN_HEAD.match(c0):
                    if not dinh_kem_tu_chon and ("tự chọn" in c0.lower() or "Elective" in c0):
                        if tu_do_g:
                            order.append(tu_do_g)
                        for lt in sorted(elect):
                            order.append(elect[lt])
                        dinh_kem_tu_chon = True
                    che_do_cert = ("Chứng chỉ" in c0 or "Certification" in c0)
                    if che_do_cert:
                        cert = {"ma_nhom": "chung_chi", "ten": "Chứng chỉ",
                                "khoi": "Chứng chỉ", "loai": "chung_chi", "mon": []}
                        order.append(cert)
                    continue
 
                if che_do_cert and cert is not None:
                    if MA_MON_FULL.match(c1):
                        cert["mon"].append(mon_moi(c1, vn_part(c2), 0)); continue
                    if c2:
                        ten = vn_part(c2)
                        cert["mon"].append(mon_moi(initials(ten), ten, 0)); continue
                    continue
 
                if MA_MON_FULL.match(c1) and c3.isdigit():
                    khoi_vn = vn_part(c4)
                    if not khoi_vn:
                        continue
                    get_group(khoi_vn)["mon"].append(mon_moi(c1, vn_part(c2), int(c3)))
                    continue
 
    if not dinh_kem_tu_chon:
        if tu_do_g:
            order.append(tu_do_g)
        for lt in sorted(elect):
            order.append(elect[lt])
 
    da_co = {m["ma"] for g in order for m in g["mon"]}
 
    SECT = re.compile(r"Group\)\s*([A-Z])\s*$")
    MEM = re.compile(r"^\d+\s+([A-Z]{2}\d{4})\s+(\D+?)\s*(\d)(?:\D|$)")
    cur_lt = None
    for page in pdf.pages:
        for line in _coord_rows(page):
            if ROMAN_HEAD.match(line):
                cur_lt = None
            ms = SECT.search(line)
            if ms and "Tối thiểu" not in line:
                cur_lt = ms.group(1)
                sau = line[ms.end():].strip()
                mm = MEM.match(sau)
                if mm and cur_lt in elect and mm.group(1) not in da_co:
                    elect[cur_lt]["mon"].append(mon_moi(mm.group(1), mm.group(2).strip(), int(mm.group(3))))
                    da_co.add(mm.group(1))
                continue
            if cur_lt and cur_lt in elect:
                mm = MEM.match(line)
                if mm and mm.group(1) not in da_co:
                    elect[cur_lt]["mon"].append(mon_moi(mm.group(1), mm.group(2).strip(), int(mm.group(3))))
                    da_co.add(mm.group(1))
 
    KHOI_KW = (r"(?:Toán|Chính trị|Nhập môn|Con người|Ngoại ngữ|Cơ sở|Chuyên ngành|"
               r"Chuyên môn|Tốt nghiệp|Quản lý|Kiến thức|Đại cương|Tự chọn)")
    
    LINE_FULL = re.compile(r"^(?:\d+\s+)?([A-Z]{2}\d{4})\s+(.+?)\s+(\d{1,2})\s+(" + KHOI_KW + r"[^\n]*)$")
    LINE_CERT = re.compile(r"^(?:\d+\s+)?((?:MI|PE)\d{4})\s+(.+)$")
    for page in pdf.pages:
        for line in _coord_rows(page):
            mfull = LINE_FULL.match(line)
            if mfull and mfull.group(1) not in da_co:
                ma, ten, tc, khoi_vn = mfull.group(1), mfull.group(2).strip(), int(mfull.group(3)), mfull.group(4).strip()
                g = get_group(khoi_vn)
                g["mon"].append(mon_moi(ma, ten, tc)); da_co.add(ma)
                canh_bao.append(f"Bù hàng rơi (B): {ma} '{ten}' ({tc} TC) -> '{g['ten']}'")
                continue
            mcert = LINE_CERT.match(line)
            if mcert and mcert.group(1) not in da_co and cert is not None:
                ma, ten = mcert.group(1), mcert.group(2).strip()
                cert["mon"].append(mon_moi(ma, ten, 0)); da_co.add(ma)
                canh_bao.append(f"Bù chứng chỉ (B): {ma} '{ten}' -> 'Chứng chỉ'")
 
    nhom = [g for g in order if g["mon"] or g["loai"] in ("tu_do", "chon_tin_chi")]
    return nhom, canh_bao
 
 
 
# =========================================================================== #
# FORMAT C  
# =========================================================================== #
def parse_format_C(pdf, md):
    canh_bao = []
    order = []
    cur = None
 
    for page in pdf.pages:
        for tb in page.extract_tables():
            for row in tb:
                cells = [(c or "").strip() for c in row]
                cells = _chuan_hoa_dong(cells, "C")
                if not any(cells):
                    continue
                c0, c1, c2, c3, c4, c5 = cells[0].replace("\n", " "), cells[1], cells[2], cells[3], cells[4], cells[5]
 
                if c0.startswith("STT") or c0.startswith("No."):
                    continue
 
                mh = re.match(r"^([A-Z][A-Z0-9_]*)\.(?:\s|$)", c0)
                if mh and not c0[:mh.end()].startswith(("No", "STT")):
                    ma_nhom = mh.group(1)
                    rest = c0[mh.end():].strip() or vn_only(c1)   
                    has_tc = "[TC]" in c0 or "[TC]" in c1
                    ten = re.sub(r"\[(BB|TC)\]", "", rest).split("(")[0].strip() or ma_nhom
                    tong = int(c3) if c3.isdigit() else None
                    loai = ("tu_do" if ("Tự chọn tự do" in rest or "Free Elective" in rest)
                            else "chon_tin_chi") if has_tc else "bat_buoc"
                    g = {"ma_nhom": ma_nhom, "ten": f"{ma_nhom}. {ten}",
                         "khoi": ten, "loai": loai, "mon": []}
                    if loai in ("tu_do", "chon_tin_chi") and tong is not None:
                        g["so_tin_chi_yeu_cau"] = tong
                    g["_tong_pdf"] = tong
                    order.append(g); cur = g
                    continue
 
                if MA_MON_FULL.match(c1):
                    tc = int(c3) if c3.isdigit() else 0
                    ten = vn_only(c2)
                    prereq = parse_prereq(c4)
                    honors = bool(HONORS_X.search(c5))
                    if cur is None:
                        cur = {"ma_nhom": "khac", "ten": "Khác", "khoi": "Khác",
                               "loai": "bat_buoc", "mon": [], "_tong_pdf": None}
                        order.append(cur)
                    cur["mon"].append(mon_moi(c1, ten, tc, prereq, honors))
                    continue
 
    for g in order:
        if g["loai"] == "bat_buoc" and g["mon"] and all(m["tin_chi"] == 0 for m in g["mon"]):
            g["loai"] = "chung_chi"
 
    for g in order:
        ten_sau = g["ten"].split(". ", 1)[-1]
        if not re.match(r"^[A-ZĐ]", ten_sau) or ten_sau.startswith("mục"):
            if g["mon"] and all(m["ma"].startswith("PE") for m in g["mon"]):
                hp = "2" if any("phần 2" in m["ten"].lower() for m in g["mon"]) else "1"
                g["ten"] = f"{g['ma_nhom']}. Giáo dục thể chất (Học phần {hp})"
                g["khoi"] = "Giáo dục thể chất"
            else:
                g["ten"] = f"{g['ma_nhom']}. (Nhóm {g['ma_nhom']})"
 
    nhom = []
    for g in order:
        if not g["mon"] and g["loai"] == "bat_buoc":
            if g.get("_tong_pdf"):
                canh_bao.append(f"Bỏ nhóm cha/điều kiện rỗng: {g['ten']} "
                                f"(tổng {g['_tong_pdf']} TC = tổng các nhóm con).")
            continue
        nhom.append(g)   
    return nhom, canh_bao
 
 
# =========================================================================== #
# KHGD — KẾ HOẠCH GIẢNG DẠY THEO HỌC KỲ (3 dạng P1 / P2 / P3)
# =========================================================================== #
KHGD_TITLE = re.compile(r"KẾ HOẠCH GIẢNG DẠY[^\n]*?KH[ÓO]A\s*(\d{4})\s*(\([^)]*\))?", re.I)

# Cập nhật: Chấp nhận cả "kỳ" / "kì" và số La Mã (I, II, III...)
HK_P1 = re.compile(r"^Học k[ỳì]\s+([IVX\d]+)\s*(\([^)]*\))?\s*([\d.]*)$", re.I)
HK_P2 = re.compile(r"^Học k[ỳì]\s+([IVX\d]+)\s*\(\s*Semester[^)]*\)\s*([\d.]*)", re.I)
# Có bản đánh số học kỳ bằng mã: "KHGD.VL.2027.1" (Khoa Công nghệ Vật liệu)
HK_MA_P3 = re.compile(r"^KHGD(?:\.[A-Za-z0-9]+)*\.(\d{1,2})\s*$", re.I)
HK_P3 = re.compile(r"(?:Kế hoạch giảng dạy.*?|Học kì.*?|Học kỳ.*?)?(?:Học k[ỳì]|HK)\s*([IVX\d]+)", re.I)

SO_TIN_TEN = re.compile(r"(\d+)\s*tín chỉ", re.I)
STT_MON = re.compile(r"^\d+(?:\.\d+)?$")
 
def parse_hk_number(val: str) -> int:
    """Chuyển đổi chuỗi học kỳ (Ả Rập hoặc La Mã) sang số nguyên."""
    val = val.upper().strip()
    roman = {'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5, 
             'VI': 6, 'VII': 7, 'VIII': 8, 'IX': 9, 'X': 10}
    
    if val in roman:
        return roman[val]
    if val.isdigit():
        return int(val)
    return 0

def detect_loai_tai_lieu(pdf) -> str:
    txt = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])
    if re.search(r"Cấu trúc CTĐT", txt, re.I):
        return "ctdt"
    if re.search(r"KẾ HOẠCH GIẢNG DẠY|Kế hoạch giảng dạy theo học kỳ", txt, re.I):
        return "khgd"
    return "ctdt"
 
 
def detect_format_khgd(pdf) -> str:
    heads, c0s = [], []
    for page in pdf.pages:
        for tb in page.extract_tables():
            for r in tb[:2]:
                heads += [(c or "") for c in r]
            for r in tb:
                c0s.append((r[0] or "").replace("\n", " ").strip())
    if "MSMH" in " ".join(heads):
        return "P1"
    if any(HK_P2.match(c) for c in c0s):
        return "P2"
    return "P3"
 
 
def _slug_nhom(ten, hk):
    s = re.sub(r"[^a-z0-9]+", "_", bo_dau(ten).lower()).strip("_")[:40] or "nhom"
    return f"hk{hk}_{s}"
 
 
def _loai_tu_ten(ten):
    low = bo_dau(ten).lower()
    if "tu chon tu do" in low or "free elective" in low:
        return "tu_do"
    if "tu chon" in low or "elective" in low:
        return "chon_tin_chi"
    return "bat_buoc"
 
 
def _nhom_moi(ten, hk, so_tin=None, loai=None):
    ten = strip_en_paren(re.sub(r"\s+", " ", ten).strip())
    loai = loai or _loai_tu_ten(ten)
    g = {"ma_nhom": _slug_nhom(ten, hk), "ten": ten, "loai": loai, "mon": []}
    if loai in ("tu_do", "chon_tin_chi") and so_tin is not None:
        g["so_tin_chi_yeu_cau"] = so_tin
    return g
 
 
def _so_tin_tu_ten(ten):
    m = SO_TIN_TEN.search(ten or "")
    return int(m.group(1)) if m else None
 
 
def _mon_khgd(ma, ten, tc, prereq=None):
    m = mon_moi(ma or "", vn_only(ten), tc, prereq)
    return m
 
 
# --------------------------------------------------------------------------- #
# Dòng môn trong text-layer (neo theo mã học phần nên chịu được chữ watermark
# dính vào đầu dòng). Phần tên tiếng Anh phía sau là tuỳ chọn vì có bản xuống dòng.
MON_TXT_P1 = re.compile(r"([A-Z]{2}\d{4})\s+(.+?)\s+(\d+)\s*(?:[A-Za-z].*)?$")


def _gon(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _co_dau_tv(s: str) -> bool:
    """True nếu chuỗi thực sự có dấu tiếng Việt.

    co_tieng_viet() chỉ xét ký tự ngoài ASCII nên dính cả dòng tiếng Anh có gạch
    ngang dài (VD "Elective Courses – Group B") -> dùng hàm này cho tên nhóm.
    """
    s = s or ""
    return bo_dau(s) != s


def _khoa_hk(dong):
    """Khoá định danh học kỳ từ 1 dòng tiêu đề: (số, có phải học kỳ hè)."""
    m = HK_P1.match(_gon(dong))
    if not m:
        return None
    return (parse_hk_number(m.group(1)), "hè" in (m.group(2) or "").lower())


def _tieu_de_dau_trang_bi_sot(page, tb):
    """Tiêu đề ở ĐẦU TRANG bị extract_tables() bỏ sót.

    Gặp ở bản 2021_CK_COD: sang trang mới, bảng bắt đầu thẳng bằng hàng môn,
    còn 'Học kỳ 6 16' + 'Các môn bắt buộc' chỉ còn trong text-layer -> nếu không
    bù thì toàn bộ môn của học kỳ đó bị gán nhầm vào học kỳ trước.
    """
    if not tb:
        return []
    hang_dau = _gon(" ".join((c or "") for c in tb[0]))
    ra = []
    for ln in (page.extract_text() or "").split("\n"):
        ln = _gon(ln)
        if not ln:
            continue
        if ln.startswith("STT") or ln.startswith("No."):
            continue
        if HK_P1.match(ln) or re.match(r"^Các môn|^Các học phần|^Nhóm môn học", ln):
            if ln not in hang_dau:
                ra.append(ln)
            continue
        break            # gặp dòng dữ liệu đầu tiên -> dừng
    return ra


def _bu_mon_roi_P1(pdf, progs, canh_bao):
    """Bù môn bị extract_tables() làm rơi ở chỗ ngắt trang (VD TR1005 của PFIEV)."""
    if not progs:
        return
    # tập mã đã có, tách theo từng học kỳ (môn tự chọn được lặp ở nhiều học kỳ)
    da_co = {}
    for pi, p in enumerate(progs):
        for hk in p["hoc_ky"]:
            k = _khoa_hk(hk.get("ten_hoc_ky", "")) or (hk["so_hoc_ky"], False)
            da_co[(pi, k)] = {m["ma"] for g in hk["nhom"] for m in g["mon"]}

    idx, cur_prog, cur_hk, cur_ten_nhom, cur_title = -1, None, None, None, None
    for page in pdf.pages:
        mt = KHGD_TITLE.search(page.extract_text() or "")
        if mt and mt.group(0) != cur_title:
            cur_title = mt.group(0); idx += 1
            cur_prog = progs[min(idx, len(progs) - 1)]
            cur_hk, cur_ten_nhom = None, None
        if cur_prog is None:
            idx, cur_prog = 0, progs[0]

        for ln in (page.extract_text() or "").split("\n"):
            ln = _gon(ln)
            if not ln:
                continue
            k = _khoa_hk(ln)
            if k:
                cur_hk = next(
                    (h for h in cur_prog["hoc_ky"]
                     if (_khoa_hk(h.get("ten_hoc_ky", "")) or (h["so_hoc_ky"], False)) == k),
                    None)
                cur_ten_nhom = None
                continue
            if re.match(r"^Các môn|^Các học phần", ln):
                cur_ten_nhom = ln
                continue
            if cur_hk is None:
                continue

            mm = MON_TXT_P1.search(ln)
            if not mm:
                continue
            ma, ten, tc = mm.group(1), _gon(mm.group(2)), int(mm.group(3))
            khoa = (progs.index(cur_prog),
                    _khoa_hk(cur_hk.get("ten_hoc_ky", "")) or (cur_hk["so_hoc_ky"], False))
            if ma in da_co.get(khoa, set()):
                continue
            # chọn nhóm khớp tên, không có thì lấy nhóm cuối / tạo nhóm bắt buộc
            g = None
            if cur_ten_nhom:
                muc = bo_dau(strip_en_paren(cur_ten_nhom)).lower()[:20]
                g = next((x for x in cur_hk["nhom"]
                          if bo_dau(x["ten"]).lower()[:20] == muc), None)
            if g is None:
                g = cur_hk["nhom"][-1] if cur_hk["nhom"] else None
            if g is None:
                g = _nhom_moi("Các môn bắt buộc", cur_hk["so_hoc_ky"])
                cur_hk["nhom"].append(g)
            g["mon"].append(_mon_khgd(ma, ten, tc))
            da_co.setdefault(khoa, set()).add(ma)
            canh_bao.append(f"Bù môn rơi ngắt trang (P1): {ma} '{ten}' ({tc} TC) "
                            f"-> HK{cur_hk['so_hoc_ky']} / '{g['ten']}'")


# --------------------------------------------------------------------------- #
def parse_khgd_P1(pdf, md):
    canh_bao = []
    progs, cur_prog, cur_hk, cur_g, cur_title = [], None, None, None, None
 
    def mo_chuong_trinh(bien):
        nonlocal cur_prog, cur_hk, cur_g
        cur_prog = {"ten_chuong_trinh": bien, "hoc_ky": []}
        progs.append(cur_prog); cur_hk = None; cur_g = None
 
    for page in pdf.pages:
        mt = KHGD_TITLE.search(page.extract_text() or "")
        if mt and mt.group(0) != cur_title:
            cur_title = mt.group(0)
            bien = (mt.group(2) or "").strip("() ").strip()
            if not bien:
                ten_ct = "Chuẩn"
            elif " " not in bien and len(bien) <= 6:
                ten_ct = bien                      
            else:
                ten_ct = bien[:1].upper() + bien[1:].lower()   
            mo_chuong_trinh(ten_ct)
        for tb_i, tb in enumerate(page.extract_tables()):
            # Bù tiêu đề bị extract_tables() bỏ sót ở đầu trang (VD 2021_CK_COD)
            if tb_i == 0:
                for dong in _tieu_de_dau_trang_bi_sot(page, tb):
                    if cur_prog is None:
                        mo_chuong_trinh("Chuẩn")
                    mh = HK_P1.match(dong)
                    if mh:
                        cur_hk = {"so_hoc_ky": parse_hk_number(mh.group(1)),
                                  "ten_hoc_ky": dong,
                                  "tin_chi_hoc_ky": int(float(mh.group(3) or 0)),
                                  "nhom": []}
                        cur_prog["hoc_ky"].append(cur_hk); cur_g = None
                        canh_bao.append(f"Bù tiêu đề rơi đầu trang (P1): '{dong}'")
                    elif re.match(r"^Các môn|^Các học phần", dong) and cur_hk is not None:
                        cur_g = _nhom_moi(dong, cur_hk["so_hoc_ky"], _so_tin_tu_ten(dong))
                        cur_hk["nhom"].append(cur_g)
            for row in tb:
                cells = [(c or "").strip() for c in row]
                cells = _chuan_hoa_dong(cells, "P1")
                if not any(cells):
                    continue
                c0, c1, c2, c3 = cells[0].replace("\n", " "), cells[1], cells[2], cells[3]

                if c0.startswith("STT") or c0.startswith("No."):
                    continue
                if cur_prog is None:
                    mo_chuong_trinh("Chuẩn")
 
                # Ô cột 0 có thể GỘP nhiều dòng, VD "Học kỳ 1 16\nCác môn bắt buộc"
                # -> phải duyệt từng dòng thay vì so khớp cả ô.
                da_xu_ly = False
                for dong in cells[0].split("\n"):
                    dong = dong.strip()
                    if not dong:
                        continue

                    m = HK_P1.match(dong)
                    if m:
                        # Tín chỉ học kỳ: cùng ô (bản 2019) hoặc ở cột 3 (bản PFIEV)
                        tc = m.group(3) or (c3 if c3.replace(".", "").isdigit() else "0")
                        cur_hk = {"so_hoc_ky": parse_hk_number(m.group(1)),
                                  "ten_hoc_ky": dong,          # giữ cả "Học kỳ 6 (hè)"
                                  "tin_chi_hoc_ky": int(float(tc)), "nhom": []}
                        cur_prog["hoc_ky"].append(cur_hk); cur_g = None
                        da_xu_ly = True
                        continue

                    if re.match(r"^Các môn|^Các học phần", dong):
                        if cur_hk is None:
                            continue
                        cur_g = _nhom_moi(dong, cur_hk["so_hoc_ky"], _so_tin_tu_ten(dong))
                        cur_hk["nhom"].append(cur_g)
                        da_xu_ly = True
                        continue

                    # "Nhóm môn học ..." chỉ là nhãn phụ bên trong nhóm tự chọn
                    # (VD PFIEV) -> KHÔNG tạo nhóm mới, giữ nguyên nhóm đang mở.
                    if re.match(r"^Nhóm môn học", dong):
                        da_xu_ly = True
                        continue
                if da_xu_ly:
                    continue
 
                if MA_MON_FULL.match(c1) and c3.replace(".", "").isdigit():
                    if cur_hk is None:
                        continue
                    if cur_g is None:
                        cur_g = _nhom_moi("Các môn bắt buộc", cur_hk["so_hoc_ky"])
                        cur_hk["nhom"].append(cur_g)
                    cur_g["mon"].append(_mon_khgd(c1, c2, int(float(c3))))
                    continue
                if not c1 and c2 and "Tự chọn tự do" in c2 and cur_g is not None:
                    if cur_g["loai"] in ("tu_do", "chon_tin_chi") and \
                            "so_tin_chi_yeu_cau" not in cur_g and c3.isdigit():
                        cur_g["so_tin_chi_yeu_cau"] = int(c3)
                    continue
    _bu_mon_roi_P1(pdf, progs, canh_bao)
    return progs, canh_bao
 
 
# --------------------------------------------------------------------------- #
def parse_khgd_P2(pdf, md):
    canh_bao = []
    prog = {"ten_chuong_trinh": "Chuẩn", "hoc_ky": []}
    cur_hk, cur_g = None, None
 
    for page in pdf.pages:
        for tb in page.extract_tables():
            for row in tb:
                cells = [(c or "").strip() for c in row]
                cells = _chuan_hoa_dong(cells, "P2")
                if not any(cells):
                    continue
                c0, c1, c2, c3, c4 = cells[0].replace("\n", " "), cells[1], cells[2], cells[3], cells[4]

                if c0.startswith("STT") or c0.startswith("No."):
                    continue
 
                m = HK_P2.match(c0)
                if m:
                    # TC học kỳ: cùng ô (bản 2022) hoặc ở cột 3 (bản 2024 song ngành)
                    tc = m.group(2) or (c3 if c3.replace(".", "").isdigit() else "0")
                    cur_hk = {"so_hoc_ky": parse_hk_number(m.group(1)),
                              "ten_hoc_ky": _gon(c0.split("\n")[0]),
                              "tin_chi_hoc_ky": int(float(tc)), "nhom": []}
                    prog["hoc_ky"].append(cur_hk)
                    cur_g = _nhom_moi("Các học phần bắt buộc", cur_hk["so_hoc_ky"],
                                      loai="bat_buoc")
                    cur_hk["nhom"].append(cur_g)
                    continue
 
                if re.match(r"^Các học phần tự chọn|^Các môn tự chọn", c0):
                    cur_g = None          
                    continue
 
                if not c1 and c2 and co_tieng_viet(vn_only(c2)) and cur_hk is not None \
                        and re.search(r"[Tt]ự chọn", c2):
                    ten = vn_only(c2)
                    cur_g = _nhom_moi(ten, cur_hk["so_hoc_ky"], _so_tin_tu_ten(ten))
                    cur_hk["nhom"].append(cur_g)
                    continue
 
                if STT_MON.match(c0) and c3.replace(".", "").isdigit() and cur_hk is not None:
                    if cur_g is None:
                        cur_g = _nhom_moi("Các học phần bắt buộc", cur_hk["so_hoc_ky"],
                                          loai="bat_buoc")
                        cur_hk["nhom"].append(cur_g)
                    ma = c1 if MA_MON_FULL.match(c1) else initials(vn_only(c2))
                    cur_g["mon"].append(_mon_khgd(ma, c2, int(float(c3)), parse_prereq(c4)))
                    continue
    return [prog], canh_bao
 
 
# --------------------------------------------------------------------------- #
def parse_khgd_P3(pdf, md):
    """6 cột CÓ tiên quyết; 'Học kỳ N' ở cột 0, TC ở cột 3; môn 2 dòng VN/EN."""
    canh_bao = []
    prog = {"ten_chuong_trinh": "Chuẩn", "hoc_ky": []}
    cur_hk, cur_g, tc_muc = None, None, None
    trong_muc_tc = False        # đang ở trong mục "Các học phần tự chọn"?
 
    for page in pdf.pages:
        for tb in page.extract_tables():
            for row in tb:
                cells = [(c or "").strip() for c in row]
                cells = _chuan_hoa_dong(cells, "P3")
                if not any(cells):
                    continue
                c0, c1, c2, c3, c4 = cells[0].replace("\n", " "), cells[1], cells[2], cells[3], cells[4]

                if c0.startswith("STT") or c0.startswith("No."):
                    continue
 
                c0_c1_text = c0 + " " + c1
                la_hang_mon = bool(MA_MON_FULL.match(c1))
                # Có bản đánh số học kỳ bằng mã "KHGD.VL.2027.1" thay cho "Học kỳ 1"
                m = None if la_hang_mon else (HK_MA_P3.match(_gon(c0))
                                              or HK_P3.search(c0_c1_text))
                
                if m:
                    # Lấy số học kỳ (xử lý cả số La Mã)
                    so_hk = parse_hk_number(m.group(1))
                    
                    # Dự phòng tín chỉ nhảy qua c2 hoặc c1 nếu STT trống
                    tc_str = c3 if c3.replace(".", "").isdigit() else (
                             c2 if c2.replace(".", "").isdigit() else (
                             c1 if c1.replace(".", "").isdigit() else "0"))
                    
                    cur_hk = {"so_hoc_ky": so_hk,
                              "tin_chi_hoc_ky": int(float(tc_str)), "nhom": []}
                    prog["hoc_ky"].append(cur_hk)
                    cur_g = None; tc_muc = None; trong_muc_tc = False
                    continue
 
                if re.search(r"Các học phần bắt buộc|Các môn bắt buộc", c0_c1_text, re.I):
                    if cur_hk is None:
                        continue
                    cur_g = _nhom_moi("Các học phần bắt buộc", cur_hk["so_hoc_ky"],
                                      loai="bat_buoc")
                    cur_hk["nhom"].append(cur_g)
                    tc_muc = None; trong_muc_tc = False
                    continue
 
                _muc = _gon(re.sub(r"\([^)]*\)", "", c0_c1_text))
                _muc = re.sub(r"[\d\s]+$", "", _muc).strip().lower()
                if _muc in ("các học phần tự chọn", "các môn tự chọn"):
                    cur_g = None; trong_muc_tc = True
                    tc_str = c3 if c3.isdigit() else (c2 if c2.isdigit() else "")
                    tc_muc = int(tc_str) if tc_str.isdigit() else None   
                    continue
 
                # Tiêu đề nhóm tự chọn có thể bị xô ra c0, c1 hoặc c2
                if not MA_MON_FULL.match(c1) and cur_hk is not None:
                    ten = vn_only(c0) if co_tieng_viet(vn_only(c0)) else (
                          vn_only(c1) if co_tieng_viet(vn_only(c1)) else vn_only(c2))

                    # Nhận nhóm khi: đang trong mục "Các học phần tự chọn" (trong_muc_tc)
                    # HOẶC tên có từ khoá nhóm. Bỏ dòng tiêu đề chung bắt đầu bằng "Các".
                    tu_khoa = re.search(r"tu chon|nhom|giao duc|quan ly|kien thuc|ky nang",
                                        bo_dau(ten).lower())
                    if _co_dau_tv(ten) and (trong_muc_tc or tu_khoa):
                        so = int(c3) if c3.isdigit() else (_so_tin_tu_ten(ten) or tc_muc)
                        # Trong mục tự chọn thì mặc định là nhóm chọn-N, kể cả khi tên
                        # không có chữ "tự chọn" (VD "Kiến thức quản lý/ khởi nghiệp").
                        loai = None
                        if trong_muc_tc and _loai_tu_ten(ten) == "bat_buoc":
                            loai = "chon_tin_chi"
                        cur_g = _nhom_moi(ten, cur_hk["so_hoc_ky"], so, loai=loai)
                        cur_hk["nhom"].append(cur_g)
                        tc_muc = None
                        continue     
 
                if MA_MON_FULL.match(c1) and cur_hk is not None:
                    if cur_g is None:
                        cur_g = _nhom_moi("Các học phần bắt buộc", cur_hk["so_hoc_ky"],
                                          loai="bat_buoc")
                        cur_hk["nhom"].append(cur_g)
                    tc = int(float(c3)) if c3.replace(".", "").isdigit() else 0
                    cur_g["mon"].append(_mon_khgd(c1, c2, tc, parse_prereq(c4)))
                    continue
    return [prog], canh_bao
 
 
# --------------------------------------------------------------------------- #
def chuan_hoa_khgd(progs, md):
    canh_bao = []
    for prog in progs:
        tong_ct = 0
        for hk in prog["hoc_ky"]:
            so_hk = hk["so_hoc_ky"]
            hk["nhom"] = [g for g in hk["nhom"]
                          if g["mon"] or g["loai"] in ("tu_do", "chon_tin_chi")]
            for g in hk["nhom"]:
                if g["mon"] and all(m["tin_chi"] == 0 for m in g["mon"]):
                    g["loai"] = "chung_chi"
                    g.pop("so_tin_chi_yeu_cau", None)
                for m in g["mon"]:
                    m["hoc_ky"] = so_hk
 
            bb = sum(m["tin_chi"] for g in hk["nhom"]
                     if g["loai"] == "bat_buoc" for m in g["mon"])
            da_biet = sum(g["so_tin_chi_yeu_cau"] for g in hk["nhom"]
                          if g["loai"] in ("tu_do", "chon_tin_chi")
                          and "so_tin_chi_yeu_cau" in g)
            thieu = [g for g in hk["nhom"] if g["loai"] in ("tu_do", "chon_tin_chi")
                     and "so_tin_chi_yeu_cau" not in g]
            con_lai = (hk["tin_chi_hoc_ky"] or 0) - bb - da_biet
            if thieu:
                if len(thieu) == 1 and con_lai > 0:
                    thieu[0]["so_tin_chi_yeu_cau"] = con_lai
                    canh_bao.append(
                        f"[{prog['ten_chuong_trinh']}] HK{so_hk}: suy ra "
                        f"'{thieu[0]['ten']}' = {con_lai} TC từ tổng TC học kỳ.")
                else:
                    for g in thieu:
                        g["so_tin_chi_yeu_cau"] = 0
                    canh_bao.append(
                        f"[{prog['ten_chuong_trinh']}] HK{so_hk}: không suy được số TC "
                        f"cho {len(thieu)} nhóm tự chọn (còn lại {con_lai} TC) — nên kiểm tra.")
 
            for g in hk["nhom"]:
                tong_mon = sum(m["tin_chi"] for m in g["mon"])
                if g["loai"] in ("tu_do", "chon_tin_chi"):
                    g["tong_tin_chi_nhom"] = g.get("so_tin_chi_yeu_cau", 0)
                    g["tin_chi_kha_dung"] = tong_mon
                else:
                    g["tong_tin_chi_nhom"] = tong_mon
                g["mon"] = g["mon"]
 
            hk["tin_chi_tu_cong"] = sum(g["tong_tin_chi_nhom"] for g in hk["nhom"]
                                        if g["loai"] != "chung_chi")
            if hk["tin_chi_hoc_ky"] and hk["tin_chi_tu_cong"] != hk["tin_chi_hoc_ky"]:
                canh_bao.append(
                    f"[{prog['ten_chuong_trinh']}] HK{so_hk}: lệch TC "
                    f"(tự cộng {hk['tin_chi_tu_cong']} ≠ PDF {hk['tin_chi_hoc_ky']}).")
            tong_ct += hk["tin_chi_tu_cong"]
        prog["tong_tin_chi"] = tong_ct
        prog["so_hoc_ky"] = len(prog["hoc_ky"])
    return progs, canh_bao
 
 
def dong_goi_khgd(md, progs, fmt):
    nganh = md["nganh"] or "Không rõ"
    khoa = md["khoa"]
    ma_ctdt = f"{initials(nganh)}_{khoa}" if khoa else initials(nganh)
    return {
        "loai_tai_lieu": "ke_hoach_giang_day",
        "nganh": nganh,
        "khoa": khoa,
        "khoa_vien": md["khoa_vien"],
        "ma_ctdt": ma_ctdt,
        "ma_ctdt_ghi_chu": "Mã tự sinh từ tên ngành + khoá (PDF không chứa mã CTĐT chính thức).",
        "tong_tin_chi": md["tong_tin_chi"],
        "format_pdf": fmt,
        "chu_thich_ky_hieu": CHU_THICH_KY_HIEU,
        "chu_thich_loai_nhom": CHU_THICH_LOAI_NHOM,
        "chu_thich_truong_nhom": {
            "tin_chi_hoc_ky": "Tổng tín chỉ của học kỳ do PDF công bố.",
            "tin_chi_tu_cong": "Tổng tín chỉ học kỳ do parser tự cộng (để đối chiếu).",
            "tong_tin_chi_nhom": "Tín chỉ nhóm đóng góp (bắt buộc: tổng TC các môn; "
                                 "chọn/tự do: số TC yêu cầu).",
            "so_tin_chi_yeu_cau": "Số TC tối thiểu phải tích luỹ (nhóm chon_tin_chi/tu_do).",
            "tin_chi_kha_dung": "Tổng TC toàn bộ môn được phép chọn trong nhóm.",
            "hoc_ky": "Học kỳ khuyến nghị học môn này theo kế hoạch giảng dạy.",
        },
        "chuong_trinh": progs,
    }
 
 
# =========================================================================== #
def chuan_hoa_nhom(nhom):
    out = []
    for g in nhom:
        declared = g.pop("_tong_pdf", None)   
        tong_mon = sum(m["tin_chi"] for m in g["mon"])
        ng = {"ma_nhom": g["ma_nhom"], "ten": g["ten"], "khoi": g.get("khoi", ""),
              "loai": g["loai"]}
        if g["loai"] in ("chon_tin_chi", "tu_do"):
            yeu_cau = g.get("so_tin_chi_yeu_cau",
                            declared if declared is not None else tong_mon)
            ng["tong_tin_chi_nhom"] = yeu_cau
            ng["so_tin_chi_yeu_cau"] = yeu_cau
            ng["tin_chi_kha_dung"] = tong_mon      
        else:
            ng["tong_tin_chi_nhom"] = declared if declared is not None else tong_mon
        ng["mon"] = g["mon"]
        out.append(ng)
    return out
 
 
def parse_pdf(pdf_path: str):
    with pdfplumber.open(pdf_path) as pdf:
        md = doc_metadata(pdf)
 
        if detect_loai_tai_lieu(pdf) == "khgd":
            fmt = detect_format_khgd(pdf)
            parser = {"P1": parse_khgd_P1, "P2": parse_khgd_P2, "P3": parse_khgd_P3}[fmt]
            progs, canh_bao = parser(pdf, md)
            progs, cb2 = chuan_hoa_khgd(progs, md)
            return dong_goi_khgd(md, progs, fmt), canh_bao + cb2
 
        fmt = detect_format(pdf)
        if fmt == "B":
            cau_truc, canh_bao = parse_format_B(pdf, md)
        elif fmt == "C":
            cau_truc, canh_bao = parse_format_C(pdf, md)
        else:
            cau_truc, canh_bao = parse_format_A(pdf, md)
    cau_truc = chuan_hoa_nhom(cau_truc)
    return dong_goi(md, cau_truc, fmt), canh_bao
 
 
def tong_tu_cong(ctdt):
    if ctdt.get("loai_tai_lieu") == "ke_hoach_giang_day":
        progs = ctdt.get("chuong_trinh") or []
        return progs[0]["tong_tin_chi"] if progs else 0
    t = 0
    for g in ctdt["cau_truc"]:
        if g["loai"] == "chung_chi":
            continue
        if g["loai"] in ("tu_do", "chon_tin_chi"):
            t += g.get("so_tin_chi_yeu_cau", 0)
        else:
            t += sum(m["tin_chi"] for m in g["mon"])
    return t
 

def main():
    input_root = Path(
        r"C:\Users\ADMIN\Desktop\agent_A\data\output\ChuongTrinhDaoTao_TheoKhoa"
    )

    output_root = Path(
        r"C:\Users\ADMIN\Desktop\agent_A\data\output\CTDT_JSON"
    )

    pdf_files = sorted(input_root.rglob("*.pdf"))

    print(f"Tìm thấy {len(pdf_files)} file PDF\n")

    success = 0
    failed = 0

    for pdf_path in pdf_files:
        try:
            print(f"Đang xử lý: {pdf_path}")

            ctdt, canh_bao = parse_pdf(str(pdf_path))

            relative = pdf_path.relative_to(input_root)

            json_path = output_root / relative.with_suffix(".json")

            json_path.parent.mkdir(parents=True, exist_ok=True)

            json_path.write_text(
                json.dumps(ctdt, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            if canh_bao:
                print(f"   ⚠ {len(canh_bao)} cảnh báo")

            print(f"   ✓ {json_path}\n")
            success += 1

        except Exception as e:
            failed += 1
            print(f"   ✗ Lỗi: {e}\n")

    print("=" * 60)
    print(f"Hoàn thành")
    print(f"Thành công : {success}")
    print(f"Thất bại   : {failed}")
    print(f"Tổng cộng  : {success + failed}")

if __name__ == "__main__":
    main()