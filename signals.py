"""
signals.py
Module score + luat HTF veto + composite + ranking.
Moi module tra ve signed float trong [-1, 1]: duong = nghieng long, am = nghieng short,
do lon = do tin cay module do. Composite = sum(module_score * weight), sau do chuan hoa 0-100.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from .config import AppConfig

LEGACY_MODULES = ["volume_profile", "tape_flow", "liquidation_impulse", "funding_extreme"]


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
    max_possible = 0.0
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
        max_possible += w
        if raw > 0.05:
            votes_long += 1
        elif raw < -0.05:
            votes_short += 1

    if abs(votes_long - votes_short) <= 1 and (votes_long + votes_short) > 0:
        reasons.append("veto: mixed (vote long/short chenh <=1)")
        return {
            "score": 0.0, "direction": "neutral", "confidence": 0.0,
            "reasons": reasons, "veto": True, "veto_reason": "mixed votes",
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

    magnitude = abs(total) / max_possible if max_possible else 0.0
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


def suggested_sl_tp(entry: float, direction: str, atr15m: float) -> Tuple[float, float]:
    """SL/TP tu ATR15m: SL = 0.8*ATR, TP = 1.5*ATR."""
    if direction == "long":
        return entry - 0.8 * atr15m, entry + 1.5 * atr15m
    if direction == "short":
        return entry + 0.8 * atr15m, entry - 1.5 * atr15m
    return entry, entry


def rank_top(results: List[dict], top_n: int = 5) -> List[dict]:
    scored = [r for r in results if r.get("direction") != "neutral" and not r.get("veto")]
    return sorted(scored, key=lambda r: r["score"], reverse=True)[:top_n]
