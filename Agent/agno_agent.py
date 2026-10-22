import os
import re
import json
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from typing import Optional
from dotenv import load_dotenv
load_dotenv()
from openai import OpenAI
from qdrant_client import QdrantClient, models
from fastembed import SparseTextEmbedding

from agno.agent import Agent
from agno.db.postgres import PostgresDb
from agno.models.openai import OpenAIChat

# ---------------- CẤU HÌNH ----------------
EMBED_MODEL  = "text-embedding-3-large"        # PHẢI trùng lúc nạp
SPARSE_MODEL = "Qdrant/bm25"                    # PHẢI trùng lúc nạp
CHAT_MODEL   = os.getenv("CHAT_MODEL", os.getenv("MODEL_FAST", "gpt-5.5"))
QDRANT_URL     = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")

COLL_CTDT, COLL_HC = "ctdt", "hanh_chinh"

# 4 mảng hành chính -> 4 tool. Hai cách back-end:
#   ADMIN_MODE="filter"   : lọc source trên collection "hanh_chinh" (KHÔNG cần nạp lại) [mặc định]
#   ADMIN_MODE="separate" : mỗi mảng là 1 collection vật lý riêng (phải nạp lại Qdrant)
ADMIN_MODE = "filter"

# source (đúng như trong payload) cho từng mảng
SRC_HOC_PHI  = "hoc_phi"
SRC_LICH     = "lich_hoc_vu"
SRC_QUYDINH  = "quydinh"
SRC_LUANVAN  = "luan_van_thuc_tap_md"

# nếu ADMIN_MODE="separate": tên collection vật lý cho từng mảng
SEP_COLL = {
    SRC_HOC_PHI: "hoc_phi",
    SRC_LICH:    "lich_hoc_vu",
    SRC_QUYDINH: "quydinh",
    SRC_LUANVAN: "luan_van",
}
DENSE_NAME, SPARSE_NAME = "dense", "sparse"
CANDIDATES = 10        # số ứng viên lấy ra từ hybrid trước khi rerank
TOP_N      = 7         # số section/đoạn cuối sau rerank.
                       # Để >=8: các chuẩn (vd ngày CTXH) được sửa qua NHIỀU kỳ họp HĐHV
                       # -> cần đủ chỗ để CẢ CÁC BẢN khác nhau cùng vào context, agent mới
                       # so sánh được ngày ban hành và phát hiện mâu thuẫn (thay vì thấy
                       # đúng 1 bản rồi trả lời tự tin, mỗi lần chạy ra một số khác).

EXPAND_CONTEXT         = True   # kéo thêm ngữ cảnh quanh chunk trúng
EXPAND_MAX_SECTION_CHUNKS = 20  # section > ngần này -> KHÔNG kéo cả section (tránh nhồi ~90 chunk CTĐT).
                                # Để 20: các section lịch học vụ dài 12-16 chunk (vd 'Học kỳ Dự thính hè
                                # 2025-2026 (HK253)' = 16 chunk) -> phải kéo TRỌN section mới đủ các đợt
                                # đăng ký; ngưỡng 12 cũ làm nó rơi về cửa sổ ±1 -> trả lời thiếu.
EXPAND_WINDOW          = 1      # khi section quá lớn / trống: lấy cửa sổ ±N chunk quanh chunk trúng
EXPAND_MAX_CHUNKS      = 40     # số bản ghi tối đa quét khi lấy section
SECTION_CHAR_CAP       = 5200   # cắt bớt nếu nội dung ghép quá dài

RERANKER = "cohere"       # "bge" (local, đa ngôn ngữ) hoặc "cohere" (API, nhanh, không cần GPU)
BGE_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
USE_FP16 = False       # CHỈ bật True khi chạy GPU; trên CPU fp16 chậm hơn/không hỗ trợ
COHERE_RERANK_MODEL = "rerank-multilingual-v3.0"   # kiểm tra model hiện hành trên docs Cohere

# ---------------- MEMORY (lịch sử hội thoại) ----------------
# Lưu ở Postgres, KHÔNG lưu Qdrant: lịch sử cần lọc theo session + sắp theo thời gian,
# và không cần embedding (embed mỗi tin nhắn chỉ tốn tiền vô ích).
DB_URL = os.getenv("DB_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/agno")
NUM_HISTORY_RUNS = 5     # số LƯỢT hỏi-đáp gần nhất đưa vào context
# Chunk quy định rất dài (tới 5.000 ký tự/block). Nếu giữ cả tool output của các lượt cũ
# thì context phình rất nhanh -> chậm + tốn tiền. 0 = chỉ giữ Q&A, bỏ tool output.
MAX_TOOL_CALLS_FROM_HISTORY = 0

# ---------------- clients ----------------
_oc = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
_qc = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
_sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL)

# DB lưu lịch sử hội thoại. Postgres chưa chạy -> agent vẫn hoạt động, chỉ là không có memory.
try:
    _db = PostgresDb(db_url=DB_URL)
    print(f"[memory] Postgres OK — giữ {NUM_HISTORY_RUNS} lượt gần nhất")
except Exception as e:
    print(f"[memory] KHÔNG kết nối được Postgres ({type(e).__name__}) -> chạy KHÔNG có lịch sử")
    _db = None

# reranker khởi tạo lười (chỉ nạp khi dùng)
_bge = None
_cohere = None
def _get_bge():
    global _bge
    if _bge is None:
        from FlagEmbedding import FlagReranker
        _bge = FlagReranker(BGE_RERANK_MODEL, use_fp16=USE_FP16)
    return _bge
def _get_cohere():
    global _cohere
    if _cohere is None:
        import cohere
        _cohere = cohere.Client(os.getenv("COHERE_API_KEY"))
    return _cohere


# ---------------- embedding query ----------------
def _dense(text):
    return _oc.embeddings.create(model=EMBED_MODEL, input=[text]).data[0].embedding

def _sparse(text):
    emb = next(iter(_sparse_model.embed([text])))
    return models.SparseVector(indices=emb.indices.tolist(), values=emb.values.tolist())


# ---------------- filter ----------------
def _norm_khoa(v):
    """Chuẩn hóa khóa về SỐ 4 chữ số (payload lưu int, chỉ từ 2000 trở lên).
       Nhận '2022', 2022, 'K22', 'khóa 22', '22' -> 2022. Không nhận diện -> None."""
    if v is None:
        return None
    s = str(v)
    m = re.search(r"\d{4}", s)          # 4 chữ số: 2022 / K2022 / khóa 2022
    if m:
        return int(m.group())
    m = re.search(r"\d{2}", s)          # 2 chữ số: K22 / khóa 22 / 22 -> 2000+xx
    if m:
        return 2000 + int(m.group())
    return None


def _build_filter(conds: dict):
    must = [models.FieldCondition(key=k, match=models.MatchValue(value=v))
            for k, v in conds.items() if v is not None]
    return models.Filter(must=must) if must else None


