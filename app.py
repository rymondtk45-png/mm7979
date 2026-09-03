"""
app.py

TelegramBot (gui alert + poll lenh /coinstrong, /scan, bo <symbol>) va
SignalEngine (vong lap chinh).
Chi bao tin hieu qua Telegram. Khong dat lenh tren san.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

import requests

from config import AppConfig, append_jsonl, get_logger, load_weights
from data import MarketContext, UniverseManager, build_features, init_rate_limiter
from signals import (
    classify_entry,
    compute_composite,
    compute_composite_scan,
    rank_top,
    suggested_sl_tp,
)

log = get_logger("app")


class TelegramBot:
    def __init__(self, cfg: AppConfig, universe: UniverseManager,
                 on_scan: Optional[Callable[[str, str], None]] = None,
                 on_unscan: Optional[Callable[[str, str], None]] = None):
        self.cfg = cfg
        self.universe = universe
        self._offset: Optional[int] = None
        self._stop = threading.Event()
        # Callback rieng cho lenh /scan va bo <symbol> - gan tu SignalEngine
        # de TelegramBot khong can biet ve build_features/compute_composite_scan.
        self.on_scan = on_scan
        self.on_unscan = on_unscan

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
        log.info("Da ket noi Telegram OK, bat dau lang nghe lenh /coinstrong, /scan, bo ...")
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
                    elif text.startswith("/scan"):
                        if self.on_scan:
                            self.on_scan(text_raw.strip(), chat_id)
                        else:
                            self.send_message("Lenh /scan chua duoc cau hinh.")
                    elif text.startswith("bo ") or text.startswith("bỏ "):
                        if self.on_unscan:
                            self.on_unscan(text_raw.strip(), chat_id)
                        else:
                            self.send_message('Lenh "bo <symbol>" chua duoc cau hinh.')
                    elif text.startswith("/start"):
                        self.send_message(
                            "Bot bao tin hieu MM/quy da san sang.\n"
                            "Dung /coinstrong on|off|status de dieu khien vu tru quet.\n"
                            "Dung /scan <symbol> de theo doi rieng 1 coin (cap nhat moi "
                            f"{self.cfg.scan_update_seconds}s), va 'bo <symbol>' de dung.")
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


def format_alert(f: dict, result: dict, entry_info: dict, sl: float, tp: float) -> str:
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
        f"SL: {sl:.6g} | TP: {tp:.6g}\n"
        f"Ly do entry: {entry_info['reason']}\n"
        f"HTF 15m/1h/4h: {f['bias_15m']}/{f['bias_1h']}/{f['bias_4h']}\n"
        f"POC15m: {vp.get('poc', 0):.6g} | CVD5m: {f['cvd_5m']:.2f} | spoof: {f['spoof_score']:.2f}\n"
        f"Long/Short ratio: {lsr_str}\n"
        f"{reasons}"
    )


def format_scan_alert(symbol: str, f: dict, result: dict, entry_info: dict, sl: float, tp: float,
                       is_first: bool, cfg: AppConfig) -> str:
    """
    Format rieng cho lenh /scan. Khac format_alert() o cho: /scan dung
    compute_composite_scan() (bo qua veto HTF/mixed-vote), nen LUON co the
    hien mot huong + entry/SL/TP thay vi NEUTRAL/score=0 khi bi veto - kem
    canh bao ro rang khi huong do dang duoc "ep" tu module manh nhat thay vi
    dong thuan day du, de nguoi dung tu can nhac rui ro.
    """
    tag = "(snapshot dau tien)" if is_first else "(cap nhat)"
    direction = result["direction"].upper()
    vp = f.get("volume_profile", {})
    lsr = f.get("long_short_ratio")
    lsr_str = f"{lsr:.2f}" if lsr is not None else "n/a"

    lines = [
        f"<b>[SCAN] {symbol}</b> {tag}",
        f"Huong: <b>{direction}</b> | Score: {result['score']:.1f} "
        f"(threshold he thong: {cfg.threshold:.1f}) | Confidence: {result['confidence']:.2f}",
        f"Regime: {f.get('regime', '')} | Gia hien tai: {f.get('last_price', 0):.6g}",
        f"HTF 15m/1h/4h: {f.get('bias_15m')}/{f.get('bias_1h')}/{f.get('bias_4h')}",
        f"POC15m: {vp.get('poc', 0):.6g} | CVD5m: {f.get('cvd_5m', 0):.2f} | "
        f"spoof: {f.get('spoof_score', 0):.2f}",
        f"Long/Short ratio: {lsr_str}",
    ]

    if result.get("htf_bypassed"):
        lines.append(f"- {result.get('htf_reason', '')} (rieng /scan: BO QUA veto nay)")
    if result.get("mixed_votes"):
        lines.append("- canh bao: dong thuan module con mong (vote long/short chenh <=1)")

    if result["direction"] == "neutral":
        lines.append("De xuat: chua module nao du du lieu de nghieng huong - chua co gi de GONG/CAT.")
    else:
        entry_type = entry_info["entry_type"]
        entry_price = entry_info["entry_price"]
        entry_line = (
            f"Entry: {entry_price:.6g} MARKET (vao ngay)" if entry_type == "MARKET"
            else f"Entry: {entry_price:.6g} LIMIT (cho khop, gia hien tai {f.get('last_price', 0):.6g})"
        )
        risk_note = ""
        if result.get("htf_bypassed") or result.get("forced_by_strength"):
            risk_note = (" [luu y: huong nay dang duoc suy tu module manh nhat / bo qua veto HTF "
                         "- RUI RO CAO HON tin hieu alert tu dong, can size nho hon]")
        lines.append(f"De xuat: {entry_line}{risk_note}")
        lines.append(f"SL: {sl:.6g} | TP: {tp:.6g} | Ly do: {entry_info['reason']}")

    lines.append(f'(Nhan "bo {symbol}" de dung cap nhat bat cu luc nao)')
    return "\n".join(lines)


class SignalEngine:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.weights = load_weights()
        self.universe = UniverseManager(cfg)
        self.ctx = MarketContext(cfg)
        self.bot = TelegramBot(cfg, self.universe,
                                on_scan=self.handle_scan_command,
                                on_unscan=self.handle_unscan_command)
        self.cooldowns: Dict[str, float] = {}
        self.active_signals: Dict[str, dict] = {}
        # symbol -> {"chat_id": str, "next_update": float}
        self.scan_subscriptions: Dict[str, dict] = {}
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        init_rate_limiter(cfg)

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

    # ----------------------------------------------------------------
    # Lenh /scan <symbol> va "bo <symbol>"
    # ----------------------------------------------------------------
    def _resolve_symbol(self, query: str, candidates: Optional[List[str]] = None) -> Optional[str]:
        """Chuan hoa input nguoi dung (vd 'Sushi', 'zkp', 'ZKPUSDT') thanh dung
        symbol Binance Futures. candidates=None -> tim trong toan bo vu tru;
        candidates=list -> chi tim trong danh sach do (dung khi unsub, uu tien
        khop voi cac symbol dang duoc theo doi)."""
        raw = "".join(ch for ch in query.strip().upper() if ch.isalnum())
        if not raw:
            return None
        pool = candidates if candidates is not None else list(self.universe.symbols_info.keys())
        if not pool:
            return None
        if raw in pool:
            return raw
        quote = self.cfg.quote_asset
        if not raw.endswith(quote) and (raw + quote) in pool:
            return raw + quote
        # fuzzy: symbol bat dau bang raw (vd 'SUSHI' -> 'SUSHIUSDT'), chon ngan nhat
        starts = sorted((s for s in pool if s.startswith(raw)), key=len)
        if starts:
            return starts[0]
        # fuzzy nguoc: raw chua ten symbol (vd query = 'ZKPUSDT' con pool chi co base 'ZKP')
        contains = sorted((s for s in pool if raw.startswith(s)), key=len, reverse=True)
        if contains:
            return contains[0]
        return None

    def handle_scan_command(self, text_raw: str, chat_id: str) -> None:
        parts = text_raw.split()
        if len(parts) < 2:
            self.bot.send_message('Cu phap: /scan <symbol>  (vi du: /scan ZKPUSDT hoac /scan Sushi)')
            return
        query = parts[1]
        if not self.universe.symbols_info:
            self.universe.refresh(force=True)
        resolved = self._resolve_symbol(query)
        if not resolved:
            self.bot.send_message(f'Khong tim thay symbol khop voi "{query}" tren Binance Futures.')
            return
        with self._state_lock:
            already = resolved in self.scan_subscriptions
            over_limit = (not already) and len(self.scan_subscriptions) >= self.cfg.scan_max_subscriptions
        if over_limit:
            self.bot.send_message(
                f"Da dat gioi han {self.cfg.scan_max_subscriptions} symbol dang /scan cung luc. "
                f'Dung "bo <symbol>" de bo bot truoc khi them symbol moi.')
            return
        if already:
            self.bot.send_message(f"{resolved} dang duoc theo doi roi (cap nhat moi "
                                   f"{self.cfg.scan_update_seconds}s).")
            return
        # Chay snapshot dau tien trong thread rieng de khong chan vong poll Telegram.
        threading.Thread(target=self._scan_snapshot_and_register,
                          args=(resolved, chat_id, True), daemon=True).start()

    def handle_unscan_command(self, text_raw: str, chat_id: str) -> None:
        parts = text_raw.split()
        if len(parts) < 2:
            self.bot.send_message('Cu phap: bo <symbol>  (vi du: bo ZKPUSDT)')
            return
        query = parts[1]
        with self._state_lock:
            current = list(self.scan_subscriptions.keys())
        resolved = self._resolve_symbol(query, candidates=current) or self._resolve_symbol(query)
        with self._state_lock:
            removed = resolved is not None and self.scan_subscriptions.pop(resolved, None) is not None
        if removed:
            self.bot.send_message(f"Da dung theo doi {resolved}.")
        else:
            self.bot.send_message(f'Khong tim thay "{query}" trong danh sach dang /scan.')

    def _scan_snapshot_and_register(self, symbol: str, chat_id: str, is_first: bool) -> None:
        """Build features + compute_composite_scan() (bo qua veto HTF, lay
        module manh nhat khi khong co huong tong ro rang) cho 1 symbol, gui
        ket qua qua Telegram. Neu is_first=True, chi dang ky theo doi dinh ky
        SAU KHI gui snapshot dau tien thanh cong."""
        try:
            is_core = symbol in self.cfg.core_symbols
            is_ws = symbol in set(self.universe.ws_symbols())
            features = build_features(symbol, self.cfg, self.ctx, is_core, is_ws)
            result = compute_composite_scan(features, self.weights, self.cfg)
            entry_info = classify_entry(features, result, self.weights)
            sl, tp = suggested_sl_tp(entry_info["entry_price"], result["direction"], features["atr15m"])
            msg = format_scan_alert(symbol, features, result, entry_info, sl, tp, is_first, self.cfg)
            self.bot.send_message(msg)
            append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                "ts": time.time(), "symbol": symbol, "event": "scan_snapshot",
                "direction": result["direction"], "score": result["score"],
                "htf_bypassed": result.get("htf_bypassed", False),
                "forced_by_strength": result.get("forced_by_strength", False),
            })
        except Exception as e:  # noqa: BLE001
            log.warning("Loi /scan cho %s: %s", symbol, e)
            if is_first:
                self.bot.send_message(f"Loi khi lay du lieu cho {symbol}, thu lai sau.")
            return
        if is_first:
            with self._state_lock:
                self.scan_subscriptions[symbol] = {
                    "chat_id": chat_id, "next_update": time.time() + self.cfg.scan_update_seconds,
                }
            log.info("Bat dau /scan theo doi %s (cap nhat moi %ss)", symbol, self.cfg.scan_update_seconds)

    def _check_scan_updates(self) -> None:
        now = time.time()
        with self._state_lock:
            due = [sym for sym, meta in self.scan_subscriptions.items() if now >= meta["next_update"]]
            for sym in due:
                self.scan_subscriptions[sym]["next_update"] = now + self.cfg.scan_update_seconds
        for sym in due:
            chat_id = self.scan_subscriptions.get(sym, {}).get("chat_id", "")
            threading.Thread(target=self._scan_snapshot_and_register,
                              args=(sym, chat_id, False), daemon=True).start()

    # ----------------------------------------------------------------
    # Vong lap alert chinh (khong doi so voi ban goc)
    # ----------------------------------------------------------------
    def _check_hits_and_expiry(self, current_prices: Dict[str, float]) -> None:
        now = time.time()
        expired = []
        for sym, sig in list(self.active_signals.items()):
            price = current_prices.get(sym)
            if price is None:
                continue
            direction = sig["direction"]
            if not sig["filled"]:
                # Kèo LIMIT: chua khop, chi theo doi xem gia da cham entry_price
                # chua truoc khi bat dau tinh TP/SL. MARKET thi filled=True ngay
                # tu luc tao nen khong bao gio vao nhanh nay.
                touched = ((direction == "long" and price <= sig["entry_price"])
                           or (direction == "short" and price >= sig["entry_price"]))
                if touched:
                    sig["filled"] = True
                    self.bot.send_message(
                        f"<b>{sym}</b> {direction.upper()} LIMIT da khop tai {price:.6g}")
                    append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                        "ts": now, "symbol": sym, "event": "limit_filled", "price": price,
                    })
                elif now - sig["created_at"] > self.cfg.signal_ttl_seconds:
                    # Limit cho qua lau khong khop -> huy, khong tinh la mot lenh da vao.
                    # FIX: truoc day chi ghi log jsonl, nguoi dung khong biet kèo da
                    # het han ma khong bao gio khop -> gio bao qua Telegram.
                    self.bot.send_message(
                        f"<b>{sym}</b> {direction.upper()} LIMIT <b>HET HAN</b> sau "
                        f"{self.cfg.signal_ttl_seconds}s, chua khop tai {sig['entry_price']:.6g} "
                        f"(gia hien tai {price:.6g}) - bo qua kèo nay.")
                    append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                        "ts": now, "symbol": sym, "event": "limit_expired_unfilled",
                    })
                    expired.append(sym)
                if not sig["filled"]:
                    continue
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
                # FIX: truoc day het TTL ma chua cham SL/TP thi bi am tham xoa
                # khoi active_signals, khong bao gi ca -> nguoi dung tuong kèo
                # con "song". Gio bao ro la het han theo doi, khong con canh
                # bao gi them (khong phai la SL/TP that su, chi la het thoi
                # gian bot theo doi).
                self.bot.send_message(
                    f"<b>{sym}</b> {direction.upper()} <b>HET HAN theo doi</b> sau "
                    f"{self.cfg.signal_ttl_seconds}s tu luc vao, chua cham SL/TP "
                    f"(gia hien tai {price:.6g}) - bot ngung theo doi kèo nay.")
                append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                    "ts": now, "symbol": sym, "event": "signal_ttl_expired", "price": price,
                })
                expired.append(sym)
        for sym in expired:
            self.active_signals.pop(sym, None)

    def _check_signal_health(self, results: List[dict]) -> None:
        """Tinh nang MOI: moi vong quet, doi voi cac kèo dang active_signals,
        neu symbol do nam trong ket qua quet vong nay (results), so lai
        huong/veto MOI NHAT voi huong luc vao kèo. Neu tinh hinh xau di ro
        ret (bi veto tro lai, hoac huong dao chieu) thi BAO 1 LAN qua
        Telegram "tin hieu xau - can nhac bo kèo". Neu van tot (cung huong,
        khong bi veto) thi IM LANG - khong nhan tin gi ca, tranh spam.

        Khong tu dong xoa kèo khoi active_signals hay huy SL/TP - nguoi dung
        van tu quyet dinh (dung theo triet ly "KHONG DAT LENH" cua bot).
        Neu sau khi bi canh bao ma tinh hinh hoi phuc tro lai (cung huong,
        khong veto) thi am tham bo canh bao (khong nhan tin "da tot lai"
        de tranh spam 2 chieu, dung y "neu tot thi khong noi gi het").

        Chi re-check duoc cho symbol nao ro rang co mat trong scan set vong
        nay (results) - alt coin nao rot khoi scan set (vd het hot, khong
        con trong top volume) se khong duoc re-check vong do, van tiep tuc
        theo doi SL/TP/TTL binh thuong o _check_hits_and_expiry.
        """
        if not self.cfg.enable_signal_health_alert:
            return
        fresh_by_symbol = {r["symbol"]: r for r in results}
        for sym, sig in list(self.active_signals.items()):
            fresh = fresh_by_symbol.get(sym)
            if fresh is None:
                continue
            orig_dir = sig["direction"]
            fresh_dir = fresh["direction"]
            turned_bad = fresh.get("veto") or fresh_dir == "neutral" or fresh_dir != orig_dir
            if turned_bad and not sig.get("warned_bad"):
                reason = fresh.get("veto_reason") or "huong da dao chieu / khong con ro rang"
                self.bot.send_message(
                    f"⚠️ <b>{sym}</b> {orig_dir.upper()} - <b>TIN HIEU XAU DI</b>, can nhac bo kèo.\n"
                    f"Luc vao: {orig_dir.upper()} | Quet lai vua roi: "
                    f"{fresh_dir.upper() if fresh_dir != 'neutral' else 'NEUTRAL'} "
                    f"(score={fresh.get('score', 0):.1f})\n"
                    f"Ly do: {reason}\n"
                    f"(Bot van tiep tuc theo doi SL/TP nhu binh thuong, day chi la canh bao "
                    f"tham khao - ban tu quyet dinh giu hay dong kèo.)")
                append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                    "ts": time.time(), "symbol": sym, "event": "signal_turned_bad",
                    "orig_direction": orig_dir, "fresh_direction": fresh_dir,
                    "fresh_score": fresh.get("score", 0), "reason": reason,
                })
                sig["warned_bad"] = True
            elif not turned_bad and sig.get("warned_bad"):
                # Hoi phuc tro lai - im lang bo canh bao, khong nhan tin gi them.
                sig["warned_bad"] = False

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
                sl, tp = suggested_sl_tp(entry_price, result["direction"], features["atr15m"])
                self.bot.send_message(format_alert(features, result, entry_info, sl, tp))
                append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                    "ts": now, "symbol": symbol, "direction": result["direction"],
                    "score": result["score"], "entry_type": entry_info["entry_type"],
                    "entry": entry_price, "sl": sl, "tp": tp,
                })
                self.cooldowns[symbol] = now
                self.active_signals[symbol] = {
                    "direction": result["direction"], "entry_price": entry_price,
                    "entry_type": entry_info["entry_type"], "sl": sl, "tp": tp,
                    "created_at": now, "filled": entry_info["entry_type"] == "MARKET",
                }

        self._check_hits_and_expiry(current_prices)
        self._check_signal_health(results)
        self._check_scan_updates()

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
