
import json
import sys
from pathlib import Path
 

def cau_rang_buoc_nhom(nhom: dict) -> str:
    loai = nhom["loai"]
    n = nhom.get("so_tin_chi_yeu_cau")
    if loai == "bat_buoc":
        return "Đây là nhóm BẮT BUỘC: sinh viên phải hoàn thành tất cả các môn trong nhóm."
    if loai == "chon_tin_chi":
        return (f"Đây là nhóm TỰ CHỌN: sinh viên chỉ cần tích luỹ đủ {n} tín chỉ "
                f"trong nhóm, KHÔNG bắt buộc học hết mọi môn.")
    if loai == "tu_do":
        return (f"Đây là nhóm TỰ CHỌN TỰ DO: sinh viên chọn môn bất kỳ "
                f"(thường ngoài chương trình ngành) cho đủ {n} tín chỉ.")
    if loai == "chung_chi":
        return ("Đây là CHỨNG CHỈ điều kiện xét tốt nghiệp, "
                "KHÔNG tính vào tổng tín chỉ tích luỹ.")
    return ""
 
 
# --- Diễn giải tiên quyết/song hành: mã -> "MÃ (Tên môn)" --------------------
def cau_tien_quyet(mon: dict, tra_ten) -> str:
    tq = mon.get("tien_quyet_song_hanh", [])
    if not tq:
        return "Không có điều kiện tiên quyết/song hành."
    nhom_theo_loai = {"TQ": [], "SH": [], "KN": []}
    for r in tq:
        ten = tra_ten(r["ma"])
        nhom_theo_loai[r["loai"]].append(f"{r['ma']} ({ten})" if ten else r["ma"])
    phan = []
    if nhom_theo_loai["TQ"]:
        phan.append("Tiên quyết (học xong trước): " + ", ".join(nhom_theo_loai["TQ"]))
    if nhom_theo_loai["SH"]:
        phan.append("Song hành (học cùng kỳ): " + ", ".join(nhom_theo_loai["SH"]))
    if nhom_theo_loai["KN"]:
        phan.append("Khuyến nghị học trước: " + ", ".join(nhom_theo_loai["KN"]))
    return ". ".join(phan) + "."
 
 
# =============================================================================
# SINH CHUNK
# =============================================================================
 
def chunk_tong_quan(ctdt: dict) -> dict:
    ten = ctdt["nganh"]; khoa = ctdt["khoa"]; tong = ctdt["tong_tin_chi"]
    khoi = []
    tu_chon = []
    for n in ctdt["cau_truc"]:
        if n["khoi"] not in khoi:
            khoi.append(n["khoi"])
        if n["loai"] in ("chon_tin_chi", "tu_do"):
            tu_chon.append(f"{n['ten']} (yêu cầu {n['so_tin_chi_yeu_cau']} tín chỉ)")
 
    text = (
        f"Chương trình đào tạo Cử nhân ngành {ten}, khoá {khoa}, "
        f"tổng cộng {tong} tín chỉ. "
        f"Chương trình gồm các khối kiến thức: {', '.join(khoi)}. "
        f"Các nhóm tự chọn và số tín chỉ yêu cầu tương ứng: {'; '.join(tu_chon)}."
    )
    return {
        "chunk_id": f"tongquan_{ctdt['ma_ctdt']}",
        "text": text,
        "metadata": {
            "loai_chunk": "tong_quan",
            "nganh": ten, "khoa": khoa, "ma_ctdt": ctdt["ma_ctdt"],
            "tong_tin_chi": tong,
        },
    }
 
 
def chunk_nhom(nhom: dict, ctdt: dict) -> dict:
    ds_mon = "; ".join(
        f"{m['ma']} {m['ten']} ({m['tin_chi']} TC)" for m in nhom["mon"]
    ) if nhom["mon"] else "(không liệt kê môn cụ thể)"
 
    text = (
        f"Nhóm '{nhom['ten']}' thuộc khối {nhom['khoi']}, "
        f"ngành {ctdt['nganh']} khoá {ctdt['khoa']}. "
        f"{cau_rang_buoc_nhom(nhom)} "
        f"Danh sách môn trong nhóm: {ds_mon}."
    )
    md = {
        "loai_chunk": "nhom",
        "nganh": ctdt["nganh"], "khoa": ctdt["khoa"], "ma_ctdt": ctdt["ma_ctdt"],
        "khoi": nhom["khoi"], "ma_nhom": nhom["ma_nhom"], "ten_nhom": nhom["ten"],
        "loai_nhom": nhom["loai"],
        "so_mon": len(nhom["mon"]),
        "ma_mon_trong_nhom": [m["ma"] for m in nhom["mon"]],
    }
    if "so_tin_chi_yeu_cau" in nhom:
        md["so_tin_chi_yeu_cau"] = nhom["so_tin_chi_yeu_cau"]
    return {"chunk_id": f"nhom__{nhom['ma_nhom']}", "text": text, "metadata": md}
 
 
