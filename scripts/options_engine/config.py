"""Frozen defaults and validation for the option-seller MVP."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class OptionsSellerConfig:
    boll_window: int = 20
    boll_deviations: float = 2.0
    boll_touch_lookback: int = 3
    boll_flat_slope_pct: float = 0.25
    dte_min: int = 30
    dte_max: int = 45
    short_delta_min: float = 0.15
    short_delta_max: float = 0.25
    cautious_short_delta_max: float = 0.20
    min_open_interest: int = 100
    suitable_min_open_interest: int = 500
    max_quote_spread_pct: float = 20.0
    suitable_max_quote_spread_pct: float = 12.0
    max_quote_age_minutes: int = 15
    min_credit_width_pct: float = 20.0
    min_width: float = 1.0
    max_width: float = 10.0
    dynamic_width_atr_min: float = 0.25
    dynamic_width_atr_max: float = 1.50
    min_expected_move_coverage: float = 0.90
    cautious_min_expected_move_coverage: float = 1.00
    iv_history_min_observations: int = 60
    macro_event_blackout_days: int = 1
    max_short_distance_pct: float = 25.0
    min_underlying_price: float = 5.0
    min_underlying_volume: float = 50_000.0
    min_underlying_dollar_volume: float = 5_000_000.0
    max_risk_pct: float = 0.5
    max_symbol_option_risk_pct: float = 2.0
    max_correlated_option_risk_pct: float = 4.0
    max_expiration_option_risk_pct: float = 2.0
    max_total_option_risk_pct: float = 6.0
    correlation_threshold: float = 0.70
    round_trip_cost_per_spread: float | None = None
    suitable_score: int = 75
    watch_score: int = 60
    max_candidates_per_symbol: int = 10
    calendar_front_dte_min: int = 14
    calendar_front_dte_max: int = 35
    calendar_back_dte_min: int = 45
    calendar_back_dte_max: int = 90
    calendar_min_expiry_gap_days: int = 21
    calendar_max_expiry_gap_days: int = 75
    calendar_max_abs_net_delta: float = 0.10
    calendar_max_moneyness_pct: float = 5.0
    calendar_max_middle_slope_pct: float = 1.0
    calendar_max_pct_b_deviation: float = 0.35
    calendar_min_front_back_iv_edge: float = -0.02
    calendar_min_net_vega: float = 0.0
    iron_condor_dte_min: int = 21
    iron_condor_dte_max: int = 45
    iron_condor_short_delta_min: float = 0.10
    iron_condor_short_delta_max: float = 0.25
    iron_condor_max_abs_net_delta: float = 0.10
    iron_condor_min_credit_width_pct: float = 20.0
    iron_condor_min_expected_move_coverage: float = 0.70
    iron_condor_max_middle_slope_pct: float = 0.75
    iron_condor_max_pct_b_deviation: float = 0.35
    iron_butterfly_dte_min: int = 21
    iron_butterfly_dte_max: int = 45
    iron_butterfly_short_delta_min: float = 0.35
    iron_butterfly_short_delta_max: float = 0.65
    iron_butterfly_max_abs_net_delta: float = 0.10
    iron_butterfly_max_body_moneyness_pct: float = 3.0
    iron_butterfly_min_credit_width_pct: float = 50.0
    iron_butterfly_min_break_even_coverage: float = 0.20
    iron_butterfly_max_middle_slope_pct: float = 0.50
    iron_butterfly_max_pct_b_deviation: float = 0.25

    def validate(self) -> "OptionsSellerConfig":
        if self.boll_window < 10:
            raise ValueError("boll_window must be >= 10")
        if self.boll_deviations <= 0:
            raise ValueError("boll_deviations must be > 0")
        if self.boll_touch_lookback < 1:
            raise ValueError("boll_touch_lookback must be >= 1")
        if self.boll_flat_slope_pct < 0:
            raise ValueError("boll_flat_slope_pct must be >= 0")
        if not 0 <= self.dte_min <= self.dte_max:
            raise ValueError("DTE range is invalid")
        if not 0 < self.short_delta_min <= self.short_delta_max < 1:
            raise ValueError("short delta range is invalid")
        if not self.short_delta_min <= self.cautious_short_delta_max <= self.short_delta_max:
            raise ValueError("cautious short delta maximum is invalid")
        if self.min_open_interest < 0:
            raise ValueError("min_open_interest must be >= 0")
        if self.suitable_min_open_interest < self.min_open_interest:
            raise ValueError("suitable_min_open_interest must be >= min_open_interest")
        if self.max_quote_spread_pct <= 0:
            raise ValueError("max_quote_spread_pct must be > 0")
        if not 0 < self.suitable_max_quote_spread_pct <= self.max_quote_spread_pct:
            raise ValueError("suitable quote spread maximum is invalid")
        if self.max_quote_age_minutes < 1:
            raise ValueError("max_quote_age_minutes must be >= 1")
        if not 0 < self.min_credit_width_pct < 100:
            raise ValueError("min_credit_width_pct must be between 0 and 100")
        if not 0 < self.min_width <= self.max_width:
            raise ValueError("spread width range is invalid")
        if not 0 < self.dynamic_width_atr_min <= self.dynamic_width_atr_max:
            raise ValueError("dynamic ATR width range is invalid")
        if self.min_expected_move_coverage <= 0:
            raise ValueError("min_expected_move_coverage must be > 0")
        if self.cautious_min_expected_move_coverage < self.min_expected_move_coverage:
            raise ValueError("cautious expected-move coverage must be >= normal coverage")
        if self.iv_history_min_observations < 5:
            raise ValueError("iv_history_min_observations must be >= 5")
        if self.macro_event_blackout_days < 0:
            raise ValueError("macro_event_blackout_days must be >= 0")
        if self.max_short_distance_pct <= 0:
            raise ValueError("max_short_distance_pct must be > 0")
        if self.min_underlying_price <= 0:
            raise ValueError("min_underlying_price must be > 0")
        if self.min_underlying_volume < 0 or self.min_underlying_dollar_volume < 0:
            raise ValueError("underlying liquidity thresholds must be >= 0")
        if not 0 < self.max_risk_pct <= 100:
            raise ValueError("max_risk_pct must be between 0 and 100")
        if not 0 < self.max_symbol_option_risk_pct <= self.max_correlated_option_risk_pct:
            raise ValueError("symbol/correlated option risk limits are invalid")
        if not 0 < self.max_expiration_option_risk_pct <= self.max_total_option_risk_pct:
            raise ValueError("expiration/total option risk limits are invalid")
        if not self.max_correlated_option_risk_pct <= self.max_total_option_risk_pct <= 100:
            raise ValueError("correlated/total option risk limits are invalid")
        if not 0 < self.correlation_threshold < 1:
            raise ValueError("correlation_threshold must be between 0 and 1")
        if self.round_trip_cost_per_spread is not None and self.round_trip_cost_per_spread < 0:
            raise ValueError("round_trip_cost_per_spread must be >= 0")
        if not 0 <= self.watch_score <= self.suitable_score <= 100:
            raise ValueError("score thresholds are invalid")
        if self.max_candidates_per_symbol < 1:
            raise ValueError("max_candidates_per_symbol must be >= 1")
        if not 0 <= self.calendar_front_dte_min <= self.calendar_front_dte_max:
            raise ValueError("calendar front DTE range is invalid")
        if not 0 <= self.calendar_back_dte_min <= self.calendar_back_dte_max:
            raise ValueError("calendar back DTE range is invalid")
        if self.calendar_front_dte_max >= self.calendar_back_dte_max:
            raise ValueError("calendar back expiration must extend beyond front expiration")
        if not 1 <= self.calendar_min_expiry_gap_days <= self.calendar_max_expiry_gap_days:
            raise ValueError("calendar expiration gap range is invalid")
        if not 0 < self.calendar_max_abs_net_delta < 1:
            raise ValueError("calendar_max_abs_net_delta must be between 0 and 1")
        if self.calendar_max_moneyness_pct <= 0:
            raise ValueError("calendar_max_moneyness_pct must be > 0")
        if self.calendar_max_middle_slope_pct < 0:
            raise ValueError("calendar_max_middle_slope_pct must be >= 0")
        if not 0 < self.calendar_max_pct_b_deviation <= 0.5:
            raise ValueError("calendar_max_pct_b_deviation must be between 0 and 0.5")
        if not -1 < self.calendar_min_front_back_iv_edge < 1:
            raise ValueError("calendar_min_front_back_iv_edge must be a decimal IV difference")
        if self.calendar_min_net_vega < 0:
            raise ValueError("calendar_min_net_vega must be >= 0")
        if not 0 <= self.iron_condor_dte_min <= self.iron_condor_dte_max:
            raise ValueError("iron condor DTE range is invalid")
        if not 0 < self.iron_condor_short_delta_min <= self.iron_condor_short_delta_max < 1:
            raise ValueError("iron condor short delta range is invalid")
        if not 0 < self.iron_condor_max_abs_net_delta < 1:
            raise ValueError("iron_condor_max_abs_net_delta must be between 0 and 1")
        if not 0 < self.iron_condor_min_credit_width_pct < 100:
            raise ValueError("iron_condor_min_credit_width_pct must be between 0 and 100")
        if self.iron_condor_min_expected_move_coverage <= 0:
            raise ValueError("iron_condor_min_expected_move_coverage must be > 0")
        if self.iron_condor_max_middle_slope_pct < 0:
            raise ValueError("iron_condor_max_middle_slope_pct must be >= 0")
        if not 0 < self.iron_condor_max_pct_b_deviation <= 0.5:
            raise ValueError("iron_condor_max_pct_b_deviation must be between 0 and 0.5")
        if not 0 <= self.iron_butterfly_dte_min <= self.iron_butterfly_dte_max:
            raise ValueError("iron butterfly DTE range is invalid")
        if not 0 < self.iron_butterfly_short_delta_min <= self.iron_butterfly_short_delta_max < 1:
            raise ValueError("iron butterfly short delta range is invalid")
        if not 0 < self.iron_butterfly_max_abs_net_delta < 1:
            raise ValueError("iron_butterfly_max_abs_net_delta must be between 0 and 1")
        if self.iron_butterfly_max_body_moneyness_pct <= 0:
            raise ValueError("iron_butterfly_max_body_moneyness_pct must be > 0")
        if not 0 < self.iron_butterfly_min_credit_width_pct < 100:
            raise ValueError("iron_butterfly_min_credit_width_pct must be between 0 and 100")
        if self.iron_butterfly_min_break_even_coverage <= 0:
            raise ValueError("iron_butterfly_min_break_even_coverage must be > 0")
        if self.iron_butterfly_max_middle_slope_pct < 0:
            raise ValueError("iron_butterfly_max_middle_slope_pct must be >= 0")
        if not 0 < self.iron_butterfly_max_pct_b_deviation <= 0.5:
            raise ValueError("iron_butterfly_max_pct_b_deviation must be between 0 and 0.5")
        return self

    def to_dict(self) -> dict:
        return asdict(self)
