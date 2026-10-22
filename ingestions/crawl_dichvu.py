import argparse
import getpass
import json
import os
import re
import sys
import time
import glob
from datetime import datetime

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException

load_dotenv()

# ---- URL: MyBK dùng chung SSO với crawl_bang_diem. LMS thường là hệ CAS riêng ----
SSO_MYBK = ("https://sso.hcmut.edu.vn/cas/login?service="
            "https%3A%2F%2Fmybk.hcmut.edu.vn%2Fapp%2Flogin%2Fcas")
SSO_LMS =  ("https://sso.hcmut.edu.vn/cas/login?service=" 
            "https%3A%2F%2Flms.hcmut.edu.vn%2Flogin%2Findex.php%3FauthCAS%3DCAS")
TKB_URL = os.getenv("TKB_URL", "https://mybk.hcmut.edu.vn/app/he-thong-quan-ly/sinh-vien/tkb")
CTXK_URL = os.getenv("CTXK_URL", "https://mybk.hcmut.edu.vn/app/he-thong-quan-ly/sinh-vien/thong-tin")
LICH_THI_URL = os.getenv("LICH_THI_URL", "https://mybk.hcmut.edu.vn/app/he-thong-quan-ly/sinh-vien/lich-thi")
MYBK_TAILIEU_URL = os.getenv("MYBK_TAILIEU_URL", "https://lms.hcmut.edu.vn/my/courses.php")

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.abspath("tai_lieu"))
OUT_LICH = os.getenv("OUT_LICH", "lich_hoc_thi.json")
OUT_TAILIEU = os.getenv("OUT_TAILIEU", "tai_lieu_lms.json")


# ---- Parse ngày deadline của Moodle -> ISO (để lich_agent lọc/sắp theo hạn) ----
# Moodle của trường hiển thị nhãn tiếng Anh (Opened/Due/Closes) nên ngày cũng theo
# định dạng tiếng Anh, vd 'Tuesday, 12 March 2024, 11:59 PM'. Ta TÁCH ra datetime rồi
# lưu dạng ISO 8601 vào 'opened_ts'/'due_ts'. KHÔNG lưu 'trang_thai' (đang mở/đã đóng)
# vì trạng thái phụ thuộc thời điểm HỎI, không phải thời điểm crawl — để lich_agent tự
# tính LIVE từ *_ts so với hôm nay, tránh dữ liệu bị "cũ" giữa các lần crawl.
_THANG = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_moodle_dt(s: str):
    """'Tuesday, 12 March 2024, 11:59 PM' -> datetime. Không parse được -> None."""
    if not s:
        return None
    s = str(s)
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if not m:
        return None
    thang = _THANG.get(m.group(2).lower())
    if not thang:
        return None
    day, year = int(m.group(1)), int(m.group(3))
    gio, phut = 0, 0
    mt = re.search(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])?", s)
    if mt:
        gio, phut = int(mt.group(1)), int(mt.group(2))
        ap = (mt.group(3) or "").upper()
        if ap == "PM" and gio != 12:
            gio += 12
        elif ap == "AM" and gio == 12:
            gio = 0
    try:
        return datetime(year, thang, day, gio, phut)
    except ValueError:
        return None


def _iso(s: str) -> str:
    """Chuỗi ngày Moodle -> ISO 8601 ('2024-03-12T23:59:00'); parse hỏng -> ''."""
    dt = _parse_moodle_dt(s)
    return dt.isoformat() if dt else ""


class HcmutBrowser:
    """Trình duyệt Selenium + login SSO."""

    def __init__(self, headless: bool = False, download_dir: str = DOWNLOAD_DIR):
        self.headless = headless
        self.download_dir = download_dir
        self.driver = None

    def setup(self):
        os.makedirs(self.download_dir, exist_ok=True)
        opt = webdriver.ChromeOptions()
        opt.add_argument("--disable-blink-features=AutomationControlled")
        opt.add_experimental_option("excludeSwitches", ["enable-automation"])
        opt.add_experimental_option("useAutomationExtension", False)
        opt.add_experimental_option("prefs", {
            "download.default_directory": self.download_dir,
            "download.prompt_for_download": False,
            "plugins.always_open_pdf_externally": True,
        })
        if self.headless:
            opt.add_argument("--headless=new")
        self.driver = webdriver.Chrome(options=opt)
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    def cleanup(self):
        if self.driver:
            self.driver.quit()
            self.driver = None

    def login_sso(self, username: str, password: str, service_url: str):
        self.driver.get(service_url)
        current_url = self.driver.current_url
        
        WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.NAME, "username"))
        ).send_keys(username)
        self.driver.find_element(By.NAME, "password").send_keys(password)
        self.driver.find_element(By.NAME, "submit").click()
        
        try:
            WebDriverWait(self.driver, 45).until(EC.url_changes(current_url))
            print("  -> Xác thực thành công, đang chuyển hướng...")
            time.sleep(3) 
        except TimeoutException:
            print("  -> [!] Chuyển hướng SSO quá chậm, có thể mạng đang lag.")


