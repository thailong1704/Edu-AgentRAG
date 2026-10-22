
import threading
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()        # đọc .env (nếu có) để set TRACING=
from fastapi import FastAPI, HTTPException, Response, Depends, Cookie
from fastapi.responses import FileResponse
from pydantic import BaseModel
import re
import auth
import pham_vi
from auth import get_mssv, get_mssv_optional
from lms_common import DATA_DIR, mssv_hien_tai
from web_download import router as download_router, dang_nhap_lms, luu_phien

# Agent (đọc nhanh). Import ở đây -> cần Postgres/Neo4j/Qdrant của bạn đang chạy.
from agno_agent import agent as rag_agent, bat_dau_luot
from hoc_vu_team import hoc_vu_team

import os as _os
if _os.getenv("TRACING", "0") == "1":
    _bat = False
    try:
        try:
            from agno_agent import _db as _trace_db
        except Exception:
            _trace_db = None
        _setup = None
        for _mod in ("agno.os.tracing", "agno.tracing", "agno.telemetry"):
            try:
                _m = __import__(_mod, fromlist=["setup_tracing"])
                _setup = getattr(_m, "setup_tracing", None)
                if _setup:
                    break
            except Exception:
                continue
        if _setup:
            try:
                _setup(db=_trace_db)      # ghi trace vào cùng DB
            except TypeError:
                _setup()                  # bản không nhận db=
            _bat = True
    except Exception as _e:
        print(f"[tracing] KHÔNG bật được ({type(_e).__name__}: {_e}) -> chạy KHÔNG trace, app vẫn OK")
    print("[tracing] BẬT — ghi vào Postgres chung" if _bat
          else "[tracing] tắt (không tìm thấy setup_tracing phù hợp bản agno này)")
else:
    print("[tracing] tắt (đặt TRACING=1 để bật)")
# ------------------------------------------------------------------------------

app = FastAPI(title="Trợ lý học vụ API")
app.include_router(download_router)          # gắn GET /download/{fid}

# Frontend 1 file, serve CÙNG GỐC (để cookie sid tự gửi kèm, không dính CORS).
_FRONTEND = Path(__file__).parent / "frontend" / "index.html"


@app.get("/")
def _trang_chu():
    return FileResponse(_FRONTEND)



class LoginIn(BaseModel):
    username: str            # = mssv (tài khoản HCMUT)
    password: str


# Tài khoản TEST được phép đăng nhập KHÔNG cần mật khẩu MyBK (chỉ cần có ket_qua.json sẵn).
# Đặt qua env, ngăn cách bằng dấu phẩy. Vd: TEST_USERS="test.nam2,test.nam3"
_TEST_USERS = set(u.strip() for u in _os.getenv("TEST_USERS", "").split(",") if u.strip())


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None      # client gửi (để tách nhiều cuộc); None -> cuộc mặc định
    che_do: str | None = None          # 'ngan' (mặc định) | 'day_du'. Nút gạt UI gửi kèm; nhớ theo cuộc.


# --- Kết nối Postgres để đọc lịch sử chat (agno lưu ở ai.agno_sessions) ---
import sqlalchemy as _sa
from agno_agent import DB_URL as _DB_URL
_eng = _sa.create_engine(_DB_URL, pool_pre_ping=True)


def _sid_hop_le(mssv: str, sid: str | None) -> str:
    """session_id phải thuộc về user này (tiền tố web-{mssv}) -> tránh ghi/đọc chéo."""
    pref = f"web-{mssv}"
    return sid if (sid and sid.startswith(pref)) else pref


def _bóc_tin_nhan(runs):
    """Từ cột runs -> list {role, content}. Chỉ lấy run CẤP CAO NHẤT (không có
    parent_run_id) = đúng lượt user↔team; bỏ sub-run nội bộ của member. Câu trả lời
    lấy ở field 'content' (content_type='str')."""
    import json as _json
    if isinstance(runs, str):
        try: runs = _json.loads(runs)
        except Exception: return []
    runs = [r for r in (runs or []) if isinstance(r, dict)]
    top = [r for r in runs if not r.get("parent_run_id")]
    use = top or runs                       # phòng khi không có run cấp cao -> lấy tất
    msgs = []
    for run in use:
        inp = run.get("input")
        u = inp.get("input_content") if isinstance(inp, dict) else (inp if isinstance(inp, str) else "")
        if u:
            msgs.append({"role": "user", "content": u})
        a = run.get("content") or run.get("output") or ""
        if isinstance(a, dict):
            a = a.get("content") or ""
        if a:
            msgs.append({"role": "assistant", "content": str(a)})
    return msgs


import re as _re
_RE_SOURCES = _re.compile(r"\n?\[\[SOURCES\]\]\s*(.+?)\s*$", _re.S)


