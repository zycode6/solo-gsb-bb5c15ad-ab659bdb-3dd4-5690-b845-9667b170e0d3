"""Pydantic request/response models."""
from __future__ import annotations

from datetime import date, time
from typing import Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .config import DEFAULT_TOLERANCE, ELEMENTS, normalize_element


class Concentrate(BaseModel):
    """One stock concentrate bottle.

    Concentrations are in mg of the *element* per L of concentrate
    (water-soluble N/P/K, elemental for micros). ``available_volume`` and
    ``max_volume`` are in mL.
    """

    name: str = Field(..., min_length=1, examples=["A液-钙肥"])
    available_volume: float = Field(..., gt=0, description="可用体积 (mL)")
    max_volume: Optional[float] = Field(
        None,
        gt=0,
        description="本配方中单液使用量上限 (mL)，缺省取可用体积",
    )
    concentrations: dict[str, float] = Field(
        default_factory=dict,
        description="各元素浓度 mg/L，键如 N/P/K/Ca/Mg/Fe ...",
    )

    # Reject misspelled keys (e.g. "concentrates" inside a concentrate)
    # instead of silently dropping them.
    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrations")
    @classmethod
    def _validate_concentrations(cls, v: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for key, val in v.items():
            elem = normalize_element(key)
            if elem is None:
                raise ValueError(
                    f"未知元素 '{key}'，支持: {', '.join(ELEMENTS)}"
                )
            if val < 0:
                raise ValueError(f"元素 {elem} 浓度不能为负")
            normalized[elem] = float(val)
        return normalized


class Targets(BaseModel):
    """Target nutrient concentrations of the final solution (mg/L)."""

    concentrations: dict[str, float]

    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrations")
    @classmethod
    def _validate_targets(cls, v: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for key, val in v.items():
            elem = normalize_element(key)
            if elem is None:
                raise ValueError(
                    f"未知元素 '{key}'，支持: {', '.join(ELEMENTS)}"
                )
            if val < 0:
                raise ValueError(f"元素 {elem} 目标不能为负")
            normalized[elem] = float(val)
        return normalized


class SourceWater(BaseModel):
    """Raw (source) water mineral baseline concentrations (mg/L).

    The dilution water is not necessarily distilled: it may already carry
    some of the tracked elements.  These concentrations contribute to the
    final solution in proportion to the *solved* dilution water volume,
    i.e. ``source_amount_e = source_concentration_e * w / 1000`` (mg).
    """

    concentrations: dict[str, float] = Field(
        default_factory=dict,
        description="原水各元素本底浓度 mg/L，键如 Ca/Mg/...",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrations")
    @classmethod
    def _validate_source(cls, v: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for key, val in v.items():
            elem = normalize_element(key)
            if elem is None:
                raise ValueError(
                    f"未知元素 '{key}'，支持: {', '.join(ELEMENTS)}"
                )
            if val < 0:
                raise ValueError(f"元素 {elem} 原水浓度不能为负")
            normalized[elem] = float(val)
        return normalized


class SolveRequest(BaseModel):
    target_volume_ml: float = Field(..., gt=0, description="目标配方总体积 (mL)")
    targets: Targets
    concentrates: list[Concentrate] = Field(..., min_length=1)
    source_water: Optional[SourceWater] = Field(
        None,
        description=(
            "原水矿物质基线（各元素浓度 mg/L）。可选，未传时按零处理；"
            "原水贡献随求得的稀释水量线性计入各元素实际量，"
            "不会按目标总体积预扣固定原水量。"
        ),
    )
    min_water_ml: float = Field(
        0,
        ge=0,
        description="稀释水最低用量 (mL)，用于强制保留稀释空间",
    )
    max_concentrate_fraction: float = Field(
        1.0,
        gt=0,
        le=1.0,
        description="浓缩液合计占总体积的最大比例 (0~1]",
    )
    tolerance: float = Field(
        DEFAULT_TOLERANCE,
        ge=0,
        le=1,
        description="可接受的最大相对偏差；超过则在 violations 中列出",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrates")
    @classmethod
    def _unique_names(cls, v: list[Concentrate]) -> list[Concentrate]:
        names = [c.name for c in v]
        if len(names) != len(set(names)):
            raise ValueError("浓缩液名称不能重复")
        return v


class ElementResult(BaseModel):
    element: str
    target_concentration: float
    actual_concentration: float
    target_amount_mg: float
    actual_amount_mg: float
    concentrate_amount_mg: float = Field(
        ..., description="浓缩液合计贡献的该元素量 (mg)"
    )
    source_water_amount_mg: float = Field(
        ...,
        description=(
            "原水本底贡献的该元素量 (mg) = 原水浓度 × 求得稀释水量 ÷ 1000；"
            "未传原水基线时为 0"
        ),
    )
    relative_deviation: Optional[float]
    status: str  # ok | under | over | unconstrained


class Violation(BaseModel):
    code: str
    message: str
    element: Optional[str] = None
    limit: Optional[float] = None
    actual: Optional[float] = None


class SolveResult(BaseModel):
    feasible: bool
    optimal: bool
    max_relative_deviation: Optional[float]
    total_volume_ml: float
    water_volume_ml: float
    concentrate_total_ml: float
    allocations: list[dict[str, float | str]]
    elements: list[ElementResult]
    violations: list[Violation] = []
    message: str = ""


class SaveRecipeRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    note: str = ""
    request: SolveRequest

    model_config = ConfigDict(extra="forbid")


class RecipeSummary(BaseModel):
    id: int
    name: str
    description: str
    created_at: str
    latest_version: int


class VersionSummary(BaseModel):
    id: int
    recipe_id: int
    version: int
    note: str
    created_at: str


class RecipeVersion(BaseModel):
    id: int
    recipe_id: int
    version: int
    note: str
    created_at: str
    request: dict
    result: dict


# --------------------------------------------------------------------------- #
# Irrigation planning
# --------------------------------------------------------------------------- #
# Planning over an unbounded range would make the response arbitrarily large;
# a full year + leap slack is far beyond any practical irrigation schedule.
MAX_PLAN_DAYS = 370


class IrrigationSessionInput(BaseModel):
    """One daily irrigation start in local wall-clock time."""

    start_time: time = Field(
        ...,
        description=(
            "本地开始时间 HH:MM 或 HH:MM:SS（按 timezone 解释，"
            "可落在前一日 00:00~次日 23:59 的普通一天内；持续到次日即跨午夜）"
        ),
    )
    duration_minutes: float = Field(..., gt=0, description="持续分钟数，必须为正")
    water_ml: float = Field(..., gt=0, description="本次目标水量 (mL)，必须为正")

    model_config = ConfigDict(extra="forbid")


class IrrigationScheduleBase(BaseModel):
    """Common validated daily-schedule fields (planning & forecasting)."""

    timezone: str = Field(
        ...,
        min_length=1,
        examples=["Asia/Shanghai"],
        description="IANA 时区名，如 Asia/Shanghai、America/New_York",
    )
    start_date: date = Field(..., description="起始日期（含，本地日历日）")
    end_date: date = Field(..., description="结束日期（含，本地日历日）")
    sessions: list[IrrigationSessionInput] = Field(
        ..., min_length=1, description="每日重复的灌溉开始时段（至少 1 个）"
    )
    suspended_dates: list[date] = Field(
        default_factory=list,
        description=(
            "本地停灌日期列表（按 timezone 解释的本地日历日）：列表中的日期"
            "不生成当日排定的灌溉批次，响应仍保留这些日期并标明停灌；"
            "不影响前一日本地日期开始、跨入该日的跨午夜批次；"
            "日期须落在 start_date~end_date 内且不可重复"
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        key = v.strip()
        if not key:
            raise ValueError("时区不能为空")
        try:
            ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(
                f"非法 IANA 时区 '{v}'，例如 Asia/Shanghai、"
                "Europe/London、America/New_York"
            ) from exc
        return key

    @field_validator("sessions")
    @classmethod
    def _unique_sessions(cls, v: list[IrrigationSessionInput]) -> list[IrrigationSessionInput]:
        seen: set[time] = set()
        for s in v:
            if s.start_time in seen:
                raise ValueError(
                    f"每日开始时间 {s.start_time.strftime('%H:%M:%S')} 重复"
                )
            seen.add(s.start_time)
        return v

    @field_validator("suspended_dates")
    @classmethod
    def _unique_suspended_dates(cls, v: list[date]) -> list[date]:
        seen: set[date] = set()
        for d in v:
            if d in seen:
                raise ValueError(f"停灌日期 {d.isoformat()} 重复")
            seen.add(d)
        return v

    @model_validator(mode="after")
    def _validate_range(self) -> "IrrigationScheduleBase":
        if self.end_date < self.start_date:
            raise ValueError(
                "end_date 不能早于 start_date"
                f"（{self.start_date.isoformat()} > "
                f"{self.end_date.isoformat()}）"
            )
        span = (self.end_date - self.start_date).days + 1
        if span > MAX_PLAN_DAYS:
            raise ValueError(
                f"日期范围最多 {MAX_PLAN_DAYS} 天，当前 {span} 天"
            )
        out_of_range = [
            d for d in self.suspended_dates
            if d < self.start_date or d > self.end_date
        ]
        if out_of_range:
            bad = ", ".join(d.isoformat() for d in sorted(out_of_range))
            raise ValueError(
                "停灌日期必须落在 "
                f"{self.start_date.isoformat()}~{self.end_date.isoformat()} "
                f"范围内：{bad}"
            )
        return self


class IrrigationPlanRequest(IrrigationScheduleBase):
    daily_capacity_ml: float = Field(
        ..., ge=0, description="设备每日供水容量 (mL)，非负；0 表示当日不可供水"
    )


class PlanSessionRef(BaseModel):
    """A constituent session occurrence inside a (possibly merged) batch."""

    day: str
    requested_time: str
    local_start: str
    dst_event: Optional[str] = None
    dst_note: Optional[str] = None
    water_ml: float


class IrrigationBatch(BaseModel):
    start_utc: str = Field(..., description="批次 UTC 开始时间 RFC3339")
    end_utc: str = Field(..., description="批次 UTC 结束时间 RFC3339")
    local_start: str = Field(..., description="本地显示开始时间（含 UTC 偏移）")
    local_end: str = Field(..., description="本地显示结束时间（含 UTC 偏移）")
    duration_seconds: int
    water_ml: float = Field(..., description="合并后总水量；超容也不截断")
    merged: bool
    batch_count_before_merge: int
    dst_event: Optional[str] = Field(
        None, description="gap=夏令时缺失本地时间（已顺延）；duplicate=重复本地时间"
    )
    dst_note: Optional[str] = None
    crosses_midnight: bool
    sources: list[PlanSessionRef]


class CapacityViolation(BaseModel):
    date: str
    planned_water_ml: float
    daily_capacity_ml: float
    shortfall_ml: float = Field(..., description="明确缺口 = 计划水量 - 日容量")
    message: str


class IrrigationDayResult(BaseModel):
    date: str
    suspended: bool = Field(
        False, description="当日是否为停灌日：停灌日不生成当日排定批次，水量为 0"
    )
    batch_count_before_merge: int
    batch_count_after_merge: int
    planned_water_ml: float
    daily_capacity_ml: float
    within_capacity: bool
    shortfall_ml: float
    dst_events: list[str]


class IrrigationPlanResult(BaseModel):
    timezone: str
    start_date: str
    end_date: str
    days: int
    suspended_dates: list[str] = Field(
        default_factory=list,
        description="请求中的停灌日期（原样保留，已按日期升序排列）；这些日期不排定批次",
    )
    batch_count_before_merge: int
    batch_count_after_merge: int
    merge_gap_seconds: int
    planned_water_ml: float
    daily_capacity_ml: float
    capacity_ok: bool
    capacity_violations: list[CapacityViolation]
    day_results: list[IrrigationDayResult]
    batches: list[IrrigationBatch]


# --------------------------------------------------------------------------- #
# Concentrate consumption forecast
# --------------------------------------------------------------------------- #
class StockLevel(BaseModel):
    """Current on-hand volume of one concentrate (mL)."""

    concentrate: str = Field(
        ..., min_length=1, description="配方版本求解结果中的浓缩液名称"
    )
    stock_ml: float = Field(..., ge=0, description="当前库存 (mL)，不能为负")

    model_config = ConfigDict(extra="forbid")


class ConsumptionForecastRequest(IrrigationScheduleBase):
    """Irrigation schedule + current stock levels for one recipe version."""

    stock: list[StockLevel] = Field(
        ..., min_length=1, description="各浓缩液当前库存；名称须与配方求解结果一致"
    )

    @field_validator("stock")
    @classmethod
    def _unique_stock(cls, v: list[StockLevel]) -> list[StockLevel]:
        names = [s.concentrate for s in v]
        if len(names) != len(set(names)):
            raise ValueError("库存浓缩液名称不能重复")
        return v


class ForecastLiquidUse(BaseModel):
    """Per-concentrate usage / gap in one batch, or on one day."""

    concentrate: str
    required_ml: float = Field(..., description="按比例应消耗 (mL)")
    consumed_ml: float = Field(
        ..., description="实际扣减 (mL)；库存不足时仅扣到 0，不会出现负库存"
    )
    shortfall_ml: float = Field(..., description="本批缺口 = 应消耗 - 实际扣减")


class ForecastBatch(BaseModel):
    """One actual (post-merge) irrigation batch with concentrate draw-down."""

    index: int = Field(..., description="按执行顺序（UTC 开始时间）的批次序号，从 1 起")
    start_utc: str
    local_start: str
    local_end: str
    attributed_date: str = Field(
        ...,
        description=(
            "归属本地日：取批次本地开始日；跨午夜/合并批次整体只计入这一天，"
            "不重复扣减"
        ),
    )
    water_ml: float
    merged: bool
    batch_count_before_merge: int
    crosses_midnight: bool
    sufficient: bool = Field(..., description="本批所有浓缩液库存是否都够")
    liquids: list[ForecastLiquidUse]


class ForecastDayLiquid(BaseModel):
    concentrate: str
    required_ml: float
    consumed_ml: float
    shortfall_ml: float
    remaining_ml: float = Field(..., description="当日结束后的剩余库存（≥ 0）")


class ForecastDay(BaseModel):
    date: str
    suspended: bool = Field(
        False, description="当日是否为停灌日：停灌日不排定批次、不扣减库存"
    )
    batches: int = Field(..., description="归属本日的实际（合并后）批次数")
    water_ml: float
    sufficient: bool
    liquids: list[ForecastDayLiquid]


class ForecastLiquidSummary(BaseModel):
    concentrate: str
    initial_stock_ml: float
    mix_ml_per_l: float = Field(
        ..., description="每升灌溉水需该浓缩液 mL（配方分配体积 / 目标体积）"
    )
    total_required_ml: float
    total_consumed_ml: float
    total_shortfall_ml: float
    remaining_ml: float
    first_shortfall_date: Optional[str] = None


class ConsumptionForecastResult(BaseModel):
    recipe_id: int
    version: int
    recipe_feasible: bool = Field(
        ..., description="对应配方版本求解结果是否在容差内可行"
    )
    warning: Optional[str] = Field(
        None, description="配方版本不可行时的提示；可行时为 null"
    )
    timezone: str
    start_date: str
    end_date: str
    days: int
    suspended_dates: list[str] = Field(
        default_factory=list,
        description="请求中的停灌日期（原样保留，已按日期升序排列）；这些日期不排定批次、不扣减库存",
    )
    batch_count_before_merge: int
    batch_count_after_merge: int
    planned_water_ml: float
    sufficient: bool = Field(..., description="整个计划周期内库存是否始终充足")
    first_shortfall_date: Optional[str] = Field(
        None, description="首个出现任一浓缩液缺口的本地日期；全程充足为 null"
    )
    liquids: list[ForecastLiquidSummary]
    day_results: list[ForecastDay]
    batches: list[ForecastBatch]
