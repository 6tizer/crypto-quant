"""策略切换器 — 决定当前环境偏策略 A 还是 B"""

import structlog

from config.settings import settings
from data.db.models import MarketSnapshot, FearGreedHistory, get_session

log = structlog.get_logger()


def get_strategy_weights() -> dict:
    """根据当前市场环境返回策略权重。

    逻辑：
    - 山寨波动率 / BTC 波动率 > 阈值 → 偏策略 A（追高）
    - 恐贪 < 暂停线 → 全暂停
    - 默认 → 偏策略 B

    Returns:
        {"weight_a": float, "weight_b": float, "paused": bool, "reason": str}
    """
    session = get_session(settings.database_url)

    try:
        # 获取 BTC 最新波动率
        from sqlalchemy import func, desc
        btc_snap = (
            session.query(MarketSnapshot)
            .filter(MarketSnapshot.symbol == "BTC/USDT")
            .order_by(desc(MarketSnapshot.captured_at))
            .first()
        )

        # 获取山寨币平均波动率（排除 BTC/ETH/黄金/白银等）
        # 先取每个 symbol 最新快照，再算平均值
        exclude = ["BTC/USDT", "ETH/USDT", "XAU/USDT", "XAG/USDT", "CL/USDT", "PAXG/USDT", "TSLA/USDT"]
        subq = (
            session.query(
                MarketSnapshot.symbol,
                func.max(MarketSnapshot.captured_at).label("max_ts"),
            )
            .filter(
                MarketSnapshot.volatility_20d > 0,
                ~MarketSnapshot.symbol.in_(exclude),
            )
            .group_by(MarketSnapshot.symbol)
            .subquery()
        )
        alt_vols = (
            session.query(MarketSnapshot.volatility_20d)
            .join(
                subq,
                (MarketSnapshot.symbol == subq.c.symbol)
                & (MarketSnapshot.captured_at == subq.c.max_ts),
            )
            .all()
        )
        alt_vol = sum(v[0] for v in alt_vols) / len(alt_vols) if alt_vols else 0

        # 获取最新恐贪指数
        fear_greed = (
            session.query(FearGreedHistory)
            .order_by(desc(FearGreedHistory.timestamp))
            .first()
        )

        btc_vol = btc_snap.volatility_20d if btc_snap else 0
        fg_value = fear_greed.value if fear_greed else 50

        # 恐贪暂停检查
        if fg_value < settings.fear_greed_pause_line:
            result = {"weight_a": 0.0, "weight_b": 0.0, "paused": True, "reason": f"恐贪指数 {fg_value} < {settings.fear_greed_pause_line}，全暂停"}
            log.warning("strategy_paused", **result)
            return result

        # 策略切换
        ratio = alt_vol / btc_vol if btc_vol > 0 else 1.0
        threshold = settings.strategy_switch_threshold

        if ratio > threshold:
            weight_a, weight_b = 0.7, 0.3
            mode = f"山寨/BTC波动率比 {ratio:.2f} > {threshold}，偏策略A（追高）"
        else:
            weight_a, weight_b = 0.3, 0.7
            mode = f"山寨/BTC波动率比 {ratio:.2f} <= {threshold}，偏策略B（多因子）"

        result = {"weight_a": weight_a, "weight_b": weight_b, "paused": False, "reason": mode, "ratio": ratio, "fear_greed": fg_value}
        log.info("strategy_weights", **result)
        return result

    finally:
        session.close()


if __name__ == "__main__":
    w = get_strategy_weights()
    print(f"A={w['weight_a']} B={w['weight_b']} paused={w['paused']} reason={w['reason']}")
