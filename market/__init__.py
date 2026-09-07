"""Данные: MOEX ISS, сборка таймфрейма, SQLite-хранилище свечей.

Слой отдаёт свечи и всё. Торговой логики здесь нет: ни средних, ни сигналов,
ни торгового окна — и слой не знает, кто и зачем читает свечи. Ни одного
импорта другого слоя проекта (ARCHITECTURE.md §2).

Три правила, от которых зависят деньги, записаны в модулях, где они действуют:

* границы и время свечи — `market.aggregate` (свеча 10:05 при пяти минутах
  включает минутки 10:05…10:09 и закрывается в 10:10);
* какой источник побеждает при конфликте — `market.storage`
  (биржа выше потока брокера, поток выше догрузки у брокера);
* в чём измерен объём свечи и что делать с источником, который даёт
  деньги, — `market.volume` (в базе контракты, всегда);
* где дыра в ряду и что из ответа брокера можно записать — `market.backfill`
  (ищется дыра, а не хвост; существующая минута не переписывается);
* что считается разрывом и почему слой не делает выводов — `market.gaps`;
* чем боевой журнал отличается от прогона по истории и почему строку журнала
  нельзя переписать задним числом — `market.journal` и раздел «Журналы»
  в `market.storage`.

Слой синхронный. Сквозной вход для окна и движка — `market.worker`:
один выделенный поток, внутри которого живёт соединение с базой
([решение 0005](../.docs/decisions/0005-concurrency-model.md)).
"""

from market.aggregate import bar_start, build_bars
from market.backfill import (
    MATCH_BAND,
    MAX_SPAN,
    NOTHING_TO_ANCHOR,
    Picked,
    Plan,
    holes,
    pick_new,
    plan,
    suspicious,
    volume_ratio,
)
from market.candles import (
    M5,
    MINUTE,
    MSK,
    Candle,
    Timeframe,
    ensure_msk,
    floor_to_minute,
)
from market.chain import (
    Leg,
    Seam,
    StitchReport,
    daily_volume,
    legs_of_chain,
    measure_seam,
    roll_day,
    stitch,
)
from market.depth import Depth, declared_depth, measured_depth
from market.fee import Commission, read_commission
from market.gaps import Gap, count_missing_minutes, find_gaps
from market.history import (
    DEFAULT_DEPTH_DAYS,
    DEFAULT_WARMUP_BARS,
    WARMUP_PERIOD,
    WARMUP_RESIDUAL,
    HistoryOutcome,
    HistoryRequest,
    load_history,
    minute_border,
    warmup_bars,
)
from market.inventory import (
    DENSE_SHARE,
    DayFill,
    Inventory,
    MonthFill,
    observed_session,
    take_inventory,
)
from market.iss import (
    FUTURES,
    MARKETS,
    MINUTE_INTERVAL,
    SHARES,
    CandleBorder,
    FetchResult,
    HttpxTransport,
    InstrumentSpec,
    IssClient,
    IssError,
    IssHttpError,
    IssPagingError,
    IssPayloadError,
    IssStopped,
    IssTransportError,
    IssUnknownInstrument,
    Market,
    candles_url,
    check_interval,
    parse_borders,
    parse_candles,
    parse_security,
    securities_url,
)
from market.journal import (
    BACKTEST_SESSIONS_KEPT,
    SECRET_MASK,
    DecisionLevel,
    DecisionRecord,
    HaltKind,
    HaltRecord,
    JournalPage,
    JournalSession,
    JournalStats,
    PruneReport,
    RunOrigin,
    SessionRecord,
    StoredDecision,
    StoredHalt,
    StoredTrade,
    TradeRecord,
    TradeSide,
    redact,
)
from market.paths import default_db_path, ensure_userdata_dir, userdata_dir
from market.point import MARKETS_PROBED, PointValue, ask_point_value
from market.reports import LoadReport
from market.storage import SCHEMA_VERSION, CandleStore, Coverage, Source, WriteStats
from market.sync import (
    CHUNK_DAYS,
    EMPTY_CHUNK_TRUSTED_DAYS,
    GAP_THRESHOLD_MINUTES,
    sync_minutes,
)
from market.synthetic import (
    SYNTHETIC_PREFIX,
    is_synthetic,
    refuse_synthetic_for_trading,
    refuse_synthetic_in_working_base,
)
from market.volume import CONTRACT_HALF, PRICE_OF, bar_price, contracts_from_turnover
from market.worker import MarketWorker, WorkerClosed

__all__ = [
    "SYNTHETIC_PREFIX",
    "is_synthetic",
    "refuse_synthetic_for_trading",
    "refuse_synthetic_in_working_base",
    "Leg",
    "Seam",
    "StitchReport",
    "daily_volume",
    "legs_of_chain",
    "measure_seam",
    "roll_day",
    "stitch",
    "MSK",
    "MINUTE",
    "M5",
    "Candle",
    "Timeframe",
    "ensure_msk",
    "floor_to_minute",
    "bar_start",
    "build_bars",
    "MAX_SPAN",
    "NOTHING_TO_ANCHOR",
    "Plan",
    "Picked",
    "holes",
    "plan",
    "pick_new",
    "volume_ratio",
    "suspicious",
    "MATCH_BAND",
    "CONTRACT_HALF",
    "PRICE_OF",
    "bar_price",
    "contracts_from_turnover",
    "Gap",
    "find_gaps",
    "count_missing_minutes",
    "DEFAULT_DEPTH_DAYS",
    "WARMUP_RESIDUAL",
    "HistoryRequest",
    "HistoryOutcome",
    "load_history",
    "minute_border",
    "warmup_bars",
    "DENSE_SHARE",
    "DayFill",
    "MonthFill",
    "Inventory",
    "take_inventory",
    "observed_session",
    "Market",
    "MARKETS",
    "SHARES",
    "FUTURES",
    "IssClient",
    "HttpxTransport",
    "FetchResult",
    "CandleBorder",
    "IssError",
    "IssTransportError",
    "IssPayloadError",
    "IssPagingError",
    "IssHttpError",
    "IssStopped",
    "IssUnknownInstrument",
    "InstrumentSpec",
    "parse_security",
    "MARKETS_PROBED",
    "PointValue",
    "ask_point_value",
    "Commission",
    "read_commission",
    "securities_url",
    "MINUTE_INTERVAL",
    "check_interval",
    "candles_url",
    "parse_candles",
    "parse_borders",
    "CandleStore",
    "Coverage",
    "Source",
    "WriteStats",
    "SCHEMA_VERSION",
    "RunOrigin",
    "DecisionLevel",
    "TradeSide",
    "HaltKind",
    "HaltRecord",
    "StoredHalt",
    "SessionRecord",
    "JournalSession",
    "DecisionRecord",
    "StoredDecision",
    "TradeRecord",
    "StoredTrade",
    "JournalPage",
    "JournalStats",
    "PruneReport",
    "BACKTEST_SESSIONS_KEPT",
    "SECRET_MASK",
    "redact",
    "LoadReport",
    "sync_minutes",
    "GAP_THRESHOLD_MINUTES",
    "CHUNK_DAYS",
    "EMPTY_CHUNK_TRUSTED_DAYS",
    "MarketWorker",
    "WorkerClosed",
    "Depth",
    "declared_depth",
    "measured_depth",
    "userdata_dir",
    "default_db_path",
    "ensure_userdata_dir",
]
