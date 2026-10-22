
import re
import json
import sys
import unicodedata
from pathlib import Path
from dataclasses import dataclass, field
from collections import Counter

import pdfplumber


# Sentinel bao quanh khối Markdown của bảng, giúp các bước sau nhận ra "đây là
# 1 bảng nguyên khối" và KHÔNG cắt đôi nó khi chia chunk.
TABLE_SENTINEL = "\u241e"  # ký tự hiếm (Record Separator symbol), gần như không xuất hiện trong văn bản


# =============================================================================
# DATA MODELS — dữ liệu truyền giữa các bước trong pipeline
# =============================================================================

@dataclass
class Line:
    """1 dòng text thô, gắn với số trang — output của Layout Parser."""
    text: str
    page: int
    top: float = 0.0          # toạ độ 'top' của dòng trên trang (để đối chiếu bbox bảng)
    is_table: bool = False    # True nếu 'dòng' này thực chất là 1 bảng (Markdown) do Docling trích


@dataclass
class HeadingMatch:
    """Một dòng được nhận diện là heading, kèm cấp độ phân cấp."""
    line_index: int      # vị trí dòng trong danh sách lines (sau khi làm sạch)
    text: str
    level: int            # 1 = cấp cao nhất (VD Chương), số càng lớn càng sâu
    pattern_name: str
    consumed_indices: list = field(default_factory=list)  # các dòng nối tiếp (tiêu đề xuống dòng) đã gộp vào text


@dataclass
class Section:
    """1 node trong cây phân cấp — output của Hierarchy Builder."""
    heading: str
    level: int
    pattern_name: str
    page: int
    body_lines: list = field(default_factory=list)   # text thuộc trực tiếp mục này
    children: list = field(default_factory=list)      # list[Section]
    parent_path: list = field(default_factory=list)   # breadcrumb các heading cha


@dataclass
class Chunk:
    chunk_id: str
    text: str
    metadata: dict

@dataclass
class ExtractedTable:
    page: int
    top: float          # cạnh trên của bbox bảng (toạ độ pdfplumber, gốc trên-trái)
    bottom: float       # cạnh dưới của bbox bảng
    markdown: str       # nội dung bảng dạng Markdown


class DoclingTableExtractor:
    """
    Chạy Docling 1 lần trên PDF, trả về danh sách bảng kèm vị trí để LayoutParser
    biết vùng nào trên trang là bảng và thay thế bằng Markdown tương ứng.

    Docling và pdfplumber dùng hệ toạ độ khác nhau:
      - pdfplumber: gốc (0,0) ở GÓC TRÊN-TRÁI, 'top' tăng khi đi xuống.
      - Docling (PDF chuẩn): gốc ở GÓC DƯỚI-TRÁI, 'y' tăng khi đi lên.
    -> Cần lật trục y theo chiều cao trang để quy về hệ pdfplumber.
    """

    def __init__(self):
        self._available = self._check_availability()

    @staticmethod
    def _check_availability() -> bool:
        try:
            import docling  # noqa: F401
            return True
        except Exception:
            return False

    @property
    def available(self) -> bool:
        return self._available

    def extract(self, pdf_path: str) -> list[ExtractedTable]:
        if not self._available:
            return []

        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        result = converter.convert(pdf_path)
        doc = result.document

        tables: list[ExtractedTable] = []
        for table in getattr(doc, "tables", []):
            try:
                md = table.export_to_markdown(doc).strip()
            except TypeError:
                # một số phiên bản Docling: export_to_markdown() không cần tham số
                md = table.export_to_markdown().strip()
            if not md:
                continue

            page_no, top, bottom = self._resolve_position(table, doc)
            tables.append(ExtractedTable(page=page_no, top=top, bottom=bottom, markdown=md))

        return tables

    def _resolve_position(self, table, doc):
        """Lấy (page, top, bottom) của bảng, quy đổi về hệ toạ độ pdfplumber."""
        try:
            prov = table.prov[0]
            page_no = prov.page_no
            bbox = prov.bbox  # Docling BoundingBox

            page = doc.pages[page_no]
            page_height = page.size.height

            # Docling bbox theo gốc dưới-trái: l, t, r, b (t > b vì y hướng lên).
            # Lật về pdfplumber (gốc trên-trái).
            top_pdfplumber = page_height - bbox.t
            bottom_pdfplumber = page_height - bbox.b
            lo, hi = sorted((top_pdfplumber, bottom_pdfplumber))
            return page_no, lo, hi
        except Exception:
            # Không lấy được vị trí -> đánh dấu page=-1 để chèn vào cuối (an toàn)
            return -1, 0.0, 0.0



