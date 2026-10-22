# -*- coding: utf-8 -*-
"""pham_vi.py — Cổng PHẠM VI & ĐĂNG NHẬP (LLM-FIRST).

Chạy TRƯỚC khi gọi model/team trong main.py, để:
  1. Chặn câu NGOÀI PHẠM VI học vụ (đời tư, xã giao, kiến thức chung) — mọi trạng thái.
  2. Chặn câu CÁ NHÂN khi CHƯA đăng nhập (GPA/điểm/lịch/kế hoạch cải thiện... của họ).

KIẾN TRÚC: LLM (gpt-4o-mini) QUYẾT CHÍNH. Keyword chỉ còn HAI fast-path cho ca HIỂN NHIÊN,
vừa tiết kiệm token vừa làm FAIL-SAFE khi LLM lỗi:
  A. Off-topic rõ (thời tiết/kể chuyện/đời tư...) -> chặn ngay.
  B. Guest + cá-nhân-có-sở-hữu rõ ('GPA của tôi', 'lịch thi của em') -> chặn ngay.
MỌI ranh giới tinh (lịch học vụ vs lịch cá nhân, 'cách tính GPA' vs 'kế hoạch cải thiện GPA
lên 3.0', số mục tiêu vs số tự-nêu) -> GIAO CHO LLM, không dùng keyword/regex (chính chúng
từng gây chặn nhầm / lọt nhầm).

Triết lý an toàn: LLM lỗi/timeout -> FAIL-SAFE cho đi tiếp (thà lọt còn hơn chặn oan); ca
cá-nhân-rõ vẫn được fast-path B bắt kể cả khi LLM chết.
"""
import re
import unicodedata


def _norm(s: str) -> str:
    """Bỏ dấu, đ->d, thường hóa, gộp khoảng trắng. Để so khớp từ khóa ổn định."""
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("\u0111", "d").replace("\u0110", "d").lower()
    return re.sub(r"\s+", " ", s).strip()


def _co(text: str, tu_khoa) -> bool:
    return any(k in text for k in tu_khoa)


# ---- NEO học vụ: có BẤT KỲ cụm nào -> coi là trong phạm vi (để RỘNG, tránh chặn nhầm) ----
_HOC_VU = (
    "hoc phi", "hoc bong", "mien giam", "tin chi", "mon", "hoc phan", "gpa", "diem",
    "xep loai", "bang diem", "tich luy", "cai thien", "lich", "hoc ky", "dang ky",
    "quy che", "quy dinh", "ctdt", "chuong trinh dao tao", "chuong trinh hoc",
    "tot nghiep", "thuc tap", "luan van", "do an", "datn", "nganh", "khoa hoc",
    "tien quyet", "song hanh", "khuyen nghi", "canh bao", "ren luyen", "dao tao",
    "sinh vien", "truong", "phong dao tao", "co van", "hoc vu", "the sinh vien",
    "mssv", "so tin chi", "ket qua hoc", "diem trung binh", "diem chu", "diem so",
    "hoc lai", "no mon", "rot mon", "diem ren luyen", "khoa 20", "ke hoach",
    # THỦ TỤC/GIẤY TỜ sinh viên — cũng là việc học vụ (Phòng Đào tạo/Phòng CTSV xử lý),
    # nhưng nghe không giống "việc học" nên LLM hay phân loại nhầm thành off-topic.
    "giay chung nhan", "giay xac nhan", "xac nhan sinh vien", "nghia vu quan su",
    "tam hoan", "thu tuc", "don xin", "bang tot nghiep", "van bang", "bao luu",
    "chuyen nganh", "thoi hoc", "nghi hoc", "vay von", "cong chung", "sao y",
    # VIẾT TẮT sinh viên hay dùng — không có mấy cụm này thì câu KHÔNG có neo nào, LLM
    # thành người quyết duy nhất và dễ phán nhầm 'ngoai'. Viết tắt 1-3 ký tự phải kèm
    # khoảng trắng hai đầu để không khớp bừa vào giữa từ khác.
    " dk ", "dacn", "datn", "dctn", "lvtn", "ttnt", "ctxh", "dsv", "ctdt",
    # Viết tắt NGÀNH — hay xuất hiện một mình khi người dùng trả lời câu hỏi lại
    " dtvt ", " khmt ", " ktmt ", " cntt ", " dddt ", " qlcn ", " kthh ", " ktdt ",
    " ckm ", " xdd ", " ktxd ", " vlxd ", " cndt ",
    # ĐỢT đăng ký / học kỳ hè / dự thính
    "dot 1", "dot 2", "dot 3", "dot dang ky", "du thinh", "hoc he", "ky he", "hoc ky he",
    # CHỨNG CHỈ ngoại ngữ (điều kiện học vụ, không phải chuyện ngoài trường)
    "toeic", "ielts", "toefl", "chung chi", "ngoai ngu", "anh van", "tieng anh",
    "canh cao", "buoc thoi hoc",
    # THI CỬ / KỶ LUẬT / CHUẨN — nhóm câu trước đây không khớp neo nào, nên LLM quyết một
    # mình và veto không che được.
    "thi cuoi ky", "thi giua ky", "cam thi", "hoan thi", "phuc khao", "gian lan",
    "cong tac xa hoi", "sinh hoat cong dan", "chuyen de", "trinh do nam", "chuan sv",
    "cntt co ban", "quoc phong", "the chat", "bao ve", "hoi dong", "mybk", "bkel",
    "chat luong cao", "dai tra", "tien tien", "quiz", "bai tap", "phong hoc",
)

