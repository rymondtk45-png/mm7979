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

    depth_levels: int = field(default_factory=lambda: _get_int("DEPTH_LEVELS", 20))
    tape_window_seconds: int = field(default_factory=lambda: _get_int("TAPE_WINDOW_SECONDS", 14400))
    large_print_quantile: float = field(default_factory=lambda: _get_float("LARGE_PRINT_QUANTILE", 0.995))
    min_large_print_usd: float = field(default_factory=lambda: _get_float("MIN_LARGE_PRINT_USD", 50000))
    book_persist_ms: int = field(default_factory=lambda: _get_int("BOOK_PERSIST_MS", 1000))
    profile_tick_buckets: int = field(default_factory=lambda: _get_int("PROFILE_TICK_BUCKETS", 40))
    signal_ttl_seconds: int = field(default_factory=lambda: _get_int("SIGNAL_TTL_SECONDS", 2400))

    # --- Lenh /scan (theo doi rieng 1 symbol theo yeu cau, khong phu thuoc
    # scan_set/threshold/HTF veto cua vong lap alert chinh) ---
    scan_update_seconds: int = field(default_factory=lambda: _get_int("SCAN_UPDATE_SECONDS", 300))
    scan_max_subscriptions: int = field(default_factory=lambda: _get_int("SCAN_MAX_SUBSCRIPTIONS", 20))

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