class LayoutParser:
    """
    Trích xuất text theo TOẠ ĐỘ TỪNG CHỮ thay vì dùng extract_text() mặc định.
    Lý do: extract_text() của pdfplumber đôi khi gộp nhầm số trang (nằm cuối
    trang, sát dòng nội dung cuối) vào chung 1 dòng với nội dung, gây ra rác
    kiểu "...nhiệm.3" trong chunk. Group theo toạ độ 'top' với dung sai nhỏ
    giúp tách đúng dòng nội dung và dòng số trang thành 2 dòng riêng biệt.

    Nếu được cấp danh sách bảng (từ DoclingTableExtractor): các dòng pdfplumber
    rơi vào vùng bbox của một bảng sẽ bị BỎ, và thay bằng 1 Line đặc biệt
    (is_table=True) chứa Markdown của bảng — đặt đúng vị trí theo toạ độ.
    """
    def __init__(self, y_tolerance: float = 3.0, tables: list[ExtractedTable] | None = None):
        self.y_tolerance = y_tolerance
        self.tables = tables or []

    def parse(self, pdf_path: str) -> list[Line]:
        lines: list[Line] = []
        tables_by_page = self._group_tables_by_page()

        with pdfplumber.open(pdf_path) as pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
                page_tables = tables_by_page.get(page_no, [])

                page_lines: list[Line] = []
                for row in self._group_into_rows(words):
                    row_top = min(w["top"] for w in row)
                    # Nếu dòng này nằm trong vùng của bất kỳ bảng nào -> bỏ (bảng thay thế sau)
                    if any(t.top <= row_top <= t.bottom for t in page_tables):
                        continue
                    text = re.sub(r"[ \t]+", " ", " ".join(w["text"] for w in row)).strip()
                    if text:
                        page_lines.append(Line(text=text, page=page_no, top=row_top))

                # Chèn bảng vào đúng vị trí theo toạ độ 'top'
                for t in page_tables:
                    page_lines.append(Line(
                        text=t.markdown, page=page_no, top=t.top, is_table=True
                    ))

                page_lines.sort(key=lambda l: l.top)
                lines.extend(page_lines)

        # Bảng không xác định được vị trí (page=-1) -> nối vào cuối cùng
        for t in self.tables:
            if t.page == -1:
                lines.append(Line(text=t.markdown, page=lines[-1].page if lines else 1,
                                  top=1e9, is_table=True))
        return lines

    def _group_tables_by_page(self) -> dict:
        by_page: dict[int, list[ExtractedTable]] = {}
        for t in self.tables:
            if t.page > 0:
                by_page.setdefault(t.page, []).append(t)
        return by_page

    def _group_into_rows(self, words: list[dict]) -> list[list[dict]]:
        """Nhóm các từ có cùng toạ độ 'top' (trong ngưỡng dung sai) thành 1 dòng."""
        words_sorted = sorted(words, key=lambda w: (round(w["top"] / self.y_tolerance), w["x0"]))
        rows: list[list[dict]] = []
        current_row: list[dict] = []
        current_top = None

        for w in words_sorted:
            if current_top is None or abs(w["top"] - current_top) <= self.y_tolerance:
                current_row.append(w)
                current_top = w["top"] if current_top is None else current_top
            else:
                rows.append(sorted(current_row, key=lambda x: x["x0"]))
                current_row = [w]
                current_top = w["top"]
        if current_row:
            rows.append(sorted(current_row, key=lambda x: x["x0"]))

        # rows hiện đang sắp theo cụm top đã round -> sort lại theo top thật để đúng thứ tự đọc
        rows.sort(key=lambda r: min(x["top"] for x in r))
        return rows




