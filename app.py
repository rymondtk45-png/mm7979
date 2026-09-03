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
from data import MarketContext, UniverseManager, build_features, fetch_premium_index, init_rate_limiter
from signals import classify_entry, compute_composite, rank_top, suggested_sl_tp

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


def format_alert(f: dict, result: dict, entry_info: dict, sl_tp: dict) -> str:
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
        f"SL: {sl_tp['sl']:.6g}\n"
        f"TP1: {sl_tp['tp1']:.6g} (1.2R) | TP2: {sl_tp['tp2']:.6g} (2.4R) | TP3: {sl_tp['tp3']:.6g} (4R)\n"
        f"Ly do entry: {entry_info['reason']}\n"
        f"HTF 15m/1h/4h: {f['bias_15m']}/{f['bias_1h']}/{f['bias_4h']}\n"
        f"POC15m: {vp.get('poc', 0):.6g} | CVD5m: {f['cvd_5m']:.2f} | spoof: {f['spoof_score']:.2f}\n"
        f"Long/Short ratio: {lsr_str}\n"
        f"{reasons}"
    )


def _humanize_veto_reason(reason: str) -> str:
    """Dich reason ky thuat sang cau ngan de nguoi dung hieu ngay trong tin nhan."""
    if not reason:
        return "khong con huong ro rang"
    if reason == "mixed votes":
        return "weak consensus (dong thuan giua cac module yeu, vote long/short qua sat nhau)"
    if "HTF conflict" in reason:
        return "HTF conflict (1h va 4h dao chieu nhau)"
    return reason


