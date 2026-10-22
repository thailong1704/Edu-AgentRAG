import json
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

def crawl_main_menu_4(url):
    """Cào dữ liệu đào tạo từ trang web của HCMUT."""
    options = Options()
    options.add_argument("--headless")  
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080") 
    
    driver = webdriver.Chrome(options=options)
    
    try:
        driver.get(url)
        
        # Dùng WebDriverWait thay vì sleep cứng để chờ element xuất hiện
        wait = WebDriverWait(driver, 10)
        menu = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li#main-menu-4")))
        nav_link = menu.find_element(By.CSS_SELECTOR, "a.nav-link")
        menu_title = nav_link.text.strip()
        
        # Cuộn chuột đến phần tử để đảm bảo nó nằm trong khung hình (tránh lỗi interactable)
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", menu)
        time.sleep(1) # Đợi trang cuộn xong
        
        # Hover chuột để hiện dropdown
        ActionChains(driver).move_to_element(nav_link).perform()
        time.sleep(1.5)  # Chờ hiệu ứng dropdown mở ra hoàn toàn
        
        # Lấy trực tiếp tất cả các link trong menu con
        dropdown_items = []
        links = menu.find_elements(By.CSS_SELECTOR, "ul.dropdown-menu li a")
        # chỉ lấy 4 dropdown items đầu tiên
        links = links[:4]   
        for link in links:
            # Lấy text thông qua thuộc tính textContent hoặc innerText (bắt rễ cả phần tử đang bị ẩn một phần)
            text = link.get_attribute("textContent").strip() 
            if text:
                dropdown_items.append({
                    "title": text,
                    "urls": link.get_attribute("href") or ""
                })
                
        return [{"title": menu_title, "dropdownItems": dropdown_items}]
        
    except Exception as e:
        print(f"Lỗi: {e}")
        return []
        
    finally:
        driver.quit()

if __name__ == "__main__":
    URL = "https://hcmut.edu.vn/" 
    print("Đang cào dữ liệu...")
    
    menu_data = crawl_main_menu_4(URL)
    
    if menu_data:
        # Lưu file JSON
        with open("dao_tao.json", "w", encoding="utf-8") as f:
            json.dump(menu_data, f, ensure_ascii=False, indent=2)
            
        print("\n=== KẾT QUẢ ===")
        print(json.dumps(menu_data, ensure_ascii=False, indent=2))
        print(f" Tổng số mục con: {len(menu_data[0]['dropdownItems'])}")