def chunk_mon(mon: dict, nhom: dict, ctdt: dict, tra_ten) -> dict:
    text = (
        f"Môn học: {mon['ten']} (mã {mon['ma']}), {mon['tin_chi']} tín chỉ. "
        f"Thuộc khối {nhom['khoi']} > nhóm '{nhom['ten']}', "
        f"ngành {ctdt['nganh']} khoá {ctdt['khoa']}. "
        f"{cau_rang_buoc_nhom(nhom)} "
        f"{cau_tien_quyet(mon, tra_ten)}"
    )
    md = {
        "loai_chunk": "mon",
        "nganh": ctdt["nganh"], "khoa": ctdt["khoa"], "ma_ctdt": ctdt["ma_ctdt"],
        "khoi": nhom["khoi"], "ma_nhom": nhom["ma_nhom"], "ten_nhom": nhom["ten"],
        "loai_nhom": nhom["loai"],
        "ma_mon": mon["ma"], "ten_mon": mon["ten"], "tin_chi": mon["tin_chi"],
        "tien_quyet": [r["ma"] for r in mon.get("tien_quyet_song_hanh", [])
                       if r.get("loai") == "TQ"],
        "song_hanh": [r["ma"] for r in mon.get("tien_quyet_song_hanh", [])
                      if r.get("loai") == "SH"],
        "khuyen_nghi": [r["ma"] for r in mon.get("tien_quyet_song_hanh", [])
                        if r.get("loai") == "KN"],
    }
    if "so_tin_chi_yeu_cau" in nhom:
        md["nhom_so_tin_chi_yeu_cau"] = nhom["so_tin_chi_yeu_cau"]
    return {"chunk_id": f"mon__{mon['ma']}", "text": text, "metadata": md}
 
 
def chunk_hoc_ky(hk: dict, prog: dict, ctdt: dict) -> dict:
    """1 chunk tóm tắt cho mỗi HỌC KỲ — trả lời 'học kỳ N học môn gì, bao nhiêu TC'."""
    phan = []
    for g in hk["nhom"]:
        ds = ", ".join(f"{m['ten']} ({m['ma']}, {m['tin_chi']} TC)" for m in g["mon"])
        if g["loai"] in ("chon_tin_chi", "tu_do"):
            phan.append(f"{g['ten']}: chọn {g.get('so_tin_chi_yeu_cau', 0)} tín chỉ"
                        + (f" trong các môn: {ds}." if ds else "."))
        elif g["loai"] == "chung_chi":
            phan.append(f"{g['ten']} (chứng chỉ, không tính vào tổng tín chỉ)"
                        + (f": {ds}." if ds else "."))
        else:
            phan.append(f"{g['ten']}: {ds}." if ds else f"{g['ten']}.")
    text = (
        f"Kế hoạch giảng dạy học kỳ {hk['so_hoc_ky']} — chương trình "
        f"{prog['ten_chuong_trinh']}, ngành {ctdt['nganh']} khoá {ctdt['khoa']}. "
        f"Tổng tín chỉ học kỳ {hk['so_hoc_ky']}: {hk['tin_chi_hoc_ky']}. " + " ".join(phan)
    )
    return {
        "chunk_id": f"hocky__{ctdt['ma_ctdt']}__{prog['ten_chuong_trinh']}__hk{hk['so_hoc_ky']}",
        "text": text,
        "metadata": {
            "loai_chunk": "hoc_ky",
            "nganh": ctdt["nganh"], "khoa": ctdt["khoa"], "ma_ctdt": ctdt["ma_ctdt"],
            "chuong_trinh": prog["ten_chuong_trinh"],
            "hoc_ky": hk["so_hoc_ky"], "tin_chi_hoc_ky": hk["tin_chi_hoc_ky"],
            "ma_mon_trong_hk": [m["ma"] for g in hk["nhom"] for m in g["mon"]],
        },
    }
 
 
