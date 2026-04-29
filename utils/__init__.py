"""符号格式统一转换。

内部统一用 BASE/USDT 格式（如 SKYAI/USDT）。
与交易所交互时转换为 BASE/USDT:USDT 格式。

所有符号格式转换必须走这两个函数，禁止手动拼/删后缀。
"""


def to_exchange_symbol(symbol: str) -> str:
    """DB 格式 → 交易所格式。

    SKYAI/USDT → SKYAI/USDT:USDT
    SKYAI/USDT:USDT → SKYAI/USDT:USDT（幂等）
    """
    if ":USDT" not in symbol:
        return f"{symbol}:USDT"
    return symbol


def to_db_symbol(symbol: str) -> str:
    """交易所格式 → DB 格式。

    SKYAI/USDT:USDT → SKYAI/USDT
    SKYAI/USDT → SKYAI/USDT（幂等）
    """
    return symbol.replace(":USDT", "")
