"""
app_patch.py
============
KHONG chay file nay truc tiep. Chi vai thay doi nho can dan vao app.py (toi co
toan bo app.py goc nen day la patch chinh xac tung dong, khong phai doan tach
roi nhu data_additions.py).

Thay doi 1: import them refresh_btc_snapshot + refresh_deribit_snapshot tu data.py
Thay doi 2: goi ca 2 ham refresh_*_snapshot 1 lan dau moi run_once(), TRUOC khi
            mo ThreadPoolExecutor (de moi symbol trong vong quet nay dung chung
            1 boi canh BTC/options nhat quan, thay vi moi thread tu fetch rieng).
            refresh_deribit_snapshot() tu co cache TTL rieng (mac dinh 600s)
            nen goi moi vong 25s cung khong sao - ham se tu bo qua neu chua het
            han cache.
Thay doi 3: doi thu tu ghi feature log - chuyen append_jsonl(feature_log_path)
            xuong SAU compute_composite() va gop them module_scores + score +
            direction vao dong log, de sau nay similarity.py co du lieu ma
            khop setup lich su (xem similarity.py). Neu ban chua can tinh nang
            similarity-based winrate thi co the bo qua thay doi 3.
"""

# --- Thay doi 1: dong import o dau app.py ---
# TRUOC:
#   from data import MarketContext, UniverseManager, build_features, init_rate_limiter
# SAU:
#   from data import (MarketContext, UniverseManager, build_features, init_rate_limiter,
#                      refresh_btc_snapshot, refresh_deribit_snapshot)


# --- Thay doi 2: trong SignalEngine.run_once(), dong dau tien cua ham ---
# TRUOC:
"""
    def run_once(self) -> List[dict]:
        self.universe.refresh(force=False)
        scan_set = self.universe.get_scan_set()
"""
# SAU:
"""
    def run_once(self) -> List[dict]:
        self.universe.refresh(force=False)
        refresh_btc_snapshot(self.ctx, self.cfg)      # <-- THEM DONG NAY
        refresh_deribit_snapshot(self.ctx, self.cfg)  # <-- THEM DONG NAY
        scan_set = self.universe.get_scan_set()
"""


# --- Thay doi 3 (TUY CHON - chi can neu muon dung similarity.py sau nay) ---
# Trong SignalEngine._process_symbol(), doi thu tu tu:
"""
    def _process_symbol(self, symbol: str, is_core: bool, is_ws: bool) -> Optional[dict]:
        try:
            features = build_features(symbol, self.cfg, self.ctx, is_core, is_ws)
            append_jsonl(self.cfg.resolve_path(self.cfg.feature_log_path), features)
            result = compute_composite(features, self.weights, self.cfg)
            result["symbol"] = symbol
            result["features"] = features
            return result
        except Exception as e:
            log.warning("Loi xu ly symbol %s: %s", symbol, e)
            return None
"""
# THANH:
"""
    def _process_symbol(self, symbol: str, is_core: bool, is_ws: bool) -> Optional[dict]:
        try:
            features = build_features(symbol, self.cfg, self.ctx, is_core, is_ws)
            result = compute_composite(features, self.weights, self.cfg)
            result["symbol"] = symbol
            result["features"] = features
            # Ghi SAU compute_composite de co them module_scores/score/direction
            # trong cung 1 dong log - can cho similarity.py doi chieu sau nay.
            log_row = dict(features)
            log_row["module_scores"] = result.get("module_scores", {})
            log_row["composite_score"] = result.get("score", 0.0)
            log_row["composite_direction"] = result.get("direction", "neutral")
            append_jsonl(self.cfg.resolve_path(self.cfg.feature_log_path), log_row)
            return result
        except Exception as e:
            log.warning("Loi xu ly symbol %s: %s", symbol, e)
            return None
"""
