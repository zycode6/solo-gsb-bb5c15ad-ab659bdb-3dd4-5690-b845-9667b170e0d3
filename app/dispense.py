"""Integer pump-count dispense planning for a saved recipe version.

Where :mod:`app.solver` decides *how many mL* of each concentrate an ideal
(continuous) mix needs, dispensing hardware can only pump whole shots of a
fixed size.  Given one shot size ``p_i`` (mL) per concentrate *i*, this
module chooses non-negative **integer** pump counts ``k_i`` minimizing the
worst-case relative deviation across all targeted elements:

* concentrate volume: ``S = sum_i p_i * k_i``
* dilution water fills the remainder: ``w = V - S``
* delivered amount (mg) of element ``e``::

      sum_i c_{i,e} * p_i * k_i / 1000 + s_e * w

  with ``c`` the concentrate concentration (mg/L) and ``s_e`` the raw-water
  baseline (mg/L).  The source-water contribution is therefore computed with
  the *actual* dilution water volume ``w`` (which shrinks as more concentrate
  is pumped), never a fixed ``s_e * V`` pre-deduction.

Hard constraints (all upper bounds on total pumped volume ``S``)::

      k_i >= 0 integer
      p_i * k_i <= min(available_volume_i, max_volume_i)   (单液上限)
      S       <= max_concentrate_fraction * V              (浓缩液占比)
      w = V - S >= min_water_ml                            (稀释水下限)

The last two are upper bounds on ``S``; pumping nothing (all ``k_i = 0``)
always satisfies them unless the version itself carries contradictory
volume settings (same checks as the continuous solver).

The minimax problem is a small mixed-integer linear program solved with
``scipy.optimize.milp`` (HiGHS).  When the integer optimum still misses the
version tolerance, auxiliary MILPs pinpoint shortage vs. companion-element
surplus exactly as :mod:`app.solver` does for the continuous case.
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from .models import SolveRequest

_EPS = 1e-7
# cap (pumps) below which a variable is considered bound in a diagnostic MILP
_BIND_EPS = 1e-6


class DispensePlanError(ValueError):
    """Pump configuration does not match the recipe version."""


def _violation(code: str, message: str, **kw) -> dict:
    v = {"code": code, "message": message}
    v.update({k: v_ for k, v_ in kw.items() if v_ is not None})
    return v


def _r(x: float, ndigits: int = 4) -> float:
    """Round for output, normalizing -0.0 from float arithmetic."""
    v = round(float(x), ndigits)
    return 0.0 if v == 0 else v


def build_dispense_plan(
    *,
    recipe_id: int,
    version: int,
    req: SolveRequest,
    solve_result: dict[str, Any],
    steps: dict[str, float],
) -> dict[str, Any]:
    """Plan integer pump counts for one saved recipe version.

    ``steps`` maps every concentrate name of the version's request to its
    single-shot pump volume (mL).  Raises :class:`DispensePlanError` for
    unknown / missing / oversized pump configurations.
    """
    concentrates = req.concentrates
    n = len(concentrates)
    names = [c.name for c in concentrates]
    V = float(req.target_volume_ml)
    tol = float(req.tolerance)

    # -- 1. pump configuration must match the version exactly -------------- #
    unknown = sorted(set(steps) - set(names))
    if unknown:
        raise DispensePlanError(
            f"泵配置中存在该版本未使用的未知浓缩液：{ '、'.join(unknown) }；"
            f"该版本浓缩液为：{ '、'.join(names) }。"
        )
    missing = [nm for nm in names if nm not in steps]
    if missing:
        raise DispensePlanError(
            f"缺少浓缩液 { '、'.join(missing) } 的泵配置；"
            "必须为该版本中的每一种浓缩液提供单次泵送量。"
        )

    caps = np.array(
        [
            min(c.available_volume, c.max_volume or c.available_volume)
            for c in concentrates
        ],
        dtype=float,
    )
    pump = np.array([float(steps[nm]) for nm in names], dtype=float)
    for i in range(n):
        if pump[i] > caps[i] + _EPS:
            raise DispensePlanError(
                f"浓缩液 {names[i]} 的单次泵送量 {pump[i]:g} mL 超过其单液"
                f"可用/使用上限 {caps[i]:g} mL，连泵送一次都无法执行；"
                "请调小单次泵送量或补充库存。"
            )

    # concentrations c [mg/L] and raw-water baseline s [mg/L] -> mg per mL
    target_conc = dict(req.targets.concentrations)
    targets = {e: conc * V / 1000.0 for e, conc in target_conc.items()}
    c_mat = {
        e: np.array(
            [c.concentrations.get(e, 0.0) / 1000.0 for c in concentrates],
            dtype=float,
        )
        for e in targets
    }
    source_conc = (
        dict(req.source_water.concentrations)
        if req.source_water is not None
        else {}
    )
    s_vec = {e: source_conc.get(e, 0.0) / 1000.0 for e in targets}

    # max integer shots per bottle and max total concentrate volume (mL)
    kmax = np.array(
        [int(math.floor(caps[i] / pump[i] + _EPS)) for i in range(n)],
        dtype=int,
    )
    grid_max = float(np.sum(pump * kmax))
    s_cap = float(req.max_concentrate_fraction * V)
    s_max = min(s_cap, grid_max)

    def _delivered_coeffs(e: str) -> tuple[float, np.ndarray]:
        """Return (constant b_e, per-pump coefficients d_e).

        delivered_e = s_e*(V - S) + sum_i c_i p_i k_i / 1000
                    = s_e*V + sum_i p_i*(c_i/1000 - s_e)*k_i
        """
        s = s_vec[e]
        d = pump * (c_mat[e] - s)
        return s * V, d

    def _report(k: np.ndarray) -> tuple[list[dict], Optional[float]]:
        return _element_report(
            targets=targets,
            target_conc=target_conc,
            c_mat=c_mat,
            s_vec=s_vec,
            V=V,
            tol=tol,
            pump=pump,
            k=k,
        )

    base = {
        "recipe_id": recipe_id,
        "version": version,
        "tolerance": tol,
        "total_volume_ml": _r(V),
        "min_water_ml": _r(float(req.min_water_ml)),
        "max_concentrate_fraction": float(req.max_concentrate_fraction),
    }
    names_meta = [
        {
            "concentrate": concentrates[i].name,
            "pump_step_ml": _r(pump[i]),
            "cap_ml": float(caps[i]),
            "max_pumps": int(kmax[i]),
            "available_volume_ml": float(concentrates[i].available_volume),
        }
        for i in range(n)
    ]

    def _assemble(
        *,
        feasible: bool,
        optimal: bool,
        k: np.ndarray,
        elements: list[dict],
        max_dev: Optional[float],
        violations: list[dict],
        message: str,
    ) -> dict[str, Any]:
        S = float(np.sum(pump * k))
        w = V - S
        allocations = []
        for i, meta in enumerate(names_meta):
            allocations.append(
                {
                    **meta,
                    "pump_count": int(k[i]),
                    "volume_ml": _r(pump[i] * k[i]),
                }
            )
        recipe_feasible = bool(solve_result.get("feasible", True))
        warning = None
        if not recipe_feasible:
            warning = (
                "该配方版本的连续求解结果在容差范围内不可行（feasible=false）；"
                "本泵送方案按版本的目标浓度、总体积、原水基线与体积约束重新"
                "求最优整数解，结果仅供参考。"
            )
        within = bool(
            feasible and max_dev is not None and max_dev <= tol + _EPS
        )
        return {
            **base,
            "recipe_feasible": recipe_feasible,
            "warning": warning,
            "feasible": feasible,
            "optimal": optimal,
            "within_tolerance": within,
            "max_relative_deviation": (
                _r(max_dev, 6) if max_dev is not None else None
            ),
            "water_volume_ml": _r(w),
            "concentrate_total_ml": _r(S),
            "allocations": allocations,
            "elements": elements,
            "violations": violations,
            "message": message,
        }

    # -- 2. hard volume sanity checks (same semantics as continuous solver) #
    hard = _hard_volume_checks(req, V)
    if hard:
        k_zero = np.zeros(n, dtype=int)
        elements, max_dev = _report(k_zero)
        return _assemble(
            feasible=False,
            optimal=False,
            k=k_zero,
            elements=elements,
            max_dev=max_dev,
            violations=hard,
            message="硬约束冲突，不存在满足体积/稀释水约束的泵送方案。",
        )

    # -- 3. minimax integer LP --------------------------------------------- #
    res = _minimax_milp(targets, _delivered_coeffs, pump, kmax, s_max)
    if res is None or res.x is None:
        k_zero = np.zeros(n, dtype=int)
        elements, max_dev = _report(k_zero)
        return _assemble(
            feasible=False,
            optimal=False,
            k=k_zero,
            elements=elements,
            max_dev=max_dev,
            violations=[_violation("SOLVER_ERROR", "整数规划求解器未能完成计算。")],
            message="求解失败，请检查版本参数与泵送配置。",
        )

    k = _clean_counts(res.x[:n], kmax)
    elements, max_dev = _report(k)
    t_star = max_dev if max_dev is not None else 0.0

    if t_star <= tol + _EPS:
        return _assemble(
            feasible=True,
            optimal=bool(res.success),
            k=k,
            elements=elements,
            max_dev=max_dev,
            violations=[],
            message=(
                "整数泵送方案求解成功：所有泵均按整泵次投料，"
                "稀释水补足剩余体积，各目标元素偏差均在版本容差范围内。"
            ),
        )

    # -- 4. diagnose why the integer grid cannot meet the tolerance band --- #
    violations = _diagnose(
        targets=targets,
        c_mat=c_mat,
        delivered_coeffs=_delivered_coeffs,
        pump=pump,
        kmax=kmax,
        s_max=s_max,
        k_star=k,
        tol=tol,
        names=names,
    )
    return _assemble(
        feasible=True,
        optimal=bool(res.success),
        k=k,
        elements=elements,
        max_dev=max_dev,
        violations=violations,
        message=(
            f"整数泵送步长无法在容差内达标：最优最大相对偏差为 "
            f"{t_star:.2%}，超过版本容差 {tol:.2%}；仍返回该最优可执行"
            "方案（稀释水补足剩余体积），详见 violations。"
        ),
    )


# --------------------------------------------------------------------------- #
# MILP builders
# --------------------------------------------------------------------------- #
def _clean_counts(x_counts: np.ndarray, kmax: np.ndarray) -> np.ndarray:
    """Round solver output to integer pump counts and clamp to the bounds."""
    k = np.rint(np.asarray(x_counts, dtype=float)).astype(int)
    return np.minimum(np.maximum(k, 0), kmax.astype(int))


def _grid_constraints(
    targets: dict[str, float],
    delivered_coeffs,
    pump: np.ndarray,
    s_max: float,
) -> tuple[np.ndarray, list[float], list[float]]:
    """Build minimax inequalities lb <= A z <= ub over z=(k..., t).

    Includes the total-volume cap ``sum p_i k_i <= s_max`` plus the
    minimax deviation bands for every element with a positive target.
    """
    n = len(pump)
    dim = n + 1
    rows: list[np.ndarray] = []
    lbs: list[float] = []
    ubs: list[float] = []

    total = np.zeros(dim)
    total[:n] = pump
    rows.append(total)
    lbs.append(-np.inf)
    ubs.append(s_max)

    for e, T in targets.items():
        if T <= _EPS:
            continue
        b_e, d = delivered_coeffs(e)
        # b + d·k <= T(1 + t)   ->  d·k - T t <= T - b
        row_up = np.zeros(dim)
        row_up[:n] = d
        row_up[n] = -T
        rows.append(row_up)
        lbs.append(-np.inf)
        ubs.append(T - b_e)
        # b + d·k >= T(1 - t)  ->  -d·k - T t <= b - T  (upper-bound row)
        row_lo = np.zeros(dim)
        row_lo[:n] = -d
        row_lo[n] = -T
        rows.append(row_lo)
        lbs.append(-np.inf)
        ubs.append(b_e - T)

    return np.vstack(rows), lbs, ubs


def _minimax_milp(
    targets: dict[str, float],
    delivered_coeffs,
    pump: np.ndarray,
    kmax: np.ndarray,
    s_max: float,
):
    """Minimize worst-case relative deviation t over integer pump counts."""
    n = len(pump)
    dim = n + 1
    A, lbs, ubs = _grid_constraints(targets, delivered_coeffs, pump, s_max)
    bounds = Bounds(
        lb=np.zeros(dim),
        ub=np.concatenate([kmax.astype(float), [np.inf]]),
    )
    integrality = np.concatenate(
        [np.ones(n, dtype=float), np.zeros(1)]
    )
    c_obj = np.zeros(dim)
    c_obj[n] = 1.0
    return milp(
        c=c_obj,
        integrality=integrality,
        bounds=bounds,
        constraints=LinearConstraint(A, np.array(lbs), np.array(ubs)),
        options={"mip_rel_gap": 0.0},
    )


def _extreme_element(
    elem_d: np.ndarray,
    b_e: float,
    maximize: bool,
    pump: np.ndarray,
    kmax: np.ndarray,
    s_max: float,
    extra_rows: Optional[list[tuple[np.ndarray, float]]] = None,
):
    """Min/max deliverable amount of one element over the integer grid.

    ``delivered = b_e + elem_d·k``.  ``extra_rows`` adds ``row·k <= rhs``
    restrictions (other elements held to their lower band).  Returns
    (amount, k) or None when infeasible.
    """
    n = len(pump)
    rows = [pump]
    ubs = [s_max]
    for row, rhs in extra_rows or []:
        rows.append(row)
        ubs.append(rhs)
    A = np.vstack(rows)
    constraints = LinearConstraint(A, -np.inf, np.array(ubs))
    c_obj = -elem_d if maximize else elem_d
    res = milp(
        c=c_obj,
        integrality=np.ones(n),
        bounds=Bounds(np.zeros(n), kmax.astype(float)),
        constraints=constraints,
        options={"mip_rel_gap": 0.0},
    )
    if not res.success or res.x is None:
        return None
    k = _clean_counts(res.x, kmax)
    return float(b_e + elem_d @ k), k


# --------------------------------------------------------------------------- #
# Diagnosis
# --------------------------------------------------------------------------- #
def _hard_volume_checks(req: SolveRequest, V: float) -> list[dict]:
    """Same hard volume predicates as the continuous solver."""
    violations: list[dict] = []
    if req.min_water_ml > V + _EPS:
        violations.append(
            _violation(
                "WATER_MIN_EXCEEDS_VOLUME",
                f"稀释水下限 {req.min_water_ml:g} mL 超过目标总体积 {V:g} mL。",
                element=None,
                limit=V,
                actual=float(req.min_water_ml),
            )
        )

    max_water_by_fraction = V * (1.0 - req.max_concentrate_fraction)
    if req.min_water_ml > max_water_by_fraction + _EPS:
        violations.append(
            _violation(
                "CONCENTRATE_FRACTION_CONFLICT",
                (
                    f"浓缩液最大占比 {req.max_concentrate_fraction:.0%} 时"
                    f"最多只能加水 {max_water_by_fraction:g} mL，"
                    f"低于稀释水下限 {req.min_water_ml:g} mL。"
                ),
                limit=max_water_by_fraction,
                actual=float(req.min_water_ml),
            )
        )
    return violations


def _diagnose(
    *,
    targets: dict[str, float],
    c_mat: dict[str, np.ndarray],
    delivered_coeffs,
    pump: np.ndarray,
    kmax: np.ndarray,
    s_max: float,
    k_star: np.ndarray,
    tol: float,
    names: list[str],
) -> list[dict]:
    n = len(pump)
    violations: list[dict] = []
    explained: set[str] = set()

    for e, T in targets.items():
        if T <= _EPS:
            continue
        b_e, d = delivered_coeffs(e)
        delivered = float(b_e + d @ k_star)
        lower_band = T * (1.0 - tol)
        upper_band = T * (1.0 + tol)

        if lower_band - _EPS <= delivered <= upper_band + _EPS:
            continue

        if delivered < lower_band - _EPS:
            # --- shortage: even every bottle pumped to its cap ----------- #
            extreme = _extreme_element(
                d, b_e, True, pump, kmax, s_max
            )
            if extreme is None:
                continue
            max_amount, k_max = extreme
            if max_amount < lower_band - _EPS:
                binding = [
                    names[i]
                    for i in range(n)
                    if k_max[i] >= kmax[i] - _BIND_EPS and c_mat[e][i] > _EPS
                ]
                reason = "受浓缩液可用体积/单液使用量上限（按整泵次计）约束"
                if binding:
                    reason += f"，已泵送到上限的浓缩液：{ '、'.join(binding) }"
                violations.append(
                    _violation(
                        "ELEMENT_SHORTAGE",
                        (
                            f"按整数泵送步长，元素 {e} 最多只能配出 "
                            f"{max_amount:.3f} mg，低于目标下限 "
                            f"{lower_band:.3f} mg（目标 {T:.3f} mg，"
                            f"容差 {tol:.0%}）；{reason}。"
                        ),
                        element=e,
                        limit=round(lower_band, 6),
                        actual=round(max_amount, 6),
                    )
                )
                explained.add(e)

        elif delivered > upper_band + _EPS:
            # --- surplus: hold every OTHER target at its lower band, then
            # minimize this element over the integer grid ----------------- #
            extra: list[tuple[np.ndarray, float]] = []
            for other, To in targets.items():
                if other == e or To <= _EPS:
                    continue
                b_o, d_o = delivered_coeffs(other)
                # b_o + d_o·k >= To(1-tol)  ->  -d_o·k <= b_o - lower
                extra.append((-d_o, b_o - To * (1.0 - tol)))
            extreme = _extreme_element(
                d, b_e, False, pump, kmax, s_max, extra_rows=extra
            )
            if extreme is not None:
                min_amount, _ = extreme
                if min_amount > upper_band + _EPS:
                    violations.append(
                        _violation(
                            "ELEMENT_SURPLUS",
                            (
                                f"按整数泵送步长，满足其余元素目标下限时，"
                                f"元素 {e} 最少也会达到 {min_amount:.3f} mg，"
                                f"高于目标上限 {upper_band:.3f} mg（目标 "
                                f"{T:.3f} mg，容差 {tol:.0%}）；"
                                f"原水基线/浓缩液成分配比耦合，无法在满足"
                                f"其他元素的同时压低 {e}。"
                            ),
                            element=e,
                            limit=round(upper_band, 6),
                            actual=round(min_amount, 6),
                        )
                    )
                    explained.add(e)

    # Remaining out-of-band elements are integer-grid trade-off victims.
    for e, T in targets.items():
        if T <= _EPS or e in explained:
            continue
        b_e, d = delivered_coeffs(e)
        delivered = float(b_e + d @ k_star)
        lower_band = T * (1.0 - tol)
        upper_band = T * (1.0 + tol)
        if delivered < lower_band - _EPS or delivered > upper_band + _EPS:
            direction = "偏低" if delivered < lower_band else "偏高"
            violations.append(
                _violation(
                    "ELEMENT_DEVIATION",
                    (
                        f"元素 {e} 在最优整数泵送折中方案中{direction}："
                        f"{delivered:.3f} mg，目标区间 "
                        f"[{lower_band:.3f}, {upper_band:.3f}] mg；"
                        f"受各浓缩液整泵次步长与其他元素相互制约，"
                        f"调小泵送步长或更换浓缩液后可进一步收敛。"
                    ),
                    element=e,
                    actual=round(delivered, 6),
                )
            )

    if not violations:
        violations.append(
            _violation(
                "TOLERANCE_UNREACHABLE",
                f"整数泵送最优最大相对偏差仍超过容差 {tol:.2%}。",
                actual=None,
                limit=tol,
            )
        )
    return violations


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #
def _element_report(
    *,
    targets: dict[str, float],
    target_conc: dict[str, float],
    c_mat: dict[str, np.ndarray],
    s_vec: dict[str, float],
    V: float,
    tol: float,
    pump: np.ndarray,
    k: np.ndarray,
) -> tuple[list[dict], Optional[float]]:
    """Per-element amounts / deviations for concrete integer pump counts."""
    S = float(np.sum(pump * k))
    w = V - S
    rows: list[dict] = []
    max_dev: Optional[float] = None

    for e, T_mg in targets.items():
        concentrate_mg = float(c_mat[e] @ (pump * k))
        source_mg = float(s_vec[e] * w)
        actual_mg = concentrate_mg + source_mg
        target_concentration = target_conc[e]
        actual_conc = actual_mg * 1000.0 / V
        if T_mg > _EPS:
            dev = (actual_mg - T_mg) / T_mg
            max_dev = abs(dev) if max_dev is None else max(max_dev, abs(dev))
            if abs(dev) <= tol + _EPS:
                status = "ok"
            else:
                status = "under" if dev < 0 else "over"
            rows.append(
                {
                    "element": e,
                    "target_concentration": _r(target_concentration, 6),
                    "actual_concentration": _r(actual_conc, 6),
                    "target_amount_mg": _r(T_mg, 6),
                    "actual_amount_mg": _r(actual_mg, 6),
                    "concentrate_amount_mg": _r(concentrate_mg, 6),
                    "source_water_amount_mg": _r(source_mg, 6),
                    "relative_deviation": _r(dev, 6),
                    "status": status,
                }
            )
        else:
            rows.append(
                {
                    "element": e,
                    "target_concentration": 0.0,
                    "actual_concentration": _r(actual_conc, 6),
                    "target_amount_mg": 0.0,
                    "actual_amount_mg": _r(actual_mg, 6),
                    "concentrate_amount_mg": _r(concentrate_mg, 6),
                    "source_water_amount_mg": _r(source_mg, 6),
                    "relative_deviation": None,
                    "status": "unconstrained",
                }
            )
    return rows, max_dev
