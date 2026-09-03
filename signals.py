"""
signals.py

Module score + luat HTF veto + composite + ranking.
Moi module tra ve signed float trong [-1, 1]: duong = nghieng long, am = nghieng short,
do lon = do tin cay module do. Composite = sum(module_score * weight), sau do chuan hoa 0-100.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

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
        elif raw < -0.05:
            votes_short += 1

    # FIX: README muc 6.5 mo ta consensus DA SIET LAI thanh "can it nhat 4
    # module co y kien VA chenh lech phieu long/short >= 3", nhung code cu
    # o day chi kiem tra "chenh lech <= 1 thi veto" (tuc chi can chenh lech
    # >= 2, KHONG co dieu kien so luong module toi thieu) - chinh la ban
    # "cu" ma README noi da bo di. Vi vay tin hieu chi co 2 module dong y
    # (vd 2 vote long, 0 vote short) van lot qua, gay nhieu tin hieu "mong
    # manh" tren symbol thanh khoan mong / du lieu it. Sua lai dung theo
    # tai lieu: khong noi long gi them, chi ap dung dung nhu tac gia da
    # dinh - vi vay day la fix AN TOAN, chi siet chat hon, khong lam giam
    # chat luong tin hieu da qua loc.
    total_votes = votes_long + votes_short
    vote_diff = abs(votes_long - votes_short)
    if total_votes < 4 or vote_diff < 3:
        reasons.append(
            f"veto: weak consensus (modules co y kien={total_votes}, "
            f"chenh lech phieu={vote_diff}, can >=4 module VA chenh lech >=3)")
        return {
            "score": 0.0, "direction": "neutral", "confidence": 0.0,
            "reasons": reasons, "veto": True, "veto_reason": "weak consensus",
            "module_scores": module_scores,
        }

    direction = "long" if total > 0 else ("short" if total < 0 else "neutral")

    # FIX: dam bao "dong thuan" la bang chung DOC LAP, khong phai 1 loai
    # tin hieu bi dem lap lai duoi nhieu ten module. MOMENTUM_MODULES (4
    # module) deu do cung 1 hien tuong - ap luc mua/ban chu dong ngay luc
    # nay - duoi cac goc nhin khac nhau, nen tuong quan rat cao. Neu ca 4
    # cung len tieng, gate cu (>=4 module, chenh lech >=3) se coi day la
    # "dong thuan manh" trong khi thuc chat chi la 1 nguon bang chung. Yeu
    # cau cac module dang vote CUNG HUONG voi tin hieu cuoi cung phai trai
    # it nhat 2/3 nhom ban chat (MOMENTUM / STRUCTURE / MEAN-REVERSION).
    dir_sign = 1.0 if direction == "long" else (-1.0 if direction == "short" else 0.0)
    categories_present = set()
    for name, raw in module_scores.items():
        if raw * dir_sign <= 0.05:
            continue
        if name in MOMENTUM_MODULES:
            categories_present.add("momentum")
        elif name in STRUCTURE_MODULES:
            categories_present.add("structure")
        elif name in MEANREV_MODULES:
            categories_present.add("meanrev")

    if len(categories_present) < 2:
        cats_str = ",".join(sorted(categories_present)) if categories_present else "none"
        reasons.append(
            f"veto: dong thuan chi tap trung 1 nhom ban chat ({cats_str}) "
            f"- can bang chung tu >=2/3 nhom (momentum/structure/mean-reversion)")
        return {
            "score": 0.0, "direction": "neutral", "confidence": 0.0,
            "reasons": reasons, "veto": True, "veto_reason": "low evidence diversity",
            "module_scores": module_scores,
        }

    if f.get("bias_4h") != "neutral" and f.get("bias_4h") == direction:
        total *= 1.15
        reasons.append("4h aligned x1.15")

    spoof_score = f.get("spoof_score", 0.0)
    if spoof_score > 0.6:
        total *= 0.75
        reasons.append(f"spoof_score {spoof_score:.2f} > 0.6 -> x0.75")

    # FIX: chuan hoa theo tong trong so cua CAC MODULE DANG THUC SU CO Y
    # KIEN (|raw|>0.05, dung ngung "vote" da dung o tren), khong phai tong
    # trong so CA 12 module (max_possible cu, hang so = 9.6). Nhieu module
    # (funding_extreme, liquidity_sweep, absorption, cross_exchange_divergence)
    # co dieu kien - phan lon thoi gian tra ve dung 0 vi thi truong khong
    # roi vao trang thai cuc doan tuong ung, KHONG phai vi tin hieu yeu. Chia
    # cho tong weight ca nhung module dang im lang do ban chat la sai ve mat
    # thong ke - phat oan tin hieu that su manh giua cac module DANG hoat
    # dong. active_weight phan anh dung "% hoi tu toi da co the dat duoc,
    # TRONG SO CAC KENH BANG CHUNG DANG LEN TIENG". Khong lam long bat ky
    # dieu kien loc chat luong nao o tren (gate >=4 module, chenh lech >=3,
    # da dang >=2/3 nhom van nguyen ven).
    active_weight = sum(
        weights[name] for name, raw in module_scores.items() if abs(raw) > 0.05
    )
    magnitude = abs(total) / active_weight if active_weight else 0.0
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


def compute_composite_scan(f: dict, weights: Dict[str, float], cfg: AppConfig) -> dict:
    """
    Bien the CHI DUNG CHO LENH /scan (khong dung trong vong lap alert chinh
    run_once/compute_composite): bo qua veto HTF, veto "weak consensus"
    (>=4 module + chenh lech phieu >=3) va veto "low evidence diversity"
    (>=2/3 nhom momentum/structure/mean-reversion) de /scan LUON tra ve mot
    huong entry co the xem, kem canh bao ro rang la huong nay chua qua het
    lop bao ve cua he thong (giong composite chinh o cho khac).

    FIX: ham nay bi mat khi signals.py duoc viet lai (compute_composite cu
    thay bang logic gate moi: >=4 module/chenh lech >=3/da dang nhom) nhung
    ban than compute_composite_scan lai chua duoc cap nhat theo -> app.py
    import compute_composite_scan bi ImportError, container crash-loop.
    Viet lai o day, dong bo voi gate/logic moi cua compute_composite.

    Logic:
    1) Van chay htf_check() nhu binh thuong nhung CHI DE GHI LAI ly do
       (htf_bypassed=True neu bi veto o composite chinh).
    2) Tinh cac module dang active (giong compute_composite) + tong co
       trong so. Neu tong ro huong (>0 hoac <0) thi dung huong do. Neu
       tong = 0, chon module co |raw*weight| lon nhat lam huong de xuat
       (forced_by_strength=True).
    3) Kiem tra 2 gate "weak consensus" va "low evidence diversity" NHUNG
       chi de danh dau canh bao (mixed_votes=True), khong con return som/
       veto nua - /scan van hien ket qua kem canh bao.
    4) Diem so (score/confidence) tinh theo cung cong thuc active_weight
       cua compute_composite; neu la huong bi "ep" theo module manh nhat
       (forced_by_strength) thi nhan them he so giam (x0.6) de phan biet
       voi tin hieu dong thuan day du.

    Tra ve dict giong compute_composite + them:
      htf_bypassed (bool): True neu htf_check() dang veto (conflict/align).
      htf_reason (str): ly do tu htf_check(), du co bypass hay khong.
      mixed_votes (bool): True neu gate "weak consensus" hoac "low
        evidence diversity" le ra se veto o composite chinh.
      forced_by_strength (bool): True neu huong duoc chon tu module manh
        nhat vi tong co trong so = 0 (khong the suy huong tu tong).
    """
    reasons: List[str] = []

    allowed, htf_reason = htf_check(f.get("bias_15m", "neutral"), f.get("bias_1h", "neutral"),
                                     f.get("bias_4h", "neutral"), cfg)
    htf_bypassed = not allowed
    reasons.append(htf_reason if allowed else f"{htf_reason} (BO QUA rieng cho /scan)")

    active_modules = LEGACY_MODULES if not cfg.enable_market_intel_scoring else list(weights.keys())

    module_scores: Dict[str, float] = {}
    total = 0.0
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
        elif raw < -0.05:
            votes_short += 1

    total_votes = votes_long + votes_short
    vote_diff = abs(votes_long - votes_short)
    weak_consensus = total_votes < 4 or vote_diff < 3
    if weak_consensus:
        reasons.append(
            f"canh bao: weak consensus (modules co y kien={total_votes}, chenh lech phieu={vote_diff}, "
            f"can >=4 module VA chenh lech >=3) - /scan van hien theo module manh nhat")

    if total > 0:
        direction = "long"
    elif total < 0:
        direction = "short"
    else:
        direction = "neutral"

    low_diversity = False
    if direction != "neutral":
        dir_sign = _dir_sign(direction)
        categories_present = set()
        for name, raw in module_scores.items():
            if raw * dir_sign <= 0.05:
                continue
            if name in MOMENTUM_MODULES:
                categories_present.add("momentum")
            elif name in STRUCTURE_MODULES:
                categories_present.add("structure")
            elif name in MEANREV_MODULES:
                categories_present.add("meanrev")
        low_diversity = len(categories_present) < 2
        if low_diversity:
            cats_str = ",".join(sorted(categories_present)) if categories_present else "none"
            reasons.append(
                f"canh bao: dong thuan chi tap trung 1 nhom ban chat ({cats_str}) "
                f"- can bang chung tu >=2/3 nhom (momentum/structure/mean-reversion)")

    mixed_votes = weak_consensus or low_diversity

    strongest_name = None
    strongest_contrib = 0.0
    for name, raw in module_scores.items():
        contrib = raw * weights.get(name, 0.0)
        if abs(contrib) > abs(strongest_contrib):
            strongest_name, strongest_contrib = name, contrib

    forced_by_strength = False
    if direction == "neutral" and strongest_name and abs(strongest_contrib) > 1e-9:
        direction = "long" if strongest_contrib > 0 else "short"
        forced_by_strength = True
        reasons.append(
            f"khong co huong tong ro rang, dung module manh nhat '{strongest_name}' "
            f"({strongest_contrib:+.3f}) -> nghieng {direction} (chi ap dung cho /scan)"
        )

    if direction != "neutral":
        if f.get("bias_4h") != "neutral" and f.get("bias_4h") == direction:
            total *= 1.15
            reasons.append("4h aligned x1.15")

        spoof_score = f.get("spoof_score", 0.0)
        if spoof_score > 0.6:
            total *= 0.75
            reasons.append(f"spoof_score {spoof_score:.2f} > 0.6 -> x0.75")

    active_weight = sum(
        weights[name] for name, raw in module_scores.items() if abs(raw) > 0.05
    )
    magnitude = abs(total) / active_weight if active_weight else 0.0
    if forced_by_strength and active_weight:
        # tong bi trung hoa ve 0 nhung van co 1 module manh -> dung do lon
        # cua module do lam proxy, giam bot (x0.6) vi day la tin hieu "mong"
        # hon dong thuan that su (chi 1 module, khong phai tong hop nhieu module).
        magnitude = min(abs(strongest_contrib) / active_weight, 1.0) * 0.6

    score = max(0.0, min(100.0, magnitude * 100.0))
    confidence = magnitude

    if direction == "neutral":
        reasons.append("khong co huong ro rang (ke ca module manh nhat cung = 0)")

    top_modules = sorted(module_scores.items(), key=lambda kv: abs(kv[1]), reverse=True)[:4]
    for name, val in top_modules:
        if abs(val) > 0.05:
            reasons.append(f"{name}: {'long' if val > 0 else 'short'} ({val:+.2f})")

    return {
        "score": score, "direction": direction, "confidence": confidence,
        "reasons": reasons, "veto": False, "veto_reason": "", "module_scores": module_scores,
        "htf_bypassed": htf_bypassed, "htf_reason": htf_reason,
        "mixed_votes": mixed_votes, "forced_by_strength": forced_by_strength,
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
