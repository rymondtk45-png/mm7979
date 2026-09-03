# Hướng dẫn tích hợp

## Vì sao không phải 1 file drop-in duy nhất

`signals.py` và `app.py` **đã là file hoàn chỉnh**, thay thế/đè trực tiếp 100% được (tôi có đầy đủ bản gốc, kể cả `app.py` vừa được đưa thêm nên các fix mới nhất — debounce cảnh báo "tín hiệu xấu đi", Deribit skew — đã áp thẳng vào file luôn, không cần dán tay). Còn `data.py` tôi vẫn **không có bản gốc đầy đủ** (GitHub cắt bớt khi fetch, chỉ được ~700/1080 dòng), nên phần này vẫn phải dán tay theo `data_additions.py` kèm chỉ dẫn vị trí chính xác, để tránh ghi đè nhầm code bạn đang chạy.

## Thứ tự làm

1. **Thay thế `signals.py`** bằng file đính kèm — hoàn chỉnh, dùng ngay được:
   - Fix veto "weak consensus" sai logic (fire quá thường xuyên).
   - `suggested_sl_tp()`: **giữ nguyên** công thức gốc GitHub 0.8×/1.5×ATR (README bị coi là chưa cập nhật đúng nên không sửa theo README).
   - 4 module cũ (OI trend, whale/retail flow, BTC regime, funding spread liên sàn) + Value Area + VPIN.
   - **Mới**: `module_options_skew` — đọc `f["options_skew"]` (Deribit put/call skew), xem cảnh báo độ tin cậy bên dưới.

2. **Thay thế `app.py`** bằng file đính kèm — hoàn chỉnh, dùng ngay được. So với bản bạn gửi, đã sửa:
   - **Fix debounce `_check_signal_health`**: đây là nguyên nhân kèo nào bắn ra cũng bị cảnh báo "TIN HIEU XAU DI" gần như ngay lập tức — xem giải thích chi tiết ở mục riêng bên dưới.
   - Còn thiếu 2 dòng gọi `refresh_btc_snapshot()` / `refresh_deribit_snapshot()` trong `run_once()` — **chưa tự thêm được** vì 2 hàm này định nghĩa trong `data.py`, mà `data.py` bạn chưa dán các đoạn ở bước 3. Xem `app_patch.py` để dán 2 dòng import + gọi hàm này **sau khi** đã xong bước 3.

3. **Dán các đoạn trong `data_additions.py` vào `data.py`** — theo đúng thứ tự 1→9 ghi trong file đó (phần 9 mới: `fetch_deribit_option_skew` + `refresh_deribit_snapshot`). Mỗi khối ghi rõ "VI TRI DAN". Phần 3 (`compute_volume_profile`) là **thay thế toàn bộ hàm cũ**, các phần còn lại là **thêm mới**, không xóa gì.

4. **Thêm 2 field vào `config.py`** (mục 8 trong `data_additions.py`):
   ```python
   whale_usd_threshold: float = float(os.getenv("WHALE_USD_THRESHOLD", 50000))
   deribit_skew_cache_seconds: int = field(default_factory=lambda: _get_int("DERIBIT_SKEW_CACHE_SECONDS", 600))
   ```
   File `config.py` đính kèm **đã có sẵn field mới `signal_health_confirm_scans`** (mục fix debounce) — chỉ cần thêm tay 2 field trên vào `AppConfig` của bạn theo đúng pattern.

5. **Dán 2 dòng trong `app_patch.py`** vào `app.py` (import + gọi `refresh_btc_snapshot`/`refresh_deribit_snapshot`) — làm **sau** khi đã xong bước 3, vì 2 hàm này nằm trong `data.py`.

6. **Cập nhật `weights.json`**: mở file gốc của bạn, **giữ nguyên 12 giá trị cũ đã tune**, thêm 5 dòng mới ở cuối — file `weights.json` đính kèm đã dùng đúng 12 giá trị gốc bạn gửi (không phải placeholder nữa) + 5 dòng mới (`open_interest_trend`, `whale_retail_flow`, `btc_regime_filter`, `funding_spread_cross_exchange`, `options_skew`), copy trực tiếp được.

7. **(Tùy chọn) Thêm `similarity.py`** vào thư mục gốc — chỉ có ý nghĩa sau khi bot chạy vài tuần và tích lũy đủ log có kết quả thắng/thua.

## Vì sao kèo nào cũng dính cảnh báo "TIN HIEU XAU DI" gần như ngay lập tức

Đây **không phải bug logic** như 2 lỗi trước — code chạy đúng như thiết kế, nhưng cách thiết kế bị quá nhạy so với tốc độ quét:

