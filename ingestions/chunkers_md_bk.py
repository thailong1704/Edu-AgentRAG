import re, json
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ───────────────────────── tiện ích & mô hình dữ liệu ─────────────────────────

def _clean(s):  return re.sub(r'\s+', ' ', s.replace('**', '')).strip()
def _ntok(t):   return max(1, round(len(t) / 3.5))
def _cells(row): return [c.strip() for c in row.strip().strip('|').split('|')]
def _is_row(l): return l.strip().startswith('|')

_RE_ROMAN  = re.compile(r'^[IVXLCDM]+\.\s+\S')
_RE_ARABIC = re.compile(r'^\d+\.\s+\S')
_RE_LETTER = re.compile(r'^[a-zđ][.)]\s+\S', re.I)
_RE_SUB    = re.compile(r'^[a-zđ]\)\s')
_RE_CAP    = re.compile(r'^_*\s*Bảng\s+[\d.]+', re.I)

def _md_heading(line):
    """(level, text) nếu cả dòng in đậm & bắt đầu La Mã/số/chữ; ngược lại None."""
    raw = line.strip()
    if not (raw.startswith('**') or raw.startswith('_**')):
        return None
    t = re.sub(r'[*_]+', '', raw).strip()
    if not t: return None
    if _RE_ROMAN.match(t):  return 1, t
    if _RE_ARABIC.match(t): return 2, t
    if _RE_LETTER.match(t): return 3, t
    return None

def _span_heading_kind(text):
    """Phân loại hàng-1-ô trong bảng-layout: 'section' (La Mã) | 'banner' (HK) | None."""
    if re.match(r'^[IVX]+\s*[.\-)]', text):  return 'section'
    if re.match(r'^(HK|Học kỳ)', text, re.I): return 'banner'
    return None


@dataclass
class Chunk:
    id: str
    heading_path: str
    section: str
    type: str
    content: str
    source_file: str = ""
    approx_tokens: int = 0
    has_table: bool = False
    n_tables: int = 0

    def __post_init__(self):
        if not self.approx_tokens:
            self.approx_tokens = _ntok(self.content)

    def to_dict(self):
        return asdict(self)


# ──────────────────────────────── lớp cơ sở ────────────────────────────────

class Chunker:
    """Lớp cơ sở: quản lý danh sách chunk, cấp id, và các tiện ích I/O."""

    def __init__(self):
        self._chunks = []
        self.source_file = ""
        
    # --- API con phải cài đặt ---
    def chunk(self, md: str):
        raise NotImplementedError

    # --- tiện ích dùng chung ---
    def _reset(self):
        self._chunks = []

    def _emit(self, content, heading_path, section, ctype, has_table=False, n_tables=0):
        content = content.strip()
        if not content:
            return
        self._chunks.append(Chunk(
            id=f'chunk_{len(self._chunks)+1:03d}',
            heading_path=heading_path,
            section=section,
            type=ctype,
            content=content,
            source_file=self.source_file,
            has_table=has_table,
            n_tables=n_tables
            ))

    def _renumber(self):
        for i, c in enumerate(self._chunks, 1):
            c.id = f'chunk_{i:03d}'
        return self._chunks

    def chunk_file(self, path):
        with open(path, encoding='utf-8') as f:
            return self.chunk(f.read())

    @staticmethod
    def to_jsonl(chunks, path):
        with open(path, 'w', encoding='utf-8') as f:
            for c in chunks:
                f.write(json.dumps(c.to_dict(), ensure_ascii=False) + '\n')

    @staticmethod
    def detect(md: str):
        """Trả về instance chunker phù hợp với cấu trúc file."""
        lines = md.replace('\r', '').split('\n')
        non_empty = [l for l in lines if l.strip()]
        non_tbl = [l for l in non_empty if not _is_row(l) and not l.startswith('#')]
        
        # 1) có ≥2 heading in đậm ngoài bảng -> văn bản phân cấp
        if sum(bool(_md_heading(l)) for l in non_tbl) >= 2:
            return HierarchicalChunker()
            
        # 2) có hàng-1-ô là heading (La Mã/HK) trong bảng -> bảng-layout
        for l in non_empty:
            if _is_row(l):
                ne = [c for c in _cells(l) if c]
                if len(ne) == 1 and _span_heading_kind(_clean(ne[0])):
                    return LayoutTableChunker()
                    
        # 3) Bảng dữ liệu rộng (Nếu phần lớn file là bảng và có >= 3 dòng)
        rows = [l for l in non_empty if _is_row(l)]
        data_rows = [c for c in rows if not all(cell == '' or set(cell) <= set('-') for cell in _cells(c))]
        if len(data_rows) >= 3 and (len(rows) / len(non_empty) > 0.3 if non_empty else True):
            return WideTable2HChunker()
            
        # 4) CÒN LẠI: Mặc định an toàn nhất là xử lý như văn bản phân cấp
        return HierarchicalChunker()

    @classmethod
    def auto(cls, md: str):
        return cls.detect(md).chunk(md)


