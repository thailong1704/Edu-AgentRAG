#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
graph_bang_diem.py — Dựng KNOWLEDGE GRAPH cho bảng điểm sinh viên (Neo4j).

Điểm mấu chốt: graph KHÔNG chỉ chứa bảng điểm. Nếu chỉ có "sinh viên -> môn đã học"
thì graph tương đương JSON (query 1 hop, không có gì để đi theo cạnh).
Giá trị thật đến từ việc NỐI bảng điểm với QUAN HỆ TIÊN QUYẾT lấy từ CTĐT:

    (SinhVien)-[:DA_HOC {diem_chu, dat}]->(MonHoc)
    (SinhVien)-[:CHUA_HOC]->(MonHoc)
    (MonHoc)-[:TIEN_QUYET]->(MonHoc)     "môn A yêu cầu học xong B trước"
    (MonHoc)-[:SONG_HANH]->(MonHoc)
    (MonHoc)-[:KHUYEN_NGHI]->(MonHoc)

Khi đó trả lời được những câu mà JSON/vector bó tay:
    - Kỳ tới đủ điều kiện đăng ký môn nào? (mọi tiên quyết đã đạt)
    - Rớt môn X thì kéo theo môn nào không học được?
    - Muốn học môn Y còn phải qua những môn nào?

Nguồn dữ liệu:
    ket_qua.json      <- crawl_bang_diem.py  (bảng điểm cá nhân)
    chunks_ctdt.json  <- pipeline CTĐT       (quan hệ tiên quyết)

