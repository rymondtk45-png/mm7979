"""
data.py
Universe management, REST/WS data cho Binance USDT-M Futures (lead venue) +
gia tham chieu cross-exchange, tape, book, volume profile, liquidation,
iceberg/spoof/absorption proxy, positioning, regime.

Khong dat lenh. Chi doc du lieu public.
"""
from __future__ import annotations

import json
import math
import statistics
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

import requests

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None

from config import AppConfig, get_logger

log = get_logger("data")

FAPI = "https://fapi.binance.com"
WS_PUBLIC = "wss://fstream.binance.com/public/stream?streams="
WS_MARKET = "wss://fstream.binance.com/market/stream?streams="

CROSS_EXCHANGE_ENDPOINTS = {
    # best-effort public ticker endpoints, symbol format handled per-exchange
    "OKX": "https://www.okx.com/api/v5/market/ticker?instId={inst}",
    "BYBIT": "https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}",
    "BINGX": "https://open-api.bingx.com/openApi/swap/v2/quote/price?symbol={inst}",
    "KUCOIN": "https://api-futures.kucoin.com/api/v1/ticker?symbol={inst}",
    "BITGET": "https://api.bitget.com/api/v2/mix/market/ticker?symbol={sym}&productType=USDT-FUTURES",
    "MEXC": "https://contract.mexc.com/api/v1/contract/ticker?symbol={inst}",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "bot_mm_fund/1.0"})
    return s


SESSION = _session()


class WeightLimiter:
    """Token-bucket theo 'request weight' cua Binance (gioi han that: 2400/phut/IP).
    Chu dong sleep truoc khi vuot ngan sach, thay vi de dinh 429/418 roi moi xu ly.
    Thread-safe, dung chung cho moi worker.

    CHI danh cho request toi Binance (fapi.binance.com). Cac san khac (cross-
    exchange) dung safe_get_external() rieng, KHONG di qua limiter nay, vi
    ngan sach 2400/phut la cua Binance, khong lien quan gi den OKX/Bybit/...
    Gop chung se lam Binance bi bop toc do gia tao khi bat CROSS_EXCHANGE_ALL
    cho hang tram cap."""

    def __init__(self, budget_per_min: int = 2000):
        self.budget = max(budget_per_min, 100)
        self.used = 0
        self.window_start = time.time()
        self._lock = threading.Lock()

    def set_budget(self, budget_per_min: int) -> None:
        with self._lock:
            self.budget = max(budget_per_min, 100)

    def acquire(self, weight: int = 1) -> None:
        while True:
            with self._lock:
                now = time.time()
                if now - self.window_start >= 60:
                    self.window_start = now
                    self.used = 0
                if self.used + weight <= self.budget:
                    self.used += weight
                    return
                sleep_for = max(60 - (now - self.window_start), 0.05)
            time.sleep(min(sleep_for, 2.0))

    def penalize(self, seconds: float) -> None:
        """Goi khi dinh 429/418: coi nhu het ngan sach cho toi het cua so hien tai."""
        with self._lock:
            self.used = self.budget
            self.window_start = time.time() - 60 + seconds


WEIGHT_LIMITER = WeightLimiter(2000)


def init_rate_limiter(cfg: "AppConfig") -> None:
    WEIGHT_LIMITER.set_budget(cfg.weight_budget_per_min)


