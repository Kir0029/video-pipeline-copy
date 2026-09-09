"""Тесты Мета-Агента: компилятор промптов и API-эндпоинты."""

import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport

from app.web.api import create_app
from app.services.meta_prompt_compiler import (
    compile_meta_prompt,
    sanitize_compiled_prompt,
    NODE_SPECIFIC_GUIDELINES,
)
from app.services.prompt_library import step_dir


def test_sanitize_compiled_prompt() -> None:
    # 1. Clean markdown fence
    raw = "```markdown\n# Role\nDo task.\n```"
    assert sanitize_compiled_prompt(raw) == "# Role\nDo task."

    # 2. Clean md fence
    raw2 = "```md\n# Prompt\nJSON format\n```"
    assert sanitize_compiled_prompt(raw2) == "# Prompt\nJSON format"

    # 3. Plain text
    raw3 = "System prompt without fences"
    assert sanitize_compiled_prompt(raw3) == "System prompt without fences"


@pytest.mark.asyncio
async def test_compile_meta_prompt_mocked() -> None:
    fake_reply = """# EXCEL GPT MOOD ANALYZER
## Role
Analyze mood of each frame.

## Output Contract (JSON)
```json
[
  { "number": 1, "mood": "tense", "palette": ["#000", "#FFF"] }
]
```
## Constraints
- Strict JSON only. No chat.
"""
    mock_gpt = AsyncMock()
    mock_gpt.ask_fresh.return_value = fake_reply

    with patch("app.services.meta_prompt_compiler.get_gpt_client", return_value=mock_gpt):
        result = await compile_meta_prompt(
            step_code="excel_gpt",
            user_intent="Анализируй настроение каждого кадра и цветовую палитру",
            project_topic="Киберпанк 2050",
            target_name="mood_analyzer",
        )

        assert result["step_code"] == "excel_gpt"
        assert result["name"] == "mood_analyzer"
        assert "EXCEL GPT MOOD ANALYZER" in result["compiled_prompt"]
        assert result["stats"]["has_schema"] is True
        assert result["stats"]["has_guards"] is True


@pytest.mark.asyncio
async def test_meta_agent_endpoints() -> None:
    app = create_app()
    transport = ASGITransport(app=app)

    fake_reply = "# TEST HERO STYLE\n[character_description]\nStrict 8k render."
    mock_gpt = AsyncMock()
    mock_gpt.ask_fresh.return_value = fake_reply

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Test compile endpoint
        with patch("app.services.meta_prompt_compiler.get_gpt_client", return_value=mock_gpt):
            resp = await client.post(
                "/api/meta-agent/compile",
                json={
                    "step_code": "hero_style",
                    "user_intent": "Аниме стиль в духе Макото Синкая",
                    "target_name": "anime_shinkai",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert "TEST HERO STYLE" in data["compiled_prompt"]

        # 2. Test save-and-activate endpoint
        save_resp = await client.post(
            "/api/meta-agent/save-and-activate",
            json={
                "step_code": "hero_style",
                "name": "test_meta_preset",
                "content": "# Test content\n8k anime style",
                "activate": False,
            },
        )
        assert save_resp.status_code == 200
        save_data = save_resp.json()
        assert save_data["ok"] is True
        assert save_data["name"] == "test_meta_preset"
        assert save_data["file_name"] == "test_meta_preset.md"

        # Cleanup test file
        folder = step_dir("hero_style")
        test_file = folder / "test_meta_preset.md"
        if test_file.exists():
            test_file.unlink()


@pytest.mark.asyncio
async def test_assist_project_endpoint() -> None:
    app = create_app()
    transport = ASGITransport(app=app)

    fake_json_reply = """```json
{
  "title": "Warhammer 40K: Бастион Кровавых Ангелов",
  "topic": "Отряд Кровавых Ангелов отбивается от ксеноморфов в древних руинах.\\n\\nОзвучка и цитаты: Брутальный баритон. В паузах звучит: 'Плоть слаба, но долг вечен'.",
  "suggested_hero_mode": "hero"
}
```"""
    mock_gpt = AsyncMock()
    mock_gpt.ask_fresh.return_value = fake_json_reply

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("app.services.gpt_client.get_gpt_client", return_value=mock_gpt):
            resp = await client.post(
                "/api/meta-agent/assist-project",
                json={
                    "topic_draft": "Космодесантники в красной броне против ксеноморфов",
                    "title_draft": "",
                    "tone": "grimdark",
                    "voiceover_style": "epic_quotes",
                    "mode": "expand",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["title"] == "Warhammer 40K: Бастион Кровавых Ангелов"
            assert "Озвучка и цитаты" in data["topic"]
            assert data["suggested_hero_mode"] == "hero"
            assert data["tone"] == "grimdark"
            assert data["voiceover_style"] == "epic_quotes"

            # Test generate mode preserving existing user title
            resp_gen = await client.post(
                "/api/meta-agent/assist-project",
                json={
                    "topic_draft": "",
                    "title_draft": "Warhammer 40K: Бастион Кровавых Ангелов",
                    "tone": "grimdark",
                    "voiceover_style": "epic_quotes",
                    "mode": "generate",
                },
            )
            assert resp_gen.status_code == 200
            data_gen = resp_gen.json()
            assert data_gen["ok"] is True
            assert data_gen["title"] == "Warhammer 40K: Бастион Кровавых Ангелов"