# AUDIT REPORT — MultiSource v4.4.27

Ngày audit: 2026-07-23

## 1. Mục tiêu bản pilot

Bản v4.4.27 thử kiến trúc **Fast Registry trước, Chromium fallback sau** cho hai nguồn đang tốn thời gian và có tỷ lệ bắt link thấp nhất:

- Xôi Lạc: BLV → `channelN` → dựng tuyến `live2` không token.
- ColaTV: BLV → stream ID 8 chữ số → dựng ba tuyến CDN `meung/miekgo`.

`all_live.m3u` vẫn giữ chính sách v4.4.26:

- Chỉ stream verified/browser-observed hợp lệ.
- Last-known-good chỉ được phục hồi sau khi probe lại thành công.
- Không đưa placeholder, metadata-only, pending, URL HTML hoặc Xôi Lạc HTTP 403 vào playlist chính.

## 2. Căn cứ thiết kế

Hai registry ban đầu được seed từ file `ttthethao.m3u` do người dùng cung cấp. File tham chiếu cho thấy:

- Xôi Lạc dùng các tuyến như `live2.streambylivepulse.com/live/channelN.flv` không có `wsSecret`, kèm Referer player và User-Agent trình duyệt di động.
- ColaTV dùng stream ID cố định theo BLV, dựng FLV/HLS trên các host `meung.app` và `miekgo.app`.

Registry chỉ tạo **candidate URL**; URL vẫn phải qua probe media của source hiện tại. Không copy mù nội dung playlist tham chiếu vào `all_live.m3u`.

## 3. Thay đổi Xôi Lạc

Build adapter: `4.4.27-XOILAC-FAST-LIVE2-REGISTRY`.

- Bỏ quy tắc loại toàn bộ host `live2` chỉ vì URL không có `wsSecret`.
- Player `type/8` vẫn bị phân loại quảng cáo/placeholder và không publish.
- Thêm hai template mặc định:
  - `https://live2.streambylivepulse.com/live/{channel}.flv`
  - `https://live2.pro2cdnlive.com/live/{channel}.flv`
- Gắn Referer mặc định `https://xlz.livepingscorex.com/` và User-Agent Chrome Android giống mẫu M3U tham chiếu.
- Probe FLV dùng Range nhỏ, chấp nhận HTTP 200/206 và bắt buộc bytes đầu là `FLV`.
- Chỉ retry TLS không xác minh khi lỗi đúng loại `CERTIFICATE_VERIFY_FAILED`, phù hợp với Chromium đang bật `ignore_https_errors`.
- Tự học BLV → `channelN` khi player thực tế lộ `/type/.../link/channelN`.
- Fast registry thất bại thì tiếp tục cơ chế cũ: mở player, bắt request, thử token signed và Chromium fallback.
- Signed URL HTTP 403 vẫn chỉ lưu debug/rejected, không vào `all_live.m3u`.

### Rào chắn tránh gán nhầm trận

Một BLV/channel có thể phát nhiều trận liên tiếp. Vì vậy fast registry chỉ được thử khi `minutes_to_kickoff` nằm trong:

```text
-150 ≤ minutes_to_kickoff ≤ +15
```

Tức là từ 15 phút trước giờ bắt đầu đến 150 phút sau giờ bắt đầu. Cửa sổ thu card và Chromium đầy đủ vẫn là `-150/+180` phút.

## 4. Thay đổi ColaTV

Build adapter: `4.4.27-COLATV-FAST-BLV-REGISTRY`.

- Thêm registry BLV → stream ID 8 chữ số.
- Dựng ba candidate mặc định:
  - `https://live05.meung.app/live/{stream_id}.flv`
  - `https://live05.meung.app/live/{stream_id}.m3u8`
  - `https://live05.miekgo.app/live/{stream_id}.m3u8`
- Probe ba candidate trước HTTP discovery và Chromium.
- Candidate registry trực tiếp không bị ép Referer/Origin trang Cola vì mẫu đầu ra tham chiếu dùng URL CDN trực tiếp.
- Có ít nhất một tuyến verified thì trả kết quả nhanh.
- Cả ba tuyến lỗi thì xóa candidate fast khỏi map và chạy đầy đủ cơ chế cũ.
- Khi HTTP-first/Chromium bắt được URL `meung/miekgo` đã verified, scanner tự rút stream ID và cập nhật registry.
- Dùng cùng rào chắn `-150/+15` cho fast registry; card/Chromium vẫn theo `-150/+180`.

## 5. Workflow và state

- Workflow: `Quet 6 nguon v4.4.27 - Fast registry pilot Xoi Lac ColaTV`.
- Cron giữ nguyên 30 phút.
- `cancel-in-progress: false`.
- Xôi Lạc vẫn quét tối đa 3 trận song song.
- Hai file registry được đưa vào artifact debug và commit lại khi scanner tự học mapping mới:
  - `xoilac_channel_registry.json`
  - `colatv_channel_registry.json`
- Audit workflow chỉ cho phép Xôi Lạc unsigned `live2` khi classification là `verified_unsigned` và playability là `verified`.

## 6. Phạm vi không thay đổi

Các file sau giống byte-for-byte v4.4.26:

- `sources/chuoichien.py`
- `sources/luongson.py`
- `sources/gavang.py`
- `sources/phaohoa.py`
- `sources/hybrid_support.py`

`merger.py` chỉ đổi build tag; chính sách verified-only/last-known-good không thay đổi.

## 7. Kiểm thử tĩnh và unit test

- Python compile: 24/24 file đạt.
- Unit test: 137/137 đạt.
- Release consistency: 9/9 đạt, kể cả chế độ không có `RELEASE_MANIFEST.json`.
- Workflow YAML: parse đạt.
- Bash workflow: 5/5 block đạt `bash -n`.
- JavaScript Playwright trích từ `evaluate/evaluate_all`: 45/45 block đạt `node --check`.
- Năm adapter không thuộc pilot và `hybrid_support.py` đã đối chiếu SHA-256, không đổi byte.
- Full ZIP giải nén độc lập: 137/137 test đạt.
- Changed-only chồng lên đúng Full Source v4.4.26: 137/137 test đạt.
- Hai ZIP đạt `zip -T`; không chứa `__pycache__` hoặc `.pyc`.

Test mới kiểm tra:

- Unsigned `live2` không còn bị loại theo hostname khi probe đúng FLV.
- Player type/8 vẫn bị loại.
- Probe Xôi Lạc chấp nhận HTTP 206 có FLV signature.
- Template `channelN` được dựng đúng.
- Cola registry dựng đủ ba URL từ stream ID.
- Fast registry không chạy cho trận còn hơn 15 phút.
- Chỉ URL đúng host/pattern Cola mới được dùng để tự học stream ID.

## 8. Giới hạn audit

Môi trường đóng gói hiện không phân giải được các host CDN `streambylivepulse`, `pro2cdnlive`, `meung` và `miekgo`, nên live smoke-test trả lỗi DNS trước khi kết nối. Vì vậy chưa thể cam kết:

- Tuyến Xôi Lạc `live2` hiện tại có trả FLV cho GitHub Runner hay không.
- ID Cola seed có còn đúng với BLV hiện tại hay không.
- Mức giảm thời gian thực tế trên GitHub.

Lượt GitHub đầu tiên cần xem các chỉ số/log:

- `Fast registry` hit/failed của Xôi Lạc và ColaTV.
- `verified_unsigned_live2`.
- Số trận fallback sang Chromium.
- Registry có tự cập nhật mapping mới hay không.
- Stream cuối trong `all_live.m3u` có phát đúng trận, đặc biệt khi BLV đổi trận sát giờ.