# ============================================================================
#  PHẦN 1 — LỊCH HỌC / LỊCH THI
# ============================================================================

def _doc_bang_theo_ten_cot(d, table_id: str) -> list:
    try:
        tbl = d.find_element(By.ID, table_id)
    except NoSuchElementException:
        return []
    ths = tbl.find_elements(By.XPATH, ".//thead//th")
    ten_cot = [t.text.strip() for t in ths]
    out = []
    for r in tbl.find_elements(By.XPATH, ".//tbody/tr"):
        cols = r.find_elements(By.TAG_NAME, "td")
        if len(cols) < 3: continue
        rec = {}
        for i, c in enumerate(cols):
            key = ten_cot[i] if i < len(ten_cot) and ten_cot[i] else f"cot_{i}"
            rec[key] = c.text.strip()
        if any(rec.values()): out.append(rec)
    return out

def _parse_tuan_hoc(raw: str) -> list:
    tuan = []
    for phan in str(raw).replace(" ", "").split("|"):
        if phan and phan != "--" and phan.isdigit(): tuan.append(int(phan))
    return tuan

def _crawl_lich_core(br: HcmutBrowser) -> dict:
    d = br.driver
    d.get(TKB_URL)
    time.sleep(8)                            
    try:
        WebDriverWait(d, 15).until(EC.presence_of_element_located((By.ID, "tblStudentSchedule")))
    except TimeoutException:
        print("[tkb] không thấy bảng tblStudentSchedule.")
        return {"tuan": "", "ngay": "", "hoc_ky": "", "lich_hoc": [], "lich_thi": [], "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}

    try:
        from selenium.webdriver.support.ui import Select
        Select(d.find_element(By.CSS_SELECTOR, "select[name='tblStudentSchedule_length']")).select_by_value("50")
        time.sleep(2)          
    except Exception:
        pass

    tuan = ngay = hoc_ky_hien_tai = ""
    try:
        body = d.find_element(By.TAG_NAME, "body").text
        mt = re.search(r"Tuần\s*:?\s*(\d+)", body)
        mn = re.search(r"Ngày\s*(\d{1,2}/\d{1,2}/\d{4})", body)
        tuan = mt.group(1) if mt else ""
        ngay = mn.group(1) if mn else ""
        mh = d.find_element(By.ID, "select2-semesterYear-container")
        hoc_ky_hien_tai = mh.text.strip()
    except Exception:
        pass

    rows = d.find_element(By.ID, "tblStudentSchedule").find_elements(By.XPATH, ".//tbody/tr")
    lich_hoc = []
    for r in rows:
        cols = r.find_elements(By.TAG_NAME, "td")
        if len(cols) < 8: continue
        ma = cols[1].text.strip()
        if not ma: continue
        def _c(i): return cols[i].text.strip() if len(cols) > i else ""
        th_raw = _c(11)
        lich_hoc.append({
            "hoc_ky": _c(0), "ma_mon": ma, "ten_mon": _c(2), "tin_chi": _c(3),
            "nhom_to": _c(5), "thu": _c(6), "tiet": _c(7), "gio_hoc": _c(8),
            "phong": _c(9), "co_so": _c(10), "tuan_hoc": th_raw, "cac_tuan": _parse_tuan_hoc(th_raw),     
        })

    lich_thi = []
    if LICH_THI_URL:
        try:
            d.get(LICH_THI_URL)
            time.sleep(8)
            try:
                from selenium.webdriver.support.ui import Select
                Select(d.find_element(By.CSS_SELECTOR, "select[name$='_length']")).select_by_value("50")
                time.sleep(1)
            except Exception: pass
            
            for tid in ("tblStudentExamSchedule", "tblExamSchedule", "tblStudentExam"):
                lich_thi = _doc_bang_theo_ten_cot(d, tid)
                if lich_thi: break
        except Exception:
            pass

    return {"tuan": tuan, "ngay": ngay, "hoc_ky": hoc_ky_hien_tai, "lich_hoc": lich_hoc, "lich_thi": lich_thi, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}


# ============================================================================
#  PHẦN 1b — CTXH (công tác xã hội) — tab "Hoạt động ngoại khóa" của MyBK
# ============================================================================
# Trang thông tin sinh viên là dạng NHIỀU TAB trong CÙNG một URL (không đổi URL khi
# chuyển tab), nên phải CLICK vào tab "Hoạt động ngoại khóa" rồi mới đọc được DOM.
# Số ngày nằm ở <span class="num-of-social-work-day">15</span>.
TEN_TAB_CTXH = "Hoạt động ngoại khóa"
CSS_SO_NGAY_CTXH = "span.num-of-social-work-day"
def _mo_tab_ctxh(d) -> bool:
    """Mở tab Hoạt động ngoại khóa."""
    try:
        el = WebDriverWait(d, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#tabActivities > a")
            )
        )

        d.execute_script(
            "arguments[0].scrollIntoView({block:'center'});"
            "arguments[0].click();",
            el
        )

        return True

    except Exception as e:
        print(f"[ctxh] Không mở được tab: {type(e).__name__}")
        return False


