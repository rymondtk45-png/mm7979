"""
data_additions.py
=================
KHONG chay file nay truc tiep. Day la tap hop cac ham/class MOI can duoc DAN
(copy-paste) vao dung vi tri trong data.py cua ban - xem chi dan "VI TRI DAN"
o dau moi khoi. Toi khong lay duoc toan bo data.py goc (GitHub cat bot noi
dung khi fetch) nen khong the tu dong merge an toan 100%; cach nay dam bao
khong ghi de nham code ban dang co.

Tom tat cac phan bo sung:
  1. MarketContext: them oi_history (nhu funding_history) + method oi_zscore()
     + btc_snapshot (dict) de luu boi canh BTC dung chung moi vong quet.
  2. TradeTape: them method whale_cvd() tach dong lenh whale/retail.
  3. compute_volume_profile(): them tinh Value Area (VAH/VAL).
  4. compute_vpin(): ham thuan tuy tinh do doc hai dong lenh tu tape.
  5. refresh_btc_snapshot(): ham cap nhat ctx.btc_snapshot 1 lan/vong quet.
  6. Cross-exchange funding (OKX, Bybit) best-effort de tinh funding_spread_cross.
  7. Doan can them vao cuoi build_features() de gan cac field moi vao dict
     tra ve (open_interest, price_change_15m_pct, whale_flow, btc_regime,
     funding_spread_cross, vpin, volume_profile.vah/val).
  9. Options skew Deribit (put/call IV, xem canh bao do tin cay o dau phan 9)
     + refresh_deribit_snapshot() cap nhat ctx.deribit_snapshot.

requirements.txt: KHONG can them thu vien nao moi (van chi statistics/collections
+ requests/websocket-client nhu ban dang co). Phan 9 chi dung datetime (chuan
lib, khong can cai them).
"""

# ============================================================================
# 1) VI TRI DAN: trong class MarketContext, ngay ben trong __init__ (them 2
#    dong sau dong `self.funding_history: Dict[str, Deque[float]] = {}`),
#    va them method oi_zscore() ngay sau method funding_zscore() da co.
# ============================================================================
"""
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.tapes: Dict[str, TradeTape] = {}
        self.books: Dict[str, LocalBook] = {}
        self.liq = LiquidationTape()
        self.funding_history: Dict[str, Deque[float]] = {}
        self.oi_history: Dict[str, Deque[float]] = {}          # <-- THEM DONG NAY
        self.btc_snapshot: dict = {}                            # <-- THEM DONG NAY
        self.deribit_snapshot: dict = {}                        # <-- THEM DONG NAY (phan 9)
        self.stream: Optional[StreamHub] = None

    def ensure_symbol(self, symbol: str) -> None:
        if symbol not in self.tapes:
            self.tapes[symbol] = TradeTape(self.cfg)
        if symbol not in self.books:
            self.books[symbol] = LocalBook(self.cfg)
        if symbol not in self.funding_history:
            self.funding_history[symbol] = deque(maxlen=200)
        if symbol not in self.oi_history:                       # <-- THEM 2 DONG NAY
            self.oi_history[symbol] = deque(maxlen=200)          # <-- (trong ensure_symbol)
"""


def _oi_zscore_method_to_paste_into_MarketContext():
    """
    VI TRI DAN: ngay sau method funding_zscore() trong class MarketContext
    (giu nguyen indent 4 space cho khop trong class khi dan).
    """
    # --- BAT DAU DOAN DAN (bo indent cua ham nay, dan y nhu duoi day) ---
    code = '''
    def oi_zscore(self, symbol: str, current_oi: float) -> Tuple[float, float]:
        """Tra ve (z_score, pct_change_from_prev). Goi 1 lan/vong quet/symbol,
        tu tich luy lich su OI (khong can REST rieng, dung chung fetch_open_interest
        dang co san)."""
        hist = self.oi_history.setdefault(symbol, deque(maxlen=200))
        prev = hist[-1] if hist else None
        hist.append(current_oi)
        if len(hist) < 5:
            return 0.0, 0.0
        mean = statistics.mean(hist)
        try:
            stdev = statistics.stdev(hist)
        except statistics.StatisticsError:
            stdev = 0.0
        z = (current_oi - mean) / stdev if stdev else 0.0
        pct_change = ((current_oi - prev) / prev) if prev else 0.0
        return z, pct_change
'''
    return code
    # --- KET THUC DOAN DAN ---