def _tach_nguon(reply: str):
    """Tách dòng '[[SOURCES]] a | b' khỏi câu trả lời -> (reply_sạch, [nguồn]).

    ĐƯỜNG LUI. Cách chính giờ là `_nguon_cua_luot()`: agno_agent ghi nguồn vào ContextVar
    ngay lúc retrieval, không nhờ LLM chuyển tiếp qua text. Giữ hàm này để vẫn nuốt được
    marker nếu bật CHEN_SOURCES_VAO_TEXT=1, hoặc nếu model lỡ tự viết ra."""
    if not reply:
        return reply, []
    m = _RE_SOURCES.search(reply)
    if not m:
        return reply, []
    nguon = [s.strip() for s in m.group(1).split("|") if s.strip()]
    # khử trùng lặp, giữ thứ tự
    seen, uniq = set(), []
    for n in nguon:
        if n not in seen:
            seen.add(n); uniq.append(n)
    return reply[:m.start()].rstrip(), uniq


def _nguon_cua_luot(kq):
    reply, nguon_text = _tach_nguon(getattr(kq, "content", str(kq)))
    try:
        from agno_agent import lay_nguon
        nguon = lay_nguon()
    except Exception:
        nguon = []
    return reply, (nguon or nguon_text)


# ============================================================================
#  TRẠNG THÁI ĐỒNG BỘ theo từng user — để frontend biết khi nào dữ liệu sẵn sàng,
#  và để báo lỗi tử tế thay vì "không có dữ liệu" khi thực ra đang crawl / crawl lỗi.
# ============================================================================
_sync_lock = threading.Lock()
_sync_tt: dict = {}     # khoa(username) -> {trang_thai, xong[], loi[]}


def _tt_bat_dau(khoa: str):
    with _sync_lock:
        _sync_tt[khoa] = {"trang_thai": "dang_chay", "xong": [], "loi": []}


def _tt_dang_chay(khoa: str) -> bool:
    with _sync_lock:
        return _sync_tt.get(khoa, {}).get("trang_thai") == "dang_chay"


def _tt_ghi(khoa: str, xong: str = None, loi: str = None):
    with _sync_lock:
        d = _sync_tt.setdefault(khoa, {"trang_thai": "dang_chay", "xong": [], "loi": []})
        if xong:
            d["xong"].append(xong)
        if loi:
            d["loi"].append(loi)


def _tt_ket_thuc(khoa: str):
    with _sync_lock:
        d = _sync_tt.get(khoa)
        if d:
            d["trang_thai"] = "loi" if d["loi"] else "xong"


# ============================================================================
#  BACKGROUND SYNC — crawl dữ liệu cá nhân lúc đăng nhập, đổ ra data/{mssv}/
# ============================================================================
def _sync_bang_diem(mssv: str, password: str, thu_muc):
    from crawl_bang_diem import MyBKCrawler
    c = MyBKCrawler(output_file=str(thu_muc / "ket_qua.json"), headless=True)
    try:
        c.setup_driver()
        c.save(c.login_and_scrape(mssv, password))
    finally:
        c.cleanup()


