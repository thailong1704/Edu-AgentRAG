# Edu-AgentRAG: Chatbot học vụ dùng Agentic RAG cho sinh viên HCMUT

Trợ lý học vụ đa tác nhân (multi-agent) giải đáp các câu hỏi về quy chế, chương trình đào tạo, học phí và lịch học vụ. Hệ thống còn kết nối với dữ liệu LMS / MyBK của từng sinh viên để tra cứu lịch học, lịch thi, deadline và theo dõi tiến độ học tập.


---

## Bối cảnh

HCMUT tuyển khoảng 6.000 tân sinh viên mỗi năm, kéo theo lượng lớn câu hỏi về chương trình đào tạo, đăng ký học phần, điều kiện tiên quyết, xét tốt nghiệp và các quy định học vụ. Các kênh hỗ trợ như email, BKSI và tư vấn trực tiếp vẫn cần thời gian phản hồi, đặc biệt vào giai đoạn cao điểm.

RAG truyền thống dùng một tác nhân duy nhất chưa đủ cho bài toán này, vì nhiều câu hỏi cần kết hợp nhiều nguồn (văn bản chính thức, bảng điểm cá nhân, dữ liệu LMS) và nhiều bước suy luận. Edu-AgentRAG giải quyết bằng thiết kế **đa tác nhân kết hợp gọi công cụ (tool calling)**.

**Câu hỏi nghiên cứu**

1. Agentic RAG có nâng cao chất lượng tra cứu và giải đáp học vụ so với RAG truyền thống không?
2. Agentic RAG có xử lý tốt hơn các câu hỏi cần tổng hợp nhiều tài liệu hoặc nhiều bước suy luận không?
3. Làm thế nào để cập nhật liên tục cơ sở tri thức, bảo đảm chatbot luôn dùng quy định mới nhất?

---

## Tính năng chính

- **Hỏi đáp học vụ kèm nguồn trích dẫn.** Câu trả lời dựa trên văn bản chính thức của nhà trường, có tham chiếu nguồn.
- **Hỗ trợ cá nhân hóa.** Tra cứu thời khóa biểu, lịch thi, tài liệu môn học và hạn nộp bài từ LMS và MyBK.
- **Tư vấn tiến độ học tập.** Tính GPA, kiểm tra điều kiện tiên quyết và điều kiện tốt nghiệp, đề xuất lộ trình học dựa trên đồ thị tri thức Neo4j.
- **Bộ nhớ hội thoại (Memory).** Duy trì ngữ cảnh qua nhiều lượt hỏi đáp.
- **Hai chế độ trả lời Flash / Deep.** Flash ngắn gọn cho tra cứu nhanh, Deep chi tiết và liệt kê thêm thông tin liên quan.
- **Ưu tiên dữ liệu mới nhất.** Cơ chế "Prefer Latest" ưu tiên tài liệu học phí và lịch học vụ mới nhất khi người dùng không nêu rõ năm học hoặc học kỳ.

---



### Các agent

| Agent | Nhiệm vụ | Nguồn dữ liệu |
|---|---|---|
| **Điều phối (Orchestrator)** | Phân tích ý định, chọn một hoặc nhiều agent (multi-agent calling), kiểm tra tính nhất quán, tổng hợp kết quả, lưu lịch sử hội thoại gần nhất | Lịch sử hội thoại |
| **Agent Học vụ** | Quy chế, chương trình đào tạo, học phí, lịch học vụ, điều kiện đăng ký học phần tốt nghiệp | Cơ sở tri thức Qdrant |
| **Agent LMS & TKB** | Thời khóa biểu, lịch thi, tìm tài liệu, hạn nộp bài, tạo liên kết tải tệp | JSON đồng bộ từ LMS (Moodle) và MyBK |
| **Agent Tư vấn tiến độ** | Tính GPA, kiểm tra tiên quyết, điều kiện tốt nghiệp, lộ trình học | Đồ thị tri thức Neo4j |