# ============================================================================
# 2) VI TRI DAN: trong class TradeTape, ngay sau method large_print_cluster().
# ============================================================================
def _whale_cvd_method_to_paste_into_TradeTape():
    code = '''
    def whale_cvd(self, whale_usd_threshold: float = 50_000.0,
                  window_seconds: int = 300) -> dict:
        """CVD rieng cho lenh lon (block/whale, notional >= threshold) va lenh
        nho (retail) trong window_seconds gan nhat. whale_ratio = ty trong
        volume (theo USD) den tu lenh lon trong ca cua so - ratio thap nghia
        la mau qua nho, khong nen tin tuong signal nay."""
        with self._lock:
            trades = list(self.trades)
        if not trades:
            return {"whale_cvd": 0.0, "retail_cvd": 0.0, "whale_ratio": 0.0}
        now = trades[-1]["ts"]
        cutoff = now - window_seconds * 1000
        window = [t for t in trades if t["ts"] >= cutoff]
        if not window:
            return {"whale_cvd": 0.0, "retail_cvd": 0.0, "whale_ratio": 0.0}
        whale = [t for t in window if t["price"] * t["qty"] >= whale_usd_threshold]
        retail = [t for t in window if t["price"] * t["qty"] < whale_usd_threshold]
        whale_cvd_val = compute_cvd(whale)
        retail_cvd_val = compute_cvd(retail)
        total_notional = sum(t["price"] * t["qty"] for t in window)
        whale_notional = sum(t["price"] * t["qty"] for t in whale)
        whale_ratio = whale_notional / total_notional if total_notional else 0.0
        return {"whale_cvd": whale_cvd_val, "retail_cvd": retail_cvd_val, "whale_ratio": whale_ratio}
'''
    return code


# ============================================================================
# 3) VI TRI DAN: THAY THE toan bo ham compute_volume_profile() hien co bang
#    ban duoi day (them tinh Value Area VAH/VAL, giu nguyen moi thu khac).
#    Day la ham module-level (khong nam trong class nao), dan luon ca dinh
#    nghia ham, khong can indent them.
# ============================================================================
def compute_volume_profile(trades, atr15m: float, buckets: int = 40,
                            fallback_klines=None) -> dict:
    """
    Bucket width = atr15m / 40 (theo spec 'bucket ATR15m/40').
    Neu tape khong du (trades rong), dung fallback_klines (15m/1h) de xay
    volume-by-price xap xi tu close cua tung nen.

    Tra ve poc, hvn (list), lvn (list), delta_at_poc, distance_to_poc,
    va MOI: vah, val (Value Area High/Low - vung chua 70% volume quanh POC).
    """
    bucket_width = atr15m / 40.0 if atr15m > 0 else 0.0
    vol_by_bucket = {}
    buy_by_bucket = {}
    sell_by_bucket = {}
    last_price = None

    def _bucket_of(price: float) -> int:
        if bucket_width <= 0:
            return 0
        return int(round(price / bucket_width))

    if trades:
        for t in trades:
            price = float(t.get("price", 0.0))
            qty = float(t.get("qty", 0.0))
            last_price = price
            b = _bucket_of(price)
            vol_by_bucket[b] = vol_by_bucket.get(b, 0.0) + qty
            if t.get("isBuyerMaker"):
                sell_by_bucket[b] = sell_by_bucket.get(b, 0.0) + qty
            else:
                buy_by_bucket[b] = buy_by_bucket.get(b, 0.0) + qty
    elif fallback_klines:
        for k in fallback_klines:
            price = k["close"]
            qty = k.get("volume", 0.0)
            last_price = price
            b = _bucket_of(price)
            vol_by_bucket[b] = vol_by_bucket.get(b, 0.0) + qty
            buy_vol = k.get("taker_buy_base", qty / 2.0)
            buy_by_bucket[b] = buy_by_bucket.get(b, 0.0) + buy_vol
            sell_by_bucket[b] = sell_by_bucket.get(b, 0.0) + max(qty - buy_vol, 0.0)

    if not vol_by_bucket:
        return {
            "poc": last_price or 0.0, "hvn": [], "lvn": [],
            "delta_at_poc": 0.0, "distance_to_poc": 0.0,
            "vah": None, "val": None,
        }

    poc_bucket = max(vol_by_bucket, key=vol_by_bucket.get)
    poc_vol = vol_by_bucket[poc_bucket]
    poc_price = poc_bucket * bucket_width if bucket_width > 0 else (last_price or 0.0)
    hvn = [b * bucket_width for b, v in vol_by_bucket.items() if v >= 0.7 * poc_vol]
    lvn = [b * bucket_width for b, v in vol_by_bucket.items() if v <= 0.1 * poc_vol]
    delta_at_poc = buy_by_bucket.get(poc_bucket, 0.0) - sell_by_bucket.get(poc_bucket, 0.0)

    distance_to_poc = 0.0
    if last_price and poc_price:
        distance_to_poc = (last_price - poc_price) / poc_price

    # --- MOI: Value Area 70% volume, mo rong tu POC ra 2 ben ---
    total_vol = sum(vol_by_bucket.values())
    va_target = total_vol * 0.7
    sorted_buckets = sorted(vol_by_bucket.items(), key=lambda kv: kv[1], reverse=True)
    va_buckets = set()
    acc = 0.0
    for b, v in sorted_buckets:
        va_buckets.add(b)
        acc += v
        if acc >= va_target:
            break
    vah = max(va_buckets) * bucket_width if va_buckets and bucket_width > 0 else poc_price
    val = min(va_buckets) * bucket_width if va_buckets and bucket_width > 0 else poc_price

    return {
        "poc": poc_price, "hvn": sorted(hvn), "lvn": sorted(lvn),
        "delta_at_poc": delta_at_poc, "distance_to_poc": distance_to_poc,
        "vah": vah, "val": val,
    }