# ---- Off-topic RÕ RÀNG: đời tư / xã giao / kiến thức chung (KHÔNG liên quan học vụ) ----
_NGOAI = (
    "sinh nhat", "ngay sinh", "tuoi cua", "bao nhieu tuoi", "ten cua toi", "ten toi la",
    "so dien thoai", "dia chi nha", "nguoi yeu", "ban gai", "ban trai", "cung hoang dao",
    "chieu cao", "can nang", "thoi tiet", "may gio", "hom nay ngay", "nau an",
    "cong thuc nau", "bong da", "the thao", "bo phim", "xem phim", "bai hat", "nghe nhac",
    "choi game", "tinh yeu", "crush", "ke chuyen", "ke mot cau", "chuyen cuoi", "lam tho",
    "sang tac", "dich sang", "dich giup", "viet code", "viet ho code", "python", "java ",
    "thu do cua", "dan so", "gia vang", "chung khoan", "tu vi", "boi toan", "y nghia ten",
)

# ---- Dấu hiệu SỞ HỮU (giữ cho ngoai_pham_vi / dùng chung nếu cần) ----
_SO_HUU = (
    " cua toi", " cua minh", " cua em", " cua tui", " cua con",
)


def _trong_pham_vi(t: str) -> bool:
    return _co(t, _HOC_VU)


def co_so_huu(msg: str) -> bool:
    """Câu có ám chỉ DỮ LIỆU RIÊNG của người dùng ('của tôi/mình/em')?"""
    return _co(" " + _norm(msg) + " ", _SO_HUU)


def ngoai_pham_vi(msg: str) -> bool:
    """Off-topic RÕ: KHÔNG có neo học vụ nào VÀ có tín hiệu đời tư/xã giao/kiến thức chung
    (danh sách _NGOAI). Off-topic TINH VI (không có từ khóa _NGOAI) để LLM bắt."""
    t = " " + _norm(msg) + " "
    if _trong_pham_vi(t):
        return False
    return _co(t, _NGOAI)


# ---- Câu trả lời tĩnh (không qua model) ----
TL_NGOAI_PHAM_VI = (
    "Mình là trợ lý học vụ, chỉ hỗ trợ các nội dung liên quan tới việc học ở trường: "
    "học phí, quy chế/quy định, chương trình đào tạo, lịch học vụ, và (khi bạn đăng nhập) "
    "điểm/GPA, lịch và kết quả học tập của bạn. Bạn hỏi mình về mấy chủ đề này nhé."
)
TL_CAN_DANG_NHAP = (
    "Nội dung này liên quan tới dữ liệu cá nhân của bạn (điểm/GPA, lịch, kết quả học tập). "
    "Bạn vui lòng ĐĂNG NHẬP bằng tài khoản sinh viên để mình tra giúp. Nếu chỉ cần thông "
    "tin chung (học phí, quy chế, chương trình đào tạo...) thì mình trả lời ngay được."
)


# Câu TIẾP NỐI: người dùng trả lời câu hỏi-lại của trợ lý, thường RẤT NGẮN và không mang
# neo học vụ nào ("dtvt", "2022", "khóa 2021", "cái đầu tiên", "ừ"). Gate chỉ nhìn câu hiện
# tại nên LLM nhận mấy ký tự trơ trọi và hay phán 'ngoai' -> chặn ngay sau khi CHÍNH trợ lý
# vừa hỏi. Đếm theo TỪ, không theo ký tự, vì "khóa 2022 ngành điện tử viễn thông" dài nhưng
# vẫn là trả lời tiếp nối.
SO_TU_TIEP_NOI = 6


def _ngan_gon(msg: str) -> bool:
    return len(_norm(msg).split()) <= SO_TU_TIEP_NOI


