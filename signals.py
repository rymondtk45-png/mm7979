"""
signals.py

Module score + luat HTF veto + composite + ranking.
Moi module tra ve signed float trong [-1, 1]: duong = nghieng long, am = nghieng short,
do lon = do tin cay module do. Composite = sum(module_score * weight), sau do chuan hoa 0-100.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from config import AppConfig

LEGACY_MODULES = ["volume_profile", "tape_flow", "liquidation_impulse", "funding_extreme"]

# --------------------------------------------------------------------------
# Phan loai LIMIT vs MARKET theo BAN CHAT cua module dang dan dat tin hieu
# --------------------------------------------------------------------------
# MOMENTUM: gia dang chay theo huong tin hieu NGAY LUC NAY (aggression/breakout/
# cascade dang xay ra). Cho retest = de mat edge hoac mat hang -> MARKET.
MOMENTUM_MODULES = {"tape_flow", "taker_buy_sell_ratio", "liquidity_sweep", "liquidation_impulse"}

# STRUCTURE: tin hieu dua tren 1 vung gia/muc thanh khoan cu the (POC, sach lenh
# ben vung) - gia thuong quay lai test vung do truoc khi di tiep -> LIMIT tai vung.
STRUCTURE_MODULES = {"volume_profile", "absorption", "persistent_book", "order_book_imbalance"}

# MEAN-REVERSION: tin hieu contrarian tu trang thai qua mua/qua ban (funding,
# basis, LSR, chenh lech gia lien san) - dien bien cham, khong can vao ngay,
# thuong co du thoi gian cho gia lui ve muc tot hon -> LIMIT.
MEANREV_MODULES = {"funding_extreme", "basis_spread", "long_short_ratio", "cross_exchange_divergence"}


def classify_entry(f: dict, result: dict, weights: Dict[str, float]) -> dict:
    """
    Quyet dinh LIMIT hay MARKET dua tren TOAN BO dong gop cua cac module (khong
    chi module manh nhat) - so sanh tong dong gop (raw*weight, cung chieu voi
    huong tin hieu cuoi cung) cua nhom MOMENTUM vs nhom STRUCTURE+MEAN-REVERSION.
    Regime va spoof_score dieu chinh nhe theo boi canh (high_volatility day ve
    MARKET, accumulation/spoof nghi ngo day ve LIMIT).

    Tra ve dict: entry_type (MARKET/LIMIT), entry_price, reason.
    """
    direction = result.get("direction", "neutral")
    last_price = f.get("last_price", 0.0)
    if direction == "neutral" or not last_price:
        return {"entry_type": "MARKET", "entry_price": last_price, "reason": "khong co huong ro rang"}

    sign = 1.0 if direction == "long" else -1.0
    module_scores = result.get("module_scores", {})

    momentum_contrib = 0.0
    structure_contrib = 0.0
    for name, raw in module_scores.items():
        contrib = raw * weights.get(name, 0.0)
        if contrib * sign <= 0:
            continue  # chi tinh dong gop CUNG huong voi tin hieu cuoi cung
        if name in MOMENTUM_MODULES:
            momentum_contrib += abs(contrib)
        elif name in STRUCTURE_MODULES or name in MEANREV_MODULES:
            structure_contrib += abs(contrib)

    regime = f.get("regime", "")
    spoof = f.get("spoof_score", 0.0)
    if regime == "high_volatility":
        momentum_contrib *= 1.15
    elif regime == "accumulation":
        structure_contrib *= 1.15
    if spoof > 0.6:
        # sach lenh nghi bi gia lap -> khong nen duoi gia bang market
        structure_contrib *= 1.3

    atr = f.get("atr15m", 0.0)
    poc = f.get("volume_profile", {}).get("poc", 0.0)

    if momentum_contrib >= structure_contrib or not atr:
        return {
            "entry_type": "MARKET",
            "entry_price": last_price,
            "reason": f"momentum {momentum_contrib:.2f} >= structure {structure_contrib:.2f}",
        }

    # LIMIT: uu tien neo vao POC (vung volume/thanh khoan) neu POC nam trong
    # 1.2x ATR va dung phia can cho gia lui ve; khong thi lui theo ATR (0.35x).
    pullback = 0.35 * atr
    if direction == "long":
        use_poc = poc and poc <= last_price and (last_price - poc) <= 1.2 * atr
        anchor = poc if use_poc else (last_price - pullback)
        entry_price = min(anchor, last_price)
    else:
        use_poc = poc and poc >= last_price and (poc - last_price) <= 1.2 * atr
        anchor = poc if use_poc else (last_price + pullback)
        entry_price = max(anchor, last_price)

    return {
        "entry_type": "LIMIT",
        "entry_price": entry_price,
        "reason": f"structure {structure_contrib:.2f} > momentum {momentum_contrib:.2f}"
        + (" (neo POC)" if 'use_poc' in locals() and use_poc else " (lui theo ATR)"),
    }


def htf_check(bias_15m: str, bias_1h: str, bias_4h: str, cfg: AppConfig) -> Tuple[bool, str]:
    """
    Tra ve (allowed, reason).
    - 1h va 4h nguoc nhau -> veto: HTF conflict (neutral)
    - Neu REQUIRE_1H_ALIGN: alert chi khi 15m VA 1h cung huong. 15m di truoc mot minh -> khong alert.
    - Neu REQUIRE_4H_ALIGN: bat buoc 4h cung huong voi 15m/1h.
    """
    if bias_1h != "neutral" and bias_4h != "neutral" and bias_1h != bias_4h:
        return False, "veto: HTF conflict (1h vs 4h nguoc chieu)"

    if cfg.require_1h_align:
        if bias_15m == "neutral":
            return False, "veto: 15m neutral"
        if bias_1h == "neutral":
            return False, "veto: 1h chua xac nhan huong (15m di truoc mot minh)"
        if bias_15m != bias_1h:
            return False, "veto: 15m va 1h khong cung huong"

    if cfg.require_4h_align:
        if bias_4h == "neutral" or bias_4h != bias_15m:
            return False, "veto: REQUIRE_4H_ALIGN bat nhung 4h khong cung huong"

    return True, "HTF ok"


def _dir_sign(direction: str) -> float:
    return {"long": 1.0, "short": -1.0}.get(direction, 0.0)


def module_volume_profile(f: dict) -> float:
    """Vi the o vung volume cao (POC/HVN) + delta_at_poc xac nhan huong."""
    vp = f.get("volume_profile", {})
    last = f.get("last_price", 0.0)
    poc = vp.get("poc", 0.0)
    if not poc or not last:
        return 0.0
    distance = vp.get("distance_to_poc", 0.0)
    near_poc = abs(distance) < 0.003  # gia dang o gan POC
    delta = vp.get("delta_at_poc", 0.0)
    if not near_poc:
        return 0.0
    strength = min(abs(delta) / (abs(delta) + 1e-6 + 1.0), 1.0) if delta else 0.0
    sign = 1.0 if delta > 0 else (-1.0 if delta < 0 else 0.0)
    return sign * max(strength, 0.3 if near_poc else 0.0)


def module_tape_flow(f: dict) -> float:
    """CVD 5m + large print cluster."""
    cvd5 = f.get("cvd_5m", 0.0)
    cluster = f.get("large_print_cluster", {})
    score = 0.0
    if cvd5 > 0:
        score += 0.4
    elif cvd5 < 0:
        score -= 0.4
    if cluster.get("cluster"):
        score += 0.6 if cluster.get("side") == "long" else -0.6
    return max(-1.0, min(1.0, score))


def module_absorption(f: dict) -> float:
    ab = f.get("absorption", {})
    if not ab.get("absorption"):
        return 0.0
    return 0.8 if ab.get("side") == "long" else -0.8


def module_liquidation_impulse(f: dict) -> float:
    """Thanh ly long lon -> co the bat day (long); thanh ly short lon -> nghieng short bounce? theo MM logic:
    liquidation cascade thuong tao phan ung nguoc: long liq nhieu -> ap luc ban da xa -> short tiep tuc yeu -> nghieng long;
    short liq nhieu -> nghieng short cover -> nghieng long tiep? De don gian va nhat quan, ta coi impulse
    la 'fuel' theo huong nguoc voi phia bi thanh ly (short squeeze / long flush)."""
    liq = f.get("liquidation", {})
    long_liq = liq.get("impulse_60s_long", 0.0)
    short_liq = liq.get("impulse_60s_short", 0.0)
    total = long_liq + short_liq
    if total <= 0:
        return 0.0
    net = (short_liq - long_liq) / total  # short bi thanh ly nhieu hon -> nghieng long
    return max(-1.0, min(1.0, net))


def module_funding_extreme(f: dict) -> float:
    z = f.get("funding_zscore", 0.0)
    if abs(z) < 1.5:
        return 0.0
    # funding duong cao bat thuong -> long qua dong -> nghieng short (mean revert), va nguoc lai
    sign = -1.0 if z > 0 else 1.0
    strength = min(abs(z) / 4.0, 1.0)
    return sign * strength


def module_persistent_book(f: dict) -> float:
    persist = f.get("persist_score", 0.0)
    imbalance = f.get("book_imbalance", 0.0)
    if persist < 0.4:
        return 0.0
    return max(-1.0, min(1.0, imbalance * persist))


def module_liquidity_sweep(f: dict) -> float:
    """Sweep chi tinh khi tape confirm (large print cluster cung phia sau sweep)."""
    sweep = f.get("sweep", {})
    cluster = f.get("large_print_cluster", {})
    if not sweep.get("swept"):
        return 0.0
    if not cluster.get("cluster") or cluster.get("side") != sweep.get("side"):
        return 0.0
    return 0.9 if sweep.get("side") == "long" else -0.9


def module_basis_spread(f: dict) -> float:
    basis = f.get("basis", 0.0)
    if abs(basis) < 0.0008:
        return 0.0
    sign = -1.0 if basis > 0 else 1.0  # basis duong cao -> qua nong -> nghieng short
    return sign * min(abs(basis) / 0.005, 1.0)


def module_taker_buy_sell_ratio(f: dict) -> float:
    ratio = f.get("taker_buy_sell_ratio", 0.5)
    centered = (ratio - 0.5) * 2.0
    return max(-1.0, min(1.0, centered))


def module_long_short_ratio(f: dict) -> float:
    lsr = f.get("long_short_ratio")
    if lsr is None:
        return 0.0
    # LSR cao (nhieu long) -> crowd long -> boi canh nghieng short (contrarian nhe)
    if lsr > 2.0:
        return -0.5
    if lsr < 0.5:
        return 0.5
    return 0.0


def module_order_book_imbalance(f: dict) -> float:
    return max(-1.0, min(1.0, f.get("book_imbalance", 0.0)))


def module_cross_exchange_divergence(f: dict) -> float:
    """Chi la boi canh, khong tu gate huong."""
    div = f.get("cross_exchange_divergence", 0.0)
    return max(-1.0, min(1.0, div * 50.0))


MODULE_FUNCS = {
    "volume_profile": module_volume_profile,
    "tape_flow": module_tape_flow,
    "absorption": module_absorption,
    "liquidation_impulse": module_liquidation_impulse,
    "funding_extreme": module_funding_extreme,
    "persistent_book": module_persistent_book,
    "liquidity_sweep": module_liquidity_sweep,
    "basis_spread": module_basis_spread,
    "taker_buy_sell_ratio": module_taker_buy_sell_ratio,
    "long_short_ratio": module_long_short_ratio,
    "order_book_imbalance": module_order_book_imbalance,
    "cross_exchange_divergence": module_cross_exchange_divergence,
}


def compute_composite(f: dict, weights: Dict[str, float], cfg: AppConfig) -> dict:
    """
    Tra ve dict: score(0-100), direction(long/short/neutral), confidence(0-1),
    reasons(list[str]), veto(bool), veto_reason(str), module_scores(dict).
    """
    reasons: List[str] = []
    allowed, htf_reason = htf_check(f.get("bias_15m", "neutral"), f.get("bias_1h", "neutral"),
                                     f.get("bias_4h", "neutral"), cfg)
    reasons.append(htf_reason)
    if not allowed:
        return {
            "score": 0.0, "direction": "neutral", "confidence": 0.0,
            "reasons": reasons, "veto": True, "veto_reason": htf_reason,
            "module_scores": {},
        }

    active_modules = LEGACY_MODULES if not cfg.enable_market_intel_scoring else list(weights.keys())
    module_scores: Dict[str, float] = {}
    total = 0.0
    # FIX: truoc day active_weight_sum dung chung voi max_possible = tong TOAN BO
    # trong so 12 module (ke ca module dang im lang), lam pha loang diem so va
    # gan nhu khong bao gio dat THRESHOLD. Gio chi tinh trong so cua cac module
    # THUC SU len tieng (|raw| > 0.05, cung nguong voi vote) -> diem phan anh
    # dung do "dong thuan that su" cua nhung module co du lieu, thay vi bi keo
    # xuong boi cac module cau truc it khi active (absorption, liquidity_sweep,
    # funding_extreme, cross_exchange_divergence...).
    active_weight_sum = 0.0
    votes_long = 0
    votes_short = 0

    for name in active_modules:
        if name not in weights:
            continue
        func = MODULE_FUNCS.get(name)
        if not func:
            continue
        raw = func(f)
        module_scores[name] = raw
        w = weights[name]
        total += raw * w
        if raw > 0.05:
            votes_long += 1
            active_weight_sum += w
        elif raw < -0.05:
            votes_short += 1
            active_weight_sum += w

    # FIX: code cu chi veto khi (votes_long+votes_short)>0 VA chenh lech <= 1,
    # tuc chi can 2 phieu chenh nhau (vd 2 long/0 short) la lot qua duoc, khac
    # voi README (can >=4 module co y kien VA chenh lech >=3). Sieit lai dung
    # spec: it tin hieu hon nhung moi tin hieu deu co nhieu module dong thuan
    # that su, giam han tin hieu "mong manh" hay dinh SL som.
    total_votes = votes_long + votes_short
    vote_diff = abs(votes_long - votes_short)
    if total_votes < 4 or vote_diff < 3:
        reasons.append(
            f"veto: weak consensus (modules={total_votes}, chenh lech={vote_diff}, "
            f"can >=4 module va chenh lech >=3)")
        return {
            "score": 0.0, "direction": "neutral", "confidence": 0.0,
            "reasons": reasons, "veto": True, "veto_reason": "weak consensus",
            "module_scores": module_scores,
        }

    direction = "long" if total > 0 else ("short" if total < 0 else "neutral")

    if f.get("bias_4h") != "neutral" and f.get("bias_4h") == direction:
        total *= 1.15
        reasons.append("4h aligned x1.15")

    spoof_score = f.get("spoof_score", 0.0)
    if spoof_score > 0.6:
        total *= 0.75
        reasons.append(f"spoof_score {spoof_score:.2f} > 0.6 -> x0.75")

    magnitude = abs(total) / active_weight_sum if active_weight_sum else 0.0
    score = max(0.0, min(100.0, magnitude * 100.0))
    confidence = magnitude

    if direction == "neutral" or score == 0.0:
        reasons.append("khong co huong ro rang")
        return {
            "score": score, "direction": "neutral", "confidence": confidence,
            "reasons": reasons, "veto": False, "veto_reason": "", "module_scores": module_scores,
        }

    top_modules = sorted(module_scores.items(), key=lambda kv: abs(kv[1]), reverse=True)[:4]
    for name, val in top_modules:
        if abs(val) > 0.05:
            reasons.append(f"{name}: {'long' if val > 0 else 'short'} ({val:+.2f})")

    return {
        "score": score, "direction": direction, "confidence": confidence,
        "reasons": reasons, "veto": False, "veto_reason": "", "module_scores": module_scores,
    }


def suggested_sl_tp(entry: float, direction: str, atr15m: float,
                     volume_profile: Optional[dict] = None) -> Tuple[float, float]:
    """
    SL/TP uu tien theo CAU TRUC volume profile (kieu MM), ATR chi la fallback +
    "luoi an toan" (sanity bound) de tranh SL/TP bi dat vao vi tri phi ly.

    - SL: neo NGOAI vung LVN (low volume node) gan nhat ve phia nguoc huong lenh.
      LVN la vung it lenh nghi/khop -> gia thuong di xuyen qua rat nhanh (vacuum).
      Neu dat SL ngay trong/ngay tai mep LVN de bi quet vi truot gia/wick; dat
      lui ra ngoai mep LVN (+/- buffer nho theo ATR) moi phan anh dung "vi the
      da that su bi pha", khong phai bi hut qua vung thanh khoan mong.
    - TP: nham vao HVN/POC ke tiep cung huong lenh - noi tap trung volume lon,
      nhieu kha nang co phan ung gia (hap thu/dao chieu), hop ly hon 1 boi so
      ATR co dinh khong quan tam den cau truc thi truong.
    - Ca hai deu duoc kep trong 1 khoang ATR hop ly (SL: 0.6x-3.0x ATR, TP:
      1.0x-5.0x ATR) - neu muc LVN/HVN gan nhat nam ngoai khoang nay (qua gan
      se bi quet ngay, qua xa thi vo ly/khong ro rang), roi ve ATR thuan
      (1.2x/2.4x, R:R=2.0) nhu truoc. Khong co volume_profile (vd goi tu test
      cu, backtest cu) -> hanh vi giu nguyen nhu ban ATR thuan.
    """
    atr_sl = 1.2 * atr15m
    atr_tp = 2.4 * atr15m

    vp = volume_profile or {}
    hvn = sorted(vp.get("hvn", []) or [])
    lvn = sorted(vp.get("lvn", []) or [])
    poc = vp.get("poc", 0.0)
    buffer = 0.15 * atr15m if atr15m else 0.0

    if direction == "long":
        sl = entry - atr_sl
        if atr15m:
            lvn_below = [p for p in lvn if p < entry]
            if lvn_below:
                candidate = max(lvn_below) - buffer
                dist = entry - candidate
                if 0.6 * atr15m <= dist <= 3.0 * atr15m:
                    sl = candidate

        tp = entry + atr_tp
        if atr15m:
            targets = sorted(p for p in (hvn + ([poc] if poc else [])) if p > entry)
            if targets:
                candidate = targets[0]
                dist = candidate - entry
                if 1.0 * atr15m <= dist <= 5.0 * atr15m:
                    tp = candidate
        return sl, tp

    if direction == "short":
        sl = entry + atr_sl
        if atr15m:
            lvn_above = [p for p in lvn if p > entry]
            if lvn_above:
                candidate = min(lvn_above) + buffer
                dist = candidate - entry
                if 0.6 * atr15m <= dist <= 3.0 * atr15m:
                    sl = candidate

        tp = entry - atr_tp
        if atr15m:
            targets = sorted((p for p in (hvn + ([poc] if poc else [])) if p < entry), reverse=True)
            if targets:
                candidate = targets[0]
                dist = entry - candidate
                if 1.0 * atr15m <= dist <= 5.0 * atr15m:
                    tp = candidate
        return sl, tp

    return entry, entry


def rank_top(results: List[dict], top_n: int = 5) -> List[dict]:
    scored = [r for r in results if r.get("direction") != "neutral" and not r.get("veto")]
    return sorted(scored, key=lambda r: r["score"], reverse=True)[:top_n]
