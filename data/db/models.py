"""SQLite 数据模型"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class MarketSnapshot(Base):
    """行情快照 — 每 5min 采集"""
    __tablename__ = "market_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(30), nullable=False, index=True)
    price = Column(Float, nullable=False)
    volume_24h = Column(Float, default=0)
    price_change_pct = Column(Float, default=0)
    volatility_20d = Column(Float, default=0)
    oi = Column(Float, default=0)
    oi_change_48h_pct = Column(Float, default=0)
    listed_days = Column(Integer, default=0)
    max_daily_move_pct = Column(Float, default=0)
    funding_rate = Column(Float, default=0)
    captured_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class SignalScore(Base):
    """信号综合打分 — 含策略 A/B 全部字段"""
    __tablename__ = "signal_scores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(30), nullable=False, index=True)
    score_total = Column(Float, default=0)
    score_square_heat = Column(Float, default=0)       # 广场热度（阶段 5）
    score_momentum = Column(Float, default=0)           # 涨幅异动
    score_oi_divergence = Column(Float, default=0)      # OI 背离
    score_whitelist = Column(Float, default=0)           # 标的池加分
    score_mvrv = Column(Float, default=0)                # MVRV（阶段 2+）
    score_sopr = Column(Float, default=0)                # SOPR（阶段 2+）
    score_kronos = Column(Float, default=0)              # Kronos（阶段 5+）
    score_smart_money = Column(Float, default=0)         # 聪明钱（阶段 5+）
    score_fear_greed = Column(Float, default=0)          # 恐贪
    strategy_type = Column(String(5), default="A")
    captured_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Trade(Base):
    """交易记录"""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(30), nullable=False, index=True)
    side = Column(String(10), default="LONG")
    entry_price = Column(Float, default=0)
    exit_price = Column(Float, default=0)
    stop_loss_price = Column(Float, default=0)
    quantity = Column(Float, default=0)
    pnl = Column(Float, default=0)
    pnl_pct = Column(Float, default=0)
    strategy = Column(String(5), default="A")
    signal_score_id = Column(Integer, default=0)
    entry_reason = Column(Text, default="")
    exit_reason = Column(String(30), default="")
    opened_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at = Column(DateTime, nullable=True)
    peak_pnl = Column(Float, default=0)  # P1-⑥: trailing stop 用
    half_closed = Column(Boolean, default=False)   # 1.5x 已平半
    closed_3x = Column(Boolean, default=False)     # 3x 已平 75%
    risk_amount = Column(Float, default=0)         # 开仓时的风险金额


class RiskState(Base):
    """风控状态"""
    __tablename__ = "risk_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    consecutive_stops = Column(Integer, default=0)
    daily_loss = Column(Float, default=0)
    total_drawdown_pct = Column(Float, default=0)
    is_paused = Column(Boolean, default=False)
    pause_reason = Column(String(200), default="")
    pause_until = Column(DateTime, nullable=True)
    peak_equity = Column(Float, default=0)  # P1-⑦: peak-to-trough 回撤用
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SquarePost(Base):
    """币安广场帖子 — 阶段 5 才用，表先建好"""
    __tablename__ = "square_posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    post_id = Column(String(50), unique=True, nullable=False)
    coin_symbol = Column(String(30), default="")
    author_name = Column(String(100), default="")
    author_renamed = Column(Boolean, default=False)
    post_count_24h = Column(Integer, default=0)
    content_hash = Column(String(64), default="")
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    is_bot = Column(Boolean, default=False)
    bot_score = Column(Float, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class OnchainMetric(Base):
    """链上指标（TVL 等）"""
    __tablename__ = "onchain_metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    metric_name = Column(String(100), nullable=False, index=True)
    value = Column(Float, default=0)
    captured_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class FearGreedHistory(Base):
    """恐惧贪婪指数历史"""
    __tablename__ = "fear_greed_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    value = Column(Integer, nullable=False)
    classification = Column(String(30), nullable=False)
    timestamp = Column(DateTime, nullable=False, unique=True)


def init_db(db_url: str) -> sessionmaker:
    """初始化数据库，返回 Session 工厂"""
    engine = _get_engine(db_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def get_session(db_url: str) -> Session:
    """获取一个数据库 session（使用全局 engine 单例）"""
    factory = init_db(db_url)
    return factory()


# 全局 engine 单例，避免每次 get_session 都新建连接
_engine_cache: dict[str, Engine] = {}


def _get_engine(db_url: str) -> Engine:
    """获取或创建 engine 单例"""
    if db_url not in _engine_cache:
        _engine_cache[db_url] = create_engine(db_url, echo=False, pool_pre_ping=True)
    return _engine_cache[db_url]
