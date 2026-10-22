import logging
import re
import os
import json
import time
from pathlib import Path
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys

# Fix encoding UTF-8 cho console output
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('crawl_log.txt', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# =====================================================================
# CLASS 1: CÔNG CỤ XỬ LÝ GOOGLE DRIVE (STATIC)
# =====================================================================
class DriveUtils:
    @staticmethod
    def get_drive_file_metadata(file_id):
        try:
            url = f"https://drive.google.com/uc?id={file_id}&export=view"
            session = requests.Session()
            response = session.head(url, allow_redirects=True, timeout=5)
            filename = None
            if "Content-Disposition" in response.headers:
                disposition = response.headers["Content-Disposition"]
                match = re.search(r"filename\*=UTF-8''(.+?)(?:;|$)", disposition)
                if match:
                    from urllib.parse import unquote
                    filename = unquote(match.group(1))
                if not filename:
                    match = re.search(r'filename=(?:"([^"]+)"|([^;]+))', disposition)
                    if match:
                        filename = match.group(1) or match.group(2)
            content_type = response.headers.get('Content-Type', '')
            return {'filename': filename, 'content_type': content_type, 'status_code': response.status_code}
        except Exception:
            return {'filename': None, 'content_type': None, 'status_code': None}

    @staticmethod
    def get_extension_from_content_type(content_type):
        mime_to_ext = {
            'application/pdf': '.pdf', 'application/msword': '.doc',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'application/vnd.ms-excel': '.xls', 'application/zip': '.zip',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
            'application/x-rar-compressed': '.rar', 'text/plain': '.txt',
        }
        for mime, ext in mime_to_ext.items():
            if mime in content_type.lower():
                return ext
        return None

    @staticmethod
    def sanitize_filename(filename):
        if not filename:
            return None

        # fix lỗi UTF-8 đọc sai
        try:
            filename = filename.encode(
                "latin1"
            ).decode("utf-8")
        except:
            pass

        filename = re.sub(
            r'[<>:"/\\|?*]',
            '',
            filename
        )

        return filename.strip()

    @staticmethod
    def extract_id(url):
        patterns = [
            r'drive\.google\.com/file/d/([a-zA-Z0-9-_]+)',
            r'drive\.google\.com/open\?id=([a-zA-Z0-9-_]+)',
            r'docs\.google\.com/.*?/d/([a-zA-Z0-9-_]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match: return match.group(1)
        return None

    @staticmethod
    def is_drive_link(url):
        return any(domain in url for domain in ['drive.google.com', 'docs.google.com'])

    @staticmethod
    def determine_filename(file_id, link_text, content_type=None):
        filename, extension = None, None
        metadata = DriveUtils.get_drive_file_metadata(file_id)
        if metadata['filename']: filename = DriveUtils.sanitize_filename(metadata['filename'])
        if not filename and link_text: filename = DriveUtils.sanitize_filename(link_text)
        if filename and '.' in filename:
            extension = os.path.splitext(filename)[1]
        else:
            content_type = metadata['content_type'] or content_type
            if content_type: extension = DriveUtils.get_extension_from_content_type(content_type)
        if not extension: extension = '.pdf'
        if not filename: filename = f"file_{file_id}"
        return filename if filename.endswith(extension) else filename + extension


# =====================================================================
# CLASS 2: QUẢN LÝ LỊCH SỬ TẢI XUỐNG
# =====================================================================
class DownloadHistoryManager:
    def __init__(self, history_file="download_history.json"):
        self.history_file = history_file
        self.history = self._load_history()

    def _load_history(self):
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception: pass
        return {'downloaded': [], 'failed': [], 'crawl_log': []}

    def _save_history(self):
        with open(self.history_file, 'w', encoding='utf-8') as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2)

    def find_new_links(self, crawled_links):
        all_old_ids = {item['file_id'] for item in self.history.get('downloaded', [])} | \
                      {item['file_id'] for item in self.history.get('failed', [])}
        return {
            'new': [l for l in crawled_links if l['file_id'] not in all_old_ids],
            'existing': [l for l in crawled_links if l['file_id'] in all_old_ids],
        }

    def add_downloaded(self, file_id, filename, filepath, file_size):
        if any(item['file_id'] == file_id for item in self.history['downloaded']): return
        self.history['downloaded'].append({
            'file_id': file_id, 'filename': filename, 'filepath': filepath,
            'file_size': file_size, 'downloaded_at': datetime.now().isoformat(), 'status': 'success'
        })
        self._save_history()

    def add_failed(self, file_id, filename, error):
        if any(item['file_id'] == file_id for item in self.history['failed']): return
        self.history['failed'].append({
            'file_id': file_id, 'filename': filename, 'error': error,
            'failed_at': datetime.now().isoformat(), 'status': 'failed'
        })
        self._save_history()


# =====================================================================
# CLASS 3: BỘ CÀO DỮ LIỆU HCMUT CHÍNH (ORCHESTRATOR)
# =====================================================================
class HCMUTScraper:
    def __init__(self, headless=True, max_workers=3):
        self.max_workers = max_workers
        self.driver = self._setup_driver(headless)

    def _setup_driver(self, headless):
        options = Options()
        if headless:
            options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        return webdriver.Chrome(options=options)

    def close(self):
        """Đóng trình duyệt an toàn"""
        if self.driver:
            self.driver.quit()
            logger.info("🔒 Đã đóng trình duyệt an toàn.")

    # --- NHIỆM VỤ 1: GOOGLE DRIVE ---
    def _extract_drive_links(self, url, retries=3, timeout=15):
        drive_links = []
        for attempt in range(1, retries + 1):
            try:
                logger.info(f"Đang tìm Drive Links (lần {attempt}): {url}")
                self.driver.get(url)
                wait = WebDriverWait(self.driver, timeout)
                wait.until(EC.presence_of_all_elements_located((By.TAG_NAME, "a")))
                
                content_el = None
                for selector in ["main", "article", ".content", "#content", ".main-content", "section"]:
                    try:
                        content_el = self.driver.find_element(By.CSS_SELECTOR, selector)
                        break
                    except: continue
                        
                scope = content_el if content_el else self.driver
                
                for link in scope.find_elements(By.TAG_NAME, "a"):
                    try:
                        href, text = link.get_attribute("href"), link.text.strip()
                        if href and DriveUtils.is_drive_link(href):
                            file_id = DriveUtils.extract_id(href)
                            if file_id:
                                drive_links.append({"url": href, "text": text or "Không tiêu đề", "file_id": file_id})
                    except: continue
                        
                return list({l['file_id']: l for l in drive_links}.values())
            except TimeoutException:
                if attempt < retries: time.sleep(2)
                else: return []
            except Exception as e:
                logger.error(f"Lỗi khi xử lý trang {url}: {e}")
                return []
    def _extract_drive_links_daihoc(self, url, retries=3, timeout=15):
        drive_links = []
        
        # ĐỊNH NGHĨA XPATH: Tìm thẻ li chứa chữ "Đào tạo đại học", sau đó đi vào div.sub-link và lấy thẻ <a>
        target_xpath = "//li[contains(@class, 'head-title') and contains(., 'Đào tạo đại học')]//div[contains(@class, 'sub-link')]//a"
        
        for attempt in range(1, retries + 1):
            try:
                logger.info(f"Đang tìm Drive Links (lần {attempt}): {url}")
                self.driver.get(url)
                wait = WebDriverWait(self.driver, timeout)
                
                # BƯỚC 1: Đợi cho đến khi phần tử thỏa mãn XPath xuất hiện
                wait.until(EC.presence_of_all_elements_located((By.XPATH, target_xpath)))
                
                # BƯỚC 2: Tìm và lấy tất cả các thẻ <a> theo đúng XPath trên
                links = self.driver.find_elements(By.XPATH, target_xpath)
                
                for link in links:
                    try:
                        href, text = link.get_attribute("href"), link.text.strip()
                        if href and DriveUtils.is_drive_link(href):
                            file_id = DriveUtils.extract_id(href)
                            if file_id:
                                drive_links.append({"url": href, "text": text or "Không tiêu đề", "file_id": file_id})
                    except: continue
                        
                # Lọc trùng lặp file_id trước khi trả về
                return list({l['file_id']: l for l in drive_links}.values())
                
            except TimeoutException:
                if attempt < retries: time.sleep(2)
                else: return []
            except Exception as e:
                logger.error(f"Lỗi khi xử lý trang {url}: {e}")
                return []        

    def _download_file(self, file_id, link_text, output_folder):
        try:
            filename = DriveUtils.determine_filename(file_id, link_text)
            output_path = os.path.join(output_folder, filename)
            counter = 1
            name, ext = os.path.splitext(filename)
            while os.path.exists(output_path):
                output_path = os.path.join(output_folder, f"{name}_{counter}{ext}")
                counter += 1
                
            session = requests.Session()
            url = f"https://drive.google.com/uc?id={file_id}&export=download"
            response = session.get(url, stream=True, timeout=30)
            
            for key, value in response.cookies.items():
                if key.startswith('download_warning'):
                    response = session.get(f"{url}&confirm={value}", stream=True, timeout=30)
                    break
                    
            if response.status_code != 200:
                return {'success': False, 'file_id': file_id, 'filename': filename, 'error': f'HTTP {response.status_code}'}
                
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk: f.write(chunk)
                    
            logger.info(f"✓ Tải thành công: {filename}")
            return {'success': True, 'file_id': file_id, 'filename': filename, 'path': output_path, 'size': os.path.getsize(output_path)}
        except Exception as e:
            return {'success': False, 'file_id': file_id, 'filename': link_text, 'error': str(e)}

    def process_drive_downloads(self, json_file_path, output_folder):
        print("\n" + "="*60 + "\n🚀 NHIỆM VỤ 1: CÀO VÀ TẢI FILE GOOGLE DRIVE\n" + "="*60)
        Path(output_folder).mkdir(parents=True, exist_ok=True)
        history_manager = DownloadHistoryManager(os.path.join(output_folder, "download_history.json"))
        
        # Đọc URL từ file JSON
        urls = []
        if os.path.exists(json_file_path):
            with open(json_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                if isinstance(item, dict):
                    urls.extend([d.get("urls") for d in item.get("dropdownItems", []) if d.get("urls")])
        urls = list(dict.fromkeys(urls)) # Lọc trùng

        for index, url in enumerate(urls, 1):
            print(f"\n[{index}/{len(urls)}] Kiểm tra trang: {url}")
            if "quy-che-quy-dinh" in url:
                drive_links = self._extract_drive_links_daihoc(url)
            else:
                drive_links = self._extract_drive_links(url)
            if drive_links:
                new_links = history_manager.find_new_links(drive_links)['new']
                print(f"📊 Tìm thấy: {len(drive_links)} link Drive | {len(new_links)} link mới.")
                if new_links:
                    with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                        futures = [executor.submit(self._download_file, l['file_id'], l['text'], output_folder) for l in new_links]
                        for f in as_completed(futures):
                            r = f.result()
                            if r.get('success'): history_manager.add_downloaded(r['file_id'], r['filename'], r['path'], r['size'])
                            else: history_manager.add_failed(r['file_id'], r['filename'], r['error'])
            else:
                print("❌ Không tìm thấy Drive links.")

    # --- NHIỆM VỤ 2: WEB CRAWLER CẤU TRÚC ---
    def crawl_chuong_trinh_dao_tao(self, url):
        print(f"\n[Website] Cào Chương trình đào tạo: {url}")
        results = []
        try:
            self.driver.get(url)
            WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.content-container")))
            for link in self.driver.find_elements(By.CSS_SELECTOR, "div.content-container a"):
                href, text = link.get_attribute("href"), link.get_attribute("textContent").strip()
                if href and text and not href.startswith(('javascript:', '#', 'mailto:')) and not DriveUtils.is_drive_link(href):
                    # bỏ khóa 2018 về trước và bỏ các khóa tiến sĩ ( bỏ luôn thông tin liên hệ)
                    if not re.search(r'\b(201[0-8])\b', text) and not re.search(r'\b(tiến sĩ)\b', text, re.IGNORECASE) and not re.search(r'\b(thông tin liên hệ)\b', text, re.IGNORECASE):
                        results.append({"chuyen_muc": "Chương trình đào tạo", "tieu_de": text, "url": href})
            return list({item['url']: item for item in results}.values())
        except Exception as e:
            print(f"❌ Lỗi: {e}"); return []

    def crawl_hoc_phi(self, url):
        print(f"\n[Website] Cào Học phí: {url}")
        results = []
        try:
            self.driver.get(url)
            WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='hoc-phi']")))
            for link in self.driver.find_elements(By.CSS_SELECTOR, "a[href*='hoc-phi']"):
                href, text = link.get_attribute("href"), link.get_attribute("textContent").strip()
                if href and text and not href.startswith(('javascript:', '#')):
                    # lọc các link trùng với link chính
                    if href != url:
                        results.append({"chuyen_muc": "Học phí", "tieu_de": text, "url": href})
                    # chỉ lấy link từ 2023-2024
                    if re.search(r'\b(202[3-9]-202[3-9])\b', text):
                        results.append({"chuyen_muc": "Học phí", "tieu_de": text, "url": href})    
            return list({item['url']: item for item in results}.values())
        except Exception as e:
            print(f"❌ Lỗi: {e}"); return []

    def crawl_lich_hoc_vu(self, url):
        print(f"\n[Website] Cào Lịch học vụ (Tương tác): {url}")
        results = []
        try:
            self.driver.get(url)
            wait = WebDriverWait(self.driver, 15)
            
            # Xóa Modal
            try: self.driver.execute_script("document.querySelectorAll('.modal, .modal-backdrop').forEach(e => e.remove());")
            except: pass
            
            def get_links(muc_name):
                time.sleep(2)
                for a in self.driver.find_elements(By.CSS_SELECTOR, "div.menu-content ul li a"):
                    h, t = a.get_attribute("href"), a.get_attribute("textContent").strip()
                    if h and t: results.append({"muc": muc_name, "tieu_de": t, "url": h})

            # Bước 1: Mặc định
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.menu-content ul li a")))
            get_links("Lịch học vụ 2025-2026")

            # Bước 2: Bấm Lịch Tốt nghiệp
            btn1 = wait.until(EC.presence_of_element_located((By.XPATH, "//div[contains(@class, 'main-menu')]//*[contains(normalize-space(text()), 'Lịch tốt nghiệp')]")))
            self.driver.execute_script("arguments[0].click();", btn1)
            get_links("Lịch tốt nghiệp 2025-2026")

            # Bước 3: Đổi tab 2026-2027
            btn_cq = wait.until(EC.presence_of_element_located((By.XPATH, "//div[contains(@class, 'main-menu')]//*[contains(normalize-space(text()), 'Lịch học vụ chính quy')]")))
            self.driver.execute_script("arguments[0].click();", btn_cq)
            time.sleep(1)
            btn_tab2 = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.year span:nth-child(2) a")))
            self.driver.execute_script("arguments[0].click();", btn_tab2)
            get_links("Lịch học vụ 2026-2027")

            # Bước 4: Tốt nghiệp 2026-2027 (Dự phòng)
            try:
                btn_tn2 = wait.until(EC.presence_of_element_located((By.XPATH, "//div[contains(@class, 'main-menu')]//*[contains(normalize-space(text()), 'Lịch tốt nghiệp')]")))
                self.driver.execute_script("arguments[0].click();", btn_tn2)
                time.sleep(2.5)
                links = self.driver.find_elements(By.CSS_SELECTOR, "div.menu-content ul li a")
                if links: get_links("Lịch tốt nghiệp 2026-2027")
            except: pass

            return list({item['url']: item for item in results}.values())
        except Exception as e:
            print(f"❌ Lỗi: {e}"); return []
    def crawl_bksi_public(self, url):
        print(f"\n[Website] Cào dữ liệu BKSI Public: {url}")
        results = []
        try:
            self.driver.get(url)
            wait = WebDriverWait(self.driver, 15)
            
            # Đợi cho đến khi phần tử chứa class 'uv-kb-folder' xuất hiện
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.uv-kb-folder")))
            time.sleep(1) # Nghỉ một nhịp nhỏ để đảm bảo DOM render hoàn toàn
            
            # Cập nhật CSS_SELECTOR chính xác dựa trên cấu trúc ảnh image_05979c.png
            elements = self.driver.find_elements(By.CSS_SELECTOR, "div.uv-kb-folder ul li a")
            
            for element in elements:
                try:
                    href = element.get_attribute("href")
                    text = element.get_attribute("textContent").strip()
                    
                    if href and text and not href.startswith(('javascript:', '#')):
                        # Xử lý đường dẫn tương đối (ví dụ: /bksi/public/vi/article/82)
                        if href.startswith('/'):
                            href = "https://mybk.hcmut.edu.vn" + href
                            
                        results.append({
                            "chuyen_muc": "Thông tin phổ biến", # Lấy theo tiêu đề h2 trong ảnh
                            "tieu_de": text,
                            "url": href
                        })
                except Exception:
                    continue
                    
            # Lọc trùng lặp dựa trên URL
            return list({item['url']: item for item in results}.values())
        except Exception as e:
            print(f"❌ Lỗi: {e}"); return []    

    def process_web_structural_links(self, output_json):
        print("\n" + "="*60 + "\n🚀 NHIỆM VỤ 2: CÀO DỮ LIỆU CẤU TRÚC WEBSITE\n" + "="*60)
        urls = {
            "dao_tao": "https://hcmut.edu.vn/dao-tao/chuong-trinh-dao-tao",
            "hoc_phi": "https://hcmut.edu.vn/dao-tao/hoc-phi",
            "lich_hoc_vu": "https://hcmut.edu.vn/dao-tao/lich-hoc-vu",
            "bksi": "https://mybk.hcmut.edu.vn/bksi/public/vi/" 
        }
        
        all_data = {
            "dao_tao": self.crawl_chuong_trinh_dao_tao(urls["dao_tao"]),
            "hoc_phi": self.crawl_hoc_phi(urls["hoc_phi"]),
            "lich_hoc_vu": self.crawl_lich_hoc_vu(urls["lich_hoc_vu"]),
            "bksi": self.crawl_bksi_public(urls["bksi"]) # Gọi hàm chạy link mới
        }
        
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(all_data, f, ensure_ascii=False, indent=4)
        print(f"\n🎉 HOÀN TẤT! Đã lưu cấu trúc web vào: {output_json}")


# =====================================================================
# KHỐI CHẠY CHÍNH TỔNG HỢP (MAIN)
# =====================================================================
if __name__ == "__main__":
    # Cấu hình đường dẫn
    INPUT_JSON_DRIVE = "dao_tao.json"
    OUTPUT_DIR_DRIVE = "output/quydinh"
    OUTPUT_JSON_WEB = "hcmut_data.json"
    
    # Khởi tạo Class (đổi headless=False nếu muốn xem trình duyệt chạy thực tế)
    scraper = HCMUTScraper(headless=True, max_workers=3)
    
    try:
        # Nhiệm vụ 1
        scraper.process_drive_downloads(json_file_path=INPUT_JSON_DRIVE, output_folder=OUTPUT_DIR_DRIVE)
        
        # Nhiệm vụ 2
        scraper.process_web_structural_links(output_json=OUTPUT_JSON_WEB)
        
    finally:
        # Dọn dẹp tài nguyên
        scraper.close()