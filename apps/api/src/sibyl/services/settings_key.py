"""One settings key for every process that reads the stored settings.

Under Redis coordination several API replicas and workers decrypt the same
settings rows. A key file is no proof the key is shared (each replica can have
its own), so the first process to start records a fingerprint of its key in
the store, and every later process refuses to start when its own key's
fingerprint differs. A single process keeps its key to itself and checks
nothing.
"""

from __future__ import annotations

from sibyl.config import settings
from sibyl.crypto import SettingsKeyError, settings_key_fingerprint
from sibyl.persistence.settings_runtime import (
    INTERNAL_SETTING_PREFIX,
    claim_system_setting,
    get_settings_session,
)
from sibyl.persistence.settings_types import SystemSettingRecord

SETTINGS_KEY_FINGERPRINT = f"{INTERNAL_SETTING_PREFIX}settings_key_fingerprint"


def _key_mismatch() -> SettingsKeyError:
    return SettingsKeyError(
        "This process's settings key (SIBYL_SETTINGS_KEY, or its key file) is not "
        "the key the other API replicas and workers recorded, so secrets they saved "
        "would read back as empty here. Give every replica and worker the same "
        "SIBYL_SETTINGS_KEY and restart. If the key was changed on purpose, delete "
        f"the system setting {SETTINGS_KEY_FINGERPRINT} (DELETE /api/settings/"
        f"{SETTINGS_KEY_FINGERPRINT} as an admin), restart every process with the new "
        "key, and enter the encrypted settings again."
    )


async def verify_shared_settings_key() -> None:
    """Refuse a settings key the other processes on this store do not share.

    Raises:
        SettingsKeyError: Under Redis coordination, when the stored fingerprint
            belongs to a different key.
    """
    if settings.resolved_coordination_backend != "redis":
        return
    fingerprint = settings_key_fingerprint()
    async with get_settings_session() as session:
        stored = await claim_system_setting(
            session,
            setting=SystemSettingRecord(
                key=SETTINGS_KEY_FINGERPRINT,
                value=fingerprint,
                description="Fingerprint of the settings encryption key every process shares",
            ),
        )
    if stored.value != fingerprint:
        raise _key_mismatch()


__all__ = ["SETTINGS_KEY_FINGERPRINT", "verify_shared_settings_key"]