def _khong_dau2(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.replace("đ", "d").replace("Đ", "d").lower()


def _chuong_trinh_query(q: str):
    """Suy CHƯƠNG TRÌNH người dùng hỏi -> khớp field payload chuong_trinh. None = không nêu."""
    s = _khong_dau2(q)
    if any(k in s for k in ("chat luong cao", "clc", "tien tien", "quoc te", "lien ket",
                            "ctta", "cttt", "ctqt", "ctnb", "cthnb")):
        return "CLC"
    if any(k in s for k in ("vlvh", "vua lam vua hoc", "dttx", "dao tao tu xa", "tu xa")):
        return "VLVH_DTTX"
    if any(k in s for k in ("sau dai hoc", "cao hoc", "thac si", "tien si", "nghien cuu sinh")):
        return "sau_dai_hoc"
    if "chinh quy" in s or "dai tra" in s or re.search(r"\bcq\b", s):
        return "CQ"
    return None


_NAM_HOC_MOI = {"hoc_phi": None}    # cache năm học phí mới nhất (không đổi lúc chạy)


def _nam_hoc_moi_nhat():
    """Tìm nam_hoc học phí MỚI NHẤT trong collection (so sánh chuỗi '2026-2027' > '2025-2026'
    đúng thứ tự). Cache 1 lần. Dùng để khóa cứng khi câu hỏi không nêu năm."""
    if _NAM_HOC_MOI["hoc_phi"] is not None:
        return _NAM_HOC_MOI["hoc_phi"]
    coll = SEP_COLL[SRC_HOC_PHI] if ADMIN_MODE == "separate" else COLL_HC
    flt = None if ADMIN_MODE == "separate" else _build_filter({"source": SRC_HOC_PHI})
    nams, off = set(), None
    try:
        for _ in range(30):
            pts, off = _qc.scroll(coll, scroll_filter=flt, limit=256,
                                  with_payload=True, offset=off)
            for p in pts:
                nh = (p.payload or {}).get("nam_hoc")
                if nh:
                    nams.add(nh)
            if not off:
                break
    except Exception as e:
        print(f"[hoc_phi] không quét được nam_hoc: {e}")
    _NAM_HOC_MOI["hoc_phi"] = max(nams) if nams else ""
    return _NAM_HOC_MOI["hoc_phi"]


def _filter_hoc_phi(ct, nam=None):
    """source=hoc_phi [+ nam_hoc=nam] VÀ (chuong_trinh==ct HOẶC chunk không gắn chương trình)."""
    must = ([] if ADMIN_MODE == "separate"
            else [models.FieldCondition(key="source", match=models.MatchValue(value=SRC_HOC_PHI))])
    if nam:
        must.append(models.FieldCondition(key="nam_hoc", match=models.MatchValue(value=nam)))
    if not ct:
        return models.Filter(must=must) if must else None
    should = [
        models.FieldCondition(key="chuong_trinh", match=models.MatchValue(value=ct)),
        models.IsEmptyCondition(is_empty=models.PayloadField(key="chuong_trinh")),
    ]
    return models.Filter(must=must or None, should=should)


# ---- Tự dựng map tên ngành -> tiền tố mã CTĐT bằng cách QUÉT Qdrant lúc khởi động ----
# Không cần file phụ / build tay; luôn khớp dữ liệu thật trong collection.
def _norm_ten(s):
    """Chuẩn hóa tên tiếng Việt: Đ->d, bỏ dấu, thường hóa, gộp khoảng trắng."""
    s = str(s or "").replace("đ", "d").replace("Đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _core_ten(n):
    for pfx in ("ky thuat ", "cong nghe ", "khoa hoc "):
        if n.startswith(pfx):
            return n[len(pfx):]
    return n


def _build_ctdt_maps():
    """Quét collection CTĐT MỘT lần, trả về 2 map keyed theo tiền tố mã:
       - nganh_map: {tiền tố: [tên ngành chuẩn hóa]} — để khớp mềm tên ngành.
       - versions:  {tiền tố: [năm PHIÊN BẢN CTĐT có thật, tăng dần]}.
    CTĐT đánh theo NĂM PHIÊN BẢN (năm trong ma_ctdt), không phải theo từng khóa: một bản
    ban hành năm Y áp dụng cho mọi khóa >= Y tới khi có bản mới. Cần danh sách năm thật này
    để quy khóa hỏi -> bản áp dụng (bản mới nhất <= khóa hỏi)."""
    names, vers = {}, {}
    offset = None
    while True:
        pts, offset = _qc.scroll(collection_name=COLL_CTDT, limit=1000,
                                 with_payload=["ma_ctdt", "nganh"], offset=offset)
        for pt in pts:
            pl = pt.payload or {}
            mc, ng = pl.get("ma_ctdt"), pl.get("nganh")
            if not mc:
                continue
            prefix = re.sub(r"_\d{4}$", "", str(mc))
            m = re.search(r"_(\d{4})$", str(mc))     # năm phiên bản ở cuối mã
            if m:
                vers.setdefault(prefix, set()).add(int(m.group(1)))
            if ng:                                    # tên ngành có thể thiếu ở vài chunk
                names.setdefault(prefix, set()).add(_norm_ten(ng))
        if offset is None:
            break
    return ({k: sorted(v) for k, v in names.items()},
            {k: sorted(v) for k, v in vers.items()})


try:
    _NGANH_MAP, _CTDT_VERSIONS = _build_ctdt_maps()
    print(f"[ctdt_map] dựng từ Qdrant: {len(_NGANH_MAP)} tiền tố ngành, "
          f"{sum(len(v) for v in _CTDT_VERSIONS.values())} phiên bản CTĐT")
except Exception as e:
    print(f"[ctdt_map] KHÔNG dựng được ({type(e).__name__}) -> bỏ ghim ma_ctdt, dùng hybrid")
    _NGANH_MAP, _CTDT_VERSIONS = {}, {}


def _prefix_cua_nganh(nganh):
    """Tên ngành -> TIỀN TỐ mã CTĐT nếu khớp CHÍNH XÁC đúng một ngành; ngược lại None.
       Bỏ qua Song ngành / tiên tiến trừ khi người dùng nêu rõ trong tên ngành."""
    un = _norm_ten(nganh)
    if not un or not _NGANH_MAP:
        return None
    want_sn = "song nganh" in un
    want_tt = "tien tien" in un
    cands = set()
    for prefix, names in _NGANH_MAP.items():
        is_sn = prefix.startswith("SN")
        is_tt = ("TTT" in prefix) or any("tien tien" in n for n in names)
        if (is_sn and not want_sn) or (is_tt and not want_tt):
            continue
        for n in names:
            if un == n or un == _core_ten(n):
                cands.add(prefix)
                break
    return next(iter(cands)) if len(cands) == 1 else None


def _ctdt_effective_year(prefix, kh):
    """Khóa hỏi kh -> NĂM BẢN CTĐT áp dụng (bản mới nhất có năm <= kh).
       Trả (year, reason):
         reason='ok'        năm bản == kh (có bản đúng khóa)
         reason='older'     dùng bản cũ hơn (chưa có bản mới hơn cho khóa này)
         reason='too_early' kh cũ hơn cả bản sớm nhất -> year=None
         reason='no_versions' không dựng được danh sách phiên bản -> giữ hành vi cũ (year=kh)."""
    years = _CTDT_VERSIONS.get(prefix, [])
    if not years:
        return kh, "no_versions"
    ap_dung = [y for y in years if y <= kh]
    if not ap_dung:
        return None, "too_early"
    v = max(ap_dung)
    return v, ("ok" if v == kh else "older")


def _resolve_ma_ctdt(nganh, khoa):
    """nganh + khoa -> 'PREFIX_YYYY' của bản CTĐT ÁP DỤNG cho khóa đó (bản mới nhất <= khóa
       hỏi), hoặc None nếu không khớp đúng một ngành / khóa cũ hơn cả bản sớm nhất.
       Giữ nguyên chữ ký cũ để chỗ khác gọi không đổi."""
    kh = _norm_khoa(khoa)
    prefix = _prefix_cua_nganh(nganh)
    if not kh or not prefix:
        return None
    v, _ = _ctdt_effective_year(prefix, kh)
    return f"{prefix}_{v}" if v else None


# ---------------- hybrid retrieval (RRF) ----------------
_VIET_TAT = [
    (r"chính quy", "CQ"),
    (r"chất lượng cao", "CLC"),
    (r"vừa (làm|học) vừa (học|làm)", "VLVH"),
    (r"đào tạo từ xa", "ĐTTX ĐTTXa"),
    (r"tiên tiến", "TT"),
]


def _mo_rong_query(q):
    """Thêm dạng VIẾT TẮT tương ứng vào query để khớp văn bản (vd bảng học phí ghi
    'Chương trình CQ' chứ không ghi 'chính quy'). Chỉ dùng cho RETRIEVAL, không cho rerank."""
    low = (q or "").lower()
    extra = [abbr for pat, abbr in _VIET_TAT
             if re.search(pat, low) and abbr.split()[0].lower() not in low]
    if re.search(r"\bCQ\b", q or "") and "chính quy" not in low:
        extra.append("chính quy")
    if re.search(r"\bCLC\b", q or "") and "chất lượng cao" not in low:
        extra.append("chất lượng cao")
    return (q + " " + " ".join(extra)).strip() if extra else q


def _hybrid(collection, query, flt, candidates):
    return _qc.query_points(
        collection_name=collection,
        prefetch=[
            models.Prefetch(query=_dense(query), using=DENSE_NAME, filter=flt, limit=candidates),
            models.Prefetch(query=_sparse(query), using=SPARSE_NAME, filter=flt, limit=candidates),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=candidates,
        with_payload=True,
    ).points


# ---------------- rerank ----------------
def _rerank(query, points, top_n):
    if not points:
        return []
    docs = [(p.payload or {}).get("text", "") for p in points]
    if RERANKER == "cohere":
        res = _get_cohere().rerank(model=COHERE_RERANK_MODEL, query=query, documents=docs, top_n=top_n)
        order = [(r.index, r.relevance_score) for r in res.results]
    else:  # bge
        scores = _get_bge().compute_score([[query, d] for d in docs], normalize=True)
        if not isinstance(scores, list):
            scores = [scores]
        order = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_n]
    return [points[i] for i, _ in order]


def _format(points):
    if not points:
        return "Không tìm thấy kết quả phù hợp."
    out = []
    for i, h in enumerate(points, 1):
        p = h.payload or {}
        head = p.get("doc_title") or p.get("source") or ""
        meta = [f"{k}={p[k]}" for k in ("ma_ctdt", "nganh", "khoa", "doc_date") if p.get(k) is not None]
        meta_s = (" [" + ", ".join(meta) + "]") if meta else ""
        out.append(f"[{i}] ({head}{meta_s})\n{p.get('section_path','')}\n{p.get('text','')}".strip())
    return "\n\n---\n\n".join(out)


def _stitch(texts):
    """Ghép các đoạn theo thứ tự, tự cắt phần chồng lấp (overlap) nếu có."""
    result = ""
    for t in (x.strip() for x in texts if x and x.strip()):
        if not result:
            result = t
            continue
        maxov = min(len(result), len(t), 400)
        ov = 0
        for k in range(maxov, 10, -1):          # tìm hậu tố result trùng tiền tố t (>=11 ký tự)
            if result[-k:] == t[:k]:
                ov = k
                break
        result += ("" if ov else "\n") + t[ov:]
    return result


def _fetch_section(collection, doc_key, section_path):
    flt = models.Filter(must=[
        models.FieldCondition(key="doc_key", match=models.MatchValue(value=doc_key)),
        models.FieldCondition(key="section_path", match=models.MatchValue(value=section_path)),
    ])
    recs, _ = _qc.scroll(collection_name=collection, scroll_filter=flt,
                         limit=EXPAND_MAX_CHUNKS, with_payload=True)
    recs.sort(key=lambda r: (r.payload or {}).get("chunk_index", 0))
    return recs


def _fetch_window(collection, doc_key, ci, w):
    flt = models.Filter(must=[
        models.FieldCondition(key="doc_key", match=models.MatchValue(value=doc_key)),
        models.FieldCondition(key="chunk_index", range=models.Range(gte=ci - w, lte=ci + w)),
    ])
    recs, _ = _qc.scroll(collection_name=collection, scroll_filter=flt,
                         limit=2 * w + 1, with_payload=True)
    recs.sort(key=lambda r: (r.payload or {}).get("chunk_index", 0))
    return recs


def _expand_one(collection, p):
    """Trả về nội dung mở rộng cho 1 chunk trúng.
       - section nhỏ (<=EXPAND_MAX_SECTION_CHUNKS): ghép cả section.
       - section quá lớn hoặc không có: chỉ cửa sổ ±EXPAND_WINDOW quanh chunk trúng."""
    dk = p.get("doc_key"); sec = p.get("section_path"); ci = p.get("chunk_index", 0)
    if not dk:
        return p.get("text", "")
    recs = _fetch_section(collection, dk, sec) if sec else []
    if not recs or len(recs) > EXPAND_MAX_SECTION_CHUNKS:
        recs = _fetch_window(collection, dk, ci, EXPAND_WINDOW)
    texts = [(r.payload or {}).get("text", "") for r in recs] or [p.get("text", "")]
    return _stitch(texts)[:SECTION_CHAR_CAP]


def _format_expanded(collection, points):
    """Mỗi chunk top -> nội dung mở rộng; gộp trùng (doc_key, section); giữ thứ tự rerank."""
    if not points:
        return "Không tìm thấy kết quả phù hợp."
    seen, blocks = set(), []
    for h in points:
        p = h.payload or {}
        key = (p.get("doc_key"), p.get("section_path"))
        if key in seen:
            continue
        seen.add(key)
        full = _expand_one(collection, p) or p.get("text", "")
        head = p.get("doc_title") or p.get("source") or ""
        meta = [f"{k}={p[k]}" for k in ("ma_ctdt", "nganh", "khoa", "doc_date") if p.get(k) is not None]
        meta_s = (" [" + ", ".join(meta) + "]") if meta else ""
        blocks.append(f"[{len(blocks)+1}] ({head}{meta_s})\n{p.get('section_path','')}\n{full}".strip())
    return "\n\n---\n\n".join(blocks)


# ---------------- ưu tiên tài liệu MỚI NHẤT khi người dùng không nêu năm ----------------
PREFER_LATEST = True        # bật ưu tiên bản mới nhất khi câu hỏi không nêu năm
RECENCY_KEEP  = 1           # giữ lại tối đa N năm mới nhất trong ứng viên

# CHỈ ưu tiên-mới-nhất ở nguồn mà VĂN BẢN MỚI THAY THẾ VĂN BẢN CŨ (học phí: mỗi năm 1 thông báo).
# KHÔNG áp dụng cho quydinh (quy định GỐC 2022 vẫn hiệu lực, QĐ 2026 chỉ sửa vài khoản),
# lich_hoc_vu (mới nhất = năm tương lai), ctdt/luan_van (đánh theo KHÓA sinh viên).
RECENCY_SOURCES = {SRC_HOC_PHI}


def _year_of(payload):
    """Suy 'năm' của một chunk: ưu tiên doc_date, không có thì lấy năm LỚN NHẤT trong doc_title."""
    dd = payload.get("doc_date")
    if dd:
        m = re.search(r"(20\d{2})", str(dd))
        if m:
            return int(m.group(1))
    years = re.findall(r"20\d{2}", str(payload.get("doc_title", "")))
    return max(int(y) for y in years) if years else None


def _query_has_year(q):
    """Câu hỏi có nêu năm/năm học/khóa không? (2026, 2026-2027, K22, khóa 22...)"""
    if re.search(r"20\d{2}", q):
        return True
    return bool(re.search(r"\b[kK]\s?\d{2}\b|khóa\s*\d{2}", q))


def _year_in_query(q):
    """Năm (lớn nhất) nêu trong câu hỏi, để lọc đúng năm đó. 'năm học 2026-2027' -> 2027
    (khớp cách _year_of lấy max năm trong doc_title)."""
    ys = [int(y) for y in re.findall(r"20\d{2}", q or "")]
    return max(ys) if ys else None


def _prefer_latest(points, query):
    """Trong RECENCY_SOURCES (học phí — văn bản mới thay cũ):
       - câu hỏi CÓ nêu năm  -> LỌC đúng năm đó (fallback: năm mới nhất nếu không có).
       - câu hỏi KHÔNG nêu năm -> giữ RECENCY_KEEP năm mới nhất.
       Nguồn ngoài phạm vi giữ nguyên ('mới nhất' KHÁC 'đang hiệu lực')."""
    if not PREFER_LATEST or not points:
        return points
    scoped = [p for p in points if (p.payload or {}).get("source") in RECENCY_SOURCES]
    others = [p for p in points if (p.payload or {}).get("source") not in RECENCY_SOURCES]
    if not scoped:
        return points
    years = {y for p in scoped if (y := _year_of(p.payload or {})) is not None}
    if not years:
        return points

    q_year = _year_in_query(query)
    if q_year is not None:
        target = q_year if q_year in years else max(years)   # nêu năm -> đúng năm đó
        def _keep(p):
            y = _year_of(p.payload or {})
            return y is None or y == target                  # CHỈ giữ năm được hỏi
    else:
        target = min(sorted(years, reverse=True)[: max(1, RECENCY_KEEP)])
        def _keep(p):
            y = _year_of(p.payload or {})
            return y is None or y >= target                  # giữ N năm mới nhất

    keep_ids = {id(p) for p in scoped if _keep(p)}
    keep_ids |= {id(p) for p in others}
    return [p for p in points if id(p) in keep_ids] or points


def _ten_nguon(t: str) -> str:
    """Dọn doc_title cho gọn khi hiển thị: bỏ đuôi 'chunks', '.signed', phần mở rộng file
    và khoảng trắng thừa. Chỉ để HIỂN THỊ, không đụng dữ liệu Qdrant."""
    import re
    t = (t or "").strip()
    t = re.sub(r"\.(pdf|docx?|txt|csv|json)$", "", t, flags=re.I)   # bỏ đuôi file
    t = re.sub(r"[\s._-]*(signed)?[\s._-]*chunks?$", "", t, flags=re.I)  # bỏ 'chunks'/'signed chunks'
    t = re.sub(r"\.signed$", "", t, flags=re.I)
    return t.strip(" ._-") or (t or "").strip()


def _dong_sources(points) -> str:
    """CŨ — giữ làm đường lui. Xem `_ghi_nguon` bên dưới.

    Cách cũ: nhét dòng '[[SOURCES]] a | b' vào cuối kết quả tool rồi DẶN LLM copy y nguyên
    xuống cuối câu trả lời. Nhược: phụ thuộc LLM copy đúng (qua team còn phải copy LẦN HAI
    ở coordinator), tốn token cả vào lẫn ra, và lộ marker ra ngoài khi gọi agent trực tiếp
    (không qua main.py). Đặt CHEN_SOURCES_VAO_TEXT=1 để bật lại."""
    ten, seen = [], set()
    for h in points or []:
        p = getattr(h, "payload", None) or {}
        t = _ten_nguon(p.get("doc_title") or p.get("source") or "")
        if t and t not in seen:
            seen.add(t); ten.append(t)
    if not ten:
        return ""
    return "\n\n[[SOURCES]] " + " | ".join(ten[:6])


# --- NGUỒN theo LƯỢT CHẠY (tất định, không qua LLM) --------------------------
# Nguồn đã biết chắc ngay lúc retrieval — trước khi LLM nói chữ nào. Nên ghi thẳng vào
# ContextVar rồi để lớp web đọc ra, thay vì nhét vào text và nhờ LLM chuyển tiếp.
# Cùng mẫu với `mssv_hien_tai` trong lms_common: ContextVar an toàn giữa các request
# chạy song song (mỗi request một ngữ cảnh riêng), khác với biến toàn cục.
#
# LƯU Ý: gom nguồn của MỌI tool được gọi trong lượt, kể cả tool mà LLM cuối cùng không
# dùng tới. Cách cũ cũng vậy, nên không kém chính xác hơn — nhưng đừng hiểu đây là
# "đúng những nguồn đã dùng để trả lời".
from contextvars import ContextVar

_nguon_luot: ContextVar[list] = ContextVar("_nguon_luot", default=None)
CHEN_SOURCES_VAO_TEXT = os.getenv("CHEN_SOURCES_VAO_TEXT", "0") == "1"


def bat_dau_luot():
    """Gọi TRƯỚC mỗi lượt run()/chat: mở một ngăn nguồn rỗng cho lượt này.

    Phải set list MỚI mỗi lượt — không dùng default dùng chung, vì như vậy nguồn của
    lượt trước sẽ dính sang lượt sau."""
    _nguon_luot.set([])


def lay_nguon() -> list:
    """Gọi SAU khi run() xong: danh sách nguồn của lượt vừa rồi (đã khử trùng lặp)."""
    return list(_nguon_luot.get() or [])


def _ghi_nguon(points) -> None:
    """Ghi doc_title THẬT của các chunk đã dùng vào ngăn nguồn của lượt hiện tại.

    Không làm gì nếu `bat_dau_luot()` chưa được gọi (vd chạy tool lẻ trong terminal,
    hoặc harness đánh giá) -> im lặng bỏ qua thay vì ném lỗi."""
    kho = _nguon_luot.get()
    if kho is None:
        return
    for h in points or []:
        p = getattr(h, "payload", None) or {}
        t = _ten_nguon(p.get("doc_title") or p.get("source") or "")
        if t and t not in kho:
            kho.append(t)


def _contexts(points):
    """List TEXT các chunk đã dùng — CHỈ cho đánh giá (RAGAS 'contexts'). Không đưa cho user."""
    out = []
    for h in points or []:
        t = ((getattr(h, "payload", None) or {}).get("text") or "").strip()
        if t:
            out.append(t)
    return out


def _retrieve(collection, query, flt, tra_contexts=False):
    cands = _hybrid(collection, _mo_rong_query(query), flt, CANDIDATES)   # mở rộng viết tắt
    cands = _prefer_latest(cands, query)      # lọc năm theo query GỐC
    top = _rerank(query, cands, TOP_N)        # rerank theo query GỐC (giữ ngữ nghĩa sạch)
    body = _format_expanded(collection, top) if EXPAND_CONTEXT else _format(top)
    _ghi_nguon(top)                           # nguồn -> ContextVar (tất định, không qua LLM)
    ket_qua = body + (_dong_sources(top) if CHEN_SOURCES_VAO_TEXT else "")
    if tra_contexts:                          # chỉ harness đánh giá bật cờ -> trả kèm contexts
        return ket_qua, _contexts(top)
    return ket_qua                            # đường USER: y hệt trước, không đổi gì


# ---------------- TOOLS ----------------
def truy_hoi_danh_gia(query: str, source: str = None, tra_contexts: bool = True):
    """CHỈ dùng cho ĐÁNH GIÁ (RAGAS / tuning K, TOP_N). Chạy ĐÚNG retrieval như tool tương
    ứng (cùng filter năm/chương trình...), trả (text_ngữ_cảnh, [contexts]). KHÔNG nằm trên
    đường chạy user, KHÔNG phải tool của agent -> không ảnh hưởng câu trả lời người dùng.
        source: 'hoc_phi' | 'quydinh' | 'luan_van_thuc_tap_md' | 'lich_hoc_vu' | 'ctdt' | None
    """
    if source == SRC_HOC_PHI:
        ct = _chuong_trinh_query(query)
        nam = None if _year_in_query(query) else _nam_hoc_moi_nhat()
        coll = SEP_COLL[SRC_HOC_PHI] if ADMIN_MODE == "separate" else COLL_HC
        return _retrieve(coll, query, _filter_hoc_phi(ct, nam), tra_contexts=tra_contexts)
    if source == "ctdt":
        return _retrieve(COLL_CTDT, query, None, tra_contexts=tra_contexts)
    if source in (SRC_QUYDINH, SRC_LUANVAN, SRC_LICH):
        coll = SEP_COLL[source] if ADMIN_MODE == "separate" else COLL_HC
        flt = None if ADMIN_MODE == "separate" else _build_filter({"source": source})
        return _retrieve(coll, query, flt, tra_contexts=tra_contexts)
    return _retrieve(COLL_HC, query, None, tra_contexts=tra_contexts)   # không rõ -> tìm chung


def _retrieve_admin(source, query):
    """Định tuyến 1 mảng hành chính về đúng collection/filter theo ADMIN_MODE."""
    if ADMIN_MODE == "separate":
        return _retrieve(SEP_COLL[source], query, None)
    return _retrieve(COLL_HC, query, _build_filter({"source": source}))


_HO_SO_CACHE = {}


def ho_so_sv(mssv: str = None) -> dict:
    """Đọc HỒ SƠ sinh viên (ngành, khóa, chương trình) từ data/{mssv}/ket_qua.json.
    Lấy mssv từ ContextVar phiên nếu không truyền. Guest / chưa đăng nhập -> {} (không cá nhân hóa).
    Cache theo mssv. Không bao giờ ném lỗi (hỏng -> trả {})."""
    try:
        from lms_common import DATA_DIR, mssv_hien_tai
    except Exception:
        return {}
    if not mssv:
        try:
            mssv = mssv_hien_tai.get()
        except Exception:
            mssv = None
    if not mssv:
        return {}
    if mssv in _HO_SO_CACHE:
        return _HO_SO_CACHE[mssv]

    hs = {}
    try:
        d = json.load(open(DATA_DIR / mssv / "ket_qua.json", encoding="utf-8"))
        tt = "\n".join(d.get("thong_tin_sinh_vien", []) or [])
        # "Ngành: Kỹ thuật Điện tử - Viễn thông - DH_CQ_DD_DVT_2022"
        m = re.search(r"Ngành:\s*(.+?)\s*-\s*([A-Z0-9_]+)\s*$", tt, re.M)
        if m:
            hs["nganh"] = m.group(1).strip()
            hs["ma_ctdt_raw"] = m.group(2).strip()
        my = re.search(r"Áp dụng từ năm:\s*(20\d{2})", tt)
        if my:
            hs["khoa"] = my.group(1)
        elif re.match(r"\d{2}", str(mssv)):
            hs["khoa"] = "20" + str(mssv)[:2]           # 2211884 -> 2022
        raw = hs.get("ma_ctdt_raw", "")
        if "_CQ_" in raw:
            hs["chuong_trinh"] = "CQ"
        elif "_CLC_" in raw or "_CC_" in raw:
            hs["chuong_trinh"] = "CLC"
        elif "_TT_" in raw or "TTT" in raw:
            hs["chuong_trinh"] = "CLC"                    # tiên tiến -> nhóm CLC
    except Exception:
        pass
    _HO_SO_CACHE[mssv] = hs
    return hs


def tra_cuu_chuong_trinh_dao_tao(
    query: str, nganh: Optional[str] = None, khoa: Optional[str] = None,
    ma_ctdt: Optional[str] = None, ma_mon: Optional[str] = None,
) -> str:
    """Tra cứu CHƯƠNG TRÌNH ĐÀO TẠO: môn học, nhóm học phần, số tín chỉ, cấu trúc CT theo ngành/khóa.

    Dùng khi hỏi: ngành X học môn gì, môn Y mấy tín chỉ, tổng tín chỉ, môn thuộc khối nào.
    Args:
        query: Nội dung cần tìm.
        nganh: Tên ngành (vd 'điện tử viễn thông', 'Điện tử - Viễn thông'). Cứ điền
               tự nhiên dù không nhớ chính xác cách viết — hệ thống khớp mềm.
        khoa: Khóa/năm tuyển sinh. Điền y như người dùng nói: '2022', 'K22', 'khóa 22'
              đều được — hệ thống tự quy về năm 4 chữ số.
        ma_ctdt: Lọc theo mã CTĐT, vd 'KTDDT_2021'.
        ma_mon: Lọc theo mã môn, vd 'MT1007'.
    """
    # ĐÃ ĐĂNG NHẬP + user KHÔNG nêu ngành/khóa -> tự lấy từ HỒ SƠ (data/{mssv}/ket_qua.json).
    # User nêu rõ thì tôn trọng cái user nói (chỉ điền khi còn trống). Guest -> hồ sơ rỗng.
    _hs = ho_so_sv()
    if _hs:
        nganh = nganh or _hs.get("nganh")
        khoa = khoa or _hs.get("khoa")
    # Ưu tiên GHIM đúng chương trình: suy ma_ctdt từ (nganh, khoa) -> lọc cứng theo mã,
    # tránh lẫn các ngành cùng tên (ĐTVT vs Điện-Điện tử vs Cơ điện tử vs Song ngành...).
    # ma_ctdt nêu trực tiếp -> tôn trọng, không quy đổi.
    if ma_ctdt:
        flt = _build_filter({"ma_ctdt": ma_ctdt, "ma_mon": ma_mon})
        return _retrieve(COLL_CTDT, query, flt)

    # Khớp đúng MỘT ngành + có khóa -> quy khóa hỏi về BẢN CTĐT áp dụng (bản mới nhất <=
    # khóa hỏi). CTĐT ban hành theo năm phiên bản, một bản dùng cho nhiều khóa tới khi có
    # bản mới; nên khóa 2026 vẫn áp dụng bản 2024 nếu chưa có bản 2026.
    prefix = _prefix_cua_nganh(nganh) if nganh else None
    kh = _norm_khoa(khoa)
    if prefix and kh:
        v, reason = _ctdt_effective_year(prefix, kh)
        if reason == "too_early":
            som = min(_CTDT_VERSIONS.get(prefix, [kh]))
            return (f"Chưa có CTĐT áp dụng cho khóa {kh}: bản sớm nhất hiện có là năm {som}. "
                    f"Nếu cần, tôi tra bản {som} để bạn tham khảo.")
        flt = _build_filter({"ma_ctdt": f"{prefix}_{v}", "ma_mon": ma_mon})
        kq = _retrieve(COLL_CTDT, query, flt)
        if reason == "older":       # phải lùi bản -> NÓI RÕ để SV không tưởng có bản riêng
            kq = (f"(Lưu ý: chưa có CTĐT riêng cho khóa {kh}; đang áp dụng bản {v} — "
                  f"bản mới nhất hiện hành.)\n\n" + kq)
        return kq

    # Không ghim được (tên mơ hồ / thiếu khóa): 'nganh' cho hybrid, lọc mềm theo khóa.
    if nganh:
        query = f"{query} ngành {nganh}"
    flt = _build_filter({"khoa": kh, "ma_mon": ma_mon})
    return _retrieve(COLL_CTDT, query, flt)


def tra_cuu_hoc_phi(query: str, **kwargs) -> str:
    """Tra cứu HỌC PHÍ: mức thu học phí theo diện đào tạo, theo tín chỉ, theo năm học,
    các khoản thu và chính sách miễn giảm.

    Dùng khi hỏi: học phí mỗi tín chỉ bao nhiêu, học phí học kỳ, mức thu năm học nào.
    Args:
        query: Nội dung cần tìm về học phí.
    """
    ct = _chuong_trinh_query(query)            # nêu chương trình -> lọc CỨNG đúng chương trình
    hs, tu_ho_so = {}, False
    if not ct:                                 # user KHÔNG nêu -> theo chương trình của SV (nếu đăng nhập)
        hs = ho_so_sv()
        ct = hs.get("chuong_trinh")
        tu_ho_so = bool(ct)
    coll = SEP_COLL[SRC_HOC_PHI] if ADMIN_MODE == "separate" else COLL_HC
    # KHÔNG nêu năm -> khóa cứng năm học phí MỚI NHẤT (retrieval hay lấy nhầm năm cũ vì
    # bảng năm cũ khớp từ khóa mạnh hơn). Nêu năm -> để _prefer_latest lọc theo năm đó.
    nam = None if _year_in_query(query) else _nam_hoc_moi_nhat()
    kq = _retrieve(coll, query, _filter_hoc_phi(ct, nam))
    if tu_ho_so:   # đã ĐĂNG NHẬP + suy được hệ từ hồ sơ -> nói RÕ, đừng để agent hiểu là 'mặc định'
        _ten = {"CQ": "chính quy", "CLC": "chất lượng cao", "VLVH": "vừa làm vừa học",
                "VLVH_DTTX": "VLVH / đào tạo từ xa"}.get(ct, ct)
        _khoa = hs.get("khoa")
        kq = (f"(Áp dụng theo HỆ {ct} — {_ten}" + (f", khóa {_khoa}" if _khoa else "")
              + " của bạn, lấy từ HỒ SƠ — KHÔNG phải mức mặc định.)\n" + kq)
    return kq


def tra_cuu_lich_hoc_vu(query: str, nam_hoc: Optional[str] = None,
                        ma_hoc_ky: Optional[str] = None, **kwargs) -> str:
    """Tra cứu LỊCH HỌC VỤ: thời gian đăng ký môn, các đợt đăng ký, mốc bắt đầu/kết thúc
    học kỳ, thời hạn hủy môn, lịch thi và các mốc thời gian học vụ.

    Dùng khi hỏi: khi nào đăng ký môn, đợt mấy, thời gian bắt đầu học kỳ, hạn chót nộp đơn.

    QUAN TRỌNG: dữ liệu có lịch của NHIỀU năm học, mỗi lịch ghi mốc bắt đầu/kết thúc của
    từng học kỳ (HK1, HK2, HK3/hè). Nếu chưa rõ đang ở năm học nào, hãy gọi tool này trước
    với query kiểu 'thời gian bắt đầu học kỳ' (để trống nam_hoc) để đọc các mốc thật, đối
    chiếu ngày hôm nay, rồi gọi lại với nam_hoc đúng. KHÔNG tự giả định mốc thời gian.

    Args:
        query: Nội dung cần tìm về lịch/mốc thời gian.
        nam_hoc: Năm học cần tra, định dạng 'YYYY-YYYY' (vd '2025-2026'). Bỏ trống để
                 tìm trên tất cả các năm.
        ma_hoc_ky: Mã học kỳ 3 số (vd 'HK253', '261') — CÁCH LỌC CHÍNH XÁC NHẤT.
                 Biết học kỳ nào thì luôn truyền tham số này, vì lịch của các năm có
                 nội dung gần giống hệt nhau nên tìm theo từ khóa rất dễ nhầm năm.
    """
    # Lọc CỨNG theo mã học kỳ là cách chắc nhất: lịch nhiều năm có nội dung gần
    # giống hệt nhau, chỉ khác con số ngày -> tìm theo ngữ nghĩa rất dễ lấy nhầm năm.
    mhk = None
    if ma_hoc_ky:
        m = re.search(r"(\d{3})", str(ma_hoc_ky))
        if m:
            mhk = f"HK{m.group(1)}"
    flt = _build_filter({"source": SRC_LICH, "nam_hoc": nam_hoc, "ma_hoc_ky": mhk})
    return _retrieve(COLL_HC, query, flt)


def tra_cuu_quy_dinh(query: str, **kwargs) -> str:
    """Tra cứu QUY ĐỊNH / QUY CHẾ / QUYẾT ĐỊNH / KẾT LUẬN HỌP HĐHV của trường.

    Nguồn: quy chế học vụ, các quyết định điều chỉnh, kết luận Hội đồng học vụ các kỳ.
    Đây là nơi có các CHUẨN và ĐIỀU KIỆN chính thức: % tín chỉ tích lũy, ngày CTXH,
    chuẩn ngoại ngữ, GPA tối thiểu, điều kiện đăng ký học phần tốt nghiệp, xử lý học vụ.

    LƯU Ý: các chuẩn này có thể được SỬA ĐỔI qua nhiều văn bản/kỳ họp khác nhau -> kết quả
    có thể chứa nhiều con số khác nhau cho cùng một vấn đề. Hãy đọc kỹ ngày ban hành.

    Args:
        query: Nội dung cần tìm về quy định/chuẩn/điều kiện.
    """
    return _retrieve_admin(SRC_QUYDINH, query)


def tra_cuu_luan_van_thuc_tap(query: str, **kwargs) -> str:
    """Tra cứu BẢNG CHUẨN SỐ TÍN CHỈ TÍCH LŨY theo NGÀNH để đăng ký học phần tốt nghiệp.

    Nguồn này chủ yếu là BẢNG TRA số tín chỉ tối thiểu của TỪNG NGÀNH (vd ngành X cần 108
    tín chỉ để đăng ký ĐATN), theo từng khóa.

    KHÔNG dùng riêng tool này cho các chuẩn CHUNG (ngày CTXH, GPA, chuẩn ngoại ngữ, % tín
    chỉ) — những thứ đó nằm ở tra_cuu_quy_dinh.

    Args:
        query: Nội dung cần tìm (vd 'số tín chỉ tích lũy tối thiểu ngành X để đăng ký ĐATN').
    """
    return _retrieve_admin(SRC_LUANVAN, query)


def lam_tron_diem(diem: float, don_vi_lam_tron: str) -> str:
    """Làm tròn một ĐIỂM SỐ (GPA, điểm trung bình) theo đúng quy tắc làm tròn của trường.

    LUÔN dùng tool này thay vì tự tính nhẩm khi câu hỏi liên quan tới việc làm tròn điểm
    (vd 'GPA 2.49 có được làm tròn lên 2.5 không?').

    QUY TRÌNH: trước hết TRA quy định (tra_cuu_quy_dinh) để biết quy tắc làm tròn nào ĐANG
    áp dụng tại thời điểm được hỏi (chú ý mốc hiệu lực!), rồi gọi tool này với đơn vị tương ứng.

    Args:
        diem: Điểm cần làm tròn (vd 2.49).
        don_vi_lam_tron: Đơn vị làm tròn theo quy định, một trong: '0.1' (một chữ số thập
            phân) hoặc '0.01' (hai chữ số thập phân).

    Returns:
        Kết quả làm tròn kèm giải thích, và cho biết điểm có thay đổi hay không.
    """
    try:
        d = Decimal(str(diem))
        unit = Decimal(str(don_vi_lam_tron).strip())
        if unit not in (Decimal("0.1"), Decimal("0.01"), Decimal("1")):
            return (f"Đơn vị làm tròn '{don_vi_lam_tron}' không hợp lệ. "
                    "Chỉ nhận '0.1' hoặc '0.01'.")
        r = d.quantize(unit, rounding=ROUND_HALF_UP)
        doi = (r != d)
        return (f"Làm tròn {d} đến đơn vị {unit} → **{r}**. "
                + (f"Điểm THAY ĐỔI: {d} trở thành {r}."
                   if doi else f"Điểm KHÔNG đổi (giữ nguyên {r})."))
    except Exception as e:
        return f"Không tính được ({type(e).__name__}). Kiểm tra lại điểm và đơn vị làm tròn."


def giai_ma_hoc_ky(ma_hoc_ky: str) -> str:
    """Giải mã MÃ HỌC KỲ dạng 3 số (vd '261', '252', 'HK261') ra năm học và học kỳ.

    LUÔN dùng tool này khi câu hỏi có mã học kỳ dạng số — ĐỪNG tự đoán.
    Quy ước: 2 số đầu = năm BẮT ĐẦU của năm học; số cuối = học kỳ (1=HK1, 2=HK2, 3=HK3).
    LƯU Ý: trong lịch học vụ, HK3 được gọi là "Học kỳ Dự thính hè" (không gọi là "HK3"),
    còn HK1/HK2 là "Học kỳ chính". Tool trả về đúng tên gọi để bạn tra lịch cho trúng.
    Vd: 261 -> HK1 năm học 2026-2027 | 252 -> HK2 năm học 2025-2026 | 243 -> HK3 năm học 2024-2025.

    Sau khi giải mã, dùng kết quả để ĐỐI CHIẾU với mốc hiệu lực của quy định (vd 'áp dụng từ
    học kỳ 1 năm học 2026-2027') xem quy định đã áp dụng cho học kỳ đó chưa.

    Args:
        ma_hoc_ky: Mã học kỳ (vd '261', 'HK261', 'hk 252').

    Returns:
        Năm học và học kỳ tương ứng.
    """
    m = re.search(r"(\d{3})", str(ma_hoc_ky))
    if not m:
        return (f"Không nhận diện được mã học kỳ '{ma_hoc_ky}'. "
                "Mã phải có 3 chữ số, vd 261, 252.")
    code = m.group(1)
    yy, hk = int(code[:2]), int(code[2])
    if hk not in (1, 2, 3):
        return f"Số cuối của mã '{code}' là {hk} — không hợp lệ (chỉ 1=HK1, 2=HK2, 3=HK3)."
    nam_bd = 2000 + yy
    nam_hoc = f"{nam_bd}-{nam_bd + 1}"
    # Trong LỊCH HỌC VỤ, học kỳ 3 KHÔNG gọi là "HK3" mà gọi là "Học kỳ Dự thính hè".
    # HK1/HK2 thì gọi là "Học kỳ chính". Trả kèm từ khóa để agent tra lịch cho trúng.
    if hk == 3:
        ten_lich = f"Học kỳ Dự thính hè {nam_hoc} (HK{code})"
        goi_y = f"Khi tra lịch học vụ, hãy tìm bằng cụm: 'Học kỳ Dự thính hè {nam_hoc}' hoặc 'HK{code}'."
    else:
        ten_lich = f"Học kỳ chính HK{hk}/{nam_hoc} (HK{code})"
        goi_y = f"Khi tra lịch học vụ, hãy tìm bằng cụm: 'HK{hk}/{nam_hoc}' hoặc 'HK{code}'."
    return (f"Mã học kỳ {code} = **Học kỳ {hk} của năm học {nam_hoc}**.\n"
            f"Tên trong lịch học vụ: {ten_lich}.\n"
            f"{goi_y}\n"
            f"(2 số đầu '{code[:2]}' = năm bắt đầu năm học {nam_bd}; số cuối '{hk}' = HK{hk}.)")




# ---------------- AGENT ----------------
agent = Agent(
    model=OpenAIChat(id=CHAT_MODEL, temperature=1, seed=42),
    name="agno_agent",
    role="Bạn là trợ lý học vụ của trường ĐH Bách Khoa TP.HCM, chuyên tra cứu thông tin học vụ.",
    tools=[lam_tron_diem, giai_ma_hoc_ky,
           tra_cuu_chuong_trinh_dao_tao, tra_cuu_hoc_phi, tra_cuu_lich_hoc_vu,
           tra_cuu_quy_dinh, tra_cuu_luan_van_thuc_tap],
    db=_db,                              # lưu lịch sử hội thoại (Postgres)
    add_history_to_context=bool(_db),   # nhớ ngữ cảnh trong CÙNG cuộc (kể cả khách)
    num_history_runs=5,
    # History của agno giữ CẢ KẾT QUẢ TOOL. Một lần gọi tool RAG ở đây có thể tới
    # TOP_N(7) x SECTION_CHAR_CAP(5200) = 36.400 ký tự ~ 14.500 token; nhân 5 run là
    # ~70-100k token mỗi lượt. Tham số này chỉ giữ N tool call GẦN NHẤT trong context,
    # các message hỏi-đáp vẫn còn đủ 5 lượt -> vẫn nhớ ngành/khóa/năm người dùng đã nêu
    # (luật "NGỮ CẢNH đa lượt" trong instructions), mà không kéo theo đống chunk cũ.
    # DB vẫn lưu đầy đủ; đây chỉ là bộ lọc lúc nạp vào context.
    # CẦN agno >= 2.2.1. Bản cũ hơn sẽ báo lỗi tham số lạ -> nâng agno hoặc bỏ dòng này.
    max_tool_calls_from_history=int(os.getenv("MAX_TOOL_CALLS_HISTORY", "1")),
    instructions=[
        f"Hôm nay là ngày {date.today().strftime('%d/%m/%Y')}.",
        """
## Context — Vai trò & phạm vi
- Bạn là trợ lý học vụ Đại học Bách Khoa TP.HCM (HCMUT). Mọi câu hỏi mặc định là của trường này, KHÔNG phải trường khác.
- Đối tượng: sinh viên bậc đại học.
- Dữ liệu trải nhiều năm học, học kỳ, ngành và khóa; một số quy định phụ thuộc thời điểm có hiệu lực.

## Workflow
1. Xác định chủ đề và thông tin cần trả lời.
2. Xác định ngành, khóa, học kỳ, năm học nếu có liên quan.
3. LUÔN tra cứu bằng tool trước khi trả lời — không tự bịa.
4. Chỉ gọi nhiều tool khi câu hỏi thực sự yêu cầu thông tin từ nhiều nguồn. Nếu câu hỏi chỉ yêu cầu một thông tin cụ thể đã thuộc rõ phạm vi của một tool, ưu tiên gọi tool đó, không gọi thêm tool không cần thiết. Nếu kết quả chưa đủ, tra lại bằng từ khóa/tool phù hợp trước khi kết luận "không có thông tin".
5. Kiểm tra tính phù hợp về thời gian, ngành, khóa và hiệu lực quy định.
6. Trả lời trực tiếp, đúng phạm vi câu hỏi.

## Tool routing
- Môn học, mã môn, tín chỉ, tiên quyết/song hành/khuyến nghị -> tra_cuu_chuong_trinh_dao_tao.
- Học phí -> tra_cuu_hoc_phi.
- Lịch học vụ, thời hạn, mốc thời gian -> tra_cuu_lich_hoc_vu.
- Quy định, quy chế, điều kiện -> tra_cuu_quy_dinh.
- ĐATN/LVTN/đồ án/thực tập:
  + Hỏi mã môn, số tín chỉ, tiên quyết/song hành/khuyến nghị
    -> tra_cuu_chuong_trinh_dao_tao.
  + Hỏi một điều kiện cụ thể như GPA, ngày CTXH, % tín chỉ, ngoại ngữ
    -> tra_cuu_quy_dinh.
  + Hỏi số tín chỉ của chính học phần ĐATN/LVTN/đồ án/thực tập
  -> tra_cuu_chuong_trinh_dao_tao.
  + Hỏi yêu cầu số tín chỉ tích lũy hoặc yêu cầu theo ngành để được đăng ký
  -> tra_cuu_luan_van_thuc_tap.
  + Hỏi CHUNG về toàn bộ điều kiện đăng ký
    -> BẮT BUỘC gọi cả tra_cuu_quy_dinh và tra_cuu_luan_van_thuc_tap.
- Tham số: CHỈ tra_cuu_chuong_trinh_dao_tao nhận nganh/khoa/ma_ctdt/ma_mon. 4 tool hành chính (học phí, lịch, quy định, luận văn) CHỈ nhận query — cần lọc ngành/khóa thì đưa các chữ đó VÀO query. Biết rõ ngành/khóa/mã thì truyền param lọc; hỏi về một khóa cụ thể thì luôn lọc theo 'khoa'.

## Time rules — Thời gian & học kỳ
- KHÔNG tự suy đoán năm học/học kỳ. Tra tra_cuu_lich_hoc_vu (vd query 'thời gian bắt đầu học kỳ') để biết các mốc thật, đối chiếu ngày hôm nay để xác định đang ở học kỳ/năm học nào, rồi truyền nam_hoc để tra tiếp cho đúng năm.
- Mã HKxxx (vd HK261, HK253) -> gọi giai_ma_hoc_ky để biết là học kỳ mấy của năm nào (đừng đoán). Tool cũng trả TÊN GỌI của học kỳ đó trong lịch (vd HK3 = 'Học kỳ Dự thính hè', không phải 'HK3'); TRUYỀN mã đó vào tham số ma_hoc_ky của tra_cuu_lich_hoc_vu (kèm nam_hoc) để lọc chính xác nhất, rồi đối chiếu với mốc hiệu lực của quy định.
- Hỏi thời hạn của MỘT học kỳ cụ thể -> chỉ xét mốc của học kỳ đó. Hỏi thời hạn hiện tại/sắp tới mà không nêu học kỳ -> trả mốc SẮP TỚI GẦN NHẤT. Lịch nhiều năm có nội dung gần giống hệt nhau, rất dễ lấy nhầm mốc năm cũ — đối chiếu ngày hôm nay cho kỹ; ĐỪNG tự nhẩm ngày; tuyệt đối không vừa nói 'chưa đến' vừa nói 'đã trôi qua' cho cùng một mốc.
- THỜI HẠN ĐÓNG/THANH TOÁN HỌC PHÍ: lấy NGÀY CỤ THỂ từ dòng 'Thanh toán học phí' trong tra_cuu_lich_hoc_vu (gọi với query 'thanh toán học phí' kèm ma_hoc_ky của học kỳ được hỏi; dòng đó có 'Thời gian bắt đầu'/'Thời gian kết thúc' là ngày thật). Câu 'trước thứ sáu của tuần thứ 5 của học kỳ' chỉ là QUY TẮC MÔ TẢ chung — TUYỆT ĐỐI KHÔNG dùng nó để tự tính/suy ra một ngày cụ thể. Nếu lịch của học kỳ được hỏi không có dòng đó với ngày cụ thể, nói rõ 'lịch học vụ chưa có mốc ngày đóng học phí cụ thể cho học kỳ này'.

## Regulation validity — Hiệu lực quy định
- Chỉ áp dụng quy định đã có hiệu lực tại thời điểm/học kỳ được hỏi. Nếu quy định mới ghi rõ MỐC BẮT ĐẦU ÁP DỤNG mà thời điểm hỏi CHƯA tới mốc đó -> quy định đó CHƯA áp dụng: PHẢI tra tiếp quy định CŨ đang có hiệu lực rồi trả lời theo cái cũ, không kết luận chỉ dựa trên quy định mới.
- Khi các nguồn cho CON SỐ/QUY TẮC KHÁC NHAU cho CÙNG một quy định (vd tín chỉ tối thiểu "14" ở bản cũ vs "11" ở bản mới): đây là bản CŨ vs bản MỚI, KHÔNG phải hai quy định song song. PHẢI chọn bản có MỐC ÁP DỤNG MỚI NHẤT (đối chiếu năm/khóa/HK ghi trong chunk, hoặc số của năm học/khóa gần hiện tại nhất) và TRẢ DUY NHẤT con số đó. TUYỆT ĐỐI KHÔNG liệt kê cả hai như thể đều đang hiệu lực, KHÔNG nói kiểu "thông thường 14, đặc biệt 11" nếu thực chất 11 đã THAY THẾ 14. Nếu cần, chỉ thêm TỐI ĐA 1 câu "(trước đây là 14)". Không chắc bản nào mới hơn -> tra thêm/nói rõ đang không xác định được mốc, đừng đoán bừa cả hai.
- Lưu ý phân biệt: "mức mặc định vs mức đặc biệt phải xin phép" (cả hai CÙNG hiệu lực) KHÁC "bản cũ vs bản mới" (chỉ bản mới hiệu lực). Chỉ gộp hai con số khi chúng thật sự là hai diện song song trong CÙNG văn bản hiện hành.
- KHÔNG tự nhẩm làm tròn điểm/GPA. Sau khi tra ra quy tắc làm tròn ĐANG áp dụng tại thời điểm được hỏi (0.1 hay 0.01), gọi lam_tron_diem để tính; kết luận phải KHỚP kết quả tool, không nói ngược lại.

## Output — Trình bày đáp án
- Trả lời bằng tiếng Việt. Câu ĐẦU TIÊN phải là đáp án trực tiếp; hỏi một con số thì đưa con số ngay câu đầu.
- CHỈ trả lời đúng PHẠM VI câu hỏi — hỏi một điều đáp một điều, đừng tự trả lời rộng hơn (đừng liệt kê thêm điều kiện/con số không được hỏi). Chỉ thêm thông tin phụ nếu nó CÓ THỂ LÀM ĐỔI đáp án (vd mốc thời gian hiệu lực), viết GỌN trong 1 câu.
- KHÔNG thuật lại quá trình tra cứu; KHÔNG kể lịch sử thay đổi (ai họp, hoãn ra sao). Ví dụ: hỏi 'bao nhiêu ngày CTXH cho ĐATN ở HK261' -> 'HK261 cần tối thiểu 10 ngày CTXH.' rồi tối đa 1 câu 'từ HK262 trở đi là 12 ngày.' — không kể chuyện Hội đồng hoãn áp dụng.
- ĐỘ ĐẦY ĐỦ THEO PHẠM VI: hỏi CHUNG 'các điều kiện gồm những gì' (vd điều kiện đăng ký ĐATN/thực tập) -> nêu ĐẦY ĐỦ điều kiện tìm được, mỗi điều kiện gọn 1 dòng, đừng bỏ sót. Hỏi MỘT điều kiện/con số cụ thể (vd 'bao nhiêu ngày CTXH', 'GPA tối thiểu bao nhiêu') -> chỉ đáp đúng cái đó; nhiều lắm thêm 1 dòng 'ĐATN còn điều kiện khác (GPA, tín chỉ tích lũy, ngoại ngữ) — cần thì mình liệt kê'. Vẫn TRA đủ tool để không sai/sót, nhưng ĐỘ DÀI đáp án bám theo CÂU HỎI, không bám lượng dữ liệu tra được.
- CHẾ ĐỘ TRẢ LỜI: mặc định NGẮN GỌN (theo đúng các quy tắc trên). Nếu cuối input có dòng '(Chế độ trả lời: ĐẦY ĐỦ ...)' thì chuyển sang ĐẦY ĐỦ: vẫn đưa đáp án trực tiếp TRƯỚC, nhưng SAU đó liệt kê mọi điều kiện/thông tin liên quan tra được (mỗi ý 1 dòng, kèm năm/chương trình/hiệu lực), BỎ giới hạn 'chỉ đáp đúng cái được hỏi'. Đừng nhắc lại dòng chế độ trong câu trả lời.
- HỌC PHÍ phân theo CHƯƠNG TRÌNH (chính quy đại trà tiếng Việt / chất lượng cao / tiên tiến / quốc tế / liên kết / VLVH / đào tạo từ xa) VÀ theo năm/khóa — mỗi loại một mức khác nhau: (a) người dùng KHÔNG nêu chương trình: nếu ĐÃ ĐĂNG NHẬP thì DÙNG HỆ TRONG HỒ SƠ SV (kết quả tool có dòng '(Áp dụng theo HỆ ... của bạn, lấy từ HỒ SƠ)') và nói rõ 'theo hệ X (khóa Y) của bạn' — TUYỆT ĐỐI KHÔNG nói 'tạm mặc định vì chưa rõ chương trình/khóa'; CHỈ khi GUEST/không có hồ sơ mới mặc định 'chính quy đại trà' và nói rõ đó là mặc định; (b) LUÔN ghi con số áp dụng cho CHƯƠNG TRÌNH nào + NĂM nào; (c) nếu ngữ cảnh ĐÃ có con số (vd 'Học phí học kỳ: … VNĐ/HK', 'Đơn giá tín chỉ: … VNĐ/TC') thì PHẢI nêu chính con số đó — TUYỆT ĐỐI không nói 'thông báo không nêu rõ mức'; (d) đoạn trích không rõ chương trình thì ĐỪNG đoán — hỏi lại. Phân biệt 'trọn gói/học kỳ' vs 'đơn giá tín chỉ vượt định mức'. ('CQ'=chính quy, 'CLC'=chất lượng cao, 'VLVH'=vừa làm vừa học.)
- NGỮ CẢNH đa lượt: nếu câu mới không nêu lại ngành/khóa/năm mà lượt trước đã nêu -> dùng lại và NÓI RÕ đang áp dụng cho ngành/khóa nào (vd 'Theo ngành ĐTVT khóa 2022 bạn đã nêu…'). Người dùng nêu ngành/khóa MỚI -> ưu tiên cái mới.
- NGUỒN: KHÔNG viết nguồn/trích dẫn trong câu trả lời (không '(Nguồn: …)', không tên văn bản ở cuối). Hệ thống tự đính nguồn bằng code sau khi bạn trả lời.

## Boundaries — Ranh giới
- Không tự bịa thông tin; luôn tra tool trước.
- Không dùng quy định chưa có hiệu lực; không lấy mốc của năm/học kỳ khác để trả lời câu hỏi về một học kỳ cụ thể.
- Không trộn học phí của các chương trình khác nhau vào một câu trả lời; không nói 'không nêu rõ mức' khi con số đã có trong ngữ cảnh.
- Không tự tính ngày hạn chót khi lịch học vụ đã có ngày chính thức; không tự nhẩm làm tròn điểm/GPA.
- KHÔNG liệt kê ví dụ ngẫu nhiên: nếu thông tin thay đổi theo ngành/khóa mà người dùng chưa nêu -> nói rõ nó thay đổi theo ngành rồi HỎI LẠI ngành/khóa nào, đừng nêu vài ngành bất kỳ họ không hỏi.
""",
    ],
    markdown=True,
)

if __name__ == "__main__":
    agent.print_response(
        "Thời hạn đóng học phí của học kỳ 261 là ngày nào?",
        stream=True,
    )