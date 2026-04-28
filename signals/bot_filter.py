"""Bot 过滤规则引擎 — 框架先行，数据阶段 5 才有"""

import structlog

log = structlog.get_logger()


def compute_bot_score(
    author_renamed: bool = False,
    post_count_24h: int = 0,
    content_similarity: float = 0.0,
    account_age_days: int = 999,
) -> float:
    """Bot 打分（来自 lana 第 3 条规则）。

    规则：
    - 改名 = 真人 (+1)
    - 未改名 + 高频发帖 >10/24h = bot (-1)
    - 内容重复度 >80% = bot (-1)
    - 账号年龄 <7 天 = bot (-1)

    Args:
        author_renamed: 是否改过名
        post_count_24h: 24h 发帖数
        content_similarity: 内容相似度 0-1
        account_age_days: 账号天数

    Returns:
        -3 到 +1 的分数（越高越像真人）
    """
    score = 0.0

    if author_renamed:
        score += 1.0
    else:
        if post_count_24h > 10:
            score -= 1.0

    if content_similarity > 0.8:
        score -= 1.0

    if account_age_days < 7:
        score -= 1.0

    return score


def is_bot(bot_score: float) -> bool:
    """判断是否为 bot。score < 0 视为 bot。"""
    return bot_score < 0


# Mock data test
if __name__ == "__main__":
    test_cases = [
        {"author_renamed": True, "post_count_24h": 2, "content_similarity": 0.1, "account_age_days": 100},
        {"author_renamed": False, "post_count_24h": 15, "content_similarity": 0.9, "account_age_days": 3},
        {"author_renamed": False, "post_count_24h": 5, "content_similarity": 0.5, "account_age_days": 30},
        {"author_renamed": False, "post_count_24h": 12, "content_similarity": 0.85, "account_age_days": 5},
    ]
    for tc in test_cases:
        score = compute_bot_score(**tc)
        print(f"score={score:+.0f} bot={is_bot(score)} | {tc}")