# ============================================================================
# 4) VI TRI DAN: ham moi, dat ngay duoi compute_volume_profile() (module-level,
#    khong nam trong class nao).
# ============================================================================
def compute_vpin(trades, bucket_notional_usd: float, n_buckets: int = 20) -> float:
    """
    VPIN xap xi (Volume-Synchronized Probability of Informed Trading), khong
    can data ngoai - chi dung chinh tape dang co. Y tuong: chia dong lenh
    thanh cac 'bucket' co KHOI LUONG (theo USD) bang nhau thay vi theo thoi
    gian, roi do mat can bang buy/sell trong tung bucket. Mat can bang cao va
    dai dang (nhieu bucket lien tiep) = dong lenh dang bi chi phoi boi 1 phia
    "biet truoc thong tin" (thuong xuat hien truoc bien dong manh).

    trades: list {'price','qty','isBuyerMaker'} theo thu tu thoi gian tang dan.
    bucket_notional_usd: kich thuoc 1 bucket tinh theo USD (vd: dat = trung
    binh 1 phut giao dich cua symbol do, tuy chinh qua config neu can).
    Tra ve 0..1, cang cao cang "doc hai".
    """
    if not trades or bucket_notional_usd <= 0:
        return 0.0
    buckets = []
    buy_acc = 0.0
    sell_acc = 0.0
    acc_notional = 0.0
    for t in trades:
        price = t.get("price", 0.0)
        qty = t.get("qty", 0.0)
        notional = price * qty
        if t.get("isBuyerMaker"):
            sell_acc += notional
        else:
            buy_acc += notional
        acc_notional += notional
        if acc_notional >= bucket_notional_usd:
            total = buy_acc + sell_acc
            if total > 0:
                buckets.append(abs(buy_acc - sell_acc) / total)
            buy_acc = sell_acc = acc_notional = 0.0
    if not buckets:
        return 0.0
    window = buckets[-n_buckets:]
    return sum(window) / len(window)


# ============================================================================
# 5) VI TRI DAN: ham moi dat gan UniverseManager/fetch_klines (module-level).
#    Goi 1 lan dau moi vong quet trong app.py (xem app_patch.py).
# ============================================================================
def refresh_btc_snapshot(ctx, cfg, btc_symbol: str = "BTCUSDT") -> None:
    """Cap nhat ctx.btc_snapshot 1 lan/vong quet - dung lam boi canh macro cho
    module_btc_regime_filter (signals.py) o moi alt trong scan set. Chi 3 REST
    call nhe (2 cai co cache TTL san), khong dang ke ve rate limit."""
    k15 = fetch_klines(btc_symbol, "15m", limit=40)
    k1h = fetch_klines(btc_symbol, "1h", limit=30, cache_ttl=cfg.htf_1h_cache_seconds)
    k4h = fetch_klines(btc_symbol, "4h", limit=20, cache_ttl=cfg.htf_4h_cache_seconds)
    ctx.btc_snapshot = {
        "bias_1h": bias_from_klines(k1h, 12),
        "bias_4h": bias_from_klines(k4h, 12),
        "regime": classify_regime(k1h),
    }