class HeaderFooterCleaner:
    ADMIN_FOOTER_MARKERS = [
        re.compile(r"^Nơi nhận\s*:", re.IGNORECASE),
        re.compile(r"^KT\.\s*(HIỆU TRƯỞNG|GIÁM ĐỐC|TRƯỞNG\b)", re.IGNORECASE),
        re.compile(r"^(HIỆU TRƯỞNG|GIÁM ĐỐC|TRƯỞNG PHÒNG|PHÓ HIỆU TRƯỞNG)\s*$"),
        re.compile(r"^(Đại diện|Người lập|Người ký)\b", re.IGNORECASE),
    ]

    
    LETTERHEAD_MARKERS = [
        re.compile(r"C\s*Ộ\s*N\s*G\s*H\s*[OÒ]\s*[ÀA]\s*X\s*Ã\s*H\s*Ộ\s*I", re.IGNORECASE),
        re.compile(r"Độc\s*lập\s*[-–—]\s*Tự\s*do\s*[-–—]\s*Hạnh\s*phúc", re.IGNORECASE),
        # "Số: 123/QĐ-ĐHBK" hoặc "Số: /QĐ-ĐHBK" (chưa điền số)
        re.compile(r"^Số\s*:\s*\S*\s*/\s*(TB|QĐ|CV|BC|KH|TTr|HD|NQ)\b", re.IGNORECASE),
        # Dòng địa danh + ngày tháng: "Thành phố Hồ Chí Minh, ngày tháng năm 2026"
        # (yêu cầu có dấu phẩy trước "ngày" để không xoá nhầm nội dung thật)
        re.compile(r",\s*ngày[\s\d]*tháng[\s\d]*năm\s*\d{4}", re.IGNORECASE),
    ]
    LETTERHEAD_SCAN_LINES = 8  # chỉ quét trong N dòng đầu của trang 1

   
    ORG_NAME_PREFIX = re.compile(
        r"^(TRƯỜNG|ĐẠI HỌC|HỌC VIỆN|VIỆN|SỞ|BỘ|UBND|ỦY BAN|UỶ BAN|HỘI ĐỒNG|TRUNG TÂM|PHÒNG|KHOA)\b"
    )

    def _is_org_name_line(self, text: str) -> bool:
        t = text.strip()
        return (bool(self.ORG_NAME_PREFIX.match(t))
                and len(t) <= 60
                and not any(ch.islower() for ch in t))

   
    DOC_DATE_PATTERN = re.compile(
        r"ngày\s*(\d{1,2})?\s*tháng\s*(\d{1,2})?\s*năm\s*(\d{4})", re.IGNORECASE
    )

    def __init__(self, repeat_threshold_ratio: float = 0.4):
       
        self.repeat_threshold_ratio = repeat_threshold_ratio
       
        self.doc_date: str | None = None
        self.doc_date_complete: bool = False

    def clean(self, lines: list[Line]) -> list[Line]:
        # Chuẩn hoá NFC toàn bộ text NGAY TỪ ĐẦU: mọi bước sau (kể cả regex
        # heading, admin footer) đều so khớp trên cùng một dạng Unicode.
        for l in lines:
            l.text = unicodedata.normalize("NFC", l.text)

        self.doc_date = None
        self.doc_date_complete = False

        lines = self._remove_letterhead(lines)
        lines = self._remove_running_header_footer(lines)
        lines = self._remove_standalone_page_numbers(lines)
        lines = self._remove_admin_footer(lines)
        return lines

    def _line_has_letterhead_marker(self, line: Line) -> bool:
        """
        Dòng thường: search trực tiếp. Dòng bảng (Markdown): tách từng ô rồi
        mới so khớp (marker neo ^ như 'Số:' nằm bên trong ô).
        """
        if not line.is_table:
            return any(p.search(line.text) for p in self.LETTERHEAD_MARKERS)

        for md_row in line.text.splitlines():
            for cell in md_row.split("|"):
                cell = cell.strip()
                if not cell or set(cell) <= {"-", ":", " "}:  # dòng kẻ |---|---|
                    continue
                if any(p.search(cell) for p in self.LETTERHEAD_MARKERS):
                    return True
        return False

    def _capture_doc_date(self, text: str) -> None:
        """Lưu ngày ban hành đầu tiên tìm thấy trong vùng letterhead."""
        if self.doc_date_complete:
            return
        m = self.DOC_DATE_PATTERN.search(text)
        if not m:
            return
        day, month, year = m.group(1), m.group(2), m.group(3)
        if day and month:
            self.doc_date = f"{int(day):02d}/{int(month):02d}/{year}"
            self.doc_date_complete = True
        elif self.doc_date is None:
            self.doc_date = year  # PDF chưa điền ngày/tháng -> chỉ giữ năm

    def _remove_letterhead(self, lines: list[Line]) -> list[Line]:
       
        cleaned: list[Line] = []
        scanned = 0
        for l in lines:
            if l.page == 1 and scanned < self.LETTERHEAD_SCAN_LINES:
                scanned += 1
                if self._line_has_letterhead_marker(l):
                    self._capture_doc_date(l.text)
                    continue  # bỏ dòng/bảng letterhead
                if not l.is_table and self._is_org_name_line(l.text):
                    continue  # bỏ dòng tên cơ quan đứng riêng
            cleaned.append(l)
        return cleaned

    def _line_has_admin_marker(self, line: Line) -> bool:
       
        if not line.is_table:
            return any(p.match(line.text) for p in self.ADMIN_FOOTER_MARKERS)

        for md_row in line.text.splitlines():
            for cell in md_row.split("|"):
                cell = cell.strip()
                if not cell or set(cell) <= {"-", ":", " "}:  # dòng kẻ |---|---|
                    continue
                if any(p.match(cell) for p in self.ADMIN_FOOTER_MARKERS):
                    return True
        return False

    def _remove_standalone_page_numbers(self, lines: list[Line]) -> list[Line]:
      
        page_number_pattern = re.compile(r"^(trang\s*)?[-–—]?\s*\d{1,4}\s*[-–—]?$", re.IGNORECASE)
        return [l for l in lines if l.is_table or not page_number_pattern.match(l.text.strip())]

    def _remove_running_header_footer(self, lines: list[Line]) -> list[Line]:
        pages = {l.page for l in lines}
        if len(pages) <= 1:
            return lines

        counts = Counter(l.text for l in lines if not l.is_table)
        threshold = max(2, int(len(pages) * self.repeat_threshold_ratio))
        repeated_texts = {text for text, c in counts.items() if c >= threshold}

        return [l for l in lines if l.is_table or l.text not in repeated_texts]

   
    RESUME_MARKERS = [
        re.compile(r"^PHỤ LỤC\s+\d+\b"),
        re.compile(r"^Chương\s+[IVXLCDM\d]+\b"),
        re.compile(r"^Điều\s+\d+[\.\:]"),
        re.compile(r"^Mục\s+\d+\b"),
    ]
    MAX_FOOTER_WINDOW = 12  # số dòng tối đa coi là "khối chữ ký" (nơi nhận, chức danh, tên)

    def _remove_admin_footer(self, lines: list[Line]) -> list[Line]:
       
        start = None
        for i, l in enumerate(lines):
            if self._line_has_admin_marker(l):
                start = i
                break
        if start is None:
            return lines

        end = min(start + self.MAX_FOOTER_WINDOW, len(lines))
        for j in range(start, end):
            if any(p.match(lines[j].text) for p in self.RESUME_MARKERS):
                end = j
                break

        return lines[:start] + lines[end:]



