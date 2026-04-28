"""信号引擎单元测试"""

import pytest
from unittest.mock import patch, MagicMock


# === momentum.py ===
from signals.momentum import detect_momentum_anomaly, detect_oi_divergence


class TestMomentumAnomaly:
    def test_strong_trigger(self):
        """涨幅 > N * volatility → 1.0"""
        # change=10%, vol=2% → 10/100 = 0.1, 2.5*0.02 = 0.05 → 0.1 > 0.05
        score = detect_momentum_anomaly("BTC/USDT", price_change_pct=10.0, volatility_20d=0.02)
        assert score == 1.0

    def test_weak_trigger(self):
        """涨幅 > 0.6 * N * volatility → 0.5"""
        # change=4%, vol=2% → 0.04, threshold=0.05, 0.6*0.05=0.03 → 0.04 > 0.03
        score = detect_momentum_anomaly("BTC/USDT", price_change_pct=4.0, volatility_20d=0.02)
        assert score == 0.5

    def test_no_trigger(self):
        """涨幅太小 → 0.0"""
        score = detect_momentum_anomaly("BTC/USDT", price_change_pct=1.0, volatility_20d=0.05)
        assert score == 0.0

    def test_zero_volatility(self):
        """波动率为 0 → 0.0"""
        score = detect_momentum_anomaly("BTC/USDT", price_change_pct=10.0, volatility_20d=0.0)
        assert score == 0.0

    def test_negative_change(self):
        """跌幅也应该触发"""
        score = detect_momentum_anomaly("BTC/USDT", price_change_pct=-10.0, volatility_20d=0.02)
        assert score == 1.0


class TestOIDivergence:
    def test_full_divergence(self):
        """OI 大变 + 价格不变 → 1.0"""
        score = detect_oi_divergence("BTC/USDT", oi_change_48h_pct=20.0, price_change_pct=1.0)
        assert score == 1.0

    def test_weak_divergence(self):
        """OI 中等变化 → 0.5"""
        score = detect_oi_divergence("BTC/USDT", oi_change_48h_pct=8.0, price_change_pct=1.0)
        assert score == 0.5

    def test_no_divergence_price_moved(self):
        """OI 和价格都变了 → 0.0"""
        score = detect_oi_divergence("BTC/USDT", oi_change_48h_pct=20.0, price_change_pct=5.0)
        assert score == 0.0

    def test_no_divergence_oi_flat(self):
        """OI 没变化 → 0.0"""
        score = detect_oi_divergence("BTC/USDT", oi_change_48h_pct=2.0, price_change_pct=1.0)
        assert score == 0.0


# === bot_filter.py ===
from signals.bot_filter import compute_bot_score, is_bot


class TestBotFilter:
    def test_real_person_renamed(self):
        """改名 = 真人"""
        assert compute_bot_score(author_renamed=True, post_count_24h=5, content_similarity=0.2, account_age_days=100) == 1.0

    def test_bot_high_freq(self):
        """未改名 + 高频 → bot"""
        score = compute_bot_score(author_renamed=False, post_count_24h=15, content_similarity=0.3, account_age_days=100)
        assert score == -1.0
        assert is_bot(score)

    def test_bot_spam(self):
        """高频 + 内容重复 → bot"""
        score = compute_bot_score(author_renamed=False, post_count_24h=12, content_similarity=0.9, account_age_days=100)
        assert score == -2.0
        assert is_bot(score)

    def test_bot_new_account(self):
        """新号 + 高频 + 重复 → 严重 bot"""
        score = compute_bot_score(author_renamed=False, post_count_24h=20, content_similarity=0.95, account_age_days=3)
        assert score == -3.0
        assert is_bot(score)

    def test_normal_user(self):
        """正常用户"""
        score = compute_bot_score(author_renamed=False, post_count_24h=3, content_similarity=0.3, account_age_days=60)
        assert score == 0.0
        assert not is_bot(score)


# === whitelist.py ===
from signals.whitelist import check_whitelist


class TestWhitelist:
    def test_new_coin(self):
        """上线 < 180 天 → 1.0"""
        assert check_whitelist("NEW/USDT", listed_days=30, max_daily_move_pct=10) == 1.0

    def test_volatile_coin(self):
        """历史大波动 > 30% → 0.8"""
        assert check_whitelist("OLD/USDT", listed_days=365, max_daily_move_pct=35) == 0.8

    def test_new_and_volatile(self):
        """新币 + 大波动 → 1.0"""
        assert check_whitelist("NEW/USDT", listed_days=30, max_daily_move_pct=40) == 1.0

    def test_stable_old_coin(self):
        """老币不波动 → 0.0"""
        assert check_whitelist("BTC/USDT", listed_days=2407, max_daily_move_pct=8) == 0.0


# === scorer.py ===
from signals.scorer import compute_strategy_a_score


class TestScorer:
    def test_all_zero(self):
        assert compute_strategy_a_score() == 0.0

    def test_max_scores(self):
        """全部满分 → 接近 1.0"""
        score = compute_strategy_a_score(
            momentum_score=1.0,
            oi_divergence_score=1.0,
            whitelist_score=1.0,
            fear_greed_score=1.0,
        )
        assert score > 0.8

    def test_partial_scores(self):
        """部分信号触发"""
        score = compute_strategy_a_score(momentum_score=1.0, oi_divergence_score=0.5)
        assert 0 < score < 1.0
