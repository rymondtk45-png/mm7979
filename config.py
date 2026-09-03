"""
config.py

Cau hinh trung tam cho bot_mm_fund. Doc .env, doc weights.json, tien ich ghi JSONL.
Khong dat lenh. Chi bao tin hieu.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

_LOG_LOCK = threading.Lock()


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    try:
        return int(val) if val not in (None, "") else default
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    try:
        return float(val) if val not in (None, "") else default
    except ValueError:
        return default


def _get_list(name: str, default: List[str]) -> List[str]:
    val = os.getenv(name)
    if not val:
        return default
    return [x.strip().upper() for x in val.split(",") if x.strip()]


@dataclass
class AppConfig:
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    core_symbols: List[str] = field(default_factory=lambda: _get_list(
        "CORE_SYMBOLS", ["BTCUSDT", "ETHUSDT", "SOLUSDT"]))
    contract_type: str = field(default_factory=lambda: os.getenv("CONTRACT_TYPE", "PERPETUAL"))
    quote_asset: str = field(default_factory=lambda: os.getenv("QUOTE_ASSET", "USDT"))
    coinstrong_default: bool = field(default_factory=lambda: _get_bool("COINSTRONG", False))
    scan_limit_off: int = field(default_factory=lambda: _get_int("SCAN_LIMIT_OFF", 25))
    scan_limit_on: int = field(default_factory=lambda: _get_int("SCAN_LIMIT_ON", 200))
    min_quote_volume: float = field(default_factory=lambda: _get_float("MIN_QUOTE_VOLUME", 20_000_000))
    min_hot_change_pct: float = field(default_factory=lambda: _get_float("MIN_HOT_CHANGE_PCT", 3.0))
    universe_refresh_seconds: int = field(default_factory=lambda: _get_int("UNIVERSE_REFRESH_SECONDS", 60))
    exchanges: List[str] = field(default_factory=lambda: _get_list(
        "EXCHANGES", ["BINANCE", "OKX", "BYBIT", "BINGX", "KUCOIN", "BITGET", "MEXC"]))

    # --- Full-model-for-all-pairs ---
    full_data_all: bool = field(default_factory=lambda: _get_bool("FULL_DATA_ALL", True))
    cross_exchange_all: bool = field(default_factory=lambda: _get_bool("CROSS_EXCHANGE_ALL", False))
    ws_cover_all: bool = field(default_factory=lambda: _get_bool("WS_COVER_ALL", True))
    ws_chunk_size: int = field(default_factory=lambda: _get_int("WS_CHUNK_SIZE", 40))

    max_workers: int = field(default_factory=lambda: _get_int("MAX_WORKERS", 12))
    # Ngan sach weight/phut, de duoi gioi han that cua Binance Futures (2400/phut/IP)
    weight_budget_per_min: int = field(default_factory=lambda: _get_int("WEIGHT_BUDGET_PER_MIN", 2000))

    funding_cache_seconds: int = field(default_factory=lambda: _get_int("FUNDING_CACHE_SECONDS", 120))
    oi_cache_seconds: int = field(default_factory=lambda: _get_int("OI_CACHE_SECONDS", 60))
    lsr_cache_seconds: int = field(default_factory=lambda: _get_int("LSR_CACHE_SECONDS", 120))
    htf_1h_cache_seconds: int = field(default_factory=lambda: _get_int("HTF_1H_CACHE_SECONDS", 180))
    htf_4h_cache_seconds: int = field(default_factory=lambda: _get_int("HTF_4H_CACHE_SECONDS", 600))

    poll_seconds: float = field(default_factory=lambda: _get_float("POLL_SECONDS", 25))
    threshold: float = field(default_factory=lambda: _get_float("THRESHOLD", 65))
    use_futures: bool = field(default_factory=lambda: _get_bool("USE_FUTURES", True))
    alert_cooldown_seconds: int = field(default_factory=lambda: _get_int("ALERT_COOLDOWN_SECONDS", 900))
    enable_telegram: bool = field(default_factory=lambda: _get_bool("ENABLE_TELEGRAM", True))
    enable_market_intel_scoring: bool = field(
        default_factory=lambda: _get_bool("ENABLE_MARKET_INTEL_SCORING", True))
    min_tf: str = field(default_factory=lambda: os.getenv("MIN_TF", "15m"))
    require_1h_align: bool = field(default_factory=lambda: _get_bool("REQUIRE_1H_ALIGN", True))
    require_4h_align: bool = field(default_factory=lambda: _get_bool("REQUIRE_4H_ALIGN", True))

    log_path: str = field(default_factory=lambda: os.getenv("LOG_PATH", "logs/signals.jsonl"))
    feature_log_path: str = field(default_factory=lambda: os.getenv("FEATURE_LOG_PATH", "logs/features.jsonl"))
    # Fix lo hong #3 (mat active_signals/cooldowns khi restart/deploy): file
    # JSON de ghi lai state sau moi vong quet, doc lai luc khoi dong.
    active_signals_state_path: str = field(default_factory=lambda: os.getenv(
        "ACTIVE_SIGNALS_STATE_PATH", "logs/active_signals_state.json"))

    depth_levels: int = field(default_factory=lambda: _get_int("DEPTH_LEVELS", 20))
    tape_window_seconds: int = field(default_factory=lambda: _get_int("TAPE_WINDOW_SECONDS", 14400))
    large_print_quantile: float = field(default_factory=lambda: _get_float("LARGE_PRINT_QUANTILE", 0.995))
    min_large_print_usd: float = field(default_factory=lambda: _get_float("MIN_LARGE_PRINT_USD", 50000))
    book_persist_ms: int = field(default_factory=lambda: _get_int("BOOK_PERSIST_MS", 1000))
    profile_tick_buckets: int = field(default_factory=lambda: _get_int("PROFILE_TICK_BUCKETS", 40))
    signal_ttl_seconds: int = field(default_factory=lambda: _get_int("SIGNAL_TTL_SECONDS", 2400))
    # Chong "loang" tin hieu: neu 1 symbol dang co tin hieu CUNG CHIEU (long/long
    # hoac short/short) con active (chua cham SL/TP3/het TTL), bo qua alert moi
    # cung chieu cho symbol do - tranh spam nhieu keo trung nhau tren cung 1 coin.
    # Tin hieu NGUOC CHIEU (vd dang long active ma co short moi) van duoc bao
    # binh thuong vi la thong tin dao the co gia tri rieng.
    suppress_duplicate_direction_signal: bool = field(
        default_factory=lambda: _get_bool("SUPPRESS_DUPLICATE_DIRECTION_SIGNAL", True))
    # Canh bao tham khao (KHONG tu dong dong keo): moi vong quet, re-scan lai
    # cac symbol dang co tin hieu active (dung ngay ket qua da tinh trong vong
    # quet nay, khong ton them API call). Neu quet lai bi veto / mat huong ro
    # rang / dao chieu so voi luc vao -> gui 1 canh bao (chi 1 lan khi trang
    # thai thay doi, khong spam lai moi vong). Bot van theo doi SL/TP binh
    # thuong, nguoi dung tu quyet dinh giu hay dong keo.
    enable_signal_health_warning: bool = field(
        default_factory=lambda: _get_bool("ENABLE_SIGNAL_HEALTH_WARNING", True))

    # --- SL/TP da tang (theo yeu cau: TP khong qua ngan, SL vua phai theo regime) ---
    # SL = entry -+ sl_mult * ATR15m. sl_mult chon theo regime (xem signals.suggested_sl_tp):
    #   trending (mac dinh)      -> SL_ATR_BASE_MULT
    #   high_volatility          -> SL_ATR_HIGH_VOL_MULT (rong hon, tranh bi quet boi bien dong lon)
    #   accumulation             -> SL_ATR_ACCUMULATION_MULT (hep hon, bien do gia nho)
    sl_atr_base_mult: float = field(default_factory=lambda: _get_float("SL_ATR_BASE_MULT", 1.2))
    sl_atr_high_vol_mult: float = field(default_factory=lambda: _get_float("SL_ATR_HIGH_VOL_MULT", 1.6))
    sl_atr_accumulation_mult: float = field(default_factory=lambda: _get_float("SL_ATR_ACCUMULATION_MULT", 1.0))
    # Neu POC nam giua entry va SL ly thuyet (SL dang cat ngang vung volume cao),
    # day SL ra ngoai POC them mot chut (tinh theo boi so ATR) de tranh bi quet
    # dung tai vung thanh khoan day.
    sl_poc_buffer_atr: float = field(default_factory=lambda: _get_float("SL_POC_BUFFER_ATR", 0.15))
    # TP = entry +- tp_mult * R, R = |entry - SL| (1 don vi rui ro).
    tp1_r_mult: float = field(default_factory=lambda: _get_float("TP1_R_MULT", 1.2))
    tp2_r_mult: float = field(default_factory=lambda: _get_float("TP2_R_MULT", 2.4))
    tp3_r_mult: float = field(default_factory=lambda: _get_float("TP3_R_MULT", 4.0))

    def resolve_path(self, rel: str) -> Path:
        p = Path(rel)
        if not p.is_absolute():
            p = BASE_DIR / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


def load_weights(path: str = None) -> dict:
    p = Path(path) if path else (BASE_DIR / "weights.json")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def append_jsonl(path, obj: dict) -> None:
    """Ghi 1 dong JSON vao file, tao thu muc neu chua co. Thread-safe."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, default=str)
    with _LOG_LOCK:
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        # QUAN TRONG: StreamHandler() khong truyen stream se mac dinh ghi ra
        # stderr -> cac nen tang log nhu Railway se gan severity=error cho
        # MOI dong log (ke ca INFO binh thuong). Ep ghi ra stdout de log
        # INFO hien thi dung muc INFO.
        handler = logging.StreamHandler(stream=sys.stdout)
        fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