class DocumentStructureParser:
    # Thứ tự level cố định theo mức độ phổ biến trong văn bản hành chính VN.
    # Số nhỏ hơn = cấp cao hơn (cha). Có thể thêm/bớt tuỳ loại văn bản.
    PATTERNS = [
        ("phu_luc", re.compile(r"^PH[UỤ]\s*L[UỤ]C\s+\d+\b.*", re.IGNORECASE), 1),
        ("chuong", re.compile(r"^CH[UƯ][OƠ]NG\s+[IVXLCDM\d]+\b.*", re.IGNORECASE), 1),
        ("muc_lon", re.compile(r"^Mục\s+\d+\b.*"), 2),
        ("dieu", re.compile(r"^Điều\s+\d+[\.\:].*"), 2),
        ("roman", re.compile(r"^[IVXLCDM]{1,6}\.\s*.+"), 2),
        ("uppercase_letter_section", re.compile(r"^[A-Z]\.\s+\S.*"), 2),
        ("multi_level_num", re.compile(r"^\d{1,2}(?:\.\d{1,2}){1,3}\.?\s+.+"), 4),
        ("numbered_dot", re.compile(r"^\d{1,2}\.\s*.+"), 3),
        ("lettered", re.compile(r"^[a-zđ]\)\s*.+"), 5),
        ("markdown", re.compile(r"^#{1,6}\s*.+"), None),  # level = số dấu #
    ]

    def __init__(self, min_matches: int = 2):
        self.min_matches = min_matches

   
    LETTERED_HEADING_MAX_LEN = 60

    def _is_valid_heading(self, name: str, text: str) -> bool:
        if name != "lettered":
            return True
        t = text.strip()
        if t.endswith(":"):
            return True
        return (len(t) <= self.LETTERED_HEADING_MAX_LEN
                and not t.endswith((";", ",", ".")))

    def parse(self, lines: list[Line]) -> list[HeadingMatch]:
        """
        Trả về danh sách heading tìm được, ĐÃ được lọc để chỉ giữ những
        pattern nào thực sự xuất hiện đủ nhiều lần (tránh khớp nhầm).
        """
        candidates: dict[str, list[HeadingMatch]] = {}

        for i, line in enumerate(lines):
            if line.is_table:
                continue  # bảng không bao giờ là heading
            for name, pattern, level in self.PATTERNS:
                if pattern.match(line.text):
                    if not self._is_valid_heading(name, line.text):
                        break  # khớp pattern nhưng là câu nội dung -> không phải heading
                    resolved_level = level
                    if name == "markdown":
                        resolved_level = len(re.match(r"^#+", line.text).group())
                    candidates.setdefault(name, []).append(
                        HeadingMatch(i, line.text, resolved_level, name)
                    )
                    break  # 1 dòng chỉ khớp 1 pattern duy nhất (ưu tiên theo thứ tự PATTERNS)

        
        valid_matches = []
        for name, matches in candidates.items():
            if len(matches) >= self.min_matches:
                valid_matches.extend(matches)

        valid_matches.sort(key=lambda m: m.line_index)
        valid_matches = self._merge_wrapped_headings(valid_matches, lines)
        return valid_matches


    _BODY_START = re.compile(r"^\s*[-+•]")

    def _merge_wrapped_headings(self, matches: list[HeadingMatch], lines: list[Line]) -> list[HeadingMatch]:
        heading_indices = {m.line_index for m in matches}
        max_continuation = 2  # tối đa gộp thêm 2 dòng tiếp theo, tránh gộp lan quá xa

        for m in matches:
            j = m.line_index + 1
            steps = 0
            while (
                steps < max_continuation
                and j < len(lines)
                and j not in heading_indices
                and not self._BODY_START.match(lines[j].text)
                and lines[j].text[:1].islower()
            ):
                m.text = m.text.rstrip() + " " + lines[j].text.strip()
                m.consumed_indices.append(j)
                j += 1
                steps += 1

        return matches




