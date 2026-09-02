"""
app.py
TelegramBot (gui alert + poll lenh /coinstrong) va SignalEngine (vong lap chinh).
Chi bao tin hieu qua Telegram. Khong dat lenh tren san.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

import requests

from config import AppConfig, append_jsonl, get_logger, load_weights
from data import MarketContext, UniverseManager, build_features
from signals import compute_composite, rank_top, suggested_sl_tp

log = get_logger("app")


class TelegramBot:
    def __init__(self, cfg: AppConfig, universe: UniverseManager):
        self.cfg = cfg
        self.universe = universe
        self._offset: Optional[int] = None
        self._stop = threading.Event()

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/{method}"

    def send_message(self, text: str) -> None:
        if not self.cfg.enable_telegram or not self.cfg.telegram_bot_token:
            log.info("[TELEGRAM-DISABLED] %s", text.replace("\n", " | "))
            return
        try:
            requests.post(self._api("sendMessage"), data={
                "chat_id": self.cfg.telegram_chat_id, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }, timeout=8)
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram send loi: %s", e)

    def poll_commands_forever(self) -> None:
        if not self.cfg.enable_telegram or not self.cfg.telegram_bot_token:
            return
        while not self._stop.is_set():
            try:
                resp = requests.get(self._api("getUpdates"), params={
                    "timeout": 20, "offset": self._offset,
                }, timeout=25)
                data = resp.json()
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    msg = upd.get("message", {})
                    text = (msg.get("text") or "").strip().lower()
                    if text.startswith("/coinstrong"):
                        self._handle_coinstrong(text)
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram poll loi: %s", e)
                time.sleep(3)

    def _handle_coinstrong(self, text: str) -> None:
        parts = text.split()
        if len(parts) >= 2 and parts[1] == "on":
            self.universe.set_coinstrong(True)
        elif len(parts) >= 2 and parts[1] == "off":
            self.universe.set_coinstrong(False)
        self.universe.refresh(force=True)
        scan = self.universe.get_scan_set()
        state = "ON" if self.universe.coinstrong else "OFF"
        preview = ", ".join(scan[:15])
        self.send_message(
            f"<b>/coinstrong {state}</b>\n"
            f"So cap dang quet: {len(scan)}\n"
            f"15 symbol dau: {preview}"
        )

    def stop(self) -> None:
        self._stop.set()


def format_alert(f: dict, result: dict) -> str:
    direction = result["direction"].upper()
    entry = f["last_price"]
    sl, tp = suggested_sl_tp(entry, result["direction"], f["atr15m"])
    vp = f.get("volume_profile", {})
    reasons = "\n".join(f"• {r}" for r in result["reasons"])
    lsr = f.get("long_short_ratio")
    lsr_str = f"{lsr:.2f}" if lsr is not None else "n/a"
    return (
        f"<b>{f['symbol']} {direction}</b>\n"
        f"Module | Regime: {f['regime']}\n"
        f"Score: {result['score']:.1f} | Confidence: {result['confidence']:.2f}\n"
        f"Entry: {entry:.6g} | SL: {sl:.6g} | TP: {tp:.6g}\n"
        f"HTF 15m/1h/4h: {f['bias_15m']}/{f['bias_1h']}/{f['bias_4h']}\n"
        f"POC15m: {vp.get('poc', 0):.6g} | CVD5m: {f['cvd_5m']:.2f} | spoof: {f['spoof_score']:.2f}\n"
        f"Long/Short ratio: {lsr_str}\n"
        f"{reasons}"
    )


class SignalEngine:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.weights = load_weights()
        self.universe = UniverseManager(cfg)
        self.ctx = MarketContext(cfg)
        self.bot = TelegramBot(cfg, self.universe)
        self.cooldowns: Dict[str, float] = {}
        self.active_signals: Dict[str, dict] = {}
        self._stop = threading.Event()

    def start(self) -> None:
        self.universe.refresh(force=True)
        ws_syms = self.universe.ws_symbols(max_extra=15)
        self.ctx.start_stream(ws_syms)
        if self.cfg.enable_telegram and self.cfg.telegram_bot_token:
            threading.Thread(target=self.bot.poll_commands_forever, daemon=True).start()
        self.run_forever()

    def stop(self) -> None:
        self._stop.set()
        self.bot.stop()
        if self.ctx.stream:
            self.ctx.stream.stop()

    def _check_hits_and_expiry(self, current_prices: Dict[str, float]) -> None:
        now = time.time()
        expired = []
        for sym, sig in list(self.active_signals.items()):
            price = current_prices.get(sym)
            if price is None:
                continue
            direction = sig["direction"]
            hit = None
            if direction == "long":
                if price <= sig["sl"]:
                    hit = "SL"
                elif price >= sig["tp"]:
                    hit = "TP"
            elif direction == "short":
                if price >= sig["sl"]:
                    hit = "SL"
                elif price <= sig["tp"]:
                    hit = "TP"
            if hit:
                self.bot.send_message(
                    f"<b>{sym}</b> {direction.upper()} da cham <b>{hit}</b> tai {price:.6g}")
                append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                    "ts": now, "symbol": sym, "event": f"hit_{hit}", "price": price,
                })
                expired.append(sym)
            elif now - sig["created_at"] > self.cfg.signal_ttl_seconds:
                expired.append(sym)
        for sym in expired:
            self.active_signals.pop(sym, None)

    def run_once(self) -> List[dict]:
        self.universe.refresh(force=False)
        scan_set = self.universe.get_scan_set()
        ws_set = set(self.universe.ws_symbols(max_extra=15))
        core_set = set(self.cfg.core_symbols)

        results = []
        current_prices: Dict[str, float] = {}
        for symbol in scan_set:
            try:
                is_core = symbol in core_set
                is_ws = symbol in ws_set
                features = build_features(symbol, self.cfg, self.ctx, is_core, is_ws)
                current_prices[symbol] = features["last_price"]
                append_jsonl(self.cfg.resolve_path(self.cfg.feature_log_path), features)

                result = compute_composite(features, self.weights, self.cfg)
                result["symbol"] = symbol
                result["features"] = features
                results.append(result)

                now = time.time()
                on_cooldown = (now - self.cooldowns.get(symbol, 0)) < self.cfg.alert_cooldown_seconds
                if (not result["veto"] and result["direction"] != "neutral"
                        and result["score"] >= self.cfg.threshold and not on_cooldown):
                    entry = features["last_price"]
                    sl, tp = suggested_sl_tp(entry, result["direction"], features["atr15m"])
                    self.bot.send_message(format_alert(features, result))
                    append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                        "ts": now, "symbol": symbol, "direction": result["direction"],
                        "score": result["score"], "entry": entry, "sl": sl, "tp": tp,
                    })
                    self.cooldowns[symbol] = now
                    self.active_signals[symbol] = {
                        "direction": result["direction"], "sl": sl, "tp": tp, "created_at": now,
                    }
            except Exception as e:  # noqa: BLE001
                log.warning("Loi xu ly symbol %s: %s", symbol, e)
            time.sleep(0.12)

        self._check_hits_and_expiry(current_prices)

        top5 = rank_top(results, 5)
        if top5:
            lines = [f"{r['symbol']} {r['direction'].upper()} score={r['score']:.1f}" for r in top5]
            log.info("TOP 5: %s", " | ".join(lines))
        return results

    def run_forever(self) -> None:
        while not self._stop.is_set():
            start = time.time()
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001
                log.error("Loi vong lap chinh: %s", e)
            elapsed = time.time() - start
            sleep_left = max(self.cfg.poll_seconds - elapsed, 1.0)
            time.sleep(sleep_left)