class TTLCache:
    """Cache don gian keyed theo (symbol, kind), moi entry co TTL rieng luc ghi."""

    def __init__(self):
        self._store: Dict[str, Tuple[float, float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            ts, ttl, value = entry
            if time.time() - ts > ttl:
                return None
            return value

    def set(self, key: str, value, ttl: float) -> None:
        with self._lock:
            self._store[key] = (time.time(), ttl, value)


REST_CACHE = TTLCache()


def safe_get(url: str, params: dict = None, timeout: float = 5.0, weight: int = 1) -> Optional[dict]:
    """Dung cho Binance (fapi.binance.com) - co WEIGHT_LIMITER + retry + xu ly
    418/429 theo dung ngu canh rate-limit cua Binance."""
    for attempt in range(3):
        WEIGHT_LIMITER.acquire(weight)
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code in (418, 429):
                retry_after = float(r.headers.get("Retry-After", 5))
                log.warning("Rate limited (%s) tren %s, cho %.1fs", r.status_code, url, retry_after)
                WEIGHT_LIMITER.penalize(retry_after)
                time.sleep(retry_after)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("GET fail %s : %s", url, e)
            return None
    return None


def safe_get_external(url: str, params: dict = None, timeout: float = 3.0) -> Optional[dict]:
    """Danh cho cac san KHONG PHAI Binance (cross-exchange reference price).
    Khong dung WEIGHT_LIMITER (ngan sach do la cua Binance, khong lien quan),
    khong retry nhieu lan va timeout ngan hon - vi day chi la du lieu tham
    khao (trong so 0.5 trong composite), 1 san bi cham/loi khong duoc phep
    lam cham ca vong quet cua toan bo scan set."""
    try:
        r = SESSION.get(url, params=params, timeout=timeout)
        if r.status_code in (418, 429):
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.debug("external GET fail %s : %s", url, e)
        return None


# --------------------------------------------------------------------------
# Pure compute helpers (khong I/O -> de test)
# --------------------------------------------------------------------------

def compute_cvd(trades: List[dict]) -> float:
    """Cumulative volume delta. trade = {'qty': float, 'isBuyerMaker': bool}.
    isBuyerMaker=True nghia la taker la nguoi ban (sell aggressor) -> tru.
    isBuyerMaker=False nghia la taker mua (buy aggressor) -> cong.
    """
    cvd = 0.0
    for t in trades:
        qty = float(t.get("qty", 0.0))
        if t.get("isBuyerMaker"):
            cvd -= qty
        else:
            cvd += qty
    return cvd


def compute_imbalance(bids: List[Tuple[float, float]], asks: List[Tuple[float, float]],
                       levels: int = 20) -> float:
    """(bidVol - askVol) / (bidVol + askVol) tren N muc dau."""
    bid_vol = sum(q for _, q in bids[:levels])
    ask_vol = sum(q for _, q in asks[:levels])
    total = bid_vol + ask_vol
    if total <= 0:
        return 0.0
    return (bid_vol - ask_vol) / total


def compute_microprice(best_bid: float, best_bid_qty: float,
                        best_ask: float, best_ask_qty: float) -> float:
    total = best_bid_qty + best_ask_qty
    if total <= 0:
        return (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0.0
    return (best_bid * best_ask_qty + best_ask * best_bid_qty) / total


def compute_atr(klines: List[dict], period: int = 14) -> float:
    """klines: list dict co high, low, close (thu tu cu -> moi). Wilder-ish simple avg TR."""
    if len(klines) < 2:
        return 0.0
    trs = []
    prev_close = klines[0]["close"]
    for k in klines[1:]:
        high, low, close = k["high"], k["low"], k["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    if not trs:
        return 0.0
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window)


def bias_from_klines(klines: List[dict], n: int) -> str:
    """So close hien tai voi close N nen truoc. long/short/neutral."""
    if len(klines) < n + 1:
        return "neutral"
    now_close = klines[-1]["close"]
    past_close = klines[-1 - n]["close"]
    if past_close == 0:
        return "neutral"
    change = (now_close - past_close) / past_close
    if change > 0.0005:
        return "long"
    if change < -0.0005:
        return "short"
    return "neutral"


def compute_volume_profile(trades: List[dict], atr15m: float, buckets: int = 40,
                            fallback_klines: Optional[List[dict]] = None) -> dict:
    """
    Bucket width = atr15m / 40 (theo spec 'bucket ATR15m/40').
    Neu tape khong du (trades rong), dung fallback_klines (15m/1h) de xay
    volume-by-price xap xi tu close cua tung nen.
    Tra ve poc, hvn (list), lvn (list), delta_at_poc, distance_to_poc.
    """
    bucket_width = atr15m / 40.0 if atr15m > 0 else 0.0
    vol_by_bucket: Dict[int, float] = {}
    buy_by_bucket: Dict[int, float] = {}
    sell_by_bucket: Dict[int, float] = {}
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

    return {
        "poc": poc_price, "hvn": sorted(hvn), "lvn": sorted(lvn),
        "delta_at_poc": delta_at_poc, "distance_to_poc": distance_to_poc,
    }


def detect_sweep(klines_15m: List[dict], klines_1h: List[dict]) -> dict:
    """Sweep = pha vo high/low 20 nen 15m HOAC 12 nen 1h roi (proxy) dong lai gan."""
    result = {"swept": False, "side": None, "tf": None}
    if len(klines_15m) >= 21:
        window = klines_15m[-21:-1]
        last = klines_15m[-1]
        hi = max(k["high"] for k in window)
        lo = min(k["low"] for k in window)
        if last["high"] > hi:
            result = {"swept": True, "side": "short", "tf": "15m"}  # sweep high -> fade short
        elif last["low"] < lo:
            result = {"swept": True, "side": "long", "tf": "15m"}
    if not result["swept"] and len(klines_1h) >= 13:
        window = klines_1h[-13:-1]
        last = klines_1h[-1]
        hi = max(k["high"] for k in window)
        lo = min(k["low"] for k in window)
        if last["high"] > hi:
            result = {"swept": True, "side": "short", "tf": "1h"}
        elif last["low"] < lo:
            result = {"swept": True, "side": "long", "tf": "1h"}
    return result


def classify_regime(klines_1h: List[dict]) -> str:
    """accumulation / trending / high_volatility tu khung 1h."""
    if len(klines_1h) < 15:
        return "accumulation"
    atr = compute_atr(klines_1h, period=14)
    closes = [k["close"] for k in klines_1h[-15:]]
    avg_price = sum(closes) / len(closes)
    if avg_price <= 0:
        return "accumulation"
    atr_pct = atr / avg_price
    directional = abs(closes[-1] - closes[0]) / avg_price
    if atr_pct > 0.02:
        return "high_volatility"
    if directional > 0.015:
        return "trending"
    return "accumulation"


# --------------------------------------------------------------------------
# REST: Universe + klines + positioning (Binance USDT-M futures = lead venue)
# --------------------------------------------------------------------------

class UniverseManager:
    """Quan ly vu tru USDT-M PERPETUAL TRADING tren Binance Futures + /coinstrong."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.coinstrong = cfg.coinstrong_default
        self._lock = threading.Lock()
        self._last_refresh = 0.0
        self.symbols_info: Dict[str, dict] = {}
        self.tickers: Dict[str, dict] = {}
        self.scan_set: List[str] = list(cfg.core_symbols)

    def set_coinstrong(self, on: bool) -> None:
        with self._lock:
            self.coinstrong = on

    def fetch_exchange_info(self) -> Dict[str, dict]:
        data = safe_get(f"{FAPI}/fapi/v1/exchangeInfo")
        out = {}
        if not data:
            return out
        for s in data.get("symbols", []):
            if (s.get("status") == "TRADING" and s.get("contractType") == self.cfg.contract_type
                    and s.get("quoteAsset") == self.cfg.quote_asset):
                out[s["symbol"]] = s
        return out

    def fetch_24h_tickers(self) -> Dict[str, dict]:
        data = safe_get(f"{FAPI}/fapi/v1/ticker/24hr")
        out = {}
        if not data:
            return out
        for t in data:
            out[t["symbol"]] = t
        return out

    def refresh(self, force: bool = False) -> bool:
        now = time.time()
        if not force and (now - self._last_refresh) < self.cfg.universe_refresh_seconds:
            return False
        info = self.fetch_exchange_info()
        tick = self.fetch_24h_tickers()
        if not info or not tick:
            log.warning("Universe refresh loi, giu du lieu cu")
            return False
        with self._lock:
            self.symbols_info = info
            self.tickers = tick
            self.scan_set = self._build_scan_set()
            self._last_refresh = now
        return True

    def _build_scan_set(self) -> List[str]:
        valid = [s for s in self.symbols_info.keys() if s in self.tickers]

        def qvol(sym: str) -> float:
            try:
                return float(self.tickers[sym].get("quoteVolume", 0.0))
            except (TypeError, ValueError):
                return 0.0

        core = [s for s in self.cfg.core_symbols if s in valid]
        rest = [s for s in valid if s not in core]

        if not self.coinstrong:
            rest_sorted = sorted(rest, key=qvol, reverse=True)
            limit = max(self.cfg.scan_limit_off - len(core), 0)
            return core + rest_sorted[:limit]

        # coinstrong ON: them alt nong theo hot_score
        def change_pct(sym: str) -> float:
            try:
                return float(self.tickers[sym].get("priceChangePercent", 0.0))
            except (TypeError, ValueError):
                return 0.0

        def range_pct(sym: str) -> float:
            t = self.tickers[sym]
            try:
                hi, lo, last = float(t["highPrice"]), float(t["lowPrice"]), float(t["lastPrice"])
                return ((hi - lo) / last * 100.0) if last else 0.0
            except (KeyError, ValueError, ZeroDivisionError):
                return 0.0

        hot_candidates = []
        for s in rest:
            chg = change_pct(s)
            vol = qvol(s)
            rng = range_pct(s)
            if abs(chg) >= self.cfg.min_hot_change_pct and vol >= self.cfg.min_quote_volume:
                # hot_score: uu tien %change/range (do "nong" thuc su), volume
                # chi dung o thang LOG lam vai tro loc thanh khoan du dung (da
                # loc cung boi min_quote_volume o dieu kien tren) - KHONG de
                # volume tuyet doi (chenh lech hang tram/nghin lan giua cac
                # coin) lan at het tin hieu %change/range nhu cong thuc cu
                # (hot_score = vol * he_so_nho) tung lam.
                hot_score = abs(chg) * (1 + rng / 100.0) * math.log10(max(vol, 10.0))
                hot_candidates.append((s, hot_score))
        hot_candidates.sort(key=lambda x: x[1], reverse=True)

        top_volume = sorted(rest, key=qvol, reverse=True)
        limit = max(self.cfg.scan_limit_on - len(core), 0)
        merged: List[str] = []
        seen = set(core)
        for s, _ in hot_candidates:
            if s not in seen and len(merged) < limit:
                merged.append(s)
                seen.add(s)
        for s in top_volume:
            if s not in seen and len(merged) < limit:
                merged.append(s)
                seen.add(s)
        return core + merged

    def get_scan_set(self) -> List[str]:
        with self._lock:
            return list(self.scan_set)

    def ws_symbols(self, max_extra: int = None) -> List[str]:
        """CORE + toi da max_extra cap dau scan set (khong trung CORE).
        Neu max_extra=None: dung cfg.ws_cover_all de quyet dinh phu toan bo
        scan set (full model) hay chi 15 cap dau (nhe, tiet kiem WS/CPU)."""
        with self._lock:
            core = list(self.cfg.core_symbols)
            if max_extra is None:
                max_extra = len(self.scan_set) if self.cfg.ws_cover_all else 15
            extra = [s for s in self.scan_set if s not in core][:max_extra]
            return core + extra


def fetch_klines(symbol: str, interval: str, limit: int = 60, cache_ttl: float = 0.0) -> List[dict]:
    """weight=1 (limit<=100 tren fapi). cache_ttl>0 -> dung TTLCache (cho 1h/4h,
    khong can lay lai moi vong quet vi nen HTF khong doi nhanh)."""
    cache_key = f"klines:{symbol}:{interval}:{limit}"
    if cache_ttl > 0:
        cached = REST_CACHE.get(cache_key)
        if cached is not None:
            return cached
    raw = safe_get(f"{FAPI}/fapi/v1/klines", params={
        "symbol": symbol, "interval": interval, "limit": limit,
    }, weight=1)
    out = []
    if not raw:
        return out
    for k in raw:
        try:
            volume = float(k[5])
            taker_buy_base = float(k[9])
            out.append({
                "open_time": k[0], "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": volume,
                "close_time": k[6], "taker_buy_base": taker_buy_base,
            })
        except (IndexError, ValueError, TypeError):
            continue
    if cache_ttl > 0 and out:
        REST_CACHE.set(cache_key, out, cache_ttl)
    return out


def fetch_agg_trades(symbol: str, limit: int = 500) -> List[dict]:
    """weight=20 tren futures - dat nhat trong toan bo cac endpoint dung.
    Chi nen goi khi seed lan dau cho 1 symbol chua co WS tape."""
    raw = safe_get(f"{FAPI}/fapi/v1/aggTrades", params={"symbol": symbol, "limit": limit}, weight=20)
    out = []
    if not raw:
        return out
    for t in raw:
        out.append({
            "price": float(t["p"]), "qty": float(t["q"]),
            "isBuyerMaker": bool(t["m"]), "ts": t["T"],
        })
    return out


def fetch_depth(symbol: str, limit: int = 20) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """weight=2 cho limit<=50 tren futures."""
    raw = safe_get(f"{FAPI}/fapi/v1/depth", params={"symbol": symbol, "limit": limit}, weight=2)
    if not raw:
        return [], []
    bids = [(float(p), float(q)) for p, q in raw.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in raw.get("asks", [])]
    return bids, asks


def fetch_premium_index(symbol: str, cache_ttl: float = 0.0) -> dict:
    """1 call duy nhat cho ca funding + mark + index (thay vi goi premiumIndex 2 lan
    nhu truoc: fetch_funding va fetch_mark_index tach roi). weight=1. Cache TTL
    vi funding chi doi moi 8h, mark/index doi nhanh hon nhung khong can moi 20-25s."""
    cache_key = f"premium:{symbol}"
    if cache_ttl > 0:
        cached = REST_CACHE.get(cache_key)
        if cached is not None:
            return cached
    raw = safe_get(f"{FAPI}/fapi/v1/premiumIndex", params={"symbol": symbol}, weight=1)
    result = {"funding": None, "mark": None, "index": None}
    if raw:
        try:
            result["funding"] = float(raw.get("lastFundingRate", 0.0))
        except (TypeError, ValueError):
            pass
        try:
            result["mark"] = float(raw.get("markPrice"))
            result["index"] = float(raw.get("indexPrice"))
        except (TypeError, ValueError):
            pass
    if cache_ttl > 0:
        REST_CACHE.set(cache_key, result, cache_ttl)
    return result


def fetch_open_interest(symbol: str, cache_ttl: float = 0.0) -> Optional[float]:
    """weight=1."""
    cache_key = f"oi:{symbol}"
    if cache_ttl > 0:
        cached = REST_CACHE.get(cache_key)
        if cached is not None:
            return cached
    raw = safe_get(f"{FAPI}/fapi/v1/openInterest", params={"symbol": symbol}, weight=1)
    val = None
    if raw:
        try:
            val = float(raw.get("openInterest"))
        except (TypeError, ValueError):
            val = None
    if cache_ttl > 0 and val is not None:
        REST_CACHE.set(cache_key, val, cache_ttl)
    return val


def fetch_long_short_ratio(symbol: str, cache_ttl: float = 0.0) -> Optional[float]:
    """weight=1."""
    cache_key = f"lsr:{symbol}"
    if cache_ttl > 0:
        cached = REST_CACHE.get(cache_key)
        if cached is not None:
            return cached
    raw = safe_get(f"{FAPI}/futures/data/topLongShortPositionRatio", params={
        "symbol": symbol, "period": "15m", "limit": 1,
    }, weight=1)
    val = None
    if raw:
        try:
            val = float(raw[0].get("longShortRatio"))
        except (IndexError, KeyError, TypeError, ValueError):
            val = None
    if cache_ttl > 0 and val is not None:
        REST_CACHE.set(cache_key, val, cache_ttl)
    return val


def fetch_last_price(symbol: str, cache_ttl: float = 0.0) -> Optional[float]:
    """Gia hien tai, nhe (GET /fapi/v1/ticker/price, weight=1, khong keo theo
    orderbook/funding/OI...). Dung RIENG de theo doi TP/SL cho cac symbol dang
    co active signal nhung da roi khoi scan_set cua vong quet hien tai (rot
    khoi top volume, /coinstrong tat, het "nong"...) va vi vay khong con duoc
    build_features() cap nhat gia moi vong nua.

    Truoc day thieu ham nay la nguyen nhan chinh khien bot 'im lang' voi
    mot so tin hieu da bat: SignalEngine._check_hits_and_expiry() chi nhan
    gia tu ket qua build_features() cua vong quet hien tai, symbol nao khong
    con trong scan_set thi khong co gia -> bi bo qua vinh vien, khong bao
    gio duoc bao TP/SL hay het han nua. Xem SignalEngine._fill_missing_prices
    trong app.py."""
    cache_key = f"lastpx:{symbol}"
    if cache_ttl > 0:
        cached = REST_CACHE.get(cache_key)
        if cached is not None:
            return cached
    raw = safe_get(f"{FAPI}/fapi/v1/ticker/price", params={"symbol": symbol}, weight=1)
    val = None
    if raw:
        try:
            val = float(raw.get("price"))
        except (TypeError, ValueError):
            val = None
    if cache_ttl > 0 and val is not None:
        REST_CACHE.set(cache_key, val, cache_ttl)
    return val


def fetch_cross_exchange_price(exchange: str, symbol: str, timeout: float = 3.0) -> Optional[float]:
    """Best-effort, tra ve None neu loi (khong lam sap vong quet).
    Dung safe_get_external: KHONG di qua WEIGHT_LIMITER cua Binance."""
    base = symbol.replace("USDT", "")
    try:
        if exchange == "OKX":
            url = CROSS_EXCHANGE_ENDPOINTS["OKX"].format(inst=f"{base}-USDT-SWAP")
            r = safe_get_external(url, timeout=timeout)
            return float(r["data"][0]["last"]) if r and r.get("data") else None
        if exchange == "BYBIT":
            url = CROSS_EXCHANGE_ENDPOINTS["BYBIT"].format(sym=symbol)
            r = safe_get_external(url, timeout=timeout)
            lst = r["result"]["list"] if r and r.get("result") else []
            return float(lst[0]["lastPrice"]) if lst else None
        if exchange == "BINGX":
            url = CROSS_EXCHANGE_ENDPOINTS["BINGX"].format(inst=f"{base}-USDT")
            r = safe_get_external(url, timeout=timeout)
            return float(r["data"]["price"]) if r and r.get("data") else None
        if exchange == "KUCOIN":
            url = CROSS_EXCHANGE_ENDPOINTS["KUCOIN"].format(inst=f"{base}USDTM")
            r = safe_get_external(url, timeout=timeout)
            return float(r["data"]["price"]) if r and r.get("data") else None
        if exchange == "BITGET":
            url = CROSS_EXCHANGE_ENDPOINTS["BITGET"].format(sym=symbol)
            r = safe_get_external(url, timeout=timeout)
            data = r.get("data") if r else None
            if data and isinstance(data, list):
                return float(data[0]["lastPr"])
            return None
        if exchange == "MEXC":
            url = CROSS_EXCHANGE_ENDPOINTS["MEXC"].format(inst=f"{base}_USDT")
            r = safe_get_external(url, timeout=timeout)
            return float(r["data"]["lastPrice"]) if r and r.get("data") else None
    except Exception as e:  # noqa: BLE001
        log.debug("cross-exchange %s %s loi: %s", exchange, symbol, e)
        return None
    return None


def fetch_cross_exchange_avg(symbol: str, cfg: AppConfig) -> Optional[float]:
    """Gia trung binh tu cac san khac Binance cho 1 symbol.

    - Cache theo cfg.cross_exchange_cache_seconds: gia tham chieu cross-exchange
      khong can fresh moi vong POLL_SECONDS, cache 30-60s la du (trong so module
      nay trong composite chi 0.5, la boi canh chu khong phai trigger).
    - Goi 6 san SONG SONG bang ThreadPoolExecutor thay vi tuan tu: neu khong,
      bat CROSS_EXCHANGE_ALL cho ca tram cap se lam vong quet cham gap nhieu lan
      (moi symbol phai cho tuan tu 6 request mang).
    - 1 san bi cham/timeout khong lam mat gia cua 5 san con lai (best-effort).
    """
    cache_key = f"crossavg:{symbol}"
    cached = REST_CACHE.get(cache_key)
    if cached is not None:
        return cached

    others = [ex for ex in cfg.exchanges if ex != "BINANCE"]
    if not others:
        return None

    prices: List[float] = []
    workers = max(min(cfg.cross_exchange_workers, len(others)), 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_cross_exchange_price, ex, symbol, cfg.cross_exchange_timeout): ex
            for ex in others
        }
        for fut in as_completed(futures):
            ex = futures[fut]
            try:
                p = fut.result()
            except Exception as e:  # noqa: BLE001
                log.debug("cross-exchange %s %s future loi: %s", ex, symbol, e)
                continue
            if p:
                prices.append(p)

    avg = (sum(prices) / len(prices)) if prices else None
    # Cache ca ket qua None (TTL ngan hon) de tranh spam lai ngay lap tuc mot
    # symbol khong ton tai / khong so gia duoc tren san nao.
    ttl = cfg.cross_exchange_cache_seconds if avg is not None else min(cfg.cross_exchange_cache_seconds, 20)
    REST_CACHE.set(cache_key, avg, ttl)
    return avg


# --------------------------------------------------------------------------
# Realtime state: TradeTape / LocalBook / LiquidationTape
# --------------------------------------------------------------------------

class TradeTape:
    """Luu trade gan nhat cho 1 symbol, tinh CVD 1m/5m/15m + large print cluster."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.trades: Deque[dict] = deque()
        self._lock = threading.Lock()

    def add_trade(self, price: float, qty: float, is_buyer_maker: bool, ts: int) -> None:
        with self._lock:
            self.trades.append({"price": price, "qty": qty, "isBuyerMaker": is_buyer_maker, "ts": ts})
            cutoff = ts - self.cfg.tape_window_seconds * 1000
            while self.trades and self.trades[0]["ts"] < cutoff:
                self.trades.popleft()

    def seed(self, trades: List[dict]) -> None:
        with self._lock:
            for t in trades:
                self.trades.append(t)

    def snapshot(self) -> List[dict]:
        with self._lock:
            return list(self.trades)

    def cvd_window(self, seconds: int) -> float:
        now = self.trades[-1]["ts"] if self.trades else int(time.time() * 1000)
        cutoff = now - seconds * 1000
        with self._lock:
            window = [t for t in self.trades if t["ts"] >= cutoff]
        return compute_cvd(window)

    def large_print_cluster(self) -> dict:
        """Cum >=3 lenh lon cung phia trong 30s. Nguong = MIN_LARGE_PRINT_USD hoac quantile."""
        with self._lock:
            trades = list(self.trades)
        if not trades:
            return {"cluster": False, "side": None, "count": 0}
        usd_vals = [t["price"] * t["qty"] for t in trades]
        try:
            q = statistics.quantiles(usd_vals, n=1000)[int(self.cfg.large_print_quantile * 1000) - 1]
        except (statistics.StatisticsError, IndexError):
            q = max(usd_vals) if usd_vals else 0.0
        threshold = max(self.cfg.min_large_print_usd, min(q, self.cfg.min_large_print_usd * 20))
        large = [t for t in trades if t["price"] * t["qty"] >= threshold]
        if not large:
            return {"cluster": False, "side": None, "count": 0}
        last_ts = large[-1]["ts"]
        window = [t for t in large if last_ts - t["ts"] <= 30_000]
        buy_count = sum(1 for t in window if not t["isBuyerMaker"])
        sell_count = sum(1 for t in window if t["isBuyerMaker"])
        if buy_count >= 3 and buy_count >= sell_count:
            return {"cluster": True, "side": "long", "count": buy_count}
        if sell_count >= 3 and sell_count > buy_count:
            return {"cluster": True, "side": "short", "count": sell_count}
        return {"cluster": False, "side": None, "count": max(buy_count, sell_count)}


class LocalBook:
    """Sổ lệnh cục bộ cho 1 symbol tu WS depth20 hoac REST fallback."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.bids: List[Tuple[float, float]] = []
        self.asks: List[Tuple[float, float]] = []
        self._history: Deque[Tuple[float, List[Tuple[float, float]], List[Tuple[float, float]]]] = deque(maxlen=50)
        self._lock = threading.Lock()
        self.last_update = 0.0

    def update(self, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]]) -> None:
        with self._lock:
            now = time.time()
            self.bids, self.asks = bids, asks
            self._history.append((now, bids, asks))
            self.last_update = now

    def imbalance(self) -> float:
        with self._lock:
            return compute_imbalance(self.bids, self.asks, self.cfg.depth_levels)

    def microprice(self) -> float:
        with self._lock:
            if not self.bids or not self.asks:
                return 0.0
            bb, bq = self.bids[0]
            ba, aq = self.asks[0]
            return compute_microprice(bb, bq, ba, aq)

    def persist_score(self) -> float:
        """0..1: top-of-book on dinh qua bao nhieu snapshot lien tiep (BOOK_PERSIST_MS)."""
        with self._lock:
            hist = list(self._history)
        if len(hist) < 2:
            return 0.0
        stable = 0
        total = 0
        for i in range(1, len(hist)):
            t0, b0, a0 = hist[i - 1]
            t1, b1, a1 = hist[i]
            if (t1 - t0) * 1000 > self.cfg.book_persist_ms * 3:
                continue
            total += 1
            if b0 and b1 and a0 and a1:
                same_bid = abs(b0[0][0] - b1[0][0]) < 1e-9
                same_ask = abs(a0[0][0] - a1[0][0]) < 1e-9
                if same_bid and same_ask:
                    stable += 1
        return stable / total if total else 0.0

    def pull_ratio_3s(self) -> float:
        """Ty le volume top-of-book bi rut trong 3s gan nhat (proxy spoof)."""
        with self._lock:
            hist = [h for h in self._history if time.time() - h[0] <= 3.0]
        if len(hist) < 2:
            return 0.0
        first_vol = sum(q for _, q in hist[0][1][:5]) + sum(q for _, q in hist[0][2][:5])
        last_vol = sum(q for _, q in hist[-1][1][:5]) + sum(q for _, q in hist[-1][2][:5])
        if first_vol <= 0:
            return 0.0
        pulled = max(first_vol - last_vol, 0.0)
        return min(pulled / first_vol, 1.0)


class LiquidationTape:
    """Tich luy thanh ly tu WS @forceOrder, theo tung symbol.
    Truoc day gop chung tat ca symbol vao 1 tong duy nhat (chi dung tam khi
    con it symbol CORE); voi 200 cap can tach rieng de moi cap co so lieu dung
    cho chinh no."""

    def __init__(self, window_seconds: int = 3600):
        self.window_seconds = window_seconds
        self.events: Dict[str, Deque[dict]] = {}
        self._lock = threading.Lock()

    def add_event(self, symbol: str, side: str, usd: float, ts: int) -> None:
        with self._lock:
            dq = self.events.setdefault(symbol, deque())
            dq.append({"side": side, "usd": usd, "ts": ts})
            cutoff = ts - self.window_seconds * 1000
            while dq and dq[0]["ts"] < cutoff:
                dq.popleft()

    def totals(self, symbol: str) -> dict:
        with self._lock:
            events = list(self.events.get(symbol, ()))
        long_liq = sum(e["usd"] for e in events if e["side"] == "long")
        short_liq = sum(e["usd"] for e in events if e["side"] == "short")
        now = events[-1]["ts"] if events else int(time.time() * 1000)
        cutoff = now - 60_000
        impulse_long = sum(e["usd"] for e in events if e["side"] == "long" and e["ts"] >= cutoff)
        impulse_short = sum(e["usd"] for e in events if e["side"] == "short" and e["ts"] >= cutoff)
        return {
            "long_liq_usd": long_liq, "short_liq_usd": short_liq,
            "impulse_60s_long": impulse_long, "impulse_60s_short": impulse_short,
        }


def detect_iceberg_spoof(book: LocalBook) -> float:
    """Proxy spoof_score 0..1 = pull_ratio_3s cao ma khong co giao dich tuong ung."""
    return book.pull_ratio_3s()


def detect_absorption(cvd_short: float, side_price_move: float, persist_score: float) -> dict:
    """
    CVD nguoc voi huong gia (vd CVD am nhung gia khong giam) + book persist cao
    => absorption. side_price_move: % thay doi gia gan nhat (duong = tang).
    """
    absorbed = False
    side = None
    if cvd_short < 0 and side_price_move >= 0 and persist_score >= 0.5:
        absorbed, side = True, "long"  # ban bi hap thu -> nghieng long
    elif cvd_short > 0 and side_price_move <= 0 and persist_score >= 0.5:
        absorbed, side = True, "short"  # mua bi hap thu -> nghieng short
    return {"absorption": absorbed, "side": side}


# --------------------------------------------------------------------------
# StreamHub: 2 WebSocket connections (public / market)
# --------------------------------------------------------------------------

class StreamHub:
    """Mo 2 ket noi WS (public: bookTicker+depth20, market: aggTrade+forceOrder+markPrice).
    Reconnect voi backoff. Khong chan REST khi WS chet."""

    def __init__(self, cfg: AppConfig, tapes: Dict[str, TradeTape], books: Dict[str, LocalBook],
                 liq: LiquidationTape):
        self.cfg = cfg
        self.tapes = tapes
        self.books = books
        self.liq = liq
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.mark_price: Dict[str, float] = {}

    def start(self, symbols: List[str]) -> None:
        if websocket is None:
            log.warning("websocket-client chua duoc cai, bo qua WS, dung REST fallback")
            return
        chunk_size = max(getattr(self.cfg, "ws_chunk_size", 40), 1)
        self._threads = []
        for i in range(0, len(symbols), chunk_size):
            group = symbols[i:i + chunk_size]
            public_streams = []
            market_streams = []
            for s in group:
                sym = s.lower()
                public_streams += [f"{sym}@bookTicker", f"{sym}@depth20@100ms"]
                market_streams += [f"{sym}@aggTrade", f"{sym}@forceOrder", f"{sym}@markPrice@1s"]
            t1 = threading.Thread(
                target=self._run, args=(WS_PUBLIC + "/".join(public_streams), self._on_public), daemon=True)
            t2 = threading.Thread(
                target=self._run, args=(WS_MARKET + "/".join(market_streams), self._on_market), daemon=True)
            t1.start()
            t2.start()
            self._threads += [t1, t2]
        log.info("StreamHub: %d symbol chia thanh %d connection-pair (chunk=%d)",
                  len(symbols), len(self._threads) // 2, chunk_size)

    def stop(self) -> None:
        self._stop.set()

    def _run(self, url: str, handler) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                ws = websocket.WebSocketApp(
                    url,
                    on_message=lambda _ws, msg: handler(msg),
                    on_error=lambda _ws, err: log.warning("WS error: %s", err),
                )
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:  # noqa: BLE001
                log.warning("WS run_forever loi: %s", e)
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _on_public(self, msg: str) -> None:
        try:
            payload = json.loads(msg)
            data = payload.get("data", payload)
            stream = payload.get("stream", "")
            if "@bookTicker" in stream:
                sym = data.get("s")
                book = self.books.get(sym)
                if book and data.get("b") and data.get("a"):
                    bb, bq = float(data["b"]), float(data["B"])
                    ba, aq = float(data["a"]), float(data["A"])
                    if not book.bids or not book.asks:
                        book.update([(bb, bq)], [(ba, aq)])
                    else:
                        with book._lock:  # cap nhat top-of-book nhanh
                            bids = [(bb, bq)] + book.bids[1:]
                            asks = [(ba, aq)] + book.asks[1:]
                        book.update(bids, asks)
            elif "@depth20" in stream:
                sym = data.get("s") or stream.split("@")[0].upper()
                book = self.books.get(sym)
                if book:
                    bids = [(float(p), float(q)) for p, q in data.get("b", [])]
                    asks = [(float(p), float(q)) for p, q in data.get("a", [])]
                    if bids and asks:
                        book.update(bids, asks)
        except Exception as e:  # noqa: BLE001
            log.debug("on_public parse loi: %s", e)

    def _on_market(self, msg: str) -> None:
        try:
            payload = json.loads(msg)
            data = payload.get("data", payload)
            stream = payload.get("stream", "")
            if "@aggTrade" in stream:
                sym = data.get("s")
                tape = self.tapes.get(sym)
                if tape:
                    tape.add_trade(float(data["p"]), float(data["q"]), bool(data["m"]), int(data["T"]))
            elif "@forceOrder" in stream:
                o = data.get("o", {})
                sym = o.get("s")
                side = "long" if o.get("S") == "SELL" else "short"
                # forceOrder SELL = thanh ly vi the LONG; BUY = thanh ly vi the SHORT
                usd = float(o.get("ap", 0.0) or o.get("p", 0.0)) * float(o.get("q", 0.0))
                if sym:
                    self.liq.add_event(sym, side, usd, int(data.get("E", time.time() * 1000)))
            elif "@markPrice" in stream:
                sym = data.get("s")
                if sym and data.get("p"):
                    self.mark_price[sym] = float(data["p"])
        except Exception as e:  # noqa: BLE001
            log.debug("on_market parse loi: %s", e)


# --------------------------------------------------------------------------
# MarketContext: gom toan bo state realtime + cache REST cho 1 lan build_features
# --------------------------------------------------------------------------

class MarketContext:
    """Container cho toan bo state (tapes, books, liq, funding history) dung xuyen suot engine."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.tapes: Dict[str, TradeTape] = {}
        self.books: Dict[str, LocalBook] = {}
        self.liq = LiquidationTape()
        self.funding_history: Dict[str, Deque[float]] = {}
        self.stream: Optional[StreamHub] = None

    def ensure_symbol(self, symbol: str) -> None:
        if symbol not in self.tapes:
            self.tapes[symbol] = TradeTape(self.cfg)
        if symbol not in self.books:
            self.books[symbol] = LocalBook(self.cfg)
        if symbol not in self.funding_history:
            self.funding_history[symbol] = deque(maxlen=200)

    def start_stream(self, ws_symbols: List[str]) -> None:
        for s in ws_symbols:
            self.ensure_symbol(s)
        self.stream = StreamHub(self.cfg, self.tapes, self.books, self.liq)
        self.stream.start(ws_symbols)

    def funding_zscore(self, symbol: str, current: float) -> float:
        hist = self.funding_history.setdefault(symbol, deque(maxlen=200))
        hist.append(current)
        if len(hist) < 5:
            return 0.0
        mean = statistics.mean(hist)
        try:
            stdev = statistics.stdev(hist)
        except statistics.StatisticsError:
            stdev = 0.0
        if stdev == 0:
            return 0.0
        return (current - mean) / stdev


def build_features(symbol: str, cfg: AppConfig, ctx: MarketContext, is_core: bool,
                    is_ws_tracked: bool) -> dict:
    """
    Gom toan bo du lieu can thiet cho 1 symbol thanh 1 dict 'features' dung chung
    cho ca signals.compute_composite() va ghi log features.jsonl.
    CORE: full data + cross-exchange. Khong CORE nhung trong WS set: tape/book live,
    khong cross-exchange (tru khi CROSS_EXCHANGE_ALL=true). Con lai (REST-only,
    van lay HTF kline): light snapshot.
    """
    ctx.ensure_symbol(symbol)
    now_ms = int(time.time() * 1000)

    klines_15m = fetch_klines(symbol, "15m", limit=40)  # luon fresh, khong cache
    klines_1h = fetch_klines(symbol, "1h", limit=30, cache_ttl=cfg.htf_1h_cache_seconds)
    klines_4h = fetch_klines(symbol, "4h", limit=20, cache_ttl=cfg.htf_4h_cache_seconds)

    bias_15m = bias_from_klines(klines_15m, 20)
    bias_1h = bias_from_klines(klines_1h, 12)
    bias_4h = bias_from_klines(klines_4h, 12)

    atr15m = compute_atr(klines_15m, period=14)
    # ATR khung 4h - dung lam co so cho TP nhieu tang (xem signals.suggested_sl_tp_multi).
    # Tan dung klines_4h da fetch san o tren (cho bias_4h) - khong ton them API call.
    atr4h = compute_atr(klines_4h, period=14)
    last_price = klines_15m[-1]["close"] if klines_15m else 0.0

    sweep = detect_sweep(klines_15m, klines_1h)
    regime = classify_regime(klines_1h)

    tape = ctx.tapes[symbol]
    book = ctx.books[symbol]

    if is_ws_tracked:
        trades = tape.snapshot()
        if not trades:
            seeded = fetch_agg_trades(symbol, limit=500)
            tape.seed(seeded)
            trades = tape.snapshot()
        cvd_1m = tape.cvd_window(60)
        cvd_5m = tape.cvd_window(300)
        cvd_15m = tape.cvd_window(900)
        cluster = tape.large_print_cluster()
        if not book.bids or not book.asks:
            bids, asks = fetch_depth(symbol, cfg.depth_levels)
            book.update(bids, asks)
        imbalance = book.imbalance()
        microprice = book.microprice()
        persist = book.persist_score()
        pull_ratio = book.pull_ratio_3s()
    else:
        trades = fetch_agg_trades(symbol, limit=200)
        cvd_1m = compute_cvd([t for t in trades if now_ms - t.get("ts", now_ms) <= 60_000])
        cvd_5m = compute_cvd([t for t in trades if now_ms - t.get("ts", now_ms) <= 300_000])
        cvd_15m = compute_cvd(trades)
        cluster = {"cluster": False, "side": None, "count": 0}
        bids, asks = fetch_depth(symbol, cfg.depth_levels)
        imbalance = compute_imbalance(bids, asks, cfg.depth_levels)
        microprice = compute_microprice(bids[0][0], bids[0][1], asks[0][0], asks[0][1]) if bids and asks else 0.0
        persist = 0.0
        pull_ratio = 0.0

    vp = compute_volume_profile(trades, atr15m, cfg.profile_tick_buckets, fallback_klines=klines_15m)

    price_move_pct = 0.0
    if len(klines_15m) >= 2 and klines_15m[-2]["close"]:
        price_move_pct = (klines_15m[-1]["close"] - klines_15m[-2]["close"]) / klines_15m[-2]["close"]
    absorption = detect_absorption(cvd_5m, price_move_pct, persist)
    spoof_score = pull_ratio if is_ws_tracked else 0.0

    # full_data_all: bat 4 module "nang" (funding/basis/OI/LSR/liquidation) cho
    # TOAN BO cap trong scan set, khong chi rieng CORE. Dung cache TTL de khong
    # phai goi lai moi vong quet (funding chi doi moi 8h tren Binance, OI/LSR
    # cung khong can lay lai moi 20-25s) -> giu weight budget an toan.
    use_full = is_core or cfg.full_data_all
    use_cross = is_core or cfg.cross_exchange_all

    liq_totals = ctx.liq.totals(symbol) if use_full else {
        "long_liq_usd": 0.0, "short_liq_usd": 0.0, "impulse_60s_long": 0.0, "impulse_60s_short": 0.0}

    funding = mark_price = index_price = None
    if is_core:
        # CORE: khong cache, luon lay moi nhat.
        premium = fetch_premium_index(symbol)
        funding, mark_price, index_price = premium["funding"], premium["mark"], premium["index"]
    elif use_full or is_ws_tracked:
        premium = fetch_premium_index(symbol, cache_ttl=cfg.funding_cache_seconds)
        funding, mark_price, index_price = premium["funding"], premium["mark"], premium["index"]

    open_interest = None
    if use_full:
        open_interest = fetch_open_interest(symbol, cache_ttl=0 if is_core else cfg.oi_cache_seconds)

    lsr = None
    if use_full:
        lsr = fetch_long_short_ratio(symbol, cache_ttl=0 if is_core else cfg.lsr_cache_seconds)

    funding_z = ctx.funding_zscore(symbol, funding) if funding is not None else 0.0

    basis = 0.0
    if mark_price and index_price:
        basis = (mark_price - index_price) / index_price

    taker_ratio = 0.0
    if klines_15m and klines_15m[-1]["volume"]:
        taker_ratio = klines_15m[-1]["taker_buy_base"] / klines_15m[-1]["volume"]

    # cross_exchange_all: bat cross-exchange divergence cho TOAN BO scan set
    # (khong chi CORE). fetch_cross_exchange_avg tu goi song song 6 san +
    # cache rieng (KHONG dung chung WEIGHT_LIMITER cua Binance), nen bat cho
    # ca tram cap van an toan cho toc do vong quet va cho ngan sach Binance.
    cross_divergence = 0.0
    if use_cross:
        if is_core:
            # CORE: khong cache, luon lay gia moi nhat tu 6 san.
            others = [ex for ex in cfg.exchanges if ex != "BINANCE"]
            prices = []
            with ThreadPoolExecutor(max_workers=max(len(others), 1)) as pool:
                futures = [pool.submit(fetch_cross_exchange_price, ex, symbol, cfg.cross_exchange_timeout)
                           for ex in others]
                for fut in as_completed(futures):
                    try:
                        p = fut.result()
                    except Exception:  # noqa: BLE001
                        continue
                    if p:
                        prices.append(p)
            avg_other = (sum(prices) / len(prices)) if prices else None
        else:
            avg_other = fetch_cross_exchange_avg(symbol, cfg)
        if avg_other and last_price:
            cross_divergence = (last_price - avg_other) / avg_other

    return {
        "symbol": symbol, "ts": now_ms, "is_core": is_core, "is_ws_tracked": is_ws_tracked,
        "last_price": last_price, "atr15m": atr15m, "atr4h": atr4h,
        "bias_15m": bias_15m, "bias_1h": bias_1h, "bias_4h": bias_4h,
        "regime": regime, "sweep": sweep,
        "volume_profile": vp,
        "cvd_1m": cvd_1m, "cvd_5m": cvd_5m, "cvd_15m": cvd_15m,
        "large_print_cluster": cluster,
        "book_imbalance": imbalance, "microprice": microprice,
        "persist_score": persist, "pull_ratio_3s": pull_ratio, "spoof_score": spoof_score,
        "absorption": absorption,
        "liquidation": liq_totals,
        "funding_rate": funding, "funding_zscore": funding_z,
        "basis": basis, "open_interest": open_interest,
        "long_short_ratio": lsr, "taker_buy_sell_ratio": taker_ratio,
        "cross_exchange_divergence": cross_divergence,
        "price_move_pct_15m": price_move_pct,
    }