# ============================================================================
# 6) VI TRI DAN: them vao CROSS_EXCHANGE_ENDPOINTS mot dict song song cho
#    funding, va 1 ham fetch_cross_exchange_funding() ngay duoi
#    fetch_cross_exchange_price() da co. Best-effort - CHi OKX va Bybit co
#    endpoint funding public don gian, du de tinh spread tham khao.
# ============================================================================
CROSS_FUNDING_ENDPOINTS = {
    "OKX": "https://www.okx.com/api/v5/public/funding-rate?instId={inst}",
    "BYBIT": "https://api.bybit.com/v5/market/funding/history?category=linear&symbol={sym}&limit=1",
}


def fetch_cross_exchange_funding(exchange: str, symbol: str):
    """Best-effort, tra ve None neu loi (khong lam sap vong quet). Chi goi cho
    CORE_SYMBOLS (giong cach cross_exchange_divergence gia dang lam)."""
    base = symbol.replace("USDT", "")
    try:
        if exchange == "OKX":
            url = CROSS_FUNDING_ENDPOINTS["OKX"].format(inst=f"{base}-USDT-SWAP")
            r = safe_get(url)
            return float(r["data"][0]["fundingRate"]) if r and r.get("data") else None
        if exchange == "BYBIT":
            url = CROSS_FUNDING_ENDPOINTS["BYBIT"].format(sym=symbol)
            r = safe_get(url)
            lst = r["result"]["list"] if r and r.get("result") else []
            return float(lst[0]["fundingRate"]) if lst else None
    except Exception as e:  # noqa: BLE001
        log.debug("cross-funding %s %s loi: %s", exchange, symbol, e)
        return None
    return None


# ============================================================================
# 7) VI TRI DAN: BEN TRONG ham build_features(), NGAY TRUOC dong `return
#    features` (ten bien co the khac trong ban goc cua ban - tim dong return
#    cuoi cung cua ham nay va dan doan duoi day ngay phia truoc no). Bien
#    `symbol`, `cfg`, `ctx`, `is_core`, `klines_15m`, `atr15m`, `last_price`,
#    `tape` (= ctx.tapes[symbol]) da ton tai san trong ham theo cau truc goc.
# ============================================================================
def _snippet_to_paste_before_return_in_build_features():
    code = '''
    # --- MOI: Open Interest trend (dung chung cho moi symbol, weight=1) ---
    oi_value = fetch_open_interest(symbol, cache_ttl=cfg.htf_1h_cache_seconds if not is_core else 0.0)
    oi_z, oi_pct_change = (0.0, 0.0)
    if oi_value is not None:
        oi_z, oi_pct_change = ctx.oi_zscore(symbol, oi_value)
    features["open_interest"] = {"value": oi_value, "pct_change": oi_pct_change, "z": oi_z}

    # --- MOI: % thay doi gia 15m gan nhat (dung cho module_open_interest_trend) ---
    if len(klines_15m) >= 2:
        prev_close = klines_15m[-2]["close"]
        features["price_change_15m_pct"] = (
            (last_price - prev_close) / prev_close if prev_close else 0.0)
    else:
        features["price_change_15m_pct"] = 0.0

    # --- MOI: whale vs retail order flow (chi co y nghia khi is_ws_tracked,
    # dung chung tape da co, khong can REST them) ---
    features["whale_flow"] = tape.whale_cvd(
        whale_usd_threshold=getattr(cfg, "whale_usd_threshold", 50_000.0))

    # --- MOI: VPIN xap xi tu chinh tape (bucket ~1 phut giao dich trung binh) ---
    trades_snapshot = tape.snapshot() if is_ws_tracked else trades
    avg_trade_notional = 0.0
    if trades_snapshot:
        avg_trade_notional = sum(t["price"] * t["qty"] for t in trades_snapshot) / len(trades_snapshot)
    bucket_notional = max(avg_trade_notional * 50, 5_000.0)  # ~50 lenh trung binh / bucket
    features["vpin"] = compute_vpin(trades_snapshot, bucket_notional_usd=bucket_notional)

    # --- MOI: BTC regime filter (ctx.btc_snapshot duoc app.py cap nhat 1
    # lan/vong quet TRUOC khi goi build_features cho tung symbol) ---
    features["btc_regime"] = dict(getattr(ctx, "btc_snapshot", {}))

    # --- MOI: funding spread lien san (chi co du lieu cho CORE_SYMBOLS, giong
    # cach cross_exchange_divergence hien tai dang gioi han) ---
    features["funding_spread_cross"] = None
    if is_core:
        binance_funding = features.get("funding")  # da co san tu fetch_premium_index
        if binance_funding is not None:
            other_fundings = []
            for ex in ("OKX", "BYBIT"):
                fr = fetch_cross_exchange_funding(ex, symbol)
                if fr is not None:
                    other_fundings.append(fr)
            if other_fundings:
                avg_other = sum(other_fundings) / len(other_fundings)
                features["funding_spread_cross"] = binance_funding - avg_other

    # --- MOI: gan vah/val vao volume_profile neu chua co (phong khi ban ghep
    # thu cong va compute_volume_profile() chua duoc thay o buoc 3) ---
    vp = features.get("volume_profile", {})
    if "vah" not in vp:
        vp["vah"] = None
        vp["val"] = None
        features["volume_profile"] = vp

    # --- MOI (phan 9): options skew Deribit, ctx.deribit_snapshot duoc
    # app.py cap nhat 1 lan/vong quet TRUOC khi goi build_features (xem
    # refresh_deribit_snapshot trong app_patch.py) ---
    features["options_skew"] = getattr(ctx, "deribit_snapshot", {}).get("btc_skew")
'''
    return code