Mỗi agent tự lập kế hoạch xử lý: LLM phân tích câu hỏi, trích xuất tham số (ngành, khóa, mã môn, học kỳ) và chọn công cụ phù hợp. Công cụ chia thành hai nhóm: **công cụ truy xuất** (chương trình đào tạo, học phí, lịch học vụ, quy định, điều kiện tốt nghiệp) và **công cụ tiện ích** (phân biệt học kỳ, làm tròn, tính toán).

---

## Cách hoạt động

### 1. Xây dựng cơ sở tri thức (ngoại tuyến)

Tài liệu chính thức được thu thập từ website đào tạo của HCMUT, xử lý rồi lập chỉ mục.

| Nguồn | Định dạng | Số lượng |
|---|---|---|
| Quy chế, quy định | PDF | 17 |
| Chương trình đào tạo (từ khóa 2019) | PDF | 319 |
| Điều kiện đăng ký học phần tốt nghiệp | Markdown | 5 |
| Học phí | Markdown | 6 |
| Lịch học vụ | Markdown | 4 |
| **Tổng** | | **351** |

Quy trình xử lý:

- Loại bỏ tiêu đề đầu trang, chân trang và chữ ký.
- Nhận diện cấu trúc **Chương → Điều → Khoản → Điểm** và dùng ngăn xếp (stack) để dựng cây phân cấp, giúp mỗi chunk giữ đúng vị trí và ngữ cảnh.
- **Semantic Splitting** theo ranh giới câu và mục, có vùng chồng lấp (overlap) giữa các chunk.
- Bảng biểu được nhận diện bằng mô hình học sâu, chuyển sang Markdown và bao bởi ký tự đánh dấu (Table Sentinel) để xử lý như một khối duy nhất.
- Metadata cho mỗi chunk: tên tài liệu, ngày ban hành, đường dẫn tiêu đề, năm học, loại dữ liệu.
- Mỗi chunk lưu vào Qdrant dưới dạng một point gồm dense vector, sparse vector và payload metadata.
- Dữ liệu tách thành hai collection (**CTĐT** và **Hành chính**) vì chương trình đào tạo chiếm hơn 97% tổng số chunk.

### 2. Truy xuất (trực tuyến)

Truy xuất hai tầng:

1. **Hybrid Search** thu hồi K = 10 chunk ứng viên.
   - Dense: `text-embedding-3-large`
   - Sparse: `Qdrant/bm25` (phù hợp với mã môn học, mã chương trình, mã học kỳ)
   - Hợp nhất bằng **Reciprocal Rank Fusion (RRF)**
2. **Re-ranking** bằng `Cohere rerank-multilingual-v3`, giữ lại N = 7 chunk tốt nhất.
3. **Context Expansion** lấy thêm các chunk liền kề trong cùng tài liệu, ghép theo thứ tự gốc, loại nội dung trùng lặp và giới hạn kích thước ngữ cảnh trước khi đưa cho LLM.

### 3. Dữ liệu cá nhân

- **LMS & MyBK:** agent tự động đăng nhập, phân tích cấu trúc HTML của MyBK và cấu trúc khóa học trên LMS (Moodle), rồi lưu dữ liệu chuẩn hóa dưới dạng JSON cục bộ. Truy vấn dùng so khớp chính xác (exact match) kết hợp xử lý logic thời gian thực (số ngày còn lại đến deadline, trạng thái mở hoặc quá hạn). Dữ liệu chỉ được đồng bộ khi sinh viên yêu cầu cập nhật.
- **Đồ thị tri thức học tập (Neo4j):** bảng điểm và chương trình đào tạo được chuẩn hóa rồi nạp vào Neo4j bằng Cypher `MERGE`, có index trên mã môn học và mã sinh viên.
  - Nút: Sinh viên, Ngoại ngữ, Môn học, Ngành đào tạo, Điều kiện tốt nghiệp
  - Quan hệ: `DA_HOC`, `CHUA_HOC`, `CO_DIEU_KIEN`, `THUOC_NGANH`, `TIEN_QUYET`, `SONG_HANH`, `KHUYEN_NGHI`