def sinh_chunks_khgd(ctdt: dict) -> list:
    """Chunk cho tài liệu KẾ HOẠCH GIẢNG DẠY (có metadata học kỳ)."""
    tra_ten = {m["ma"]: m["ten"]
               for p in ctdt["chuong_trinh"] for hk in p["hoc_ky"]
               for g in hk["nhom"] for m in g["mon"]}.get
    chunks = []
    for prog in ctdt["chuong_trinh"]:
        tq = (f"Kế hoạch giảng dạy ngành {ctdt['nganh']} khoá {ctdt['khoa']}, "
              f"chương trình {prog['ten_chuong_trinh']}: {prog['so_hoc_ky']} học kỳ, "
              f"tổng {prog['tong_tin_chi']} tín chỉ. Phân bổ theo học kỳ: "
              + "; ".join(f"HK{hk['so_hoc_ky']} {hk['tin_chi_hoc_ky']} TC"
                          for hk in prog["hoc_ky"]) + ".")
        chunks.append({
            "chunk_id": f"tongquan__{ctdt['ma_ctdt']}__{prog['ten_chuong_trinh']}",
            "text": tq,
            "metadata": {"loai_chunk": "tong_quan", "nganh": ctdt["nganh"],
                         "khoa": ctdt["khoa"], "ma_ctdt": ctdt["ma_ctdt"],
                         "chuong_trinh": prog["ten_chuong_trinh"],
                         "tong_tin_chi": prog["tong_tin_chi"]},
        })
        for hk in prog["hoc_ky"]:
            chunks.append(chunk_hoc_ky(hk, prog, ctdt))
            for g in hk["nhom"]:
                for mon in g["mon"]:
                    nhom_view = dict(g, khoi=f"Học kỳ {hk['so_hoc_ky']}")
                    c = chunk_mon(mon, nhom_view, ctdt, tra_ten)
                    c["chunk_id"] = (f"mon__{ctdt['ma_ctdt']}__{prog['ten_chuong_trinh']}"
                                     f"__hk{hk['so_hoc_ky']}__{mon['ma']}")
                    c["text"] += (f" Theo kế hoạch giảng dạy ({prog['ten_chuong_trinh']}), "
                                  f"môn này học ở học kỳ {hk['so_hoc_ky']}.")
                    c["metadata"]["hoc_ky"] = hk["so_hoc_ky"]
                    c["metadata"]["chuong_trinh"] = prog["ten_chuong_trinh"]
                    chunks.append(c)
    return chunks
 
 
def sinh_chunks(ctdt: dict) -> list:
    if ctdt.get("loai_tai_lieu") == "ke_hoach_giang_day":
        return sinh_chunks_khgd(ctdt)
 
    # Bảng tra mã -> tên môn để diễn giải tiên quyết cho dễ đọc.
    ten_theo_ma = {m["ma"]: m["ten"] for n in ctdt["cau_truc"] for m in n["mon"]}
    tra_ten = ten_theo_ma.get
 
    chunks = [chunk_tong_quan(ctdt)]
    for nhom in ctdt["cau_truc"]:
        chunks.append(chunk_nhom(nhom, ctdt))
        for mon in nhom["mon"]:
            chunks.append(chunk_mon(mon, nhom, ctdt, tra_ten))
    return chunks


def main():
    input_root = Path(
        r"C:\Users\ADMIN\Desktop\agent_A\data\output\CTDT_JSON"
    )

    output_root = Path(
        r"C:\Users\ADMIN\Desktop\agent_A\data\KnowledgeGraph\output\chunks\CTDT_CHUNKS"
    )

    json_files = sorted(input_root.rglob("*.json"))

    print(f"Tìm thấy {len(json_files)} file JSON\n")

    success = 0
    failed = 0

    for json_path in json_files:
        try:
            print(f"Đang chunk: {json_path}")

            # Đọc CTDT
            ctdt = json.loads(json_path.read_text(encoding="utf-8"))

            # Sinh chunk
            chunks = sinh_chunks(ctdt)

            # Giữ nguyên cấu trúc thư mục
            relative = json_path.relative_to(input_root)

            out_json = output_root / relative.parent / f"{relative.stem}_chunks.json"
            out_jsonl = output_root / relative.parent / f"{relative.stem}_chunks.jsonl"

            out_json.parent.mkdir(parents=True, exist_ok=True)

            # JSON đẹp
            out_json.write_text(
                json.dumps(chunks, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            # JSONL
            with open(out_jsonl, "w", encoding="utf-8") as f:
                for chunk in chunks:
                    f.write(json.dumps(chunk, ensure_ascii=False) + "\n")

            dem = {}
            for chunk in chunks:
                loai = chunk["metadata"]["loai_chunk"]
                dem[loai] = dem.get(loai, 0) + 1

            print(f"   ✓ {out_json.name}")
            print(f"   ✓ {out_jsonl.name}")
            print(f"     {len(chunks)} chunks -> {dem}\n")

            success += 1

        except Exception as e:
            failed += 1
            print(f"   ✗ Lỗi: {json_path}")
            print(e)
            print()

    print("=" * 60)
    print(f"Hoàn thành")
    print(f"Thành công : {success}")
    print(f"Thất bại   : {failed}")
    print(f"Tổng cộng  : {success + failed}")


if __name__ == "__main__":
    main()