def quyet_dinh(msg: str, da_dang_nhap: bool, intent_set=None, phan_loai_llm=None,
               tiep_noi: bool = False):
    """Cổng chính (LLM-FIRST). Gọi ở ĐẦU handler /chat, TRƯỚC khi dựng executor / gọi model.

    Trả (hanh_dong, tra_loi):
        ('chan', <chuỗi>)  -> trả thẳng <chuỗi>, KHÔNG gọi model/RAG.
        ('tiep', None)     -> đi tiếp pipeline.

    Thứ tự:
      1. Off-topic RÕ (keyword _NGOAI) -> chặn — cả guest lẫn login.
      2. Đã đăng nhập -> cho đi tiếp (không cần chặn cá nhân; off-topic tinh vi thì RAG tự nói).
      3. GUEST còn lại -> LLM QUYẾT: pham_vi='ngoai' -> chặn off-topic; ca_nhan=true -> chặn
         đăng nhập; ngược lại -> đi tiếp. LLM lỗi/không có -> FAIL-SAFE cho đi tiếp.

    Không còn fast-path keyword cho 'cá nhân' — chính nó (vd 'GPA của tôi' trong câu GIẢ ĐỊNH)
    gây chặn nhầm; giao hẳn cho LLM. Guest có lọt câu cá nhân khi LLM chết cũng vô hại: guest
    chỉ chạy RAG (không truy được hồ sơ ai), nên không rò dữ liệu.

    intent_set: không dùng (giữ để tương thích chữ ký cũ) — mọi phân loại tinh do LLM.
    """
    if ngoai_pham_vi(msg):                       # (1) off-topic hiển nhiên -> khỏi LLM
        return "chan", TL_NGOAI_PHAM_VI
    if tiep_noi and _ngan_gon(msg):
        # (1b) Lượt trước ĐÃ được trả lời (đang trong mạch học vụ) + câu này ngắn
        # -> gần như chắc chắn là trả lời câu hỏi-lại của trợ lý. Cho đi tiếp TẤT ĐỊNH,
        # khỏi hỏi LLM (nó không có ngữ cảnh nên sẽ đoán sai). Vẫn qua được bước (1) nên
        # câu ngắn mà lạc đề rõ ("thời tiết?") đã bị chặn từ trước.
        return "tiep", None
    if da_dang_nhap:                             # (2) đã đăng nhập -> đi tiếp
        return "tiep", None
    if phan_loai_llm:                            # (3) LLM QUYẾT CHÍNH cho guest
        try:
            kq = phan_loai_llm(msg) or {}
        except Exception:
            kq = {}
        # VETO off-topic khi câu CÓ neo học vụ rõ: tầng 1 đã bắt off-topic hiển nhiên
        # (_NGOAI), nên nếu câu chứa neo học vụ mà LLM vẫn phán 'ngoai' thì nhiều khả năng
        # LLM sai. Ca thật: 'quy trình xin giấy chứng nhận sinh viên để hoãn nghĩa vụ quân
        # sự' — việc học vụ 100%, nhưng nghe như chuyện quân sự nên LLM gắn 'ngoai'.
        # Đúng triết lý của file: thà lọt xuống RAG rồi báo 'không tìm thấy' còn hơn chặn oan.
        if kq.get("pham_vi") == "ngoai" and not _trong_pham_vi(" " + _norm(msg) + " "):
            return "chan", TL_NGOAI_PHAM_VI
        if kq.get("ca_nhan") is True:            # strict: chỉ chặn khi LLM nói rõ cá nhân
            return "chan", TL_CAN_DANG_NHAP
    return "tiep", None


