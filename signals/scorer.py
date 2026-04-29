"""综合打分引擎 — 读取各信号分数，加权求和，写入 signal_scores"""

from datetime import datetime, timezone

import structlog

from config.settings import settings
from data.db.models import FearGreedHistory, SignalScore, get_session

log = structlog.get_logger()


def _get_fear_greed_score() -> float:
    """从 fear_greed_history 读最新值，归一化到 0-1。

    恐贪 0-100 映射：>=80 → 1.0（极度贪婪），<=20 → 0.0（极度恐惧），线性插值。
    """
    session = get_session(settings.database_url)
    try:
        from sqlalchemy import desc
        row = session.query(FearGreedHistory).order_by(desc(FearGreedHistory.timestamp)).first()
        if row:
            return max(0.0, min(1.0, (row.value - 20) / 60))
        return 0.5  # 无数据时中性
    finally:
        session.close()


def compute_strategy_a_score(
    momentum_score: float = 0,
    oi_divergence_score: float = 0,
    whitelist_score: float = 0,
    fear_greed_score: float = 0,
) -> float:
    """策略 A（追高）加权打分。

    权重从 settings 读取：
    - momentum: 0.35
    - square_heat: 0.25（阶段 5 才有，暂不参与）
    - oi_divergence: 0.20
    - whitelist: 0.10
    - kronos: 0.10（阶段 5 后补，暂不参与）

    缺失信号的权重按比例重新分配给已有信号。
    """
    # 有值的信号和权重
    signals = {
        "momentum": (momentum_score, settings.weight_a_momentum),
        "oi_divergence": (oi_divergence_score, settings.weight_a_oi_divergence),
        "whitelist": (whitelist_score, settings.weight_a_whitelist),
        "fear_greed": (fear_greed_score, 0.05),  # 小权重辅助
    }

    total_weight = sum(w for _, w in signals.values())
    if total_weight <= 0:
        return 0.0

    weighted_sum = sum(score * weight for score, weight in signals.values())
    # 归一化到 0-1（权重总和可能 < 1.0 因为缺 square_heat 和 kronos）
    return min(1.0, weighted_sum / total_weight)


def score_all_symbols() -> list[dict]:
    """对全部币种执行信号打分流程。

    Returns:
        Top N 排序后的结果 [{symbol, total_score, ...}, ...]
    """
    from signals.momentum import compute_all_signals
    from signals.whitelist import get_whitelist_symbols

    # 1. 计算各信号
    momentum_results = compute_all_signals()
    whitelist_results = get_whitelist_symbols()

    # 构建 whitelist 查找表
    wl_map = {r["symbol"]: r["whitelist_score"] for r in whitelist_results}

    # 1.5 读最新恐贪指数
    fg_score = _get_fear_greed_score()

    # 2. 综合打分
    scored = []
    for m in momentum_results:
        sym = m["symbol"]
        total = compute_strategy_a_score(
            momentum_score=m["momentum_score"],
            oi_divergence_score=m["oi_divergence_score"],
            whitelist_score=wl_map.get(sym, 0),
            fear_greed_score=fg_score,
        )
        scored.append({
            "symbol": sym,
            "score_total": round(total, 4),
            "score_momentum": m["momentum_score"],
            "score_oi_divergence": m["oi_divergence_score"],
            "score_whitelist": wl_map.get(sym, 0),
            "strategy_type": "A",
        })

    # 3. 按 score 降序排列
    scored.sort(key=lambda x: x["score_total"], reverse=True)

    # 4. 写入数据库
    session = get_session(settings.database_url)
    now = datetime.now(timezone.utc)
    try:
        for s in scored:
            record = SignalScore(
                symbol=s["symbol"],
                score_total=s["score_total"],
                score_momentum=s["score_momentum"],
                score_oi_divergence=s["score_oi_divergence"],
                score_whitelist=s["score_whitelist"],
                score_square_heat=0,  # 阶段 5
                score_mvrv=0,         # 阶段 2+
                score_sopr=0,         # 阶段 2+
                score_kronos=0,       # 阶段 5+
                score_smart_money=0,  # 阶段 5+
                score_fear_greed=fg_score,   # 从 DB 读
                strategy_type=s["strategy_type"],
                captured_at=now,
            )
            session.add(record)
        session.commit()
        log.info("scores_saved", count=len(scored))
    except Exception as e:
        session.rollback()
        log.error("scores_save_failed", error=str(e))
        raise
    finally:
        session.close()

    return scored


if __name__ == "__main__":
    results = score_all_symbols()
    print(f"\n=== Top 10 by total score (Strategy A) ===")
    for r in results[:10]:
        print(f'  {r["symbol"]:15s} total={r["score_total"]:.3f} momentum={r["score_momentum"]:.1f} oi={r["score_oi_divergence"]:.1f} wl={r["score_whitelist"]:.1f}')
    print(f"\nTotal scored: {len(results)}")
