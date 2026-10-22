
import os
import uuid

from agno.models.openai import OpenAIChat
from agno.team import Team

from agno_agent import agent as rag_agent, _db          # agent RAG + Postgres đã cấu hình
from gpa_agent import gpa_agent
from lich_agent import lich_agent                        # agent tra cứu lịch/LMS (đọc JSON)

TEAM_MODEL = os.getenv("TEAM_MODEL", "gpt-5.5")

# agent RAG trong agno_agent.py chưa đặt tên/vai trò -> đặt để coordinator định tuyến đúng
rag_agent.name = "Tra cuu hoc vu"
rag_agent.id = "rag-hoc-vu"
rag_agent.role = ("Tra cứu tài liệu chính thức của trường: chương trình đào tạo, môn học, "
                  "tín chỉ, học phí, lịch học vụ, quy định/quy chế, điều kiện tốt nghiệp")

hoc_vu_team = Team(
    name="Tro ly hoc vu",
    model=OpenAIChat(id=TEAM_MODEL, temperature=1, seed=42),
    members=[rag_agent, gpa_agent, lich_agent],
    db=_db,                              # dùng chung Postgres để lưu lịch sử hội thoại
    add_history_to_context=bool(_db),
    num_history_runs=5,
    max_tool_calls_from_history=int(os.getenv("MAX_TOOL_CALLS_HISTORY", "1")),
    instructions=[
        "Bạn điều phối 3 chuyên gia. Mỗi chuyên gia có ID cố định — khi giao việc PHẢI "
        "dùng đúng ID này (không dùng tên hiển thị): rag-hoc-vu, gpa-advisor, lich-lms. "
        "CHỈ giao MỘT chuyên gia cho mỗi nhu cầu; KHÔNG gọi trùng một chuyên gia 2 lần "
        "cho cùng một câu hỏi.",

        "PHÂN VIỆC:",
        "1) rag-hoc-vu — TÀI LIỆU CHUNG của trường: môn học, chương trình đào tạo, tín chỉ, "
        "môn tiên quyết, HỌC PHÍ, lịch HỌC VỤ (mốc đăng ký/hủy môn của trường), quy định/"
        "quy chế, điều kiện đăng ký/tốt nghiệp.",
        "2) gpa-advisor — BẢNG ĐIỂM CÁ NHÂN: GPA hiện tại, môn chưa học, môn nên cải thiện, "
        "kỳ tới đăng ký được môn gì, kế hoạch đạt GPA mục tiêu, đối chiếu điều kiện tốt nghiệp. "
        "Chuyên gia này CHỈ có điểm/môn/tín chỉ — KHÔNG có số ngày CTXH, KHÔNG có khoản học "
        "phí phải nộp, KHÔNG có lịch. ĐỪNG giao mấy thứ đó cho nó.",
        "3) lich-lms — DỮ LIỆU CÁ NHÂN ngoài bảng điểm: thời khóa biểu tuần này/thứ mấy, lịch "
        "THI, bài tập/quiz sắp hết hạn, tài liệu (slide/đề cương) môn học trên LMS, SỐ NGÀY "
        "CTXH (công tác xã hội / hoạt động ngoại khóa) đã tích lũy, và các KHOẢN HỌC PHÍ SINH "
        "VIÊN CÒN PHẢI NỘP (số tiền, nội dung, hạn nộp).",

        "CTXH / CÔNG TÁC XÃ HỘI / HOẠT ĐỘNG NGOẠI KHÓA: số ngày đã tích lũy CHỈ có ở lich-lms "
        "— luôn giao cho lich-lms, TUYỆT ĐỐI không giao gpa-advisor (nó không có dữ liệu này "
        "và sẽ trả lời 'không có dữ liệu'). Yêu cầu tối thiểu bao nhiêu ngày là QUY ĐỊNH -> "
        "rag-hoc-vu. Hỏi 'em đủ ngày CTXH chưa' -> gọi CẢ HAI: lich-lms lấy số ngày đang có, "
        "rag-hoc-vu lấy ngưỡng đang hiệu lực, rồi bạn so sánh.",

        "PHÂN BIỆT hai loại 'lịch': lịch HỌC VỤ (mốc đăng ký học phần, hạn đóng học phí của "
        "TOÀN TRƯỜNG) -> rag-hoc-vu. Lịch HỌC/THI cá nhân (thứ mấy học môn gì, thi ngày nào) "
        "-> lich-lms.",

        "PHÂN BIỆT hai loại 'học phí': MỨC THU/đơn giá tín chỉ theo chương trình-năm (ai cũng "
        "như nhau) -> rag-hoc-vu. KHOẢN SINH VIÊN NÀY CÒN PHẢI NỘP + hạn nộp thực tế của họ "
        "-> lich-lms. 'Học phí của tôi bao nhiêu', 'tôi còn nợ học phí không', 'hạn đóng học "
        "phí của tôi' -> lich-lms trước.",

        "ƯU TIÊN DỮ LIỆU CÁ NHÂN: khi câu hỏi nói về CHÍNH người dùng (có 'của tôi/mình/em', "
        "hoặc ngầm hiểu là hỏi tình trạng bản thân), hãy lấy SỐ LIỆU THỰC TẾ của họ TRƯỚC "
        "(gpa-advisor cho điểm/môn/tín chỉ; lich-lms cho CTXH, học phí phải nộp, lịch, "
        "deadline), rồi mới hỏi rag-hoc-vu phần QUY ĐỊNH/MỨC CHUNG nếu cần để diễn giải con "
        "số đó. Câu trả lời phải bắt đầu bằng con số THỰC TẾ của họ, không thay bằng mức "
        "chung. Nếu chuyên gia cá nhân báo CHƯA CÓ DỮ LIỆU thì nói thẳng là chưa có dữ liệu "
        "của bạn — ĐỪNG lặng lẽ trả lời bằng thông tin chung như thể đó là của họ.",

        "GỌI NHIỀU chuyên gia CHỈ khi câu hỏi thật sự cần nhiều nguồn khác nhau. Ví dụ: "
        "'học lại mấy môn này tốn bao nhiêu tiền' -> rag-hoc-vu lấy học phí, rồi gpa-advisor "
        "tính tổng. 'đủ điều kiện làm đồ án tốt nghiệp chưa' -> rag-hoc-vu lấy điều kiện, "
        "gpa-advisor đối chiếu bảng điểm. Câu hỏi đơn (vd 'GPA của tôi') CHỈ gọi 1 chuyên gia.",

        "KHI NGƯỜI DÙNG MUỐN TẢI tài liệu: giao lich-lms — nó trả LINK TẢI (/download/...) "
        "để người dùng bấm. Giữ nguyên link đó trong câu trả lời, đừng bịa hay đổi link.",

        "KHÔNG tự trả lời bằng kiến thức của mình — luôn giao cho chuyên gia phù hợp. "
        "Không tự bịa số liệu (học phí, GPA, điều kiện, lịch, deadline).",

        "Khi tổng hợp: giữ nguyên CON SỐ và NGUỒN chuyên gia trả về, không làm tròn hay "
        "diễn giải lại. Nếu các bên mâu thuẫn, nêu rõ kèm nguồn.",

        "NGUỒN: KHÔNG viết nguồn/trích dẫn trong câu trả lời (không '(Nguồn: …)', không tên "
        "văn bản ở cuối). Hệ thống tự đính nguồn bằng code sau khi bạn trả lời.",

        "Trả lời tiếng Việt. Câu ĐẦU TIÊN là đáp án trực tiếp, đúng trọng tâm câu hỏi; "
        "không thuật lại quá trình giao việc/tra cứu.",

        "CHẾ ĐỘ TRẢ LỜI: mặc định NGẮN GỌN — chỉ đưa đáp án cho đúng câu hỏi, thêm tối đa "
        "1 câu caveat nếu nó làm đổi đáp án; không liệt kê thêm thông tin không được hỏi. "
        "Nếu cuối input người dùng có dòng '(Chế độ trả lời: ĐẦY ĐỦ ...)' thì chuyển sang "
        "ĐẦY ĐỦ: vẫn đáp án trực tiếp TRƯỚC, rồi tổng hợp thêm mọi thông tin liên quan mà "
        "các chuyên gia trả về (mỗi ý 1 dòng, kèm năm/chương trình/hiệu lực nếu có). Dù chế "
        "độ nào cũng KHÔNG bịa và KHÔNG nhắc lại dòng chế độ trong câu trả lời.",
    ],
    markdown=True,
)


