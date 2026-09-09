"""Шаг 4 (Hero): генерация референса главного героя — с поддержкой
вариаций и обязательным «стилем персонажа» (мастер-промт из
prompts/04_hero_style/).

Контракт работы шага (per-variation HITL):

  Один вызов `run(...)` обрабатывает РОВНО ОДНУ пару
  (hero_index, variation_index) — следующую неодобренную в порядке
  обхода (h=1..N, v=1..K_h). Каждая вариация попадает в TG отдельным
  фото с HITL-кнопками (✅/🔁/❌). После ✅ воркер вернётся в
  generating_hero и сделает следующую вариацию (или следующего героя,
  или поставит hero_ready). После 🔁 перегенерит ТУ ЖЕ вариацию.

  В hero_descriptions[i-1] лежит человекописанное описание героя i.
  В hero_variations[i-1]   лежит кол-во вариаций (1..5) для героя i.
  В hero_variation_modifiers[i-1][j-2] — текст «отличий» для вариации
  j∈[2..K_h] героя i (что менять относительно вариации 1).
  В overrides['hero_style'] лежит имя пресета стиля
  (prompts/04_hero_style/<name>.md).

  Промт от ChatGPT генерится ОДИН РАЗ на героя — на v=1 — и кешится в
  meta артефакта. Вариации 2..K_h берут его из meta v=1 и приклеивают
  к нему «текст отличий» + reference_image=v=1.png.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from aiogram import Bot
from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.browser import browser_session
from app.services.gpt_client import get_gpt_client
from app.bots.outsee import (
    OutseeBot,
    OutseeContentRejectedError,
    OutseeImageError,
)
from app.generation_options import (
    DEFAULTS,
    IMAGE_GENERATORS_BY_ID,
    IMAGE_RESOLUTIONS_BY_ID,
    OUTSEE_PROMPT_MAX_CHARS,
)
from app.models import (
    Artifact,
    ArtifactKind,
    HITLDecision,
    HITLKind,
    HITLRequest,
    Project,
    ProjectStatus,
)
from app.services import gpt_text_builder as gtb
from app.services.excel_characters import ExcelCharacter
from app.services.hitl import send_hitl_photo
from app.services.outsee_retry import generate_image_with_retries
from app.services.prompt_library import (
    prompt_path,
    resolve_project_prompt_name,
)
from app.settings import settings
from app.storage import for_project as _sheet_for_project

# Hero turnaround sheet всегда генерится в ландшафтном 16:9 и с Relax=ON,
# НЕЗАВИСИМО от настроек проекта (которые под shorts — 9:16 и
# обычно Relax=OFF). Причина: референсный лист «стороны/выражения»
# влезает в 16:9, а Relax при этом дёшевле и идёт без очереди.
HERO_ASPECT_RATIO = "16:9"
HERO_RELAX = True


def _excel_hero_http_primary() -> bool:
    """Outsee HTTP — без Chrome CDP (параллельные волны не валятся)."""
    from app.bots.outsee_http import outsee_api_configured, outsee_api_enabled_for_image

    return bool(outsee_api_enabled_for_image() or outsee_api_configured())


@asynccontextmanager
async def _optional_browser_session(
    need_cdp: bool,
) -> AsyncIterator[Any]:
    if not need_cdp:
        yield None
        return
    async with browser_session() as bs:
        yield bs


def _read_hero_style(project: Project) -> str | None:
    """Возвращает содержимое выбранного для проекта пресета стиля
    из prompts/04_hero_style/. Если стиль не задан или файл отсутствует
    — возвращает None (вызывающий должен решить, как фоллбэчить)."""
    overrides = getattr(project, "prompt_overrides", None) or {}
    meta = getattr(project, "meta", None) or {}
    name = resolve_project_prompt_name(overrides, "hero_style", meta=meta)
    p = prompt_path("hero_style", name)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[#{}] hero_style read failed ({}): {}", project.id, p, e
        )
        return None


def _hero_target_pairs(
    n_total: int, variations_cfg: list[int]
) -> list[tuple[int, int]]:
    """Список всех пар (hero_idx, var_idx) в порядке обхода: сначала
    все вариации героя 1, потом героя 2, и т.д. Каждая вариация —
    отдельная HITL-карточка."""
    pairs: list[tuple[int, int]] = []
    for hi in range(1, n_total + 1):
        n_var = 1
        if hi - 1 < len(variations_cfg):
            try:
                n_var = int(variations_cfg[hi - 1] or 1)
            except (TypeError, ValueError):
                n_var = 1
        n_var = max(1, min(5, n_var))
        for vi in range(1, n_var + 1):
            pairs.append((hi, vi))
    return pairs


async def _approved_pairs(
    session: AsyncSession, project: Project
) -> set[tuple[int, int]]:
    """Множество (hero_idx, var_idx), для которых уже есть одобренная
    HITL-карточка. variation_index по умолчанию 1 (legacy)."""
    rows = (
        await session.execute(
            select(HITLRequest)
            .where(
                HITLRequest.project_id == project.id,
                HITLRequest.kind == HITLKind.approve_hero,
                HITLRequest.decision == HITLDecision.approved,
            )
        )
    ).scalars().all()
    out: set[tuple[int, int]] = set()
    for r in rows:
        p = r.payload or {}
        hi = p.get("hero_index")
        vi = p.get("variation_index", 1)
        if isinstance(hi, int) and isinstance(vi, int):
            out.add((hi, vi))
    return out


async def _is_regen_for_pair(
    session: AsyncSession,
    project: Project,
    hero_idx: int,
    var_idx: int,
) -> bool:
    """True если ПОСЛЕДНЯЯ HITL-карточка для (hero_idx, var_idx) —
    regenerate. Используется чтобы понять «эту вариацию надо передать
    через кнопку Повторить» (для v=1) вместо fresh generate."""
    rows = (
        await session.execute(
            select(HITLRequest)
            .where(
                HITLRequest.project_id == project.id,
                HITLRequest.kind == HITLKind.approve_hero,
            )
            .order_by(desc(HITLRequest.id))
        )
    ).scalars().all()
    for r in rows:
        p = r.payload or {}
        if p.get("hero_index") == hero_idx and p.get(
            "variation_index", 1
        ) == var_idx:
            return r.decision is HITLDecision.regenerate
    return False


async def _approved_excel_ids(
    session: AsyncSession, project: Project
) -> set[str]:
    """Множество ID персонажей из excel-режима, у которых HITL-карточка
    `approve_hero` помечена как `approved`. ID берётся из `payload.excel_id`."""
    rows = (
        await session.execute(
            select(HITLRequest)
            .where(
                HITLRequest.project_id == project.id,
                HITLRequest.kind == HITLKind.approve_hero,
                HITLRequest.decision == HITLDecision.approved,
            )
        )
    ).scalars().all()
    out: set[str] = set()
    for r in rows:
        p = r.payload or {}
        xid = p.get("excel_id")
        if isinstance(xid, str) and xid:
            out.add(xid)
    return out


async def _excel_ids_with_artifact(
    session: AsyncSession, project: Project
) -> set[str]:
    """ID персонажей, для которых уже есть hero_reference с файлом на диске."""
    rows = (
        await session.execute(
            select(Artifact)
            .where(
                Artifact.project_id == project.id,
                Artifact.kind == ArtifactKind.hero_reference,
            )
            .order_by(desc(Artifact.id))
        )
    ).scalars().all()
    out: set[str] = set()
    for a in rows:
        m = a.meta or {}
        xid = m.get("excel_id")
        if not isinstance(xid, str) or not xid or xid in out:
            continue
        if a.path and Path(a.path).is_file():
            out.add(xid)
    return out


def _excel_batch_auto(project: Project) -> bool:
    """Всегда batch: HITL-одобрений персонажей больше нет.

    Параллель = meta.outsee_streams / img_streams (см. ``_run_excel``).
    """
    _ = project
    return True


def _excel_ref_deps_met(
    ch: ExcelCharacter,
    *,
    approved: set[str],
    generated: set[str],
    batch_auto: bool,
) -> bool:
    """Реф-зависимости: файл рефа должен быть на диске.

    HITL ``approved`` без файла (после wipe на повторном ▶) — НЕ deps:
    иначе batch-цикл крутит персонажа с missing ref и спамит лог.
    В ручном режиме дополнительно требуем approve рефа.
    """
    if not ch.ref_ids:
        return True
    for rid in ch.ref_ids:
        if rid not in generated:
            return False
        if not batch_auto and rid not in approved:
            return False
    return True


async def _is_regen_for_excel_id(
    session: AsyncSession, project: Project, excel_id: str
) -> bool:
    """True если последнее HITL-решение по этому excel_id — regenerate."""
    rows = (
        await session.execute(
            select(HITLRequest)
            .where(
                HITLRequest.project_id == project.id,
                HITLRequest.kind == HITLKind.approve_hero,
            )
            .order_by(desc(HITLRequest.id))
        )
    ).scalars().all()
    for r in rows:
        p = r.payload or {}
        if p.get("excel_id") == excel_id:
            return r.decision is HITLDecision.regenerate
    return False


async def _excel_artifact_for_id(
    session: AsyncSession, project: Project, excel_id: str
) -> Artifact | None:
    """Самый свежий артефакт hero_reference с meta.excel_id == excel_id."""
    rows = (
        await session.execute(
            select(Artifact)
            .where(
                Artifact.project_id == project.id,
                Artifact.kind == ArtifactKind.hero_reference,
            )
            .order_by(desc(Artifact.id))
        )
    ).scalars().all()
    for a in rows:
        m = a.meta or {}
        if m.get("excel_id") == excel_id:
            return a
    return None


async def _v1_artifact_for_hero(
    session: AsyncSession, project: Project, hero_idx: int
) -> Artifact | None:
    """Возвращает САМЫЙ СВЕЖИЙ артефакт v=1 для данного героя (то, что
    мы будем использовать как reference для v>=2 и брать оттуда
    закешированный hero_prompt)."""
    rows = (
        await session.execute(
            select(Artifact)
            .where(
                Artifact.project_id == project.id,
                Artifact.kind == ArtifactKind.hero_reference,
            )
            .order_by(desc(Artifact.id))
        )
    ).scalars().all()
    for a in rows:
        m = a.meta or {}
        if (
            m.get("hero_index") == hero_idx
            and m.get("variation_index") == 1
        ):
            return a
    return None


async def _load_entity_characters(
    session: AsyncSession, project: Project
) -> list:
    """Entity(type=character) → ExcelCharacter list (пусто если нет)."""
    from sqlalchemy import select

    from app.models import Entity
    from app.services.excel_characters import characters_from_entities

    try:
        ents = list(
            (
                await session.execute(
                    select(Entity)
                    .where(
                        Entity.project_id == project.id,
                        Entity.type == "character",
                    )
                    .order_by(Entity.sort_key, Entity.id)
                )
            ).scalars().all()
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[#{}] excel_hero Entity load failed: {}", project.id, e)
        return []
    return characters_from_entities(ents)


async def _load_excel_hero_from_xlsx(
    session: AsyncSession,
    project: Project,
) -> dict | None:
    """Загрузить персонажей для hero: Entity (SoT) → fallback лист Excel."""
    meta = dict(project.meta or {})
    if project.hero_mode == "no_hero":
        return None

    from app.services.excel_characters import (
        is_polluted_character_field,
        parse_persons_sheet,
    )

    chars = await _load_entity_characters(session, project)
    source = "entity"
    if not chars:
        xlsx = project.data_dir / "project.xlsx"
        if xlsx.exists():
            try:
                chars = parse_persons_sheet(xlsx)
            except Exception as e:  # noqa: BLE001
                logger.debug("[#{}] excel_hero load: parse failed: {}", project.id, e)
                chars = []
            if chars:
                source = "xlsx"

    # Если в Entity или xlsx ничего нет — проверяем, задан ли ручной hero
    if not chars:
        return None

    # Если пользователь ЯВНО отключил excel_hero_enabled в meta (meta["excel_hero_enabled"] is False)
    if meta.get("excel_hero_enabled") is False:
        has_manual_hero = bool(
            (project.hero_description or "").strip()
            or (project.hero_count and project.hero_count > 0)
            or any((d or "").strip() for d in (project.hero_descriptions or []))
        )
        if has_manual_hero:
            return None

    # Авто-исцеление загрязненных имен персонажей (например, c02 'оставь формат неизменным')
    id_to_name = {c.id: c.name for c in chars if not is_polluted_character_field(c.name)}
    for c in chars:
        if is_polluted_character_field(c.name):
            parent_name = ""
            for rid in getattr(c, "ref_ids", []):
                if rid in id_to_name:
                    parent_name = id_to_name[rid]
                    break
            old_name = c.name
            if parent_name:
                c.name = f"{parent_name} (вариация {c.id})"
            else:
                c.name = f"Персонаж {c.id}"
            logger.warning(
                "[#{}] excel_hero {}: авто-исправление служебного имени {!r} -> {!r}",
                project.id,
                c.id,
                old_name,
                c.name,
            )

    cfg = {"characters": [c.to_dict() for c in chars], "source": source}
    meta["excel_hero"] = cfg
    meta["excel_hero_enabled"] = True
    project.meta = meta
    await session.flush()
    logger.info(
        "[#{}] excel_hero load: {} персонаж(ей) из {} ",
        project.id,
        len(chars),
        source,
    )
    return cfg


# Обратная совместимость для тестов и внешних импортов.
_bootstrap_excel_hero_from_xlsx = _load_excel_hero_from_xlsx


async def run(session: AsyncSession, project: Project, bot: Bot) -> None:
    if project.status is not ProjectStatus.generating_hero:
        return

    # Excel-режим: если юзер запустил генерацию из листа «Персонажи»
    # (кнопка «🧾 Из EXCEL»), в project.meta['excel_hero'] лежит список
    # персонажей. Идём по отдельной ветке — hero_descriptions/hero_count
    # в этом режиме не используются.
    meta = dict(project.meta or {})
    excel_cfg = meta.get("excel_hero")
    # После enrich_3 xlsx на диске новее meta — всегда перечитываем.
    if isinstance(excel_cfg, dict) or meta.get("excel_hero_enabled"):
        loaded = await _load_excel_hero_from_xlsx(session, project)
        if loaded:
            excel_cfg = loaded
    elif not (excel_cfg and isinstance(excel_cfg, dict)):
        excel_cfg = await _load_excel_hero_from_xlsx(session, project)
    if excel_cfg and isinstance(excel_cfg, dict):
        await _run_excel(session, project, bot, excel_cfg)
        return

    if project.hero_mode == "no_hero" or project.hero_count == 0:
        meta = dict(project.meta or {})
        meta["hero_skipped_empty"] = True
        project.meta = meta
        logger.info(
            "[#{}] hero skipped (hero_mode={}, hero_count={})",
            project.id, project.hero_mode, project.hero_count,
        )
        project.status = ProjectStatus.hero_ready
        await session.flush()
        return

    # Конфиг героев из проекта.
    descriptions: list[str] = list(project.hero_descriptions or [])
    variations_cfg: list[int] = list(project.hero_variations or [])
    n_total = project.hero_count or (1 if project.hero_description else 0)
    if n_total == 0:
        # legacy fallback: hero_description есть, hero_count не задан → 1.
        if project.hero_description:
            descriptions = [project.hero_description]
            n_total = 1
        else:
            if (project.hero_description or "").strip():
                descriptions = [project.hero_description.strip()]
                n_total = 1
                if not project.hero_count:
                    project.hero_count = 1
            else:
                # Ещё раз excel (лист «Персонажи») — мог появиться после enrich.
                meta["excel_hero_enabled"] = True
                project.meta = meta
                await session.flush()
                loaded = await _load_excel_hero_from_xlsx(session, project)
                if loaded:
                    await _run_excel(session, project, bot, loaded)
                    return
                # Пустой skip: помечаем meta, иначе clamp откатит hero_ready →
                # frames_ready и auto_advance снова запустит generating_hero
                # (бесконечный цикл каждые ~5 с).
                meta = dict(project.meta or {})
                meta["hero_skipped_empty"] = True
                project.meta = meta
                logger.warning(
                    "[#{}] hero: hero_count/description пусты и лист "
                    "«Персонажи» без данных — пропускаю шаг (hero_ready, "
                    "hero_skipped_empty). Заполни «Описание героя» на листе "
                    "«Общий план» или столбцы на «Персонажи», затем ▶ Hero",
                    project.id,
                )
                project.status = ProjectStatus.hero_ready
                await session.flush()
                return

    # Определяем СЛЕДУЮЩУЮ пару (hero_idx, var_idx), которую надо сделать.
    target_pairs = _hero_target_pairs(n_total, variations_cfg)
    approved = await _approved_pairs(session, project)
    target: tuple[int, int] | None = None
    for pair in target_pairs:
        if pair not in approved:
            target = pair
            break
    if target is None:
        logger.info(
            "[#{}] hero: все {} пар (hero, variation) одобрены — "
            "перехожу к hero_ready",
            project.id, len(target_pairs),
        )
        project.status = ProjectStatus.hero_ready
        return
    hero_idx, v_idx = target

    user_brief = (
        descriptions[hero_idx - 1]
        if hero_idx - 1 < len(descriptions) else ""
    ).strip()
    if len(user_brief) < 5:
        raise RuntimeError(
            f"hero_descriptions[{hero_idx - 1}] пустой — нечем описать "
            f"героя {hero_idx}/{n_total}. Тыкни «4. Hero» в меню заново."
        )

    # Кол-во вариаций героя (нужно для подписи в HITL и для индексации
    # модификаторов).
    n_variations = 1
    if hero_idx - 1 < len(variations_cfg):
        try:
            n_variations = int(variations_cfg[hero_idx - 1] or 1)
        except (TypeError, ValueError):
            n_variations = 1
    n_variations = max(1, min(5, n_variations))

    # Текстовые «отличия» для вариаций 2..N этого героя.
    modifiers_all = list(
        getattr(project, "hero_variation_modifiers", None) or []
    )
    variation_mods_for_hero: list[str] = []
    if hero_idx - 1 < len(modifiers_all):
        raw = modifiers_all[hero_idx - 1] or []
        if isinstance(raw, list):
            variation_mods_for_hero = [str(x or "").strip() for x in raw]

    is_regen = await _is_regen_for_pair(session, project, hero_idx, v_idx)

    logger.info(
        "[#{}] generate_hero pair=({}/{}, v{}/{}) starting "
        "(brief: {} симв, regen={})",
        project.id, hero_idx, n_total, v_idx, n_variations,
        len(user_brief), is_regen,
    )

    # Стиль персонажа (мастер-промт из prompts/04_hero_style/) —
    # обязательно подмешивается к ChatGPT-промту, чтобы итоговое
    # изображение было в нужном визуале (фото-реализм / аниме / 3D / etc).
    hero_style_content = _read_hero_style(project)
    style_chosen = (
        getattr(project, "prompt_overrides", None) or {}
    ).get("hero_style") or "default"
    if not hero_style_content:
        # Hard fallback: текст-плейсхолдер. Не падаем — но логируем.
        logger.warning(
            "[#{}] hero_style '{}' не найден на диске — продолжаю без стиля",
            project.id, style_chosen,
        )
        hero_style_content = ""

    # Загружаем уже готовый hero_prompt из meta артефакта v=1 (если есть и
    # это не его собственная регенерация).
    hero_prompt: str = ""
    if v_idx >= 2:
        v1_art = await _v1_artifact_for_hero(session, project, hero_idx)
        if v1_art is not None:
            cached = (v1_art.meta or {}).get("hero_prompt")
            if isinstance(cached, str) and len(cached) >= 100:
                hero_prompt = cached
                logger.info(
                    "[#{}] hero pair=({}, v{}): использую закешированный "
                    "hero_prompt из meta v=1 артефакта (id={}, {} симв)",
                    project.id, hero_idx, v_idx, v1_art.id, len(hero_prompt),
                )

    async with browser_session() as bs:
        # `gpt` нужен ОБОИМ путям:
        #   - v=1 / cache miss — для генерации hero_prompt из ChatGPT;
        #   - любой v — для GPT-rewrite внутри generate_image_with_retries
        #     (после 3 неудачных попыток в outsee он попросит ChatGPT
        #     переписать промт без триггеров модерации).
        gpt = get_gpt_client()

        # 1) ChatGPT — нужен только для v=1 (или если кеш не нашёлся).
        if not hero_prompt:
            # Текст «сопр. сообщения» берём из gpt_text_builder — там же
            # лежит выбор между юзерским override и дефолтом. Обязательно
            # прогоняем через `render_hero_text` — в шаблоне живут литеральные
            # плейсхолдеры `{{BRIEF}}` и `{{HERO_STYLE}}`, заполняем их
            # отдельно для этого героя.
            hero_template = gtb.get_effective_text(project, "hero")
            hero_ask = gtb.render_hero_text(
                hero_template, brief=user_brief, hero_style=hero_style_content,
            )
            last_reply = ""
            for attempt in range(1, 4):
                ask = hero_ask
                if (
                    attempt > 1
                    and last_reply
                    and len(last_reply) > OUTSEE_PROMPT_MAX_CHARS
                ):
                    ask = (
                        f"Прошлый ответ был {len(last_reply)} символов — это "
                        f"больше лимита {OUTSEE_PROMPT_MAX_CHARS}. Сожми его до "
                        f"≤{OUTSEE_PROMPT_MAX_CHARS} символов: убери повторы, "
                        "объедини похожие пункты, оставь самое важное. "
                        "Структуру (turnaround sheet) сохрани. Верни ТОЛЬКО "
                        "новый сокращённый промт, без пояснений.\n\n"
                        "Прошлый промт:\n\n" + last_reply
                    )
                reply = await gpt.ask_fresh(ask, timeout=600, project_id=project.id)
                last_reply = reply or ""
                logger.info(
                    "[#{}] hero ChatGPT attempt {}: {} симв",
                    project.id, attempt, len(last_reply),
                )
                logger.info(
                    "[#{}] hero ChatGPT preview:\n{}",
                    project.id, last_reply[:600],
                )
                if not last_reply or len(last_reply) < 100:
                    logger.warning(
                        "[#{}] hero ChatGPT вернул слишком короткий ответ "
                        "({} симв), пробую ещё раз",
                        project.id, len(last_reply),
                    )
                    continue
                hero_prompt = last_reply.strip()
                if len(hero_prompt) <= OUTSEE_PROMPT_MAX_CHARS:
                    break
                logger.warning(
                    "[#{}] hero ChatGPT вернул {} симв (лимит {}), "
                    "прошу сжать",
                    project.id, len(hero_prompt), OUTSEE_PROMPT_MAX_CHARS,
                )
            if not hero_prompt:
                raise RuntimeError(
                    f"ChatGPT не вернул заполненный hero-промт после 3 "
                    f"попыток. Последний ответ ({len(last_reply)} симв): "
                    f"{last_reply[:200]!r}"
                )
            if len(hero_prompt) > OUTSEE_PROMPT_MAX_CHARS:
                logger.warning(
                    "[#{}] hero prompt всё ещё длиннее лимита: {} > {} — "
                    "отправляю как есть, outsee может не принять",
                    project.id, len(hero_prompt), OUTSEE_PROMPT_MAX_CHARS,
                )

        # 2) Сборка финального prompt_text.
        # v=1: полный hero_prompt от ChatGPT (без референса).
        # v>=2: ТОЛЬКО текст-модификатор от пользователя + reference-картинка
        #       (которая прикрепляется отдельно через `reference_image=`).
        #       Полный hero_prompt НЕ повторяем — outsee получит «keep this
        #       character + change <X>», где character закодирован в самом
        #       reference image. Этот короткий промт сильно реже триггерит
        #       модерацию, чем длинный hero_prompt с описанием анатомии.
        if v_idx == 1:
            prompt_text = hero_prompt
        else:
            modifier_text = ""
            idx_in_mods = v_idx - 2
            if 0 <= idx_in_mods < len(variation_mods_for_hero):
                modifier_text = variation_mods_for_hero[idx_in_mods]
            if modifier_text.strip():
                prompt_text = modifier_text.strip()
            else:
                # Модификатор не задан — отправляем минимально-короткую
                # инструкцию: «другая поза/ракурс/наряд для того же
                # персонажа на референсе». Без длинного hero_prompt.
                prompt_text = (
                    "Different pose, camera angle, or outfit for the same "
                    "character shown in the reference image."
                )
        logger.info(
            "[#{}] hero pair=({}, v{}): prompt {} симв "
            "(style='{}', regen={})",
            project.id, hero_idx, v_idx, len(prompt_text),
            style_chosen, is_regen,
        )

        # 3) Reference-картинка для v>=2: путь к v=1 артефакту героя.
        ref_path: Path | None = None
        if v_idx >= 2:
            v1_art = await _v1_artifact_for_hero(session, project, hero_idx)
            if v1_art is not None and v1_art.path:
                cand = Path(v1_art.path)
                if cand.exists():
                    ref_path = cand
                else:
                    logger.warning(
                        "[#{}] hero v{}: v=1 файл {} не найден на диске — "
                        "пойду без референса",
                        project.id, v_idx, cand,
                    )
            else:
                logger.warning(
                    "[#{}] hero v{}: v=1 артефакт героя {} не найден — "
                    "пойду без референса",
                    project.id, v_idx, hero_idx,
                )

        # 4) Генерация в outsee.
        outsee = OutseeBot(bs)
        out_dir = project.data_dir / "characters"
        from app.services.vibecode_catalog import resolve_node_media_settings

        media = resolve_node_media_settings(project, node_type="hero")
        img_gid = media["image_generator_id"]
        img_gen = IMAGE_GENERATORS_BY_ID.get(img_gid)
        # Aspect ratio и Relax для hero жёстко захардкожены: 16:9 + Relax=ON.
        # См. HERO_ASPECT_RATIO / HERO_RELAX в верху файла.
        ir = IMAGE_RESOLUTIONS_BY_ID.get(media["resolution_id"])
        quality_slug = media["quality_slug"]

        short_uuid = uuid.uuid4().hex[:8]
        file_name = f"hero_{hero_idx}_v{v_idx}_{short_uuid}.png"
        out_path = out_dir / file_name
        prompt_id_prefix = (
            f"[ID: P{project.id}-HERO{hero_idx}-V{v_idx}-{short_uuid}]"
        )

        result = None
        if v_idx == 1 and is_regen:
            logger.info(
                "[#{}] regenerate hero {}/{} v1: пробую кнопку «Повторить»",
                project.id, hero_idx, n_total,
            )
            try:
                result = await outsee.regenerate_image(out_path)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[#{}] «Повторить» не сработала ({}), делаю fresh "
                    "generate", project.id, e,
                )
                result = None

        if result is None:
            # Внутри generate_image_with_retries:
            #   - до 3 попыток с исходным prompt_text;
            #   - если все 3 провалились — ChatGPT переписывает промт
            #     и ещё 3 попытки. Если все 6 провалились — НЕ паркуем
            #     в failed (юзер каждый раз потом должен «вытаскивать»
            #     проект, а другие шаги становятся 🔒 — это бесит).
            #     Вместо этого откатываем status на frames_ready (туда
            #     откуда юзер запустил шаг), шлём ясное TG-сообщение
            #     и выходим. Юзер правит описание героя и просто жмёт
            #     «4. Hero» снова — никакого «failed → unfail» дёрганья.
            try:
                result = await generate_image_with_retries(
                    outsee, gpt,
                    prompt=prompt_text,
                    out_path=out_path,
                    max_attempts_per_prompt=3,
                    gpt_rewrite=True,
                    aspect_ratio=HERO_ASPECT_RATIO,
                    model_slug=img_gen.outsee_slug if img_gen else None,
                    resolution=ir.outsee_slug if ir else None,
                    quality=quality_slug,
                    relax=HERO_RELAX,
                    prompt_id_prefix=prompt_id_prefix,
                    reference_image=ref_path,
                    timeout=600,
                )
            except OutseeImageError as e:
                # Отличаем модерационный reject от прочего
                # (таймаут/селектор отвалился) — текст в TG разный.
                is_moderation = isinstance(e, OutseeContentRejectedError)
                logger.error(
                    "[#{}] hero pair=({}/{}, v{}/{}) RAN OUT попыток: {}",
                    project.id, hero_idx, n_total, v_idx, n_variations,
                    e.reason if hasattr(e, "reason") else str(e),
                )
                # Откатываем статус ровно туда, откуда юзер тыкнул
                # «4. Hero» — это `frames_ready`. Все остальные шаги
                # тогда не блокируются 🔒 (как при `failed`), и юзер
                # может просто исправить описание героя и снова нажать
                # «4. Hero». Никакого «вытащить проект из failed».
                project.status = ProjectStatus.frames_ready
                await session.flush()
                project_label = (project.topic or project.slug or "")[:60]
                if is_moderation:
                    msg = (
                        f"🚫 Проект #{project.id} «{project_label}» — hero {hero_idx}/{n_total} v{v_idx}/{n_variations}:\n"
                        f"6 попыток в outsee подряд отклонены модерацией (3 оригинал + 3 после GPT-rewrite).\n\n"
                        f"Статус откатил обратно в <b>frames_ready</b> — проект НЕ failed, "
                        f"никакие файлы/настройки не трогал. Действия:\n"
                        f"1) В <code>/menu → проект → ⚙ Настройки</code> поправь «Описание героя» "
                        f"(убери реальные имена/страны/эпохи/религии, добавь safety-приписку).\n"
                        f"2) Нажми «4. Hero» ещё раз — пойдёт повтор.\n\n"
                        f"Последняя ошибка от outsee:\n<code>{(getattr(e, 'reason', None) or str(e))[:400]}</code>"
                    )
                else:
                    msg = (
                        f"🚫 Проект #{project.id} «{project_label}»: hero "
                        f"{hero_idx}/{n_total} v{v_idx}/{n_variations} — "
                        f"outsee 6 раз подряд провалился (не модерация, что-то с outsee/сетью).\n"
                        f"<code>{(getattr(e, 'reason', None) or str(e))[:600]}</code>\n"
                        f"Статус откатил в <b>frames_ready</b>. Проверь Chrome/outsee и нажми «4. Hero» снова."
                    )
                try:
                    await bot.send_message(
                        settings.telegram_owner_chat_id,
                        msg[:3800],
                        parse_mode="HTML",
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "[#{}] не удалось отправить TG-ошибку пользователю",
                        project.id,
                    )
                # Выходим ЧИСТО — без raise. worker loop увидит
                # status=frames_ready (не «active» в его списке) и проект
                # больше не будет автоматически брать на этой итерации.
                return

    # 5) Сохраняем артефакт ОДНОЙ вариации.
    file_path = Path(result.file_path)
    art_meta: dict = {
        "hero_index": hero_idx,
        "variation_index": v_idx,
        "variations_total": n_variations,
        "hero_total": n_total,
    }
    if v_idx == 1:
        # Кешируем hero_prompt именно в meta v=1 артефакта — вариации
        # 2..N будут читать его отсюда без повторного запроса в ChatGPT.
        art_meta["hero_prompt"] = hero_prompt
    art = Artifact(
        project_id=project.id,
        kind=ArtifactKind.hero_reference,
        uuid=uuid.uuid4().hex,
        path=str(file_path),
        meta=art_meta,
    )
    session.add(art)
    project.status = ProjectStatus.hero_ready
    await session.flush()

    # 6) xlsx — пишем «текущий» референс героя.
    try:
        _sheet_for_project(project).write_general(
            status=project.status.value,
            hero_description=user_brief,
            hero_image_path=str(file_path),
            hero_image_url=getattr(result, "raw_url", None),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[#{}] project_sheet hero write failed: {}", project.id, e)

    # 7) HITL-карточка на ЭТУ вариацию (юзер одобряет каждую отдельно).
    final_prompt_id_prefix = prompt_id_prefix
    if v_idx < n_variations:
        approve_hint = (
            f"✅ — принять и перейти к v{v_idx + 1}/{n_variations}; "
            f"🔁 — перегенерить эту вариацию."
        )
    elif hero_idx < n_total:
        approve_hint = (
            f"✅ — принять и перейти к герою {hero_idx + 1}/{n_total}; "
            f"🔁 — перегенерить эту вариацию."
        )
    else:
        approve_hint = (
            "✅ — принять (это последняя вариация последнего героя); "
            "🔁 — перегенерить эту вариацию."
        )
    await send_hitl_photo(
        bot, session, project,
        kind=HITLKind.approve_hero,
        photo_path=str(file_path),
        caption=(
            f"{final_prompt_id_prefix}\n"
            f"Герой {hero_idx}/{n_total}, "
            f"вариация {v_idx}/{n_variations}.\n"
            f"Стиль: {style_chosen}\n"
            f"{approve_hint}"
        ),
        payload={
            "step": "hero",
            "artifact_id": art.id,
            "prompt_id_prefix": final_prompt_id_prefix,
            "photo_path": str(file_path),
            "hero_index": hero_idx,
            "hero_total": n_total,
            "variation_index": v_idx,
            "variations_total": n_variations,
            "hero_style": style_chosen,
        },
    )


# ---------------------------------------------------------------------------
# Excel-режим (кнопка «🧾 Из EXCEL» в шаге 4): персонажи берутся из листа
# «Персонажи» project.xlsx. Каждый персонаж — отдельная HITL-карточка.
#   - Без ref_ids → полный GPT-промт (как v=1 обычного hero).
#   - С ref_ids → без GPT, текст изменений из excel + reference image(s)
#                 одобренных персонажей.
# Имя файла на выходе = `<id>.png` (id из R1 листа «Персонажи»).
# ---------------------------------------------------------------------------


def _excel_characters_from_meta(cfg: dict) -> list[ExcelCharacter]:
    raw = cfg.get("characters") or []
    out: list[ExcelCharacter] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        try:
            out.append(ExcelCharacter.from_dict(d))
        except Exception as e:  # noqa: BLE001
            logger.warning("excel_hero: пропускаю кривого персонажа {}: {}", d, e)
    return out


async def _run_excel(
    session: AsyncSession,
    project: Project,
    bot: Bot,
    cfg: dict,
) -> None:
    """Entity/Excel-режим: все персонажи за один run(), без HITL.

    Параллель = ``meta.outsee_streams`` / ``img_streams`` (общий пул Outsee).
    Реф-вариации ждут файл родителя, затем идут следующей волной.
    """
    from app.db import SessionLocal
    from app.services.hero_check_regen import META_IDS, get_hero_check_regen_ids
    from app.services.img_streams import get_img_streams
    from app.services.step_cancel import raise_if_cancelled
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.check_analysis import normalize_hero_excel_id

    batch_auto = True
    streams = get_img_streams(project)
    logger.info(
        "[#{}] excel_hero: batch+parallel outsee_streams={} (без HITL)",
        project.id,
        streams,
    )
    if streams == 0:
        chars0 = _excel_characters_from_meta(cfg)
        generated0 = await _excel_ids_with_artifact(session, project)
        missing = [c.id for c in chars0 if c.id not in generated0]
        if missing:
            raise RuntimeError(
                f"outsee_streams=0: нет PNG у {missing[:12]}. "
                "Поставь потоки 1..4 или положи файлы в characters/."
            )
        project.status = ProjectStatus.hero_ready
        await session.flush()
        return

    project_id = int(project.id)

    while True:
        raise_if_cancelled(project_id)
        await session.refresh(project)
        meta = dict(project.meta or {})
        cfg = meta.get("excel_hero") or cfg
        chars = _excel_characters_from_meta(cfg)
        if not chars:
            logger.warning(
                "[#{}] excel_hero: список персонажей пуст — переход в hero_ready",
                project.id,
            )
            project.status = ProjectStatus.hero_ready
            return

        approved: set[str] = set()  # HITL снят — deps только по файлам
        generated = await _excel_ids_with_artifact(session, project)
        regen_ids = set(get_hero_check_regen_ids(project))

        if not regen_ids and all(ch.id in generated for ch in chars):
            logger.info(
                "[#{}] excel_hero: все {} PNG на диске — hero_ready",
                project.id,
                len(chars),
            )
            project.status = ProjectStatus.hero_ready
            await session.flush()
            return

        ready: list[ExcelCharacter] = []
        skipped: list[ExcelCharacter] = []
        for ch in chars:
            if regen_ids and ch.id not in regen_ids:
                continue
            has_file = ch.id in generated
            is_regen = await _is_regen_for_excel_id(session, project, ch.id)
            if regen_ids and ch.id in regen_ids:
                is_regen = True
            if has_file and not is_regen:
                continue
            if not _excel_ref_deps_met(
                ch, approved=approved, generated=generated, batch_auto=batch_auto
            ):
                skipped.append(ch)
                continue
            ready.append(ch)

        if not ready:
            if regen_ids and not skipped:
                logger.info(
                    "[#{}] excel_hero: check-regen ids {} готовы — hero_ready",
                    project.id,
                    sorted(regen_ids),
                )
                project.status = ProjectStatus.hero_ready
                await session.flush()
                return
            if len(generated) >= len(chars):
                logger.info(
                    "[#{}] excel_hero: batch — все {} артефактов на диске, hero_ready",
                    project.id,
                    len(chars),
                )
                project.status = ProjectStatus.hero_ready
                await session.flush()
                return
            names = ", ".join(ch.id for ch in skipped)
            msg = (
                f"🚫 Проект #{project.id} excel-hero: "
                f"реф-зависимости не закрыты ({names}). "
                "Проверь rules/правила (ссылки на cXX). "
                "Статус откатил в <b>frames_ready</b>."
            )
            logger.error("[#{}] excel_hero deadlock: {}", project.id, names)
            project.status = ProjectStatus.frames_ready
            await session.flush()
            try:
                await bot.send_message(
                    settings.telegram_owner_chat_id, msg, parse_mode="HTML"
                )
            except Exception:  # noqa: BLE001
                logger.warning("[#{}] не удалось отправить TG-deadlock", project.id)
            return

        batch = ready[: max(1, streams)]
        logger.info(
            "[#{}] excel_hero wave n={} ids={} (streams={})",
            project.id,
            len(batch),
            [c.id for c in batch],
            streams,
        )

        # Отпустить write-txn на время параллельной генерации.
        await session.commit()

        async def _one(ch: ExcelCharacter) -> str:
            async with SessionLocal() as s:
                p = (
                    await s.execute(select(Project).where(Project.id == project_id))
                ).scalar_one()
                p.status = ProjectStatus.generating_hero
                await _generate_one_excel_character(
                    s,
                    p,
                    bot,
                    ch,
                    chars=chars,
                    approved=set(),
                    batch_auto=True,
                )
                # SQLite: параллельные commit 4× иногда ловят database is locked.
                for attempt in range(1, 8):
                    try:
                        await s.commit()
                        break
                    except OperationalError as e:
                        if "locked" not in str(e).lower() or attempt >= 7:
                            raise
                        await asyncio.sleep(0.15 * attempt)
                return ch.id

        results = await asyncio.gather(
            *(_one(ch) for ch in batch), return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        ok_ids = [r for r in results if isinstance(r, str)]
        for err in errors:
            logger.error("[#{}] excel_hero wave error: {}", project_id, err)
        if errors and not ok_ids:
            await session.refresh(project)
            project.status = ProjectStatus.frames_ready
            await session.flush()
            raise RuntimeError(
                f"excel_hero: волна упала без успехов: {errors[0]!r}"
            )

        await session.refresh(project)
        project.status = ProjectStatus.generating_hero

        if regen_ids and ok_ids:
            meta_r = dict(project.meta or {})
            left = [
                x
                for x in (meta_r.get(META_IDS) or [])
                if normalize_hero_excel_id(str(x)) not in set(ok_ids)
            ]
            meta_r[META_IDS] = left
            project.meta = meta_r
            flag_modified(project, "meta")
            await session.flush()
            if not left:
                logger.info(
                    "[#{}] excel_hero: check-regen все ids готовы — hero_ready",
                    project.id,
                )
                project.status = ProjectStatus.hero_ready
                await session.flush()
                return

        if project.status is not ProjectStatus.generating_hero:
            return


async def _generate_one_excel_character(
    session: AsyncSession,
    project: Project,
    bot: Bot,
    ch: ExcelCharacter,
    *,
    chars: list[ExcelCharacter],
    approved: set[str],
    batch_auto: bool = False,
) -> None:
    """Генерирует одну картинку для excel-персонажа `ch` и шлёт HITL."""
    is_regen = await _is_regen_for_excel_id(session, project, ch.id)
    used_refs = bool(ch.ref_ids)

    # Reference image(s): берём пути к одобренным артефактам каждого ref.
    ref_paths: list[Path] = []
    if used_refs:
        for rid in ch.ref_ids:
            art = await _excel_artifact_for_id(session, project, rid)
            if art is None or not art.path:
                logger.warning(
                    "[#{}] excel_hero {}: ref {} артефакт не найден",
                    project.id, ch.id, rid,
                )
                continue
            p = Path(art.path)
            if not p.exists():
                logger.warning(
                    "[#{}] excel_hero {}: ref {} файл {} не существует",
                    project.id, ch.id, rid, p,
                )
                continue
            ref_paths.append(p)
        if not ref_paths:
            # Все ссылки «одобрены», но файлы пропали (wipe / old/) —
            # один ERROR и выход; while True в _run_excel остановится по status.
            logger.error(
                "[#{}] excel_hero {}: нет файлов рефов {} — откат frames_ready "
                "(не крутим batch-цикл)",
                project.id, ch.id, ch.ref_ids,
            )
            project.status = ProjectStatus.frames_ready
            await session.flush()
            try:
                await bot.send_message(
                    settings.telegram_owner_chat_id,
                    f"🚫 Проект #{project.id} excel-hero {ch.id}: "
                    f"референс-файлы для {ch.ref_ids} пропали с диска. "
                    "Перегенери референсы.",
                )
            except Exception:  # noqa: BLE001
                logger.warning("[#{}] не удалось отправить TG", project.id)
            return

    # Стиль (общий для проекта — выбирается в обычном hero-flow).
    hero_style_content = _read_hero_style(project) or ""
    style_chosen = (
        (getattr(project, "prompt_overrides", None) or {}).get("hero_style")
        or "default"
    )

    out_dir = project.data_dir / "characters"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{ch.id}.png"
    short_uuid = uuid.uuid4().hex[:8]
    prompt_id_prefix = f"[ID: P{project.id}-EXCEL-{ch.id}-{short_uuid}]"

    http_primary = _excel_hero_http_primary()
    async with _optional_browser_session(need_cdp=not http_primary) as bs:
        gpt = get_gpt_client()
        outsee = OutseeBot(bs) if bs is not None else None
        if http_primary:
            logger.info(
                "[#{}] excel_hero {}: HTTP image API (без CDP)",
                project.id,
                ch.id,
            )

        # Сборка промта.
        from app.services.excel_characters import is_polluted_character_field

        if is_polluted_character_field(ch.name):
            parent_name = ""
            for p in chars:
                if p.id in ch.ref_ids and not is_polluted_character_field(p.name):
                    parent_name = p.name
                    break
            old_name = ch.name
            if parent_name:
                ch.name = f"{parent_name} (вариация {ch.id})"
            else:
                ch.name = f"Персонаж {ch.id}"
            logger.warning(
                "[#{}] excel_hero {}: авто-исцеление служебного имени {!r} -> {!r}",
                project.id,
                ch.id,
                old_name,
                ch.name,
            )

        if used_refs:
            # Реф = картинка (CDP attach) + короткий текст. Style передаём компактно (до 300 симв),
            # чтобы сохранить визуальную эстетику и не сбить reference.
            from app.services.excel_characters import build_ref_variation_sheet_prompt

            style_for_ref = (hero_style_content or "").strip()[:300]
            prompt_text = build_ref_variation_sheet_prompt(ch, style=style_for_ref)
            # Дополнительно усиливаем identity lock.
            prompt_text = (
                "CRITICAL: The attached reference image is the SAME person. "
                "Copy face, skull, mustache/hair, age, body proportions exactly. "
                "Only apply the listed Changes; do not invent a new character.\n\n"
                + prompt_text
            )
            logger.info(
                "[#{}] excel_hero {}: REF mode refs={} files={} prompt_len={}",
                project.id,
                ch.id,
                ch.ref_ids,
                [str(p) for p in ref_paths],
                len(prompt_text),
            )
        else:
            # Base-персонаж: слот hero = turnaround sheet (не агент реестра).
            gtb.assert_hero_master_is_sheet(project)
            hero_template = gtb.get_effective_text(project, "hero")
            brief = ch.brief_for_gpt()
            hero_ask = gtb.render_hero_text(
                hero_template, brief=brief, hero_style=hero_style_content,
            )
            last_reply = ""
            prompt_text = ""
            for attempt in range(1, 4):
                ask = hero_ask
                if (
                    attempt > 1
                    and last_reply
                    and len(last_reply) > OUTSEE_PROMPT_MAX_CHARS
                ):
                    ask = (
                        f"Прошлый ответ был {len(last_reply)} символов — "
                        f"больше лимита {OUTSEE_PROMPT_MAX_CHARS}. Сожми до "
                        f"≤{OUTSEE_PROMPT_MAX_CHARS} символов: убери повторы, "
                        "оставь самое важное. Структуру сохрани. Верни "
                        "ТОЛЬКО новый текст промта.\n\n"
                        "Прошлый промт:\n\n" + last_reply
                    )
                reply = await gpt.ask_fresh(ask, timeout=600, project_id=project.id)
                last_reply = reply or ""
                logger.info(
                    "[#{}] excel_hero {} GPT attempt {}: {} симв",
                    project.id, ch.id, attempt, len(last_reply),
                )
                if not last_reply or len(last_reply) < 100:
                    continue
                prompt_text = last_reply.strip()
                if len(prompt_text) <= OUTSEE_PROMPT_MAX_CHARS:
                    break
            if not prompt_text:
                raise RuntimeError(
                    f"ChatGPT не вернул заполненный промт для excel "
                    f"персонажа {ch.id} после 3 попыток"
                )

        # Генератор / разрешение — с ноды hero; aspect/Relax как в обычном hero.
        from app.services.vibecode_catalog import resolve_node_media_settings

        media = resolve_node_media_settings(project, node_type="hero")
        img_gid = media["image_generator_id"]
        img_gen = IMAGE_GENERATORS_BY_ID.get(img_gid)
        ir = IMAGE_RESOLUTIONS_BY_ID.get(media["resolution_id"])
        quality_slug = media["quality_slug"]

        from app.services.img_streams import acquire_image_slot

        result = None
        # regenerate_image — только CDP UI; при HTTP делаем свежий generate.
        if not used_refs and is_regen and outsee is not None:
            try:
                async with acquire_image_slot():
                    result = await outsee.regenerate_image(out_path)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[#{}] excel_hero {} «Повторить» упала ({}), fresh "
                    "generate", project.id, ch.id, e,
                )
                result = None

        if result is None:
            try:
                async with acquire_image_slot():
                    result = await generate_image_with_retries(
                        outsee, gpt,
                        prompt=prompt_text,
                        out_path=out_path,
                        max_attempts_per_prompt=3,
                        gpt_rewrite=not used_refs,
                        aspect_ratio=HERO_ASPECT_RATIO,
                        model_slug=img_gen.outsee_slug if img_gen else None,
                        resolution=ir.outsee_slug if ir else None,
                        quality=quality_slug,
                        relax=HERO_RELAX,
                        prompt_id_prefix=prompt_id_prefix,
                        reference_image=(ref_paths or None) if used_refs else None,
                        timeout=600,
                        project_id=project.id,
                    )
            except OutseeImageError as e:
                is_moderation = isinstance(e, OutseeContentRejectedError)
                logger.error(
                    "[#{}] excel_hero {} RAN OUT попыток: {}",
                    project.id, ch.id,
                    e.reason if hasattr(e, "reason") else str(e),
                )
                project.status = ProjectStatus.frames_ready
                await session.flush()
                project_label = (project.topic or project.slug or "")[:60]
                if is_moderation:
                    msg = (
                        f"🚫 Проект #{project.id} «{project_label}» "
                        f"excel-hero {ch.id}: 6 попыток в outsee отклонены "
                        f"модерацией.\n"
                        f"Поправь описание персонажа в листе «Персонажи» "
                        f"(R3-R7 столбца {ch.id}) и тыкни «4. Hero» снова."
                    )
                else:
                    msg = (
                        f"🚫 Проект #{project.id} excel-hero {ch.id}: "
                        f"outsee провалился ({(getattr(e, 'reason', None) or str(e))[:300]}).\n"
                        f"Статус откатил в <b>frames_ready</b>."
                    )
                try:
                    await bot.send_message(
                        settings.telegram_owner_chat_id,
                        msg[:3800],
                        parse_mode="HTML",
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "[#{}] не удалось отправить TG-ошибку", project.id
                    )
                return

    file_path = Path(result.file_path)
    art_meta: dict = {
        "excel_id": ch.id,
        "excel_ref_ids": list(ch.ref_ids),
        "excel_prompt_name": ch.prompt_name,
        "excel_used_refs": used_refs,
    }
    if not used_refs:
        art_meta["hero_prompt"] = prompt_text
    art = Artifact(
        project_id=project.id,
        kind=ArtifactKind.hero_reference,
        uuid=uuid.uuid4().hex,
        path=str(file_path),
        meta=art_meta,
    )
    session.add(art)
    # Без HITL: всегда продолжаем batch/wave в _run_excel.
    project.status = ProjectStatus.generating_hero
    await session.flush()
    generated_now = await _excel_ids_with_artifact(session, project)
    remaining_n = sum(1 for c in chars if c.id not in generated_now)
    logger.info(
        "[#{}] excel_hero batch: {} → {} (осталось {}, style={}, "
        "batch_auto={}, approved={})",
        project.id,
        ch.id,
        file_path.name,
        remaining_n,
        style_chosen,
        batch_auto,
        len(approved),
    )
