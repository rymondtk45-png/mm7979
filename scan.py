"""
scan.py

Tinh nang /scan <COIN>: phan tich theo yeu cau, dung LAI y het pipeline he thong
dang dung cho vong quet chinh (build_features -> compute_composite -> classify_entry
-> suggested_sl_tp trong data.py / signals.py). Entry/SL/TP/huong KHONG duoc tinh
rieng le "lung tung" - day la dung lai dung 1 bo logic voi TOP5/alert chinh, chi khac
o cho: luon tra loi ngay (khong bi chan boi THRESHOLD/cooldown) va sau do tu dong
cap nhat lai dinh ky cho toi khi nguoi dung xin dung ("bo <COIN>").

Gioi han da biet: du lieu duoc lay lai qua REST moi lan cap nhat (khong mo them
WebSocket rieng cho coin chi duoc /scan, de khong lam phuc tap them StreamHub dang
chay). Nghia la CVD/tape giua 2 lan cap nhat khong "song" tung tick nhu cac cap nam
san trong scan set/WS, nhung huong/entry/SL/TP van la ket qua CUA DUNG HE THONG tai
thoi diem goi, khong phai so lieu bia.
"""
from __future__ import annotations

import threading
from typing import Dict, Optional

from config import AppConfig, get_logger
from data import MarketContext, build_features
from signals import classify_entry, compute_composite, suggested_sl_tp

log = get_logger("scan")


