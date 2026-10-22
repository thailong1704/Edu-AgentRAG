
import os
import re
import math
from typing import List, Optional

from dotenv import load_dotenv
from neo4j import GraphDatabase
from pydantic import BaseModel, Field, field_validator

from agno.agent import Agent
from agno.models.openai import OpenAIChat

from lms_common import mssv_hien_tai, doc_mssv, doc_ctxh

load_dotenv()

GPA_MODEL = os.getenv("GPA_MODEL", "gpt-5.5")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASS", "12345678")

DIEM_CHU_MAP = {"A+": 4.0, "A": 4.0, "B+": 3.5, "B": 3.0,
                "C+": 2.5, "C": 2.0, "D+": 1.5, "D": 1.0, "F": 0.0}
# "dat" trên cạnh DA_HOC = QUA MÔN (chỉ F là rớt), dùng cho tiên quyết.

# --- Cơ chế 2 (cải thiện GPA): thang bậc điểm hệ 4 (tăng dần) để tính 'bước nhảy' khi học lại.
_BAC_DIEM = [0.0, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
_VAL_TO_CHU = {4.0: "A", 3.5: "B+", 3.0: "B", 2.5: "C+", 2.0: "C", 1.5: "D+", 1.0: "D", 0.0: "F"}


def _idx_bac(ds: float) -> int:
    """Chỉ số bậc gần nhất (<=) của điểm ds trong thang _BAC_DIEM."""
    i = 0
    for k, v in enumerate(_BAC_DIEM):
        if v <= ds + 1e-9:
            i = k
    return i


def _uoc_diem_mon_moi(gpa_ht: float, cap: float = 4.0) -> float:
    """Ước điểm GIẢ ĐỊNH cho MÔN CHƯA HỌC = tầng GPA hiện tại + 2 bậc (cap A). Vd GPA ~2.0
    (tầng C) muốn 3.0 -> giả định B; không giả định cố định. cap để chặn theo mục tiêu nếu cần."""
    j = min(_idx_bac(gpa_ht) + 2, len(_BAC_DIEM) - 1)
    return min(_BAC_DIEM[j], cap)


def _muc_tieu_buoc_nhay(ds: float, buoc: int):
    """Điểm mục tiêu khi học lại theo TRẦN BƯỚC NHẢY (số bậc, mặc định 2). Trả (chu, val),
    hoặc None nếu đã kịch trần (không nâng thêm được)."""
    j = min(_idx_bac(ds) + max(1, buoc), len(_BAC_DIEM) - 1)
    v = _BAC_DIEM[j]
    return (_VAL_TO_CHU[v], v) if v > ds + 1e-9 else None


def _parse_muc_tieu_mon(s: str) -> dict:
    """'EE2013:B+, MT1005:B' -> {'EE2013':3.5,'MT1005':3.0}. Override mục tiêu TỪNG MÔN theo
    ý user (1b), đè lên trần bước nhảy mặc định. Bỏ mục sai định dạng."""
    out = {}
    for phan in str(s or "").replace(";", ",").split(","):
        if ":" not in phan:
            continue
        ma, dc = phan.split(":", 1)
        ma, dc = ma.strip().upper(), dc.strip().upper()
        if ma and dc in DIEM_CHU_MAP:
            out[ma] = DIEM_CHU_MAP[dc]
    return out


def _parse_bac_giam(s):
    """'12:15, 9:30, 6:45' -> [(6,45.0),(9,30.0),(12,15.0)] (tăng dần theo ngưỡng TC).
    Bảng bậc giảm học phí do coordinator tra HỌC PHÍ (kỳ mới nhất) rồi truyền vào — KHÔNG hardcode."""
    out = []
    for p in str(s or "").replace(";", ",").split(","):
        if ":" not in p:
            continue
        a, b = p.split(":", 1)
        try:
            out.append((int(a.strip()), float(b.strip())))
        except ValueError:
            pass
    return sorted(out)


def _tinh_hoc_phi(tc, muc_tron_goi, dinh_muc_tc, don_gia_vuot, bac_giam=None):
    """TỔNG học phí một kỳ + % giảm. Trả (tien, pct) hoặc (None, 0) nếu thiếu dữ liệu.
      • tc > định mức        -> gói + (tc - định mức) × đơn giá vượt (pct=0).
      • tc <= định mức       -> áp BẢNG BẬC GIẢM (bac_giam) nếu có: ngưỡng nhỏ nhất thỏa -> giảm %.
        Không có bảng -> nguyên gói (pct=0). MỌI mức (gói, định mức, đơn giá, bảng giảm) do
        coordinator tra HỌC PHÍ KỲ MỚI NHẤT bơm vào — tool KHÔNG tự bịa/không hardcode."""
    if not muc_tron_goi:
        return None, 0
    if dinh_muc_tc and tc > dinh_muc_tc:
        return float(muc_tron_goi) + (tc - dinh_muc_tc) * float(don_gia_vuot or 0), 0
    for nguong, pct in (bac_giam or []):        # tăng dần -> lấy bậc thấp nhất thỏa (giảm nhiều nhất)
        if tc <= nguong:
            return float(muc_tron_goi) * (1 - pct / 100.0), int(pct)
    return float(muc_tron_goi), 0


def _tien(v) -> str:
    """Định dạng tiền VND: 15750000 -> '15.750.000đ'."""
    return f"{int(round(v)):,}".replace(",", ".") + "đ"
# ΔGPA khi học lại: (điểm_mới − điểm_cũ)×TC / tổng_TC (tổng_TC cố định vì lấy điểm cao nhất,
# môn cũ không cộng thêm mẫu số). ΔGPA/tín-chỉ = (điểm_mới − điểm_cũ) -> TÍN CHỈ TRIỆT TIÊU,
# nên ưu tiên theo MỨC TĂNG ĐIỂM khả thi, KHÔNG theo số tín chỉ.

try:
    _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    print(f"[gpa] Neo4j: {NEO4J_URI}")
except Exception as e:
    print(f"[gpa] KHÔNG kết nối Neo4j ({type(e).__name__}) — chạy graph_bang_diem.py trước")
    _driver = None


def _mssv() -> str:
    """MSSV để LỌC graph (graph khóa theo MSSV thật).

    mssv_hien_tai giữ KHÓA ĐĂNG NHẬP (tên đăng nhập = thư mục data/). Đổi sang MSSV bằng
    doc_mssv (đọc data/{khoa}/ket_qua.json). Nếu không đọc được (vd test terminal truyền
    thẳng MSSV vào GPA_MSSV) thì dùng chính giá trị đó."""
    khoa = mssv_hien_tai.get() or os.getenv("GPA_MSSV", "")
    if not khoa:
        return ""
    return doc_mssv(khoa) or khoa


def _khoa() -> str:
    """KHÓA ĐĂNG NHẬP (= tên thư mục data/{...}/) — dùng đọc FILE theo user (vd CTXH ở
    lich_hoc_thi.json). Khác _mssv(): _mssv là MSSV thật để lọc Neo4j; file data/ lại đặt
    theo tên đăng nhập, nên doc_ctxh/path_lich phải dùng khóa này, KHÔNG dùng MSSV."""
    return mssv_hien_tai.get() or os.getenv("GPA_MSSV", "")


# --- Cấp (năm) của môn = chữ số ĐẦU ngay sau cụm chữ cái trong mã (MT1003->1, EE2015->2).
# SV trình độ năm L chỉ đăng ký được môn cấp <= L. Anh văn (LA...) theo CHUỖI TIÊN QUYẾT
# riêng, KHÔNG theo cấp năm -> MIỄN lọc cấp (trả None). Mã không theo quy ước -> None
# (permissive: không chặn nhầm). Thêm tiền tố vào _MIEN_LOC_CAP nếu có nhóm ngoại lệ khác.
_MIEN_LOC_CAP = ("LA",)
_RE_CAP = re.compile(r"^[A-Z]+(\d)")


def _cap_mon(ma) -> Optional[int]:
    s = str(ma or "").strip().upper()
    if any(s.startswith(p) for p in _MIEN_LOC_CAP):
        return None
    m = _RE_CAP.match(s)
    return int(m.group(1)) if m else None


def _hop_cap(ma, cap_toi_da) -> bool:
    """Môn có được phép đăng ký ở trình độ cap_toi_da? None (miễn lọc/không rõ) -> luôn OK."""
    if not cap_toi_da:
        return True
    c = _cap_mon(ma)
    return c is None or c <= cap_toi_da


def _hk_hien_tai() -> int:
    """HỌC KỲ SV đang học tới = HK CAO NHẤT trong các môn ĐÃ HỌC (đạt) + 1.

    KHÔNG dùng '% môn đã học của mỗi HK' vì HK có NHÓM TỰ CHỌN (vd nhóm A 15+ môn, SV chỉ lấy
    1-2) -> % luôn thấp -> tính SAI học kỳ hiện tại -> loại oan môn HK sau, kể cả ĐATN. Dùng
    'max HK đã học' thì miễn nhiễm với môn tự chọn không lấy.

    Cho phép đăng ký môn HK <= mức này (+ nợ HK cũ + môn không rõ HK ở nơi gọi); môn HK cao hơn
    = chưa tới lượt. Trả 99 nếu SV chưa học môn nào có gắn học kỳ (an toàn: không loại gì)."""
    r = _q("""MATCH (:SinhVien {ma: $sv})-[d:DA_HOC]->(m:MonHoc)
              WHERE d.dat = true AND m.hoc_ky_ke_hoach IS NOT NULL
              RETURN max(m.hoc_ky_ke_hoach) AS hk_max""")
    hk_max = r[0]["hk_max"] if (r and r[0].get("hk_max") is not None) else None
    return int(hk_max) + 1 if hk_max is not None else 99


def _q(cypher: str, **params):
    """Chạy Cypher -> list dict. Tự chèn $sv = mssv hiện tại để mọi tool lọc đúng người.
    Lỗi -> list rỗng để agent tự báo thiếu dữ liệu."""
    if _driver is None:
        return []
    params.setdefault("sv", _mssv())      # mọi Cypher dùng (SinhVien {ma:$sv})
    try:
        with _driver.session() as s:
            return [r.data() for r in s.run(cypher, **params)]
    except Exception as e:
        print(f"[gpa] Lỗi Cypher ({type(e).__name__}): {e}")
        return []


# ---------------- MODEL KẾT QUẢ (pydantic v2) ----------------
class MonCanCaiThien(BaseModel):
    ma_mon: str = Field(..., description="Mã môn")
    ten_mon: str = Field(..., description="Tên môn")
    tin_chi: int = Field(..., description="Số tín chỉ")
    diem_chu: str = Field(..., description="Điểm chữ hiện tại")
    ly_do: str = Field(..., description="Vì sao nên cải thiện")
    diem_du_kien: Optional[str] = Field(None, description="Điểm chữ dự kiến")

    @field_validator("tin_chi")
    @classmethod
    def _tc(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Số tín chỉ phải > 0")
        return v


class PhanTichGPA(BaseModel):
    """Dùng khi cần kết quả có cấu trúc: tao_gpa_agent(output_schema=PhanTichGPA)."""
    gpa_hien_tai: float
    tong_tin_chi_da_hoc: int
    nhan_xet_tong_quan: str
    mon_can_cai_thien: List[MonCanCaiThien] = Field(default_factory=list)
    gpa_du_kien: float
    tong_chi_phi: float = 0.0
    du_bao_ket_qua: str


# ---------------- TOOLS ----------------
def tinh_gpa_hien_tai() -> str:
    """Tính GPA hiện tại và tổng tín chỉ tích lũy của sinh viên từ graph.

    Dùng ĐẦU TIÊN để nắm tình hình trước khi tư vấn.
    """
    r = _q("""
        MATCH (v:SinhVien {ma: $sv})-[d:DA_HOC]->(m:MonHoc)
        WHERE d.diem_so IS NOT NULL AND m.tin_chi > 0
        RETURN sum(d.diem_so * m.tin_chi) AS tong, sum(m.tin_chi) AS tc_tinh,
               sum(CASE WHEN d.dat = false THEN 1 ELSE 0 END) AS so_rot
    """)
    if not r or not r[0].get("tc_tinh"):
        return "Graph chưa có bảng điểm. Hãy chạy graph_bang_diem.py."
    d = r[0]
    gpa = round(d["tong"] / d["tc_tinh"], 2)

    # Tín chỉ tích lũy: ưu tiên CON SỐ CHÍNH THỨC từ MyBK (node SinhVien), vì cách tự cộng
    # tín chỉ các môn có điểm sẽ THIẾU những tín chỉ không tính GPA (vd Anh văn) mà nhà
    # trường vẫn cộng vào tổng tích lũy.
    sv = _q("MATCH (v:SinhVien {ma: $sv}) RETURN v.tin_chi_tich_luy AS tl, v.tong_tin_chi_can AS can")
    tc_chinh_thuc = sv[0]["tl"] if (sv and sv[0].get("tl")) else None
    tong_can = sv[0]["can"] if (sv and sv[0].get("can")) else None

    if tc_chinh_thuc:
        dong_tc = f"Tín chỉ tích lũy: {tc_chinh_thuc}"
        if tong_can:
            dong_tc += f"/{tong_can}"
    else:
        dong_tc = f"Tín chỉ tính điểm (GPA): {d['tc_tinh']}"

    chua = _q("MATCH (:SinhVien {ma: $sv})-[:CHUA_HOC]->(m:MonHoc) RETURN count(m) AS n")
    return (f"GPA hiện tại: {gpa}/4.0 | {dong_tc}\n"
            f"Số môn RỚT (điểm F): {d['so_rot']}\n"
            f"Số môn chưa học: {chua[0]['n'] if chua else 0}")


def liet_ke_mon_cai_thien(buoc_nhay: int = 2) -> str:
    """Liệt kê môn ĐÃ HỌC theo ƯU TIÊN CẢI THIỆN, kèm ΔGPA mỗi môn đem lại nếu học lại.

    Metric = MỨC TĂNG ĐIỂM khả thi (theo trần bước nhảy), KHÔNG theo số tín chỉ (vì ΔGPA/tín
    chỉ = mức tăng điểm, tín chỉ triệt tiêu). Môn điểm thấp -> tiềm năng tăng lớn -> lên đầu.
    Anh văn KHÔNG điểm không xuất hiện ở đây (không có gì để cải thiện).

    Args:
        buoc_nhay: trần số bậc điểm giả định nâng được mỗi lần học lại (mặc định 2, vd C->B+).
    """
    r = _q("""
        MATCH (:SinhVien {ma: $sv})-[d:DA_HOC]->(m:MonHoc)
        WHERE d.diem_so IS NOT NULL AND m.tin_chi > 0
        RETURN m.ma AS ma, m.ten AS ten, m.tin_chi AS tc,
               d.diem_chu AS diem, d.diem_so AS ds
    """)
    if not r:
        return "Chưa có môn nào đã học (hoặc graph chưa có dữ liệu)."
    tc_tong = sum(x["tc"] for x in r) or 1
    ds_list = []
    for x in r:
        mt = _muc_tieu_buoc_nhay(x["ds"], buoc_nhay)
        if not mt:                       # đã kịch trần điểm -> không cải thiện được
            continue
        chu, val = mt
        dgpa = (val - x["ds"]) * x["tc"] / tc_tong
        ds_list.append({**x, "muc": chu, "dgpa": dgpa})
    if not ds_list:
        return "Mọi môn đã học đều ở mức cao — không còn môn nào đáng cải thiện."
    # ưu tiên ΔGPA lớn nhất (mức tăng điểm × TC), rồi điểm thấp trước
    ds_list.sort(key=lambda x: (-x["dgpa"], x["ds"]))
    dong = [f"  {x['ma']} | {(x['ten'] or '?')[:30]:<30} | {x['tc']} TC | {x['diem']}→{x['muc']} "
            f"| +{x['dgpa']:.3f} GPA" for x in ds_list]
    return (f"Môn nên cải thiện ({len(ds_list)} môn, ưu tiên ΔGPA giảm dần; giả định nâng tối đa "
            f"{buoc_nhay} bậc):\n" + "\n".join(dong))


def mon_chua_hoc() -> str:
    """Liệt kê TẤT CẢ môn sinh viên CHƯA HỌC trong chương trình (không xét tiên quyết).

    Dùng khi hỏi ĐÚNG CÂU 'môn nào chưa học', 'còn môn gì chưa học', 'chưa học những gì'.
    KHÁC với mon_du_dieu_kien_dang_ky (lọc theo đủ tiên quyết) và mon_chua_du_dieu_kien
    (lọc theo thiếu tiên quyết) — tool này trả về MỌI môn chưa học, không lọc gì.
    """
    r = _q("""
        MATCH (v:SinhVien {ma: $sv})-[:CHUA_HOC]->(m:MonHoc)
        RETURN m.ma AS ma, m.ten AS ten, coalesce(m.tin_chi,0) AS tc,
               m.hoc_ky_ke_hoach AS hk
        ORDER BY coalesce(m.hoc_ky_ke_hoach, 99), m.ma
    """)
    if not r:
        return "Không còn môn nào chưa học — bạn đã học hết chương trình (theo dữ liệu graph)."
    dong = []
    for x in r:
        hk = f"HK{x['hk']}" if x["hk"] else "  ?"
        dong.append(f"  {hk:<5} {x['ma']:<8} {x['ten'] or '?'} | {x['tc']} TC")
    return f"Môn CHƯA HỌC ({len(r)} môn):\n" + "\n".join(dong)


def mon_du_dieu_kien_dang_ky(cap_toi_da: int = 0) -> str:
    """Môn CHƯA HỌC mà sinh viên ĐỦ ĐIỀU KIỆN đăng ký ngay (mọi tiên quyết đã đạt).

    Truy vấn GRAPH: duyệt cạnh TIEN_QUYET rồi đối chiếu bảng điểm.
    Dùng khi hỏi 'kỳ tới em học được môn gì'.

    Args:
        cap_toi_da: Trình độ NĂM hiện tại của SV (1..4). Nếu >0, CHỈ giữ môn có cấp <= mức
            này (SV_N2 chỉ đăng ký môn mã 1,2...). Anh văn được miễn lọc. 0 = không lọc cấp.
    """
    r = _q("""
        MATCH (v:SinhVien {ma: $sv})-[:CHUA_HOC]->(m:MonHoc)
        WHERE NOT EXISTS {
            MATCH (m)-[:TIEN_QUYET]->(tq:MonHoc)
            WHERE NOT EXISTS { MATCH (v)-[d:DA_HOC]->(tq) WHERE d.dat = true }
        }
        RETURN m.ma AS ma, m.ten AS ten, m.tin_chi AS tc,
               count { (m)-[:TIEN_QUYET]->() } AS so_tq
        ORDER BY m.tin_chi DESC
    """)
    if r and cap_toi_da:
        loai = len(r)
        r = [x for x in r if _hop_cap(x["ma"], cap_toi_da)]
        bi_loai = loai - len(r)
    else:
        bi_loai = 0
    if not r:
        return "Không có môn nào đủ điều kiện (hoặc graph chưa có quan hệ tiên quyết)."
    dong = [f"  {x['ma']} | {x['ten'] or '?'} | {x['tc']} TC"
            + (f" (đã qua {x['so_tq']} tiên quyết)" if x["so_tq"] else " (không có tiên quyết)")
            for x in r]
    tieu_de = f"ĐỦ ĐIỀU KIỆN đăng ký ({len(r)} môn"
    tieu_de += f", đã lọc theo trình độ năm {cap_toi_da}" if cap_toi_da else ""
    tieu_de += "):"
    kq = tieu_de + "\n" + "\n".join(dong)
    if bi_loai:
        kq += (f"\n(Ẩn {bi_loai} môn cấp CAO HƠN trình độ năm {cap_toi_da} — đủ tiên quyết "
               f"nhưng phải LÊN TRÌNH ĐỘ mới đăng ký được; hỏi kế hoạch lên trình độ nếu cần.)")
    return kq


def mon_chua_du_dieu_kien(cap_hien_tai: int = 0) -> str:
    """Môn CHƯA HỌC còn VƯỚNG — tách 2 lý do: (1) thiếu TIÊN QUYẾT, (2) VƯỢT CẤP năm.

    Dùng khi hỏi 'còn môn nào em chưa học được, vì sao'.

    Args:
        cap_hien_tai: Trình độ NĂM hiện tại của SV (1..4). Nếu >0, các môn đã đủ tiên quyết
            nhưng cấp CAO HƠN mức này sẽ được liệt kê riêng ('cần lên trình độ'). 0 = bỏ qua.
    """
    r = _q("""
        MATCH (v:SinhVien {ma: $sv})-[:CHUA_HOC]->(m:MonHoc)-[:TIEN_QUYET]->(tq:MonHoc)
        WHERE NOT EXISTS { MATCH (v)-[d:DA_HOC]->(tq) WHERE d.dat = true }
        RETURN m.ma AS ma, m.ten AS ten, collect(tq.ma) AS thieu
        ORDER BY size(collect(tq.ma)) ASC
    """)
    phan = []
    if r:
        dong = [f"  {x['ma']} | {x['ten'] or '?'} — còn thiếu: {', '.join(x['thieu'])}" for x in r]
        phan.append(f"CHƯA đủ TIÊN QUYẾT ({len(r)} môn):\n" + "\n".join(dong))

    if cap_hien_tai:
        # môn ĐỦ tiên quyết nhưng VƯỢT cấp năm -> phải lên trình độ mới đăng ký được
        du = _q("""
            MATCH (v:SinhVien {ma: $sv})-[:CHUA_HOC]->(m:MonHoc)
            WHERE NOT EXISTS {
                MATCH (m)-[:TIEN_QUYET]->(tq:MonHoc)
                WHERE NOT EXISTS { MATCH (v)-[d:DA_HOC]->(tq) WHERE d.dat = true }
            }
            RETURN m.ma AS ma, m.ten AS ten, m.tin_chi AS tc
            ORDER BY m.ma
        """)
        vuot = [x for x in du if (_cap_mon(x["ma"]) or 0) > cap_hien_tai]
        if vuot:
            dong = [f"  {x['ma']} | {x['ten'] or '?'} | cấp {_cap_mon(x['ma'])} "
                    f"(bạn đang trình độ năm {cap_hien_tai})" for x in vuot]
            phan.append(f"ĐỦ tiên quyết nhưng VƯỢT CẤP — cần LÊN TRÌNH ĐỘ trước ({len(vuot)} môn):\n"
                        + "\n".join(dong))

    if not phan:
        return "Không có môn nào bị vướng (tiên quyết đủ, không vượt cấp)."
    return "\n\n".join(phan)


def kiem_tra_len_trinh_do(cap_muc_tieu: int = 0, nguong_tin_chi: int = 0,
                          av_bac_can: int = 0, ctxh_can: int = 0) -> str:
    """Kiểm tra SV đã ĐỦ ĐIỀU KIỆN lên trình độ năm chưa (để đăng ký môn cấp cao hơn).

    So SỐ THỰC TẾ của SV (tín chỉ tích lũy + bậc Anh văn đạt + CTXH) với NGƯỠNG của trình độ
    mục tiêu. NGƯỠNG do coordinator tra Bảng chuẩn (#7) rồi TRUYỀN VÀO — tool KHÔNG tự bịa.

    Args:
        cap_muc_tieu: Trình độ năm muốn đạt (2/3/4). 0 nếu chưa rõ.
        nguong_tin_chi: Ngưỡng tín chỉ tích lũy tối thiểu (N2=28, N3/N4=56 theo #7).
        av_bac_can: Bậc Anh văn tối thiểu phải đạt (N2->1, N3->3 hoặc 4, N4->4...).
        ctxh_can: Số ngày CTXH tối thiểu nếu quy định áp dụng cho khóa của SV (0 = bỏ qua).
    """
    sv = _q("MATCH (v:SinhVien {ma: $sv}) RETURN v.tin_chi_tich_luy AS tl")
    tc = sv[0]["tl"] if (sv and sv[0].get("tl") is not None) else None
    av = _q("""MATCH (:SinhVien {ma: $sv})-[r:CHUAN_NGOAI_NGU]->(:NgoaiNgu)
               WHERE r.dat = true AND r.bac IS NOT NULL RETURN max(r.bac) AS bac""")
    av_max = av[0]["bac"] if (av and av[0].get("bac") is not None) else 0
    ctxh = doc_ctxh(_khoa())

    if not (nguong_tin_chi or av_bac_can or ctxh_can):
        return ("Chưa có ngưỡng để đối chiếu — coordinator cần tra Bảng chuẩn (#7) lấy ngưỡng "
                "tín chỉ / bậc Anh văn / CTXH của trình độ này rồi truyền vào.")

    dich = f"lên trình độ năm {cap_muc_tieu}" if cap_muc_tieu else "lên trình độ"
    out, du = [f"ĐIỀU KIỆN {dich.upper()}:"], True
    if nguong_tin_chi:
        if tc is None:
            out.append(f"  • Tín chỉ tích lũy: CHƯA đọc được (cần ≥ {nguong_tin_chi})."); du = False
        elif tc >= nguong_tin_chi:
            out.append(f"  • Tín chỉ tích lũy: {tc} ≥ {nguong_tin_chi}")
        else:
            out.append(f"  • Tín chỉ tích lũy: {tc}/{nguong_tin_chi} — THIẾU {nguong_tin_chi - tc} TC"); du = False
    if av_bac_can:
        if av_max >= av_bac_can:
            out.append(f"  • Anh văn: đã đạt bậc {av_max} (cần {av_bac_can})")
        else:
            out.append(f"  • Anh văn: mới bậc {av_max}/{av_bac_can} — cần đạt Anh văn {av_bac_can}"); du = False
    if ctxh_can:
        if ctxh is None:
            out.append(f"  • CTXH: CHƯA đọc được (cần ≥ {ctxh_can} ngày)."); du = False
        elif ctxh >= ctxh_can:
            out.append(f"  • CTXH: {ctxh} ≥ {ctxh_can} ngày")
        else:
            out.append(f"  • CTXH: {ctxh}/{ctxh_can} ngày — THIẾU {ctxh_can - ctxh} ngày"); du = False

    out.append("")
    out.append(f"=> ĐỦ điều kiện {dich}." if du else "=> CHƯA đủ — xem mục CÒN THIẾU ở trên.")
    out.append("(Còn chuẩn khác — CNTTCB, GDQP, GDTC, môn nhập môn — kiểm ở MyBK/Bảng chuẩn.)")
    return "\n".join(out)


def lap_ke_hoach_bu(cap_hien_tai: int, nguong_tin_chi: int, cap_muc_tieu: int = 0,
                    tin_chi_toi_da: int = 18, av_bac_can: int = 0, ctxh_can: int = 0) -> str:
    """Lập KẾ HOẠCH BÙ để đạt ngưỡng LÊN TRÌNH ĐỘ (Cơ chế 1, mức trung bình, bắc cầu 1 chặng).

    Hướng A: SV muốn đăng ký môn cấp K -> phải lên trình độ N-K (ngưỡng do coordinator tra #7
    rồi truyền vào). Kế hoạch BÁM THỨ TỰ HỌC KỲ trong CTĐT (hoc_ky_ke_hoach): lấy môn của các
    học kỳ kế tiếp mà SV ĐĂNG KÝ ĐƯỢC NGAY (cấp <= cap_hien_tai, đủ tiên quyết) — KỂ CẢ Anh
    văn bậc kế tiếp (mỗi bậc +2 TC, đi tuần tự) — cho đến khi đủ ngưỡng. Môn cấp cao hơn (năm
    K) trong các học kỳ đó ĐỂ LẠI, chỉ mở sau khi lên hạng.`

    Args:
        cap_hien_tai: Trình độ năm hiện tại (1..4) — giới hạn cấp môn thường được chọn.
        nguong_tin_chi: Ngưỡng tín chỉ tích lũy của trình độ mục tiêu (vd N3 = 56).
        cap_muc_tieu: Trình độ muốn lên (để ghi 'mở khóa môn cấp K'). 0 nếu không rõ.
        tin_chi_toi_da: Trần tín chỉ đăng ký một kỳ (mặc định 25).
        av_bac_can / ctxh_can: Bậc Anh văn / số ngày CTXH tối thiểu của trình độ mục tiêu.
    """
    if cap_hien_tai <= 0 or nguong_tin_chi <= 0:
        return ("Cần biết trình độ hiện tại và ngưỡng tín chỉ của trình độ mục tiêu "
                "(coordinator tra Bảng chuẩn #7 rồi truyền vào).")
    sv = _q("MATCH (v:SinhVien {ma: $sv}) RETURN v.tin_chi_tich_luy AS tl")
    tc_ht = sv[0]["tl"] if (sv and sv[0].get("tl") is not None) else None
    av = _q("""MATCH (:SinhVien {ma: $sv})-[r:CHUAN_NGOAI_NGU]->(:NgoaiNgu)
               WHERE r.dat = true AND r.bac IS NOT NULL RETURN max(r.bac) AS bac""")
    av_max = av[0]["bac"] if (av and av[0].get("bac") is not None) else 0
    ctxh = doc_ctxh(_khoa())
    dich = f"N{cap_muc_tieu}" if cap_muc_tieu else "trình độ tiếp theo"
    out = [f"KẾ HOẠCH lên {dich} (ngưỡng {nguong_tin_chi} TC tích lũy):"]

    if tc_ht is None:
        out.append("  • CHƯA đọc được tín chỉ tích lũy — kiểm ở MyBK trước.")
        return "\n".join(out)
    if tc_ht >= nguong_tin_chi and av_max >= av_bac_can:
        out.append(f"  • Tín chỉ tích lũy {tc_ht} ≥ {nguong_tin_chi} và Anh văn bậc {av_max} — "
                   f"đã đủ điều kiện về TC & ngoại ngữ.")
    else:
        thieu = max(0, nguong_tin_chi - tc_ht)
        out.append(f"  • Tín chỉ tích lũy: {tc_ht}/{nguong_tin_chi}"
                   + (f" — cần thêm {thieu} TC." if thieu else "."))

        # (a) MÔN THƯỜNG đăng ký được: chưa học, đủ TIÊN QUYẾT (bỏ qua khuyến nghị),
        #     cấp <= trình độ hiện tại. Lấy kèm HỌC KỲ KẾ HOẠCH để bám lộ trình CTĐT.
        reg = _q("""
            MATCH (v:SinhVien {ma: $sv})-[:CHUA_HOC]->(m:MonHoc)
            WHERE NOT EXISTS {
                MATCH (m)-[:TIEN_QUYET]->(tq:MonHoc)
                WHERE NOT EXISTS { MATCH (v)-[d:DA_HOC]->(tq) WHERE d.dat = true }
            }
            RETURN m.ma AS ma, m.ten AS ten, coalesce(m.tin_chi,0) AS tc,
                   coalesce(m.hoc_ky_ke_hoach, 99) AS hk
        """)
        cand = [{"ma": x["ma"], "ten": x["ten"], "tc": x["tc"], "hk": x["hk"], "av": False}
                for x in reg if _hop_cap(x["ma"], cap_hien_tai) and x["tc"] > 0]

        # (b) ANH VĂN bậc kế tiếp CHƯA đạt (tuần tự tới av_bac_can) -> ĐƯA VÀO kế hoạch,
        #     tính +TC. Anh văn có tiên quyết bậc kế tiếp nên chỉ nhận bậc = av_max+1... liên tục.
        if av_bac_can and av_max < av_bac_can:
            avq = _q("""MATCH (v:SinhVien {ma: $sv})-[r:CHUAN_NGOAI_NGU]->(nn:NgoaiNgu)
                        WHERE r.dat = false AND nn.bac IS NOT NULL
                        RETURN nn.ma AS ma, nn.ten AS ten, coalesce(nn.tin_chi,2) AS tc,
                               nn.bac AS bac ORDER BY nn.bac""")
            for x in avq:
                if av_max < x["bac"] <= av_bac_can:      # đúng chuỗi bậc kế tiếp
                    cand.append({"ma": x["ma"], "ten": x["ten"], "tc": x["tc"],
                                 "hk": x["bac"], "av": True})

        # SẮP THEO HỌC KỲ KẾ HOẠCH (bám CTĐT); trong cùng kỳ ưu tiên Anh văn (chuẩn bắt buộc).
        cand.sort(key=lambda x: (x["hk"], 0 if x["av"] else 1, x["ma"]))
        chon, tc_gom, av_them = [], 0, []
        for m in cand:
            if tc_gom + m["tc"] > tin_chi_toi_da:
                continue
            chon.append(m); tc_gom += m["tc"]
            if m["av"]:
                av_them.append(m["ma"])
            if tc_gom >= thieu and av_max + len(av_them) >= av_bac_can:
                break

        if not chon:
            out.append("    (Không có môn phù hợp trong kế hoạch để gom — chờ mở lớp / học cải thiện.)")
        else:
            out.append("    Kế hoạch kỳ tới (theo thứ tự học kỳ trong CTĐT):")
            for m in chon:
                ghi = "  ← Anh văn (đạt chuẩn ngoại ngữ)" if m["av"] else ""
                out.append(f"      HK{m['hk']} | {m['ma']} | {(m['ten'] or '?')[:32]} | {m['tc']} TC{ghi}")
            out.append(f"    -> {len(chon)} môn, {tc_gom} TC; sau kỳ này: {tc_ht + tc_gom} "
                       f"/{nguong_tin_chi} TC"
                       + (" (đủ ngưỡng TC)" if tc_ht + tc_gom >= nguong_tin_chi
                          else f" (còn thiếu {nguong_tin_chi - tc_ht - tc_gom} — chia tiếp kỳ sau)"))

    # CTXH KHÔNG phải tín chỉ -> luôn nhắc riêng (không gom vào kế hoạch môn).
    if ctxh_can and (ctxh is None or ctxh < ctxh_can):
        con = "?" if ctxh is None else ctxh_can - ctxh
        out.append(f"  • CTXH: cần đủ {ctxh_can} ngày (đang {ctxh if ctxh is not None else '?'}) "
                   f"— còn {con} ngày; đăng ký hoạt động ngoại khóa (không tính tín chỉ).")

    if cap_muc_tieu:
        out += ["", f"→ Hoàn thành kế hoạch trên để lên {dich}; KHI ĐÓ mới đăng ký được môn cấp "
                    f"{cap_muc_tieu} (hiện chưa đăng ký được ở trình độ năm {cap_hien_tai})."]
    return "\n".join(out)


def xem_quan_he_mon(ma_mon: str) -> str:
    """Môn này yêu cầu những môn nào (tiên quyết / song hành / khuyến nghị) và đã đạt chưa.

    Args:
        ma_mon: Mã môn cần tra, vd 'EE4011'.
    """
    ma = str(ma_mon).strip().upper()
    r = _q("""
        MATCH (m:MonHoc {ma: $ma})
        OPTIONAL MATCH (m)-[rel]->(k:MonHoc)
        WHERE type(rel) IN ['TIEN_QUYET','SONG_HANH','KHUYEN_NGHI']
        OPTIONAL MATCH (:SinhVien {ma: $sv})-[d:DA_HOC]->(k)
        RETURN m.ten AS ten_mon, m.tin_chi AS tc, type(rel) AS loai,
               k.ma AS ma_k, k.ten AS ten_k, d.diem_chu AS diem, d.dat AS dat
    """, ma=ma)
    if not r:
        return f"Không tìm thấy môn '{ma}' trong graph."
    nhom = {"TIEN_QUYET": [], "SONG_HANH": [], "KHUYEN_NGHI": []}
    for x in r:
        if not x["loai"]:
            continue
        tt = ("ĐÃ ĐẠT" if x["dat"] else
              f"CHƯA ĐẠT ({x['diem']})" if x["diem"] else "chưa học")
        nhom[x["loai"]].append(f"    {x['ma_k']} ({x['ten_k'] or '?'}) — {tt}")
    ten_hien = {"TIEN_QUYET": "TIÊN QUYẾT (BẮT BUỘC học xong trước)",
                "SONG_HANH": "SONG HÀNH (học cùng kỳ hoặc trước)",
                "KHUYEN_NGHI": "KHUYẾN NGHỊ (KHÔNG bắt buộc)"}
    out = [f"{ma} — {r[0]['ten_mon'] or '?'} ({r[0]['tc'] or '?'} TC)"]
    for loai in ("TIEN_QUYET", "SONG_HANH", "KHUYEN_NGHI"):
        out.append(f"  {ten_hien[loai]}:" + ("" if nhom[loai] else " không có"))
        out.extend(nhom[loai])
    return "\n".join(out)


def mon_bi_chan_boi(ma_mon: str) -> str:
    """Không đạt môn này thì những môn nào bị CHẶN (không đăng ký được)?

    Truy vấn GRAPH đi NGƯỢC cạnh TIEN_QUYET. Dùng khi hỏi 'rớt môn X ảnh hưởng gì'.

    Args:
        ma_mon: Mã môn, vd 'EE3011'.
    """
    ma = str(ma_mon).strip().upper()
    r = _q("""
        MATCH (sau:MonHoc)-[:TIEN_QUYET]->(m:MonHoc {ma: $ma})
        RETURN sau.ma AS ma, sau.ten AS ten, sau.tin_chi AS tc
        ORDER BY sau.ma
    """, ma=ma)
    if not r:
        return (f"{ma} KHÔNG phải tiên quyết của môn nào -> không đạt môn này "
                f"KHÔNG chặn môn khác (nhưng vẫn ảnh hưởng GPA và tín chỉ tích lũy).")
    dong = [f"  {x['ma']} | {x['ten'] or '?'} | {x['tc']} TC" for x in r]
    return f"Không đạt {ma} sẽ CHẶN {len(r)} môn:\n" + "\n".join(dong)


def lap_ke_hoach_hoc_ky(tin_chi_toi_da: int = 25, hoc_phi_moi_tin_chi: float = 0) -> str:
    """Lập kế hoạch đăng ký cho học kỳ tới theo ĐÚNG THỨ TỰ ƯU TIÊN của trường.

    NGUYÊN TẮC (rất quan trọng, đừng làm ngược):
      1. Ưu tiên MÔN CHƯA HỌC theo kế hoạch giảng dạy — kể cả môn nợ của học kỳ trước.
         Phải học cho hết chương trình trước đã.
      2. CHỈ KHI còn dư tín chỉ (chưa chạm mức tối đa) mới đề xuất học CẢI THIỆN.
      3. Luôn tính tổng học phí để sinh viên cân đối được.

    Args:
        tin_chi_toi_da: Số tín chỉ tối đa được đăng ký một học kỳ (mặc định 25 theo
            quy định; nếu không chắc hãy hỏi agent tra cứu học vụ).
        hoc_phi_moi_tin_chi: Học phí một tín chỉ (VNĐ). Để 0 nếu chưa biết — khi đó
            tool bỏ qua phần chi phí và bạn phải đi hỏi mức thu hiện hành, ĐỪNG bịa.
    """
    if tin_chi_toi_da <= 0:
        return "Số tín chỉ tối đa phải lớn hơn 0."

    # --- (1) môn chưa học, ĐỦ tiên quyết, sắp theo học kỳ kế hoạch (nợ cũ lên trước) ---
    can_hoc = _q("""
        MATCH (v:SinhVien {ma: $sv})-[:CHUA_HOC]->(m:MonHoc)
        WHERE NOT EXISTS {
            MATCH (m)-[:TIEN_QUYET]->(tq:MonHoc)
            WHERE NOT EXISTS { MATCH (v)-[d:DA_HOC]->(tq) WHERE d.dat = true }
        }
        RETURN m.ma AS ma, m.ten AS ten, coalesce(m.tin_chi,0) AS tc,
               m.hoc_ky_ke_hoach AS hk, coalesce(m.loai_nhom,'') AS nhom
        ORDER BY coalesce(m.hoc_ky_ke_hoach, 99), m.ma
    """)
    if not can_hoc:
        return ("Không tìm thấy môn nào đủ điều kiện đăng ký. "
                "Có thể graph chưa nạp CTĐT (chạy graph_bang_diem.py).")

    chon, tc_chon = [], 0
    for m in can_hoc:
        if tc_chon + m["tc"] > tin_chi_toi_da:
            continue
        chon.append(m)
        tc_chon += m["tc"]

    out = [f"KẾ HOẠCH ĐĂNG KÝ (tối đa {tin_chi_toi_da} TC)", "",
           "A. MÔN THEO KẾ HOẠCH — ưu tiên 1, phải học hết trước khi cải thiện:"]
    for m in chon:
        hk = f"HK{m['hk']}" if m["hk"] else "  ?"
        out.append(f"   {hk:<5} {m['ma']:<8} {(m['ten'] or '?')[:34]:<34} {m['tc']} TC"
                   + ("  [bắt buộc]" if m["nhom"] == "bat_buoc" else ""))
    out.append(f"   -> {len(chon)} môn, {tc_chon} TC")

    con_du = tin_chi_toi_da - tc_chon
    tc_cai_thien, ct_chon = 0, []

    # --- (2) chỉ khi CÒN DƯ tín chỉ mới xét cải thiện ---
    if con_du <= 0:
        out += ["", f"B. CẢI THIỆN: KHÔNG còn tín chỉ trống (đã dùng hết {tc_chon}/"
                    f"{tin_chi_toi_da} TC) -> học kỳ này CHƯA nên học cải thiện."]
    else:
        ct = _q("""
            MATCH (:SinhVien {ma: $sv})-[d:DA_HOC]->(m:MonHoc)
            WHERE d.diem_so IS NOT NULL
            RETURN m.ma AS ma, m.ten AS ten, coalesce(m.tin_chi,0) AS tc,
                   d.diem_chu AS diem, d.diem_so AS ds
            ORDER BY d.diem_so ASC, m.tin_chi DESC
        """)
        for m in ct:
            if m["tc"] and tc_cai_thien + m["tc"] <= con_du:
                ct_chon.append(m)
                tc_cai_thien += m["tc"]
        out += ["", f"B. CÒN DƯ {con_du} TC -> có thể học cải thiện:"]
        if not ct_chon:
            out.append("   (không có môn nào vừa với số tín chỉ còn lại)")
        for m in ct_chon:
            out.append(f"         {m['ma']:<8} {(m['ten'] or '?')[:34]:<34} {m['tc']} TC"
                       f"  (đang {m['diem']})")
        if ct_chon:
            out.append(f"   -> {len(ct_chon)} môn, {tc_cai_thien} TC")

    # --- (3) học phí ---
    tong_tc = tc_chon + tc_cai_thien
    out += ["", f"TỔNG ĐĂNG KÝ: {tong_tc}/{tin_chi_toi_da} TC"]
    if hoc_phi_moi_tin_chi > 0:
        out.append(f"HỌC PHÍ: {tong_tc} × {hoc_phi_moi_tin_chi:,.0f} = "
                   f"{tong_tc * hoc_phi_moi_tin_chi:,.0f} VNĐ")
        if tc_cai_thien:
            out.append(f"   trong đó phần CẢI THIỆN: {tc_cai_thien} × "
                       f"{hoc_phi_moi_tin_chi:,.0f} = "
                       f"{tc_cai_thien * hoc_phi_moi_tin_chi:,.0f} VNĐ "
                       f"(khoản này là chi thêm, cân nhắc)")
    else:
        out.append("HỌC PHÍ: chưa biết mức thu mỗi tín chỉ — cần tra cứu học phí hiện hành.")
    return "\n".join(out)


def kiem_tra_dieu_kien_tot_nghiep() -> str:
    """Đối chiếu các điều kiện tốt nghiệp mà hệ thống CÓ dữ liệu, và nói rõ điều kiện nào
    KHÔNG kiểm tra được.

    QUAN TRỌNG: MyBK chỉ cho biết môn/điểm/tín chỉ và vài môn điều kiện (ngoại ngữ đầu ra,
    GDQP, GDTC). Hệ thống KHÔNG có: ngày công tác xã hội (CTXH), điểm rèn luyện, và một số
    chứng chỉ. Vì vậy TUYỆT ĐỐI không kết luận 'đủ điều kiện tốt nghiệp' — chỉ báo cáo phần
    kiểm được và liệt kê rõ phần CHƯA kiểm được để sinh viên tự xác nhận với phòng đào tạo.
    """
    # GPA + tín chỉ tích lũy
    g = _q("""
        MATCH (v:SinhVien {ma: $sv})-[d:DA_HOC]->(m:MonHoc)
        WHERE d.diem_so IS NOT NULL AND m.tin_chi > 0
        RETURN sum(d.diem_so * m.tin_chi) AS tong, sum(m.tin_chi) AS tc
    """)
    tt = _q("MATCH (v:SinhVien {ma: $sv}) RETURN v.tin_chi_tich_luy AS tl, v.tong_tin_chi_can AS can")
    out = ["ĐỐI CHIẾU ĐIỀU KIỆN TỐT NGHIỆP", "", "A. Đã kiểm tra được:"]
    if g and g[0].get("tc"):
        gpa = round(g[0]["tong"] / g[0]["tc"], 2)
        dat_gpa = "ĐẠT" if gpa >= 2.0 else "CHƯA ĐẠT"
        out.append(f"   - GPA tích lũy: {gpa}/4.0 (yêu cầu ≥ 2.0) → {dat_gpa}")
    if tt and tt[0].get("can"):
        tl, can = tt[0]["tl"] or 0, tt[0]["can"]
        pct = round(tl / can * 100) if can else 0
        out.append(f"   - Tín chỉ tích lũy: {tl}/{can} ({pct}%)")

    # môn điều kiện TN (ngoại ngữ, GDQP...)
    dk = _q("""
        MATCH (:SinhVien {ma: $sv})-[r:CO_DIEU_KIEN]->(dk:DieuKienTN)
        RETURN dk.ma AS ma, dk.ten AS ten, r.da_hoan_thanh AS xong
        ORDER BY r.da_hoan_thanh
    """)
    for d in dk:
        out.append(f"   - {d['ma']} ({d['ten'] or '?'}): "
                   + ("đã hoàn thành" if d["xong"] else "CHƯA hoàn thành"))

    # môn chưa học còn lại (đã lọc theo khối chưa đủ ngay từ crawl)
    con = _q("MATCH (:SinhVien {ma: $sv})-[:CHUA_HOC]->(m:MonHoc) RETURN count(m) AS n")
    if con and con[0]["n"]:
        out.append(f"   - Còn {con[0]['n']} môn thuộc khối CHƯA đủ cần hoàn thành")

    out += ["", "B. KHÔNG kiểm tra được (hệ thống không có dữ liệu):",
            "   - Điểm rèn luyện các học kỳ",
            "   - Một số điều kiện khác nếu ngành yêu cầu (chứng chỉ chuyên môn...)",
            "",
            "=> KHÔNG thể kết luận 'đủ điều kiện tốt nghiệp'. Các mục ở phần B bạn phải tự "
            "kiểm tra với phòng đào tạo / cố vấn học tập."]
    return "\n".join(out)


def lap_ke_hoach_dat_gpa(gpa_muc_tieu: float, tin_chi_muon: int = 0,
                         muc_tieu_mon: str = "", buoc_nhay: int = 2, diem_mon_moi: str = "",
                         muc_tron_goi: float = 0, dinh_muc_tc: int = 0, don_gia_vuot: float = 0,
                         tc_toi_thieu: int = 0, bac_giam: str = "") -> str:
    """Lập kế hoạch CẢI THIỆN GPA đạt mục tiêu, ĐÚNG số tín chỉ user muốn, kèm TỔNG học phí.

    LUẬT:
      • Coordinator PHẢI hỏi tin_chi_muon TRƯỚC. =0 -> tool nhắc hỏi.
      • LẬP ĐÚNG tin_chi_muon: MÔN MỚI theo LỘ TRÌNH HỌC KỲ (CTĐT) trước, rồi HỌC LẠI lấp cho
        ĐỦ số tín chỉ đó. Điểm nâng VỪA ĐỦ để đạt mục tiêu (KHÔNG ép kịch A nếu số TC đã đủ để
        đạt bằng điểm thấp hơn — vd không cần ĐATN 'A'); môn mới ước = tầng GPA + 2 bậc.
        User ghi 'môn X lên B+' -> override qua muc_tieu_mon.
      • Chỉ khi ĐÃ dùng hết môn (mới + học lại) mà vẫn < tin_chi_muon -> báo số TC thực xếp được.
      • Học phí: <=định mức thì áp BẬC GIẢM theo số TC (<=12: -15%, <=9: -30%, <=6: -45% trên
        gói); vượt định mức = gói + phần vượt × đơn giá. Đăng ký DƯỚI tối thiểu (mặc định 11)
        -> nhắc PHẢI nộp đơn xin đăng ký dưới mức, nếu không bị CẢNH CÁO HỌC VỤ (CCHV).

    Học phí + tối thiểu do coordinator tra HỌC PHÍ KỲ MỚI NHẤT rồi truyền vào (muc_tron_goi,
    dinh_muc_tc, don_gia_vuot, tc_toi_thieu, bac_giam='12:15,9:30,6:45'). Bậc giảm KHÔNG
    hardcode, KHÔNG lấy ở #7 — lấy từ bảng học phí mới nhất.
    """
    if tin_chi_muon <= 0:
        return ("Bạn muốn đăng ký BAO NHIÊU TÍN CHỈ kỳ này? Cho mình biết số tín chỉ để lập "
                "kế hoạch và tính học phí.")

    rows = _q("""MATCH (:SinhVien {ma: $sv})-[d:DA_HOC]->(m:MonHoc)
                 WHERE d.diem_so IS NOT NULL AND m.tin_chi > 0
                 RETURN m.ma AS ma, m.ten AS ten, m.tin_chi AS tc, d.diem_chu AS diem, d.diem_so AS ds""")
    if not rows:
        return "Graph chưa có bảng điểm."
    tong0 = sum(r["ds"] * r["tc"] for r in rows)
    tc0 = sum(r["tc"] for r in rows)
    gpa0 = round(tong0 / tc0, 2)
    if gpa0 >= gpa_muc_tieu:
        return f"GPA hiện tại {gpa0} đã đạt/vượt mục tiêu {gpa_muc_tieu} — không cần kế hoạch cải thiện."

    override = _parse_muc_tieu_mon(muc_tieu_mon)
    g_new_est = (DIEM_CHU_MAP.get(str(diem_mon_moi).strip().upper())
                 if str(diem_mon_moi).strip() else None) or _uoc_diem_mon_moi(gpa0)

    new_pool = _q("""
        MATCH (v:SinhVien {ma: $sv})-[:CHUA_HOC]->(m:MonHoc)
        WHERE NOT EXISTS {
            MATCH (m)-[:TIEN_QUYET]->(tq:MonHoc)
            WHERE NOT EXISTS { MATCH (v)-[d:DA_HOC]->(tq) WHERE d.dat = true }
        }
        RETURN m.ma AS ma, m.ten AS ten, coalesce(m.tin_chi,0) AS tc,
               coalesce(m.hoc_ky_ke_hoach, 99) AS hk
    """)
    # CUỐN CHIẾU theo học kỳ: GIỮ môn HK <= hiện tại (nợ cũ + HK hiện tại) VÀ môn KHÔNG RÕ học
    # kỳ (hk=99: ĐATN/tự chọn/môn nợ chưa gắn HK) — ĐỪNG loại oan. CHỈ loại môn có HK THẬT >
    # hiện tại (HK sau chưa tới lượt). Môn CHƯA HỌC/NỢ luôn được xếp TRƯỚC học lại (ở _thu).
    hk_ht = _hk_hien_tai()
    new_pool = [m for m in new_pool
                if m["tc"] > 0 and (m["hk"] == 99 or m["hk"] <= hk_ht)]

    # Thứ tự: NỢ HK CŨ trước (phải học bù trước) -> môn HK HIỆN TẠI -> môn không rõ HK cuối.
    def _uu_tien_hk(m):
        if m["hk"] == 99:
            return (2, 99, m["ma"])            # không rõ HK (ĐATN/tự chọn) -> sau cùng
        if m["hk"] < hk_ht:
            return (0, m["hk"], m["ma"])        # NỢ HK cũ -> trước
        return (1, m["hk"], m["ma"])            # HK hiện tại
    new_pool.sort(key=_uu_tien_hk)

    def _thu(B):
        used = 0
        new_sel, rt_sel = [], []
        for m in new_pool:                                   # MÔN MỚI trước (theo học kỳ)
            if used + m["tc"] <= B:
                new_sel.append({**m, "gia_dinh": g_new_est}); used += m["tc"]
        for r in sorted(rows, key=lambda x: (x["ds"], -x["tc"])):   # HỌC LẠI lấp đủ (thấp trước)
            if r["ds"] >= 4.0:
                continue
            if used + r["tc"] <= B:
                if r["ma"] in override and override[r["ma"]] > r["ds"]:
                    nv = override[r["ma"]]
                else:
                    mt = _muc_tieu_buoc_nhay(r["ds"], buoc_nhay)   # vừa đủ (trần bước nhảy), cap A
                    nv = mt[1] if mt else r["ds"]
                rt_sel.append({**r, "nv": nv}); used += r["tc"]
        # GPA với môn mới (ước) + học lại (nv)
        tong, tcg = tong0, tc0
        for m in new_sel:
            tong += m["gia_dinh"] * m["tc"]; tcg += m["tc"]
        for r in rt_sel:
            tong += (r["nv"] - r["ds"]) * r["tc"]

        def gpa():
            return round(tong / tcg, 2)

        # còn THIẾU -> nâng thêm tới A: học lại trước (ΔGPA lớn), rồi môn mới
        if gpa() < gpa_muc_tieu:
            for r in sorted(rt_sel, key=lambda x: -(4.0 - x["nv"]) * x["tc"]):
                if gpa() >= gpa_muc_tieu:
                    break
                if r["nv"] < 4.0:
                    tong += (4.0 - r["nv"]) * r["tc"]; r["nv"] = 4.0
            for m in new_sel:
                if gpa() >= gpa_muc_tieu:
                    break
                if m["gia_dinh"] < 4.0:
                    tong += (4.0 - m["gia_dinh"]) * m["tc"]; m["gia_dinh"] = 4.0
        return {"dat": gpa() >= gpa_muc_tieu, "new": new_sel,
                "retake": [r for r in rt_sel if r["nv"] > r["ds"]],
                "used": used, "gpa": gpa()}

    # Lập ĐÚNG tin_chi_muon. Nếu số đó (dù nâng tới A) vẫn không đạt -> nới ≤ 18 tìm số nhỏ nhất đủ.
    res = _thu(tin_chi_muon); plan_B = tin_chi_muon; noi = False; khong_dat = False
    if not res["dat"]:
        for B in range(tin_chi_muon + 1, 19):
            r = _thu(B)
            if r["dat"]:
                res, plan_B, noi = r, B, True
                break
        else:
            res, plan_B, khong_dat = _thu(18), 18, True

    out = [f"KẾ HOẠCH CẢI THIỆN GPA {gpa0} → mục tiêu {gpa_muc_tieu}:"]
    if noi:
        out.append(f"Học đủ {tin_chi_muon} TC theo kế hoạch kỳ này CHƯA đủ để lên GPA "
                   f"{gpa_muc_tieu}; đã lập kế hoạch {plan_B} TC (thêm phần cải thiện).")
    if res["new"]:
        out += ["", "• HỌC THEO KẾ HOẠCH HỌC KỲ (môn mới):"]
        for m in res["new"]:
            out.append(f"   HK{m['hk']} | {m['ma']} | {(m['ten'] or '?')[:30]} | {m['tc']} TC "
                       f"| giả định {_VAL_TO_CHU.get(m['gia_dinh'], m['gia_dinh'])}")
    if res["retake"]:
        out += ["", "• HỌC LẠI / CẢI THIỆN (lấy điểm cao nhất, không thêm tín chỉ tích lũy):"]
        for r in res["retake"]:
            out.append(f"   {r['ma']} | {(r['ten'] or '?')[:30]} | {r['tc']} TC | "
                       f"{r['diem']} → {_VAL_TO_CHU.get(r['nv'], r['nv'])}")
    if not res["new"] and not res["retake"]:
        out.append("   (Không có môn phù hợp để lập kế hoạch.)")

    out += ["", f"TỔNG ĐĂNG KÝ: {res['used']} TC | GPA {gpa0} → {res['gpa']} "
                + (" đạt mục tiêu" if res["dat"] else " CHƯA đạt")]
    if res["used"] < tin_chi_muon and not noi:
        out.append(f"   (Chỉ xếp được {res['used']}/{tin_chi_muon} TC — đã hết môn phù hợp.)")
    if khong_dat and not res["dat"]:
        out.append(f"   Tới 18 TC/kỳ vẫn chưa đạt GPA {gpa_muc_tieu} — hãy cải thiện tiếp ở "
                   f"(các) kỳ sau.")

    hp, giam = _tinh_hoc_phi(res["used"], muc_tron_goi, dinh_muc_tc, don_gia_vuot,
                             _parse_bac_giam(bac_giam))
    if hp is not None:
        if dinh_muc_tc and res["used"] > dinh_muc_tc:
            ghi = f" (gói {_tien(muc_tron_goi)} + {res['used'] - dinh_muc_tc} TC vượt × {_tien(don_gia_vuot)})"
        elif giam:
            ghi = f" (giảm {giam}% do đăng ký {res['used']} TC ở HK chính)"
        else:
            ghi = ""
        out.append(f"   HỌC PHÍ kỳ này: {_tien(hp)}{ghi}")
    else:
        out.append("   HỌC PHÍ: nhờ tra cứu mức thu (gói + định mức + đơn giá vượt) rồi tính.")
    if tc_toi_thieu and res["used"] < tc_toi_thieu:
        out.append(f"   {res['used']} TC DƯỚI mức tối thiểu {tc_toi_thieu} TC/kỳ: PHẢI nộp đơn/điền "
                   f"FORM xin đăng ký dưới mức tối thiểu, nếu không sẽ bị CẢNH CÁO HỌC VỤ (CCHV).")
    return "\n".join(out)


def lap_ke_hoach_hoc_phan_tot_nghiep(loai_hp: str = "học phần tốt nghiệp",
                                     nguong_phan_tram: float = 0, nguong_tin_chi: int = 0,
                                     gpa_toi_thieu: float = 2.0,
                                     av_bac_can: int = 0, ctxh_can: int = 0,
                                     tin_chi_toi_da: int = 25, diem_mon_moi: str = "B",
                                     buoc_nhay: int = 2, muc_tieu_mon: str = "",
                                     muc_tron_goi: float = 0, dinh_muc_tc: int = 0,
                                     don_gia_vuot: float = 0, tc_toi_thieu: int = 0,
                                     bac_giam: str = "") -> str:
    """Kiểm tra ĐIỀU KIỆN đăng ký HỌC PHẦN TỐT NGHIỆP + lập KẾ HOẠCH HỢP NHẤT một kỳ.

    Điều kiện: (1) ĐTB tích lũy >= gpa_toi_thieu (2.0); (2) đủ TÍN CHỈ TÍCH LŨY theo NGƯỠNG %
    của CTĐT (TTNT 65%, ĐACN 70%, LVTN/ĐATN 82.5% — coordinator tra #7 bơm nguong_phan_tram
    hoặc nguong_tin_chi); (3) LVTN thêm ngoại ngữ + CTXH.

    KẾ HOẠCH HỢP NHẤT (điểm mấu chốt): môn MỚI vừa +tín chỉ tích lũy VỪA nâng GPA (cộng cả tử
    lẫn mẫu). Nên: ƯU TIÊN môn mới (theo lộ trình CTĐT, giả định đạt diem_mon_moi, mặc định B)
    để đóng CẢ HAI gap; CHỈ KHI môn mới vẫn chưa kéo GPA đủ mới BÙ bằng HỌC LẠI (không thêm
    tín chỉ). Tất cả trong TRẦN tín chỉ một kỳ (tin_chi_toi_da).

    Args:
        loai_hp: tên học phần để hiển thị. nguong_phan_tram/nguong_tin_chi: ngưỡng tín chỉ.
        gpa_toi_thieu: ĐTB tối thiểu (2.0). av_bac_can/ctxh_can: chuẩn LVTN (0=bỏ qua).
        tin_chi_toi_da: trần TC một kỳ. diem_mon_moi: điểm GIẢ ĐỊNH môn mới (mặc định 'B',
            user override được). buoc_nhay: trần bậc nâng khi học lại (mặc định 2).
        muc_tieu_mon: override mục tiêu học lại từng môn 'EE2013:B+,...'.
        muc_tron_goi/dinh_muc_tc/don_gia_vuot/tc_toi_thieu: học phí + tối thiểu (coordinator bơm).
    """
    g = _q("""MATCH (:SinhVien {ma: $sv})-[d:DA_HOC]->(m:MonHoc)
              WHERE d.diem_so IS NOT NULL AND m.tin_chi > 0
              RETURN m.ma AS ma, m.ten AS ten, m.tin_chi AS tc, d.diem_chu AS diem, d.diem_so AS ds""")
    if not g:
        return "Graph chưa có bảng điểm. Chạy graph_bang_diem.py trước."
    tong = sum(r["ds"] * r["tc"] for r in g)
    tc_gpa = sum(r["tc"] for r in g)
    gpa_ht = round(tong / tc_gpa, 2)
    sv = _q("MATCH (v:SinhVien {ma: $sv}) RETURN v.tin_chi_tich_luy AS tl, v.tong_tin_chi_can AS can")
    tich_luy = sv[0]["tl"] if (sv and sv[0].get("tl") is not None) else None
    tong_can = sv[0]["can"] if (sv and sv[0].get("can")) else None
    nguong_tc = nguong_tin_chi or (math.ceil(nguong_phan_tram * tong_can)
                                   if (nguong_phan_tram and tong_can) else 0)
    ctxh = doc_ctxh(_khoa()) if ctxh_can else None
    av_max = 0
    if av_bac_can:
        a = _q("""MATCH (:SinhVien {ma: $sv})-[r:CHUAN_NGOAI_NGU]->(:NgoaiNgu)
                  WHERE r.dat = true AND r.bac IS NOT NULL RETURN max(r.bac) AS b""")
        av_max = a[0]["b"] if (a and a[0].get("b") is not None) else 0

    out = [f"ĐIỀU KIỆN ĐĂNG KÝ {loai_hp.upper()}:"]
    du = True
    if gpa_ht >= gpa_toi_thieu:
        out.append(f"  • ĐTB tích lũy: {gpa_ht} ≥ {gpa_toi_thieu}")
    else:
        out.append(f"  • ĐTB tích lũy: {gpa_ht} < {gpa_toi_thieu} — CHƯA đạt"); du = False
    if nguong_tc:
        gt = (f" ({int(round(nguong_phan_tram*100))}% × {tong_can})"
              if nguong_phan_tram and tong_can and not nguong_tin_chi else "")
        if tich_luy is None:
            out.append(f"  • Tín chỉ tích lũy: CHƯA đọc được (cần ≥ {nguong_tc}{gt})"); du = False
        elif tich_luy >= nguong_tc:
            out.append(f"  • Tín chỉ tích lũy: {tich_luy} ≥ {nguong_tc}{gt}")
        else:
            out.append(f"  • Tín chỉ tích lũy: {tich_luy}/{nguong_tc}{gt} — THIẾU {nguong_tc-tich_luy} TC")
            du = False
    else:
        out.append("  • Tín chỉ tích lũy: (chưa có ngưỡng — coordinator tra #7 lấy % của loại này)")
    if av_bac_can:
        if av_max >= av_bac_can:
            out.append(f"  • Ngoại ngữ: đạt bậc {av_max} (cần {av_bac_can})")
        else:
            out.append(f"  • Ngoại ngữ: bậc {av_max}/{av_bac_can} — cần đạt Anh văn {av_bac_can}"); du = False
    if ctxh_can:
        if ctxh is not None and ctxh >= ctxh_can:
            out.append(f"  • CTXH: {ctxh} ≥ {ctxh_can} ngày")
        else:
            out.append(f"  • CTXH: {ctxh if ctxh is not None else '?'}/{ctxh_can} ngày — chưa đủ"); du = False

    out.append("")
    if du:
        out.append(f"=> ĐỦ điều kiện đăng ký {loai_hp}.")
        return "\n".join(out)
    out.append(f"=> CHƯA đủ. KẾ HOẠCH một kỳ (môn mới nâng CẢ tín chỉ lẫn GPA; học lại chỉ bù "
               f"GPA nếu còn thiếu):")

    v_new = DIEM_CHU_MAP.get(str(diem_mon_moi).strip().upper(), 3.0)
    override = _parse_muc_tieu_mon(muc_tieu_mon)
    tl = tich_luy if tich_luy is not None else 0
    tong2, tcg2, tl2, used = tong, tc_gpa, tl, 0

    def con_thieu():
        thieu_tc = bool(nguong_tc) and tl2 < nguong_tc
        thieu_gpa = round(tong2 / tcg2, 2) < gpa_toi_thieu
        return thieu_tc, thieu_gpa

    # 1) MÔN MỚI: đóng gap tín chỉ + kéo GPA (mỗi môn +tín chỉ và +điểm giả định)
    new_pool = _q("""
        MATCH (v:SinhVien {ma: $sv})-[:CHUA_HOC]->(m:MonHoc)
        WHERE NOT EXISTS {
            MATCH (m)-[:TIEN_QUYET]->(tq:MonHoc)
            WHERE NOT EXISTS { MATCH (v)-[d:DA_HOC]->(tq) WHERE d.dat = true }
        }
        RETURN m.ma AS ma, m.ten AS ten, coalesce(m.tin_chi,0) AS tc,
               coalesce(m.hoc_ky_ke_hoach, 99) AS hk
        ORDER BY coalesce(m.hoc_ky_ke_hoach, 99), m.ma
    """)
    chon_moi = []
    for m in new_pool:
        thieu_tc, thieu_gpa = con_thieu()
        if not thieu_tc and not thieu_gpa:
            break
        if m["tc"] <= 0 or used + m["tc"] > tin_chi_toi_da:
            continue
        tong2 += v_new * m["tc"]; tcg2 += m["tc"]; tl2 += m["tc"]; used += m["tc"]
        chon_moi.append(m)

    # 2) HỌC LẠI: chỉ khi GPA VẪN thiếu (không thêm tín chỉ tích lũy)
    chon_lai = []
    cand = []
    for r in g:
        val = override.get(r["ma"])
        if val is None:
            mt = _muc_tieu_buoc_nhay(r["ds"], buoc_nhay)
            if mt:
                val = mt[1]
        if val and val > r["ds"]:
            cand.append({**r, "val": val, "chu": _VAL_TO_CHU.get(val, str(val))})
    cand.sort(key=lambda x: (-(x["val"] - x["ds"]) * x["tc"], x["ds"]))
    for r in cand:
        if round(tong2 / tcg2, 2) >= gpa_toi_thieu:
            break
        if used + r["tc"] > tin_chi_toi_da:
            continue
        tong2 += (r["val"] - r["ds"]) * r["tc"]; used += r["tc"]
        chon_lai.append(r)

    gpa2 = round(tong2 / tcg2, 2)
    # trình bày
    if chon_moi:
        out += ["", f"• MÔN MỚI (giả định đạt {str(diem_mon_moi).strip().upper()}):"]
        for m in chon_moi:
            out.append(f"   HK{m['hk']} | {m['ma']} | {(m['ten'] or '?')[:30]} | {m['tc']} TC")
        out.append(f"   -> {len(chon_moi)} môn, {sum(m['tc'] for m in chon_moi)} TC")
    if chon_lai:
        out += ["", "• HỌC LẠI (bù GPA — không thêm tín chỉ tích lũy):"]
        for r in chon_lai:
            out.append(f"   {r['ma']} | {(r['ten'] or '?')[:30]} | {r['tc']} TC | {r['diem']} → {r['chu']}")
    if not chon_moi and not chon_lai:
        out.append("   (Không còn môn mới/môn học lại phù hợp trong trần tín chỉ — chia thêm kỳ.)")

    tc_line = f"{tl2}/{nguong_tc}" if nguong_tc else f"{tl2}"
    ok_tc = (not nguong_tc) or tl2 >= nguong_tc
    out += ["", f"SAU KỲ: tín chỉ tích lũy {tc_line} " + ("" if ok_tc else "")
                + f" | GPA {gpa_ht} → {gpa2} " + ("" if gpa2 >= gpa_toi_thieu else "")
                + f" | đăng ký {used} TC"]
    if (not ok_tc) or gpa2 < gpa_toi_thieu:
        out.append("   Chưa đủ trong 1 kỳ (kịch trần tín chỉ) — cần chia thêm kỳ.")
    hp, giam = _tinh_hoc_phi(used, muc_tron_goi, dinh_muc_tc, don_gia_vuot,
                             _parse_bac_giam(bac_giam))
    if hp is not None:
        if dinh_muc_tc and used > dinh_muc_tc:
            ghi = f" (trọn gói {_tien(muc_tron_goi)} + {used - dinh_muc_tc} TC vượt × {_tien(don_gia_vuot)})"
        elif giam:
            ghi = f" (giảm {giam}% do đăng ký {used} TC)"
        else:
            ghi = ""
        out.append(f"   HỌC PHÍ kỳ này: {_tien(hp)}{ghi}")
    if tc_toi_thieu and used < tc_toi_thieu:
        out.append(f"   {used} TC DƯỚI tối thiểu {tc_toi_thieu} TC/kỳ: PHẢI nộp đơn xin đăng ký "
                   f"dưới mức tối thiểu, nếu không sẽ bị CẢNH CÁO HỌC VỤ (CCHV).")
    if av_bac_can and av_max < av_bac_can:
        out.append(f"   • Còn cần đạt Anh văn bậc {av_bac_can} (đang {av_max}).")
    if ctxh_can and (ctxh is None or ctxh < ctxh_can):
        out.append(f"   • Còn cần đủ {ctxh_can} ngày CTXH (đang {ctxh if ctxh is not None else '?'}).")
    return "\n".join(out)


def mo_phong_cai_thien(ma_mon_va_diem: str) -> str:
    """Tính GPA MỚI nếu học lại các môn với điểm dự kiến — KHÔNG tự tính nhẩm.

    Args:
        ma_mon_va_diem: Danh sách 'mã môn=điểm chữ' cách nhau bằng ';'.
            Vd: 'MT1003=A; PH1003=B+'
    """
    thay_doi = {}
    for phan in str(ma_mon_va_diem).split(";"):
        if "=" not in phan:
            continue
        ma, _, dc = phan.partition("=")
        ma, dc = ma.strip().upper(), dc.strip().upper()
        if not ma:
            continue
        if dc not in DIEM_CHU_MAP:
            return f"Điểm chữ '{dc}' không hợp lệ. Chỉ nhận: {', '.join(DIEM_CHU_MAP)}."
        thay_doi[ma] = dc
    if not thay_doi:
        return "Không đọc được cặp nào. Định dạng: 'MT1003=A; PH1003=B+'."

    rows = _q("""
        MATCH (:SinhVien {ma: $sv})-[d:DA_HOC]->(m:MonHoc)
        WHERE d.diem_so IS NOT NULL AND m.tin_chi > 0
        RETURN m.ma AS ma, m.tin_chi AS tc, d.diem_so AS ds, d.diem_chu AS dc
    """)
    if not rows:
        return "Graph chưa có bảng điểm."

    tong_cu = sum(x["ds"] * x["tc"] for x in rows)
    tc_tong = sum(x["tc"] for x in rows)
    tong_moi, tc_doi, canh_bao, thay_duoc = tong_cu, 0, [], set()
    for x in rows:
        if x["ma"] in thay_doi:
            thay_duoc.add(x["ma"])
            moi = DIEM_CHU_MAP[thay_doi[x["ma"]]]
            if moi <= x["ds"]:
                canh_bao.append(f"{x['ma']} (điểm mới {thay_doi[x['ma']]} không cao hơn "
                                f"điểm hiện tại {x['dc']})")
            tong_moi += (moi - x["ds"]) * x["tc"]
            tc_doi += x["tc"]

    gpa_cu, gpa_moi = round(tong_cu / tc_tong, 2), round(tong_moi / tc_tong, 2)
    out = [f"GPA cũ: {gpa_cu} → GPA mới: {gpa_moi} (chênh {gpa_moi - gpa_cu:+.2f})",
           f"Tín chỉ bị thay đổi: {tc_doi}/{tc_tong}"]
    thieu = set(thay_doi) - thay_duoc
    if thieu:
        out.append(f"CẢNH BÁO: không có trong bảng điểm: {', '.join(sorted(thieu))}")
    if canh_bao:
        out.append("LƯU Ý: cải thiện không hiệu quả: " + "; ".join(canh_bao))
    return "\n".join(out)


def tinh_chi_phi_hoc_lai(ma_mon: str, hoc_phi_moi_tin_chi: float) -> str:
    """Tính tổng chi phí học lại các môn.

    Args:
        ma_mon: Danh sách mã môn cách nhau bằng ';'. Vd: 'MT1003; PH1003'
        hoc_phi_moi_tin_chi: Học phí một tín chỉ (VNĐ). Chưa biết thì hỏi agent
            tra cứu học vụ lấy mức thu hiện hành, ĐỪNG tự bịa.
    """
    if hoc_phi_moi_tin_chi < 0:
        return "Học phí không được âm."
    ma_list = [x.strip().upper() for x in str(ma_mon).split(";") if x.strip()]
    if not ma_list:
        return "Chưa nêu mã môn."
    r = _q("MATCH (m:MonHoc) WHERE m.ma IN $ds RETURN m.ma AS ma, m.tin_chi AS tc", ds=ma_list)
    if not r:
        return f"Không tìm thấy môn nào trong: {', '.join(ma_list)}"
    tong_tc = sum(x["tc"] or 0 for x in r)
    dong = [f"  {x['ma']}: {x['tc']} TC × {hoc_phi_moi_tin_chi:,.0f} = "
            f"{(x['tc'] or 0) * hoc_phi_moi_tin_chi:,.0f} VNĐ" for x in r]
    thieu = set(ma_list) - {x["ma"] for x in r}
    if thieu:
        dong.append(f"  (không tìm thấy: {', '.join(sorted(thieu))})")
    return "\n".join(dong) + f"\nTỔNG: {tong_tc} TC → {tong_tc * hoc_phi_moi_tin_chi:,.0f} VNĐ"


# ---------------- AGENT ----------------
def tao_gpa_agent(output_schema=None) -> Agent:
    """Tạo agent tư vấn GPA. Để output_schema=None khi dùng làm thành viên team."""
    return Agent(
        name="GPA Advisor",
        id="gpa_advisor",
        role="Phân tích bảng điểm cá nhân, tư vấn cải thiện GPA và lộ trình học",
        model=OpenAIChat(id=GPA_MODEL, temperature=1, seed=42),
        tools=[tinh_gpa_hien_tai, liet_ke_mon_cai_thien, lap_ke_hoach_dat_gpa,
               mon_chua_hoc, mon_du_dieu_kien_dang_ky,
               mon_chua_du_dieu_kien, xem_quan_he_mon, mon_bi_chan_boi,
               lap_ke_hoach_hoc_ky,
               kiem_tra_len_trinh_do, lap_ke_hoach_bu,
               lap_ke_hoach_hoc_phan_tot_nghiep,
               mo_phong_cai_thien, tinh_chi_phi_hoc_lai,
               kiem_tra_dieu_kien_tot_nghiep],
        description="Truy vấn knowledge graph: bảng điểm cá nhân + quan hệ tiên quyết môn học.",
        instructions=[
            "LUÔN dùng tool để lấy số liệu và tính toán. TUYỆT ĐỐI không tự tính nhẩm GPA — "
            "kết luận phải khớp kết quả tool trả về.",
            "CẢI THIỆN GPA (Cơ chế 2): khi user nêu mục tiêu GPA ('muốn lên 3.0') -> lập kế "
            "hoạch bằng lap_ke_hoach_dat_gpa. NHƯNG PHẢI có tin_chi_muon TRƯỚC: nếu user chưa "
            "nói số tín chỉ muốn đăng ký kỳ này thì HỎI 'bạn muốn đăng ký bao nhiêu tín chỉ kỳ "
            "này?' rồi mới gọi tool. Mặc định nâng mỗi môn theo trần bước nhảy; nếu user ghi "
            "'môn X lên B+' thì truyền muc_tieu_mon='X:B+'. HỌC PHÍ (mức trọn gói, định mức TC, "
            "đơn giá vượt, BẢNG BẬC GIẢM khi đăng ký ít TC) + TÍN CHỈ TỐI THIỂU do coordinator "
            "tra HỌC PHÍ KỲ MỚI NHẤT rồi truyền vào: muc_tron_goi, dinh_muc_tc, don_gia_vuot, "
            "tc_toi_thieu, bac_giam (chuỗi 'ngưỡngTC:phần_trăm', vd '12:15,9:30,6:45'). KHÔNG "
            "tự bịa; bậc giảm lấy từ BẢNG HỌC PHÍ mới nhất, KHÔNG lấy ở #7.",
            "Tư vấn cải thiện chung (không nêu mục tiêu GPA): (1) tinh_gpa_hien_tai; "
            "(2) liet_ke_mon_cai_thien (đã kèm ΔGPA mỗi môn); (3) mo_phong_cai_thien thử phương "
            "án. Ưu tiên theo ΔGPA (mức tăng điểm × tín chỉ), KHÔNG theo số tín chỉ đơn thuần.",
            "THỨ TỰ ƯU TIÊN khi lập kế hoạch học kỳ: PHẢI học hết các môn CHƯA HỌC theo kế "
            "hoạch giảng dạy (kể cả môn nợ kỳ trước) TRƯỚC; CHỈ KHI còn dư tín chỉ mới đề "
            "xuất học cải thiện. Dùng lap_ke_hoach_hoc_ky, đừng tự sắp xếp.",
            "LUÔN tính HỌC PHÍ khi đề xuất học lại/cải thiện — sinh viên cần biết tốn bao "
            "nhiêu. Chưa biết mức thu mỗi tín chỉ thì hỏi agent tra cứu học vụ lấy mức hiện "
            "hành rồi truyền vào tool, TUYỆT ĐỐI không bịa số.",
            "Ưu tiên môn nhiều tín chỉ + điểm thấp (tác động GPA lớn nhất).",
            "PHÂN BIỆT RÕ 3 quan hệ: 'Tiên quyết' = BẮT BUỘC học xong trước (chưa đạt thì "
            "KHÔNG đăng ký được môn sau); 'Song hành' = học cùng kỳ hoặc trước; 'Khuyến nghị' "
            "= chỉ là lời khuyên, KHÔNG bắt buộc. Đừng gộp lẫn ba loại.",
            "Định tuyến tool: 'kỳ tới học được môn gì' -> mon_du_dieu_kien_dang_ky; "
            "'còn vướng môn nào' -> mon_chua_du_dieu_kien; 'rớt môn X ảnh hưởng gì' -> "
            "mon_bi_chan_boi; 'môn Y cần học trước gì' -> xem_quan_he_mon.",
            "Cần học phí mỗi tín chỉ mà chưa biết thì nói rõ cần tra cứu học phí, ĐỪNG bịa số.",
            "Hỏi 'đủ điều kiện tốt nghiệp chưa' -> kiem_tra_dieu_kien_tot_nghiep. TUYỆT ĐỐI "
            "KHÔNG kết luận 'đủ điều kiện' — hệ thống THIẾU dữ liệu về CTXH, rèn luyện và một "
            "số chứng chỉ. Luôn nêu rõ phần đã kiểm được và phần KHÔNG kiểm được, yêu cầu sinh "
            "viên xác nhận với phòng đào tạo.",
            "PHÂN BIỆT 3 tool dễ nhầm về môn chưa học: hỏi 'MÔN NÀO CHƯA HỌC' (chung) -> "
            "mon_chua_hoc (mọi môn chưa học). Hỏi 'kỳ tới ĐĂNG KÝ ĐƯỢC môn gì' -> "
            "mon_du_dieu_kien_dang_ky (đủ tiên quyết). Hỏi 'còn VƯỚNG môn gì, vì sao' -> "
            "mon_chua_du_dieu_kien (thiếu tiên quyết). ĐỪNG dùng lẫn.",
            "LUẬT CẤP NĂM (rất quan trọng): cấp của môn = CHỮ SỐ ĐẦU sau cụm chữ trong mã "
            "(MT1003=năm1, EE2015=năm2). SV trình độ năm L CHỈ đăng ký được môn cấp <= L "
            "(N1: mã 1; N2: mã 1,2; N3: 1,2,3; N4: 1,2,3,4). Anh văn (LA...) là NGOẠI LỆ — "
            "theo chuỗi tiên quyết riêng, KHÔNG bị chặn theo cấp năm. Khi biết trình độ hiện "
            "tại của SV thì truyền cap_toi_da vào mon_du_dieu_kien_dang_ky (và cap_hien_tai "
            "vào mon_chua_du_dieu_kien) để lọc đúng — đừng liệt kê môn năm cao hơn trình độ.",
            "LÊN TRÌNH ĐỘ / 'muốn học môn cấp K': ngưỡng (tín chỉ tích lũy, bậc Anh văn, CTXH) "
            "NẰM Ở BẢNG CHUẨN #7 — coordinator/agent tra học vụ phải lấy rồi TRUYỀN VÀO tool, "
            "KHÔNG tự bịa. Muốn học môn CẤP K -> cần đạt chuẩn năm K: gọi kiem_tra_len_trinh_do "
            "(cap_muc_tieu=K, nguong_tin_chi, av_bac_can, ctxh_can) để biết còn thiếu gì; nếu "
            "chưa đủ, gọi lap_ke_hoach_bu (cap_hien_tai=trình độ hiện tại, nguong_tin_chi, "
            "cap_muc_tieu=K) để lập kế hoạch gom tín chỉ bằng MÔN CẤP <= trình độ hiện tại. "
            "Nói rõ: đủ các mục này -> lên năm K -> KHI ĐÓ mới đăng ký được môn cấp K.",
            "ĐĂNG KÝ HỌC PHẦN TỐT NGHIỆP (TTNT/ĐACN/LVTN/ĐATN): hỏi 'đủ điều kiện đăng ký "
            "[ĐATN/thực tập ngoài trường/đồ án chuyên ngành] chưa' -> lap_ke_hoach_hoc_phan_tot_nghiep. "
            "Điều kiện: GPA ≥ 2.0 VÀ tín chỉ tích lũy ≥ % của CTĐT (coordinator tra #7 rồi truyền "
            "nguong_phan_tram: TTNT 0.65, ĐACN 0.70, LVTN/ĐATN 0.825; LVTN thêm av_bac_can + "
            "ctxh_can). Nếu GPA < 2.0 thì đây là ĐIỀU KIỆN CHẶN — tool TỰ ĐỘNG lập kế hoạch cải "
            "thiện GPA lên 2.0 (nên truyền kèm tin_chi_muon + học phí muc_tron_goi/dinh_muc_tc/"
            "don_gia_vuot + tc_toi_thieu để phần cải thiện đầy đủ). Thiếu tín chỉ -> tool gom môn "
            "theo lộ trình.",
            "Trả lời tiếng Việt, ngắn gọn, nêu rõ GPA hiện tại → GPA dự kiến và danh sách môn.",
        ],
        output_schema=output_schema,
        markdown=True,
        add_history_to_context=True,
                num_history_runs=3,
    )


gpa_agent = tao_gpa_agent()

if __name__ == "__main__":
    gpa_agent.print_response(
        "GPA hiện tại của em bao nhiêu, kỳ tới em đăng ký được môn nào?",
        stream=True,
    )