def chat_loop():
    """Chat nhiều lượt với team trong terminal."""
    # Terminal KHÔNG có đăng nhập web -> phải tự cho khóa, nếu không GPA/LMS đọc rỗng.
    # Nhập TÊN ĐĂNG NHẬP (= tên thư mục data/{...}/). GPA sẽ tự đổi sang MSSV để tra graph.
    from lms_common import mssv_hien_tai
    mssv = os.getenv("GPA_MSSV") or input("Tên đăng nhập để test (Enter = bỏ qua, chỉ hỏi RAG): ").strip()
    if mssv:
        mssv_hien_tai.set(mssv)

    session_id = f"team-{uuid.uuid4().hex[:8]}"
    print("=" * 66)
    print(f"  Trợ lý học vụ (TEAM)  |  session: {session_id}"
          + (f"  |  mssv: {mssv}" if mssv else "  |  (chưa set mssv)"))
    print("  Thành viên: Tra cuu hoc vu (RAG/Qdrant) + GPA Advisor (Graph/Neo4j)")
    print("             + Lich va LMS (lịch/tài liệu LMS)")
    print("  Lệnh: 'moi' = phiên mới | 'thoat' = kết thúc")
    print("=" * 66)
    while True:
        try:
            q = input("\nBạn: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nTạm biệt!")
            break
        if not q:
            continue
        if q.lower() in ("thoat", "exit", "quit"):
            print("Tạm biệt!")
            break
        if q.lower() in ("moi", "new"):
            session_id = f"team-{uuid.uuid4().hex[:8]}"
            print(f"--- Phiên MỚI: {session_id} ---")
            continue
        print()
        hoc_vu_team.print_response(q, session_id=session_id, stream=True)


if __name__ == "__main__":
    chat_loop()