class SignalEngine:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.weights = load_weights()
        self.universe = UniverseManager(cfg)
        self.ctx = MarketContext(cfg)
        self.bot = TelegramBot(cfg, self.universe)
        self.cooldowns: Dict[str, float] = {}
        # Fix lo hong #1: key theo "SYMBOL#timestamp_ms" (id rieng cho tung tin
        # hieu), KHONG con key theo symbol -> tin hieu moi cho cung 1 coin se
        # KHONG con de mat tin hieu cu dang active (truoc day bi ghi de va
        # "bien mat" khoi theo doi ma khong co thong bao gi).
        self.active_signals: Dict[str, dict] = {}
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        init_rate_limiter(cfg)
        # Fix lo hong #3: khoi phuc active_signals/cooldowns tu lan chay truoc
        # (neu co) ngay khi khoi tao, thay vi luon bat dau tu rong sau moi
        # lan deploy/restart/crash.
        self._load_state()

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

    # ------------------------------------------------------------------
    # Fix lo hong #3: luu/doc active_signals + cooldowns ra dia (JSON)
    # ------------------------------------------------------------------
    def _save_state(self) -> None:
        try:
            path = self.cfg.resolve_path(self.cfg.active_signals_state_path)
            payload = {"active_signals": self.active_signals, "cooldowns": self.cooldowns}
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            tmp.replace(path)  # ghi atomically, tranh file hong neu crash giua chung
        except Exception as e:  # noqa: BLE001
            log.warning("Khong luu duoc active_signals/cooldowns ra dia: %s", e)

    def _load_state(self) -> None:
        try:
            path = self.cfg.resolve_path(self.cfg.active_signals_state_path)
            if not path.exists():
                return
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            self.active_signals = payload.get("active_signals", {}) or {}
            self.cooldowns = payload.get("cooldowns", {}) or {}
            if self.active_signals:
                log.info("Khoi phuc %d tin hieu dang active tu lan chay truoc "
                         "(khong bi mat khi restart/deploy nua)", len(self.active_signals))
        except Exception as e:  # noqa: BLE001
            log.warning("Khong doc duoc trang thai active_signals/cooldowns tu dia: %s", e)

    # ------------------------------------------------------------------
    # Fix lo hong #2: bu gia cho cac symbol dang co tin hieu active nhung
    # KHONG con nam trong scan_set vong nay (rot khoi top volume/hot).
    # ------------------------------------------------------------------
    def _fetch_missing_prices(self, current_prices: Dict[str, float]) -> None:
        active_symbols = {sig["symbol"] for sig in self.active_signals.values()}
        missing = active_symbols - set(current_prices.keys())
        for sym in missing:
            try:
                info = fetch_premium_index(sym)
                mark = info.get("mark") if info else None
                if mark:
                    current_prices[sym] = mark
                else:
                    log.warning("Khong lay duoc gia bu cho %s (tin hieu dang active nhung "
                               "da rot khoi scan set vong nay)", sym)
            except Exception as e:  # noqa: BLE001
                log.warning("Loi fetch gia bu cho %s: %s", sym, e)

    # ------------------------------------------------------------------
    # Canh bao tham khao: re-scan lai tin hieu dang active, KHONG tu dong
    # dong keo, chi bao cho nguoi dung biet thesis co con vung khong.
    # ------------------------------------------------------------------
    def _check_signal_health(self, results_by_symbol: Dict[str, dict]) -> None:
        """Dung ngay ket qua compute_composite() da tinh trong VONG QUET HIEN
        TAI (khong goi them API/tinh them gi ca) cho cac symbol dang co tin
        hieu active va van nam trong scan set vong nay. So huong/score quet
        lai voi huong luc vao:
          - veto (HTF conflict / weak consensus...) hoac mat huong (neutral)
            -> canh bao "TIN HIEU YEU".
          - huong quet lai NGUOC voi huong luc vao -> canh bao "DAO CHIEU".
          - van cung huong, khong veto -> "ok", khong canh bao.
        Chi gui canh bao 1 LAN khi trang thai chuyen tu "ok" sang "khong ok"
        (khong lap lai moi vong neu van dang o trang thai yeu/dao chieu, tranh
        spam). Neu quet lai ve lai "ok" thi reset, lan sau yeu lai se canh
        bao tiep. Day CHI la canh bao tham khao - bot VAN tiep tuc theo doi
        SL/TP nhu binh thuong, khong tu dong dong keo."""
        now = time.time()
        for key, sig in list(self.active_signals.items()):
            if not sig.get("filled"):
                continue  # LIMIT chua khop thi chua co vi the gi de canh bao giu/dong
            sym = sig["symbol"]
            result = results_by_symbol.get(sym)
            if result is None:
                continue  # symbol khong nam trong scan set vong nay, khong co du lieu moi de re-scan

            orig_dir = sig["direction"]
            cur_dir = result.get("direction", "neutral")
            veto = bool(result.get("veto", False))
            score = result.get("score", 0.0)

            if veto or cur_dir == "neutral":
                new_state = "weak"
                reason = _humanize_veto_reason(result.get("veto_reason", ""))
                label = "TIN HIEU YEU"
            elif cur_dir != orig_dir:
                new_state = "reversed"
                reason = f"quet lai dao chieu sang {cur_dir.upper()}"
                label = "DAO CHIEU"
            else:
                new_state = "ok"
                reason = ""
                label = ""

            prev_state = sig.get("warn_state", "ok")
            if new_state != "ok" and new_state != prev_state:
                self.bot.send_message(
                    f"<b>{sym}</b> canh bao {label}\n"
                    f"Luc vao: {orig_dir.upper()} | Quet lai vua roi: {cur_dir.upper()} (score={score:.1f})\n"
                    f"Ly do: {reason}\n"
                    f"(Bot van tiep tuc theo doi SL/TP nhu binh thuong, day chi la canh bao "
                    f"tham khao - ban tu quyet dinh giu hay dong keo.)"
                )
                append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                    "ts": now, "symbol": sym, "event": "signal_health_warning",
                    "signal_id": key, "orig_direction": orig_dir, "rescan_direction": cur_dir,
                    "rescan_score": score, "warn_state": new_state, "reason": reason,
                })
            sig["warn_state"] = new_state

    def _check_hits_and_expiry(self, current_prices: Dict[str, float]) -> None:
        now = time.time()
        expired: List[str] = []

        for key, sig in list(self.active_signals.items()):
            sym = sig["symbol"]
            price = current_prices.get(sym)
            if price is None:
                # Van khong co gia (ca scan set lan fetch bu deu that bai vong
                # nay) -> thu lai vong sau, KHONG con bo qua vinh vien nhu truoc.
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
                        "signal_id": key,
                    })
                elif now - sig["created_at"] > self.cfg.signal_ttl_seconds:
                    # Limit cho qua lau khong khop -> huy, khong tinh la mot lenh da vao.
                    append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                        "ts": now, "symbol": sym, "event": "limit_expired_unfilled",
                        "signal_id": key,
                    })
                    expired.append(key)
                continue

            # Tu day tro di sig["filled"] chac chan True.
            sl_hit = ((direction == "long" and price <= sig["sl"])
                     or (direction == "short" and price >= sig["sl"]))
            if sl_hit:
                # SL cham la "gay" luon, dong tin hieu bat ke da an duoc tang
                # TP nao truoc do (giu dung tinh than "cham SL la gay").
                hit_tiers = sig.get("hit_tiers", [])
                note = f" (da an truoc do: {', '.join(hit_tiers)})" if hit_tiers else ""
                self.bot.send_message(
                    f"<b>{sym}</b> {direction.upper()} da cham <b>SL</b> tai {price:.6g}{note}")
                append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                    "ts": now, "symbol": sym, "event": "hit_SL", "price": price,
                    "signal_id": key, "hit_tiers_before_sl": hit_tiers,
                })
                expired.append(key)
                continue

            hit_tiers = sig.setdefault("hit_tiers", [])
            for tier_name, tier_price in (("TP1", sig["tp1"]), ("TP2", sig["tp2"]), ("TP3", sig["tp3"])):
                if tier_name in hit_tiers:
                    continue
                reached = ((direction == "long" and price >= tier_price)
                          or (direction == "short" and price <= tier_price))
                if reached:
                    hit_tiers.append(tier_name)
                    self.bot.send_message(
                        f"<b>{sym}</b> {direction.upper()} da cham <b>{tier_name}</b> tai {price:.6g}")
                    append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                        "ts": now, "symbol": sym, "event": f"hit_{tier_name}", "price": price,
                        "signal_id": key,
                    })

            if "TP3" in hit_tiers:
                expired.append(key)
                continue

            if now - sig["created_at"] > self.cfg.signal_ttl_seconds:
                note = f" (da an: {', '.join(hit_tiers)})" if hit_tiers else " (chua an tang TP nao)"
                self.bot.send_message(
                    f"<b>{sym}</b> {direction.upper()} het han theo doi (TTL){note}, "
                    f"gia hien tai {price:.6g}")
                append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                    "ts": now, "symbol": sym, "event": "ttl_expired", "price": price,
                    "signal_id": key, "hit_tiers_before_expiry": hit_tiers,
                })
                expired.append(key)

        for key in expired:
            self.active_signals.pop(key, None)

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

                existing_for_symbol = [s for s in self.active_signals.values() if s["symbol"] == symbol]
                existing_same_dir = [s for s in existing_for_symbol if s["direction"] == result["direction"]]

                # Chong loang: da co tin hieu CUNG CHIEU dang active (chua cham
                # SL/TP3/het TTL) cho chinh symbol nay -> bo qua alert moi, khong
                # bien no thanh keo thu 2 trung lap. Tin hieu NGUOC CHIEU (dao
                # the) van duoc bao binh thuong (khong bi chan boi dieu kien nay).
                if existing_same_dir and self.cfg.suppress_duplicate_direction_signal:
                    log.info("%s: da co %d tin hieu %s dang active (chua cham SL/TP), "
                             "bo qua alert trung chieu luc nay", symbol, len(existing_same_dir),
                             result["direction"])
                    continue

                entry_info = classify_entry(features, result, self.weights)
                entry_price = entry_info["entry_price"]
                regime = features.get("regime", "")
                poc = features.get("volume_profile", {}).get("poc", 0.0)
                sl_tp = suggested_sl_tp(entry_price, result["direction"], features["atr15m"],
                                        regime, poc, self.cfg)

                if existing_for_symbol:
                    log.info("%s da co %d tin hieu khac chieu dang active, them tin hieu moi song song "
                             "(khong ghi de/xoa tin hieu cu)", symbol, len(existing_for_symbol))

                self.bot.send_message(format_alert(features, result, entry_info, sl_tp))
                append_jsonl(self.cfg.resolve_path(self.cfg.log_path), {
                    "ts": now, "symbol": symbol, "direction": result["direction"],
                    "score": result["score"], "entry_type": entry_info["entry_type"],
                    "entry": entry_price, "sl": sl_tp["sl"],
                    "tp1": sl_tp["tp1"], "tp2": sl_tp["tp2"], "tp3": sl_tp["tp3"],
                })
                self.cooldowns[symbol] = now

                sig_id = f"{symbol}#{int(now * 1000)}"
                self.active_signals[sig_id] = {
                    "symbol": symbol,
                    "direction": result["direction"], "entry_price": entry_price,
                    "entry_type": entry_info["entry_type"],
                    "sl": sl_tp["sl"], "tp1": sl_tp["tp1"], "tp2": sl_tp["tp2"], "tp3": sl_tp["tp3"],
                    "created_at": now, "filled": entry_info["entry_type"] == "MARKET",
                    "hit_tiers": [], "warn_state": "ok",
                }

        # Canh bao tham khao: re-scan tin hieu active bang chinh ket qua vong
        # quet nay (khong ton them API call). Chi canh bao, khong tu dong dong.
        if self.cfg.enable_signal_health_warning:
            results_by_symbol = {r["symbol"]: r for r in results}
            self._check_signal_health(results_by_symbol)

        # Fix lo hong #2: bu gia cho active_signals bi rot khoi scan_set truoc
        # khi kiem tra TP/SL, tranh "treo lo lung mai mai".
        self._fetch_missing_prices(current_prices)
        self._check_hits_and_expiry(current_prices)

        # Fix lo hong #3: ghi state ra dia sau moi vong (alert moi + hit/expiry
        # deu da duoc ap dung vao self.active_signals/self.cooldowns o tren).
        self._save_state()

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