def crawl_ctxh(br: HcmutBrowser) -> dict:
    """Lấy TỔNG SỐ NGÀY CTXH + danh sách hoạt động ngoại khóa (tab của trang thông tin SV).

    Trả: {'tong_so_ngay': int|None, 'sinh_vien': str, 'hoat_dong': [...], 'timestamp': str}
    tong_so_ngay=None nghĩa là KHÔNG đọc được (đừng hiểu nhầm thành 0 ngày).
    Cần đã login SSO_MYBK trước đó (dùng chung phiên với crawl_lich).
    """
    d = br.driver
    kq = {"tong_so_ngay": None, "sinh_vien": "",
          "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not CTXK_URL:
        return kq

    d.get(CTXK_URL)
    time.sleep(3)                              # trang MyBK render chậm, chờ khung tab
    if not _mo_tab_ctxh(d):
        print(f"[ctxh] không tìm thấy tab '{TEN_TAB_CTXH}'.")
    time.sleep(3)                              # chờ nội dung tab đổ ra

    # --- tổng số ngày ---
    try:
        el = WebDriverWait(d, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, CSS_SO_NGAY_CTXH)))
        txt = (el.text or el.get_attribute("textContent") or "").strip()
        m = re.search(r"\d+", txt)
        if m:
            kq["tong_so_ngay"] = int(m.group())
    except TimeoutException:
        # Dự phòng: đọc thẳng text trang (phòng khi class đổi tên).
        try:
            m = re.search(r"Tổng số ngày CTXH\s*:?\s*(\d+)",
                          d.find_element(By.TAG_NAME, "body").text)
            if m:
                kq["tong_so_ngay"] = int(m.group(1))
        except Exception:
            pass
    if kq["tong_so_ngay"] is None:
        print("[ctxh] KHÔNG đọc được tổng số ngày CTXH.")

    # --- tên sinh viên (để đối chiếu đúng người) ---
    try:
        kq["sinh_vien"] = d.find_element(By.CSS_SELECTOR, "span.student-name").text.strip()
    except NoSuchElementException:
        pass



    print(f"[ctxh] tổng số ngày: {kq['tong_so_ngay']}")
    return kq


def _mo_tab_hocphi(d) -> bool:
    """Mở tab Học phí."""
    try:
        el = WebDriverWait(d, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#tabTuition > a")
            )
        )

        d.execute_script(
            "arguments[0].scrollIntoView({block:'center'});"
            "arguments[0].click();",
            el
        )

        return True

    except Exception as e:
        print(f"hocphi Không mở được tab: {type(e).__name__}")
        return False


def _tien(s: str):
    """'250,000' -> 250000. Không đọc được -> None."""
    so = re.sub(r"[^\d]", "", str(s or ""))
    return int(so) if so else None


