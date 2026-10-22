
import argparse
import getpass
import json
import os
import re
import sys
import time

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

load_dotenv()

SSO_URL = ("https://sso.hcmut.edu.vn/cas/login?service="
           "https%3A%2F%2Fmybk.hcmut.edu.vn%2Fapp%2Flogin%2Fcas")
GRADE_URL = "https://mybk.hcmut.edu.vn/app/sinh-vien/ket-qua-hoc-tap/chuong-trinh-dao-tao"

# Thang điểm chữ -> hệ 4 (khớp gpa_agent.py)
DIEM_CHU_MAP = {"A+": 4.0, "A": 4.0, "B+": 3.5, "B": 3.0,
                "C+": 2.5, "C": 2.0, "D+": 1.5, "D": 1.0, "F": 0.0}
# Môn ĐIỀU KIỆN TỐT NGHIỆP dạng đạt/chưa (ngoại ngữ đầu ra, GDQP, GDTC, kỹ năng...):
# KHÔNG tính vào GPA, nhưng PHẢI hoàn thành mới đủ điều kiện tốt nghiệp -> GIỮ LẠI
# thành nhóm riêng để biết đã "tick" chưa, thay vì vứt đi.
MON_DIEU_KIEN_TN = {"ENG_GC_600", "SA4001", "MI1003", "CCGDTC"}


def _parse_float(value: str) -> float:
    """Parse điểm số an toàn. '--'/rỗng -> None. Giá trị > 10 -> None: đó là MÃ TRẠNG THÁI
    (12/21 = đạt, 23 = tạm đạt của môn chuẩn) hiển thị ở cột điểm, KHÔNG phải điểm GPA
    (thang điểm ≤ 10). Nếu để nguyên sẽ nhét 12/21 vào diem_so làm hỏng tính điểm."""
    v = (value or "").strip()
    if v in ("", "--"):
        return None
    try:
        f = float(v.replace(",", "."))
    except (ValueError, AttributeError):
        return None
    return None if f > 10 else f


# Anh văn (chuẩn ngoại ngữ theo năm: Anh văn 1..4) = mã bắt đầu 'LA'. KHÁC 'ENG_GC_600'
# (chuẩn ngoại ngữ ĐẦU RA tốt nghiệp, đã nằm trong MON_DIEU_KIEN_TN). Tách RIÊNG khỏi GPA:
# Anh văn KHÔNG tính GPA, nhưng 'đạt Anh văn N' là điều kiện lên trình độ năm (SV_N2/N3/N4).
def _la_anh_van(ma: str) -> bool:
    return str(ma or "").strip().upper().startswith("LA")


# MÃ CHUẨN ở cột "điểm" khi môn ĐẠT BẰNG CHUẨN (không có điểm chữ): 21=Đạt, 23=Tạm đạt
# (doc #2), 12=Đạt. Các mã này => ĐẠT + CỘNG tín chỉ tích lũy, nhưng KHÔNG có điểm -> KHÔNG
# tính GPA. (KHÁC điểm số thật hệ 10 vốn <= 10.)
_MA_CHUAN_DAT = {"12", "21", "23"}


_RE_BAC_AV = re.compile(r"anh\s*v[ăa]n\s*(\d)", re.IGNORECASE)


def _bac_anh_van(ten_mon: str):
    """Bậc Anh văn (1..4) trích từ TÊN môn ('Anh văn 2' -> 2). Không rõ -> None."""
    m = _RE_BAC_AV.search(str(ten_mon or ""))
    return int(m.group(1)) if m else None