# ─────────────────── 1) văn bản phân cấp + bảng (học phí) ───────────────────

class HierarchicalChunker(Chunker):
    def __init__(self, max_tokens=512, min_tokens=40):
        super().__init__()
        self.max_tokens = max_tokens
        self.min_tokens = min_tokens

    @staticmethod
    def _is_cap(l): return bool(_RE_CAP.match(l.strip()))

    def _split_table(self, text):
        """Bảng 'mỗi dòng = 1 mục' -> nhiều bảng con (caption+header+dòng nhóm+1 dòng)."""
        lines = text.split('\n')
        cap = lines[0] if lines and not _is_row(lines[0]) else None
        rows = [l for l in lines if _is_row(l)]
        if len(rows) < 3:
            return [text]
        header, sep = rows[0], rows[1]
        body = lines[lines.index(sep) + 1:]
        out, group, buf = [], None, []

        def emit_row(dl):
            return '\n'.join(([cap] if cap else []) + [header, sep] + ([group] if group else []) + dl)

        for l in body:
            if _is_row(l):
                cs = [c for c in _cells(l) if c]
                if len(cs) <= 1 and '**' in l:
                    if buf: out.append(emit_row(buf)); buf = []
                    group = l; continue
                if not cs:
                    continue
                if buf: out.append(emit_row(buf))
                buf = [l]
            elif buf and l.strip():
                buf.append(l)
        if buf: out.append(emit_row(buf))
        return out or [text]

    def _build_blocks(self, lines):
        blocks, i, n = [], 0, len(lines)
        while i < n:
            s = lines[i].strip()
            if not s:
                i += 1; continue
            h = _md_heading(lines[i])
            if h:
                blocks.append({'kind': 'heading', 'level': h[0], 'text': h[1]}); i += 1; continue
            if self._is_cap(lines[i]) or _is_row(lines[i]):
                buf = []
                if self._is_cap(lines[i]):
                    buf.append(s); i += 1
                    while i < n and not lines[i].strip(): i += 1

                def resumes(p):
                    seen = 0
                    while p < n and seen < 5:
                        if not lines[p].strip(): p += 1; continue
                        if _md_heading(lines[p]) or self._is_cap(lines[p]): return False
                        if _is_row(lines[p]): return True
                        seen += 1; p += 1
                    return False

                while i < n:
                    if _is_row(lines[i]): buf.append(lines[i].rstrip()); i += 1; continue
                    if _md_heading(lines[i]): break
                    if resumes(i + 1):
                        if lines[i].strip(): buf.append(lines[i].rstrip())
                        i += 1; continue
                    break
                blocks.append({'kind': 'table', 'text': '\n'.join(buf)}); continue

            buf = []
            while i < n:
                s = lines[i].strip()
                if not s: i += 1; break
                if _md_heading(lines[i]) or _is_row(lines[i]) or self._is_cap(lines[i]): break
                if buf and _RE_SUB.match(s): break
                buf.append(s); i += 1
            if buf: blocks.append({'kind': 'text', 'text': '\n'.join(buf)})
        return blocks

    def chunk(self, md, doc_title=None):
        self._reset()
        lines = md.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        if doc_title is None:
            head = []
            for ln in lines:
                if not ln.strip(): continue
                if _md_heading(ln): break
                head.append(re.sub(r'[*_]+', '', ln.strip()))
                if len(head) >= 2: break
            doc_title = ' – '.join(h for h in head if h) or 'Tài liệu'

        stack, pending, path = {}, [], []

        def emit(blocks):
            body = '\n\n'.join(b['text'] for b in blocks).strip()
            if not body: return
            nt = sum(b['kind'] == 'table' for b in blocks)
            crumb = ' > '.join([doc_title] + path)
            self._emit(f"[NGỮ CẢNH] {crumb}\n\n{body}", crumb,
                       path[-1] if path else doc_title,
                       'table' if nt else 'text', has_table=bool(nt), n_tables=nt)

        def flush():
            cur, tok = [], 0
            for b in pending:
                if b['kind'] == 'table' and _ntok(b['text']) > self.max_tokens:
                    parts = self._split_table(b['text'])
                    if len(parts) > 1:
                        if cur: emit(cur); cur, tok = [], 0
                        for p in parts: emit([{'kind': 'table', 'text': p}])
                        continue
                bt = _ntok(b['text'])
                if cur and tok + bt > self.max_tokens:
                    emit(cur); cur, tok = [], 0
                cur.append(b); tok += bt
            if cur: emit(cur)
            pending.clear()

        for b in self._build_blocks(lines):
            if b['kind'] == 'heading':
                flush()
                for L in [L for L in stack if L >= b['level']]: del stack[L]
                stack[b['level']] = b['text']
                path[:] = [stack[L] for L in sorted(stack)]
            else:
                if not pending: path[:] = [stack[L] for L in sorted(stack)]
                pending.append(b)
        flush()
        self._merge_small()
        return self._renumber()

    def _merge_small(self):
        merged = []
        for c in self._chunks:
            p = merged[-1] if merged else None
            if (p and c.approx_tokens < self.min_tokens and p.heading_path == c.heading_path
                    and p.approx_tokens + c.approx_tokens <= self.max_tokens):
                head = p.content.split('\n\n', 1)
                tail = c.content.split('\n\n', 1)[1]
                p.content = head[0] + '\n\n' + head[1] + '\n\n' + tail
                p.approx_tokens = _ntok(p.content)
                p.n_tables += c.n_tables
                p.has_table = p.has_table or c.has_table
                p.type = 'table' if p.has_table else 'text'
            else:
                merged.append(c)
        self._chunks = merged


