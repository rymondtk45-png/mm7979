# bot_mm_fund

Bot **BAO TIN HIEU** long/short crypto futures theo tu duy market maker / quy:
vi the o vung volume cao (POC/HVN), book chi tin khi persist, tape phai confirm,
crowding (funding/basis/OI/LSR) chi la boi canh, veto khi khung lon mau thuan.

> **KHONG DAT LENH.** Bot chi phan tich va gui canh bao qua Telegram. Nguoi dung
> tu quyet dinh vao lenh tren san.

Xay moi hoan toan, khong copy code tu bot7979 — nhung giu lai cac tinh nang tuong
duong: sweep, funding extreme, order book imbalance (OBI), cross-exchange
divergence, composite scoring co trong so (`weights.json`), log JSONL, WS + REST
fallback, ranking top, cooldown, TTL, backtest doc feature log.

## 1. Cai dat

```bash
cd bot_mm_fund
python3 -m venv venv && source venv/bin/activate   # tuy chon
pip install -r requirements.txt
cp .env.example .env
# mo .env, dien TELEGRAM_BOT_TOKEN va TELEGRAM_CHAT_ID
```

Neu khong dung Telegram, dat `ENABLE_TELEGRAM=False` trong `.env` — bot van chay
va in canh bao ra log console (`[TELEGRAM-DISABLED] ...`).

## 2. Chay

```bash
python main.py
```

Bot se:
1. Lay vu tru USDT-M PERPETUAL dang TRADING tu Binance Futures.
2. Xay scan set (xem muc 3).
3. Mo 2 ket noi WebSocket (public: bookTicker + depth20; market: aggTrade +
   forceOrder + markPrice) cho CORE + toi da 15 cap dau scan set.
4. Vong lap moi `POLL_SECONDS` giay: danh gia tung symbol, ghi feature log,
   tinh composite score, gui alert Telegram neu du dieu kien, kiem tra
   SL/TP cua tin hieu dang active, log TOP 5.

## 3. Vu tru + `/coinstrong`

- Vu tru = toan bo symbol USDT-M PERPETUAL `status=TRADING` tren Binance
  Futures (`GET /fapi/v1/exchangeInfo` + `GET /fapi/v1/ticker/24hr`).
- Ten symbol va tin hieu **luon theo Binance**. Cac san khac (OKX, Bybit,
  BingX, KuCoin, Bitget, MEXC) chi dung de so gia (cross-exchange divergence)
  khi symbol ton tai ben do, va **chi cho CORE_SYMBOLS** de tranh spam request.
- `CORE_SYMBOLS` mac dinh `BTCUSDT,ETHUSDT,SOLUSDT` — luon nam trong scan set.

**`/coinstrong OFF`** (mac dinh): CORE + top volume cua vu tru, gioi han
`SCAN_LIMIT_OFF=25` cap, khong duoi theo alt "nong".

**`/coinstrong ON`**: them alt dang bien dong manh —
`|change24h| >= MIN_HOT_CHANGE_PCT` (mac dinh 3%) va
`quoteVolume >= MIN_QUOTE_VOLUME` (mac dinh 20,000,000 USDT), xep hang theo

```
hot_score = volume * (1 + |change24h|/10) * (1 + range_pct/20)
```

Gioi han `SCAN_LIMIT_ON=80` cap. Vu tru duoc lam moi moi
`UNIVERSE_REFRESH_SECONDS=60` giay.

Lenh Telegram:
```
/coinstrong on
/coinstrong off
/coinstrong        # xem trang thai hien tai
```
Bot tra loi: ON/OFF, so cap dang quet, 15 symbol dau.

## 4. WebSocket vs REST — tai sao khong quet depth/tape 400 cap moi vong

Mo WS cho ca vu tru (co the toi 400+ cap) se qua tai va khong can thiet — phan
lon alt khong du volume de co tin hieu MM/quy dang tin. Vi vay:

- **CORE_SYMBOLS**: du lieu day du — WS live (tape, book, liquidation) +
  cross-exchange 7 san + funding/basis/OI/LSR qua REST.
- **Toi da 15 cap dau scan set (khong tinh CORE)**: WS live cho tape/book,
  nhung **khong** cross-exchange (tiet kiem request).
- **Cac cap con lai trong scan set**: "light snapshot" — van lay kline
  15m/1h/4h qua REST vi HTF bat buoc phai co, cong voi 1 lan REST snapshot
  cho tape/depth moi vong; **khong** cross-exchange, **khong** WS.

Neu WS chet (mat ket noi, rate limit...), luong REST van chay binh thuong —
bot khong dung hoan toan, chi giam chat luong du lieu realtime cho cac cap
dang WS.

## 5. Luat khung thoi gian (KHONG SCALP)

- Thesis huong: **4h + 1h**. Location/profile/sweep: **15m + 1h**. Trigger toi
  thieu: **15m**. Confirm live: tape + book + liquidation realtime.
- **Cam** dung nen 1m/5m de xac dinh huong tin hieu (chi dung cho CVD ngan han
  mang tinh confirm).
- Bias tung khung = so gia dong hien tai voi gia dong N nen truoc: 15m (N=20),
  1h (N=12), 4h (N=12).
- **1h va 4h nguoc chieu nhau** → toan bo tin hieu bi veto, direction=neutral,
  ly do `"veto: HTF conflict"`.