Chạy:  python graph_bang_diem.py
"""
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASS", "12345678")

BANG_DIEM_FILE = os.getenv("BANG_DIEM_FILE", r"C:\Users\ADMIN\Desktop\agent_A\data\KnowledgeGraph\agent_GPA\data\test.nam2\ket_qua.json")
CTDT_FILE = os.getenv("CTDT_FILE", "chunks_ctdt.json")
# Chỉ nạp quan hệ của đúng chương trình sinh viên đang học (vd 'KTDTVT_2022').
# Để None = nạp mọi chương trình (graph to hơn nhưng vẫn đúng vì lọc theo mã môn).
MA_CTDT = os.getenv("MA_CTDT") or None

# Đặt RECREATE_DB=1 để DROP & tạo lại cả database mỗi lần chạy (sạch HOÀN TOÀN, kể cả
# danh sách property keys cũ như 'code'/'credits'/'name' còn sót từ Agent_core).
# Chỉ hiệu lực trên Neo4j Enterprise; Community sẽ tự bỏ qua và chỉ xóa node/index.
RECREATE_DB = os.getenv("RECREATE_DB", "0") == "1"

# "dat" = QUA MÔN (đủ điều kiện làm tiên quyết cho môn sau), KHÔNG phải "điểm khá".
# Chỉ F (điểm 0) là rớt; C/D vẫn qua môn, chỉ là điểm thấp nên ưu tiên cải thiện.
DIEM_RUOT = {"F"}     # điểm chữ coi là RỚT (chưa qua môn)
DIEM_CHU_MAP = {"A+": 4.0, "A": 4.0, "B+": 3.5, "B": 3.0,
                "C+": 2.5, "C": 2.0, "D+": 1.5, "D": 1.0, "F": 0.0}

# ---------------- regex parse quan hệ từ CTĐT ----------------
RE_MON = re.compile(r"Môn học:\s*(.+?)\s*\(mã\s*([A-Z]{2}\d{4})\),\s*(\d+)\s*tín chỉ")
RE_MA = re.compile(r"[A-Z]{2}\d{4}")
RE_QUAN_HE = {
    "TIEN_QUYET": re.compile(r"Tiên quyết[^:]*:\s*([^.]+)"),
    "SONG_HANH": re.compile(r"Song hành[^:]*:\s*([^.]+)"),
    "KHUYEN_NGHI": re.compile(r"Khuyến nghị học trước:\s*([^.]+)"),
}


# ---------------- đọc dữ liệu ----------------
def doc_sinh_vien(data: Dict[str, Any]) -> Dict[str, Any]:
    """Trích thông tin sinh viên từ các dòng text trong ket_qua.json."""
    sv = {"ma": "", "ten": "", "nganh": "", "gpa": 0.0, "tc_tich_luy": 0, "tc_can": 0}
    for item in data.get("chuong_trinh_dao_tao", []) or []:
        s = str(item)
        if s.startswith("Họ và tên:"):
            sv["ten"] = s.split(":", 1)[1].strip()
        elif s.startswith("Mã sinh viên:"):
            sv["ma"] = s.split(":", 1)[1].strip()
        elif s.startswith("Số tín chỉ tích lũy:"):
            try:
                a, b = s.split(":", 1)[1].strip().split("/")
                sv["tc_tich_luy"], sv["tc_can"] = int(a), int(b)
            except (ValueError, IndexError):
                pass
        elif s.startswith("Điểm TBTL hệ 4:"):
            try:
                sv["gpa"] = float(s.split(":")[1].split("/")[0].strip())
            except (ValueError, IndexError):
                pass
    for item in data.get("thong_tin_sinh_vien", []) or []:
        s = str(item)
        if s.startswith("Ngành:"):
            sv["nganh"] = s.split(":", 1)[1].strip()
    return sv


def doc_mon_hoc(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Gom tất cả môn từ 3 nhóm, gắn cờ đã học / đạt."""
    nhom = data.get("mon_hoc_theo_nhom", {}) or {}
    ds = []
    for ten_nhom in ("da_hoc", "duoi_C", "tu_C+", "mon_can_hoc"):  # đọc cả cấu trúc cũ lẫn mới
        for m in nhom.get(ten_nhom, []) or []:
            if not isinstance(m, dict):
                continue
            dc = str(m.get("diem_chu", "")).strip()
            diem_so = DIEM_CHU_MAP.get(dc)
            try:
                tc = int(m.get("tin_chi", 0) or 0)
            except (TypeError, ValueError):
                tc = 0
            ds.append({
                "ma": str(m.get("ma_mon_hoc", "")).strip(),
                "ten": str(m.get("ten_mon_hoc", "")).strip(),
                "tin_chi": tc,
                "diem_chu": dc if diem_so is not None else None,
                "diem_so": diem_so,
                "da_hoc": diem_so is not None,
                "dat": (dc not in DIEM_RUOT) if dc in DIEM_CHU_MAP else False,
                "nhom": ten_nhom,
                "khoi": str(m.get("khoi", "")).strip(),   # khối kiến thức (từ tiêu đề bảng)
            })
    return [m for m in ds if m["ma"]]


