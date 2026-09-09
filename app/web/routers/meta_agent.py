"""Роутер Мета-Агента: компиляция и сохранение системных мастер-промптов."""

from __future__ import annotations

import json
import re
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import commit_with_retry
from app.web.deps import get_session
from app.models import Project
from app.services.meta_prompt_compiler import compile_meta_prompt
from app.services.prompt_library import (
    STEP_FOLDERS,
    step_dir,
    touch_prompt_meta,
    _sanitize_name,
)

router = APIRouter(prefix="/meta-agent", tags=["meta-agent"])


class ProjectAssistRequest(BaseModel):
    topic_draft: str = Field(default="", description="Черновик сюжета/идеи от пользователя")
    title_draft: str = Field(default="", description="Черновик названия (если есть)")
    tone: str | None = Field(default=None, description="Желаемый тон/атмосфера (опционально)")
    voiceover_style: str | None = Field(default=None, description="Стиль озвучки (опционально)")
    mode: str = Field(default="expand", description="'expand' (доработать) или 'generate' (с нуля)")


class ProjectAssistResponse(BaseModel):
    ok: bool = True
    title: str
    topic: str
    suggested_hero_mode: str = "auto"
    tone: str | None = None
    voiceover_style: str | None = None


def _clean_json_reply(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    else:
        m2 = re.search(r"(\{.*\})", text, re.DOTALL)
        if m2:
            text = m2.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        return {}


class CompilePromptRequest(BaseModel):
    step_code: str = Field(..., description="Код шага/папки (excel_gpt, hero_style, items, img_pr, etc.)")
    user_intent: str = Field(..., description="Описание задачи на естественном языке")
    project_id: int | None = Field(default=None, description="ID проекта (для контекста темы)")
    target_name: str | None = Field(default=None, description="Желаемое имя файла/пресета")


class SaveAndActivateRequest(BaseModel):
    step_code: str = Field(..., description="Код шага/папки")
    name: str = Field(..., description="Имя файла без .md")
    content: str = Field(..., description="Тело промпта")
    project_id: int | None = Field(default=None, description="ID проекта для активации")
    activate: bool = Field(default=True, description="Активировать этот вариант в проекте")


@router.post("/compile")
async def compile_prompt_endpoint(
    req: CompilePromptRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Генерирует готовый системный промпт через LLM."""
    topic = ""
    if req.project_id:
        project = await db.get(Project, req.project_id)
        if project:
            topic = project.topic or project.title or ""

    try:
        result = await compile_meta_prompt(
            step_code=req.step_code,
            user_intent=req.user_intent,
            project_topic=topic,
            target_name=req.target_name or "",
        )
        return {"ok": True, **result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ошибка генерации промпта: {exc}")


@router.post("/save-and-activate")
async def save_and_activate_endpoint(
    req: SaveAndActivateRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Сохраняет скомпилированный .md файл и при необходимости активирует его."""
    clean_code = (req.step_code or "").strip().lower()
    if clean_code not in STEP_FOLDERS:
        raise HTTPException(status_code=400, detail=f"Неизвестный step_code: {clean_code}")

    clean_name = _sanitize_name(req.name or "custom_agent_prompt")
    if not clean_name:
        clean_name = "custom_agent_prompt"

    folder = step_dir(clean_code)
    target_file = folder / f"{clean_name}.md"

    # Запись на диск
    target_file.write_text(req.content, encoding="utf-8")
    touch_prompt_meta(clean_code, clean_name, len(req.content.encode("utf-8")))

    activated = False
    if req.activate and req.project_id:
        project = await db.get(Project, req.project_id)
        if project:
            overrides = dict(project.prompt_overrides or {})
            overrides[clean_code] = clean_name
            project.prompt_overrides = overrides
            await commit_with_retry(db)
            activated = True

    return {
        "ok": True,
        "step_code": clean_code,
        "name": clean_name,
        "file_name": f"{clean_name}.md",
        "file_path": str(target_file),
        "activated": activated,
    }


@router.post("/assist-project", response_model=ProjectAssistResponse)
async def assist_project_endpoint(req: ProjectAssistRequest) -> ProjectAssistResponse:
    """ИИ-ассистент для создания проекта: разворачивает черновик идеи в синематик-бриф с цитатами или генерирует идею с нуля."""
    from loguru import logger
    from app.services.gpt_client import get_gpt_client

    tone_hints = {
        "grimdark": "Атмосфера: Grimdark / Sci-Fi (мрачная эстетика, металл, разряды молний, дым, тяжелый реализм).",
        "action": "Атмосфера: Кино-экшен (высокая динамика, напряжение, адреналин, тактический бой).",
        "drama": "Атмосфера: Философия / Драма (глубокий психологизм, тяжесть выбора, внутренний конфликт).",
        "mystery": "Атмосфера: Мистика / Саспенс (тревожная неизвестность, древние тайны, нагнетание).",
    }
    voice_hints = {
        "epic_quotes": (
            "Озвучка и цитаты: Глубокий, брутальный закадровый голос рассказчика (баритон). "
            "В ключевые моменты и драматические паузы — сильные, афористичные жизненные цитаты о долге, чести, цене победы и бренности жизни."
        ),
        "narrator": "Озвучка: Спокойный, вдумчивый кинематографичный рассказчик от третьего лица.",
        "dynamic": "Озвучка: Динамичный, ёмкий закадровый голос с короткими рублеными фразами.",
        "none": "Озвучка: Без диктора (чистый саунд-дизайн, музыка и звуковые эффекты).",
    }

    tone_text = tone_hints.get(str(req.tone or "").lower().strip(), "")
    voice_text = voice_hints.get(str(req.voiceover_style or "").lower().strip(), "")

    system_prompt = (
        "Ты — опытный креативный продюсер и сценарист видеороликов.\n"
        "Твоя задача — помочь автору сформулировать захватывающий сюжетный бриф для генерации видео в студии.\n\n"
        "ПРАВИЛА:\n"
        "1. Заголовок (title): емкий, цепляющий, 3-6 слов, БЕЗ эмодзи, на русском языке.\n"
        "2. Сюжет (topic): связный, кинематографичный текст на 120-250 слов, описывающий:\n"
        "   - Сеттинг и локацию;\n"
        "   - Главных героев или действующих лиц;\n"
        "   - Развитие событий и яркую кульминацию;\n"
        "   - Блок «Озвучка и цитаты»: обязательно укажи стиль подачи голоса и 2-3 конкретные сильные жизненные/философские цитаты, которые должны прозвучать в сценарии.\n"
        "3. Верни результат СТРОГО в формате JSON без вступительных слов и без маркдаун-тегов, кроме кода JSON:\n"
        "{\n"
        '  "title": "Хлёсткое название",\n'
        '  "topic": "Текст сюжета с описанием и блоком озвучки...",\n'
        '  "suggested_hero_mode": "hero" | "no_hero" | "auto"\n'
        "}"
    )

    title_in = (req.title_draft or "").strip()
    topic_in = (req.topic_draft or "").strip()

    if req.mode == "generate":
        if title_in:
            user_prompt = f"Придумай оригинальную захватывающую сюжетную идею для кинематографичного ролика по теме/названию «{title_in}»."
        else:
            user_prompt = "Придумай оригинальную захватывающую идею и название для кинематографичного ролика с нуля."
        if tone_text:
            user_prompt += f"\nЖелаемая {tone_text}"
        if voice_text:
            user_prompt += f"\n{voice_text}"
    else:
        if topic_in:
            user_prompt = f"Развей и структурируй идею автора в кинематографичный бриф:\n«{topic_in}»"
        elif title_in:
            user_prompt = f"Развей и структурируй сюжетный бриф для кинематографичного ролика на основе названия: «{title_in}»"
        else:
            user_prompt = "Сформулируй кинематографичный сюжетный бриф для ролика."
        if title_in:
            user_prompt += f"\nИсходное название: {title_in}"
        if tone_text:
            user_prompt += f"\nЖелаемая {tone_text}"
        if voice_text:
            user_prompt += f"\n{voice_text}"

    full_prompt = f"{system_prompt}\n\nЗАДАЧА:\n{user_prompt}"

    try:
        gpt = get_gpt_client()
        reply = await gpt.ask_fresh(full_prompt, timeout=60)
        parsed = _clean_json_reply(reply)
        title = title_in or str(parsed.get("title") or "").strip()
        topic = str(parsed.get("topic") or "").strip()
        hero_mode = str(parsed.get("suggested_hero_mode") or "auto").strip()
        if not title:
            title = "Новый проект"
        if not topic:
            topic = reply.strip() or topic_in
        return ProjectAssistResponse(
            ok=True,
            title=title,
            topic=topic,
            suggested_hero_mode=hero_mode if hero_mode in {"hero", "no_hero", "auto"} else "auto",
            tone=req.tone,
            voiceover_style=req.voiceover_style,
        )
    except Exception as exc:
        logger.warning("assist_project_endpoint error: {}", exc)
        fallback_title = req.title_draft.strip() or "Новый проект"
        fallback_topic = req.topic_draft.strip()
        if not fallback_topic and req.tone:
            fallback_topic = tone_hints.get(req.tone, "")
        return ProjectAssistResponse(
            ok=False,
            title=fallback_title,
            topic=fallback_topic,
            suggested_hero_mode="auto",
            tone=req.tone,
            voiceover_style=req.voiceover_style,
        )