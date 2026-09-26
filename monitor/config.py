"""Configuration for the monitoring poller and dashboard.

Follows the same pydantic-settings pattern as oracle_core.config and
agent_app.config. All values have defaults, so no new env vars are
strictly required — override via environment when needed.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class MonitorSettings(BaseSettings):
    """Monitor settings loaded from environment variables."""

    # Poller behavior
    monitor_poll_interval_seconds: int = 60
    monitor_long_running_query_seconds: int = 60
    monitor_tablespace_warn_pct: float = 85.0
    monitor_tablespace_crit_pct: float = 95.0
    monitor_top_n_wait_events: int = 5

    # Write-workload rate thresholds. Rates are calculated from V$SYSSTAT
    # deltas between successful poll cycles.
    monitor_write_warn_commits_per_sec: float = 40.0
    monitor_write_crit_commits_per_sec: float = 150.0
    monitor_write_warn_redo_mb_per_sec: float = 5.0
    monitor_write_crit_redo_mb_per_sec: float = 25.0
    monitor_write_warn_executes_per_sec: float = 2000.0
    monitor_write_crit_executes_per_sec: float = 10000.0

    # Where issue/acknowledgment state lives (separate from Oracle)
    monitor_sqlite_path: str = "./monitor.db"

    # Optional: use a different (e.g. smaller/cheaper) OpenWebUI model for
    # recommendations. Empty string = use the agent's default LLM_MODEL.
    monitor_llm_model: str = ""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_monitor_settings() -> MonitorSettings:
    """Return one cached MonitorSettings instance."""
    return MonitorSettings()