class ScanManager:
    """Quan ly cac coin dang duoc theo doi thu cong qua lenh /scan."""

    def __init__(self, cfg: AppConfig, ctx: MarketContext, weights: dict, send_message):
        self.cfg = cfg
        self.ctx = ctx
        self.weights = weights
        self.send_message = send_message
        self._tracked: Dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API - goi tu TelegramBot khi nhan lenh
    # ------------------------------------------------------------------
    def normalize(self, raw: str) -> str:
        s = (raw or "").strip().upper()
        if not s:
            return s
        if not s.endswith(self.cfg.quote_asset):
            s = s + self.cfg.quote_asset
        return s

    def start(self, raw_symbol: str) -> str:
        symbol = self.normalize(raw_symbol)
        if not symbol or len(symbol) < 5:
            return "Ten coin khong hop le. Vi du: /scan BTCUSDT hoac /scan sol"

        with self._lock:
            if symbol in self._tracked:
                interval = self.cfg.scan_update_seconds
                return (f"{symbol} da dang duoc theo doi (cap nhat moi {interval}s). "
                        f"Nhan \"bo {symbol}\" de dung.")
            stop_event = threading.Event()
            self._tracked[symbol] = {"stop": stop_event, "thread": None}

        # Snapshot dau tien: chay ngay (dong bo) de tra loi nguoi dung lien.
        try:
            self._run_once(symbol, first=True)
        except Exception as e:  # noqa: BLE001
            log.warning("Loi /scan lan dau cho %s: %s", symbol, e)
            self.send_message(f"[SCAN] {symbol}: loi khi lay du lieu he thong ({e}). Thu lai sau.")

        interval = self.cfg.scan_update_seconds
        t = threading.Thread(target=self._loop, args=(symbol, stop_event, interval), daemon=True)
        with self._lock:
            entry = self._tracked.get(symbol)
            if entry is not None:
                entry["thread"] = t
        t.start()
        return (f"Da bat dau theo doi {symbol}, se cap nhat moi {interval}s "
                f"({interval // 60} phut). Nhan \"bo {symbol}\" khi muon dung.")

    def stop(self, raw_symbol: str) -> str:
        symbol = self.normalize(raw_symbol)
        with self._lock:
            entry = self._tracked.pop(symbol, None)
        if not entry:
            return f"{symbol} hien khong nam trong danh sach dang duoc /scan theo doi."
        entry["stop"].set()
        return f"Da dung theo doi {symbol}."

    def list_tracked(self) -> str:
        with self._lock:
            syms = list(self._tracked.keys())
        if not syms:
            return "Hien khong co coin nao dang duoc /scan theo doi."
        return "Dang theo doi (/scan): " + ", ".join(syms)

    def stop_all(self) -> None:
        with self._lock:
            entries = list(self._tracked.values())
            self._tracked.clear()
        for entry in entries:
            entry["stop"].set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _loop(self, symbol: str, stop_event: threading.Event, interval: int) -> None:
        while not stop_event.wait(interval):
            try:
                self._run_once(symbol, first=False)
            except Exception as e:  # noqa: BLE001
                log.warning("Loi /scan cap nhat cho %s: %s", symbol, e)

    def _run_once(self, symbol: str, first: bool) -> None:
        ctx = self.ctx
        ctx.ensure_symbol(symbol)
        # Dung lai DUNG pipeline he thong (khong tinh entry rieng):
        # is_core=True, is_ws_tracked=True -> lay day du module (tape/book/HTF/
        # funding/OI/LSR...) qua REST ngay tai thoi diem goi.
        features = build_features(symbol, self.cfg, ctx, is_core=True, is_ws_tracked=True)
        result = compute_composite(features, self.weights, self.cfg)
        result["symbol"] = symbol
        entry_info = classify_entry(features, result, self.weights)
        sl, tp = suggested_sl_tp(
            entry_info["entry_price"], result["direction"], features.get("atr15m", 0.0))
        advice = self._advice(features, result, entry_info, sl, tp)
        msg = self._format(symbol, features, result, entry_info, sl, tp, advice, first)
        self.send_message(msg)

    def _advice(self, f: dict, result: dict, entry_info: dict, sl: float, tp: float) -> str:
        """De xuat GONG/CAT. Dua tren: con huong ro rang khong, score con tren
        threshold he thong khong, HTF con dong thuan khong, va gia da di duoc bao
        nhieu % quang duong tu SL toi TP so voi gia hien tai."""
        direction = result.get("direction", "neutral")
        last_price = f.get("last_price", 0.0)

        if direction == "neutral" or result.get("veto"):
            reason = result.get("veto_reason") or "chua co huong ro rang"
            return f"Chua co vi the ro rang luc nay ({reason}) - chua co gi de GONG/CAT."

        if direction == "long":
            total_range = tp - sl
            progress = (last_price - sl) / total_range if total_range else 0.5
        else:
            total_range = sl - tp
            progress = (sl - last_price) / total_range if total_range else 0.5
        progress = max(0.0, min(1.0, progress))

        notes = []
        if progress >= 0.75:
            notes.append(
                f"Da di ~{progress * 100:.0f}% quang duong toi TP -> co the GONG tiep, "
                f"nhung nen keo SL ve entry/hoa von de bao ve loi nhuan.")
        elif progress <= 0.25:
            notes.append(
                f"Moi di ~{progress * 100:.0f}% quang duong, con gan vung SL goc -> "
                f"chua vi vang gi de CAT, giu theo ke hoach ban dau.")
        else:
            notes.append(
                f"Dang o giua duong (~{progress * 100:.0f}%) -> GONG theo SL/TP ban dau, "
                f"chua co tin hieu can hanh dong gap.")

        if result["score"] < self.cfg.threshold:
            notes.append(
                f"Score hien tai {result['score']:.1f} da xuong duoi threshold he thong "
                f"({self.cfg.threshold}) -> tin hieu dang YEU DI, can trong neu dinh GONG them.")

        htf_ok = (f.get("bias_15m") == direction) and (f.get("bias_1h") == direction)
        if not htf_ok:
            notes.append(
                "HTF 15m/1h khong con dong thuan voi huong ban dau -> canh bao kha nang "
                "dao chieu, uu tien nghieng ve CAT giam rui ro.")

        return " ".join(notes)

    def _format(self, symbol: str, f: dict, result: dict, entry_info: dict,
                sl: float, tp: float, advice: str, first: bool) -> str:
        tag = "snapshot dau tien" if first else "cap nhat dinh ky"
        direction = result["direction"].upper()
        vp = f.get("volume_profile", {})
        lsr = f.get("long_short_ratio")
        lsr_str = f"{lsr:.2f}" if lsr is not None else "n/a"
        reasons = "\n".join(f"- {r}" for r in result.get("reasons", []))

        lines = [
            f"<b>[SCAN] {symbol}</b> ({tag})",
            f"Huong: {direction} | Score: {result['score']:.1f} "
            f"(threshold he thong: {self.cfg.threshold}) | Confidence: {result['confidence']:.2f}",
            f"Regime: {f.get('regime')} | Gia hien tai: {f.get('last_price', 0):.6g}",
            f"HTF 15m/1h/4h: {f.get('bias_15m')}/{f.get('bias_1h')}/{f.get('bias_4h')}",
        ]

        if result["direction"] != "neutral" and not result.get("veto"):
            lines.append(f"Entry: {entry_info['entry_price']:.6g} {entry_info['entry_type']}")
            lines.append(f"SL: {sl:.6g} | TP: {tp:.6g}")
            lines.append(f"Ly do entry: {entry_info['reason']}")

        lines.append(
            f"POC15m: {vp.get('poc', 0):.6g} | CVD5m: {f.get('cvd_5m', 0):.2f} | "
            f"spoof: {f.get('spoof_score', 0):.2f}")
        lines.append(f"Long/Short ratio: {lsr_str}")
        if reasons:
            lines.append(reasons)
        lines.append(f"<b>De xuat:</b> {advice}")
        if first:
            lines.append(f"(Nhan \"bo {symbol}\" de dung cap nhat bat cu luc nao)")

        return "\n".join(lines)
