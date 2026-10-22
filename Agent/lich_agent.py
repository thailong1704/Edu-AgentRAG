import json
import os
from datetime import datetime

from dotenv import load_dotenv
from agno.agent import Agent
from agno.models.openai import OpenAIChat

from lms_common import file_id, mssv_hien_tai, path_lich, path_tai_lieu

load_dotenv()

LICH_MODEL = os.getenv("LICH_MODEL", "gpt-5.5")
# Dùng khi CHƯA có ngữ cảnh người dùng (chạy terminal 1 người). Trên web, mssv đến từ
# mssv_hien_tai (lớp web set từ phiên auth) -> đọc data/{mssv}/... thay vì các file này.
LICH_FILE = os.getenv("OUT_LICH", "lich_hoc_thi.json")
TAILIEU_FILE = os.getenv("OUT_TAILIEU", "tai_lieu_lms.json")


def _doc_json(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


def _lich():
    """Lịch của user hiện tại: theo mssv nếu web đã set, else file env (terminal)."""
    mssv = mssv_hien_tai.get()
    return _doc_json(path_lich(mssv) if mssv else LICH_FILE)


def _tailieu():
    """Danh mục tài liệu của user hiện tại: theo mssv nếu có, else file env."""
    mssv = mssv_hien_tai.get()
    return _doc_json(path_tai_lieu(mssv) if mssv else TAILIEU_FILE)


# ---------------- TOOLS: LỊCH ----------------
def xem_lich_tuan_nay() -> str:
    """Xem thời khóa biểu TUẦN HIỆN TẠI — các môn có học trong tuần đang diễn ra.

    Đọc tuần hiện tại từ dữ liệu crawl, lọc môn có tuần đó trong lịch học.
    """
    d = _lich()
    if not d:
        return "Hiện chưa có dữ liệu lịch học/thi để tra cứu."
    tuan = d.get("tuan", "")
    if not tuan:
        return "Dữ liệu lịch không có thông tin tuần hiện tại."
    try:
        tuan_n = int(tuan)
    except ValueError:
        tuan_n = None

    buoi = []
    for m in d.get("lich_hoc", []) or []:
        cac_tuan = m.get("cac_tuan", [])
        if tuan_n is None or tuan_n in cac_tuan:
            buoi.append(m)
    if not buoi:
        return f"Tuần {tuan} ({d.get('ngay','')}) không có buổi học nào."

    # sắp theo thứ rồi tiết
    def _key(m):
        try:
            return (int(str(m.get("thu", "")).strip() or 99), m.get("tiet", ""))
        except ValueError:
            return (99, "")
    buoi.sort(key=_key)

    out = [f"Thời khóa biểu TUẦN {tuan} ({d.get('ngay','')}) — {d.get('hoc_ky','')}:"]
    for m in buoi:
        thu = m.get("thu", "?")
        thu_hien = f"Thứ {thu}" if str(thu).isdigit() else "Chưa xếp lịch"
        out.append(f"  {thu_hien} | tiết {m.get('tiet','?')} | {m.get('gio_hoc','')} | "
                   f"{m.get('ma_mon','')} {m.get('ten_mon','')} | "
                   f"phòng {m.get('phong','?')} ({m.get('co_so','')})")
    return "\n".join(out)


def xem_lich_theo_thu(thu: str) -> str:
    """Xem lịch học của một THỨ cụ thể trong tuần.

    Args:
        thu: Thứ trong tuần, dạng số '2'..'7' (thứ 2 đến thứ 7) hoặc 'cn'.
    """
    d = _lich()
    if not d:
        return "Hiện chưa có dữ liệu lịch học/thi để tra cứu."
    t = str(thu).strip().lower().replace("thứ", "").strip()
    buoi = [m for m in d.get("lich_hoc", []) or []
            if str(m.get("thu", "")).strip().lower() == t]
    if not buoi:
        return f"Thứ {thu} không có buổi học nào trong lịch."
    buoi.sort(key=lambda m: m.get("tiet", ""))
    out = [f"Lịch học Thứ {thu}:"]
    for m in buoi:
        out.append(f"  tiết {m.get('tiet','?')} | {m.get('gio_hoc','')} | "
                   f"{m.get('ma_mon','')} {m.get('ten_mon','')} | phòng {m.get('phong','?')}")
    return "\n".join(out)


def xem_lich_thi() -> str:
    """Xem toàn bộ lịch thi đã có."""
    d = _lich()
    if not d:
        return "Hiện chưa có dữ liệu lịch để tra cứu."
    thi = d.get("lich_thi", []) or []
    if not thi:
        return "Chưa có lịch thi (có thể chưa tới kỳ thi hoặc chưa có dữ liệu lịch thi)."
    out = [f"Lịch thi ({len(thi)} môn):"]
    for m in thi:
        # lịch thi lưu theo tên cột gốc -> hiển thị các trường có
        dong = " | ".join(f"{k}: {v}" for k, v in m.items() if v)
        out.append(f"  {dong}")
    return "\n".join(out)


def xem_ngay_ctxh() -> str:
    """Xem TỔNG SỐ NGÀY công tác xã hội (CTXH) đã tích lũy 

    Dùng khi hỏi 'em có bao nhiêu ngày CTXH', 'đã làm được mấy ngày công tác xã hội',
    'hoạt động ngoại khóa của em'. Dữ liệu lấy từ tab 'Hoạt động ngoại khóa' trên MyBK.

    """
    d = _lich()
    if not d:
        return "Hiện chưa có dữ liệu để tra cứu CTXH."
    c = d.get("ctxh") or {}
    so_ngay = c.get("tong_so_ngay")
    if so_ngay is None:
        return ("Chưa đọc được số ngày CTXH từ MyBK (dữ liệu đồng bộ chưa có mục này). "
                "Bạn thử bấm 'Cập nhật' để đồng bộ lại, hoặc xem trực tiếp ở MyBK > "
                "Thông tin sinh viên > Hoạt động ngoại khóa.")

    out = [f"Tổng số ngày CTXH đã tích lũy: {so_ngay} ngày"
           + (f"  ({c['sinh_vien']})" if c.get("sinh_vien") else "")]
    return "\n".join(out)


def xem_hoc_phi_can_dong() -> str:
    """Xem các khoản HỌC PHÍ CHƯA ĐÓNG của sinh viên: số tiền, nội dung, hạn nộp.

    Dùng khi hỏi 'em còn nợ học phí không', 'phải đóng bao nhiêu tiền', 'hạn đóng học
    phí của em là khi nào'. Dữ liệu lấy từ tab 'Học phí - BKPay2' trên MyBK — là khoản
    thu THỰC TẾ của riêng sinh viên này, KHÁC với mức học phí chung của trường.
    """
    d = _lich()
    if not d:
        return "Hiện chưa có dữ liệu để tra cứu học phí."
    ds = d.get("hoc_phi_can_dong")
    if ds is None:
        return ("Chưa có dữ liệu học phí (dữ liệu đồng bộ chưa có mục này). "
                "Bạn thử bấm 'Cập nhật' để đồng bộ lại.")
    if not ds:
        return "Bạn không còn khoản học phí nào cần thanh toán."

    tong = sum(x.get("so_tien_phai_nop") or 0 for x in ds)
    out = [f"Còn {len(ds)} khoản cần thanh toán — tổng {tong:,.0f} VNĐ:"]
    for x in ds:
        out.append(f"  {x.get('noi_dung','?')} | {x.get('hoc_ky','')} | "
                   f"{(x.get('so_tien_phai_nop') or 0):,.0f} VNĐ | "
                   f"hạn: {x.get('thoi_han_nop','?')} | {x.get('trang_thai','')}")
    return "\n".join(out)


# ---------------- TOOLS: LMS (tài liệu + deadline) ----------------
def _dt(ts):
    """ISO string -> datetime, hỏng/rỗng -> None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def bai_tap_sap_het_han() -> str:
    """Liệt kê assign/quiz CÒN HẠN (chưa đóng), sắp theo hạn nộp gần nhất.

    Trạng thái 'đang mở / sắp mở / đã đóng' tính LIVE theo hôm nay từ opened_ts/due_ts,
    KHÔNG đọc cờ lưu sẵn (cờ đó dễ cũ giữa các lần crawl). Bài đã quá hạn bị loại;
    bài chưa rõ hạn (parse ngày hỏng) vẫn liệt kê nhưng đẩy xuống cuối.

    Dùng khi hỏi 'bài tập nào sắp hết hạn', 'còn quiz nào chưa làm'.
    """
    ds = _tailieu()
    if not ds:
        return "Hiện chưa có dữ liệu tài liệu LMS để tra cứu."
    bai = [f for f in ds if f.get("loai") in ("assign", "quiz")]
    if not bai:
        return "Chưa có bài tập/quiz nào trong dữ liệu."

    now = datetime.now()
    con_han = []
    for f in bai:
        due = _dt(f.get("due_ts"))
        if due is not None and due < now:
            continue                      # đã đóng -> bỏ
        con_han.append((due, f))
    if not con_han:
        return "Không có bài tập/quiz nào còn hạn (tất cả đã đóng, hoặc chưa có dữ liệu deadline)."

    # hạn gần nhất lên trước; mục không rõ hạn (due=None) xuống cuối
    con_han.sort(key=lambda x: (x[0] is None, x[0] or datetime.max))

    out = [f"Bài tập/quiz CÒN HẠN ({len(con_han)}):"]
    for due, f in con_han:
        loai = "Bài tập" if f.get("loai") == "assign" else "Quiz"
        opened = _dt(f.get("opened_ts"))
        tt = "sắp mở" if (opened is not None and now < opened) else "đang mở"
        if due is not None:
            con_ngay = (due - now).days
            han = f"{f.get('due','?')} (còn {con_ngay} ngày)" if con_ngay >= 0 else f.get("due", "?")
        else:
            han = f.get("due") or "chưa rõ hạn"
        out.append(f"  [{f.get('mon','?')}] {loai} — {tt}: {f.get('ten_file','?')} — hạn: {han}")
    return "\n".join(out)


def tai_lieu_cua_mon(ten_mon: str) -> str:
    """Liệt kê tài liệu (file/folder/label) của một môn trên LMS.

    Args:
        ten_mon: Tên hoặc một phần tên môn, vd 'giải tích', 'siêu cao tần'.
    """
    ds = _tailieu()
    if not ds:
        return "Hiện chưa có dữ liệu tài liệu LMS để tra cứu."
    tk = str(ten_mon).strip().lower()
    khop = [f for f in ds if tk in f.get("mon", "").lower()]
    if not khop:
        return f"Không tìm thấy tài liệu môn khớp '{ten_mon}'."
    # nhóm theo section
    out = [f"Tài liệu môn khớp '{ten_mon}':"]
    icon = {"file": "📄", "folder": "📁", "link": "🔗", "label": "🏷️",
            "assign": "⏰", "quiz": "📝"}
    for f in khop:
        if f.get("loai") == "label":
            continue     # nhãn là ghi chú, không liệt kê như tài liệu
        out.append(f"  {icon.get(f.get('loai'),'')} [{f.get('section','')}] "
                   f"{f.get('ten_file','?')}")
    out.append("\n(Muốn tải, nói 'tải tài liệu môn này' để nhận link tải.)")
    return "\n".join(out)


def tim_tai_lieu(tu_khoa: str) -> str:
    """Tìm tài liệu theo từ khóa trong TÊN FILE hoặc SECTION (mọi môn).

    Args:
        tu_khoa: Từ khóa, vd 'chương 3', 'slide', 'đề cương'.
    """
    ds = _tailieu()
    if not ds:
        return "Hiện chưa có dữ liệu tài liệu LMS để tra cứu."
    tk = str(tu_khoa).strip().lower()
    khop = [f for f in ds if f.get("loai") in ("file", "folder")
            and (tk in f.get("ten_file", "").lower()
                 or tk in f.get("section", "").lower())]
    if not khop:
        return f"Không có tài liệu nào khớp '{tu_khoa}'."
    out = [f"Tài liệu khớp '{tu_khoa}' ({len(khop)}):"]
    for f in khop:
        out.append(f"  [{f.get('mon','?')}] [{f.get('section','')}] {f.get('ten_file','?')}")
    out.append("\n(Agent chỉ tra cứu, không tự tải file. Tải trực tiếp trên LMS ở môn/section tương ứng.)")
    return "\n".join(out)


def tai_file(ten_mon: str) -> str:
    """Trả về LINK TẢI cho tài liệu (file/folder) của một môn.

    Khác tai_lieu_cua_mon (chỉ liệt kê tên): tool này tạo link '/download/{fid}' để người
    dùng bấm tải trực tiếp. Agent KHÔNG tự tải bytes — backend dùng phiên LMS của chính
    sinh viên (đã đăng nhập) để lấy file khi link được bấm.

    Args:
        ten_mon: Tên hoặc một phần tên môn, vd 'giải tích', 'siêu cao tần'.
    """
    ds = _tailieu()
    if not ds:
        return "Hiện chưa có dữ liệu tài liệu LMS để tạo link tải."
    tk = str(ten_mon).strip().lower()
    khop = [f for f in ds if f.get("loai") in ("file", "folder")
            and f.get("url") and tk in f.get("mon", "").lower()]
    if not khop:
        return f"Không tìm thấy file tải được cho môn khớp '{ten_mon}'."
    out = [f"Link tải tài liệu môn khớp '{ten_mon}' ({len(khop)}):"]
    for f in khop:
        out.append(f"  - [{f.get('ten_file','?')}](/download/{file_id(f['url'])})"
                   f"  [{f.get('section','')}]")
    out.append("\n(Link cần bạn đang đăng nhập; nếu báo hết phiên thì đăng nhập lại.)")
    return "\n".join(out)


# ---------------- AGENT ----------------
def tao_lich_agent(output_schema=None) -> Agent:
    """Tạo agent tra cứu lịch/LMS. output_schema=None khi làm thành viên team."""
    return Agent(
        name="Lich va LMS",
        id="lich_lms",
        role="Tra cứu lịch học, lịch thi, tài liệu và deadline bài tập/quiz trên LMS",
        model=OpenAIChat(id=LICH_MODEL, temperature=1, seed=42),
        tools=[xem_lich_tuan_nay, xem_lich_theo_thu, xem_lich_thi, xem_ngay_ctxh, xem_hoc_phi_can_dong,
               bai_tap_sap_het_han, tai_lieu_cua_mon, tim_tai_lieu, tai_file],
        description="Đọc dữ liệu lịch/LMS để trả lời.",
        instructions=[
            "Chỉ tra cứu từ dữ liệu đã đồng bộ. Nếu chưa có dữ liệu, báo rõ là hiện chưa "
            "có, ĐỪNG bịa lịch hay deadline.",
            "Hỏi 'tuần này học gì' -> xem_lich_tuan_nay. Hỏi 'thứ X học gì' -> xem_lich_theo_thu. "
            "Hỏi lịch thi -> xem_lich_thi.",
            "chỉ trả đúng lịch thi Giữa thi hoặc Cuối kỳ, không được liệt kê cả 2 nếu người dùng chỉ hỏi một trong hai.",
            "Hỏi số ngày CTXH / công tác xã hội / hoạt động ngoại khóa -> xem_ngay_ctxh. "
            "Tool này chỉ cho biết sinh viên ĐANG CÓ bao nhiêu ngày; nó KHÔNG biết yêu cầu "
            "tối thiểu của trường. Nếu được hỏi 'đủ chưa', chỉ nêu số ngày hiện có và nói rõ "
            "cần đối chiếu với quy định — TUYỆT ĐỐI không tự nêu ngưỡng (4/8/12 ngày...) từ "
            "trí nhớ, vì ngưỡng khác nhau theo năm học và theo mục đích (xét tốt nghiệp, "
            "đăng ký ĐATN...).",
            "Hỏi học phí còn phải đóng / nợ học phí / hạn đóng học phí của SINH VIÊN NÀY "
            "-> xem_hoc_phi_can_dong. Tool chỉ có khoản thu THỰC TẾ của sinh viên; hỏi MỨC "
            "học phí chung của trường thì không phải việc của tool này.",
            "Hỏi 'bài nào sắp hết hạn / quiz nào chưa làm' -> bai_tap_sap_het_han. "
            "Hỏi tài liệu môn -> tai_lieu_cua_mon. Tìm file theo từ khóa -> tim_tai_lieu.",
            "Hỏi 'TẢI tài liệu môn X' -> tai_file (trả link tải để người dùng bấm). Chỉ muốn "
            "XEM có tài liệu gì -> tai_lieu_cua_mon. Agent không tự tải bytes, chỉ phát link.",
            "Trả lời tiếng Việt, ngắn gọn, nêu rõ thời gian/phòng/hạn nộp khi có.",
        ],
        output_schema=output_schema,
        add_history_to_context=True,
        num_history_runs=3,
        markdown=True,
    )


lich_agent = tao_lich_agent()

if __name__ == "__main__":
    lich_agent.print_response("Tuần này em học những môn gì?", stream=True)