# ─────────────────── 2) bảng-làm-layout (lịch học vụ) ───────────────────

class LayoutTableChunker(Chunker):
    @staticmethod
    def _logical_rows(lines):
        pre, rows = [], []
        for l in lines:
            s = l.rstrip()
            if not s.strip():
                continue
            if s.lstrip().startswith('|'):
                rows.append(s)
            elif rows:
                rows[-1] += ' ' + s.strip()
            else:
                pre.append(s.strip())
        return pre, rows

    @staticmethod
    def _is_sep(cs):   return all(c == '' or set(c) <= set('-') for c in cs)

    @staticmethod
    def _is_header(cs):
        ne = [c for c in cs if c]
        return len(ne) >= 2 and all(len(c) <= 45 and not re.match(r'^\d', c) for c in ne)

    @staticmethod
    def _spanning(cs):
        ne = [c for c in cs if c]
        return ne[0] if len(ne) == 1 else None

    def chunk(self, md):
        self._reset()
        lines = md.replace('\r', '').split('\n')
        cell = lambda r: [_clean(c) for c in r.strip().strip('|').split('|')]
        pre, rows = self._logical_rows(lines)

        title = next((_clean(l[2:]) for l in pre if l.startswith('##')), None) \
                or ' – '.join(_clean(l) for l in pre[:2]) or 'Tài liệu'
        intro = [l for l in pre if not l.startswith('##')]

        banner = section = None
        headers = []

        def crumb():
            return ' > '.join(x for x in [title, banner, section] if x)

        def emit(body, tag):
            self._emit(f"[NGỮ CẢNH] {crumb()}\n\n{body}", crumb(),
                       section or banner or title, tag)

        if intro:
            emit('\n'.join(intro), 'intro')

        for r in rows:
            cs = cell(r)
            if self._is_sep(cs):
                continue
            sp = self._spanning(cs)
            if sp is not None:
                k = _span_heading_kind(sp)
                if k == 'banner':    banner, section = sp, None
                elif k == 'section': section = sp
                else:                emit(sp, 'note')
                continue
            if self._is_header(cs):
                headers = cs
                continue
            parts = []
            for i, c in enumerate(cs):
                if not c and i:
                    continue
                if i < len(headers):
                    parts.append(f'{headers[i]}: {c}')
                else:
                    parts.append(c if i == 0 else f'Cột {i+1}: {c}')
            emit('\n'.join(parts), 'row')
        return self._renumber()


