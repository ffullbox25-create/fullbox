from __future__ import annotations

from dataclasses import dataclass

from fbs.models import FbsIntegrationProfile, FbsWavePolicy


MIN_WAVE_SIZE = 1
MAX_WAVE_SIZE = 150
DEFAULT_MAX_ORDERS_PER_WAVE = 50
DEFAULT_MAX_UNITS_PER_WAVE = 50

# Preserve the larger automatic waves that were already enabled for this client
# before cabinet-level policies existed.
LEGACY_AGENCY_WAVE_LIMITS = {
    2951: (100, 100),
}

# ООО «ПОЗИТИВ»: every FBS write path uses one fixed 150-order/150-unit wave
# limit, while the defaults and client-specific limits of other agencies stay
# unchanged.
FORCED_AGENCY_WAVE_LIMITS = {
    33: (150, 150),
}


@dataclass(frozen=True)
class FbsWaveLimits:
    max_orders: int
    max_units: int | None
    configured: bool = False


def _profile_policy(profile: FbsIntegrationProfile):
    try:
        return profile.wave_policy
    except FbsWavePolicy.DoesNotExist:
        return None


def default_wave_limits(profile: FbsIntegrationProfile) -> FbsWaveLimits:
    max_orders, max_units = FORCED_AGENCY_WAVE_LIMITS.get(
        int(profile.agency_id),
        LEGACY_AGENCY_WAVE_LIMITS.get(
            int(profile.agency_id),
            (DEFAULT_MAX_ORDERS_PER_WAVE, DEFAULT_MAX_UNITS_PER_WAVE),
        ),
    )
    return FbsWaveLimits(max_orders=max_orders, max_units=max_units)


def _forced_wave_limits(profile: FbsIntegrationProfile) -> FbsWaveLimits | None:
    limits = FORCED_AGENCY_WAVE_LIMITS.get(int(profile.agency_id))
    if limits is None:
        return None
    max_orders, max_units = limits
    return FbsWaveLimits(max_orders=max_orders, max_units=max_units)


def configured_or_default_wave_limits(
    profile: FbsIntegrationProfile,
) -> FbsWaveLimits:
    forced = _forced_wave_limits(profile)
    if forced is not None:
        return forced
    policy = _profile_policy(profile)
    if policy is None:
        return default_wave_limits(profile)
    return FbsWaveLimits(
        max_orders=int(policy.max_orders_per_wave),
        max_units=int(policy.max_units_per_wave),
        configured=True,
    )


def enforce_profile_wave_limits(
    profile: FbsIntegrationProfile,
    *,
    requested_max_orders: int,
    requested_max_units: int | None,
) -> FbsWaveLimits:
    """Apply a configured or forced cabinet maximum to every wave write path."""
    forced = _forced_wave_limits(profile)
    if forced is not None:
        return forced
    policy = _profile_policy(profile)
    if policy is None:
        return FbsWaveLimits(
            max_orders=int(requested_max_orders),
            max_units=(
                None
                if requested_max_units is None
                else int(requested_max_units)
            ),
        )
    configured_orders = int(policy.max_orders_per_wave)
    configured_units = int(policy.max_units_per_wave)
    return FbsWaveLimits(
        max_orders=min(int(requested_max_orders), configured_orders),
        max_units=(
            configured_units
            if requested_max_units is None
            else min(int(requested_max_units), configured_units)
        ),
        configured=True,
    )