def _sync_lich(mssv: str, password: str, thu_muc):
    import json
    import crawl_dichvu as cd
    br = cd.HcmutBrowser(headless=True)
    try:
        br.setup()
        br.login_sso(mssv, password, cd.SSO_MYBK)
        lich = cd.crawl_lich(br)
        json.dump(lich, open(thu_muc / "lich_hoc_thi.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    finally:
        br.cleanup()


def _sync_tai_lieu(mssv: str, password: str, thu_muc):
    import json
    import crawl_dichvu as cd
    br = cd.HcmutBrowser(headless=True)
    try:
        br.setup()
        br.login_sso(mssv, password, cd.SSO_LMS)
        files = []
        for c in cd.liet_ke_tai_lieu(br):
            files.extend(cd.crawl_tai_lieu_mon_hoc(br, c["url"], c["mon"]))
        # điền deadline + ISO cho assign/quiz (giống crawl_dichvu.main)
        for a in [f for f in files if f.get("loai") in ("assign", "quiz")]:
            ct = cd._lay_thong_deadline(br, a["url"])
            a["opened"] = ct.get("opened", "")
            a["due"] = ct.get("due", "")
            a["mo_ta"] = ct.get("mo_ta", "")
            a["opened_ts"] = cd._iso(a["opened"])
            a["due_ts"] = cd._iso(a["due"])
        json.dump(files, open(thu_muc / "tai_lieu_lms.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    finally:
        br.cleanup()


def _sync_graph(mssv: str, password: str, thu_muc):
    """Dựng Neo4j graph từ ket_qua.json vừa crawl. Graph khóa theo MSSV THẬT (đọc từ
    bảng điểm), KHÔNG theo tên đăng nhập — vì mỗi SV có MSSV riêng, đó mới là định danh."""
    import json
    import graph_bang_diem as gb
    data = json.load(open(thu_muc / "ket_qua.json", encoding="utf-8"))
    stats = gb.nap_graph_tu_data(data)   # ma_sv=None -> dùng MSSV parse từ bảng điểm
    print(f"[sync {mssv}] graph: {stats}")


def dong_bo_nen(mssv: str, password: str):
    """Chạy trong thread nền. Lỗi từng phần không làm hỏng phần khác; ghi lại trạng thái
    để /status báo cho user (đang chạy / xong / lỗi phần nào)."""
    thu_muc = DATA_DIR / mssv
    thu_muc.mkdir(parents=True, exist_ok=True)
    for ten, ham in (("bảng điểm", _sync_bang_diem),
                     ("lịch", _sync_lich),
                     ("tài liệu", _sync_tai_lieu),
                     ("graph", _sync_graph)):   # graph phải sau 'bảng điểm' (đọc ket_qua.json)
        try:
            ham(mssv, password, thu_muc)
            _tt_ghi(mssv, xong=ten)
            print(f"[sync {mssv}] xong {ten}")
        except Exception as e:
            _tt_ghi(mssv, loi=f"{ten}: {type(e).__name__}: {e}")
            print(f"[sync {mssv}] LỖI {ten}: {type(e).__name__}: {e}")
    _tt_ket_thuc(mssv)


# ============================================================================
#  ROUTES
# ============================================================================


@app.post("/login")
def login(inp: LoginIn, resp: Response):
    # TÀI KHOẢN TEST (env TEST_USERS="test.nam2,..."): BỎ QUA xác thực MyBK — chỉ cần đã có
    # sẵn data/{tên}/ket_qua.json. Dùng để test các trình độ (N2/N3...) mà account thật không
    # có. KHÔNG ảnh hưởng tài khoản thường (chỉ kích hoạt khi tên nằm trong whitelist).
    if inp.username in _TEST_USERS and (DATA_DIR / inp.username / "ket_qua.json").exists():
        token = auth.tao_phien_web(inp.username)
        resp.set_cookie(auth.COOKIE_NAME, token, httponly=True, samesite="lax")
        with _sync_lock:
            _sync_tt[inp.username] = {"trang_thai": "xong",
                                      "xong": ["tài khoản test (dữ liệu có sẵn)"], "loi": []}
        return {"ok": True, "mssv": inp.username, "trang_thai": "xong", "da_co_du_lieu": True}

    # Xác thực = thử ĐĂNG NHẬP LMS thật. Được thì credential hợp lệ + có luôn phiên tải.
    try:
        sess = dang_nhap_lms(inp.username, inp.password)
    except Exception:
        raise HTTPException(401, "Sai tài khoản hoặc mật khẩu trường.")

    luu_phien(inp.username, sess)                       # phiên LMS (để /download)
    token = auth.tao_phien_web(inp.username)            # phiên web (ai đang đăng nhập)
    resp.set_cookie(auth.COOKIE_NAME, token, httponly=True, samesite="lax")
    # (*) secure=True khi đã chạy HTTPS

    # ĐÃ ĐỒNG BỘ TRƯỚC ĐÓ? -> có file ket_qua.json thì DÙNG LẠI dữ liệu cũ, KHÔNG crawl lại.
    # (Muốn dữ liệu mới nhất, user bấm "Cập nhật" -> /dong-bo.) Phiên LMS vẫn vừa lập ở trên
    # nên tải tài liệu vẫn chạy.
    if (DATA_DIR / inp.username / "ket_qua.json").exists():
        with _sync_lock:
            _sync_tt[inp.username] = {"trang_thai": "xong", "xong": ["dữ liệu đã có sẵn"], "loi": []}
        return {"ok": True, "mssv": inp.username, "trang_thai": "xong", "da_co_du_lieu": True}

    if _tt_dang_chay(inp.username):        # đang crawl dở (login lại) -> không crawl chồng
        return {"ok": True, "mssv": inp.username, "trang_thai": "dang_chay", "da_co_du_lieu": False}

    _tt_bat_dau(inp.username)
    # Đồng bộ LẦN ĐẦU (chưa có dữ liệu). Selenium chậm -> chạy nền, không chặn response.
    threading.Thread(target=dong_bo_nen, args=(inp.username, inp.password), daemon=True).start()
    return {"ok": True, "mssv": inp.username, "trang_thai": "dang_chay", "da_co_du_lieu": False}


class DongBoIn(BaseModel):
    password: str


@app.post("/dong-bo")
def dong_bo(inp: DongBoIn, khoa: str = Depends(get_mssv)):
    """User CHỦ ĐỘNG cập nhật dữ liệu mới nhất. Cần mật khẩu để crawl (ta không lưu mật
    khẩu), nên frontend hỏi lại mật khẩu mỗi lần bấm 'Cập nhật'. Xác thực + làm mới phiên
    LMS luôn, rồi crawl nền ghi đè dữ liệu cũ."""
    try:
        sess = dang_nhap_lms(khoa, inp.password)
    except Exception:
        raise HTTPException(401, "Mật khẩu không đúng.")
    luu_phien(khoa, sess)
    if _tt_dang_chay(khoa):
        return {"ok": True, "trang_thai": "dang_chay"}
    _tt_bat_dau(khoa)
    threading.Thread(target=dong_bo_nen, args=(khoa, inp.password), daemon=True).start()
    return {"ok": True, "trang_thai": "dang_chay"}


@app.get("/status")
def status(khoa: str = Depends(get_mssv)):
    """Frontend hỏi để biết dữ liệu đã sẵn sàng chưa: dang_chay / xong / loi.
    'xong' = danh sách phần đã crawl xong; 'loi' = phần nào lỗi (nếu có)."""
    with _sync_lock:
        return _sync_tt.get(khoa, {"trang_thai": "chua_co", "xong": [], "loi": []})


@app.post("/logout")
def logout(resp: Response, sid_cookie: str = Cookie(default=None, alias=auth.COOKIE_NAME)):
    if sid_cookie:
        auth.xoa_phien_web(sid_cookie)
    resp.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


import unicodedata as _ud

# --- Gate lịch sử: chỉ dùng memory khi câu mới LIÊN QUAN lượt trước (tránh dính ngữ cảnh
#     lạc đề + đỡ tốn token). Heuristic miễn phí, không thêm lượt LLM. ---
_MSG_CUOI: dict = {}      # session_id -> câu hỏi user gần nhất (RAM, mất khi restart)
_MACH: dict = {}        # sid -> lượt trước có được trả lời không (để nhận câu trả lời tiếp nối ngắn)

# --- Chế độ trả lời: 'ngan' (mặc định) | 'day_du'. Nút gạt UI gửi kèm mỗi lượt; backend NHỚ
#     theo session để lượt sau không gửi vẫn giữ nguyên. NGẮN = không chèn gì (giữ hành vi cũ),
#     chỉ ĐẦY ĐỦ mới chèn chỉ thị TỰ CHỨA vào message (chạy cho cả RAG lẫn team). ---
_CHE_DO: dict = {}        # session_id -> chế độ gần nhất
_DAY_DU_TOKENS = {"day_du", "dayu", "day-du", "full", "deep", "chi_tiet", "chitiet", "chi-tiet",
                  "1", "true", "on", "yes"}   # 'deep' = nhãn UI hiện tại (Flash/Deep)
_DIRECTIVE_DAY_DU = (
    "\n\n---\n(Chế độ trả lời: ĐẦY ĐỦ — đưa đáp án trực tiếp TRƯỚC, rồi liệt kê MỌI điều kiện/"
    "thông tin liên quan tra được, mỗi ý 1 dòng, kèm ghi chú năm/chương trình/hiệu lực. Bỏ "
    "giới hạn 'chỉ đáp đúng cái được hỏi'. Vẫn không bịa, không kể quá trình tra cứu, và "
    "KHÔNG nhắc lại dòng chế độ này trong câu trả lời.)"
)


def _chuan_che_do(v) -> str:
    """Giá trị che_do từ client -> 'ngan' | 'day_du' | '' (không gửi)."""
    if v is None:
        return ""
    return "day_du" if str(v).strip().lower().replace(" ", "_") in _DAY_DU_TOKENS else "ngan"


def _che_do_cua(sid: str, che_do_in) -> str:
    """Chốt chế độ cho lượt này: ưu tiên giá trị client gửi; không gửi -> dùng cái đã NHỚ của
    cuộc; chưa có -> 'ngan'. Rồi nhớ lại theo session."""
    m = _chuan_che_do(che_do_in) or _CHE_DO.get(sid, "ngan")
    _CHE_DO[sid] = m
    return m


def _them_che_do(msg: str, mode: str) -> str:
    """Chèn chỉ thị chế độ vào message gửi cho agent. NGẮN GỌN = mặc định -> không chèn gì."""
    return msg + _DIRECTIVE_DAY_DU if mode == "day_du" else msg

_STOP = set("la gi bao nhieu the nao co khong cho toi minh ban cua va o tai ve nhu mot cac "
            "nhung day do nay kia thi ma nhe a duoc lam sao khi nao dau con hay xin vui long".split())
_CUE = ("con ", "the ", "vay", "cai do", "cai nay", "tuong tu", "nhu vay", "nhu tren",
        "o tren", "vua roi", "the con", "vay con", " no ", " do ", "kia")


def _khong_dau_(s: str) -> str:
    s = _ud.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if _ud.category(c) != "Mn")
    return s.replace("đ", "d").replace("Đ", "d").lower()


def _tu_noi_dung(s: str):
    return {w for w in re.findall(r"[a-z0-9]+", _khong_dau_(s)) if len(w) > 1 and w not in _STOP}


# Bộ phân loại Ý ĐỊNH/CHỦ ĐỀ (rule-based) — gom theo các mảng của trợ lý. Một câu có thể
# thuộc nhiều nhóm; hai câu 'liên quan' nếu CHUNG nhóm chủ đề.
_INTENT = {
    "hoc_phi": ("hoc phi", "muc thu", "don gia", "hoc bong", "mien giam", "trong goi",
                "vuot dinh muc", "tin chi bao nhieu tien"),
    "ca_nhan": ("gpa", "diem trung binh", "bang diem", "cai thien", "xep loai", "tich luy diem",
                "nen hoc", "ky nay", "ky toi", "ky sau", "dang ky mon", "ke hoach", "lo trinh",
                "du dieu kien", "con thieu", "mon nao", "hoc phan nao", "hoc lai mon"),
    "lich_lms": ("lich", "thoi khoa bieu", "tkb", "lich thi", "lich hoc", "deadline", "han nop",
                 "het han", "quiz", "bai tap", "tai lieu", "slide", "de cuong", "tai ve", "download"),
    "quy_che": ("quy dinh", "quy che", "tot nghiep", "ngoai ngu", "hoc vu", "buoc thoi hoc",
                "canh bao hoc vu", "dieu kien"),
    "ctdt": ("chuong trinh dao tao", "ctdt", "mon hoc", "tien quyet", "song hanh", "ma mon",
             "nganh", "chuyen nganh", "do an", "thuc tap", "khoa luan", "luan van", "hoc phan",
             "dang ky chung", "tu chon", "bat buoc", "so tin chi", "tin chi tich luy", "khoa 20",
             "ke hoach hoc tap"),
}


def _intent(msg: str):
    low = _khong_dau_(msg)
    return {nhom for nhom, kws in _INTENT.items() if any(w in low for w in kws)}


def _la_bo_sung(cur: str) -> bool:
    """Câu chủ yếu CUNG CẤP ngành/khóa/chương trình/mã (trả lời điều agent vừa hỏi)
    -> luôn coi là nối tiếp câu trước."""
    low = _khong_dau_(cur)
    return bool(
        re.search(r"\bkhoa\s*k?\d{2,4}\b", low)                    # 'khóa k22', 'khóa 2022'
        or re.search(r"\bnganh\b", low)                            # 'ngành ...'
        or re.search(r"\b(cq|clc|vlvh|dttx|chinh quy|chat luong cao|tien tien)\b", low)
        or re.search(r"\b[a-z]{2,4}\d{3,4}\b", low)                # mã môn/CTĐT: EE2063, KTDTVT...
    )


def _lien_quan(prev: str, cur: str) -> bool:
    """Câu mới nối tiếp câu trước không?
       1) có từ tham chiếu / rất ngắn / là câu bổ sung ngành-khóa -> CÓ;
       2) cả hai phân loại được: cùng nhóm -> có, khác nhóm -> KHÔNG;
       3) chỉ một bên phân loại được -> chưa chắc lạc đề -> thiên về GIỮ memory;
       4) không bên nào -> fallback trùng từ nội dung."""
    if not prev:
        return False
    low = " " + _khong_dau_(cur) + " "
    if any(c in low for c in _CUE):
        return True
    if len(low.split()) <= 3:
        return True
    if _la_bo_sung(cur):                               # 'ngành X khóa Y' -> bổ sung câu trước
        return True
    ip, ic = _intent(prev), _intent(cur)
    if ip and ic:
        return bool(ip & ic)                          # cùng chủ đề -> liên quan; khác -> lạc đề
    if ip or ic:                                       # 1 bên rõ chủ đề -> giữ memory cho chắc
        return True
    return bool(_tu_noi_dung(prev) & _tu_noi_dung(cur))


def _dat_memory(obj, sid: str, msg: str) -> None:
    """Bật/tắt add_history_to_context của agent/team theo mức liên quan, rồi lưu câu hiện tại."""
    dung = _lien_quan(_MSG_CUOI.get(sid), msg)
    try:
        obj.add_history_to_context = dung          # lạc đề -> False -> không nạp lịch sử
    except Exception:
        pass
    _MSG_CUOI[sid] = msg


import json as _json
from agno_agent import _oc as _gate_oc            # tái dùng OpenAI client sẵn có
_GATE_MODEL = _os.getenv("GATE_MODEL", _os.getenv("MODEL_FAST", "gpt-4o-mini"))
_GATE_SYS = (
    "Bạn phân loại câu hỏi sinh viên gửi trợ lý học vụ HCMUT. CHỈ trả JSON 2 khóa, "
    "không giải thích, không markdown.\n"
    '"pham_vi": "hoc_vu" nếu liên quan việc học ở trường — GỒM CẢ thủ tục/giấy tờ sinh '
    "viên với trường (giấy chứng nhận SV, giấy xác nhận vay vốn, tạm hoãn nghĩa vụ quân sự, "
    "bảo lưu, chuyển ngành, rút môn, cấp lại thẻ, văn bằng, bảng điểm công chứng...). Mấy "
    "thủ tục này do Phòng Đào tạo/CTSV xử lý nên LÀ học vụ, dù nội dung nghe không giống "
    'chuyện học. "ngoai" chỉ khi thật sự lạc đề (đời tư, xã giao, kiến thức chung).\n'
    '"ca_nhan": true CHỈ KHI phải TRA HỒ SƠ/DỮ LIỆU RIÊNG đang lưu của người hỏi mới trả '
    "lời được (điểm/GPA/lịch/kết quả/tiến độ THỰC TẾ của họ), HOẶC là yêu cầu LẬP KẾ HOẠCH / "
    "LỘ TRÌNH để CẢI THIỆN hay ĐẠT một mức GPA/điểm mục tiêu của họ (phải dựa trên bảng điểm "
    "của họ). false nếu trả lời được bằng QUY CHẾ hoặc thông tin CHUNG mà KHÔNG cần tra hồ sơ.\n"
    "QUY TẮC 1 (số TỰ NÊU vs số MỤC TIÊU): nếu người hỏi TỰ NÊU số liệu hiện có (GPA/tín chỉ/"
    "số môn) rồi hỏi kết quả theo quy chế, HOẶC hỏi GIẢ ĐỊNH ('nếu...','giả sử...'), thì "
    "ca_nhan=FALSE (chỉ áp quy chế lên số họ đưa). NHƯNG số MỤC TIÊU như 'lên 3.0','đạt 3.0',"
    "'cải thiện lên 3.0' KHÔNG phải số tự nêu — đó là yêu cầu lập kế hoạch cho họ => ca_nhan=TRUE.\n"
    "QUY TẮC 2 (lịch): 'lịch học vụ', 'mốc/lịch đăng ký học phần', 'lịch đóng học phí', 'lịch "
    "học vụ HK261/262...' là lịch CHUNG của trường => ca_nhan=FALSE. Chỉ 'lịch THI của tôi', "
    "'thời khóa biểu của tôi', 'lịch học của em' mới là cá nhân => TRUE.\n"
    "QUY TẮC 3 ('cách' vs 'kế hoạch'): 'cách cải thiện GPA', 'cách tính GPA' là hỏi CHUNG => "
    "FALSE; 'LẬP kế hoạch/lộ trình cải thiện GPA' là cho họ => TRUE.\n"
    "QUY TẮC 4 (KỂ HOÀN CẢNH vs CẦN TRA HỒ SƠ): người hỏi thường kể tình huống của mình "
    "('mình bận thi TOEIC nên trễ đợt 1', 'kỳ cuối chỉ còn ĐATN') rồi hỏi một điều trả lời "
    "được bằng QUY CHẾ hoặc LỊCH CHUNG (hạn chót đợt 2, số tín chỉ tối thiểu, có bị cảnh cáo "
    "không). Kể hoàn cảnh KHÔNG làm câu đó thành cá nhân => ca_nhan=FALSE. Chỉ TRUE khi phải "
    "MỞ hồ sơ của họ ra xem mới trả lời được.\n"
    "VIẾT TẮT hay gặp (đều là học vụ): ĐK=đăng ký, ĐACN=đồ án chuyên ngành, ĐATN=đồ án tốt "
    "nghiệp, LVTN=luận văn tốt nghiệp, TTNT=thực tập ngoài trường, CTXH=công tác xã hội, "
    "CTĐT=chương trình đào tạo, 'dự thính hè'=học kỳ hè.\n"
    "Ví dụ:\n"
    '- "nếu GPA của tôi 1.8 nhưng tích lũy 2.2 thì có bị cảnh báo học vụ không" => '
    '{"pham_vi":"hoc_vu","ca_nhan":false}\n'
    '- "giả sử tôi rớt 2 môn thì có bị buộc thôi học không" => {"pham_vi":"hoc_vu","ca_nhan":false}\n'
    '- "điều kiện bị cảnh báo học vụ là gì" => {"pham_vi":"hoc_vu","ca_nhan":false}\n'
    '- "cách tính GPA" => {"pham_vi":"hoc_vu","ca_nhan":false}\n'
    '- "cách cải thiện GPA" => {"pham_vi":"hoc_vu","ca_nhan":false}\n'
    '- "lịch học vụ 262" => {"pham_vi":"hoc_vu","ca_nhan":false}\n'
    '- "lịch học vụ học kỳ 1 năm 2026" => {"pham_vi":"hoc_vu","ca_nhan":false}\n'
    '- "học phí của tôi bao nhiêu" => {"pham_vi":"hoc_vu","ca_nhan":false}\n'
    '- "GPA của em bao nhiêu" => {"pham_vi":"hoc_vu","ca_nhan":true}\n'
    '- "lịch thi của tôi" => {"pham_vi":"hoc_vu","ca_nhan":true}\n'
    '- "cho tôi xem bảng điểm" => {"pham_vi":"hoc_vu","ca_nhan":true}\n'
    '- "kỳ tới em đăng ký được môn gì" => {"pham_vi":"hoc_vu","ca_nhan":true}\n'
    '- "lập kế hoạch cải thiện GPA lên 3.0" => {"pham_vi":"hoc_vu","ca_nhan":true}\n'
    '- "kế hoạch cải thiện điểm lên 3.0" => {"pham_vi":"hoc_vu","ca_nhan":true}\n'
    '- "tôi có đủ điều kiện tốt nghiệp chưa" => {"pham_vi":"hoc_vu","ca_nhan":true}\n'
    '- "quy trình xin giấy chứng nhận sinh viên để hoãn nghĩa vụ quân sự" => '
    '{"pham_vi":"hoc_vu","ca_nhan":false}\n'
    '- "thủ tục xin bảo lưu kết quả học tập" => {"pham_vi":"hoc_vu","ca_nhan":false}\n'
    '- "mình bận thi TOEIC nên trễ đợt 1, hạn chót đăng ký ĐACN đợt 2,3 là ngày nào" => '
    '{"pham_vi":"hoc_vu","ca_nhan":false}\n'
    '- "dự thính hè có đợt 3 không" => {"pham_vi":"hoc_vu","ca_nhan":false}\n'
    '- "kỳ cuối chỉ còn ĐATN nên đăng ký không đủ tín chỉ, làm sao để không bị cảnh cáo học '
    'vụ" => {"pham_vi":"hoc_vu","ca_nhan":false}'
)


def _phan_loai_llm(msg: str) -> dict:
    """Classifier vùng xám (gpt-4o-mini). Fail-safe: lỗi/parse hỏng -> coi như CHUNG,
    trong phạm vi (cho đi tiếp) để KHÔNG chặn nhầm câu hợp lệ."""
    try:
        r = _gate_oc.chat.completions.create(
            model=_GATE_MODEL, temperature=0, max_tokens=30,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": _GATE_SYS},
                      {"role": "user", "content": msg[:500]}],
        )
        d = _json.loads(r.choices[0].message.content)
        return {"pham_vi": d.get("pham_vi", "hoc_vu"),
                "ca_nhan": bool(d.get("ca_nhan", False))}
    except Exception as e:
        print(f"[gate_llm] fail-safe ({type(e).__name__}) -> cho đi tiếp")
        return {"pham_vi": "hoc_vu", "ca_nhan": False}


@app.post("/chat")
def chat(inp: ChatIn, mssv=Depends(get_mssv_optional)):
    # --- CỔNG PHẠM VI (HYBRID): chặn TRƯỚC khi gọi model/RAG (tiết kiệm token + rõ hơn). ---
    # Tầng 1 keyword (free): off-topic rõ + cá-nhân-rõ. Tầng 2 LLM (gpt-4o-mini): CHỈ chạy khi
    # guest rơi vào VÙNG XÁM personal-vs-chung mà keyword không phân biệt nổi. Off-topic áp cho
    # cả guest lẫn login (và trước bước sync: câu lạc đề khỏi chờ đồng bộ).
    # _MACH[sid]: lượt TRƯỚC trong cùng cuộc đã được trả lời (không bị chặn) chưa.
    # Dùng để nhận ra câu TRẢ LỜI TIẾP NỐI: trợ lý hỏi lại "ngành và khóa nào?", người dùng
    # gõ "dtvt" — bốn ký tự trơ trọi, không neo học vụ nào, LLM gate không có ngữ cảnh nên
    # phán 'ngoai' và chặn ngay sau khi CHÍNH trợ lý vừa hỏi.
    _sid_mach = inp.session_id or ("guest" if not mssv else str(mssv))
    _hd, _tl = pham_vi.quyet_dinh(inp.message, da_dang_nhap=bool(mssv),
                                  intent_set=_intent(inp.message),
                                  phan_loai_llm=_phan_loai_llm,
                                  tiep_noi=_MACH.get(_sid_mach, False))
    if _hd == "chan":
        _MACH[_sid_mach] = False        # bị chặn -> mạch đứt, câu ngắn sau KHÔNG được ưu tiên
        return {"reply": _tl, "sources": [], "guest": not bool(mssv), "dong_bo": None}
    _MACH[_sid_mach] = True             # đi tiếp -> đang trong mạch học vụ

    if not mssv:
        # Guest -> CHỈ RAG (không lộ GPA/LMS cá nhân). Truyền session_id để NHỚ ngữ cảnh
        # trong cùng cuộc. Khách không có mssv -> user_id=None, session_id do client sinh
        # ('guest-xxxx'); nếu thiếu thì dùng 'guest' chung.
        gsid = inp.session_id if (inp.session_id and inp.session_id.startswith("guest")) else "guest"
        mode = _che_do_cua(gsid, inp.che_do)
        _dat_memory(rag_agent, gsid, inp.message)     # lạc đề -> tắt memory
        bat_dau_luot()                                # mở ngăn nguồn cho lượt này
        kq = rag_agent.run(_them_che_do(inp.message, mode), session_id=gsid, user_id=None)
        reply, nguon = _nguon_cua_luot(kq)
        return {"reply": reply, "sources": nguon, "guest": True, "dong_bo": None, "che_do": mode}

    # Đã đăng nhập: nếu sync CHƯA XONG -> báo thẳng "đang đồng bộ", KHÔNG chạy agent
    # (chắc chắn ra rỗng, phí lượt LLM). Frontend hỏi /status để biết khi nào xong.
    with _sync_lock:
        d = _sync_tt.get(mssv, {})
        tt = d.get("trang_thai", "chua_co")
        so_xong = len(d.get("xong", []))

    if tt == "dang_chay":
        return {"reply": "⏳ Đang đồng bộ dữ liệu của bạn (bảng điểm, lịch, tài liệu), "
                         "vui lòng chờ khoảng 1–2 phút rồi hỏi lại.",
                "guest": False, "dong_bo": "dang_chay"}

    # Sync LỖI: chỉ CHẶN khi hỏng TOÀN BỘ (không bước nào xong -> vd sai mạng/MyBK sập).
    # Lỗi MỘT PHẦN (còn ít nhất 1 bước xong) -> KHÔNG chặn: câu RAG vẫn trả lời được,
    # câu cá nhân dùng phần dữ liệu đã có; cờ dong_bo='loi' để frontend hiện cảnh báo.
    if tt == "loi" and so_xong == 0:
        return {"reply": "⚠️ Đồng bộ dữ liệu thất bại hoàn toàn. Vui lòng đăng nhập lại; "
                         "nếu vẫn lỗi, thử lại sau ít phút.",
                "guest": False, "dong_bo": "loi"}

    # Sync xong (hoặc lỗi một phần) -> chạy FULL team như bình thường.
    mssv_hien_tai.set(mssv)
    sid = _sid_hop_le(mssv, inp.session_id)      # tách nhiều cuộc, chặn ghi chéo
    mode = _che_do_cua(sid, inp.che_do)
    _dat_memory(hoc_vu_team, sid, inp.message)   # lạc đề -> tắt memory
    bat_dau_luot()                               # mở ngăn nguồn cho lượt này
    kq = hoc_vu_team.run(_them_che_do(inp.message, mode), user_id=mssv, session_id=sid)
    reply, nguon = _nguon_cua_luot(kq)
    return {"reply": reply, "sources": nguon, "guest": False, "dong_bo": tt, "che_do": mode}


# ============================================================================
#  RECENTS — liệt kê & mở lại các cuộc chat của user (đọc ai.agno_sessions).
#  Lọc theo user_id=mssv -> chỉ cuộc của chính người đăng nhập (session terminal
#  user_id=None sẽ không hiện). /sessions/{id} kèm user_id -> chặn đọc chéo.
# ============================================================================
def _tieu_de(runs) -> str:
    for m in _bóc_tin_nhan(runs):
        if m["role"] == "user":
            t = m["content"].strip().replace("\n", " ")
            return (t[:48] + "…") if len(t) > 48 else t
    return "Cuộc trò chuyện"


@app.get("/sessions")
def sessions(mssv: str = Depends(get_mssv)):
    with _eng.connect() as c:
        rows = c.execute(_sa.text(
            "SELECT session_id, runs, updated_at FROM ai.agno_sessions "
            "WHERE user_id = :u ORDER BY updated_at DESC LIMIT 40"
        ), {"u": mssv}).fetchall()
    return [{"id": r[0], "title": _tieu_de(r[1]), "updated": str(r[2])} for r in rows]


@app.get("/sessions/{sess_id}")
def session_detail(sess_id: str, mssv: str = Depends(get_mssv)):
    with _eng.connect() as c:
        row = c.execute(_sa.text(
            "SELECT runs FROM ai.agno_sessions WHERE session_id = :s AND user_id = :u"
        ), {"s": sess_id, "u": mssv}).fetchone()        # kiểm user_id -> không đọc chéo
    if not row:
        raise HTTPException(404, "Không tìm thấy cuộc trò chuyện.")
    return _bóc_tin_nhan(row[0])


@app.delete("/sessions/{sess_id}")
def session_delete(sess_id: str, mssv: str = Depends(get_mssv)):
    # AND user_id -> chỉ xoá được cuộc của CHÍNH mình
    with _eng.begin() as c:
        res = c.execute(_sa.text(
            "DELETE FROM ai.agno_sessions WHERE session_id = :s AND user_id = :u"
        ), {"s": sess_id, "u": mssv})
    return {"ok": True, "deleted": res.rowcount}