class HierarchyBuilder:
    def build(self, lines: list[Line], headings: list[HeadingMatch]) -> list[Section]:
        if not headings:
            # Không có cấu trúc heading -> coi toàn bộ văn bản là 1 section gốc
            root = Section(
                heading="(Toàn văn bản)", level=0, pattern_name="none",
                page=lines[0].page if lines else 1,
                body_lines=[l.text for l in lines],
            )
            return [root]

        roots: list[Section] = []
        stack: list[Section] = []  # ngăn xếp các Section cha đang mở, theo level tăng dần

        heading_by_index = {h.line_index: h for h in headings}
        # Các dòng đã bị gộp vào tiêu đề (do tiêu đề bị xuống dòng) -> bỏ qua,
        # không đưa vào body_lines nữa để tránh lặp nội dung.
        consumed_indices = {idx for h in headings for idx in h.consumed_indices}

        current_section = None
        for i, line in enumerate(lines):
            if i in consumed_indices:
                continue
            if i in heading_by_index:
                h = heading_by_index[i]
                new_section = Section(
                    heading=h.text, level=h.level, pattern_name=h.pattern_name,
                    page=line.page,
                )

                # Đóng các section có level >= level hiện tại trên stack
                while stack and stack[-1].level >= h.level:
                    stack.pop()

                if stack:
                    new_section.parent_path = stack[-1].parent_path + [stack[-1].heading]
                    stack[-1].children.append(new_section)
                else:
                    roots.append(new_section)

                stack.append(new_section)
                current_section = new_section
            else:
                line_text = f"{TABLE_SENTINEL}{line.text}{TABLE_SENTINEL}" if line.is_table else line.text
                if current_section is not None:
                    current_section.body_lines.append(line_text)
                else:
                    # Nội dung trước heading đầu tiên (VD phần mở đầu) -> gom vào 1 section riêng
                    if not roots or roots[0].pattern_name != "preamble":
                        preamble = Section(
                            heading="(Mở đầu)", level=0, pattern_name="preamble",
                            page=line.page,
                        )
                        roots.insert(0, preamble)
                    roots[0].body_lines.append(line_text)

        return roots




