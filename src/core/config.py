"""Configuration loader for the autonomous agent."""

import json
import os
import yaml
from pathlib import Path
from dotenv import load_dotenv
from .types import AgentConfig

# Safe settings that can be edited from the dashboard (no secrets)
SAFE_SETTINGS = {
    "bot_name", "owner_name", "log_level",
    "default_model", "subagent_model", "chat_model", "intent_model",
    "max_iterations", "timeout_seconds", "auto_commit",
    "self_build_mode", "dashboard_port", "user_timezone",
}

SETTINGS_FILE = Path("data/settings.json")


def load_settings() -> dict:
    """Load safe settings from data/settings.json."""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_settings(settings: dict) -> None:
    """Save safe settings to data/settings.json (only whitelisted keys)."""
    filtered = {k: v for k, v in settings.items() if k in SAFE_SETTINGS}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(filtered, f, indent=2)


def _get(key: str, settings: dict, fallback):
    """Get config value: env var > settings.json > fallback."""
    env_val = os.getenv(key.upper())
    if env_val is not None:
        return env_val
    settings_key = key.lower()
    if settings_key in settings:
        return settings[settings_key]
    return fallback


def load_config(env_file: str = ".env", config_file: str = "config/agent.yaml") -> AgentConfig:
    """Load configuration from environment, settings.json, and yaml files.

    Priority: .env > data/settings.json > config/agent.yaml > defaults

    Args:
        env_file: Path to .env file
        config_file: Path to agent.yaml config file

    Returns:
        AgentConfig instance with all settings
    """
    # Load environment variables
    load_dotenv(env_file)

    # Load safe settings from dashboard-editable file
    settings = load_settings()

    # Load YAML config if exists
    yaml_config = {}
    if Path(config_file).exists():
        with open(config_file, 'r') as f:
            yaml_config = yaml.safe_load(f) or {}

    # Build config from environment (takes precedence) and YAML
    models_config = yaml_config.get("agent", {}).get("models", {})
    local_model_config = yaml_config.get("local_model", {})

    gemini_api_key = os.getenv("GEMINI_API_KEY", "")
    grok_api_key = os.getenv("GROK_API_KEY", "")

    config = AgentConfig(
        # API - Multi-tier model configuration
        api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        default_model=_get("DEFAULT_MODEL", settings, models_config.get("default", "gemini/gemini-2.0-flash")),
        subagent_model=_get("SUBAGENT_MODEL", settings, models_config.get("subagent", "gemini/gemini-2.0-flash")),
        chat_model=_get("CHAT_MODEL", settings, models_config.get("chat", "gemini/gemini-2.0-flash")),
        intent_model=_get("INTENT_MODEL", settings, models_config.get("intent", "gemini/gemini-2.0-flash")),

        # Gemini (optional — intent + simple chat via LiteLLM)
        gemini_model=os.getenv("GEMINI_MODEL", yaml_config.get("agent", {}).get("models", {}).get("gemini_flash", "gemini/gemini-2.0-flash")),
        gemini_enabled=bool(gemini_api_key),

        # Grok (optional, via LiteLLM — fallback)
        grok_enabled=bool(grok_api_key),
        grok_models=yaml_config.get("agent", {}).get("models", {}).get("grok", {}) or {
            "flash": os.getenv("GROK_FLASH_MODEL", "xai/grok-beta"),
            "haiku": os.getenv("GROK_HAIKU_MODEL", "xai/grok-1"),
            "sonnet": os.getenv("GROK_SONNET_MODEL", "xai/grok-advanced"),
            "quality": os.getenv("GROK_QUALITY_MODEL", "xai/grok-4.1")
        },

        # Local Models (optional)
        local_model_enabled=os.getenv("LOCAL_MODEL_ENABLED", str(local_model_config.get("enabled", False))).lower() == "true",
        local_model_name=os.getenv("LOCAL_MODEL_NAME", local_model_config.get("name", "HuggingFaceTB/SmolLM2-1.7B-Instruct")),
        local_model_endpoint=os.getenv("LOCAL_MODEL_ENDPOINT", local_model_config.get("endpoint")),
        local_model_for=os.getenv("LOCAL_MODEL_FOR", local_model_config.get("use_for", "trivial,simple")),

        # Specialized local coder model
        local_coder_enabled=os.getenv("LOCAL_CODER_ENABLED", str(local_model_config.get("coder", {}).get("enabled", False))).lower() == "true",
        local_coder_name=os.getenv("LOCAL_CODER_NAME", local_model_config.get("coder", {}).get("name", "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B")),
        local_coder_endpoint=os.getenv("LOCAL_CODER_ENDPOINT", local_model_config.get("coder", {}).get("endpoint")),

        # Execution
        max_iterations=int(_get("MAX_ITERATIONS", settings, yaml_config.get("agent", {}).get("execution", {}).get("max_iterations", 50))),
        timeout_seconds=int(_get("TIMEOUT_SECONDS", settings, yaml_config.get("agent", {}).get("execution", {}).get("timeout_seconds", 300))),
        retry_attempts=int(os.getenv("RETRY_ATTEMPTS", yaml_config.get("agent", {}).get("execution", {}).get("retry_attempts", 3))),
        self_build_mode=str(_get("SELF_BUILD_MODE", settings, "false")).lower() == "true",

        # Brain
        vector_db_path=os.getenv("VECTOR_DB_PATH", "./data/lancedb"),
        core_brain_path=os.getenv("CORE_BRAIN_PATH", "./data/core_brain"),
        digital_clone_brain_path=os.getenv("DIGITAL_CLONE_BRAIN_PATH", "./data/digital_clone_brain"),
        memory_path=os.getenv("MEMORY_PATH", "./data/memory"),

        # Git
        auto_commit=str(_get("AUTO_COMMIT", settings, "true")).lower() == "true",
        git_user_name=os.getenv("GIT_USER_NAME", ""),
        git_user_email=os.getenv("GIT_USER_EMAIL", ""),

        # Monitoring
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        
        # Twilio Voice & WhatsApp
        twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
        twilio_phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
        twilio_whatsapp_number=os.getenv("TWILIO_WHATSAPP_NUMBER") or 
            (f"whatsapp:{os.getenv('TWILIO_PHONE_NUMBER')}" if os.getenv("TWILIO_PHONE_NUMBER") and not os.getenv("TWILIO_PHONE_NUMBER").startswith("whatsapp:") else os.getenv("TWILIO_PHONE_NUMBER")),
        
        # WhatsApp (Meta Cloud API - Deprecated but kept for fallback/legacy)
        whatsapp_api_token=os.getenv("WHATSAPP_API_TOKEN"),
        whatsapp_phone_id=os.getenv("WHATSAPP_PHONE_ID"), 
        whatsapp_verify_token=os.getenv("WHATSAPP_VERIFY_TOKEN"),
        whatsapp_allowed_numbers=os.getenv("WHATSAPP_ALLOWED_NUMBERS", "").split(",") if os.getenv("WHATSAPP_ALLOWED_NUMBERS") else [],

        # iOS Shortcut / direct API
        nova_api_key=os.getenv("NOVA_API_KEY"),

        # Identity
        bot_name=_get("BOT_NAME", settings, "Nova"),
        owner_name=_get("OWNER_NAME", settings, "User"),

        dashboard_enabled=yaml_config.get("monitoring", {}).get("dashboard", {}).get("enabled", True),
        dashboard_host=os.getenv("DASHBOARD_HOST", yaml_config.get("monitoring", {}).get("dashboard", {}).get("host", "0.0.0.0")),
        dashboard_port=int(_get("DASHBOARD_PORT", settings, yaml_config.get("monitoring", {}).get("dashboard", {}).get("port", 18789))),

        # Logging
        log_level=_get("LOG_LEVEL", settings, "INFO"),
        log_file=os.getenv("LOG_FILE", "./data/logs/agent.log"),
    )

    # Validate required fields
    # Validate required fields
    if not config.api_key and not config.gemini_enabled and not config.grok_enabled:
        raise ValueError("At least one API key (ANTHROPIC_API_KEY or GEMINI_API_KEY or GROK_API_KEY) is required")

    return config