- **`REQUIRE_1H_ALIGN=true`** (mac dinh): chi alert khi 15m VA 1h cung huong.
  Neu 15m di truoc mot minh (1h con neutral hoac nguoc) → khong alert.
- **`REQUIRE_4H_ALIGN=false`** (mac dinh): neu bat, bat buoc 4h cung huong
  voi 15m/1h moi duoc alert.
- Neu 4h cung huong voi tin hieu → nhan score voi **1.15**.
- Sweep: pha vo high/low cua **20 nen 15m** hoac **12 nen 1h** truoc do.
- SL/TP goi y tu **ATR15m**: SL = entry ∓ 0.8×ATR, TP = entry ± 1.5×ATR.
- Regime (accumulation / trending / high_volatility) lay tu khung **1h**.

## 6. Composite score + veto

`weights.json` chua trong so 12 module (volume_profile, tape_flow, absorption,
liquidation_impulse, funding_extreme, persistent_book, liquidity_sweep,
basis_spread, taker_buy_sell_ratio, long_short_ratio, order_book_imbalance,
cross_exchange_divergence). Moi module tra ve gia tri co dau trong [-1, 1]
(duong = nghieng long, am = nghieng short).

Thu tu tinh:
1. Bias 15m/1h/4h → ap luat khung o muc 5. Fail → dung, neutral.
2. `score = sum(module_score * weight)`, chuan hoa ve 0–100 theo bien do.
3. 4h aligned → nhan 1.15.
4. `spoof_score > 0.6` (proxy tu pull_ratio_3s cua order book) → nhan 0.75.
5. So phieu long/short tu cac module: neu chenh lech `<= 1` → veto "mixed".
6. Sweep chi duoc tinh diem khi co tape confirm (cum >=3 lenh lon cung phia
   trong 30s, cung huong voi sweep).
7. Cross-exchange divergence chi la boi canh (trong so thap 0.5), khong tu
   gate huong mot minh.
8. `ENABLE_MARKET_INTEL_SCORING=false` → chi dung 4 module "co dien" kieu
   bot7979 (volume_profile, tape_flow, liquidation_impulse, funding_extreme)
   nhung **van giu nguyen luat khung HTF** o muc 5.

## 7. Heuristic iceberg/spoof va absorption (proxy, khong phai su that tuyet doi)

- **spoof_score** = ty le volume top-of-book bi rut trong 3 giay gan nhat
  (`pull_ratio_3s`). Diem cao → nghi ngo lenh ao/spoof → chi **giam trong so**
  (nhan 0.75) tin hieu hien tai, **khong** tu dao chieu huong mot minh.
- **absorption**: CVD di nguoc huong gia (vd CVD am nhung gia khong giam) VA
  book duy tri on dinh (`persist_score >= 0.5`) → xem nhu mot phia dang bi
  "hap thu", nghieng ve huong con lai.
- Day la **proxy** dua tren du lieu public (khong co full order-by-order
  data), dung de loc/giam trong so, khong phai tin hieu doc lap tuyet doi.

## 8. File

```
bot_mm_fund/
  README.md
  requirements.txt
  .env.example
  weights.json
  main.py              # entry point
  backtest.py           # doc logs/features.jsonl, tai tinh composite, uoc luong winrate
  src/
    config.py           # AppConfig (.env), load_weights, append_jsonl, logger
    data.py              # UniverseManager, REST 7 san, StreamHub (2 WS),
                          # TradeTape, LocalBook, VolumeProfile, LiquidationTape,
                          # iceberg/spoof/absorption proxy, build_features
    signals.py            # module score + HTF veto + composite + ranking
    app.py                 # TelegramBot + SignalEngine (vong lap chinh)
  tests/
    test_core.py           # CVD, imbalance, POC, HTF veto, composite co huong
```

Python 3.12. **Khong** dung numpy/pandas — chi `requests`, `python-dotenv`,
`websocket-client` va thu vien chuan (`statistics`, `collections`, ...).

## 9. Chay test

```bash
python -m unittest tests.test_core -v
```

Cac bai test:
- CVD > 0 khi co nhieu lenh mua chu dong (buy aggressor).
- Order book imbalance > 0 khi ben bid day hon.
- POC (Point of Control) tinh dung — bucket co nhieu volume nhat.
- Composite luon co `direction` hop le (long/short/neutral).
- 1h long + 4h short → neutral, ly do `"veto: HTF conflict"`.
- 15m long ma 1h short, `REQUIRE_1H_ALIGN=true` → khong alert (veto), neutral.

## 10. Backtest (xap xi, ngoai tuyen)

```bash
python backtest.py [duong_dan/features.jsonl]
```

Doc lai `logs/features.jsonl` (do `main.py` ghi khi chay live), tai tinh
composite score cho tung dong bang logic hien hanh trong `src/signals.py`,
roi so gia cac dong log tiep theo cua CUNG symbol voi SL/TP goi y tu ATR15m de
uoc luong thang/thua. Day la cong cu tham khao, **khong** phai backtest
tick-by-tick chinh xac va **khong** ket noi san giao dich.

## 11. Luu y quan trong

- Bot **khong** tu dat lenh, **khong** ket noi API key giao dich — chi doc du
  lieu public va gui tin nhan Telegram.
- Moi nguong (THRESHOLD, cooldown, weights...) can duoc nguoi dung tu backtest
  va tinh chinh theo khau vi rui ro rieng truoc khi dung that.
- Day la cong cu ho tro phan tich, khong phai loi khuyen dau tu.
