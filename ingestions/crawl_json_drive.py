import os
import time
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from crawl_drive import DriveUtils, DownloadHistoryManager, HCMUTScraper, logger

class StructuredHCMUTScraper(HCMUTScraper):
    
    def extract_structured_drive_tasks(self, url, base_output_folder):
        tasks = []
        try:
            logger.info(f"Đang phân tích cấu trúc bảng tại: {url}")
            self.driver.get(url)
            
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "table"))
            )
            
            rows = self.driver.find_elements(By.CSS_SELECTOR, "table tr")
            current_khoa = "Khoa_Khac" 
            
            for row in rows:
                cells = row.find_elements(By.XPATH, "./td | ./th")
                if not cells: 
                    continue
                
                # Bỏ qua dòng tiêu đề
                if "KHOA" in cells[0].text.upper() and len(cells) > 1 and "NGÀNH" in cells[1].text.upper():
                    continue
                if len(cells) >= 4:
                    # Dòng có đủ Khoa (index 0), Ngành (index 1), CTĐT (index 2), KHGD (index 3)
                    raw_khoa = cells[0].text.strip()
                    if raw_khoa: 
                        current_khoa = DriveUtils.sanitize_filename(raw_khoa)
                    
                    nganh = DriveUtils.sanitize_filename(cells[1].text.strip())
                    
                    # CHỈ lấy cột index 3 (KHGD), bỏ qua cột index 2
                    link_cells = [cells[3]] 
                    
                elif len(cells) >= 3:
                    # Dòng bị gộp Khoa: Ngành (index 0), CTĐT (index 1), KHGD (index 2)
                    nganh = DriveUtils.sanitize_filename(cells[0].text.strip())
                    
                    # CHỈ lấy cột index 3 (KHGD), bỏ qua cột index 1
                    link_cells = [cells[2]]
                else:
                    continue 
                # ---------------------------------------------------------
                
                nganh = nganh if nganh else "Chung"
                folder_path = os.path.join(base_output_folder, current_khoa, nganh)
                
                # Quét thẻ <a> chỉ trong cột Chương trình đào tạo đã chọn
                for cell in link_cells:
                    for link in cell.find_elements(By.TAG_NAME, "a"):
                        href = link.get_attribute("href")
                        text = link.text.strip()
                        
                        if href and DriveUtils.is_drive_link(href):
                            file_id = DriveUtils.extract_id(href)
                            if file_id:
                                tasks.append({
                                    'file_id': file_id,
                                    'text': text or "Tai_lieu",
                                    'output_folder': folder_path
                                })
                                
            return tasks
            
        except Exception as e:
            logger.error(f"Lỗi khi đọc bảng cấu trúc trang {url}: {e}")
            return []

    def process_structured_downloads(self, url, base_output_folder):
        print("\n" + "="*70)
        print("🚀 BẮT ĐẦU CÀO VÀ PHÂN LOẠI FILE THEO: KHOA -> NGÀNH (Bỏ qua KHGD)")
        print("="*70)
        
        tasks = self.extract_structured_drive_tasks(url, base_output_folder)
        
        if not tasks:
            print("❌ Không tìm thấy dữ liệu Drive hoặc không thể đọc bảng.")
            return

        history_manager = DownloadHistoryManager(os.path.join(base_output_folder, "download_history.json"))
        
        new_tasks = []
        all_old_ids = {item['file_id'] for item in history_manager.history.get('downloaded', [])} | \
                      {item['file_id'] for item in history_manager.history.get('failed', [])}
                      
        for t in tasks:
            if t['file_id'] not in all_old_ids:
                new_tasks.append(t)
                
        print(f"📊 Tìm thấy tổng cộng: {len(tasks)} file Drive.")
        print(f"🆕 Cần tải mới: {len(new_tasks)} file.")
        
        if new_tasks:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                for t in new_tasks:
                    os.makedirs(t['output_folder'], exist_ok=True)
                
                futures = [
                    executor.submit(self._download_file, t['file_id'], t['text'], t['output_folder']) 
                    for t in new_tasks
                ]
                
                for f in as_completed(futures):
                    r = f.result()
                    if r.get('success'):
                        history_manager.add_downloaded(r['file_id'], r['filename'], r['path'], r['size'])
                    else:
                        history_manager.add_failed(r['file_id'], r['filename'], r['error'])
                        
        print("\n🎉 HOÀN TẤT TẢI VÀ SẮP XẾP FILE!")
