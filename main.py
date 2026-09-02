#!/usr/bin/env python3
"""
main.py
Entry point cho bot_mm_fund - bot BAO TIN HIEU long/short crypto futures.
Khong dat lenh tren san. Chi phan tich va gui canh bao qua Telegram.

Chay: python main.py  (sau khi pip install -r requirements.txt va cau hinh .env)
"""
from __future__ import annotations

import signal
import sys

from app import SignalEngine
from config import AppConfig, get_logger

log = get_logger("main")


def main() -> None:
    cfg = AppConfig()
    engine = SignalEngine(cfg)

    def _shutdown(signum, frame):  # noqa: ANN001
        log.info("Nhan tin hieu dung (%s), dang tat engine...", signum)
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Khoi dong bot_mm_fund. CORE_SYMBOLS=%s | THRESHOLD=%.1f | POLL=%ss",
              cfg.core_symbols, cfg.threshold, cfg.poll_seconds)
    log.info("Bot CHI BAO TIN HIEU, khong tu dong dat lenh.")
    engine.start()


if __name__ == "__main__":
    main()
