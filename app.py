"""
app.py
TelegramBot (gui alert + poll lenh /coinstrong) va SignalEngine (vong lap chinh).
Chi bao tin hieu qua Telegram. Khong dat lenh tren san.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import requests

from config import AppConfig, append_jsonl, get_logger, load_weights
from data import MarketContext, UniverseManager, build_features, init_rate_limiter
from signals import classify_entry, compute_composite, rank_top, suggested_sl_tp_multi

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
        if not self.cfg.telegram_chat_id:
            log.warning("TELEGRAM_CHAT_ID rong -> khong gui duoc tin nhan: %s",
                        text.replace("\n", " | "))
            return
        try:
            resp = requests.post(self._api("sendMessage"), data={
                "chat_id": self.cfg.telegram_chat_id, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }, timeout=8)
            data = resp.json()
            if not data.get("ok"):
                log.warning("Telegram sendMessage tra ve loi: %s", data)
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram send loi: %s", e)

    def poll_commands_forever(self) -> None:
        if not self.cfg.enable_telegram:
            log.warning("ENABLE_TELEGRAM=False -> khong lang nghe lenh Telegram.")
            return
        if not self.cfg.telegram_bot_token:
            log.warning("TELEGRAM_BOT_TOKEN rong -> khong lang nghe lenh Telegram. "
                        "Kiem tra bien moi truong tren Railway (tab Variables).")
            return

        ok = self._verify_bot_token()
        if not ok:
            log.error("TELEGRAM_BOT_TOKEN khong hop le (Telegram tra ve loi khi goi getMe). "
                      "Kiem tra lai token.")
            return

        log.info("Da ket noi Telegram OK, bat dau lang nghe lenh /coinstrong ...")
        while not self._stop.is_set():
            try:
                resp = requests.get(self._api("getUpdates"), params={
                    "timeout": 20, "offset": self._offset,
                }, timeout=25)
                data = resp.json()
                if not data.get("ok", True):
                    log.warning("Telegram getUpdates tra ve loi: %s", data)
                    time.sleep(3)
                    continue
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    msg = upd.get("message", {})
                    text_raw = msg.get("text") or ""
                    text = text_raw.strip().lower()
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    log.info("Nhan tin nhan Telegram tu chat_id=%s: %r", chat_id, text_raw)
                    if text.startswith("/coinstrong"):
                        self._handle_coinstrong(text)
                    elif text.startswith("/start"):
                        self.send_message(
                            "Bot bao tin hieu MM/quy da san sang.\n"
                            "Dung /coinstrong on|off|status de dieu khien vu tru quet.")
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram poll loi: %s", e)
                time.sleep(3)

    def _verify_bot_token(self) -> bool:
        try:
            resp = requests.get(self._api("getMe"), timeout=8)
            data = resp.json()
            if data.get("ok"):
                log.info("Telegram bot xac thuc OK: @%s", data["result"].get("username"))
                return True
            log.error("Telegram getMe that bai: %s", data)
            return False
        except Exception as e:  # noqa: BLE001
            log.error("Khong the ket noi Telegram API de xac thuc token: %s", e)
            return False

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


def format_alert(f: dict, result: dict, entry_info: dict, sltp: dict) -> str:
    """sltp: dict co cac khoa sl, tp1, tp2, tp3 (xem signals.suggested_sl_tp_multi)."""
    direction = result["direction"].upper()
    entry_type = entry_info["entry_type"]
    entry_price = entry_info["entry_price"]
    vp = f.get("volume_profile", {})
    reasons = "\n".join(f"• {r}" for r in result["reasons"])
    lsr = f.get("long_short_ratio")
    lsr_str = f"{lsr:.2f}" if lsr is not None else "n/a"

    entry_line = (
        f"Entry: {entry_price:.6g} MARKET (vao ngay)" if entry_type == "MARKET"
        else f"Entry: {entry_price:.6g} LIMIT (cho khop, gia hien tai {f['last_price']:.6g})"
    )

    return (
        f"<b>{f['symbol']} {direction}</b>\n"
        f"Module | Regime: {f['regime']}\n"
        f"Score: {result['score']:.1f} | Confidence: {result['confidence']:.2f}\n"
        f"{entry_line}\n"
        f"SL: {sltp['sl']:.6g}\n"
        f"TP1: {sltp['tp1']:.6g} (chot 50%, SL -> breakeven)\n"
        f"TP2: {sltp['tp2']:.6g} (chot them 30%, SL -> TP1, bat dau trailing)\n"
        f"TP3: {sltp['tp3']:.6g} (muc tieu xa, hoac trailing bat kip truoc do)\n"
        f"Ly do entry: {entry_info['reason']}\n"
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
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        init_rate_limiter(cfg)

        # Persist active_signals xuong dia de khong mat theo doi TP/SL khi bot
        # bi restart (vi du Railway redeploy) - quan trong vi TTL gio dai (ngay)
        # nen kha nang bot restart giua chung 1 tin hieu la co that.
        self._state_path = self.cfg.resolve_path("logs/active_signals.json")
        self.active_signals: Dict[str, dict] = self._load_active_signals()
        if self.active_signals:
            log.info("Da khoi phuc %d tin hieu dang theo doi tu %s",
                      len(self.active_signals), self._state_path)

    def _load_active_signals(self) -> Dict[str, dict]:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                log.warning("Khong doc duoc active_signals.json (%s) -> bat dau rong", e)
        return {}

    def _save_active_signals(self) -> None:
        try:
            self._state_path.write_text(
                json.dumps(self.active_signals, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Khong luu duoc active_signals.json: %s", e)

    def start(self) -> None:
        self.universe.refresh(force=True)
        # ws_symbols() khong truyen max_extra -> dung cfg.ws_cover_all (mac dinh
        # True = phu toan bo scan set, khong cap 15 nhu truoc).
        ws_syms = self.universe.ws_symbols()
        log.info("WS se theo doi %d/%d cap trong scan set", len(ws_syms), len(self.universe.get_scan_set()))
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
        """
        Theo doi tin hieu dang active qua 3 tang TP + trailing stop sau TP2:
          - Chua khop (LIMIT): cho gia cham entry_price, het TTL ma chua khop -> huy.
          - SL cham (o bat ky stage nao, ke ca da doi len breakeven/TP1/trailing)
            -> dong tin hieu, log kem stage de biet da chot duoc bao nhieu truoc do.
          - stage 0 -> 1: cham TP1 -> chot mot phan, doi SL ve entry (breakeven).
          - stage 1 -> 2: cham TP2 -> chot them, doi SL len TP1 (khoa loi), tu day
            SL duoc trailing theo ATR4h thay vi co dinh.
          - stage >= 2: cap nhat trailing stop moi vong lap; cham TP3 -> dong het.
          - Het TTL o bat ky stage nao -> bao trang thai hien tai roi dong, khong
            de tin hieu "bien mat" trong im lang.
        """
        now = time.time()
        expired: List[str] = []

        for sym, sig in list(self.active_signals.items()):
            price = current_prices.get(sym)
            if price is None:
                continue
            direction = sig["direction"]
            long = direction == "long"

            if not sig["filled"]:
                touched = (price <= sig["entry_price"]) if long else (price >= sig["entry_price"])
                if touched:
                    sig["filled"] = True
                    self.bot.send_message(
                        f"<b>{sym}</b> {direction.upper()} LIMIT da khop tai {price:.6g}")
                    append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                        "ts": now, "symbol": sym, "event": "limit_filled", "price": price,
                    })
                elif now - sig["created_at"] > self.cfg.signal_ttl_seconds:
                    append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                        "ts": now, "symbol": sym, "event": "limit_expired_unfilled",
                    })
                    expired.append(sym)
                continue

            # --- SL (co the da doi len breakeven / TP1 / dang trailing) ---
            hit_sl = (price <= sig["sl"]) if long else (price >= sig["sl"])
            if hit_sl:
                self.bot.send_message(
                    f"<b>{sym}</b> {direction.upper()} da cham SL tai {price:.6g} "
                    f"(stage={sig['stage']})")
                append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                    "ts": now, "symbol": sym, "event": f"hit_SL_stage{sig['stage']}", "price": price,
                })
                expired.append(sym)
                continue

            stage = sig["stage"]

            if stage < 1:
                hit_tp1 = (price >= sig["tp1"]) if long else (price <= sig["tp1"])
                if hit_tp1:
                    sig["stage"] = 1
                    sig["sl"] = sig["entry_price"]
                    self.bot.send_message(
                        f"<b>{sym}</b> {direction.upper()} cham TP1 tai {price:.6g} -> "
                        f"chot {self.cfg.tp1_close_pct * 100:.0f}%, doi SL ve breakeven "
                        f"({sig['sl']:.6g})")
                    append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                        "ts": now, "symbol": sym, "event": "hit_TP1", "price": price,
                    })
                    self._save_active_signals()
                    continue

            if stage < 2:
                hit_tp2 = (price >= sig["tp2"]) if long else (price <= sig["tp2"])
                if hit_tp2:
                    sig["stage"] = 2
                    sig["sl"] = sig["tp1"]
                    self.bot.send_message(
                        f"<b>{sym}</b> {direction.upper()} cham TP2 tai {price:.6g} -> "
                        f"chot them {self.cfg.tp2_close_pct * 100:.0f}%, doi SL len TP1 "
                        f"({sig['sl']:.6g}), bat dau trailing")
                    append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                        "ts": now, "symbol": sym, "event": "hit_TP2", "price": price,
                    })
                    self._save_active_signals()
                    continue

            if stage >= 2:
                trail_dist = self.cfg.trail_atr4h_mult * sig.get("atr4h", 0.0)
                if trail_dist:
                    if long:
                        new_sl = price - trail_dist
                        if new_sl > sig["sl"]:
                            sig["sl"] = new_sl
                    else:
                        new_sl = price + trail_dist
                        if new_sl < sig["sl"]:
                            sig["sl"] = new_sl

                hit_tp3 = (price >= sig["tp3"]) if long else (price <= sig["tp3"])
                if hit_tp3:
                    self.bot.send_message(
                        f"<b>{sym}</b> {direction.upper()} cham TP3 tai {price:.6g} -> "
                        f"chot toan bo")
                    append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                        "ts": now, "symbol": sym, "event": "hit_TP3", "price": price,
                    })
                    expired.append(sym)
                    continue

            if now - sig["created_at"] > self.cfg.signal_ttl_seconds:
                self.bot.send_message(
                    f"<b>{sym}</b> {direction.upper()} het TTL o stage {sig['stage']}, "
                    f"gia hien tai {price:.6g}")
                append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                    "ts": now, "symbol": sym, "event": f"ttl_expired_stage{sig['stage']}",
                    "price": price,
                })
                expired.append(sym)

        for sym in expired:
            self.active_signals.pop(sym, None)
        self._save_active_signals()

    def _process_symbol(self, symbol: str, is_core: bool, is_ws: bool) -> Optional[dict]:
        """Chay trong 1 worker thread. Tra ve result dict hoac None neu loi.
        Khong dung chung state (cooldowns/active_signals) o day de tranh race
        condition - xu ly state o thread chinh sau khi thu thap xong."""
        try:
            features = build_features(symbol, self.cfg, self.ctx, is_core, is_ws)
            append_jsonl(self.cfg.resolve_path(self.cfg.feature_log_path), features)
            result = compute_composite(features, self.weights, self.cfg)
            result["symbol"] = symbol
            result["features"] = features
            return result
        except Exception as e:  # noqa: BLE001
            log.warning("Loi xu ly symbol %s: %s", symbol, e)
            return None

    def run_once(self) -> List[dict]:
        self.universe.refresh(force=False)
        scan_set = self.universe.get_scan_set()
        ws_set = set(self.universe.ws_symbols())
        core_set = set(self.cfg.core_symbols)

        results: List[dict] = []
        current_prices: Dict[str, float] = {}

        # Da luong co kiem soat: WeightLimiter (trong data.py) la nut that chung
        # cho moi worker, dam bao tong weight/phut khong vuot ngan sach du chay
        # bao nhieu thread. max_workers chi anh huong toc do "xep hang", khong
        # anh huong toc do "goi API thuc te" - an toan de tang len khi can.
        with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as pool:
            futures = {
                pool.submit(self._process_symbol, symbol, symbol in core_set, symbol in ws_set): symbol
                for symbol in scan_set
            }
            for future in as_completed(futures):
                result = future.result()
                if result is None:
                    continue
                results.append(result)
                current_prices[result["symbol"]] = result["features"]["last_price"]

        # Xu ly alert/cooldown/active_signals tuan tu o thread chinh (tranh race).
        now = time.time()
        for result in results:
            symbol = result["symbol"]
            features = result["features"]
            on_cooldown = (now - self.cooldowns.get(symbol, 0)) < self.cfg.alert_cooldown_seconds
            if (not result["veto"] and result["direction"] != "neutral"
                    and result["score"] >= self.cfg.threshold and not on_cooldown):
                entry_info = classify_entry(features, result, self.weights)
                entry_price = entry_info["entry_price"]

                # atr4h gio da duoc data.py.build_features() cung cap that (tinh
                # tu klines_4h da fetch san cho bias_4h, khong ton them API call).
                atr4h = features.get("atr4h", 0.0)

                sltp = suggested_sl_tp_multi(
                    entry_price, result["direction"], features["atr15m"], atr4h, self.cfg)

                self.bot.send_message(format_alert(features, result, entry_info, sltp))
                append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                    "ts": now, "symbol": symbol, "direction": result["direction"],
                    "score": result["score"], "entry_type": entry_info["entry_type"],
                    "entry": entry_price, **sltp,
                })
                self.cooldowns[symbol] = now
                self.active_signals[symbol] = {
                    "direction": result["direction"], "entry_price": entry_price,
                    "entry_type": entry_info["entry_type"],
                    "sl": sltp["sl"], "tp1": sltp["tp1"], "tp2": sltp["tp2"], "tp3": sltp["tp3"],
                    "atr4h": atr4h, "stage": 0,
                    "created_at": now, "filled": entry_info["entry_type"] == "MARKET",
                }
                self._save_active_signals()

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