def crawl_hoc_phi(br: HcmutBrowser) -> list:
    """Các khoản học phí CHƯA ĐÓNG (số tiền phải nộp > 0) ở tab 'Học phí - BKPay2'.

    Trả list dict: {hoc_ky, noi_dung, so_tien_phai_nop, thoi_han_nop, trang_thai}.
    Cần đã login SSO_MYBK trước đó (dùng chung phiên với crawl_lich).
    """
    d = br.driver
    if not CTXK_URL:
        return []

    d.get(CTXK_URL)
    time.sleep(3)
    if not _mo_tab_hocphi(d):
        return []
    time.sleep(3)

    # Bảng mặc định 10 dòng/trang -> khoản chưa đóng có thể nằm ở trang sau.
    try:
        from selenium.webdriver.support.ui import Select
        Select(d.find_element(By.CSS_SELECTOR, "select[name='tblTuition_length']")).select_by_value("50")
        time.sleep(2)
    except Exception:
        print("[hoc_phi] không đổi được 50 dòng/trang -> đọc theo mặc định.")

    try:
        tbl = WebDriverWait(d, 20).until(
            EC.presence_of_element_located((By.ID, "tblTuition")))
    except TimeoutException:
        print("[hoc_phi] không thấy bảng tblTuition.")
        return []

    # Lấy index cột theo TÊN cột (thứ tự cột có thể đổi giữa các bản MyBK).
    ten_cot = [t.text.strip().lower() for t in tbl.find_elements(By.XPATH, ".//thead//th")]

    def _idx(*tu_khoa):
        for i, t in enumerate(ten_cot):
            if all(k in t for k in tu_khoa):
                return i
        return None

    i_hk, i_nd = _idx("học kỳ"), _idx("nội dung")
    i_phai, i_han, i_tt = _idx("phải nộp"), _idx("thời hạn"), _idx("trạng thái")
    if i_phai is None:
        print("[hoc_phi] không tìm thấy cột 'Số tiền phải nộp'.")
        return []

    can_dong = []
    for r in tbl.find_elements(By.XPATH, ".//tbody/tr"):
        cols = r.find_elements(By.TAG_NAME, "td")
        if len(cols) <= i_phai:
            continue

        def _c(i):
            return cols[i].text.strip() if (i is not None and i < len(cols)) else ""

        tien = _tien(_c(i_phai))
        if not tien:                       # 0 hoặc rỗng -> đã đóng xong, bỏ
            continue
        can_dong.append({
            "hoc_ky": _c(i_hk),
            "noi_dung": _c(i_nd),
            "so_tien_phai_nop": tien,
            "thoi_han_nop": _c(i_han),
            "trang_thai": _c(i_tt),
        })

    print(f"[hoc_phi] {len(can_dong)} khoản cần thanh toán")
    return can_dong