class SemanticSplitter:
    def __init__(self, min_chars=150, max_chars=2000, overlap_chars=100):
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars

    def split(self, sections: list[Section]) -> list[dict]:
        """
        Trả về list các "leaf unit": {heading, path, page, text}
        đã qua gộp/cắt để nằm trong khoảng [min_chars, max_chars] hợp lý.
        """
        leaves = []
        self._collect_leaves(sections, leaves)
        leaves = self._merge_short(leaves)
        final = []
        for leaf in leaves:
            final.extend(self._split_long(leaf))
        final = self._merge_orphan_short(final)
        return final

    def _merge_orphan_short(self, leaves: list[dict]) -> list[dict]:
        
        if not leaves:
            return leaves
        out = []
        i = 0
        while i < len(leaves):
            cur = leaves[i]
            cur_len = len(cur["text"])
            if (
                cur_len < self.min_chars
                and i + 1 < len(leaves)
                and leaves[i + 1]["path"] == cur["path"]
                and cur_len + len(leaves[i + 1]["text"]) + 1 <= self.max_chars
            ):
                nxt = leaves[i + 1]
                merged = {**nxt, "text": (cur["text"] + "\n" + nxt["text"]).strip()}
                # giữ heading gốc (không phải "[phần tiếp theo]") nếu mảnh sau là phần tiếp
                merged["heading"] = cur["heading"]
                out.append(merged)
                i += 2
            else:
                out.append(cur)
                i += 1
        return out

    def _collect_leaves(self, sections: list[Section], out: list[dict]):
        for sec in sections:
            own_text = "\n".join([sec.heading] + sec.body_lines).strip() \
                if sec.pattern_name not in ("preamble", "none") \
                else "\n".join(sec.body_lines).strip()

            has_table = any(TABLE_SENTINEL in bl for bl in sec.body_lines)

            if sec.children:
                # Section có con: nội dung trực tiếp (nếu có, ngoài heading) vẫn
                # được giữ làm 1 leaf riêng trước khi đệ quy vào con.
                if sec.body_lines:
                    out.append({
                        "heading": sec.heading,
                        "path": sec.parent_path + [sec.heading],
                        "page": sec.page,
                        "text": own_text,
                        "has_table": has_table,
                    })
                self._collect_leaves(sec.children, out)
            else:
                out.append({
                    "heading": sec.heading,
                    "path": sec.parent_path + [sec.heading],
                    "page": sec.page,
                    "text": own_text,
                    "has_table": has_table,
                })

    def _merge_short(self, leaves: list[dict]) -> list[dict]:
        if not leaves:
            return leaves
        merged = [leaves[0]]
        for leaf in leaves[1:]:
            if len(leaf["text"]) < self.min_chars:
                merged[-1]["text"] += "\n" + leaf["text"]
            else:
                merged.append(leaf)
        return merged

    def _split_long(self, leaf: dict) -> list[dict]:
        text = leaf["text"]
        if len(text) <= self.max_chars and TABLE_SENTINEL not in text:
            return [leaf]

       
        raw_blocks = text.split(TABLE_SENTINEL)
        blocks = []  # list[(is_table, content)]
        for k, seg in enumerate(raw_blocks):
            seg = seg.strip()
            if not seg:
                continue
            # split() với 2 sentinel bao quanh bảng -> các đoạn index LẺ là bảng
            blocks.append((k % 2 == 1, seg))

        
        pieces, buffer = [], ""
        for is_table, content in blocks:
            if is_table:
                
                if buffer and len(buffer) + len(content) + 1 <= self.max_chars:
                    pieces.append((buffer + "\n" + content).strip())
                    buffer = ""
                else:
                    if buffer:
                        pieces.append(buffer)
                        buffer = ""
                    pieces.append(content)  # bảng đứng riêng 1 piece
                continue
            # text thường: có thể tách nhỏ theo câu nếu quá dài
            for sub in self._split_prose(content):
                candidate = (buffer + "\n" + sub).strip() if buffer else sub
                if len(candidate) <= self.max_chars:
                    buffer = candidate
                else:
                    if buffer:
                        pieces.append(buffer)
                    buffer = sub
        if buffer:
            pieces.append(buffer)

        out = []
        for i, piece in enumerate(pieces):
            suffix = "" if i == 0 else "  [phần tiếp theo]"
            out.append({**leaf, "text": piece, "heading": leaf["heading"] + suffix})
        return out

    def _split_prose(self, text: str) -> list[str]:
        """Cắt 1 đoạn văn thường quá dài theo ranh giới câu / dấu ; / xuống dòng."""
        if len(text) <= self.max_chars:
            return [text]
        units = re.split(r"(?<=[.!?…;])\s+|\n", text)
        units = [u.strip() for u in units if u.strip()]
        pieces, buffer = [], ""
        for u in units:
            candidate = (buffer + " " + u).strip() if buffer else u
            if len(candidate) <= self.max_chars:
                buffer = candidate
            else:
                if buffer:
                    pieces.append(buffer)
                buffer = u
        if buffer:
            pieces.append(buffer)
        return pieces



