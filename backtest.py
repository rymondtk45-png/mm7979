#!/usr/bin/env python3
"""
backtest.py
Doc logs/features.jsonl (do main.py ghi trong luc chay live), tai tinh composite
score cho tung dong bang logic hien tai trong src/signals.py, roi danh gia xap xi
ty le thang/thua bang cach so gia dong logged tiep theo cua CUNG symbol voi
SL/TP goi y tu ATR15m tai thoi diem alert (score >= THRESHOLD).

Day la cong cu nghien cuu ngoai tuyen, KHONG dat lenh, khong ket noi san giao dich.
Chay: python backtest.py [duong_dan_features.jsonl]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from config import AppConfig, load_weights
from signals import compute_composite, suggested_sl_tp


def load_features(path: Path) -> List[dict]:
    rows = []
    if not path.exists():
        print(f"Khong tim thay file: {path}")
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def run_backtest(rows: List[dict], cfg: AppConfig, weights: Dict[str, float]) -> None:
    by_symbol: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        by_symbol[r["symbol"]].append(r)
    for sym in by_symbol:
        by_symbol[sym].sort(key=lambda r: r["ts"])

    wins, losses, no_result, total_alerts = 0, 0, 0, 0

    for sym, series in by_symbol.items():
        for i, f in enumerate(series):
            result = compute_composite(f, weights, cfg)
            if result["veto"] or result["direction"] == "neutral":
                continue
            if result["score"] < cfg.threshold:
                continue
            total_alerts += 1
            entry = f["last_price"]
            sl, tp = suggested_sl_tp(entry, result["direction"], f.get("atr15m", 0.0))

            outcome = None
            for future in series[i + 1:]:
                price = future["last_price"]
                if result["direction"] == "long":
                    if price <= sl:
                        outcome = "loss"
                        break
                    if price >= tp:
                        outcome = "win"
                        break
                else:
                    if price >= sl:
                        outcome = "loss"
                        break
                    if price <= tp:
                        outcome = "win"
                        break
            if outcome == "win":
                wins += 1
            elif outcome == "loss":
                losses += 1
            else:
                no_result += 1

    print("=" * 50)
    print(f"Tong so alert (score >= THRESHOLD={cfg.threshold}): {total_alerts}")
    print(f"Win: {wins} | Loss: {losses} | Chua ro ket qua (het du lieu): {no_result}")
    graded = wins + losses
    if graded:
        print(f"Winrate (tren so co ket qua): {wins / graded * 100:.1f}%")
    print("=" * 50)
    print("Luu y: day la backtest xap xi tren du lieu feature log da thu thap,")
    print("khong phai simulation chinh xac tick-by-tick. Chi de tham khao.")


def main() -> None:
    cfg = AppConfig()
    weights = load_weights()
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else cfg.resolve_path(cfg.feature_log_path)
    rows = load_features(path)
    if not rows:
        print("Khong co du lieu features.jsonl de backtest. Chay main.py truoc de thu thap log.")
        return
    run_backtest(rows, cfg, weights)


if __name__ == "__main__":
    main()