class GraduationDocsScraper(HCMUTScraper):
    """
    Class KẾ THỪA từ HCMUTScraper chuyên dùng để cào các link quy định, 
    hướng dẫn làm Luận văn / Thực tập tốt nghiệp từ trang web.
    """
    
    def crawl_links_to_json(self, url, output_json_path):
        """
        Quét trang web, tìm thẻ chứa danh sách link và xuất ra file JSON.
        """
        print(f"\n🚀 Đang cào dữ liệu từ: {url}")
        results = []
        
        try:
            self.driver.get(url)
            
            # Đợi cho phần tử chứa bài viết xuất hiện (dựa trên class trong hình của bạn)
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.uv-paper-section"))
            )
            
            # Sử dụng CSS Selector nhắm chính xác vào các thẻ <a> nằm trong <p> của <section>
            # Cấu trúc: div.uv-paper-section -> section -> p -> a
            links = self.driver.find_elements(By.CSS_SELECTOR, "div.uv-paper-section section p a")
            
            for link in links:
                href = link.get_attribute("href")
                text = link.text.strip()
                
                # Lọc bỏ các link rỗng hoặc link javascript
                if href and text and not href.startswith(('javascript:', '#', 'mailto:')):
                    # ---------------------------------------------------------
                    # BẢN CẬP NHẬT: LỌC LẤY NĂM >= 2019
                    # ---------------------------------------------------------
                    # Tìm chuỗi 4 chữ số bắt đầu bằng "20" (ví dụ: 2018, 2019, 2023)
                    year_match = re.search(r'\b(20\d{2})\b', text)
                    
                    # Nếu tìm thấy năm VÀ năm đó lớn hơn hoặc bằng 2019 thì mới đưa vào danh sách
                    if year_match and int(year_match.group(1)) >= 2019:
                        results.append({
                            "chuyen_muc": "Thực tập - Luận văn",
                            "tieu_de": text,
                            "url": href
                        })
                    
            # Loại bỏ các link trùng lặp (nếu có) bằng cách dùng Dictionary comprehension
            unique_results = list({item['url']: item for item in results}.values())
            
            print(f"📊 Tìm thấy {len(unique_results)} đường dẫn hợp lệ.")
            
            # Lưu ra file JSON
            os.makedirs(os.path.dirname(output_json_path) or '.', exist_ok=True)
            with open(output_json_path, 'w', encoding='utf-8') as f:
                json.dump(unique_results, f, ensure_ascii=False, indent=4)
                
            print(f"🎉 Đã lưu thành công vào: {output_json_path}")
            
            return unique_results

        except Exception as e:
            logger.error(f"❌ Lỗi khi cào trang {url}: {e}")
            return []
if __name__ == "__main__":
    TARGET_URL = "https://hcmut.edu.vn/bai-viet/chuong-trinh-dao-tao-tu-khoa-2019" 
    BASE_OUTPUT_DIR = "output/ChuongTrinhDaoTao_TheoKhoa"
    OUTPUT_JSON = "output/luan_van_thuc_tap.json"
    TARGET_GRADUATION_URL = "https://mybk.hcmut.edu.vn/bksi/public/vi/blog/thuc-tap-tot-nghiep---de-cuong-luan-van---luan-van-tot-nghiep"
    scraper_Graduation = GraduationDocsScraper(headless=True)
    scraper = StructuredHCMUTScraper(headless=True, max_workers=3)
    
    try:
        scraper.process_structured_downloads(TARGET_URL, BASE_OUTPUT_DIR)
        scraper_Graduation.crawl_links_to_json(TARGET_GRADUATION_URL, OUTPUT_JSON)
    finally:
        scraper.close()