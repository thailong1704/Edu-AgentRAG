#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
embed_load_qdrant_hybrid.py
---------------------------
Nạp 2 collection Qdrant với HYBRID: dense (OpenAI) + sparse BM25 (fastembed).
Named vectors: "dense" (VectorParams) + "sparse" (BM25, modifier=IDF).

Chuẩn bị:
  pip install openai qdrant-client "fastembed>=0.3"
  set OPENAI_API_KEY ; chạy Qdrant ; đã có chunks_ctdt.json / chunks_hanh_chinh.json
"""
import os
import json
import time
import uuid
import hashlib
from dotenv import load_dotenv
load_dotenv()  # đọc .env nếu có
from openai import OpenAI
from qdrant_client import QdrantClient, models
from fastembed import SparseTextEmbedding

# ---------------- CẤU HÌNH ----------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
EMBED_MODEL    = "text-embedding-3-large"
EMBED_DIMENSIONS = None
SPARSE_MODEL   = "Qdrant/bm25"        # BM25 (không phụ thuộc ngôn ngữ, khớp mã tốt)

QDRANT_URL     = "http://localhost:6333"
QDRANT_API_KEY = None
BATCH_SIZE = 100
RECREATE   = os.getenv("KB_RECREATE", "0") == "1"   # 0 = INCREMENTAL (chỉ embed cái mới/đổi);
                                                    # 1 = xoá sạch & embed lại toàn bộ

# Ghép NGỮ CẢNH (doc_title > section_path) vào text ĐEM ĐI EMBED.
# Payload vẫn lưu text GỐC sạch để hiển thị -> câu trả lời không bị lẫn tiêu đề.
# Lý do: chunk dữ liệu thô (vd "- Học phí học kỳ: 15.750.000 VNĐ/HK") tự nó không
# chứa từ khoá người dùng hỏi ("mức thu", "chương trình CQ") -> khó được tìm thấy.
EMBED_WITH_CONTEXT = True
DISTANCE   = models.Distance.COSINE

OUTPUT_DIR = r"C:\Users\ADMIN\Desktop\agent_A\data\KnowledgeGraph\output"
INPUT_FILES = {
    "ctdt":       os.path.join(OUTPUT_DIR, "chunks_ctdt.json"),
    "hanh_chinh": os.path.join(OUTPUT_DIR, "chunks_hanh_chinh.json"),
}
INDEX_FIELDS = {
    "ctdt":       {"keyword": ["source", "ma_ctdt", "nganh", "khoa", "ma_mon", "chunk_type", "collection", "doc_key", "section_path"],
                   "integer": ["chunk_index", "hoc_ky"], "bool": ["has_table"]},
    "hanh_chinh": {"keyword": ["source", "doc_date", "nam_hoc", "ma_hoc_ky", "chunk_type", "collection", "doc_key", "section_path", "chuong_trinh", "bac", "khoa", "nganh", "ma_nganh"],
                   "integer": ["chunk_index", "hoc_ky"], "bool": ["has_table"]},
}
LIFT_FROM_EXTRA = ["ma_ctdt", "nganh", "khoa", "ma_mon", "ten_mon", "ten_nhom", "tong_tin_chi", "loai_chunk"]

DENSE_NAME  = "dense"
SPARSE_NAME = "sparse"

_sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL)


def model_dim() -> int:
    if EMBED_DIMENSIONS:
        return EMBED_DIMENSIONS
    return 1536 if EMBED_MODEL.endswith("-small") else 3072


def embed_text(chunk):
    """Text ĐEM ĐI EMBED = ngữ cảnh (doc_title > section_path) + text gốc.
       Trả về text gốc nếu tắt EMBED_WITH_CONTEXT."""
    text = chunk.get("text", "") or ""
    if not EMBED_WITH_CONTEXT:
        return text
    md = chunk.get("metadata", {}) or {}
    parts = []
    for k in ("doc_title", "section_path"):
        v = (md.get(k) or "").strip()
        if v and v.lower() not in text[:200].lower():   # tránh lặp nếu text đã chứa
            parts.append(v)
    if not parts:
        return text
    return " > ".join(parts) + "\n" + text


def build_payload(chunk):
    md = chunk.get("metadata", {}) or {}
    ex = md.get("extra", {}) or {}
    cid = chunk.get("id", "") or ""
    doc_key, _, idx = cid.rpartition("_")        # 'src__file_0007' -> ('src__file', '0007')
    try:
        chunk_index = int(idx)
    except ValueError:
        doc_key, chunk_index = cid, 0
    p = {
        "text": chunk.get("text", ""), "chunk_id": cid,
        "doc_key": doc_key, "chunk_index": chunk_index,   # để dựng lại section/đoạn
        "collection": md.get("collection"), "source": md.get("source"),
        "nam_hoc": md.get("nam_hoc"),
        "ma_hoc_ky": md.get("ma_hoc_ky"), "hoc_ky": md.get("hoc_ky"),
        "doc_title": md.get("doc_title"), "doc_date": md.get("doc_date"),
        "section_path": md.get("section_path"), "chunk_type": md.get("chunk_type"),
        "n_tokens": md.get("n_tokens"), "has_table": md.get("has_table"),
        # metadata phân loại (do normalize.enrich_phan_loai gắn ở top-level md)
        "chuong_trinh": md.get("chuong_trinh"), "bac": md.get("bac"),
        "khoa": md.get("khoa"), "nganh": md.get("nganh"), "ma_nganh": md.get("ma_nganh"),
    }
    for k in LIFT_FROM_EXTRA:
        if k in ex and ex[k] is not None:
            p[k] = ex[k]
    p["extra"] = ex
    return {k: v for k, v in p.items() if v is not None}


def point_id(chunk_id): return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))
def batched(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def embed_dense(oc, texts, max_retry=5):
    kwargs = {"model": EMBED_MODEL, "input": texts}
    if EMBED_DIMENSIONS:
        kwargs["dimensions"] = EMBED_DIMENSIONS
    for attempt in range(max_retry):
        try:
            resp = oc.embeddings.create(**kwargs)
            return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
        except Exception as e:
            if attempt == max_retry - 1:
                raise
            time.sleep(2 ** attempt)
            print(f"    ! retry embedding: {type(e).__name__}")


def embed_sparse(texts):
    """Trả list[models.SparseVector] cho từng text (BM25 qua fastembed)."""
    out = []
    for emb in _sparse_model.embed(list(texts)):
        out.append(models.SparseVector(indices=emb.indices.tolist(), values=emb.values.tolist()))
    return out


def ensure_collection(qc, name, dim):
    exists = qc.collection_exists(name)
    if RECREATE and exists:
        qc.delete_collection(name); exists = False
    if not exists:
        qc.create_collection(
            collection_name=name,
            vectors_config={DENSE_NAME: models.VectorParams(size=dim, distance=DISTANCE)},
            sparse_vectors_config={SPARSE_NAME: models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
    fields = INDEX_FIELDS.get(name, {})
    for f in fields.get("keyword", []):
        try: qc.create_payload_index(name, f, models.PayloadSchemaType.KEYWORD)
        except Exception: pass
    for f in fields.get("integer", []):
        try: qc.create_payload_index(name, f, models.PayloadSchemaType.INTEGER)
        except Exception: pass
    for f in fields.get("bool", []):
        try: qc.create_payload_index(name, f, models.PayloadSchemaType.BOOL)
        except Exception: pass


def _hash_chunk(c) -> str:
    """Hash nội dung dùng-để-embed + payload -> đổi khi text hoặc metadata đổi."""
    key = embed_text(c) + "\x00" + json.dumps(build_payload(c), ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _manifest_path(name):
    return os.path.join(OUTPUT_DIR, f".embed_manifest_{name}.json")


def _load_manifest(name):
    p = _manifest_path(name)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def load_collection(oc, qc, name, path, dim):
    with open(path, encoding="utf-8") as fh:
        chunks = json.load(fh)
    ensure_collection(qc, name, dim)

    # INCREMENTAL: so hash từng chunk với lần embed trước (manifest) -> chỉ làm cái MỚI/ĐỔI.
    manifest = {} if RECREATE else _load_manifest(name)
    cur, todo = {}, []
    for c in chunks:
        pid = point_id(c["id"])
        h = _hash_chunk(c)
        cur[pid] = h
        if manifest.get(pid) != h:          # mới hoặc đã đổi -> cần embed lại
            todo.append(c)
    goners = [pid for pid in manifest if pid not in cur]   # chunk biến mất -> xoá điểm

    print(f"\n[{name}] {len(chunks)} chunk | mới/đổi: {len(todo)} | "
          f"giữ nguyên: {len(chunks) - len(todo)} | xoá: {len(goners)}")

    if goners:
        qc.delete(collection_name=name, points_selector=models.PointIdsList(points=goners))

    total = 0
    for batch in batched(todo, BATCH_SIZE):
        texts = [embed_text(c) for c in batch]        # ngữ cảnh + text, dùng để EMBED
        dvecs = embed_dense(oc, texts)
        svecs = embed_sparse(texts)
        points = [
            models.PointStruct(id=point_id(c["id"]),
                               vector={DENSE_NAME: d, SPARSE_NAME: s},
                               payload=build_payload(c))
            for c, d, s in zip(batch, dvecs, svecs)
        ]
        qc.upsert(collection_name=name, points=points)
        total += len(points)
        print(f"    embed {total}/{len(todo)}")

    with open(_manifest_path(name), "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False)         # lưu manifest cho lần sau

    if not todo and not goners:
        print(f"    (không có gì mới — bỏ qua embed) tổng {qc.get_collection(name).points_count} điểm")
    else:
        print(f"    XONG: {qc.get_collection(name).points_count} điểm "
              f"(embed lần này: {total}, xoá: {len(goners)})")
    return total


def main():
    if not OPENAI_API_KEY:
        raise SystemExit("Chưa đặt OPENAI_API_KEY.")
    dim = model_dim()
    print("=" * 64)
    print(f"HYBRID | dense={EMBED_MODEL}({dim}) + sparse={SPARSE_MODEL} | RRF sẵn sàng cho query")
    print("=" * 64)
    oc = OpenAI(api_key=OPENAI_API_KEY)
    qc = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    grand = 0
    for name, path in INPUT_FILES.items():
        if not os.path.exists(path):
            print(f"[{name}] BỎ QUA — thiếu {path}"); continue
        grand += load_collection(oc, qc, name, path, dim)
    print(f"\nHOÀN TẤT: {grand} điểm (dense+sparse) vào Qdrant.")


if __name__ == "__main__":
    main()