"""
FruitcakeAI v5 — Application Configuration
Loaded from environment variables / .env file via pydantic-settings.
"""

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Application ──────────────────────────────────────────────────────────
    app_name: str = "FruitcakeAI"
    app_version: str = "0.7.3"
    debug: bool = False
    log_level: str = "INFO"
    app_port: int = 30417

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://fruitcake:fruitcake@localhost:5432/fruitcake_v5"
    # Sync URL is only used by Alembic migrations
    database_url_sync: str = "postgresql+psycopg2://fruitcake:fruitcake@localhost:5432/fruitcake_v5"

    # ── Auth ──────────────────────────────────────────────────────────────────
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiration_hours: int = 24
    jwt_refresh_expiration_days: int = 30
    admin_roles: List[str] = ["admin", "super_admin"]
    default_user_role: str = "parent"

    # ── LLM Backend ───────────────────────────────────────────────────────────
    # Supported: anthropic | openai | ollama | openai_compat
    llm_backend: str = "ollama"
    llm_model: str = "ollama_chat/qwen2.5:32b"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    openai_models: str = ""
    anthropic_models: str = ""
    gemini_models: str = ""
    local_models: str = ""
    secrets_master_key: str = ""
    # For Ollama / llama.cpp (openai_compat)
    local_api_base: str = "http://localhost:11434/v1"
    local_api_key: str = "sk-local"
    local_model: str = "qwen2.5:32b"
    # Task-stage routing (Phase 5.4.x)
    task_small_model: str = "ollama_chat/qwen2.5:14b"
    task_large_model: str = "ollama_chat/qwen2.5:32b"
    task_model_routing_enabled: bool = True
    task_large_retry_enabled: bool = True
    task_large_retry_max_attempts: int = 1
    task_force_large_for_planning: bool = False
    task_force_large_for_final_synthesis: bool = True
    task_plan_default_steps: int = 8
    task_plan_max_steps: int = 20
    # Scheduler guardrails (laptop sleep / transient local LLM outages)
    scheduler_llm_health_gate_enabled: bool = True
    scheduler_unhealthy_cooldown_seconds: int = 300
    scheduler_stale_running_recovery_minutes: int = 20
    document_ingest_poll_seconds: int = 5
    document_ingest_stale_running_recovery_minutes: int = 20
    document_ingest_max_concurrent: int = 1
    # Phase 5.5.1 chat complexity routing
    chat_complexity_routing_enabled: bool = True
    chat_complexity_threshold: int = 3
    # Phase 5.5.2 orchestrated chat path
    chat_orchestration_enabled: bool = True
    chat_orchestration_max_steps: int = 3
    chat_orchestration_kill_switch: bool = False
    # Phase 5.5.3 chat grounding checks
    chat_validation_enabled: bool = True
    chat_validation_retry_enabled: bool = True
    chat_validation_retry_max_attempts: int = 1
    # Sprint 5.6.5 knowledge skills
    skills_preview_allowed_domains: List[str] = [
        "github.com",
        "raw.githubusercontent.com",
        "gist.githubusercontent.com",
    ]
    skills_preview_fetch_timeout_seconds: int = 5
    skills_preview_fetch_max_bytes: int = 100_000
    skills_similarity_threshold: float = 0.35
    skills_max_injected: int = 3
    skills_max_tokens_per_skill: int = 250
    skills_total_max_tokens: int = 500

    # ── Embeddings ───────────────────────────────────────────────────────────
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dimension: int = 384
    embedding_batch_size: int = 32
    embedding_cache_dir: str = "./storage/model_cache/huggingface"

    # ── Storage ───────────────────────────────────────────────────────────────
    storage_dir: str = "./storage"
    workspace_dir: str = "./workspace"
    linked_source_allowed_roots: str = ""
    upload_max_size_mb: int = 50
    filesystem_mcp_max_read_bytes: int = 100_000
    filesystem_mcp_max_write_bytes: int = 100_000
    filesystem_mcp_max_search_results: int = 50

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins: List[str] = ["http://localhost:3000", "http://localhost:5173"]

    # ── Google Calendar (optional) ─────────────────────────────────────────────
    google_calendar_enabled: bool = False
    google_calendar_service_account_file: str = ""
    google_calendar_delegated_user: str = ""   # e.g. "you@gmail.com" for domain-wide delegation
    google_calendar_default_id: str = "primary"

    # ── Apple CalDAV (optional) ────────────────────────────────────────────────
    apple_caldav_enabled: bool = False
    apple_caldav_url: str = ""          # e.g. https://caldav.icloud.com
    apple_caldav_username: str = ""     # Apple ID email
    apple_caldav_app_password: str = "" # App-specific password from appleid.apple.com
    apple_caldav_default_calendar: str = "home"

    # ── APNs Push (Phase 4 Sprint 4.3) ────────────────────────────────────────
    # Leave empty to disable push (tasks still run, results not pushed to device)
    apns_key_id: str = ""           # 10-char Key ID from developer.apple.com
    apns_team_id: str = ""          # 10-char Team ID
    apns_auth_key_path: str = ""    # Absolute path to AuthKey_<key_id>.p8
    apns_bundle_id: str = "none.FruitcakeAi"
    apns_environment: str = "sandbox"  # "sandbox" | "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