# ─────────────────── 3) bảng dữ liệu rộng, header 2 tầng ───────────────────

class WideTable2HChunker(Chunker):
    def chunk(self, md):
        self._reset()
        lines = md.replace('\r', '').split('\n')
        cell = lambda r: [_clean(c) for c in r.strip().strip('|').split('|')]
        is_sep = lambda cs: all(c == '' or set(c) <= set('-') for c in cs)

        title = next((_clean(l[1:]) for l in lines if l.startswith('#')), 'Tài liệu')
        
        # ==========================================================
        # BẢN VÁ LỖI: HÀN GẮN CÁC DÒNG BẢNG BỊ ĐỨT GÃY
        # ==========================================================
        merged_lines = []
        for l in lines:
            s = l.rstrip()
            if not s.strip():
                continue
            
            if s.lstrip().startswith('|') or s.startswith('#'):
                merged_lines.append(s)
            elif merged_lines and merged_lines[-1].lstrip().startswith('|'):
                # Phân biệt ghi chú trong ô (thường có dấu | ở cuối) và ghi chú chung ở cuối bài
                if (s.lstrip().startswith('_Lưu ý:_') or s.lstrip() == 'Lưu ý:') and not s.endswith('|'):
                     merged_lines.append(s) # Đẩy ra khỏi bảng, thành dòng note riêng
                else:
                     # Gắn các gạch đầu dòng và text bị rớt lại vào trong ô của bảng
                    merged_lines[-1] += ' ' + s.strip()
            else:
                merged_lines.append(s)
                
        # Dùng merged_lines thay vì lines gốc để lọc rows và notes
        rows = [l for l in merged_lines if l.lstrip().startswith('|')]
        notes = [_clean(l) for l in merged_lines
                 if l.strip() and not l.lstrip().startswith('|') and not l.startswith('#')]
        # ==========================================================

        data = [cell(r) for r in rows if not is_sep(cell(r))]
        if not data:
            return HierarchicalChunker().chunk(md)

        # Trích xuất Header thông minh và an toàn
        labels = []
        body = []
        
        if len(data) >= 2:
            # Bảng 1 tầng có dòng Title (Dòng 1 có <= 1 ô, Dòng 2 nhiều ô)
            if sum(1 for c in data[0] if c) <= 1 and sum(1 for c in data[1] if c) > 1:
                labels = data[1]
                body = data[2:]
                first_cell = next((c for c in data[0] if c), '')
                if first_cell: title = f"{title} - {first_cell}"
                
            # Bảng 2 tầng (Dòng 1 và 2 đều nhiều ô)
            elif len(data[0]) < len(data[1]):
                group, sub = data[0], data[1]
                ncol = max(len(r) for r in data[2:]) if len(data) > 2 else len(sub)
                pairs = len(sub) // 2
                singles = ncol - pairs * 2
                if singles < 0: singles = 0
                
                labels = list(group[:singles])
                for k in range(pairs):
                    g = group[singles + k] if singles + k < len(group) else (group[-1] if group else f'Nhóm {k+1}')
                    sub_1 = sub[2*k] if 2*k < len(sub) else ''
                    sub_2 = sub[2*k+1] if 2*k+1 < len(sub) else ''
                    labels += [f'{g} — {sub_1}', f'{g} — {sub_2}']
                body = data[2:]
                
            # Bảng 1 tầng tiêu chuẩn
            else:
                labels = data[0]
                body = data[1:]
        else:
            labels = data[0]
            body = []

        ncol = max((len(r) for r in body), default=len(labels))
        
        # Đệm thêm nhãn Cột nếu số lượng dữ liệu lớn hơn số lượng Header
        while len(labels) < ncol:
            labels.append(f'Cột {len(labels)+1}')

        def emit(section, body_text, tag):
            crumb = f'{title} > {section}' if section else title
            self._emit(f'[NGỮ CẢNH] {crumb}\n\n{body_text}', crumb, section or title, tag)

        for r in body:
            if not any(r): continue
            r = r + [''] * (ncol - len(r))
            parts = [f'{labels[i]}: {r[i]}' for i in range(ncol) if r[i]]
            # Chọn cột làm tên section: Ưu tiên cột 2, nếu không thì cột 1
            section_name = r[1] if len(r) > 1 and r[1] else (r[0] if r else '')
            emit(section_name, '\n'.join(parts), 'row')
            
        if notes:
            emit(None, '\n'.join(notes), 'note')
            
        return self._renumber()