class MyBKCrawler:
    def __init__(self, output_file: str = "ket_qua.json", headless: bool = False):
        self.output_file = output_file
        self.headless = headless
        self.driver = None

    # ---------- Selenium ----------
    def setup_driver(self):
        options = webdriver.ChromeOptions()
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        if self.headless:
            options.add_argument("--headless=new")
        self.driver = webdriver.Chrome(options=options)
        self.driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    def cleanup(self):
        if self.driver:
            self.driver.quit()
            self.driver = None

    # ---------- đăng nhập + scrape ----------
    def login_and_scrape(self, username: str, password: str) -> dict:
        self.driver.get(SSO_URL)
        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.NAME, "username"))
        ).send_keys(username)
        self.driver.find_element(By.NAME, "password").send_keys(password)
        self.driver.find_element(By.NAME, "submit").click()
        WebDriverWait(self.driver, 20).until(
            lambda d: "sso.hcmut.edu.vn" not in d.current_url
        )

        self.driver.get(GRADE_URL)
        time.sleep(10)                       # chờ SPA render (giữ như bản gốc)
        WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.ID, "tab_1"))
        )
        return self._extract_grades()

    def _extract_grades(self) -> dict:
        # Thông tin sinh viên (khối 1) + chương trình đào tạo (khối 3)
        try:
            lines = self.driver.find_element(
                By.XPATH, "//div[@id='printPage']/div[1]").text.splitlines()
        except NoSuchElementException:
            lines = []
        try:
            lines_ctdt = self.driver.find_element(
                By.XPATH, "//body/div/div/section/div/div/div/div/div[@id='printPage']/div[3]"
            ).text.splitlines()
        except NoSuchElementException:
            lines_ctdt = []

        ma_ctdt = self._doc_ma_ctdt(lines + lines_ctdt)

        # Bảng điểm (tab_1)
        rows = self.driver.find_element(By.ID, "tab_1").find_elements(By.XPATH, ".//tbody/tr")
        mon_gpa, mon_dieu_kien, ngoai_ngu, khoi_list = [], [], [], []
        khoi_ht = None                 # khối đang xét

        for row in rows:
            cols = row.find_elements(By.TAG_NAME, "td")
            if not cols:
                continue

            # ---------- DÒNG TIÊU ĐỀ KHỐI (td đầu có colspan) ----------
            # [0]=tên khối (colspan), [1]=x/y tín chỉ, [2]=tick, [3]=loại khối
            if cols[0].get_attribute("colspan"):
                ten_khoi = cols[0].text.strip()
                tc_dat = tc_can = None
                if len(cols) >= 2:
                    m = re.search(r"(\d+)\s*/\s*(\d+)", cols[1].text)
                    if m:
                        tc_dat, tc_can = int(m.group(1)), int(m.group(2))
                # tick khối = có icon fa-circle-check ở dòng title
                co_tick = len(row.find_elements(By.CSS_SELECTOR, "i.fa-circle-check")) > 0
                loai_khoi = cols[3].text.strip() if len(cols) >= 4 else ""
                # ĐỦ khối: ưu tiên so x>=y (chắc nhất); nếu không đọc được số thì dựa tick
                du = (tc_dat >= tc_can) if (tc_dat is not None and tc_can is not None) else co_tick
                khoi_ht = {
                    "ten_khoi": ten_khoi,
                    "tin_chi_dat": tc_dat,
                    "tin_chi_can": tc_can,
                    "du": du,
                    "co_tick": co_tick,
                    "loai_khoi": loai_khoi,      # 'Khối kiến thức Bắt buộc' / 'Tự chọn'
                }
                khoi_list.append(khoi_ht)
                continue

            # ---------- DÒNG MÔN ----------
            if len(cols) < 6:
                continue
            ma = cols[1].text.strip()
            if not ma:
                continue
            diem_chu = cols[4].text.strip() or "--"
            try:
                tc = int(cols[5].text.strip()) if cols[5].text.strip().isdigit() else 0
            except (ValueError, AttributeError):
                tc = 0
            ten_khoi_ht = khoi_ht["ten_khoi"] if khoi_ht else ""
            mon = {
                "ma_mon_hoc": ma,
                "ten_mon_hoc": cols[2].text.strip(),
                "diem_so": _parse_float(cols[3].text),
                "diem_chu": diem_chu,
                "tin_chi": tc,
                "khoi": ten_khoi_ht,
            }

            # Môn điều kiện tốt nghiệp (đạt/chưa) -> nhóm riêng.
            # Suy trạng thái hoàn thành theo 2 mức:
            #   (1) nếu KHỐI của môn đã đủ (du=True) -> coi như hoàn thành
            #       (đúng cho GDTC/GDQP: mỗi khối 1 điều kiện);
            #   (2) khối chưa đủ nhưng khối chứa nhiều điều kiện khác trạng thái
            #       (vd 'Điều kiện tốt nghiệp' có cả tiếng Anh chưa xong lẫn HĐSV đã xong)
            #       -> đọc ICON tick của chính dòng đó.
            if ma in MON_DIEU_KIEN_TN:
                khoi_du = bool(khoi_ht and khoi_ht.get("du"))
                icons = row.find_elements(
                    By.CSS_SELECTOR, "td i, td span")
                co_tick = any(
                    ("check" in (ic.get_attribute("class") or "").lower()
                     or "green" in (ic.get_attribute("class") or "").lower())
                    for ic in icons)
                mon["da_hoan_thanh"] = khoi_du or co_tick
                mon_dieu_kien.append(mon)
                continue

            # ANH VĂN: HỌC LẤY ĐIỂM. Nếu CÓ ĐIỂM CHỮ thật -> tính GPA như môn thường + được
            # cải thiện. Nhưng nhiều môn Anh văn chỉ hiện MÃ TRẠNG THÁI ('12'/'21' = đạt) +
            # điểm chữ '--': tức ĐẠT chuẩn và +2 tín chỉ tích lũy, NHƯNG không có điểm nên
            # KHÔNG tính GPA. Luôn ghi vào 'ngoai_ngu' (bậc + đạt/chưa) để xét lên trình độ năm.
            if _la_anh_van(ma):
                co_diem = diem_chu in DIEM_CHU_MAP           # có điểm chữ thật -> vào GPA
                # 'Đạt' = có điểm chữ (không F) HOẶC được tick đạt (trường hợp '12'/'21' + '--').
                icons = row.find_elements(By.CSS_SELECTOR, "td i, td span")
                co_tick = any(("check" in (ic.get_attribute("class") or "").lower()
                               or "green" in (ic.get_attribute("class") or "").lower())
                              for ic in icons)
                mon["bac"] = _bac_anh_van(mon["ten_mon_hoc"])
                mon["dat"] = (diem_chu != "F") and (co_diem or co_tick)
                ngoai_ngu.append(mon)                        # luôn ghi để xét chuẩn ngoại ngữ
                if co_diem:
                    mon_gpa.append(mon)     # có điểm chữ -> tính GPA (không ẩn khỏi cải thiện)
                continue

            mon_gpa.append(mon)

        def da_hoc(m):
            return m["diem_chu"] in DIEM_CHU_MAP

        # GPA: mọi môn ĐÃ HỌC, sắp điểm tăng dần (thấp trước để ưu tiên cải thiện)
        da_hoc_ds = sorted(
            [m for m in mon_gpa if da_hoc(m)],
            key=lambda m: (DIEM_CHU_MAP[m["diem_chu"]], -m["tin_chi"]),
        )

        # "Cần học" xét theo KHỐI, KHÔNG theo từng môn:
        #   khối ĐÃ ĐỦ (vd Chuyên ngành 9/9) -> bỏ qua dù còn môn '--' bên trong.
        #   khối CHƯA ĐỦ -> lấy môn chưa học trong khối đó làm gợi ý.
        khoi_chua_du = {k["ten_khoi"] for k in khoi_list if not k["du"]}
        can_hoc = [m for m in mon_gpa
                   if not da_hoc(m) and m["khoi"] in khoi_chua_du]

        return {
            "thong_tin_sinh_vien": lines,
            "chuong_trinh_dao_tao": lines_ctdt,
            "ma_ctdt": ma_ctdt,
            "khoi_kien_thuc": khoi_list,     # trạng thái từng khối: x/y tín chỉ, đủ/chưa
            "mon_hoc_theo_nhom": {
                "da_hoc": da_hoc_ds,             # đã có điểm, sắp theo ưu tiên cải thiện
                "mon_can_hoc": can_hoc,          # CHỈ môn chưa học thuộc khối CHƯA đủ
                "dieu_kien_tot_nghiep": mon_dieu_kien,
                "ngoai_ngu": ngoai_ngu,          # Anh văn 1..4: đạt/chưa, KHÔNG tính GPA
            },
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @staticmethod
    def _doc_ma_ctdt(lines) -> str:
        """Cố gắng đọc mã CTĐT (vd 'KTDTVT_2022') từ text ngành + khóa.
        Không chắc chắn nên chỉ trả gợi ý; để trống nếu không đọc được."""
        nganh = khoa = None
        for s in lines:
            s = str(s)
            if "Ngành" in s and ":" in s:
                nganh = s.split(":", 1)[1].strip()
            m = re.search(r"(20\d{2})", s)
            if m and ("Khó" in s or "khoá" in s.lower() or "khóa" in s.lower()):
                khoa = m.group(1)
        return f"{nganh} {khoa}".strip() if (nganh or khoa) else ""

    def save(self, data: dict):
        with open(self.output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def tom_tat(data: dict) -> bool:
    """In tóm tắt + validation gate. Trả False nếu crawl ra rỗng."""
    nhom = (data or {}).get("mon_hoc_theo_nhom", {}) or {}
    dk = nhom.get("dieu_kien_tot_nghiep", []) or []
    da_tick = sum(1 for m in dk if m.get("da_hoan_thanh"))
    av = nhom.get("ngoai_ngu", []) or []
    av_dat = sum(1 for m in av if m.get("dat"))
    tong = sum(len(nhom.get(k, []) or []) for k in ("da_hoc", "mon_can_hoc"))
    print("\n" + "=" * 48)
    print(f"  Mã CTĐT (gợi ý): {data.get('ma_ctdt') or '(không đọc được)'}")
    print(f"  Đã học (tính GPA)  : {len(nhom.get('da_hoc', []))}")
    print(f"  Chưa học           : {len(nhom.get('mon_can_hoc', []))}")
    print(f"  Anh văn (chuẩn NN) : {av_dat}/{len(av)} đã đạt")
    print(f"  Môn điều kiện TN   : {da_tick}/{len(dk)} đã hoàn thành")
    print(f"  TỔNG (tính GPA)    : {tong} môn")
    print("=" * 48)
    if tong == 0:
        print("CẢNH BÁO: không lấy được môn nào — sai tài khoản, MyBK đổi giao diện, "
              "hoặc bị chặn đăng nhập. KHÔNG dùng file này để tư vấn GPA.")
        return False
    print("OK. Tiếp theo: đặt MA_CTDT rồi chạy graph_bang_diem.py")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Crawl bảng điểm MyBK -> JSON")
    ap.add_argument("-u", "--user", default=os.getenv("MYBK_USER"))
    ap.add_argument("-o", "--out", default=os.getenv("BANG_DIEM_FILE", "ket_qua.json"))
    ap.add_argument("--headless", action="store_true", help="Chạy ẩn trình duyệt")
    args = ap.parse_args()

    username = args.user or input("Tài khoản MyBK: ").strip()
    password = os.getenv("MYBK_PASS") or getpass.getpass("Mật khẩu MyBK: ")
    if not username or not password:
        print("Thiếu tài khoản hoặc mật khẩu.")
        return 1

    crawler = MyBKCrawler(output_file=args.out, headless=args.headless)
    try:
        print("[1/3] Mở trình duyệt...")
        crawler.setup_driver()
        print("[2/3] Đăng nhập MyBK và lấy bảng điểm...")
        data = crawler.login_and_scrape(username, password)
    except TimeoutException:
        print("LỖI: quá thời gian tải trang. File cũ được giữ nguyên.")
        return 1
    except Exception as e:
        print(f"LỖI khi crawl ({type(e).__name__}): {e}. File cũ được giữ nguyên.")
        return 1
    finally:
        crawler.cleanup()

    if not tom_tat(data):
        print("(Không ghi đè file vì dữ liệu rỗng.)")
        return 1

    print(f"[3/3] Ghi ra '{args.out}'...")
    crawler.save(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())