class MetadataBuilder:
    def build(self, leaves: list[dict], source_file: str, doc_metadata: dict) -> list[Chunk]:
        chunks = []
        for idx, leaf in enumerate(leaves, start=1):
            breadcrumb = " > ".join(leaf["path"]) if leaf["path"] else leaf["heading"]
            raw_text = leaf["text"]
            has_table = TABLE_SENTINEL in raw_text or leaf.get("has_table", False)
            clean = raw_text.replace(TABLE_SENTINEL, "").strip()  # gỡ sentinel khỏi output
            metadata = {
                **doc_metadata,
                "source_file": source_file,
                "section_path": breadcrumb,
                "heading": leaf["heading"].replace(TABLE_SENTINEL, ""),
                "approx_page": leaf["page"],
                "chunk_index": idx,
                "char_count": len(clean),
                "has_table": has_table,
            }
            chunks.append(Chunk(
                chunk_id=f"{Path(source_file).stem}_chunk_{idx}",
                text=clean,
                metadata=metadata,
            ))
        return chunks




class ChunkingPipeline:
    def __init__(self, use_docling_tables: bool = True):
        # use_docling_tables=True: nếu máy có cài Docling -> trích bảng bằng Docling.
        #                          nếu chưa cài -> tự động bỏ qua (chạy như cũ).
        self.hf_cleaner = HeaderFooterCleaner()
        self.structure_parser = DocumentStructureParser()
        self.hierarchy_builder = HierarchyBuilder()
        self.semantic_splitter = SemanticSplitter()
        self.metadata_builder = MetadataBuilder()

        self.table_extractor = DoclingTableExtractor() if use_docling_tables else None

    def run(self, pdf_path: str, doc_metadata: dict | None = None) -> list[Chunk]:
        doc_metadata = doc_metadata or {}

        # BƯỚC 0 (tuỳ chọn): trích bảng bằng Docling
        tables = []
        if self.table_extractor and self.table_extractor.available:
            tables = self.table_extractor.extract(pdf_path)

        layout_parser = LayoutParser(tables=tables)                      # PDF (+bảng Docling)
        lines = layout_parser.parse(pdf_path)                            # -> Layout Parser
        lines = self.hf_cleaner.clean(lines)                             # -> Header/Footer Cleaner

       
        if "doc_date" not in doc_metadata:
            pdf_date = self._extract_pdf_metadata_date(pdf_path)
            if self.hf_cleaner.doc_date_complete:
                doc_metadata["doc_date"] = self.hf_cleaner.doc_date
                doc_metadata["doc_date_source"] = "letterhead"
            elif pdf_date:
                doc_metadata["doc_date"] = pdf_date
                doc_metadata["doc_date_source"] = "pdf_metadata"
                if self.hf_cleaner.doc_date:  # vẫn giữ năm ghi trên văn bản
                    doc_metadata["doc_year"] = self.hf_cleaner.doc_date
            elif self.hf_cleaner.doc_date:
                doc_metadata["doc_date"] = self.hf_cleaner.doc_date
                doc_metadata["doc_date_source"] = "letterhead_year_only"

        headings = self.structure_parser.parse(lines)                   # -> Document Structure Parser
        sections = self.hierarchy_builder.build(lines, headings)         # -> Hierarchy Builder
        leaves = self.semantic_splitter.split(sections)                  # -> Semantic Splitter
        chunks = self.metadata_builder.build(                            # -> Metadata Builder
            leaves, Path(pdf_path).name, doc_metadata
        )
        return chunks                                                    # -> Chunk

    @staticmethod
    def _extract_pdf_metadata_date(pdf_path: str) -> str | None:
        """
        Đọc CreationDate/ModDate trong metadata PDF (định dạng 'D:YYYYMMDD...')
        -> trả về 'dd/mm/yyyy'. Với văn bản ký số, đây thường là ngày ký.
        """
        try:
            with pdfplumber.open(pdf_path) as pdf:
                meta = pdf.metadata or {}
            raw = meta.get("CreationDate") or meta.get("ModDate") or ""
            m = re.search(r"D:(\d{4})(\d{2})(\d{2})", raw)
            if m:
                return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
        except Exception:
            pass
        return None