# ──────────────────────────── hàm tiện dụng ────────────────────────────

def chunk_text(md: str):
    return Chunker.auto(md)

def chunk_file(path: str):
    with open(path, encoding='utf-8') as f:
        return Chunker.auto(f.read())


def split_markdown_by_h1(text):
    """
    Tách một file Markdown tổng hợp thành nhiều bài viết nhỏ 
    dựa trên thẻ Heading 1 (#).
    """
    sections = []
    current_section = []
    
    for line in text.split('\n'):
        if line.startswith('# '):
            if current_section:
                sections.append('\n'.join(current_section))
            current_section = [line]
        else:
            current_section.append(line)
            
    if current_section:
        sections.append('\n'.join(current_section))
        
    return [s.strip() for s in sections if s.strip()]


if __name__ == "__main__":
    # ==========================
    # CẤU HÌNH
    # ==========================
    INPUT_ROOT = Path(
        r"C:\Users\ADMIN\Desktop\agent_A\data\output\bksi_md"
    )

    OUTPUT_ROOT = Path(
        r"C:\Users\ADMIN\Desktop\agent_A\data\KnowledgeGraph\output\chunks\bksi_md"
    )

    md_files = sorted(INPUT_ROOT.rglob("*.md"))

    print(f"Tìm thấy {len(md_files)} file Markdown.\n")

    for md_file in md_files:
        print("=" * 80)
        print(md_file.relative_to(INPUT_ROOT))

        with open(md_file, "r", encoding="utf-8") as f:
            text = f.read()

        # ==========================================================
        # TÁCH FILE VÀ CHUNKING LINH HOẠT
        # ==========================================================
        sub_docs = split_markdown_by_h1(text)
        all_chunks = []
        
        for i, sub_doc in enumerate(sub_docs, 1):
            chunker = Chunker.detect(sub_doc)
            chunker.source_file = md_file.name
            all_chunks.extend(chunker.chunk(sub_doc))
            
        for i, c in enumerate(all_chunks, 1):
            c.id = f'chunk_{i:03d}'
            
        chunks = all_chunks
        # ==========================================================

        relative = md_file.relative_to(INPUT_ROOT)
        json_path = OUTPUT_ROOT / relative.with_suffix(".json")
        jsonl_path = OUTPUT_ROOT / relative.with_suffix(".jsonl")

        json_path.parent.mkdir(parents=True, exist_ok=True)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump([c.to_dict() for c in chunks], f, ensure_ascii=False, indent=2)

        with open(jsonl_path, "w", encoding="utf-8") as f:
            for c in chunks:
                f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")

        if not chunks:
            print(f"Bỏ qua: Không có chunk nào được tạo ra.")
            continue

        toks = [c.approx_tokens for c in chunks]

        print(f"Số bài viết : {len(sub_docs)}")
        print(f"Số chunk    : {len(chunks)}")
        print(f"Token       : min={min(toks)} max={max(toks)} tb={round(sum(toks)/len(toks))}")
        print(f"JSON        : {json_path}")
        print(f"JSONL       : {jsonl_path}")

    print("\nHoàn thành.")