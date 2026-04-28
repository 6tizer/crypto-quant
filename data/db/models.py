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
    price_change_pct = Column(Float, default=0)       # 涨跌幅 %
    volatility_20d = Column(Float, default=0)          # 20 日波动率标准差
    oi = Column(Float, default=0)                      # 当前 OI
    oi_change_48h_pct = Column(Float, default=0)       # 48h OI 变动 %
    listed_days = Column(Integer, default=0)            # 上线天数
    max_daily_move_pct = Column(Float, default=0)       # 历史最大单日波动 %
    funding_rate = Column(Float, default=0)             # 资金费率
    captured_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SignalScore(Base):
    """信号综合打分"""
    __tablename__ = "signal_scores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(30), nullable=False, index=True)
    score_total = Column(Float, default=0)
    score_momentum = Column(Float, default=0)          # 涨幅异动分
    score_oi_divergence = Column(Float, default=0)     # OI 背离分
    score_whitelist = Column(Float, default=0)          # 标的池加分
    score_fear_greed = Column(Float, default=0)         # 恐贪加分
    strategy_type = Column(String(5), default="A")      # A or B
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
    pnl = Column(Float, default=0)                     # 盈亏金额 (u)
    pnl_pct = Column(Float, default=0)
    strategy = Column(String(5), default="A")
    signal_score_id = Column(Integer, default=0)
    entry_reason = Column(Text, default="")             # JSON: 各信号贡献
    exit_reason = Column(String(30), default="")        # stop_loss / take_profit / manual / risk_guard
    opened_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at = Column(DateTime, nullable=True)


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
    engine = create_engine(db_url, echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def get_session(db_url: str) -> Session:
    """获取一个数据库 session（采集器用完即关）"""
    engine = create_engine(db_url, echo=False)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return factory()
