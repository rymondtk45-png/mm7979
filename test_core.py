"""
tests/test_core.py
Unit test cho cac ham thuan (khong I/O) trong src/data.py va src/signals.py.
Chay: python -m unittest tests.test_core -v   (tu thu muc goc bot_mm_fund)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data import (  # noqa: E402
    compute_cvd, compute_imbalance, compute_volume_profile, bias_from_klines,
)
from src.signals import compute_composite, htf_check  # noqa: E402
from src.config import AppConfig  # noqa: E402


def make_kline(close, high=None, low=None, volume=100.0, taker_buy=None):
    high = high if high is not None else close * 1.001
    low = low if low is not None else close * 0.999
    return {
        "open": close, "high": high, "low": low, "close": close,
        "volume": volume, "taker_buy_base": taker_buy if taker_buy is not None else volume / 2,
        "open_time": 0, "close_time": 0,
    }


class TestCVD(unittest.TestCase):
    def test_cvd_positive_on_buy_prints(self):
        trades = [
            {"qty": 1.0, "isBuyerMaker": False},  # buy aggressor
            {"qty": 2.0, "isBuyerMaker": False},  # buy aggressor
            {"qty": 0.5, "isBuyerMaker": True},   # sell aggressor
        ]
        cvd = compute_cvd(trades)
        self.assertGreater(cvd, 0.0)
        self.assertAlmostEqual(cvd, 2.5)


class TestImbalance(unittest.TestCase):
    def test_imbalance_positive_when_bid_heavy(self):
        bids = [(100.0, 10.0), (99.9, 5.0)]
        asks = [(100.1, 2.0), (100.2, 1.0)]
        imb = compute_imbalance(bids, asks, levels=20)
        self.assertGreater(imb, 0.0)

    def test_imbalance_negative_when_ask_heavy(self):
        bids = [(100.0, 1.0)]
        asks = [(100.1, 10.0)]
        imb = compute_imbalance(bids, asks, levels=20)
        self.assertLess(imb, 0.0)


class TestVolumeProfile(unittest.TestCase):
    def test_poc_correct(self):
        # Tao trades tap trung nhieu volume quanh gia 100
        trades = []
        for _ in range(20):
            trades.append({"price": 100.0, "qty": 10.0, "isBuyerMaker": False})
        for _ in range(5):
            trades.append({"price": 105.0, "qty": 1.0, "isBuyerMaker": True})
        for _ in range(5):
            trades.append({"price": 95.0, "qty": 1.0, "isBuyerMaker": True})
        vp = compute_volume_profile(trades, atr15m=4.0, buckets=40)
        # bucket_width = 4/40 = 0.1 -> POC phai gan 100.0
        self.assertAlmostEqual(vp["poc"], 100.0, delta=0.2)
        self.assertGreater(vp["delta_at_poc"], 0.0)  # toan la buy aggressor tai POC


class TestHTFVeto(unittest.TestCase):
    def setUp(self):
        os.environ["REQUIRE_1H_ALIGN"] = "true"
        os.environ["REQUIRE_4H_ALIGN"] = "false"
        self.cfg = AppConfig()

    def test_1h_long_4h_short_conflict(self):
        allowed, reason = htf_check("long", "long", "short", self.cfg)
        self.assertFalse(allowed)
        self.assertIn("HTF conflict", reason)

    def test_15m_leads_alone_no_alert(self):
        allowed, reason = htf_check("long", "neutral", "neutral", self.cfg)
        self.assertFalse(allowed)

    def test_15m_and_1h_aligned_ok(self):
        allowed, reason = htf_check("long", "long", "neutral", self.cfg)
        self.assertTrue(allowed)


class TestComposite(unittest.TestCase):
    def setUp(self):
        os.environ["REQUIRE_1H_ALIGN"] = "true"
        os.environ["REQUIRE_4H_ALIGN"] = "false"
        self.cfg = AppConfig()
        self.weights = {
            "volume_profile": 1.1, "tape_flow": 1.1, "absorption": 1.0,
            "liquidation_impulse": 1.0, "funding_extreme": 0.9, "persistent_book": 0.8,
            "liquidity_sweep": 0.8, "basis_spread": 0.7, "taker_buy_sell_ratio": 0.6,
            "long_short_ratio": 0.6, "order_book_imbalance": 0.5,
            "cross_exchange_divergence": 0.5,
        }

    def _base_features(self):
        return {
            "symbol": "BTCUSDT", "last_price": 100.0, "atr15m": 1.0,
            "bias_15m": "long", "bias_1h": "long", "bias_4h": "long",
            "regime": "trending",
            "sweep": {"swept": False, "side": None, "tf": None},
            "volume_profile": {"poc": 100.0, "hvn": [], "lvn": [], "delta_at_poc": 5.0,
                                "distance_to_poc": 0.001},
            "cvd_1m": 1.0, "cvd_5m": 5.0, "cvd_15m": 10.0,
            "large_print_cluster": {"cluster": True, "side": "long", "count": 4},
            "book_imbalance": 0.3, "microprice": 100.0,
            "persist_score": 0.6, "pull_ratio_3s": 0.1, "spoof_score": 0.1,
            "absorption": {"absorption": False, "side": None},
            "liquidation": {"long_liq_usd": 0, "short_liq_usd": 0,
                             "impulse_60s_long": 0, "impulse_60s_short": 20000},
            "funding_rate": 0.0001, "funding_zscore": 0.2,
            "basis": 0.0002, "open_interest": 1000.0,
            "long_short_ratio": 1.0, "taker_buy_sell_ratio": 0.7,
            "cross_exchange_divergence": 0.0001, "price_move_pct_15m": 0.001,
        }

    def test_composite_has_direction(self):
        f = self._base_features()
        result = compute_composite(f, self.weights, self.cfg)
        self.assertIn(result["direction"], ("long", "short", "neutral"))
        self.assertFalse(result["veto"])
        self.assertGreater(result["score"], 0.0)

    def test_1h_long_4h_short_forces_neutral_veto(self):
        f = self._base_features()
        f["bias_1h"] = "long"
        f["bias_4h"] = "short"
        result = compute_composite(f, self.weights, self.cfg)
        self.assertTrue(result["veto"])
        self.assertEqual(result["direction"], "neutral")
        self.assertIn("HTF conflict", result["veto_reason"])

    def test_15m_long_1h_short_with_require_align_blocks(self):
        f = self._base_features()
        f["bias_15m"] = "long"
        f["bias_1h"] = "short"
        f["bias_4h"] = "neutral"
        result = compute_composite(f, self.weights, self.cfg)
        self.assertTrue(result["veto"])
        self.assertEqual(result["direction"], "neutral")


if __name__ == "__main__":
    unittest.main()