# ============================================================================
# 8) VI TRI DAN: trong config.py, class AppConfig - them 1 field moi (co gia
#    tri mac dinh, khong bat buoc nguoi dung phai sua .env):
#       whale_usd_threshold: float = float(os.getenv("WHALE_USD_THRESHOLD", 50000))
#    Neu AppConfig cua ban dung dataclass, them dong tuong tu cac field khac
#    (vd tape_window_seconds) dang co.
#    Them 1 field nua cho phan 9:
#       deribit_skew_cache_seconds: int = field(default_factory=lambda:
#           _get_int("DERIBIT_SKEW_CACHE_SECONDS", 600))
# ============================================================================


# ============================================================================
# 9) OPTIONS SKEW DERIBIT (put/call IV) - lam boi canh macro giong btc_regime.
#
# !!! CANH BAO DO TIN CAY - DOC TRUOC KHI BAT !!!
# Toi viet phan nay theo tai lieu public API cua Deribit (khong can API key -
# get_book_summary_by_currency la endpoint public), NHUNG moi truong toi dang
# chay KHONG co mang ra ngoai nen toi KHONG the tu goi thu de kiem chung field
# tra ve dung format nhu ky vong. Truoc khi bat trong weights.json:
#   1. Chay rieng doan test cuoi phan nay (test_fetch_deribit_option_skew) va
#      IN RA gia tri that, xem co hop ly khong (vd BTC thuong dao dong nho,
#      vai % IV, hiem khi vuot +-10 diem tru luc thi truong hoang loan that).
#   2. Cho bot chay vai vong, xem field "options_skew" trong
#      logs/features.jsonl co ra so # khong phai luon None/0.0.
#   3. Neu Deribit doi cau truc response (vd doi ten field "mark_iv"), ham
#      se fail am tham (tra ve None qua try/except) - KHONG lam sap bot,
#      nhung ban se khong biet neu khong tu kiem tra dinh ky.
#   4. Day la xap xi THO 25-delta bang cach chon strike gan +-15% quanh gia
#      (vi endpoint book_summary khong tra ve delta/greeks truc tiep), KHONG
#      phai 25-delta chuan nhu Bloomberg/Genesis Volatility hay dung - dung
#      de tham khao xu huong (skew nghieng put hay call), khong dung de tinh
#      gia option that.
#
# VI TRI DAN: dat 2 ham nay (fetch_deribit_option_skew, refresh_deribit_snapshot)
# va helper _parse_deribit_expiry ngay canh refresh_btc_snapshot() (module-level).
# Them "self.deribit_snapshot: dict = {}" vao MarketContext.__init__ (giong
# btc_snapshot o phan 1). Can `from datetime import datetime, timezone` o dau
# data.py neu chua co.
# ============================================================================
DERIBIT_BOOK_SUMMARY_URL = (
    "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
    "?currency={ccy}&kind=option"
)


