import pytest
from unittest.mock import AsyncMock, MagicMock
from app.models import Entity, Project, ProjectStatus
from app.orchestrator.steps.generate_hero import _load_excel_hero_from_xlsx
from app.services.gpt_text_builder import (
    HERO_PLACEHOLDER_BRIEF,
    HERO_PLACEHOLDER_STYLE,
    render_hero_text,
)
from app.monitor.report import render_html_report


@pytest.fixture(autouse=True)
def patch_project_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Project, "data_dir", property(lambda self: tmp_path))


@pytest.mark.asyncio
async def test_load_excel_hero_loads_all_entities_even_with_hero_description(tmp_path):
    """Проверяем, что _load_excel_hero_from_xlsx не сваливается в 1 героя при наличии Entity."""
    project = Project(
        id=991,
        slug="test-proj-4chars",
        status=ProjectStatus.generating_hero,
        hero_mode="auto",
        hero_description="История про ткача в древнем городе",
        hero_count=1,
        meta={},
    )

    ents = [
        Entity(id=1, project_id=991, type="character", code="c01", name="Ткач", attrs={"look": "высокий худой"}, sort_key=10.0),
        Entity(id=2, project_id=991, type="character", code="c02", name="Помощник", attrs={"look": "молодой парень"}, sort_key=20.0),
        Entity(id=3, project_id=991, type="character", code="c03", name="Купец", attrs={"look": "богатый торговец"}, sort_key=30.0),
        Entity(id=4, project_id=991, type="character", code="c04", name="Стражник", attrs={"look": "в доспехах"}, sort_key=40.0),
    ]

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = ents
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result
    mock_session.flush = AsyncMock()

    cfg = await _load_excel_hero_from_xlsx(mock_session, project)
    assert cfg is not None
    assert cfg["source"] == "entity"
    assert len(cfg["characters"]) == 4
    char_ids = [c["id"] for c in cfg["characters"]]
    assert char_ids == ["c01", "c02", "c03", "c04"]
    assert project.meta.get("excel_hero_enabled") is True
    assert project.meta["excel_hero"]["characters"][0]["name"] == "Ткач"


@pytest.mark.asyncio
async def test_load_excel_hero_auto_heals_polluted_names(tmp_path):
    """Проверяем авто-исцеление технического имени c02 ('оставь формат неизменным')."""
    project = Project(
        id=992,
        slug="test-proj-healed",
        status=ProjectStatus.generating_hero,
        hero_mode="auto",
        hero_description="Ткач",
        hero_count=1,
        meta={},
    )

    ents = [
        Entity(id=1, project_id=992, type="character", code="c01", name="Ткач", attrs={"look": "ткач"}, sort_key=10.0),
        Entity(
            id=2,
            project_id=992,
            type="character",
            code="c02",
            name="оставь формат неизменным",
            attrs={"look": "ткач в грязной одежде", "rules": "c01"},
            sort_key=20.0,
        ),
    ]

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = ents
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result
    mock_session.flush = AsyncMock()

    cfg = await _load_excel_hero_from_xlsx(mock_session, project)
    assert cfg is not None
    assert len(cfg["characters"]) == 2
    c02 = cfg["characters"][1]
    assert c02["id"] == "c02"
    assert "Ткач (вариация c02)" in c02["name"]


def test_render_hero_text_preserves_style_without_placeholder():
    """Проверяем, что стиль принудительно добавляется, даже если шаблон не содержит {{HERO_STYLE}}."""
    template_without_placeholder = "Создай промт для персонажа на белом фоне."
    rendered = render_hero_text(
        template_without_placeholder,
        brief="Мальчик 12 лет",
        hero_style="Cinematic 8k photorealism, soft lighting",
    )
    assert "Visual style (применять обязательно):" in rendered
    assert "Cinematic 8k photorealism, soft lighting" in rendered
    assert "Описание персонажа:" in rendered
    assert "Мальчик 12 лет" in rendered


def test_render_hero_text_with_placeholders():
    """Проверяем стандартную замену плейсхолдеров в шаблоне."""
    template = f"Шаблон:\nStyle: {HERO_PLACEHOLDER_STYLE}\nBrief: {HERO_PLACEHOLDER_BRIEF}"
    rendered = render_hero_text(
        template,
        brief="Художник",
        hero_style="Anime style",
    )
    assert "Style: Anime style" in rendered
    assert "Brief: Художник" in rendered


def test_render_html_report_validity():
    """Проверяем структуру и наполнение HTML-отчёта мониторинга."""
    analysis = {
        "total_events": 42,
        "screenshots_count": 5,
        "projects_seen": [101, 102],
        "timing": {
            "img_pr": {"count": 4, "total_s": 45.2, "avg_s": 11.3, "min_s": 8.0, "max_s": 15.0},
            "generate_images": {"count": 10, "total_s": 120.0, "avg_s": 12.0, "min_s": 10.0, "max_s": 14.0},
        },
        "errors": [
            {
                "ts": "2026-09-07T12:34:56",
                "project_id": 101,
                "error_type": "DNSLookupError",
                "error_msg": "Yandex Cloud DNS timeout",
                "screenshot": "screen_101.png",
            }
        ],
        "event_counts": {"step_start": 14, "step_end": 14, "error": 1},
    }

    html = render_html_report(analysis, title="Тестовый отчёт")
    assert "<!DOCTYPE html>" in html
    assert "Тестовый отчёт" in html
    assert "#101" in html
    assert "DNSLookupError" in html
    assert "Yandex Cloud DNS timeout" in html
    assert "img_pr" in html
    assert "45.2s" in html