# ---- Tự kiểm tra nhanh ----
if __name__ == "__main__":
    def _mock_llm(msg):
        t = _norm(msg)
        if any(k in t for k in ("quan su", "thu tuc", "giay xac nhan", "bao luu",
                                "toeic", "du thinh")):
            return {"pham_vi": "ngoai", "ca_nhan": False}   # LỖI THẬT của gpt-4o-mini
        sh = any(k in (" " + t + " ") for k in (" cua toi", " cua em", " cua minh", " cua tui"))
        if "neu " in t or "gia su " in t:                      # giả định -> chung
            return {"pham_vi": "hoc_vu", "ca_nhan": False}
        if ("ke hoach" in t or "cai thien" in t) and ("gpa" in t or "diem" in t or "len " in t):
            return {"pham_vi": "hoc_vu", "ca_nhan": True}       # kế hoạch cải thiện của mình
        if "len gpa" in t or "dat gpa" in t:
            return {"pham_vi": "hoc_vu", "ca_nhan": True}
        if sh and _co(t, ("lich thi", "bang diem", "lich hoc", "ket qua hoc", "tkb", "diem cua")):
            return {"pham_vi": "hoc_vu", "ca_nhan": True}       # 'X của tôi' = hồ sơ riêng
        if "cach tinh" in t or "la gi" in t or "lich hoc vu" in t or "hoc phi" in t or "quy dinh" in t:
            return {"pham_vi": "hoc_vu", "ca_nhan": False}      # định nghĩa / chung
        if "bao nhieu" in t and ("gpa" in t or "diem" in t):
            return {"pham_vi": "hoc_vu", "ca_nhan": True}       # hỏi GPA/điểm của mình
        if "ky toi" in t or "ky sau" in t or "dang ky duoc" in t or "dang ky mon" in t:
            return {"pham_vi": "hoc_vu", "ca_nhan": True}
        if "du dieu kien" in t:
            return {"pham_vi": "hoc_vu", "ca_nhan": True}
        return {"pham_vi": "hoc_vu", "ca_nhan": False}

    cases = [
        ("thời tiết hôm nay thế nào", False, "chan"),
        ("kể cho tôi một câu chuyện cười", False, "chan"),
        ("GPA của em bao nhiêu", False, "chan"),
        ("lịch thi của tôi", False, "chan"),
        ("bảng điểm của mình", False, "chan"),
        ("GPA của em bao nhiêu", True, "tiep"),
        ("lịch học vụ 262", False, "tiep"),
        ("lịch học vụ học kỳ 2 năm 2026", False, "tiep"),
        ("học phí chính quy 2026", False, "tiep"),
        ("học phí của tôi bao nhiêu", False, "tiep"),
        ("cách tính GPA", False, "tiep"),
        ("Nếu GPA của tôi 1.8 thì có bị cảnh báo học vụ không", False, "tiep"),
        ("kế hoạch cải thiện lên gpa 3.0", False, "chan"),
        ("lập kế hoạch cải thiện điểm lên 3.0", False, "chan"),
        ("gpa bao nhiêu", False, "chan"),
        ("kỳ tới em đăng ký được môn gì", False, "chan"),
        ("tôi có đủ điều kiện tốt nghiệp chưa", False, "chan"),
        # THỦ TỤC/GIẤY TỜ: việc học vụ nhưng LLM hay phán nhầm 'ngoai' -> veto phải cứu
        ("Quy trình xin cấp giấy chứng nhận sinh viên tạm thời để hoãn nghĩa vụ quân sự?",
         False, "tiep"),
        ("thủ tục xin bảo lưu kết quả học tập", False, "tiep"),
        ("giấy xác nhận sinh viên để vay vốn ngân hàng xin ở đâu", False, "tiep"),
        # VIẾT TẮT + đợt đăng ký + học kỳ hè: trước đây KHÔNG có neo nào -> LLM quyết một
        # mình và phán nhầm 'ngoai'. Giờ có neo nên veto cứu được.
        ("mình chuẩn bị thi toeic nên ko kịp đk dacn vào đợt 1, đk đợt 2,3 hạn cuối ngày nào?",
         False, "tiep"),
        ("Dự thính hè có đợt 3 không?", False, "tiep"),
    ]
    # TRẢ LỜI TIẾP NỐI: trợ lý vừa hỏi lại ngành/khóa, người dùng gõ mấy ký tự. Không có
    # tiep_noi=True thì LLM (không ngữ cảnh) phán 'ngoai' và chặn ngay sau câu hỏi của
    # chính trợ lý.
    tiep_noi_cases = [
        ("dtvt", "tiep"), ("2022", "tiep"), ("khóa 2021", "tiep"),
        ("cái đầu tiên", "tiep"), ("ừ", "tiep"),
        ("thời tiết", "chan"),            # lạc đề rõ -> tầng 1 vẫn chặn
    ]
    loi = 0
    for cau, dn, mong in cases:
        hd, _tl = quyet_dinh(cau, dn, phan_loai_llm=_mock_llm)
        ok = "OK " if hd == mong else "SAI"
        loi += hd != mong
        print(f"  [{ok}] dn={int(dn)} {hd:<4} | {cau[:52]}")
    def _llm_khong_ngu_canh(msg):
        """Mô phỏng LLM gate khi nhận câu ngắn trơ trọi: không có ngữ cảnh -> đoán 'ngoai'."""
        t = _norm(msg)
        if len(t.split()) <= 3 and not _trong_pham_vi(" " + t + " "):
            return {"pham_vi": "ngoai", "ca_nhan": False}
        return _mock_llm(msg)

    print("  -- trả lời tiếp nối (lượt trước đã được đáp) --")
    for cau, mong in tiep_noi_cases:
        hd, _tl = quyet_dinh(cau, False, phan_loai_llm=_llm_khong_ngu_canh, tiep_noi=True)
        ok = "OK " if hd == mong else "SAI"
        loi += hd != mong
        print(f"  [{ok}] tn=1 {hd:<4} | {cau[:52]}")

    print(f"\n{'TAT CA DAT' if loi == 0 else f'{loi} ca SAI'}")