# Giao thức giao tiếp để đánh giá ý tưởng

Tất cả các tác nhân sau này làm việc trong hệ sinh thái tùy chỉnh menu ngữ cảnh Windows phải tuân thủ giao thức có cấu trúc này khi thách thức, đánh giá hoặc tinh chỉnh các ý tưởng được đề xuất.

## 1. Học thuyết "Người rơm mạnh" (Steel-Man)
Trước khi thách thức một ý tưởng, tác nhân đánh giá phải xây dựng phiên bản mạnh nhất có thể của đề xuất ban đầu.
- Nêu rõ đề xuất giá trị cốt lõi rõ ràng hơn tác giả ban đầu.
- Xác định ít nhất một lợi ích chưa được nêu của phương pháp.

## 2. Red-Teaming (đánh giá lỗ hổng)
Khi ý tưởng đã được củng cố, các tác nhân phải thách thức nó theo các hướng sau:
- **Phù hợp hệ sinh thái:** Công cụ này có giống một công cụ menu ngữ cảnh gốc hay đang cố trở thành một ứng dụng đầy đủ?
- **Hiệu suất:** Điều gì xảy ra nếu vô tình chạy trên thư mục có 100.000 tệp?
- **Tính hủy hoại:** Có rủi ro mất dữ liệu không thể khôi phục không?
- **Chi phí phụ thuộc:** Có yêu cầu phụ thuộc bên ngoài quá mức không (ví dụ: thư viện Python khổng lồ hoặc tệp nhị phân chưa cài đặt)?

## 3. Định dạng phản bác
Mọi lời chỉ trích phải được cấu trúc như sau:
- **Giả thuyết:** Ý tưởng nhằm giải quyết điều gì.
- **Lỗ hổng:** Khiếm khuyết hoặc rủi ro cụ thể đã xác định.
- **Công thức thay thế:** Một bước ngoặt được đề xuất giữ nguyên giá trị đồng thời giảm thiểu rủi ro.

## 4. Cơ chế phán quyết cuối cùng
Không được bác bỏ ý tưởng hoàn toàn mà không đề xuất bước ngoặt, trừ khi chúng gây rủi ro thảm khốc cho hệ thống tệp (ví dụ: xóa đệ quy không được theo dõi).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