- `_check_signal_health()` so lại hướng/veto MỚI NHẤT với lúc vào kèo, **mỗi vòng quét** (mặc định `poll_seconds=25s`).
- Điều kiện veto "weak consensus" (sau khi đã sửa đúng README) cần **chênh lệch phiếu long/short ≥ 3**. Nhưng nhiều module dùng để "vote" (`tape_flow`, `taker_buy_sell_ratio`, `order_book_imbalance`, `liquidation_impulse`...) là dữ liệu ngắn hạn/tick-level, rất dễ dao qua lại quanh ngưỡng `0.05` chỉ trong vài chục giây dù thị trường **không hề đổi hướng thật sự**.
- Kết quả: kèo vừa đạt đồng thuận đủ mạnh để bắn tín hiệu (vd 85.2 điểm), chỉ 1 vòng quét sau (~25s) phiếu bầu xê dịch nhẹ → rớt xuống dưới ngưỡng đồng thuận → veto lại → bị báo "xấu đi" ngay, dù chưa có gì thay đổi thật về mặt thị trường. Đúng như ảnh chụp màn hình bạn gửi (2 tin nhắn cùng phút).

**Cách sửa** (đã áp trong `app.py`/`config.py` đính kèm): thêm cơ chế đếm chuỗi — chỉ báo "xấu đi" khi kết quả xấu (veto/đảo chiều/neutral) xuất hiện **liên tục N vòng quét** (mặc định `signal_health_confirm_scans=3`, tức ~75s với `poll_seconds=25s`), thay vì 1 vòng lẻ tẻ là báo ngay. Nếu chỉ 1-2 vòng bị nhiễu ngắn hạn rồi quay lại bình thường thì streak bị reset về 0, không báo gì cả — im lặng như logic cũ vẫn làm khi "vẫn tốt". Bạn có thể chỉnh `SIGNAL_HEALTH_CONFIRM_SCANS` trong `.env` nếu 3 vòng vẫn thấy nhạy/chậm quá.

## Test trước khi deploy thật

```
python -m unittest tests.test_core -v
```

Test cũ vẫn phải pass nguyên vì tôi không đổi hành vi HTF veto/CVD/POC. Nếu muốn chắc hơn, viết thêm 1-2 test riêng cho `compute_composite` với input giả để kiểm tra veto "weak consensus" mới hoạt động đúng (vd: 2 module có ý kiến → phải veto; 5 module với 4-1 → không veto).

## Cảnh báo về độ tin cậy từng phần

| Phần | Độ tin cậy | Ghi chú |
|---|---|---|
| Fix veto "weak consensus" | Cao | Logic thuần Python, đối chiếu đúng README |
| `suggested_sl_tp` | Không đổi | Giữ nguyên bản GitHub gốc (0.8×/1.5×ATR); README bị coi là sai/chưa cập nhật nên không sửa theo README |
| Fix debounce cảnh báo "xấu đi" | Cao | Logic đếm chuỗi thuần Python, không phụ thuộc data nguồn ngoài |
| OI trend | Cao | Dùng endpoint Binance đã có sẵn trong bot (`fetch_open_interest`) |
| Whale/retail flow | Cao | Dùng tape đã có sẵn, không cần data nguồn mới |
| Value Area (VAH/VAL) | Cao | Thuần toán học trên volume-by-price đã có |
| BTC regime filter | Cao | Dùng lại `bias_from_klines`/`classify_regime` sẵn có |
| Funding spread liên sàn | Trung bình | Endpoint OKX/Bybit funding-rate tôi viết theo tài liệu public, **chưa chạy thử thực tế** — nên bật thử ở weight thấp và theo dõi log vài ngày trước khi tin tưởng |
| VPIN | Trung bình | Công thức chuẩn nhưng ngưỡng bucket_notional là ước lượng, cần backtest để tinh chỉnh |
| **Options skew (Deribit)** | **Thấp — chưa kiểm chứng với API thật** | Môi trường tôi chạy không có mạng ra ngoài nên **không tự gọi thử được** Deribit. Code viết đúng theo tài liệu public API (`get_book_summary_by_currency`, không cần key), nhưng: (1) là xấp xỉ THÔ 25-delta bằng cách chọn strike gần ±15% quanh spot proxy, không phải 25-delta chuẩn; (2) nếu Deribit đổi tên field (vd `mark_iv`) code sẽ âm thầm trả `None` qua try/except chứ không báo lỗi. **Bắt buộc**: chạy `_test_fetch_deribit_option_skew()` ở cuối phần 9 trong `data_additions.py` bằng tay, tự mắt xem số ra có hợp lý không, rồi theo dõi field `options_skew` trong `logs/features.jsonl` vài vòng trước khi tin. Vì lý do đó weight khởi điểm trong `weights.json` để rất thấp (`0.3`) — tự tăng dần khi thấy số liệu ổn định. |
| Similarity-based winrate | Cần thời gian | Vô dụng cho tới khi có đủ log lịch sử (vài tuần chạy thật) |

## Sau khi tích hợp xong

Chạy bot ở chế độ backtest/paper 1-2 tuần, xem `logs/features.jsonl` để đối chiếu `open_interest`, `whale_flow`, `vpin`, `btc_regime`, `funding_spread_cross` có ra số hợp lý không (không phải toàn `0.0`/`None`) trước khi tăng trọng số các module mới lên cao.