---

## Đánh giá

Bộ kiểm thử gồm **100 câu hỏi** dựa trên truy vấn thực tế, thuộc các nhóm chương trình đào tạo, học phí, lịch học vụ, quy định và điều kiện học vụ. Mỗi mẫu có câu trả lời tham chiếu và gold chunk.

**Chất lượng truy xuất (K = 10)**

| K | N | Context Precision | Context Recall | F1 | F2 |
|---|---|---|---|---|---|
| 10 | 6 | 0.7564 | 0.6042 | 0.5493 | 0.5645 |
| **10** | **7** | 0.7235 | 0.6538 | 0.5924 | **0.6055** |
| 10 | 8 | 0.7206 | 0.6667 | 0.5763 | 0.5893 |

F2 được chọn làm chỉ số chính vì bài toán học vụ ưu tiên khả năng thu hồi đầy đủ thông tin. Cấu hình K = 10, N = 7 đạt F2 cao nhất trong các cấu hình K = 20, 15 và 10 đã khảo sát.

**Chất lượng câu trả lời end-to-end**

| Chỉ số | Kết quả |
|---|---|
| Số câu đúng | 91 / 100 |
| Accuracy | 91% |
| Thời gian phản hồi trung bình | 17.33 giây |

**Khảo sát người dùng** (5 sinh viên, thang Likert 5 mức)

| Tiêu chí | Điểm trung bình |
|---|---|
| Giao diện và tính dễ sử dụng | 4.4 |
| Mức độ rõ ràng, dễ hiểu của câu trả lời | 4.2 |
| Mức độ hài lòng chung | 4.2 |
| Độ chính xác của câu trả lời | 4.0 |
| Khả năng hiểu ngữ cảnh | 4.0 |
| Hiệu suất và tốc độ phản hồi | 3.4 |

---

## Công nghệ sử dụng

| Lĩnh vực | Công nghệ |
|---|---|
| Kiến trúc agent | Agentic RAG, Multi-Agent, Planning, Tool Calling |
| Cơ sở dữ liệu vector | Qdrant (dense + sparse vector) |
| Cơ sở dữ liệu đồ thị | Neo4j, Cypher |
| Embedding | OpenAI `text-embedding-3-large` |
| Truy xuất từ khóa | BM25 (`Qdrant/bm25`), hợp nhất RRF |
| Re-ranking | Cohere `rerank-multilingual-v3` |
| Nguồn dữ liệu | Website HCMUT, MyBK, LMS (Moodle) |


## Hạn chế

- **Thời gian phản hồi:** truy xuất nhiều bước kết hợp gọi LLM khiến một số truy vấn chậm, nhất là câu hỏi cần nhiều bước suy luận.
- **Chi phí vận hành:** phụ thuộc vào các API thương mại cho embedding, re-ranking và LLM.
- **Phụ thuộc vào LLM:** câu trả lời có thể chưa đầy đủ khi ngữ cảnh truy xuất không rõ ràng hoặc có nhiều ràng buộc.
- **Phạm vi dữ liệu:** cơ sở tri thức hiện tập trung vào bậc đại học, khóa K19 về sau, chưa phủ toàn trường.

## Hướng phát triển

- Mở rộng cơ sở tri thức sang quy định dành cho giảng viên và các bậc sau đại học.
- Bổ sung công cụ như dự đoán khả năng tốt nghiệp, nhắc đóng học phí.
- Giảm chi phí và độ trễ bằng mô hình mã nguồn mở được fine-tune và cơ chế caching.
- Xây dựng đồ thị tri thức cho văn bản quy định để biết văn bản nào thay thế, sửa đổi hoặc hết hiệu lực.