def process_path(input_path: str, output_dir: str = r"C:\Users\ADMIN\Desktop\agent_A\data\KnowledgeGraph\output\chunks\quydinh"):
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(input_path.glob("*.pdf")) if input_path.is_dir() else [input_path]
    if not pdf_files:
        print(f"Không tìm thấy PDF trong: {input_path}")
        return

    pipeline = ChunkingPipeline()

    docling_on = pipeline.table_extractor and pipeline.table_extractor.available
    print(f"[Docling table extraction: {'BẬT' if docling_on else 'TẮT (chưa cài docling -> chạy fallback pdfplumber)'}]")
    for pdf_file in pdf_files:
        print(f"\n=== {pdf_file.name} ===")
        try:
            chunks = pipeline.run(str(pdf_file))
        except Exception as e:
            print(f"  Lỗi: {e}")
            continue

        out_data = [{"chunk_id": c.chunk_id, "text": c.text, "metadata": c.metadata} for c in chunks]
        out_file = output_dir / f"{pdf_file.stem}_chunks.json"
        out_file.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"  Số chunk: {len(chunks)}  ->  {out_file}")
        for c in chunks[:5]:
            preview = c.text.replace("\n", " ")[:70]
            print(f"    - [{c.metadata['section_path']}] {preview}...")
        if len(chunks) > 5:
            print(f"    ... và {len(chunks) - 5} chunk khác")
if __name__ == "__main__":
    input_path = r"C:\Users\ADMIN\Desktop\agent_A\data\output\quydinh"
    process_path(input_path, output_dir=r"C:\Users\ADMIN\Desktop\agent_A\data\KnowledgeGraph\output\chunks\quydinh")