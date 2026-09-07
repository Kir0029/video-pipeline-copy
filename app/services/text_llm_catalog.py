"""Каталог текстовых LLM: GPT (kie) + GPT 5.5/5.6 Sol (vibecode) + Kimi.

Выбор активной модели: data/text_llm_choice.json (Studio UI).
GPT_* / VIBECODE_* в .env не затираются.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from app.settings import Settings, settings

_CHOICE_NAME = "text_llm_choice.json"

CATALOG: list[dict[str, str]] = [
    # OpenAI
    {
        "id": "gpt-5.5-vibecode",
        "provider": "vibecode",
        "group": "OpenAI",
        "label": "GPT 5.5",
        "site": "vibecode.moe",
        "api_model": "gpt-5.6-sol",
    },
    {
        "id": "gpt-5.6-sol-vibecode",
        "provider": "vibecode",
        "group": "OpenAI",
        "label": "GPT 5.6 Sol",
        "site": "vibecode.moe",
        "api_model": "gpt-5.6-sol",
    },
    {
        "id": "gpt-5.6-terra-vibecode",
        "provider": "vibecode",
        "group": "OpenAI",
        "label": "GPT 5.6 Terra",
        "site": "vibecode.moe",
        "api_model": "gpt-5.6-terra",
    },
    {
        "id": "gpt-5.6-luna-vibecode",
        "provider": "vibecode",
        "group": "OpenAI",
        "label": "GPT 5.6 Luna",
        "site": "vibecode.moe",
        "api_model": "gpt-5.6-luna",
    },
    # Google
    {
        "id": "gemini-3.7-flash-vibecode",
        "provider": "vibecode",
        "group": "Google",
        "label": "Gemini 3.7 Flash",
        "site": "vibecode.moe",
        "api_model": "gemini-3.7-flash",
    },
    {
        "id": "gemini-3.1-pro-vibecode",
        "provider": "vibecode",
        "group": "Google",
        "label": "Gemini 3.1 Pro",
        "site": "vibecode.moe",
        "api_model": "gemini-3.1-pro-preview",
    },
    {
        "id": "gemini-3-flash-vibecode",
        "provider": "vibecode",
        "group": "Google",
        "label": "Gemini 3 Flash",
        "site": "vibecode.moe",
        "api_model": "gemini-3-flash-preview",
    },
    # DeepSeek
    {
        "id": "deepseek-v4-pro-vibecode",
        "provider": "vibecode",
        "group": "DeepSeek",
        "label": "DeepSeek V4 Pro",
        "site": "vibecode.moe",
        "api_model": "deepseek-v4-pro",
    },
    {
        "id": "deepseek-v4-flash-vibecode",
        "provider": "vibecode",
        "group": "DeepSeek",
        "label": "DeepSeek V4 Flash",
        "site": "vibecode.moe",
        "api_model": "deepseek-v4-flash",
    },
    # KIE
    {
        "id": "gpt-kie",
        "provider": "kie",
        "group": "KIE",
        "label": "GPT (kie.ai)",
        "site": "kie.ai",
    },
]

_PROVIDERS = frozenset({"kie", "vibecode"})
_MODEL_ALIASES = {
    "gpt-5.5": "gpt-5.6-sol-vibecode",
    "gpt-5-5": "gpt-5.6-sol-vibecode",
    "gpt-5.5-vibecode": "gpt-5.6-sol-vibecode",
    "gpt-5.6-sol": "gpt-5.6-sol-vibecode",
    "gpt-5-6-sol": "gpt-5.6-sol-vibecode",
    "gpt-5.6-sol-vibecode": "gpt-5.6-sol-vibecode",
    "gpt-5.6-terra": "gpt-5.6-terra-vibecode",
    "gpt-5.6-terra-vibecode": "gpt-5.6-terra-vibecode",
    "gpt-5.6-luna": "gpt-5.6-luna-vibecode",
    "gpt-5.6-luna-vibecode": "gpt-5.6-luna-vibecode",
    "gemini-3.7-flash": "gemini-3.7-flash-vibecode",
    "gemini-3.7-flash-vibecode": "gemini-3.7-flash-vibecode",
    "gemini-3.1-pro": "gemini-3.1-pro-vibecode",
    "gemini-3.1-pro-preview": "gemini-3.1-pro-vibecode",
    "gemini-3.1-pro-vibecode": "gemini-3.1-pro-vibecode",
    "gemini-3-flash": "gemini-3-flash-vibecode",
    "gemini-3-flash-preview": "gemini-3-flash-vibecode",
    "gemini-3-flash-vibecode": "gemini-3-flash-vibecode",
    "deepseek-v4-pro": "deepseek-v4-pro-vibecode",
    "deepseek-v4-pro-vibecode": "deepseek-v4-pro-vibecode",
    "deepseek-v4-flash": "deepseek-v4-flash-vibecode",
    "deepseek-v4-flash-vibecode": "deepseek-v4-flash-vibecode",
    "gpt-kie": "gpt-kie",
}


def _choice_path(cfg: Settings) -> Path:
    return Path(cfg.data_dir) / _CHOICE_NAME


def catalog_item(model_id: str | None) -> dict[str, str] | None:
    cid = _MODEL_ALIASES.get((model_id or "").strip())
    if not cid:
        raw = (model_id or "").strip()
        cid = raw if any(it["id"] == raw for it in CATALOG) else ""
    if not cid:
        return None
    return next((it for it in CATALOG if it["id"] == cid), None)


def catalog_api_model(model_id: str | None, *, default: str = "gpt-5.6-sol") -> str:
    item = catalog_item(model_id)
    if item and item.get("api_model"):
        return item["api_model"]
    return default


def read_choice(cfg: Settings | None = None) -> dict[str, Any]:
    s = cfg or settings
    path = _choice_path(s)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("text_llm_choice: read failed: {}", e)
        return {}


def write_choice(
    *,
    provider: str,
    model_id: str | None = None,
    cfg: Settings | None = None,
) -> dict[str, Any]:
    s = cfg or settings
    provider = (provider or "kie").strip().lower()
    if provider in {"kimi", "kimi-k3", "moonshot"}:
        provider = "tokenrouter"
    if provider in {"vibe", "vibecode.moe"}:
        provider = "vibecode"
    aliased = catalog_item(model_id)
    if aliased:
        provider = aliased["provider"]
        model_id = aliased["id"]
    if provider not in _PROVIDERS:
        raise ValueError(f"unknown text LLM provider: {provider!r}")
    if provider == "tokenrouter":
        model_id = model_id or "kimi-k3-tokenrouter"
    elif provider == "vibecode":
        model_id = model_id or "gpt-5.6-sol-vibecode"
    else:
        model_id = model_id or "gpt-kie"
    payload = {"provider": provider, "model_id": model_id}
    path = _choice_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    logger.info(
        "text_llm_choice: active → {} ({})", payload["provider"], payload["model_id"]
    )
    return payload


def resolve_active_provider(cfg: Settings | None = None) -> str:
    """kie по умолчанию; vibecode/tokenrouter — по явному выбору."""
    s = cfg or settings
    raw_choice = str(read_choice(s).get("provider") or "").strip().lower()
    if raw_choice in {"tokenrouter", "kimi", "kimi-k3"}:
        return "tokenrouter"
    if raw_choice in {"vibecode", "vibe"}:
        return "vibecode"
    if raw_choice in {"kie", "gpt", "openai"}:
        return "kie"
    raw = (s.text_llm_provider or "kie").strip().lower()
    if raw in {"vibecode", "vibe"}:
        return "vibecode"
    return "kie"


def resolve_active_model_id(cfg: Settings | None = None) -> str:
    s = cfg or settings
    raw = str(read_choice(s).get("model_id") or "").strip()
    item = catalog_item(raw)
    if item:
        return item["id"]
    prov = resolve_active_provider(s)
    if prov == "vibecode":
        return "gpt-5.6-sol-vibecode"
    return "gpt-kie"


def catalog_status(cfg: Settings | None = None) -> dict[str, Any]:
    s = cfg or settings
    active = resolve_active_provider(s)
    active_id = resolve_active_model_id(s)
    models: list[dict[str, Any]] = []
    for item in CATALOG:
        prov = item["provider"]
        if prov == "tokenrouter":
            model = s.tokenrouter_model
            key_ok = bool((s.tokenrouter_api_key or "").strip())
            base = s.tokenrouter_base_url
        elif prov == "vibecode":
            model = item.get("api_model") or "gpt-5.5"
            key_ok = bool((s.vibecode_api_key or "").strip())
            base = s.vibecode_base_url
        else:
            model = s.gpt_model
            key_ok = bool((s.gpt_api_key or "").strip())
            base = s.gpt_base_url
        models.append(
            {
                **item,
                "model": model,
                "base_url": base,
                "key_configured": key_ok,
                "active": item["id"] == active_id,
            }
        )
    if active == "tokenrouter":
        label = "Kimi K3"
        active_model = s.tokenrouter_model
    elif active == "vibecode":
        item = catalog_item(active_id)
        label = (item or {}).get("label") or "GPT 5.6 Sol"
        active_model = (item or {}).get("api_model") or "gpt-5.6-sol"
    else:
        label = "GPT (kie.ai)"
        active_model = s.gpt_model
    return {
        "active_provider": active,
        "active_label": label,
        "active_model": active_model,
        "models": models,
    }
