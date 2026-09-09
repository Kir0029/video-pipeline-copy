"""Parity with outsee.io/image UI (gpt-image-2 etc.) — slugs, res map, errors."""

from __future__ import annotations

import pytest

from app.bots.outsee import (
    OutseePromptTooLongError,
    _OUTSEE_LENGTH_MARKERS,
    _OUTSEE_MODERATION_MARKERS,
    _outsee_failure_kind,
    _quality_selectors,
    _raise_outsee_failure,
)
from app.generation_options import (
    IMAGE_GENERATORS_BY_ID,
    IMAGE_RESOLUTIONS_BY_GENERATOR,
    allowed_image_resolution_ids,
    clamp_image_resolution_id,
)


def test_outsee_image_model_slugs_match_live_ui() -> None:
    """Slug'и из outsee JS (июль 2026), не «интуитивные» с дефисами."""
    expected = {
        "gpt_image_2_vip": "gpt-image-2-vip",
        "nano_banana_2": "nano-banana-2",
        "nano_banana_2_lite": "nano-banana-2-lite",
        "seedream_5_pro": "seedream-5-pro",
        "flux_2_pro": "flux-2-pro",
        "z_image": "z-image",
        "qwen3_image": "qwen3-image",
    }
    for gid, slug in expected.items():
        assert IMAGE_GENERATORS_BY_ID[gid].outsee_slug == slug
    assert IMAGE_GENERATORS_BY_ID["gpt_image_2"].outsee_slug == "gpt-image-2-vip"


def test_gpt_image_2_resolutions() -> None:
    assert allowed_image_resolution_ids("gpt_image_2") == ("1k",)
    assert allowed_image_resolution_ids("gpt_image_2_vip") == ("1k", "2k", "4k")
    assert clamp_image_resolution_id("gpt_image_1_5", "4k") == "2k"
    assert clamp_image_resolution_id("seedream_5_0_lite", "4k") == "2k"
    assert "2k" in IMAGE_RESOLUTIONS_BY_GENERATOR["seedream_5_0_lite"]


def test_prompt_too_long_banner_is_length() -> None:
    assert "промпт слишком длинный" in _OUTSEE_LENGTH_MARKERS
    assert _outsee_failure_kind("Промпт слишком длинный") == "length"
    with pytest.raises(OutseePromptTooLongError):
        _raise_outsee_failure(
            text="Промпт слишком длинный",
            gen_id="abc",
            elapsed=2.0,
            in_result=False,
            prompt_len=5000,
        )


def test_outsee_moderation_copy_from_ui() -> None:
    text = (
        "Ваш текстовый запрос содержит запрещённые слова и не прошёл модерацию. "
        "Попробуйте переформулировать описание"
    )
    assert _outsee_failure_kind(text) == "moderation"
    assert any("запрещ" in m for m in _OUTSEE_MODERATION_MARKERS)


def test_quality_selectors_include_data_value() -> None:
    sels = _quality_selectors("Среднее")
    assert any("data-value='medium'" in s for s in sels)
    assert any("Среднее" in s for s in sels)