def crawl_lich(br: HcmutBrowser) -> dict:
    """Lịch học/thi + CTXH trong MỘT lần chạy (cùng phiên MyBK) -> ghi chung lich_hoc_thi.json.

    Gộp ở đây để mọi nơi đang gọi crawl_lich (terminal main(), main.py `_sync_lich`) tự có
    thêm CTXH mà không phải sửa. CTXH lỗi KHÔNG làm hỏng phần lịch."""
    data = _crawl_lich_core(br)
    try:
        data["ctxh"] = crawl_ctxh(br)
    except Exception as e:
        print(f"[ctxh] LỖI: {type(e).__name__}: {e}")
        data["ctxh"] = {"tong_so_ngay": None, "sinh_vien": "", "hoat_dong": [],
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        data["hoc_phi_can_dong"] = crawl_hoc_phi(br)
    except Exception as e:
        print(f"[hoc_phi] LỖI: {type(e).__name__}: {e}")
        data["hoc_phi_can_dong"] = []
    return data


# ============================================================================
#  PHẦN 2 — TẢI TÀI LIỆU & DEADLINE (LMS)
# ============================================================================
def liet_ke_tai_lieu(br: HcmutBrowser) -> list:
    """Liệt kê tài liệu các môn của TẤT CẢ các học kỳ hiển thị. Chống lỗi Stale DOM."""
    d = br.driver
    if not MYBK_TAILIEU_URL: return []
    d.get(MYBK_TAILIEU_URL)
    items = []
    
    try:
        WebDriverWait(d, 60).until(EC.presence_of_element_located((By.CSS_SELECTOR, "ul.list-group")))
        print("[lms] Đang chờ giao diện LMS ổn định...")
        time.sleep(5) 
        
        lis = d.find_elements(By.CSS_SELECTOR, "ul.list-group li.course-listitem")
        so_luong_mon = len(lis)
        current_hk = "Không xác định"
        
        for i in range(so_luong_mon):
            try:
                li = d.find_elements(By.CSS_SELECTOR, "ul.list-group li.course-listitem")[i]
                h4 = li.find_elements(By.TAG_NAME, "h4")
                if h4 and "Học kỳ" in h4[0].text:
                    current_hk = h4[0].text.strip()
                    print(f"[lms] Đang quét dữ liệu cho: {current_hk}")
                else:
                    course_id = li.get_attribute("data-course-id")
                    if course_id:
                        ten_mon = li.text.split('\n')[0].strip()
                        course_url = f"https://lms.hcmut.edu.vn/course/view.php?id={course_id}"
                        items.append({"mon": ten_mon, "hoc_ky": current_hk, "url": course_url, "course_id": course_id})
            except StaleElementReferenceException:
                print(f"[lms] [!] Bỏ qua 1 phần tử do giao diện LMS bị giật/tải lại giữa chừng.")
                continue
    except TimeoutException:
        print("[lms] Quá thời gian chờ: Không tìm thấy danh sách khóa học.")
    return items


def crawl_tai_lieu_mon_hoc(br: HcmutBrowser, course_url: str, ten_mon: str) -> list:
    d = br.driver
    d.get(course_url)
    try:
        ul_topics = WebDriverWait(d, 60).until(EC.presence_of_element_located((By.CSS_SELECTOR, "ul.topics")))
    except TimeoutException:
        print(f"[lms] Không load được nội dung môn: {ten_mon}")
        return []

    file_links = []
    sections = ul_topics.find_elements(By.CSS_SELECTOR, "li.course-section")
    for sec in sections:
        
        # 1. TRÍCH XUẤT TÊN SECTION (Thay vì chỉ lấy aria-label)
        try:
            sec_title_el = sec.find_element(By.CSS_SELECTOR, ".sectionname, .course-section-header h3, .course-section-header h4")
            sec_name = sec_title_el.text.strip()
        except NoSuchElementException:
            sec_name = sec.get_attribute("aria-label") or sec.get_attribute("id") or "Không rõ Section"

        for act in sec.find_elements(By.CSS_SELECTOR, "li.activity"):
            cls = act.get_attribute("class") or ""
            
            # 2. XỬ LÝ NHÃN (LABEL) - Không có thẻ <a>, cần bóc luôn text hiển thị
            if "modtype_label" in cls:
                try:
                    noi_dung = act.text.strip()
                    if noi_dung:
                        file_links.append({
                            "mon": ten_mon, "section": sec_name,
                            "ten_file": "[Nhãn/Ghi chú]", "url": "", 
                            "mo_ta": noi_dung, "loai": "label"
                        })
                except Exception:
                    pass
                continue
            
            # Xử lý các dạng bài có đính kèm Link tải (File, Folder, Assign, Quiz...)
            try:
                item_div = act.find_element(By.CSS_SELECTOR, "div.activity-item")
                ten_file = item_div.get_attribute("data-activityname") or item_div.text.split("\n")[0].strip()
                href = act.find_element(By.CSS_SELECTOR, "a").get_attribute("href")
            except NoSuchElementException:
                continue
            
            if not href: continue

            # Phân loại mở rộng
            if "modtype_resource" in cls: loai = "file"
            elif "modtype_folder" in cls: loai = "folder"
            elif "modtype_url" in cls or "modtype_data" in cls or "modtype_lti" in cls: loai = "link"          
            elif "modtype_assign" in cls: loai = "assign"
            elif "modtype_quiz" in cls: loai = "quiz" # <--- THÊM NHẬN DIỆN QUIZ
            else: continue

            file_links.append({"mon": ten_mon, "section": sec_name, "ten_file": ten_file, "url": href, "loai": loai})
            
    return file_links


def _lay_thong_tin_deadline(br: HcmutBrowser, url: str) -> dict:
    """Truy cập trang nộp bài tập (assign) HOẶC quiz để lấy Opened, Due/Closes và Description."""
    d = br.driver
    d.get(url)
    info = {"opened": "", "due": "", "mo_ta": ""}
    
    try:
        WebDriverWait(d, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[role='main']")))
        
        # 1. Lấy thông tin ngày tháng (Opened, Due, Closes)
        try:
            # Quét cả .activity-dates (Bài tập) và .quizinfo (Bài trắc nghiệm)
            dates_elements = d.find_elements(By.CSS_SELECTOR, "div.activity-dates, div.quizinfo")
            for el in dates_elements:
                # Tách thành từng dòng để kiểm tra
                for line in el.text.split('\n'):
                    text = line.strip()
                    if "Opened:" in text: info["opened"] = text.replace("Opened:", "").strip()
                    elif "Due:" in text: info["due"] = text.replace("Due:", "").strip()
                    elif "Closes:" in text: info["due"] = text.replace("Closes:", "").strip()
        except NoSuchElementException:
            pass
            
        # 2. Lấy thông tin mô tả bài tập / quiz
        try:
            desc_div = d.find_element(By.CSS_SELECTOR, "div.activity-description, div.box.generalbox")
            info["mo_ta"] = desc_div.text.strip()
        except NoSuchElementException:
            pass
            
    except TimeoutException:
        print(f"     [!] Quá thời gian chờ lấy thông tin deadline: {url}")
        
    return info


def _mo_rong_folder(br: HcmutBrowser, folder_url: str, mon: str, section: str) -> list:
    d = br.driver
    d.get(folder_url)
    files = []
    try:
        WebDriverWait(d, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[role='main']")))
        for a in d.find_elements(By.CSS_SELECTOR, "a[href*='pluginfile.php']"):
            href = a.get_attribute("href")
            if href:
                files.append({"mon": mon, "section": section, "ten_file": a.text.strip() or href.split("/")[-1].split("?")[0], "url": href, "loai": "file"})
    except TimeoutException:
        pass
    return files


def tai_tai_lieu(br: HcmutBrowser, items: list) -> int:
    files = [it for it in items if it.get("loai") == "file"]
    folders = [it for it in items if it.get("loai") == "folder"]
    links = [it for it in items if it.get("loai") == "link"]

    for f in folders:
        con = _mo_rong_folder(br, f["url"], f.get("mon", ""), f.get("section", ""))
        print(f"[lms] Folder '{f['ten_file']}': +{len(con)} file")
        files.extend(con)

    if links:
        print(f"[lms] Bỏ qua {len(links)} link ngoài/label.")

    seen, uniq = set(), []
    for f in files:
        if f["url"] not in seen:
            seen.add(f["url"])
            uniq.append(f)

    d = br.driver
    n = 0
    for f in uniq:
        truoc = set(glob.glob(os.path.join(br.download_dir, "*")))
        d.get(f["url"])
        for _ in range(30):
            time.sleep(1)
            hien = set(glob.glob(os.path.join(br.download_dir, "*")))
            dang_tai = glob.glob(os.path.join(br.download_dir, "*.crdownload"))
            if hien - truoc and not dang_tai: break
        n += 1
        print(f"  [{n}/{len(uniq)}] {f['ten_file']}")
    
    if n: print(f"[lms] Đã tải {n} file vào '{br.download_dir}'.")
    return n


# ============================================================================
#  MENU / MAIN
# ============================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="Agent LMS: lịch học/thi + tải tài liệu LMS")
    ap.add_argument("-u", "--user", default=os.getenv("MYBK_USER"))
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--che-do", choices=["lich", "tai-lieu"], default=None)
    args = ap.parse_args()

    username = args.user or input("Tài khoản: ").strip()
    password = os.getenv("MYBK_PASS") or getpass.getpass("Mật khẩu: ")
    if not username or not password:
        print("Thiếu tài khoản/mật khẩu.")
        return 1

    che_do = args.che_do
    if not che_do:
        print("Chọn chức năng:\n  1. Lấy lịch học / lịch thi (LMS)\n  2. Tải tài liệu giáo viên (LMS)")
        che_do = {"1": "lich", "2": "tai-lieu"}.get(input("> ").strip())
        if not che_do:
            print("Lựa chọn không hợp lệ.")
            return 1

    br = HcmutBrowser(headless=args.headless)
    try:
        br.setup()
        if che_do == "lich":
            print("[1/2] Đăng nhập MyBK...")
            br.login_sso(username, password, SSO_MYBK) 
            print("[2/2] Lấy lịch...")
            data = crawl_lich(br)
            if data["lich_hoc"] or data["lich_thi"] or (data.get("ctxh") or {}).get("tong_so_ngay") is not None:
                json.dump(data, open(OUT_LICH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                print(f"Đã lưu {OUT_LICH}: {data.get('hoc_ky','')} | Tuần {data.get('tuan','?')} | {len(data['lich_hoc'])} buổi học, {len(data['lich_thi'])} lịch thi, CTXH {(data.get('ctxh') or {}).get('tong_so_ngay')} ngày.")
            else:
                print("Không lấy được lịch.")
        else:
            print("[1/3] Đăng nhập LMS...")
            br.login_sso(username, password, SSO_LMS) 
            
            print("[2/3] Quét danh sách môn học...")
            courses = liet_ke_tai_lieu(br)
            
            all_files = []
            if courses:
                print(f"[+] Tìm thấy {len(courses)} môn học. Bắt đầu quét chi tiết...")
                for c in courses:
                    print(f"  -> Đang quét [{c.get('hoc_ky', '')}]: {c['mon']}...")
                    all_files.extend(crawl_tai_lieu_mon_hoc(br, c['url'], c['mon']))
            
            if all_files:
                # 3. Trích xuất chi tiết Deadline cho CẢ BÀI TẬP (assign) VÀ KIỂM TRA (quiz)
                deadlines = [f for f in all_files if f.get("loai") in ["assign", "quiz"]]
                if deadlines:
                    print(f"\n[+] Đang trích xuất thời hạn cho {len(deadlines)} bài tập/quiz...")
                    for i, a in enumerate(deadlines, 1):
                        print(f"  -> Đọc deadline ({i}/{len(deadlines)}): {a['ten_file']}")
                        chi_tiet = _lay_thong_deadline(br, a["url"])
                        a["opened"] = chi_tiet.get("opened", "")
                        a["due"] = chi_tiet.get("due", "")
                        a["mo_ta"] = chi_tiet.get("mo_ta", "")
                        # ISO để lich_agent lọc/sắp theo hạn (trang_thai tính live bên đó)
                        a["opened_ts"] = _iso(a["opened"])
                        a["due_ts"] = _iso(a["due"])

                from collections import Counter
                loai_dem = Counter(f.get("loai", "?") for f in all_files)
                print(f"\n--- TÌM THẤY {len(all_files)} MỤC "
                      f"(file: {loai_dem.get('file',0)}, folder: {loai_dem.get('folder',0)}, "
                      f"link: {loai_dem.get('link',0)}, deadline/quiz: {loai_dem.get('assign',0) + loai_dem.get('quiz',0)}, "
                      f"nhãn: {loai_dem.get('label',0)}) ---")
                
                # Cập nhật format in để hiển thị rõ tên Section
                for i, f in enumerate(all_files, 1):
                    tag = {"file": "📄", "folder": "📁", "link": "🔗", "assign": "⏰", "quiz": "📝", "label": "🏷️"}.get(f.get("loai"), "❓")
                    print(f"  {i}. {tag} [{f['mon']}] - [{f['section']}] {f['ten_file']}")

                json.dump(all_files, open(OUT_TAILIEU, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                print(f"[lms] Đã lưu danh mục tài liệu -> {OUT_TAILIEU}")

                if input("\nTải tất cả (chỉ file + folder)? (y/N) ").strip().lower() == "y":
                    print("[3/3] Đang tải xuống máy...")
                    tai_tai_lieu(br, all_files)
            else:
                print("Không tìm thấy tài liệu nào hợp lệ để tải.")
    except Exception as e:
        print(f"LỖI: {e}")
        return 1
    finally:
        br.cleanup()
    return 0


# Đổi lại tên gọi hàm để fix lỗi tham chiếu trong phần Main phía trên
def _lay_thong_deadline(br, url):
    return _lay_thong_tin_deadline(br, url)

if __name__ == "__main__":
    sys.exit(main())