import asyncio
import os
import json
import re
from crawl4ai import AsyncWebCrawler
from crawl4ai.async_configs import BrowserConfig, CrawlerRunConfig

async def crawl_everything(tasks_config: list):
    """
    Quét qua nhiều file JSON, gom toàn bộ link lại và crawl với CSS Selector tương ứng cho từng link.
    """
    all_links_to_crawl = []

    # =====================================================================
    # BƯỚC 1: GOM LINK VÀ LƯU KÈM CSS SELECTOR CỦA TỪNG LINK
    # =====================================================================
    for task in tasks_config:
        json_path = task.get("json_file")
        # (MỚI) Lấy CSS Selector riêng của file JSON này, nếu không có thì mặc định lấy thẻ body
        task_css = task.get("css_selector", "body") 
        
        if not os.path.exists(json_path):
            print(f"❌ Bỏ qua: Không tìm thấy file {json_path}")
            continue

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if "categories" in task:
            for cat_key, out_dir in task["categories"].items():
                items = data.get(cat_key, []) if isinstance(data, dict) else [
                    i for i in data if cat_key in str(i.get("chuyen_muc", "")).lower() or cat_key in str(i.get("muc", "")).lower()
                ]
                for idx, item in enumerate(items, 1):
                    if item.get("url"):
                        all_links_to_crawl.append({
                            "url": item["url"],
                            "title": item.get("tieu_de", f"Bai_{cat_key}_{idx}"),
                            "out_dir": out_dir,
                            "idx": idx,
                            "css_selector": task_css  # (MỚI) Gắn CSS vào hàng đợi
                        })

        elif "output_dir" in task:
            out_dir = task["output_dir"]
            items = data if isinstance(data, list) else [val for sublist in data.values() for val in sublist]
            for idx, item in enumerate(items, 1):
                if isinstance(item, dict) and item.get("url"):
                    all_links_to_crawl.append({
                        "url": item["url"],
                        "title": item.get("tieu_de", f"Bai_viet_{idx}"),
                        "out_dir": out_dir,
                        "idx": idx,
                        "css_selector": task_css  # (MỚI) Gắn CSS vào hàng đợi
                    })

    if not all_links_to_crawl:
        print("⚠️ Không có dữ liệu hợp lệ nào để cào!")
        return

    print(f"\n🚀 TỔNG HỢP: Bắt đầu cào {len(all_links_to_crawl)} đường dẫn...")
    print("="*70)

    # =====================================================================
    # BƯỚC 2: KHỞI TẠO CRAWLER VÀ THAY ĐỔI CSS LINH HOẠT
    # =====================================================================
    browser_config = BrowserConfig(headless=True)

    async with AsyncWebCrawler(config=browser_config) as crawler:
        for i, task_item in enumerate(all_links_to_crawl, 1):
            url = task_item["url"]
            out_dir = task_item["out_dir"]
            title = task_item["title"]
            idx = task_item["idx"]
            css = task_item["css_selector"] # (MỚI) Lấy CSS riêng của link này

            os.makedirs(out_dir, exist_ok=True)
            print(f"[{i}/{len(all_links_to_crawl)}] Đang cào -> {title} (CSS: {css})")
            
            # (MỚI) Tạo cấu hình run_config mới cho mỗi vòng lặp để áp dụng đúng CSS
            run_config = CrawlerRunConfig(css_selector=css)
            result = await crawler.arun(url=url, config=run_config)

            safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip().replace(" ", "_")
            file_path = os.path.join(out_dir, f"{idx}_{safe_title}.md")

            if result.success and result.markdown:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(result.markdown)
                print(f"   ✓ Lưu thành công: {file_path}")
            else:
                print(f"   ❌ Lỗi hoặc thiếu thẻ {css} tại: {url}")

    print("\n🎉 HOÀN TẤT TOÀN BỘ QUÁ TRÌNH CRAWL!")


async def main():
    # =====================================================================
    # CẤU HÌNH TẠI ĐÂY: Thêm CSS riêng cho từng file JSON
    # =====================================================================
    TASKS_CONFIG = [
        # Nhóm 1: Trang web Bách Khoa cũ (hoặc trang chính) dùng ck_editor_view
        {
            "json_file": "hcmut_data.json",
            "categories": {
                "hoc_phi": "output/hoc_phi",
                "lich_hoc_vu": "output/lich_hoc_vu"
            },
            "css_selector": "div.ck_editor_view"  # <-- Định nghĩa CSS ở đây
        },
        
        # Nhóm 2: Trang web khác (hoặc sub-domain) dùng cấu trúc khác
        {
            "json_file": "output/luan_van_thuc_tap.json", 
            "output_dir": "output/luan_van_thuc_tap_md",
            "css_selector": "div.uv-paper-section" # <-- Định nghĩa CSS khác ở đây
        },
        # nhóm 3: Trang web BKSI (ví dụ) dùng cấu trúc khác nữa
        {
            "json_file": "hcmut_data.json", # Tên file JSON chứa danh sách link BKSI của bạn
            "categories": {
                "bksi": "output/bksi_md"
            },
            #"output_dir": "output/bksi_md", # Thư mục bạn muốn lưu file .md
            "css_selector": "div.uv-paper-section" # Bộ chọn bọc lấy phần nội dung chính
        }
    ]
    
    
    await crawl_everything(tasks_config=TASKS_CONFIG)

if __name__ == "__main__":
    asyncio.run(main())