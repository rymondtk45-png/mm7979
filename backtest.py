#!/usr/bin/env python3
"""
backtest.py

Doc logs/features.jsonl (do main.py ghi trong luc chay live), tai tinh composite
score cho tung dong bang logic hien tai trong signals.py, roi danh gia xap xi
ty le thang/thua bang cach so gia dong logged tiep theo cua CUNG symbol voi
SL/TP goi y (SL theo ATR15m + regime, 3 tang TP theo R) tai thoi diem alert
(score >= THRESHOLD).

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


def _evaluate_trade(direction: str, sl: float, tp1: float, tp2: float, tp3: float,
                     future_prices: List[float]) -> tuple:
    """Duyet gia tuong lai cua CUNG symbol, tra ve (outcome, tiers_hit).
    outcome: "win" (cham TP3), "partial" (an >=1 tang TP nhung chua cham SL/TP3
    truoc khi het du lieu), "loss" (cham SL - gay, bat ke da an tang nao truoc do),
    None (chua ro, het du lieu ma khong cham gi ca).
    SL luon duoc uu tien kiem tra truoc trong tung nen (gay la gay, dung tinh
    than 'cham SL la dong tin hieu' o app.py)."""
    tiers_hit: List[str] = []
    for price in future_prices:
        sl_hit = (price <= sl) if direction == "long" else (price >= sl)
        if sl_hit:
            return "loss", tiers_hit
        for name, level in (("TP1", tp1), ("TP2", tp2), ("TP3", tp3)):
            if name in tiers_hit:
                continue
            reached = (price >= level) if direction == "long" else (price <= level)
            if reached:
                tiers_hit.append(name)
        if "TP3" in tiers_hit:
            return "win", tiers_hit
    if tiers_hit:
        return "partial", tiers_hit
    return None, tiers_hit


def run_backtest(rows: List[dict], cfg: AppConfig, weights: Dict[str, float]) -> None:
    by_symbol: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        by_symbol[r["symbol"]].append(r)
    for sym in by_symbol:
        by_symbol[sym].sort(key=lambda r: r["ts"])

    wins, partials, losses, no_result, total_alerts = 0, 0, 0, 0, 0

    for sym, series in by_symbol.items():
        for i, f in enumerate(series):
            result = compute_composite(f, weights, cfg)
            if result["veto"] or result["direction"] == "neutral":
                continue
            if result["score"] < cfg.threshold:
                continue
            total_alerts += 1

            entry = f["last_price"]
            regime = f.get("regime", "")
            poc = f.get("volume_profile", {}).get("poc", 0.0)
            levels = suggested_sl_tp(entry, result["direction"], f.get("atr15m", 0.0),
                                     regime, poc, cfg)
            future_prices = [future["last_price"] for future in series[i + 1:]]
            outcome, tiers_hit = _evaluate_trade(
                result["direction"], levels["sl"], levels["tp1"], levels["tp2"], levels["tp3"],
                future_prices)

            if outcome == "win":
                wins += 1
            elif outcome == "partial":
                partials += 1
            elif outcome == "loss":
                losses += 1
            else:
                no_result += 1

    print("=" * 50)
    print(f"Tong so alert (score >= THRESHOLD={cfg.threshold}): {total_alerts}")
    print(f"Win (cham TP3 - an het): {wins}")
    print(f"Partial (an >=1 tang TP, chua het/chua gay SL truoc khi het du lieu): {partials}")
    print(f"Loss (cham SL - gay): {losses}")
    print(f"Chua ro ket qua (het du lieu, khong cham gi): {no_result}")
    graded = wins + partials + losses
    if graded:
        print(f"Ty le co loi (Win+Partial / tong co ket qua): {(wins + partials) / graded * 100:.1f}%")
    graded_full = wins + losses
    if graded_full:
        print(f"Winrate full TP3 (tren Win+Loss, bo qua Partial): {wins / graded_full * 100:.1f}%")
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