def _parse_deribit_expiry(expiry_str: str):
    """'27DEC24' -> unix timestamp luc 08:00 UTC (gio dao han chuan Deribit).
    Tra ve None neu parse loi (format doi khac ky vong)."""
    try:
        dt = datetime.strptime(expiry_str, "%d%b%y").replace(
            hour=8, minute=0, second=0, tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return None


def fetch_deribit_option_skew(currency: str = "BTC") -> Optional[float]:
    """
    Uoc luong put/call IV skew tu Deribit, xap xi 25-delta bang cach chon
    strike gan spot*1.15 (call, "OTM upside") va spot*0.85 (put, "OTM
    downside") trong ky han gan nhat con 5-20 ngay (tranh 0DTE qua nhieu; tranh
    ky han qua xa it thanh khoan). Spot duoc uoc luong tho bang median cac
    strike dang niem yet trong ky han do (book_summary khong luon co
    underlying_price on dinh cho moi dong).

    Tra ve signed float:
      duong = call IV > put IV -> thi truong dinh gia upside dat hon -> hoi
              nghieng "tham lam"/bullish.
      am    = put IV > call IV (thuong xuyen hon trong crypto) -> thi truong
              dinh gia downside dat hon -> hoi nghieng "so hai"/risk-off.
    Tra ve None neu loi mang, currency khong co option tren Deribit, hoac
    khong du du lieu (< 4 dong hop le trong ky han da chon).
    """
    try:
        data = safe_get(DERIBIT_BOOK_SUMMARY_URL.format(ccy=currency))
        rows = data.get("result") if data else None
        if not rows:
            return None
    except Exception as e:  # noqa: BLE001
        log.debug("Deribit book summary %s loi: %s", currency, e)
        return None

    now = time.time()
    parsed = []
    for row in rows:
        name = row.get("instrument_name", "")  # vd "BTC-27DEC24-60000-C"
        parts = name.split("-")
        if len(parts) != 4:
            continue
        _, expiry_str, strike_str, opt_type = parts
        expiry_ts = _parse_deribit_expiry(expiry_str)
        if expiry_ts is None:
            continue
        try:
            strike = float(strike_str)
        except ValueError:
            continue
        days_left = (expiry_ts - now) / 86400.0
        if not (5.0 <= days_left <= 20.0):
            continue
        iv = row.get("mark_iv")
        if iv is None:
            continue
        parsed.append({"strike": strike, "type": opt_type, "iv": iv})

    if len(parsed) < 4:
        return None

    strikes_all = sorted(p["strike"] for p in parsed)
    spot_proxy = strikes_all[len(strikes_all) // 2]

    calls = [p for p in parsed if p["type"] == "C"]
    puts = [p for p in parsed if p["type"] == "P"]
    if not calls or not puts:
        return None

    call_otm = min(calls, key=lambda p: abs(p["strike"] - spot_proxy * 1.15))
    put_otm = min(puts, key=lambda p: abs(p["strike"] - spot_proxy * 0.85))

    skew_points = call_otm["iv"] - put_otm["iv"]  # IV tinh theo % (vd 65.0 = 65%)
    return max(-1.0, min(1.0, skew_points / 15.0))  # +-15 diem IV = bien do cuc dai


def refresh_deribit_snapshot(ctx, cfg) -> None:
    """Cap nhat ctx.deribit_snapshot toi da 1 lan moi
    ~cfg.deribit_skew_cache_seconds giay (mac dinh 600s = 10 phut - IV khong
    doi nhanh nhu tape/orderbook nen KHONG can goi lai moi vong quet 25s, du
    ham nay duoc goi moi vong - tu bo qua neu chua het han cache ben trong)."""
    last_ts = getattr(ctx, "deribit_snapshot", {}).get("ts", 0)
    ttl = getattr(cfg, "deribit_skew_cache_seconds", 600)
    if time.time() - last_ts < ttl:
        return
    skew = fetch_deribit_option_skew("BTC")
    ctx.deribit_snapshot = {"btc_skew": skew, "ts": time.time()}


def _test_fetch_deribit_option_skew():
    """KHONG phai unit test tu dong (goi mang that ra ngoai) - chay rieng
    bang tay 1 lan (python -c "from data_additions import _test...; ...()")
    de TU MAT KIEM TRA ket qua truoc khi tin tuong module nay, dung theo
    canh bao do tin cay o dau phan 9."""
    skew = fetch_deribit_option_skew("BTC")
    print(f"BTC options skew (raw signed [-1,1]): {skew}")
    if skew is None:
        print("-> None: kiem tra lai response tho cua Deribit (co the doi format).")