def doc_dieu_kien_tn(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Đọc nhóm môn điều kiện tốt nghiệp (đạt/chưa) — ngoại ngữ đầu ra, GDQP, GDTC..."""
    nhom = data.get("mon_hoc_theo_nhom", {}) or {}
    ds = []
    for m in nhom.get("dieu_kien_tot_nghiep", []) or []:
        if not isinstance(m, dict):
            continue
        ma = str(m.get("ma_mon_hoc", "")).strip()
        if not ma:
            continue
        dc = str(m.get("diem_chu", "")).strip()
        ds.append({
            "ma": ma,
            "ten": str(m.get("ten_mon_hoc", "")).strip(),
            "da_hoan_thanh": bool(m.get("da_hoan_thanh", dc not in ("--", ""))),
        })
    return ds


def doc_ngoai_ngu(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Đọc nhóm Anh văn (chuẩn ngoại ngữ theo năm: Anh văn 1..4). Tách riêng khỏi MonHoc:
       Anh văn KHÔNG tính GPA và KHÔNG là tiên quyết môn thường — nhưng 'đạt Anh văn N' là
       điều kiện lên trình độ năm (SV_N2/N3/N4). Giữ cả môn CHƯA học để biết còn thiếu bậc nào."""
    nhom = data.get("mon_hoc_theo_nhom", {}) or {}
    ds = []
    for m in nhom.get("ngoai_ngu", []) or []:
        if not isinstance(m, dict):
            continue
        ma = str(m.get("ma_mon_hoc", "")).strip()
        if not ma:
            continue
        dc = str(m.get("diem_chu", "")).strip()
        ds.append({
            "ma": ma,
            "ten": str(m.get("ten_mon_hoc", "")).strip(),
            "bac": m.get("bac"),                                  # 1..4 hoặc None
            "tin_chi": m.get("tin_chi") or 0,                     # để kế hoạch tính +TC
            "diem_chu": dc if dc in DIEM_CHU_MAP else None,
            "dat": bool(m.get("dat", dc in DIEM_CHU_MAP and dc != "F")),
        })
    return ds


def _norm(s: str) -> str:
    """Chuẩn hóa chuỗi để so khớp ngành (bỏ dấu, thường, gộp khoảng trắng)."""
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


def resolve_ma_ctdt(data: Dict[str, Any], ctdt_path: str) -> Optional[str]:
    """Tự tìm ma_ctdt (vd 'KTDTVT_2022') từ NGÀNH + KHÓA trong ket_qua.json,
    đối chiếu với chunks_ctdt.json. KHÔNG cần set env thủ công.

    Ưu tiên chương trình KHÔNG phải 'Song ngành' để tránh chọn nhầm.
    """
    # lấy tên ngành + khóa từ ket_qua.json
    nganh = khoa = None
    for s in data.get("thong_tin_sinh_vien", []) or []:
        s = str(s)
        if s.startswith("Ngành:"):
            nganh = s.split(":", 1)[1].split(" - DH")[0].strip()
        if "Áp dụng từ năm" in s:
            m = re.search(r"(20\d{2})", s)
            if m:
                khoa = int(m.group(1))
    if not khoa:
        for s in data.get("thong_tin_sinh_vien", []) or []:
            m = re.search(r"_(20\d{2})\b", str(s))
            if m:
                khoa = int(m.group(1)); break
    if not nganh:
        return None

    try:
        chunks = json.load(open(ctdt_path, encoding="utf-8"))
    except Exception:
        return None

    nganh_n = _norm(nganh)
    ung_vien = {}
    for c in chunks:
        ex = (c.get("metadata", {}) or {}).get("extra", {}) or {}
        mc, ng, kh = ex.get("ma_ctdt"), _norm(ex.get("nganh")), ex.get("khoa")
        if not mc or not ng:
            continue
        if khoa and str(kh) != str(khoa):
            continue
        # khớp CHÍNH XÁC tên ngành, loại 'song nganh'
        if ng == nganh_n and "song nganh" not in ng:
            ung_vien[mc] = ung_vien.get(mc, 0) + 1
    if ung_vien:
        best = max(ung_vien, key=ung_vien.get)
        print(f"[graph] tự map ngành '{nganh}' khóa {khoa} -> ma_ctdt = {best}")
        return best
    print(f"[graph] KHÔNG map được ngành '{nganh}' khóa {khoa} -> nạp TẤT CẢ chương trình "
          f"(có thể lẫn tiên quyết giữa các ngành)")
    return None


def doc_ctdt(path: str, ma_ctdt: Optional[str] = None) -> tuple:
    """Đọc CTĐT -> (thuộc tính môn, quan hệ).

    Format MỚI có sẵn field trong metadata (ma_mon, ten_mon, tin_chi, hoc_ky, loai_nhom,
    tien_quyet) -> ưu tiên đọc field vì chính xác và đầy đủ hơn parse regex từ text.
    Vẫn giữ regex làm phương án dự phòng cho format CŨ.
    """
    try:
        chunks = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print(f"[graph] KHÔNG đọc được CTĐT '{path}' ({type(e).__name__}) "
              f"-> graph sẽ KHÔNG có tiên quyết / kế hoạch học kỳ")
        return {}, []

    mon, canh, seen = {}, [], set()
    for c in chunks:
        md = c.get("metadata", {}) or {}
        ex = md.get("extra", {}) or {}          # phòng khi đã qua normalize
        md = {**ex, **md}
        if ma_ctdt and str(md.get("ma_ctdt", "")) != ma_ctdt:
            continue
        text = c.get("text", "") or ""

        # --- thuộc tính môn ---
        ma = str(md.get("ma_mon") or "").strip().upper()
        if not ma:
            m = RE_MON.search(text)
            if m:
                ma = m.group(2)
        if not ma:
            continue
        if ma not in mon:
            mon[ma] = {
                "ma": ma,
                "ten": md.get("ten_mon") or "",
                "tin_chi": md.get("tin_chi") or 0,
                "hoc_ky": md.get("hoc_ky"),          # học kỳ theo KẾ HOẠCH giảng dạy (1..8)
                "loai_nhom": md.get("loai_nhom") or "",   # bat_buoc / tu_chon
            }

        # --- quan hệ: ưu tiên BA FIELD riêng trong metadata (chính xác, tách bạch
        #     tiên quyết / song hành / khuyến nghị). Regex text chỉ là dự phòng cho
        #     format cũ, vì text gộp nhãn nên dễ hiểu nhầm 'khuyến nghị' thành 'tiên quyết'.
        FIELD_MAP = {"tien_quyet": "TIEN_QUYET",
                     "song_hanh": "SONG_HANH",
                     "khuyen_nghi": "KHUYEN_NGHI"}
        co_field = False
        for field, loai in FIELD_MAP.items():
            ds = md.get(field)
            if not isinstance(ds, list) or not ds:
                continue
            co_field = True
            for den in ds:
                den = str(den).strip().upper()
                key = (ma, loai, den)
                if den and den != ma and key not in seen:
                    seen.add(key)
                    canh.append({"tu": ma, "loai": loai, "den": den})
        if not co_field:                       # format cũ -> parse từ text
            for loai, pat in RE_QUAN_HE.items():
                mm = pat.search(text)
                if not mm:
                    continue
                for den in RE_MA.findall(mm.group(1)):
                    key = (ma, loai, den)
                    if den != ma and key not in seen:
                        seen.add(key)
                        canh.append({"tu": ma, "loai": loai, "den": den})
    return mon, canh


# ---------------- ghi vào Neo4j ----------------
class GraphBuilder:
    def __init__(self, uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASS):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def tao_lai_database(self, db: str = "neo4j"):
        """DROP rồi CREATE lại database -> sạch HOÀN TOÀN, kể cả danh sách property keys
        (thứ mà DETACH DELETE không dọn được). Chỉ chạy được trên Neo4j Enterprise hoặc
        khi có quyền quản trị system DB; Community Edition sẽ báo lỗi và ta bỏ qua.
        """
        try:
            with self.driver.session(database="system") as s:
                s.run(f"CREATE OR REPLACE DATABASE `{db}` WAIT")
            print(f"[reset] đã tạo lại database '{db}' (sạch hoàn toàn)")
        except Exception as e:
            print(f"[reset] không drop được database ({type(e).__name__}) — "
                  f"có thể là Neo4j Community. Vẫn xóa node/index như thường.")

    def xoa_het(self):
        """Xóa SẠCH để dựng lại từ đầu mỗi lần chạy (giống Agent_core trước đây).

        Ngoài node/cạnh, còn xóa index/constraint cũ. Điều này gỡ bỏ tàn dư schema
        từ các lần nạp trước (vd 'code', 'credits', 'name'... của Agent_core v1).

        LƯU Ý: Neo4j vẫn GIỮ TÊN property key trong danh sách metadata kể cả khi không
        node nào dùng — đó là hạn chế của Neo4j, DETACH DELETE không dọn được. Muốn danh
        sách property keys hoàn toàn sạch thì phải tạo lại database (drop volume/DB).
        """
        with self.driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n")
            # xóa mọi index/constraint cũ để schema về trạng thái trắng
            try:
                for rec in s.run("SHOW INDEXES YIELD name RETURN name"):
                    s.run(f"DROP INDEX `{rec['name']}` IF EXISTS")
            except Exception:
                pass
            try:
                for rec in s.run("SHOW CONSTRAINTS YIELD name RETURN name"):
                    s.run(f"DROP CONSTRAINT `{rec['name']}` IF EXISTS")
            except Exception:
                pass

    def xoa_sinh_vien(self, ma_sv: str):
        """Xóa CHỈ dữ liệu cá nhân của 1 sinh viên: node SinhVien + mọi cạnh của nó
        (DA_HOC/CHUA_HOC/CO_DIEU_KIEN/THUOC_NGANH). GIỮ NGUYÊN catalog dùng chung
        (MonHoc, tiên quyết, DieuKienTN, NganhHoc) và dữ liệu SINH VIÊN KHÁC.

        Dùng cho multi-user: mỗi lần đồng bộ 1 người chỉ dựng lại phần của người đó,
        KHÔNG đụng người khác (khác hẳn xoa_het() vốn xóa sạch mọi thứ)."""
        with self.driver.session() as s:
            s.run("MATCH (v:SinhVien {ma: $sv}) DETACH DELETE v", sv=ma_sv)

    def tao_index(self):
        with self.driver.session() as s:
            s.run("CREATE INDEX mon_ma IF NOT EXISTS FOR (m:MonHoc) ON (m.ma)")
            s.run("CREATE INDEX sv_ma IF NOT EXISTS FOR (v:SinhVien) ON (v.ma)")

    def nap_sinh_vien(self, sv: Dict[str, Any]):
        with self.driver.session() as s:
            s.run("""
                MERGE (v:SinhVien {ma: $ma})
                SET v.ten = $ten, v.gpa_he_4 = $gpa,
                    v.tin_chi_tich_luy = $tc_tl, v.tong_tin_chi_can = $tc_can
                WITH v
                FOREACH (_ IN CASE WHEN $nganh <> '' THEN [1] ELSE [] END |
                    MERGE (n:NganhHoc {ten: $nganh})
                    MERGE (v)-[:THUOC_NGANH]->(n))
            """, ma=sv["ma"] or "SV", ten=sv["ten"], gpa=sv["gpa"],
                 tc_tl=sv["tc_tich_luy"], tc_can=sv["tc_can"], nganh=sv["nganh"])

    def nap_mon_va_bang_diem(self, ma_sv: str, mon: List[Dict[str, Any]]):
        with self.driver.session() as s:
            for m in mon:
                s.run("""
                    MERGE (mh:MonHoc {ma: $ma})
                    SET mh.ten = coalesce($ten, mh.ten, mh.ma), mh.tin_chi = $tc,
                        mh.khoi = CASE WHEN $khoi <> '' THEN $khoi ELSE mh.khoi END
                """, ma=m["ma"], ten=m["ten"] or None, tc=m["tin_chi"], khoi=m.get("khoi", ""))
                if m["da_hoc"]:
                    s.run("""
                        MATCH (v:SinhVien {ma: $sv}), (mh:MonHoc {ma: $ma})
                        MERGE (v)-[r:DA_HOC]->(mh)
                        SET r.diem_chu = $dc, r.diem_so = $ds, r.dat = $dat
                    """, sv=ma_sv, ma=m["ma"], dc=m["diem_chu"],
                         ds=m["diem_so"], dat=m["dat"])
                else:
                    s.run("""
                        MATCH (v:SinhVien {ma: $sv}), (mh:MonHoc {ma: $ma})
                        MERGE (v)-[:CHUA_HOC]->(mh)
                    """, sv=ma_sv, ma=m["ma"])

    def nap_dieu_kien_tn(self, ma_sv: str, ds: List[Dict[str, Any]]):
        """Mỗi điều kiện TN = 1 node DieuKienTN nối với SinhVien, có cờ da_hoan_thanh.
           Tách riêng khỏi MonHoc vì đây là điều kiện đạt/chưa, không tính GPA."""
        with self.driver.session() as s:
            for d in ds:
                s.run("""
                    MATCH (v:SinhVien {ma: $sv})
                    MERGE (dk:DieuKienTN {ma: $ma})
                    SET dk.ten = $ten
                    MERGE (v)-[r:CO_DIEU_KIEN]->(dk)
                    SET r.da_hoan_thanh = $xong
                """, sv=ma_sv, ma=d["ma"], ten=d["ten"], xong=d["da_hoan_thanh"])

    def nap_ngoai_ngu(self, ma_sv: str, ds: List[Dict[str, Any]]):
        """Anh văn 1..4 = node NgoaiNgu nối SinhVien, cờ dat + bac + tin_chi. Tách khỏi MonHoc
           để KHÔNG lẫn vào GPA; nhưng GIỮ tin_chi để kế hoạch lên trình độ tính +2 TC khi
           học bậc Anh văn kế tiếp (Anh văn có tín chỉ, phải nằm TRONG kế hoạch, không tra ngoài)."""
        with self.driver.session() as s:
            for d in ds:
                s.run("""
                    MATCH (v:SinhVien {ma: $sv})
                    MERGE (nn:NgoaiNgu {ma: $ma})
                    SET nn.ten = $ten, nn.bac = $bac, nn.tin_chi = $tc
                    MERGE (v)-[r:CHUAN_NGOAI_NGU]->(nn)
                    SET r.dat = $dat, r.bac = $bac, r.diem_chu = $dc
                """, sv=ma_sv, ma=d["ma"], ten=d["ten"], bac=d["bac"],
                     tc=d.get("tin_chi") or 2, dat=d["dat"], dc=d["diem_chu"])

    def nap_thuoc_tinh_mon(self, mon: dict):
        """Gắn tên / tín chỉ / học kỳ kế hoạch / loại nhóm lên node MonHoc."""
        with self.driver.session() as s:
            for m in mon.values():
                s.run("""
                    MERGE (mh:MonHoc {ma: $ma})
                    SET mh.ten = coalesce($ten, mh.ten, mh.ma),
                        mh.tin_chi = coalesce($tc, mh.tin_chi),
                        mh.hoc_ky_ke_hoach = $hk,
                        mh.loai_nhom = $ln
                """, ma=m["ma"], ten=m["ten"] or None, tc=m["tin_chi"] or None,
                     hk=m["hoc_ky"], ln=m["loai_nhom"])

    def nap_quan_he(self, canh: List[Dict[str, str]]):
        """Tạo cạnh quan hệ. Môn chưa có node -> tạo node có ten = mã (KHÔNG để trần,
        tránh node hiển thị nhãn loai_nhom hoặc rỗng khi bấm vào)."""
        with self.driver.session() as s:
            for c in canh:
                s.run(f"""
                    MERGE (a:MonHoc {{ma: $tu}})
                      ON CREATE SET a.ten = $tu
                    MERGE (b:MonHoc {{ma: $den}})
                      ON CREATE SET b.ten = $den
                    MERGE (a)-[:{c['loai']}]->(b)
                """, tu=c["tu"], den=c["den"])

    def thong_ke(self) -> str:
        with self.driver.session() as s:
            r = s.run("""
                MATCH (m:MonHoc) WITH count(m) AS mon
                MATCH ()-[d:DA_HOC]->() WITH mon, count(d) AS da_hoc
                OPTIONAL MATCH ()-[t:TIEN_QUYET]->() WITH mon, da_hoc, count(t) AS tq
                OPTIONAL MATCH ()-[sh:SONG_HANH]->() WITH mon, da_hoc, tq, count(sh) AS song_hanh
                OPTIONAL MATCH ()-[kn:KHUYEN_NGHI]->() 
                RETURN mon, da_hoc, tq, song_hanh, count(kn) AS khuyen_nghi
            """).single()
            return (f"  MonHoc: {r['mon']} | DA_HOC: {r['da_hoc']} | "
                    f"TIEN_QUYET: {r['tq']} | SONG_HANH: {r['song_hanh']} | "
                    f"KHUYEN_NGHI: {r['khuyen_nghi']}")


def nap_graph_tu_data(data: Dict[str, Any], ctdt_file: str = CTDT_FILE,
                      ma_ctdt: Optional[str] = None, ma_sv: Optional[str] = None,
                      xoa_toan_bo: bool = False) -> str:
    """Dựng graph cho MỘT sinh viên từ dict bảng điểm. Trả chuỗi thống kê.

    ma_sv: nếu truyền vào, ÉP mã sinh viên trong graph = mã này (vd mssv từ phiên đăng
           nhập) thay vì mã đọc từ bảng điểm — buộc danh tính graph khớp người đăng nhập.
    xoa_toan_bo=False (mặc định, multi-user): chỉ xóa dữ liệu cá nhân của SV này.
    xoa_toan_bo=True (dev/1 người): xóa sạch cả DB trước khi dựng.
    """
    sv = doc_sinh_vien(data)
    if ma_sv:
        sv["ma"] = ma_sv
    mon = doc_mon_hoc(data)
    dieu_kien = doc_dieu_kien_tn(data)
    ngoai_ngu = doc_ngoai_ngu(data)
    if not mon:
        raise ValueError("bảng điểm rỗng — không nạp graph (tránh ghi đè bằng dữ liệu hỏng).")

    ma_ctdt = ma_ctdt or MA_CTDT or data.get("ma_ctdt_resolved") or resolve_ma_ctdt(data, ctdt_file)
    mon_ctdt, canh = doc_ctdt(ctdt_file, ma_ctdt)
    khoa_sv = sv["ma"] or "SV"

    b = GraphBuilder()
    try:
        if xoa_toan_bo:
            if RECREATE_DB:
                b.tao_lai_database()
            b.xoa_het()
        b.tao_index()
        b.xoa_sinh_vien(khoa_sv)            # chỉ dọn dữ liệu cá nhân của CHÍNH SV này
        b.nap_sinh_vien(sv)
        b.nap_quan_he(canh)                 # catalog dùng chung (idempotent qua MERGE)
        b.nap_mon_va_bang_diem(khoa_sv, mon)
        b.nap_thuoc_tinh_mon(mon_ctdt)
        b.nap_dieu_kien_tn(khoa_sv, dieu_kien)
        b.nap_ngoai_ngu(khoa_sv, ngoai_ngu)
        return b.thong_ke()
    finally:
        b.close()


def main() -> int:
    try:
        data = json.load(open(BANG_DIEM_FILE, encoding="utf-8"))
    except Exception as e:
        print(f"LỖI: không đọc được '{BANG_DIEM_FILE}' ({type(e).__name__}).")
        print("Hãy chạy 'python crawl_bang_diem.py' trước.")
        return 1

    sv = doc_sinh_vien(data)
    mon = doc_mon_hoc(data)
    dieu_kien = doc_dieu_kien_tn(data)
    if not mon:
        print("LỖI: bảng điểm rỗng — không nạp graph (tránh ghi đè bằng dữ liệu hỏng).")
        return 1

    xong = sum(1 for d in dieu_kien if d["da_hoan_thanh"])
    print(f"[1/4] Bảng điểm: SV {sv['ma'] or '?'} | {len(mon)} môn | GPA {sv['gpa']}"
          f" | điều kiện TN: {xong}/{len(dieu_kien)}")

    # CLI: mặc định multi-user-safe (chỉ dựng lại phần SV này). Đặt WIPE_ALL=1 để xóa sạch DB.
    xoa_toan_bo = os.getenv("WIPE_ALL", "0") == "1" or RECREATE_DB
    try:
        print("[2/4] Nạp Neo4j..." + (" (XÓA SẠCH toàn bộ)" if xoa_toan_bo else " (chỉ SV này)"))
        stats = nap_graph_tu_data(data, xoa_toan_bo=xoa_toan_bo)
        print("[3/4] Xong. Thống kê graph:")
        print(f"[4/4] {stats}")
    except Exception as e:
        print(f"LỖI khi nạp Neo4j ({type(e).__name__}): {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())