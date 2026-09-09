"""outsee.io — генерация картинок (nano-banana-2) и видео (veo-3-fast Relax)
через автоматизацию браузера (Playwright поверх CDP).

⚠️ Селекторы UI outsee.io не задокументированы публично. На момент написания
этого файла мы ещё не провели DOM-рекон. Поэтому здесь скелет с несколькими
разумными кандидатами селекторов; при первом запуске их почти наверняка
придётся скорректировать, увидев реальный DOM. Для этого есть режим разведки:
    python -m app.bots.outsee recon-image "тестовый промт"
    python -m app.bots.outsee recon-video "тестовый промт" /путь/к/картинке.png

Он откроет страницу, подождёт, и выведет в лог подходящие элементы
(textarea, button, input[type=file]) с их селекторами — чтобы быстро их
закрепить в этом файле.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeoutError

from app.bots.browser import BrowserSession, browser_session
from app.settings import settings

ERRORS_LOG_PATH = Path("logs/errors.log")

OUTSEE_LOGIN_URL_MARKERS: tuple[str, ...] = (
    "/login",
    "/sign-in",
    "/signin",
    "/auth",
)

OUTSEE_LOGIN_PAGE_MARKERS: tuple[str, ...] = (
    "sign in",
    "log in",
    "войти",
    "вход в аккаунт",
    "войдите",
    "email",
    "пароль",
    "password",
)


def _outsee_queue_mode() -> bool:
    """Вариант A: одна генерация Outsee, первая новая картинка после baseline."""
    return bool(getattr(settings, "outsee_queue_mode", True))


def outsee_login_url(url: str) -> bool:
    u = (url or "").lower()
    return any(m in u for m in OUTSEE_LOGIN_URL_MARKERS)


def outsee_login_page_text(text: str) -> bool:
    hay = (text or "").lower()
    if not hay:
        return False
    if outsee_login_url(hay):
        return True
    has_pw = "password" in hay or "парол" in hay
    has_login = any(m in hay for m in ("sign in", "log in", "войти", "вход"))
    if has_pw and has_login:
        return True
    return any(m in hay for m in OUTSEE_LOGIN_PAGE_MARKERS)


def _log_outsee_error(*, kind: str, text: str, node: str = "outsee") -> None:
    try:
        ERRORS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().isoformat(timespec="seconds")
        line = f"{ts}\tbot=outsee\tnode={node}\tkind={kind}\t{text}"
        with ERRORS_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.warning("outsee: cannot write {}: {}", ERRORS_LOG_PATH, e)


def _outsee_download_timeout_s() -> float:
    return float(getattr(settings, "outsee_download_timeout_s", 120.0))


_CARD_SEARCH_POLL_STEP_S = 0.45
_CARD_SEARCH_DEADLINE_S = 12.0


def _log_download_stage(
    *,
    stage: str,
    duration_s: float,
    strategy: str,
    media: str = "file",
    project_id: int | None = None,
    extra: str = "",
) -> None:
    pid = project_id if project_id is not None else "?"
    detail = (
        f"media={media}\tstage={stage}\tduration_s={duration_s:.2f}"
        f"\tstrategy={strategy}"
    )
    if extra:
        detail += f"\t{extra}"
    _log_outsee_error(kind="download_stage", text=detail, node=f"project={pid}")


async def _update_download_progress(
    project_id: int | None,
    progress_text: str | None,
) -> None:
    if project_id is None:
        return
    try:
        from app.db import session_scope
        from app.models import Project
        from app.services.run_sync import update_active_node_progress_text

        async with session_scope() as session:
            project = await session.get(Project, project_id)
            if project is not None:
                await update_active_node_progress_text(
                    session, project, progress_text
                )
    except Exception as e:  # noqa: BLE001
        logger.warning("outsee: cannot update download progress: {}", e)


async def _poll_gallery_card(
    find_fn,
    *,
    deadline_s: float = _CARD_SEARCH_DEADLINE_S,
    poll_step_s: float = _CARD_SEARCH_POLL_STEP_S,
    project_id: int | None = None,
):
    from app.services.step_cancel import abort_if_cancelled, sleep_cancellable

    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < deadline_s:
        abort_if_cancelled(project_id)
        card = await find_fn()
        if card is not None:
            return card
        await sleep_cancellable(poll_step_s, project_id)
    return None


async def _collect_visible_alert_snippets(page: Page, *, limit: int = 5) -> list[str]:
    try:
        raw = await page.evaluate(
            """(limit) => {
                const out = [];
                const seen = new Set();
                const isVis = (el) => {
                    const cs = window.getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const roles = ['alert', 'status', 'dialog'];
                for (const role of roles) {
                    for (const el of document.querySelectorAll(`[role="${role}"]`)) {
                        if (!isVis(el)) continue;
                        const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                        if (t.length < 6 || seen.has(t)) continue;
                        seen.add(t);
                        out.push(t.slice(0, 240));
                    }
                }
                for (const el of document.querySelectorAll(
                    '[class*="error" i], [class*="alert" i], [class*="toast" i], [data-testid*="error" i]'
                )) {
                    if (!isVis(el)) continue;
                    const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                    if (t.length < 6 || t.length > 500 || seen.has(t)) continue;
                    seen.add(t);
                    out.push(t.slice(0, 240));
                }
                return out.slice(0, limit);
            }""",
            limit,
        )
        return [s for s in (raw or []) if isinstance(s, str) and s.strip()]
    except Exception:  # noqa: BLE001
        return []


def _outsee_timeout_message(base: str, alerts: list[str]) -> str:
    if not alerts:
        return base
    joined = " | ".join(alerts[:3])
    return f"{base}. Плашки на странице: {joined}"


# Сколько последних (верхних) thumb'ов в галерее перебираем по [ID: …].
# Outsee рендерит новые сверху — slice(0, N) = N самых свежих.
_GALLERY_ID_SCAN_LIMIT = 80

_GENERATE_BUTTON_WAIT_SEC = 90.0


# Порядок попыток — первый сработавший используется
PROMPT_INPUT_SELECTORS = [
    "textarea[placeholder*='prompt' i]",
    "textarea[placeholder*='промпт' i]",
    "textarea[placeholder*='промт' i]",
    "textarea[placeholder*='опис' i]",
    "textarea[name='prompt']",
    "textarea[data-testid='prompt']",
    "textarea",
    "div[contenteditable='true']",
]

GENERATE_BUTTON_SELECTORS = [
    # :has-text() — регистроНЕзависимый частичный матч.
    # Добавляем :not(:has-text('Режим')):not(:has-text('Безлимит'))
    # чтобы не матчить Relax-кнопку «Relax Режим / может генерировать».
    "button:has-text('Генерировать'):not([disabled]):not(:has-text('Режим')):not(:has-text('Безлимит'))",
    "button:has-text('Сгенерировать'):not([disabled])",
    "button:has-text('Создать'):not([disabled]):not(:has-text('Режим'))",
    "button:has-text('Generate'):not([disabled]):not(:has-text('Mode'))",
    "button:has-text('Генерировать'):not(:has-text('Режим')):not(:has-text('Безлимит'))",
    "button:has-text('Генерация'):not(:has-text('Режим'))",
    "button:has-text('Сгенерировать')",
    "button:has-text('Создать'):not(:has-text('Режим'))",
    "button:has-text('Generate'):not(:has-text('Mode'))",
    "button:has-text('Run')",
    "button[data-testid='generate']",
]

ASPECT_9_16_SELECTORS = [
    "button:has-text('9:16')",
    "[data-value='9:16']",
    "[aria-label='9:16']",
    "input[value='9:16']",
    # На outsee «Соотношение» отображается как кликабельный блок, внутри
    # которого текст «9:16». Ищем родителя через :has().
    "*:has(> :text-is('9:16'))",
]


async def _dump_page(
    page: Any, label: str, *, max_html_chars: int = 400_000
) -> tuple[Path | None, Path | None]:
    """Сохраняет полный outerHTML и скриншот страницы в
    `<settings.data_dir>/outsee_dumps/<label>_<ts>.{html,png}`.
    Возвращает (html_path, png_path); элементы могут быть None если что-то
    не получилось.

    Используется для отладки селекторов на outsee.io: при ненайденной
    кнопке (aspect / relax / generate / др.) дампим страницу — потом
    оркестратор присылает файл в TG, и можно подобрать селектор.
    """
    from datetime import datetime as _dt

    dumps_dir = Path(settings.data_dir) / "outsee_dumps"
    dumps_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.utcnow().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in label)
    html_path = dumps_dir / f"{safe}_{ts}.html"
    png_path = dumps_dir / f"{safe}_{ts}.png"
    html_ok: Path | None = None
    png_ok: Path | None = None
    try:
        html = await page.content()
        if html and len(html) > max_html_chars:
            html = html[: max_html_chars // 2] + "\n<!-- ...truncated... -->\n" + html[-max_html_chars // 2 :]
        html_path.write_text(html or "", encoding="utf-8")
        html_ok = html_path
    except Exception as e:  # noqa: BLE001
        logger.warning("outsee dump html failed for {}: {}", label, e)
    try:
        await page.screenshot(path=str(png_path), full_page=True, timeout=10_000)
        png_ok = png_path
    except Exception as e:  # noqa: BLE001
        logger.warning("outsee dump png failed for {}: {}", label, e)
    if html_ok or png_ok:
        logger.info(
            "outsee dump '{}': html={} png={}",
            label,
            html_ok and html_ok.name,
            png_ok and png_ok.name,
        )
    return html_ok, png_ok


def _aspect_selectors(ratio: str) -> list[str]:
    """Набор CSS-селекторов для кнопки выбора aspect ratio.

    Работает для любого формата `W:H` — 1:1, 16:9, 9:16, 4:3, 3:4, 2:3,
    3:2, 21:9. На outsee.io это могут быть button / div role=radio / div
    role=button / label с текстом внутри. Чем точнее селектор — тем
    меньше шансов попасть в текст «9:16» внутри hint-подписи где-то
    ещё на странице.
    """
    return [
        # Самые точные — кнопки/радио с текстом ровно ratio.
        f"button:has-text('{ratio}')",
        f"[role='radio']:has-text('{ratio}')",
        f"[role='button']:has-text('{ratio}')",
        f"label:has-text('{ratio}')",
        f"[data-value='{ratio}']",
        f"[aria-label='{ratio}']",
        f"input[value='{ratio}']",
        # Менее точно — любой родитель с дочерним :text-is.
        f"*:has(> :text-is('{ratio}'))",
    ]


# Селекторы dropdown-кнопки «Соотношение …» (открывает выбор aspect).
# В outsee.io это <button>, у которого внутри лежит <span>«Соотношение»</span>
# и <span>текущее_значение</span>.
ASPECT_DROPDOWN_OPENER_SELECTORS: list[str] = [
    "button:has(span:text-is('Соотношение'))",
    "button:has-text('Соотношение')",
    "[role='button']:has-text('Соотношение')",
]


def _aspect_option_selectors(ratio: str) -> list[str]:
    """Селекторы пункта со значением aspect ratio в открывшемся
    dropdown-списке. Текст пункта может быть ровно W:H."""
    return [
        f"[role='option']:text-is('{ratio}')",
        f"[role='menuitem']:text-is('{ratio}')",
        f"[role='radio']:text-is('{ratio}')",
        f"button:text-is('{ratio}')",
        f"li:text-is('{ratio}')",
        f"div[role]:text-is('{ratio}')",
        f"span:text-is('{ratio}')",
        # Любой кликабельный родитель, у которого ровно ratio в дочернем span.
        f"button:has(> span:text-is('{ratio}'))",
        f"li:has(span:text-is('{ratio}'))",
    ]


async def _is_aspect_selected(page: Any, sel: str) -> bool | None:
    """Проверка, выбран ли вариант aspect-ratio после клика. None — не
    смогли определить (тогда не уверены)."""
    try:
        loc = page.locator(sel).first
        for attr, want in (
            ("aria-checked", "true"),
            ("aria-pressed", "true"),
            ("data-state", "checked"),
            ("data-state", "active"),
            ("data-state", "selected"),
        ):
            try:
                v = await loc.get_attribute(attr, timeout=200)
                if v is not None:
                    if str(v).lower() == want:
                        return True
            except Exception:  # noqa: BLE001
                continue
        # Класс с маркером "selected" / "active" / "checked".
        try:
            cls = await loc.get_attribute("class", timeout=200) or ""
            cls_low = cls.lower()
            for marker in ("selected", "active", "checked", "is-active"):
                if marker in cls_low:
                    return True
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        return None
    return None


async def _select_aspect_ratio(
    page: Any, ratio: str, *, where: str = "image",
    dumps: list[Path] | None = None,
    project_id: int | None = None,
) -> bool:
    """Выбирает aspect ratio в outsee.io. Поддерживает 2 типа UI:

    1) **Dropdown-кнопка «Соотношение …»** (новый UI 2026): в правой
       панели лежит <button> с двумя <span>: «Соотношение» + текущее
       значение. Клик открывает попап со списком вариантов; кликаем
       нужный.
    2) **Прямая кнопка/радио с текстом ratio** (старый UI / fallback).

    Если кнопка не найдена — дампит страницу в outsee_dumps/ и (если
    передан dumps-список) добавляет туда пути файлов; вызывающий код
    может потом отправить их в TG."""
    from app.services.step_cancel import sleep_cancellable

    from app.services.step_cancel import abort_if_cancelled

    abort_if_cancelled(project_id)

    # 1) Сначала пробуем NEW UI: dropdown «Соотношение N:M».
    opener_sel = await _first_visible(
        page, ASPECT_DROPDOWN_OPENER_SELECTORS, timeout_ms=2_000, project_id=project_id
    )
    if opener_sel:
        try:
            opener = page.locator(opener_sel).first
            # Если в кнопке уже стоит нужное значение — ничего не делаем.
            try:
                cur_text = (await opener.inner_text(timeout=1_000)) or ""
            except Exception:  # noqa: BLE001
                cur_text = ""
            if ratio in cur_text:
                logger.info(
                    "outsee.{}: aspect {} уже выбран в dropdown ({})",
                    where, ratio, cur_text.strip().replace("\n", " ")[:80],
                )
                return True
            try:
                await opener.scroll_into_view_if_needed(timeout=1_500)
            except Exception:  # noqa: BLE001
                pass
            await opener.click(timeout=3_000)
            logger.info(
                "outsee.{}: aspect dropdown открыт (был '{}', хочу '{}')",
                where, cur_text.strip().replace("\n", " ")[:60], ratio,
            )
            await sleep_cancellable(0.3, project_id)
            opt_sel = await _first_visible(
                page, _aspect_option_selectors(ratio), timeout_ms=4_000, project_id=project_id
            )
            if opt_sel:
                try:
                    await page.locator(opt_sel).first.click(timeout=3_000)
                    logger.info(
                        "outsee.{}: aspect {} — выбран в dropdown ({})",
                        where, ratio, opt_sel,
                    )
                    await sleep_cancellable(0.3, project_id)
                    return True
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "outsee.{}: aspect {} клик в dropdown упал: {} ({})",
                        where, ratio, e, opt_sel,
                    )
            else:
                logger.warning(
                    "outsee.{}: aspect dropdown открыт, но опция '{}' "
                    "не найдена",
                    where, ratio,
                )
                # Пытаемся закрыть dropdown (Escape), чтобы не мешал.
                try:
                    await page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "outsee.{}: dropdown «Соотношение» поломался: {}",
                where, e,
            )

    # 2) Fallback: ищем прямую кнопку/радио с текстом ratio (старый UI).
    sel = await _first_visible(page, _aspect_selectors(ratio), timeout_ms=4_000, project_id=project_id)
    if not sel:
        logger.warning(
            "outsee.{}: aspect {} — ни dropdown «Соотношение», ни "
            "прямая кнопка не найдены",
            where, ratio,
        )
        h, p = await _dump_page(page, f"aspect_{ratio.replace(':', 'x')}_notfound")
        if dumps is not None:
            for x in (h, p):
                if x:
                    dumps.append(x)
        return False
    try:
        loc = page.locator(sel).first
        try:
            await loc.scroll_into_view_if_needed(timeout=2_000)
        except Exception:  # noqa: BLE001
            pass
        await loc.click(timeout=3_000)
        logger.info(
            "outsee.{}: aspect {} — клик ({})", where, ratio, sel
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "outsee.{}: aspect {} клик упал: {} (sel={})",
            where, ratio, e, sel,
        )
        return False

    await sleep_cancellable(0.3, project_id)
    ok = await _is_aspect_selected(page, sel)
    if ok is True:
        logger.info(
            "outsee.{}: aspect {} подтверждён выбран (sel={})",
            where, ratio, sel,
        )
        return True
    try:
        sel2 = await _first_visible(
            page, _aspect_selectors(ratio), timeout_ms=2_000, project_id=project_id
        )
        if sel2 and sel2 != sel:
            await page.locator(sel2).first.click(timeout=2_000)
            logger.info(
                "outsee.{}: aspect {} — повторный клик по другому селектору ({})",
                where, ratio, sel2,
            )
    except Exception:  # noqa: BLE001
        pass
    return True


def _resolution_selectors(resolution: str) -> list[str]:
    """Селекторы для кнопки 1K / 2K / 3K / 4K (картинка) или 720p / 1080p (видео)."""
    return [
        f"button:has-text('{resolution}')",
        f"button:text-is('{resolution}')",
        f"[data-value='{resolution}']",
        f"[aria-label='{resolution}']",
        f"*:has(> :text-is('{resolution}'))",
    ]


def _quality_selectors(quality_label: str) -> list[str]:
    """Селекторы «Детализация»: Низкое/Среднее/Высокое (+ data-value low/medium/high)."""
    from app.generation_options import IMAGE_QUALITY_DOM_VALUE

    value = IMAGE_QUALITY_DOM_VALUE.get(quality_label, "")
    sels = [
        f"button:has-text('{quality_label}')",
        f"button:text-is('{quality_label}')",
        f"[aria-label='{quality_label}']",
        f"*:has(> :text-is('{quality_label}'))",
    ]
    if value:
        sels = [
            f"button[data-value='{value}']",
            f"[data-value='{value}']",
            *sels,
        ]
    return sels


# Кнопка/тогл «Relax» (для всех картиночных моделей и для veo-3-1-fast).
# В outsee.io это просто кнопка/тогл с текстом «Relax».
RELAX_SELECTORS: list[str] = [
    "button:has-text('Relax')",
    "[role='switch']:has-text('Relax')",
    "label:has-text('Relax')",
    "[aria-label='Relax']",
    "[data-value='relax']",
    "*:has(> :text-is('Relax'))",
]

# Селекторы тогла «Безлимит» — на outsee.io это и есть Relax (юзер
# подтвердил, что «Безлимит» = Relax-режим = включается при relax=True).
# bg-primary = тогл ВКЛ (Relax включён). bg-gray-*/без bg-primary = ВЫКЛ.
LIMIT_TOGGLE_SELECTORS: list[str] = [
    "button:has(span:text-is('Безлимит'))",
    "button:has-text('Безлимит')",
    "[role='switch']:has-text('Безлимит')",
]


async def _read_limit_toggle_on(page: Any, sel: str) -> bool | None:
    """True если тогл «Безлимит» включён, False если выключен,
    None если не смогли определить."""
    try:
        loc = page.locator(sel).first
        try:
            cls = await loc.locator("div.rounded-full").first.get_attribute(
                "class", timeout=500
            ) or ""
            cls_low = cls.lower()
            if "bg-primary" in cls_low:
                return True
            if "bg-gray" in cls_low or "bg-zinc" in cls_low:
                return False
        except Exception:  # noqa: BLE001
            pass
        # Запасной способ — позиция «шарика» (left-[18px] = ON).
        try:
            ball_cls = await loc.locator("div.absolute").first.get_attribute(
                "class", timeout=300
            ) or ""
            ball_low = ball_cls.lower()
            if "left-[18px]" in ball_low:
                return True
            if "left-[2px]" in ball_low:
                return False
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        return None
    return None


async def _toggle_relax(
    page: Any, *, want_on: bool, where: str = "image",
    dumps: list[Path] | None = None,
    project_id: int | None = None,
) -> None:
    """Best-effort: ставит тогл Relax в нужное состояние.

    На outsee.io 2026 года тогл называется «Безлимит» — и это И ЕСТЬ
    Relax-режим (юзер подтвердил):
      Relax=ON  ⇔ Безлимит=ON
      Relax=OFF ⇔ Безлимит=OFF

    Если тогл не нашёлся — пробуем старые «Relax»-селекторы. Если совсем
    нет — тихо выходим (модель его не поддерживает). Если want_on=True и
    кнопку не нашли — дампим страницу для отладки.
    """
    # 1) Сначала пробуем NEW UI: «Безлимит».
    limit_sel = await _first_visible(
        page, LIMIT_TOGGLE_SELECTORS, timeout_ms=1_500, project_id=project_id
    )
    if limit_sel:
        try:
            current_on = await _read_limit_toggle_on(page, limit_sel)
            # Семантика: relax want_on == True ⇔ Безлимит должно быть ON.
            desired_limit_on = want_on
            if current_on is desired_limit_on:
                logger.info(
                    "outsee.{}: Relax {} — Безлимит уже {} (тогл не трогаем)",
                    where, "ON" if want_on else "OFF",
                    "OFF" if desired_limit_on is False else "ON",
                )
                return
            if current_on is None:
                logger.info(
                    "outsee.{}: Relax {} — состояние «Безлимит» неизвестно, "
                    "кликаю (physical)",
                    where, "ON" if want_on else "OFF",
                )
            loc = page.locator(limit_sel).first
            await _physical_mouse_click(
                page, loc, project_id=project_id, label=f"{where} Безлимит"
            )
            await asyncio.sleep(0.45)
            after = await _read_limit_toggle_on(page, limit_sel)
            logger.info(
                "outsee.{}: Безлимит physical click → want={}, read={}",
                where,
                want_on,
                after,
            )
            if after == want_on:
                return
            # Повтор CDP по bbox
            box = await loc.bounding_box()
            if box:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                await _cdp_dispatch_click(
                    page, cx, cy, project_id=project_id
                )
                await asyncio.sleep(0.45)
                if await _read_limit_toggle_on(page, limit_sel) == want_on:
                    return
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "outsee.{}: тогл «Безлимит» поломался: {}", where, e,
            )

    # 2) Fallback: старые «Relax»-селекторы.
    sel = await _first_visible(page, RELAX_SELECTORS, timeout_ms=2_000, project_id=project_id)
    if not sel:
        if want_on:
            logger.warning(
                "outsee.{}: Relax=on запрошен, но ни тогл «Безлимит», ни "
                "кнопка «Relax» не найдены",
                where,
            )
            h, p = await _dump_page(page, "relax_notfound")
            if dumps is not None:
                for x in (h, p):
                    if x:
                        dumps.append(x)
        return
    try:
        loc = page.locator(sel).first
        state: str | None = None
        for attr in ("aria-checked", "aria-pressed", "data-state"):
            try:
                v = await loc.get_attribute(attr, timeout=500)
                if v is not None:
                    state = str(v).lower()
                    break
            except Exception:  # noqa: BLE001
                continue
        is_on: bool | None = None
        if state in ("true", "on", "checked"):
            is_on = True
        elif state in ("false", "off", "unchecked"):
            is_on = False
        if want_on and is_on is True:
            logger.info("outsee.{}: Relax уже включён, пропускаем клик", where)
            return
        if not want_on and is_on is False:
            logger.info("outsee.{}: Relax уже выключен, пропускаем клик", where)
            return
        if not want_on and is_on is None:
            logger.info(
                "outsee.{}: Relax=off запрошен, но состояние неизвестно — не трогаем",
                where,
            )
            return
        await _physical_mouse_click(
            page, loc, project_id=project_id, label=f"{where} Relax"
        )
        logger.info(
            "outsee.{}: Relax physical {} (sel={})",
            where,
            "ON" if want_on else "OFF",
            sel,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("outsee.{}: Relax toggle упал: {}", where, e)


async def _scan_toggle_targets(
    page: Page, *, keywords: list[str]
) -> list[dict[str, Any]]:
    try:
        raw = await page.evaluate(
            """(keys) => {
                const out = [];
                for (const el of document.querySelectorAll(
                    'button, [role="switch"], label, [role="button"]'
                )) {
                    const text = (el.innerText || el.textContent || '').trim();
                    const low = text.toLowerCase();
                    if (!keys.some(k => low.includes(k))) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 24 || r.height < 14) continue;
                    const cs = getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                    out.push({
                        text: text.slice(0, 48),
                        cx: Math.round(r.x + r.width / 2),
                        cy: Math.round(r.y + r.height / 2),
                        area: Math.round(r.width * r.height),
                    });
                }
                out.sort((a, b) => b.area - a.area);
                return out;
            }""",
            [k.lower() for k in keywords],
        )
        return list(raw or [])
    except Exception:  # noqa: BLE001
        return []


def _image_page_url(model_slug: str | None) -> str:
    """Строит URL страницы outsee.io/image для нужной модели."""
    base = settings.outsee_image_url
    if not model_slug:
        return base
    # Если в settings.outsee_image_url уже есть `?model=...`, заменяем его
    # на выбранный slug. Иначе добавляем.
    if "?model=" in base:
        head = base.split("?model=")[0]
        return f"{head}?model={model_slug}"
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}model={model_slug}"


def _video_page_url(model_slug: str | None) -> str:
    base = settings.outsee_video_url
    if not model_slug:
        return base
    if "?model=" in base:
        head = base.split("?model=")[0]
        return f"{head}?model={model_slug}"
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}model={model_slug}"

FILE_UPLOAD_SELECTORS = [
    "input[type='file']",
]

# Селекторы для скачивания результата. Покрываем два случая:
#   - result картинка отображается как <img src=...>
#   - видео отображается как <video src=...> или есть кнопка download
RESULT_IMAGE_SELECTORS = [
    "img[data-testid='result']",
    "img[alt*='result' i]",
    "[role='img'] img",
    "main img",
]
RESULT_VIDEO_SELECTORS = [
    "video source",
    "video",
    "a[download][href*='.mp4']",
]
DOWNLOAD_BUTTON_SELECTORS = [
    "button[aria-label='Download']",
    "button:has-text('Download')",
    "button:has-text('Скачать')",
    "a[download]",
]


@dataclass
class GenerationResult:
    """Итог генерации."""
    file_path: Path
    raw_url: str | None = None
    gen_id: str | None = None  # uuid v4, привязан к одной попытке (для трейсинга)
    # Пути к dump-файлам (html/png) если по ходу генерации не нашлась
    # какая-то кнопка (aspect/relax/etc.). Оркестратор отправляет их
    # в TG для отладки селекторов.
    dumps: list[Path] | None = None

    @property
    def image_path(self) -> Path:
        """Совместимость для шагов, обращающихся к image_path."""
        return self.file_path


class OutseeImageError(RuntimeError):
    """Ошибка с описательным контекстом — пайплайн использует это,
    чтобы запостить понятную ошибку в Telegram, а не системный traceback."""

    def __init__(
        self,
        reason: str,
        *,
        context: dict[str, Any] | None = None,
        dumps: list[Path] | None = None,
    ) -> None:
        self.reason = reason
        self.context = dict(context or {})
        # html/png дампы страницы для отладки селекторов outsee.io.
        self.dumps: list[Path] = list(dumps or [])
        super().__init__(self.format_text())

    def format_text(self) -> str:
        lines = [self.reason]
        for k, v in self.context.items():
            s = str(v)
            if len(s) > 200:
                s = s[:200] + "…"
            lines.append(f"  {k}: {s}")
        return "\n".join(lines)


async def _check_outsee_session(page: Page) -> None:
    url = page.url or ""
    if outsee_login_url(url):
        msg = "outsee: слетела сессия — нужно перелогиниться"
        _log_outsee_error(kind="session_lost", text=msg)
        raise OutseeImageError(msg, context={"kind": "session_lost"})
    try:
        body_text = await page.evaluate(
            "() => (document.body && document.body.innerText) || ''"
        )
        has_pw_input = await page.evaluate(
            "() => !!document.querySelector('input[type=password]')"
        )
    except Exception:  # noqa: BLE001
        return
    if has_pw_input or outsee_login_page_text(body_text):
        msg = "outsee: слетела сессия — нужно перелогиниться"
        _log_outsee_error(kind="session_lost", text=msg)
        raise OutseeImageError(msg, context={"kind": "session_lost"})


class OutseeContentRejectedError(OutseeImageError):
    """Outsee показал плашку «Контент отклонён» (модерация запрещённых
    слов в промте). Отдельный класс, чтобы caller мог решить — ретраить
    с тем же промтом или просить GPT переписать его без триггеров.

    Сама `OutseeImageError` остаётся базовым классом, поэтому весь
    существующий error-handling в caller'ах продолжит работать без правок."""


class OutseePromptTooLongError(OutseeImageError):
    """Промт длиннее лимита outsee или обрезан textarea — GPT-сжатие, не rewrite."""


class OutseeDownloadError(OutseeImageError):
    """URL картинки/ролика уже есть, скачивание не удалось — не нужен новый Generate."""


class OutseeDuplicateVideoError(OutseeImageError):
    """Скачанный ролик совпадает с уже имеющимся — нужен другой URL или перегенерация."""


# Маркеры видимых плашек ошибок outsee (см. `_detect_outsee_failure`).
_OUTSEE_LENGTH_MARKERS: tuple[str, ...] = (
    "промпт слишком длинный",
    "промт слишком длинный",
    "слишком длин",
    "too long",
    "too many character",
    "превышает",
    "maximum length",
    "max length",
    "максимум символ",
    "лимит символ",
    "character limit",
    "prompt is too",
    "промт слишком",
    "промпт слишком",
)
_OUTSEE_MODERATION_MARKERS: tuple[str, ...] = (
    "контент отклон",
    "content reject",
    "не прошёл модер",
    "не прошел модер",
    "не прошла модер",
    "аудиодорожка видео не прошла",
    "аудиодорожка не прошла",
    "содержит запрещ",
    # «запрещённый контент» — да; голое «запрещено» в промте — нет (см. looks_like_prompt_body).
    "запрещённ",
    "запрещенн",
    "запрещённый контент",
    "запрещенный контент",
    # НЕ «запрещен/запрещено» сами по себе: в промтах часто
    # «запрещено добавлять персонажей» — ложный abort до download.
    "forbidden word",
    "текстовый запрос содержит",
    "некорректный текстовый",
    "нарушает правила",
    "нарушает политику",
    "policy violation",
    "moderation",
    "отклонён",
    "отклонен",
    "не прошёл модерацию модели",
    "не прошел модерацию модели",
    "попробуйте изменить описание",
    "попробуйте переформулировать",
)
_OUTSEE_GENERATION_ERROR_MARKERS: tuple[str, ...] = (
    "ошибка генера",
    "произошла ошибка",
    "ошибка veo",
    "ошибка kling",
    "ошибка сервиса генерации",
    "ошибка сервера",
    "не удалось сгенер",
    "не удалось создать",
    "generation failed",
    "failed to generate",
    "failed to download",
    "something went wrong",
    "что-то пошло не так",
    "попробуйте снова",
    "повторите попытку",
    "try again",
    "unable to generate",
    "отказ в генерации",
    "генерация отклон",
    "сетевая ошибка",
    "network error",
    "networkerror",
)

# Не ошибка генерации: слот безлимита занят — ЖДАТЬ завершения, не fail/GPT-rewrite.
# Лог: «Безлимитная генерация уже активна. Дождитесь завершения…»
_OUTSEE_BUSY_MARKERS: tuple[str, ...] = (
    "безлимит занят",
    "безлимитная генерация уже активна",
    "дождитесь завершения или отключите безлимит",
    "unlimited generation is already active",
    "wait for it to finish",
)


def _outsee_failure_kind(text: str) -> str:
    """`moderation` | `length` | `busy` | `generation` | `unknown`."""
    low = text.lower()
    # Модерация важнее: outsee явно пишет «запрещённое» — не путаем с длиной.
    for m in _OUTSEE_MODERATION_MARKERS:
        if m in low:
            return "moderation"
    for m in _OUTSEE_LENGTH_MARKERS:
        if m in low:
            return "length"
    for m in _OUTSEE_BUSY_MARKERS:
        if m in low:
            return "busy"
    for m in _OUTSEE_GENERATION_ERROR_MARKERS:
        if m in low:
            return "generation"
    return "unknown"


def outsee_error_is_moderation(err: OutseeImageError) -> bool:
    """True если outsee отклонил промт по модерации (не длина, не busy)."""
    if isinstance(err, OutseeContentRejectedError):
        return True
    ctx = err.context or {}
    if ctx.get("kind") == "moderation" or ctx.get("ui_kind") == "moderation":
        return True
    failure = str(ctx.get("failure") or "")
    if failure and _outsee_failure_kind(failure) == "moderation":
        return True
    if _outsee_failure_kind(err.reason or "") == "moderation":
        return True
    return False


def outsee_error_kind(err: OutseeImageError) -> str:
    """Класс ошибки для логов/TG: length | moderation | prompt_fill | generation | other."""
    if isinstance(err, OutseePromptTooLongError):
        return "length"
    if isinstance(err, OutseeContentRejectedError):
        return "moderation"
    ctx = err.context or {}
    if ctx.get("error_kind") in (
        "length",
        "moderation",
        "generation",
        "prompt_fill",
    ):
        return str(ctx["error_kind"])
    reason = (err.reason or "").lower()
    if any(
        m in reason
        for m in (
            "лимит outsee",
            "промт обрезан",
            "не удалось сжать",
            "gpt недоступен для сжатия",
        )
    ):
        return "length"
    if any(m in reason for m in ("не попал в поле", "id промта не найден")):
        return "prompt_fill"
    if ctx.get("kind") == "moderation":
        return "moderation"
    if ctx.get("kind") == "generation":
        return "generation"
    return "other"


def outsee_error_kind_label(kind: str) -> str:
    return {
        "length": "лимит символов",
        "moderation": "модерация",
        "prompt_fill": "промт не попал в поле",
        "generation": "ошибка генерации",
        "other": "ошибка outsee",
    }.get(kind, kind)


def _normalize_outsee_failure_text(text: str) -> str:
    return " ".join(text.split()).strip().lower()[:300]


_MAX_ACTIVE_FAILURE_CHARS = 260


def _prompt_id_core_token(prompt_id_prefix: str | None) -> str | None:
    """P17-F90-dda7487c из `[ID: P17-F90-dda7487c r1a2]`."""
    if not prompt_id_prefix:
        return None
    m = re.search(r"P\d+-F\d+-[a-f0-9]+", prompt_id_prefix, re.I)
    return m.group(0) if m else None


def _failure_text_matches_prompt_id(
    text: str, prompt_id_prefix: str | None
) -> bool:
    """Ошибка относится к ЭТОЙ генерации, а не к чужой карточке в очереди."""
    core = _prompt_id_core_token(prompt_id_prefix)
    if not core:
        return True
    low = text.lower()
    if core.lower() in low:
        return True
    return f"[id: {core.lower()}" in low or f"[id:{core.lower()}" in low


def _fail_fast_while_generate_disabled(failure_text: str) -> bool:
    """True — Generate disabled + moderation/length: нечего кликать.

    Если кнопка уже активна, плашку игнорируем: кликаем Generate, а отказ
    ловим после клика (см. pre_hit baseline в generate_image).
    """
    return _outsee_failure_kind(failure_text) in ("moderation", "length")


def _normalize_pre_failure_baseline(
    text: str | None,
    *,
    prompt_id_prefix: str | None = None,
) -> str | None:
    """Игнорируем мусор «Ошибка» (6 симв) до клика Generate — иначе
    живую модерацию в queue не считаем новой."""
    if not text:
        return None
    t = " ".join(text.split()).strip()
    if not t:
        return None
    if _outsee_failure_kind(t) == "moderation":
        if not _failure_text_matches_prompt_id(t, prompt_id_prefix):
            return None
        return t
    norm = _normalize_outsee_failure_text(t)
    if norm in ("ошибка", "error") or len(t) < 12:
        return None
    return t


def _outsee_failure_looks_like_prompt_body(text: str) -> bool:
    """Ложное срабатывание: в плашку попал текст промта, а не ошибка outsee."""
    t = " ".join(text.split()).strip().lower()
    if not t:
        return False
    if t.startswith("--no ") or t.startswith("-- no "):
        return True
    if t.startswith("no text,") or t.startswith("no text "):
        return True
    if "subtitles" in t and "watermarks" in t and "captions" in t:
        return True
    if len(t) > 120 and " duplicated" in t and "logos" in t:
        return True
    # Промт монтажа: «…запрещено добавлять…» / хвост UI «…кадреВидео».
    # Без маркеров «отклонён/модерация/запрещённый контент» это не плашка Outsee.
    real_mod = (
        "контент отклон" in t
        or "отклонён" in t
        or "отклонен" in t
        or "модерац" in t
        or "содержит запрещ" in t
        or "запрещённ" in t
        or "запрещенн" in t
        or "policy violation" in t
        or "content reject" in t
    )
    if real_mod:
        return False
    if "запрещено" in t or "кадревидео" in t.replace(" ", ""):
        return True
    if t.endswith("видео") and "отклон" not in t and len(t) > 40:
        return True
    return False


def _outsee_failure_text_is_noise(text: str) -> bool:
    """Карточки истории outsee (Veo+ID) — не live-плашка. Модерацию не режем."""
    t = " ".join(text.split()).strip()
    if _outsee_failure_looks_like_prompt_body(t):
        return True
    if _outsee_failure_kind(t) == "moderation":
        return False
    if len(t) > _MAX_ACTIVE_FAILURE_CHARS:
        return True
    if re.search(r"\[ID:\s*P\d+-F\d+", t, re.I) and re.search(r"veo", t, re.I):
        return True
    if re.match(r"Ошибка\s*Veo", t, re.I):
        return True
    return False


def _outsee_failure_is_stale(
    ftext: str,
    *,
    baseline_failure_texts: frozenset[str],
    in_result: bool,
    elapsed: float,
    stale_non_result_sec: float = 15.0,
    gen_idle: bool = False,
    queue_mode: bool = False,
    prompt_id_prefix: str | None = None,
    card_scoped: bool = False,
) -> bool:
    """Плашка от прошлой генерации / другого кадра — не ронять текущую."""
    if _outsee_failure_text_is_noise(ftext):
        return True
    kind = _outsee_failure_kind(ftext)
    # «Безлимит уже активна» = слот занят, генерация идёт — ЖДАТЬ, не fail.
    # Иначе abort на 3-й сек → нет download/replace кадра (лог P47-F18).
    if kind == "busy":
        return True
    result_kinds = ("moderation", "generation", "length")
    id_match = card_scoped or bool(
        queue_mode
        and prompt_id_prefix
        and _failure_text_matches_prompt_id(ftext, prompt_id_prefix)
    )
    if queue_mode and prompt_id_prefix and not id_match:
        # Video result often shows prompt text without `[ID: P…-F…]` token.
        if not (in_result and kind in result_kinds):
            return True
    norm = _normalize_outsee_failure_text(ftext)
    # Generate уже завершился, ролика нет — «Контент отклонён» в результате
    # это провал ТЕКУЩЕЙ попытки, даже если такая же плашка была до клика.
    if in_result and kind in result_kinds and norm not in baseline_failure_texts:
        if kind == "moderation" and elapsed >= 4.0:
            return False
        if gen_idle and elapsed >= 20.0:
            return False
        # Outsee часто оставляет Generate disabled после отказа — не ждём idle.
        if kind in result_kinds and elapsed >= 6.0:
            return False
    if norm in baseline_failure_texts:
        return True
    # Плашка вне блока результата при активной генерации — шум из сайдбара.
    if not in_result:
        min_sidebar_sec = stale_non_result_sec
        if id_match:
            min_sidebar_sec = 4.0
        if elapsed < min_sidebar_sec:
            return True
        # Очередь: отказ по НАШЕМУ prompt_id — fail-fast, даже если Generate ещё disabled.
        if id_match and kind in result_kinds:
            return False
        if not gen_idle:
            return True
        if norm in ("ошибка", "error"):
            return True
    return False


_VIDEO_DOWNLOAD_ATTEMPTS = 3
_VIDEO_PICK_ATTEMPTS = 4


def _raise_outsee_failure(
    *,
    text: str,
    gen_id: str,
    elapsed: float,
    in_result: bool,
    prompt_len: int | None = None,
) -> None:
    from app.generation_options import OUTSEE_PROMPT_MAX_CHARS

    ui_kind = _outsee_failure_kind(text)
    kind = ui_kind
    ctx: dict[str, object] = {
        "gen_id": gen_id,
        "failure": text[:200],
        "elapsed_sec": round(elapsed, 1),
        "in_result_panel": in_result,
        "kind": kind,
        "ui_kind": ui_kind,
        "error_kind": kind,
    }
    if prompt_len is not None:
        ctx["prompt_len"] = prompt_len
        ctx["limit"] = OUTSEE_PROMPT_MAX_CHARS
    if kind == "length":
        n = prompt_len if prompt_len is not None else 0
        raise OutseePromptTooLongError(
            f"outsee: промт {n} симв — лимит outsee {OUTSEE_PROMPT_MAX_CHARS}",
            context=ctx,
        )
    if kind == "moderation":
        detail = (text or "").strip().replace("\n", " ")[:120]
        msg = (
            "outsee image: контент отклонён модерацией"
            + (f" ({detail})" if detail else "")
        )
        _log_outsee_error(kind="moderation", text=msg)
        raise OutseeContentRejectedError(msg, context=ctx)
    msg = "outsee image: ошибка генерации на outsee.io"
    _log_outsee_error(kind=kind, text=f"{msg}: {text[:120]}")
    raise OutseeImageError(msg, context=ctx)


# Минимум «настоящей» картинки из nano-banana — обычно 300 KB – 5 MB.
# Thumb/preview outsee ~50–100 KB; placeholder/skeleton ещё меньше.
_MIN_IMAGE_BYTES = 200_000

# Magic-байты файловых форматов, которые реально может вернуть nano-banana.
# Используется в `_validate_downloaded_image` для отсева HTML-страниц,
# error-pages и SVG-плейсхолдеров.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_RIFF_MAGIC = b"RIFF"
_WEBP_TAG = b"WEBP"


def _validate_downloaded_image(
    out_path: Path, *, gen_id: str, img_url: str
) -> None:
    """Проверяет, что скачанный файл — настоящая картинка от nano-banana,
    а не placeholder/skeleton/error-page.

    Бывает, что `_wait_image_url_strict` возвращает URL outsee-плейсхолдера
    (тёмный фон с тремя белыми квадратами — outsee показывает его, пока
    идёт генерация), и `_download_via_context` сохраняет этот мусор как
    «результат». Бот потом отправляет это в TG, и пользователь видит
    placeholder вместо реальной картинки.

    Проверки:
      1) размер файла >= `_MIN_IMAGE_BYTES` (50 KB) — placeholder/skeleton
         сжимается в единицы KB, реальная nano-banana картинка 300 KB+;
      2) magic-байты PNG/JPEG/WebP — отсекает HTML-страницы и SVG.

    На любую неудачу — удаляем «битый» файл (чтобы случайно не
    отправился в TG) и кидаем `OutseeDownloadError`. Retry-обёртка
    (`outsee_retry.generate_image_with_retries`) делает download-only
    повтор без нового Generate — картинка уже есть на outsee.
    """
    try:
        size = out_path.stat().st_size
    except OSError as e:
        raise OutseeDownloadError(
            "outsee image: скачанный файл недоступен после download",
            context={
                "gen_id": gen_id,
                "img_url": img_url,
                "err": f"{type(e).__name__}: {e}",
            },
        ) from e

    # UI-пресеты (freepreset/gptimage2.webp) — никогда не кадр монтажа.
    # _UI_ASSET_MARKERS объявлен ниже в модуле; читаем на вызове.
    low_url = (img_url or "").lower()
    if low_url and any(m in low_url for m in _UI_ASSET_MARKERS):  # noqa: F821
        try:  # noqa: SIM105
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise OutseeDownloadError(
            "outsee image: URL — UI-пресет/пример, не результат генерации",
            context={
                "gen_id": gen_id,
                "img_url": img_url,
                "size_bytes": size,
            },
        )

    # Раньше отклоняли любой thumb-URL даже после успешного browser-Download
    # полного PNG → файл удалялся, деньги за генерацию сгорали.
    # Решение: судим по байтам файла. Thumb-URL при полноценном файле — только WARN.
    if _is_outsee_thumb_url(img_url) and size < _MIN_IMAGE_BYTES:
        try:  # noqa: SIM105
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise OutseeDownloadError(
            "outsee image: скачан thumb вместо full PNG — не принимаем",
            context={
                "gen_id": gen_id,
                "img_url": img_url,
                "size_bytes": size,
            },
        )
    if _is_outsee_thumb_url(img_url) and size >= _MIN_IMAGE_BYTES:
        logger.warning(
            "outsee image: wait вернул thumb URL, но файл полный ({} B) — "
            "принимаем (gen_id={})",
            size,
            gen_id[:8] if gen_id else "—",
        )

    if size < _MIN_IMAGE_BYTES:
        try:  # noqa: SIM105
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise OutseeDownloadError(
            "outsee image: скачанный файл слишком мал — похоже на "
            "placeholder/skeleton, а не реальную генерацию",
            context={
                "gen_id": gen_id,
                "img_url": img_url,
                "size_bytes": size,
                "min_bytes": _MIN_IMAGE_BYTES,
            },
        )

    try:
        with out_path.open("rb") as f:
            head = f.read(16)
    except OSError as e:
        raise OutseeDownloadError(
            "outsee image: не удалось прочитать заголовок скачанного файла",
            context={
                "gen_id": gen_id,
                "img_url": img_url,
                "err": f"{type(e).__name__}: {e}",
            },
        ) from e

    is_png = head.startswith(_PNG_MAGIC)
    is_jpeg = head.startswith(_JPEG_MAGIC)
    is_webp = head[:4] == _RIFF_MAGIC and head[8:12] == _WEBP_TAG
    if not (is_png or is_jpeg or is_webp):
        try:  # noqa: SIM105
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise OutseeDownloadError(
            "outsee image: скачанный файл не выглядит как PNG/JPEG/WebP "
            "(возможно, error-page или SVG-плейсхолдер)",
            context={
                "gen_id": gen_id,
                "img_url": img_url,
                "size_bytes": size,
                "head_hex": head.hex(),
            },
        )


# Пути, по которым точно не лежат результаты генерации.
#
# `videomobilepreview/topaz.webp` — outsee.io кладёт сюда статус-плашку
# «идёт обработка через Topaz» (тёмный фон + три белых квадрата
# в виде loading-анимации). Если её НЕ отфильтровать на уровне URL,
# `_wait_image_url_strict` радостно вернёт этот URL как «новая
# картинка в DOM» (она и правда новая), валидатор размера её
# не ловит (webp-анимация ~80КБ), и она сохраняется как hero.png.
# Юзер потом видит лоадер вместо персонажа.
_UI_ASSET_MARKERS = (
    "/_next/",
    "/static/",
    "/assets/",
    "/icons/",
    "/logo",
    "favicon",
    "sprite",
    "/videomobilepreview/",
    "topaz.webp",
    "/preview/loader",
    "/skeleton",
    # Пресеты/примеры на outsee.io — НЕ результат генерации.
    # Recover d3 ошибочно хватал gptimage2.webp и затирал кадр.
    "/videoexamples/",
    "/freepreset/",
    "gptimage2.webp",
    "/examples/",
    "/placeholder",
)

# Маркеры путей/имён, которые соответствуют ВЫБРАННОМУ ПОЛЬЗОВАТЕЛЕМ
# референсу (то, что мы только что загрузили в input[type=file]) либо
# его превью-копии. Эти URL outsee возвращает в DOM как «вот ваш инпут»
# через несколько секунд после клика Generate, и без этого фильтра мы
# их ошибочно принимаем за результат генерации (см. v=3-баг: бот сохранил
# /temp-images/3787/input_*.png вместо реального результата).
_INPUT_REF_MARKERS = (
    "/temp-images/",
    "/input_",
    "/uploads/",
    "/upload/",
)


def _strip_url_query(url: str | None) -> str:
    """Снимает `?query` и `#fragment`, оставляет scheme+host+path.

    Outsee.io хранит thumb'ы в Yandex Cloud Storage с подписанными URL
    `…image_X.jpg?X-Amz-Algorithm=...&X-Amz-Signature=…`. Подписи
    перевыпускаются на каждом ререндере страницы, поэтому одна и та же
    галерейная картинка в baseline и в DOM после Generate имеет РАЗНЫЕ
    URL-строки — без нормализации все галерейные thumb'ы фолсли
    помечались «новыми» и `clean[-1]/clean[0]` уносил случайную старую.

    Stable-идентификатор картинки = host+path, потому что
    `image_<ts>_<idx>_thumb.jpg` уникален на стороне outsee и не меняется
    между перевыпусками подписи.
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:  # noqa: BLE001
        # На совсем странных URL'ах оставляем как есть.
        i = url.find("?")
        if i >= 0:
            url = url[:i]
        i = url.find("#")
        if i >= 0:
            url = url[:i]
        return url


_OUTSEE_IMAGE_KEY_RE = re.compile(r"(image_\d+_\d+)", re.I)


def _outsee_image_stable_key(url: str | None) -> str:
    """Стабильный ключ картинки outsee: `image_<ts>_<idx>`."""
    if not url:
        return ""
    path = _strip_url_query(url)
    name = Path(path).name
    m = _OUTSEE_IMAGE_KEY_RE.search(name)
    if m:
        return m.group(1).lower()
    base = re.sub(r"_thumb", "", name, flags=re.I)
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base.lower()


def _is_outsee_thumb_url(url: str | None) -> bool:
    if not url:
        return False
    return "_thumb" in _strip_url_query(url).lower()


# Outsee отдаёт thumb и full PNG с разных CDN-хостов (см. логи:
# thumb → storage.yandexcloud.net/outseehistory/…_thumb.jpg,
# full  → outseehistory.storage.yandexcloud.net/…_0.png).
_OUTSEE_CDN_BASES: tuple[str, ...] = (
    "https://outseehistory.storage.yandexcloud.net/",
    "https://storage.yandexcloud.net/outseehistory/",
)
_OUTSEE_GENERATED_PATH_RE = re.compile(
    r"(generated/\d+/\d+)/(image_\d+_\d+)", re.I
)


def _png_basename_from_thumb_filename(name: str) -> str | None:
    """`image_1780279147074_0_thumb.jpg` → `image_1780279147074_0.png`.

    Нельзя делать `{key}_0.png` по regex-ключу: ключ уже `…_0`, иначе
    получается битый `…_0_0.png` (404), как в логе frame 8.
    """
    if "_thumb" not in name.lower():
        return None
    return re.sub(r"_thumb\.(jpe?g|webp|png)$", ".png", name, flags=re.I)


def _guess_full_png_url_from_thumb(url: str) -> str | None:
    """Outsee CDN: thumb → full PNG (тот же basename, без `_thumb`)."""
    candidates = _all_full_png_url_candidates(url)
    return candidates[0] if candidates else None


def _all_full_png_url_candidates(url: str) -> list[str]:
    """Все варианты full-PNG URL для thumb/handoff (оба CDN-хоста).

    ВАЖНО (SigV4): НЕ копируем `?X-Amz-Signature=…` с thumb.jpg на .png.
    Подпись AWS/Yandex привязана к Canonical URI (конкретному path объекта).
    Подпись для `…_thumb.jpg` на `…_0.png` → CDN 403 всегда. Раньше
    «сохранение подписи» создавало ложное чувство фикса, а скачивание
    по-прежнему падало. Рабочие пути:
      1) реальный full PNG URL из DOM / network (своя подпись),
      2) клик «Скачать» (expect_download),
      3) unsigned guess (редко, если CDN пускает по cookie — обычно нет).
    """
    if not url:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str | None) -> None:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    path = _strip_url_query(url)
    name = Path(path).name
    png_name = _png_basename_from_thumb_filename(name)
    if not png_name:
        # Уже full PNG (не thumb) — сохраняем СОБСТВЕННУЮ подпись URL.
        if path.lower().endswith(".png"):
            _add(url)
            if url != path:
                _add(path)
        return out

    dir_part = path[: path.rfind("/") + 1] if "/" in path else ""
    same_host = f"{dir_part}{png_name}" if dir_part else png_name
    _add(same_host)

    gm = _OUTSEE_GENERATED_PATH_RE.search(path)
    if gm:
        gen_dir = gm.group(1) + "/"
        for host_base in _OUTSEE_CDN_BASES:
            _add(f"{host_base}{gen_dir}{png_name}")

    return out


def _url_download_priority(url: str) -> tuple[int, int]:
    """Меньше = лучше кандidate для скачивания (full PNG предпочтительнее thumb)."""
    low = _strip_url_query(url).lower()
    score = 0
    if "_thumb" in low:
        score += 100
    if low.endswith((".jpg", ".jpeg")):
        score += 40
    if low.endswith(".png"):
        score -= 30
    if "outseehistory.storage.yandexcloud.net" in low:
        score -= 5  # full PNG чаще на этом хосте, не на storage/…/outseehistory
    # Подписанный URL переживает CDN auth — важнее голого пути.
    if "x-amz-signature" in url.lower() or "signature=" in url.lower():
        score -= 20
    return (score, len(low))


def _resolve_best_download_url(
    primary: str,
    *,
    net_events: list[tuple[float, str]] | None = None,
    extra_urls: list[str] | None = None,
) -> str:
    """Из thumb DOM URL выбирает полноразмерный PNG из net_events/DOM."""
    if not primary:
        return primary
    key = _outsee_image_stable_key(primary)
    pool: list[str] = [primary]
    pool.extend(_all_full_png_url_candidates(primary))
    if net_events:
        pool.extend(u for _, u in net_events)
    if extra_urls:
        pool.extend(extra_urls)

    candidates: list[str] = []
    seen: set[str] = set()
    for u in pool:
        if not u or u in seen:
            continue
        low = u.lower()
        if any(m in low for m in _UI_ASSET_MARKERS):
            continue
        if any(m in low for m in _INPUT_REF_MARKERS):
            continue
        if key and _outsee_image_stable_key(u) != key:
            continue
        seen.add(u)
        candidates.append(u)

    if not candidates:
        return primary
    best = min(candidates, key=_url_download_priority)
    if best != primary and _is_outsee_thumb_url(primary):
        logger.info(
            "outsee url-resolve: thumb {} → full {}",
            primary[:100],
            best[:100],
        )
    return best


def _collect_download_url_candidates(
    primary: str,
    *,
    net_events: list[tuple[float, str]] | None = None,
    extra_urls: list[str] | None = None,
) -> list[str]:
    """Упорядоченный список URL для fallback-скачивания (full PNG первым)."""
    key = _outsee_image_stable_key(primary)
    pool: list[str] = []
    if primary:
        pool.append(primary)
    pool.append(_resolve_best_download_url(primary, net_events=net_events, extra_urls=extra_urls))
    if net_events:
        pool.extend(u for _, u in net_events)
    if extra_urls:
        pool.extend(extra_urls)

    ordered: list[str] = []
    seen: set[str] = set()
    keyed: list[str] = []
    for u in pool:
        if not u or u in seen:
            continue
        if key and _outsee_image_stable_key(u) == key:
            keyed.append(u)
            seen.add(u)
    keyed.sort(key=_url_download_priority)
    ordered.extend(keyed)
    for u in pool:
        if u and u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


async def _find_full_png_in_dom(page: Page, stable_key: str) -> str | None:
    if not stable_key:
        return None
    try:
        urls: list[str] = await page.evaluate(
            """() => Array.from(document.querySelectorAll('img'))
                .map(i => i.currentSrc || i.src)
                .filter(Boolean)"""
        )
    except Exception:  # noqa: BLE001
        return None
    matches = [
        u
        for u in urls
        if stable_key in _strip_url_query(u).lower()
        and "_thumb" not in u.lower()
        and ".png" in u.lower()
    ]
    if not matches:
        return None
    return min(matches, key=_url_download_priority)


def _url_is_fresh(
    url: str | None, net_events: list[tuple[float, str]] | None
) -> bool:
    """Returns True iff `url` actually came over the network in
    `net_events` (list of (offset_sec, url) tuples) AFTER the Generate
    click. Used by `_wait_image_url_strict` to filter out stale
    cached/history images that appear in the DOM without a real
    network load.

    Semantics:
      - `net_events is None` → caller didn't opt in; return True (legacy
        behaviour, keeps backwards compat for any other future caller).
      - `net_events == []` → caller opted in, but no candidate image
        responses have arrived yet — return False (we'll re-check in
        the next loop iteration).
      - `net_events` non-empty → return True iff `url` matches one of
        the events. Matching: exact string OR host+path equality
        (strips query strings/fragments).
    """
    if not url:
        return False
    if net_events is None:
        return True
    if not net_events:
        return False
    fresh_urls = {u for _, u in net_events}
    if url in fresh_urls:
        return True
    try:
        from urllib.parse import urlparse

        target = urlparse(url)
        for u in fresh_urls:
            try:
                p = urlparse(u)
            except Exception:  # noqa: BLE001
                continue
            if p.netloc == target.netloc and p.path == target.path:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _is_candidate_image_response(resp: Any) -> bool:
    """Подходит ли сетевой ответ под «вероятно, это результат nano-banana»:
    image/* (не svg/ico), не UI-ассет, тело ≥ 50 KB."""
    try:
        url = resp.url or ""
        ct = (resp.headers.get("content-type") or "").lower()
        if not ct.startswith("image/"):
            return False
        if ct in ("image/svg+xml", "image/x-icon", "image/vnd.microsoft.icon"):
            return False
        low = url.lower()
        if any(marker in low for marker in _UI_ASSET_MARKERS):
            return False
        if any(marker in low for marker in _INPUT_REF_MARKERS):
            # Это URL ВХОДНОГО референса (тот файл, который мы только что
            # загрузили), а не результат генерации. Не считаем кандидатом.
            return False
        # Content-Length — дешёвый способ отсечь мелочь без .body()
        cl = resp.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) < _MIN_IMAGE_BYTES:
                    return False
            except ValueError:
                pass
        return True
    except Exception:  # noqa: BLE001
        return False


# Минимум «настоящего» mp4 от veo — обычно сотни KB+.
_MIN_VIDEO_BYTES = 80_000


def _validate_downloaded_video(
    out_path: Path, *, gen_id: str, video_url: str
) -> None:
    try:
        size = out_path.stat().st_size
    except OSError as e:
        raise OutseeDownloadError(
            "outsee video: скачанный файл недоступен после download",
            context={"gen_id": gen_id, "video_url": video_url},
        ) from e
    if size < _MIN_VIDEO_BYTES:
        with contextlib.suppress(OSError):
            out_path.unlink(missing_ok=True)
        raise OutseeDownloadError(
            "outsee video: слишком маленький mp4 (placeholder?)",
            context={"gen_id": gen_id, "video_url": video_url, "bytes": size},
        )
    with out_path.open("rb") as fh:
        head = fh.read(12)
    if len(head) < 8 or head[4:8] != b"ftyp":
        with contextlib.suppress(OSError):
            out_path.unlink(missing_ok=True)
        raise OutseeDownloadError(
            "outsee video: файл не похож на mp4",
            context={"gen_id": gen_id, "video_url": video_url},
        )


def _first_fresh_video_url(
    urls: list[str],
    *,
    rejected: set[str],
) -> str | None:
    for u in urls:
        if _strip_url_query(u) not in rejected:
            return u
    return None


def _is_candidate_video_response(resp: Any) -> bool:
    """Сетевой ответ похож на результат veo: video/* или mp4, не UI-ассет."""
    try:
        url = resp.url or ""
        ct = (resp.headers.get("content-type") or "").lower()
        low = url.lower()
        is_video_ct = ct.startswith("video/")
        is_mp4_url = ".mp4" in low or "/video/" in low
        if not is_video_ct and not is_mp4_url:
            return False
        if any(marker in low for marker in _UI_ASSET_MARKERS):
            return False
        if any(marker in low for marker in _INPUT_REF_MARKERS):
            return False
        cl = resp.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) < _MIN_VIDEO_BYTES:
                    return False
            except ValueError:
                pass
        return True
    except Exception:  # noqa: BLE001
        return False


def _video_url_looks_like_result(url: str | None) -> bool:
    if not url or url.startswith("data:"):
        return False
    low = url.lower()
    if any(marker in low for marker in _UI_ASSET_MARKERS):
        return False
    if any(marker in low for marker in _INPUT_REF_MARKERS):
        return False
    return True


async def _first_visible(
    page: Page, selectors: list[str], *, timeout_ms: int = 15_000, project_id: int | None = None
) -> str | None:
    """Возвращает CSS-селектор с уже вставленным `:nth-match(sel, N)`, который
    гарантированно попадает в первый ВИДИМЫЙ элемент. Страницы outsee часто
    рендерят 2–3 копии одного textarea (desktop + mobile + sidebar), и
    locator(sel).first может ткнуть в скрытую."""
    from app.services.step_cancel import abort_if_cancelled, sleep_cancellable

    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    while asyncio.get_event_loop().time() < deadline:
        abort_if_cancelled(project_id)
        if selectors is PROMPT_INPUT_SELECTORS:
            await _check_outsee_session(page)
        for sel in selectors:
            try:
                base = page.locator(sel)
                count = await base.count()
                if count == 0:
                    continue
                for i in range(min(count, 8)):
                    loc = base.nth(i)
                    try:
                        if await loc.is_visible():
                            # Вернём Playwright-селектор `:nth-match(X, N+1)`
                            return f":nth-match({sel}, {i + 1})"
                    except Exception:  # noqa: BLE001
                        # input[type=file] обычно hidden, но валиден
                        if "file" in sel:
                            return f":nth-match({sel}, {i + 1})"
                        continue
            except Exception:  # noqa: BLE001
                continue
        await sleep_cancellable(0.3, project_id)
    return None


class OutseeBot:
    def __init__(self, session: BrowserSession) -> None:
        self.session = session

    async def _try_http_image(self, **kwargs: Any) -> GenerationResult | None:
        """Outsee Developer API (Bearer OUTSEE_API_KEY). None = fallback CDP UI."""
        from app.bots import outsee_http as oh
        from app.settings import settings as st

        if not oh.outsee_http_enabled():
            return None
        try:
            return await oh.generate_image(**kwargs)
        except oh.NanoBananaProOutseeBannedError:
            raise
        except oh.OutseeHttpAuthError as e:
            if not getattr(st, "outsee_http_fallback_cdp", True):
                raise
            logger.warning("outsee API auth → CDP UI: {}", e.reason)
            return None
        except oh.OutseeHttpError as e:
            if not getattr(st, "outsee_http_fallback_cdp", True):
                raise
            logger.warning("outsee API fail → CDP UI: {}", e.reason)
            return None

    async def _try_http_video(self, **kwargs: Any) -> GenerationResult | None:
        from app.bots import outsee_http as oh
        from app.settings import settings as st

        if not oh.outsee_http_enabled():
            return None
        try:
            return await oh.generate_video(**kwargs)
        except oh.OutseeHttpAuthError as e:
            if not getattr(st, "outsee_http_fallback_cdp", True):
                raise
            logger.warning("outsee API video auth → CDP UI: {}", e.reason)
            return None
        except oh.OutseeHttpError as e:
            if not getattr(st, "outsee_http_fallback_cdp", True):
                raise
            logger.warning("outsee API video fail → CDP UI: {}", e.reason)
            return None

    async def generate_music(
        self,
        prompt: str,
        out_path: Path,
        *,
        title: str | None = None,
        timeout: float = 900,
        model_slug: str | None = "suno-5-5",
        instrumental: bool = False,
        prompt_id_prefix: str | None = None,
        project_id: int | None = None,
    ) -> GenerationResult:
        """Audio: Developer API не поддерживает — только CDP/пайплайн."""
        del prompt, out_path, title, timeout, model_slug, instrumental
        del prompt_id_prefix, project_id
        from app.bots.outsee_http import OutseeApiError

        raise OutseeApiError(
            "Outsee Developer API не поддерживает audio — "
            "используй шаг audio (ElevenLabs/Suno) или CDP UI",
            context={"hint": "нет /api/v1/audio"},
        )

    # ----- IMAGE (nano-banana-2) -----

    async def generate_image(
        self,
        prompt: str,
        out_path: Path,
        *,
        aspect_ratio: str = "9:16",
        timeout: float = 600,
        gen_id: str | None = None,
        model_slug: str | None = None,
        resolution: str | None = None,
        quality: str | None = None,
        relax: bool = False,
        prompt_id_prefix: str | None = None,
        reference_image: Path | list[Path] | None = None,
        project_id: int | None = None,
    ) -> GenerationResult:
        """Генерирует картинку на outsee.io.

        Параметры:
          model_slug      — slug для URL (`?model=<slug>`). Если None —
                            используется settings.outsee_image_url как есть.
          aspect_ratio    — строка-ярлык кнопки («16:9», «9:16»…). Жмём
                            кнопку и проверяем состояние.
          resolution      — строка-ярлык («1K» / «2K» / «4K»). Best-effort клик.
          quality         — строка-ярлык («Низкое» / «Среднее» / «Высокое»).
          relax           — если True и Relax-кнопка есть на странице — включаем.
          reference_image — если передан Path или list[Path] — загружаем
                            картинку(и) как референс(ы) для генерации
                            (через input[type=file] на странице outsee.io).
                            Используется в hero-вариациях (1 ref персонажа)
                            и в шаге 8 «Картинки» (до 2 ref: персонаж +
                            предмет, читаются из xlsx R38/R39 для кадра).
          prompt_id_prefix — строка вида `[ID: P12-F3-a7f2b01c]`. Будет
                             поставлена ПЕРВОЙ строкой промта, чтобы в
                             истории outsee однозначно отличать эту
                             генерацию от всех прошлых.
        """
        import time as _time
        import uuid as _uuid

        from app.services.step_cancel import abort_if_cancelled, await_with_cancel

        abort_if_cancelled(project_id)
        from app.services.media_route import is_nano_banana_pro

        if is_nano_banana_pro(model_slug):
            from app.bots.outsee_http import (
                NANO_BANANA_PRO_OUTSEE_BAN,
                NanoBananaProOutseeBannedError,
            )

            raise NanoBananaProOutseeBannedError(
                NANO_BANANA_PRO_OUTSEE_BAN,
                context={"model": model_slug, "provider": "outsee", "banned": True},
            )
        gen_id = gen_id or _uuid.uuid4().hex

        # 1) HTTP API outsee.io/create (cookies из Chrome /profile)
        refs: list[Path] = []
        if isinstance(reference_image, Path):
            refs = [reference_image]
        elif isinstance(reference_image, list):
            refs = [p for p in reference_image if isinstance(p, Path)]
        http_res = await self._try_http_image(
            prompt=prompt,
            out_path=out_path,
            model_slug=model_slug,
            aspect_ratio=aspect_ratio,
            resolution=resolution or "1K",
            detail_level=quality,
            reference_images=refs or None,
            use_unlimited=bool(relax),
            prompt_id_prefix=prompt_id_prefix,
            timeout=float(timeout),
            gen_id=gen_id,
            project_id=project_id,
        )
        if http_res is not None:
            return http_res

        if prompt_id_prefix:
            from app.generation_options import prepend_gen_id

            prompt = prepend_gen_id(prompt, prompt_id_prefix)
            mode = "queue" if _outsee_queue_mode() else "gallery-id"
            logger.info(
                "outsee.generate_image: prompt_id_prefix={} mode={}",
                prompt_id_prefix,
                mode,
            )
        _verify_prompt_length_before_send(prompt, where="generate_image")

        from app.services.outsee_lane import outsee_lane

        async with outsee_lane(project_id=project_id, op="generate_image"):
            page_url = _image_page_url(model_slug)
            logger.info(
                "outsee.generate_image: открываю страницу gen_id={} url={}",
                gen_id[:8], page_url,
            )
            page = await self.session.open_page(page_url, reuse=True)
            from app.services.step_cancel import (
                register_active_page,
                unregister_active_page,
            )

            if project_id is not None:
                register_active_page(project_id, page)
            try:
                return await self._generate_image_on_page(
                    page,
                    prompt=prompt,
                    out_path=out_path,
                    aspect_ratio=aspect_ratio,
                    timeout=timeout,
                    gen_id=gen_id,
                    model_slug=model_slug,
                    resolution=resolution,
                    quality=quality,
                    relax=relax,
                    prompt_id_prefix=prompt_id_prefix,
                    reference_image=reference_image,
                    project_id=project_id,
                    page_url=page_url,
                )
            finally:
                if project_id is not None:
                    unregister_active_page(project_id)

    async def _generate_image_on_page(
        self,
        page: Any,
        *,
        prompt: str,
        out_path: Path,
        aspect_ratio: str,
        timeout: float,
        gen_id: str,
        model_slug: str | None,
        resolution: str | None,
        quality: str | None,
        relax: bool,
        prompt_id_prefix: str | None,
        reference_image: Path | list[Path] | None,
        project_id: int | None,
        page_url: str,
    ) -> GenerationResult:
        """Тело generate_image — отдельно, чтобы register_active_page в finally."""
        import time as _time
        import uuid as _uuid

        from app.services.step_cancel import abort_if_cancelled, await_with_cancel

        abort_if_cancelled(project_id)
        dumps: list[Path] = []
        try:
            await await_with_cancel(
                page.goto(page_url, wait_until="domcontentloaded"), project_id
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "outsee.generate_image: page.goto({}) упал: {} — продолжаю "
                "без явного reload", page_url, e,
            )
        await await_with_cancel(page.wait_for_load_state("domcontentloaded"), project_id)
        try:
            await await_with_cancel(
                page.wait_for_load_state("networkidle", timeout=15_000), project_id
            )
        except Exception:
            pass
        abort_if_cancelled(project_id)
        logger.info("outsee.generate_image: страница готова, гидрация ok")

        # Снимок «до» — все большие картинки и URL-ы, которые уже на странице.
        # Свежим результатом считаем ТОЛЬКО то, чего тут не было.
        # ВАЖНО: храним baseline как НОРМАЛИЗОВАННЫЕ host+path (без
        # `?X-Amz-Signature=...`). Иначе re-sign на каждом ререндере
        # делает все галерейные thumb'ы «новыми» и бот хватает старую
        # картинку из истории outsee.
        baseline_result_img = _strip_url_query(await self._result_img_src(page))
        baseline_big_imgs = {
            _strip_url_query(u) for u in await self._all_big_imgs(page)
        }
        baseline_dom_srcs = {
            _strip_url_query(u) for u in await self._all_img_srcs(page)
        }
        logger.info(
            "outsee.generate_image: baseline result_img={}, big_imgs={}, all_imgs={}",
            (baseline_result_img[:80] if baseline_result_img else None),
            len(baseline_big_imgs),
            len(baseline_dom_srcs),
        )

        # Сетевой listener — ловит ВСЕ image/* ответы с timestamp.
        # Используется только как мониторинг (для подсказки в ошибке),
        # НО не как fallback для «возьму последнюю» — это и есть тот баг,
        # из-за которого приходили чужие/старые картинки.
        click_ts = _time.monotonic()
        net_events: list[tuple[float, str]] = []  # (ts_offset_from_click, url)

        def _on_response(resp: Any) -> None:
            try:
                if not _is_candidate_image_response(resp):
                    return
                net_events.append((_time.monotonic() - click_ts, resp.url))
            except Exception:  # noqa: BLE001
                pass

        page.on("response", _on_response)

        try:
            pre_rejected_text: str | None = None

            # 1) вбить промт — всегда (как в рабочем TG-боте до anti-dup skip).
            input_sel = await _first_visible(
                page, PROMPT_INPUT_SELECTORS, timeout_ms=60_000, project_id=project_id
            )
            if not input_sel:
                h, p = await _dump_page(page, "prompt_input_notfound")
                for x in (h, p):
                    if x:
                        dumps.append(x)
                raise OutseeImageError(
                    "outsee image: не найден ввод промта",
                    context={"gen_id": gen_id},
                    dumps=dumps,
                )
            logger.info("outsee.generate_image: textarea найдена ({})", input_sel)
            try:
                await await_with_cancel(
                    page.locator(input_sel).first.scroll_into_view_if_needed(
                        timeout=5_000
                    ),
                    project_id,
                )
            except Exception:  # noqa: BLE001
                pass
            await await_with_cancel(page.locator(input_sel).first.click(), project_id)
            await await_with_cancel(page.locator(input_sel).first.fill(prompt), project_id)
            from app.services.sidebar_layout import log_prompt_send

            log_prompt_send(
                bot="outsee",
                project_id=project_id,
                node="generate_image",
                source="prompt",
                text=prompt,
            )
            await _verify_composer_prompt_filled(
                page,
                input_sel,
                expected_prompt=prompt,
                prompt_id_prefix=prompt_id_prefix,
                where="generate_image",
            )
            actual_len = len(await _read_composer_prompt_value(page, input_sel))
            logger.info(
                "outsee.generate_image: промт в поле ввода (отправлено {} симв, "
                "в textarea {} симв)",
                len(prompt),
                actual_len,
            )
            abort_if_cancelled(project_id)

            # НЕ проверяем disabled здесь: для montage correction Generate
            # часто inactive пока не прикреплён reference (текущий кадр).
            # Раньше ранний raise «промт не принят» убивал apply до attach refs.
            # Рабочий путь img-шага: настройки → refs → _wait_button_enabled.

            # 2) выбрать aspect ratio (поддержка любого W:H, с верификацией)
            if aspect_ratio:
                await _select_aspect_ratio(
                    page, aspect_ratio, where="generate_image", dumps=dumps,
                    project_id=project_id,
                )

            # 2.5) выбрать разрешение 1K/2K/3K/4K (best-effort + warn если нет кнопки)
            if resolution:
                res_sel = await _first_visible(
                    page, _resolution_selectors(resolution), timeout_ms=3_000,
                    project_id=project_id,
                )
                if res_sel:
                    try:
                        await await_with_cancel(
                            page.locator(res_sel).first.click(), project_id
                        )
                        logger.info(
                            "outsee.generate_image: {} выбран ({})",
                            resolution, res_sel,
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "resolution {} не кликнулось ({})", resolution, res_sel
                        )
                else:
                    logger.warning(
                        "outsee.generate_image: кнопка разрешения {} не найдена "
                        "(модель может не поддерживать этот размер)",
                        resolution,
                    )

            # 2.6) «Детализация» Низкое/Среднее/Высокое (GPT Image)
            if quality:
                qual_sel = await _first_visible(
                    page, _quality_selectors(quality), timeout_ms=3_000,
                    project_id=project_id,
                )
                if qual_sel:
                    try:
                        await await_with_cancel(
                            page.locator(qual_sel).first.click(), project_id
                        )
                        logger.info(
                            "outsee.generate_image: {} выбран ({})",
                            quality, qual_sel,
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "quality {} не кликнулось ({})", quality, qual_sel
                        )
                else:
                    logger.warning(
                        "outsee.generate_image: кнопка детализации «{}» не найдена",
                        quality,
                    )

            # 2.7) Relax (если попросили)
            await _toggle_relax(
                page, want_on=relax, where="generate_image", dumps=dumps,
                project_id=project_id,
            )
            abort_if_cancelled(project_id)

            # 2.9) Reference-картинка (для hero-вариаций 2..N).
            # На странице outsee.io image обычно есть СКРЫТЫй input[type=file]
            # для подгрузки референса. Обычный _first_visible его НЕ найдёт
            # (видимость=False), поэтому используем робастный хелпер
            # `_attach_ref_image_robust`: он первым делом пытается видимый
            # input, иначе берёт ЛЮБОЙ input[type=file] в DOM и бьёт
            # set_input_files в него (по Playwright он работает на скрытых тоже).
            if reference_image is not None:
                refs: list[Path] = (
                    [reference_image]
                    if isinstance(reference_image, Path)
                    else list(reference_image)
                )
                missing = [p for p in refs if not p.exists()]
                for ref_path in missing:
                    logger.warning(
                        "outsee.generate_image: reference {} не найден на диске",
                        ref_path,
                    )
                attached_n = await self._attach_reference_images_robust(
                    page,
                    [p for p in refs if p.exists()],
                    where="generate_image",
                    project_id=project_id,
                )
                if attached_n < len(refs) - len(missing):
                    h, p = await _dump_page(page, "ref_input_notfound")
                    for x in (h, p):
                        if x:
                            dumps.append(x)

            # 3) кнопка generate
            gen_sel = await _first_visible(
                page, GENERATE_BUTTON_SELECTORS, timeout_ms=10_000, project_id=project_id
            )
            if not gen_sel:
                h, p = await _dump_page(page, "generate_button_notfound")
                for x in (h, p):
                    if x:
                        dumps.append(x)
                raise OutseeImageError(
                    "outsee image: не найдена кнопка Generate",
                    context={"gen_id": gen_id},
                    dumps=dumps,
                )
            logger.info("outsee.generate_image: кнопка Generate найдена ({})", gen_sel)
            await self._wait_button_enabled(
                page, gen_sel, timeout_s=600, project_id=project_id
            )
            abort_if_cancelled(project_id)

            # Re-baseline ПОСЛЕ всех настроек (aspect dropdown, разрешение,
            # Relax, референс) — клики по dropdown вызывают ререндер
            # правой панели и могут «принести» в DOM другую картинку,
            # которую мы иначе ошибочно посчитаем «новым результатом».
            # См. коммент выше про _strip_url_query.
            baseline_result_img = _strip_url_query(
                await self._result_img_src(page)
            )
            baseline_big_imgs = {
                _strip_url_query(u) for u in await self._all_big_imgs(page)
            }
            baseline_dom_srcs = {
                _strip_url_query(u) for u in await self._all_img_srcs(page)
            }
            logger.info(
                "outsee.generate_image: re-baseline перед Generate "
                "result_img={}, big_imgs={}, all_imgs={}",
                (baseline_result_img[:80] if baseline_result_img else None),
                len(baseline_big_imgs),
                len(baseline_dom_srcs),
            )

            # ВАЖНО: после re-baseline нужно повторно исключить URL
            # уже-существующей карточки из baseline. Re-baseline
            # пересобрал множества с нуля, и если карточка дошла
            # до полной отрисовки между initial baseline и re-baseline
            # — её URL снова попадёт в baseline_*. Делаем это ПОСЛЕ
            # re-baseline, чтобы исключение точно сработало.
            # (В ветке `already_in_progress` мы сюда не заходим —
            # re-baseline не делается, baseline_* уже почищены.)

            # Снимок видимой плашки ошибки ДО клика Generate (модерация или
            # «ошибка генерации»). Outsee часто оставляет остаток от прошлой
            # попытки в history/result — передаём в wait, чтобы не путать
            # с НОВОЙ ошибкой, но повтор той же плашки в result-панели после
            # клика всё равно считаем свежим сбоем.
            pre_hit = await self._detect_outsee_failure(
                page,
                queue_mode=_outsee_queue_mode(),
                prompt_id_prefix=prompt_id_prefix,
            )
            pre_rejected_text = _normalize_pre_failure_baseline(
                str(pre_hit["text"]) if pre_hit else None,
                prompt_id_prefix=prompt_id_prefix,
            )
            if pre_rejected_text:
                logger.info(
                    "outsee.generate_image: pre-click failure_text"
                    " обнаружена ({} симв, kind={}) — baseline для детектора",
                    len(pre_rejected_text),
                    _outsee_failure_kind(pre_rejected_text),
                )

            click_ts = _time.monotonic()
            net_events.clear()
            await await_with_cancel(page.locator(gen_sel).first.click(), project_id)
            logger.info(
                "outsee.generate_image: Generate кликнут, жду картинку (gen_id={})",
                    gen_id[:8],
                )

            # 4) строгое ожидание свежей картинки.
            # Передаём prompt_id_prefix — `_wait_image_url_strict` тогда
            # ищет в DOM карточку именно с НАШИМ `[ID: P1-HERO1-V1-…]`
            # и игнорирует все остальные (старые/чужие фото из истории
            # outsee). Это самая строгая верификация — без неё бот мог
            # подхватить чужой/прошлый результат, см. баг с
            # «загрузило старую фотку с другим идентификатором».
            queue_mode = _outsee_queue_mode()
            if queue_mode:
                logger.info(
                    "outsee.generate_image: queue-mode — ждём одну новую "
                    "картинку (ID={}), gen_id={}",
                    "да" if prompt_id_prefix else "нет",
                    gen_id[:8],
                )
            try:
                img_url = await self._wait_image_url_strict(
                    page,
                    timeout=timeout,
                    baseline_result_img=baseline_result_img,
                    baseline_big_imgs=baseline_big_imgs,
                    baseline_all_srcs=baseline_dom_srcs,
                    net_events=net_events,
                    gen_id=gen_id,
                    pre_rejected_text=pre_rejected_text,
                    prompt_id_prefix=prompt_id_prefix,
                    project_id=project_id,
                    queue_mode=queue_mode,
                    prompt_len=len(prompt),
                )
            except (OutseeContentRejectedError, OutseePromptTooLongError) as e:
                # Модерация — дамп НЕ снимаем (caller всё равно его не
                # покажет, см. требование «не слать дампы при ошибках
                # генерации»). Просто пробрасываем дальше — retry-обёртка
                # сама решит: ретраить тот же промт или просить GPT
                # переписать его без триггеров.
                e.dumps = list(dumps)
                raise
            except OutseeImageError as e:
                # При таймауте дампим страницу — пригодится для отладки
                # «почему Generate не запустил генерацию» и подбора селекторов.
                h, p = await _dump_page(page, "image_timeout")
                for x in (h, p):
                    if x:
                        dumps.append(x)
                e.dumps = list(dumps)
                raise
        finally:
            try:
                page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        # 5) скачиваем — ЕДИНЫЙ путь (нода img / montage recover / retry).
        # РАНЬШЕ: prompt_id_prefix (всегда у montage) уводил в gallery/CDN
        # cascade → thumb→unsigned PNG → CDN 403 → файл не сохранялся.
        try:
            img_url = await download_image_like_generate(
                page,
                out_path=out_path,
                img_url=img_url,
                gen_id=gen_id,
                prompt_id_prefix=prompt_id_prefix,
                project_id=project_id,
                net_events=net_events,
                soft_verify=self._verify_img_url_matches_prompt_id,
            )
        except OutseeDownloadError as e:
            e.context.setdefault("gen_id", gen_id)
            e.context.setdefault("img_url", img_url)
            e.dumps = list(dumps)
            raise
        except OutseeImageError as e:
            e.context.setdefault("gen_id", gen_id)
            e.context.setdefault("img_url", img_url)
            e.dumps = list(dumps)
            reason_l = (e.reason or "").lower()
            if "скач" in reason_l or "download" in reason_l:
                raise OutseeDownloadError(
                    e.reason, context=dict(e.context), dumps=list(dumps)
                ) from e
            raise
        except Exception as e:  # noqa: BLE001
            raise OutseeDownloadError(
                "outsee image: скачивание результата упало",
                context={
                    "gen_id": gen_id,
                    "img_url": img_url,
                    "err": f"{type(e).__name__}: {e}",
                },
                dumps=dumps,
            ) from e

        logger.info("outsee image saved → {} (gen_id={})", out_path, gen_id[:8])
        return GenerationResult(
            file_path=out_path, raw_url=img_url, gen_id=gen_id,
            dumps=dumps or None,
        )

    async def regenerate_image(
        self,
        out_path: Path,
        *,
        timeout: float = 600,
        gen_id: str | None = None,
        project_id: int | None = None,
        model_slug: str | None = None,
    ) -> GenerationResult:
        """Жмёт «Повторить» на существующем результате генерации — без ChatGPT,
        без перезаполнения промта. Сайт использует тот же промт и настройки."""
        import time as _time
        import uuid as _uuid

        from app.services.step_cancel import (
            abort_if_cancelled,
            await_with_cancel,
            register_active_page,
            unregister_active_page,
        )

        abort_if_cancelled(project_id)
        gen_id = gen_id or _uuid.uuid4().hex
        from app.services.outsee_lane import outsee_lane

        page_url = _image_page_url(model_slug)
        async with outsee_lane(project_id=project_id, op="regenerate_image"):
            page = await self.session.open_page(page_url, reuse=True)
            if project_id is not None:
                register_active_page(project_id, page)
            try:
                await await_with_cancel(
                    page.wait_for_load_state("domcontentloaded"), project_id
                )
                try:
                    await await_with_cancel(
                        page.wait_for_load_state("networkidle", timeout=15_000),
                        project_id,
                    )
                except Exception:
                    pass
                abort_if_cancelled(project_id)

                baseline_result_img = _strip_url_query(
                    await self._result_img_src(page)
                )
                baseline_big_imgs = {
                    _strip_url_query(u) for u in await self._all_big_imgs(page)
                }
                baseline_dom_srcs = {
                    _strip_url_query(u) for u in await self._all_img_srcs(page)
                }

                click_ts = _time.monotonic()
                net_events: list[tuple[float, str]] = []

                def _on_response(resp: Any) -> None:
                    try:
                        if not _is_candidate_image_response(resp):
                            return
                        net_events.append(
                            (_time.monotonic() - click_ts, resp.url)
                        )
                    except Exception:  # noqa: BLE001
                        pass

                page.on("response", _on_response)

                try:
                    retry_sel = await _first_visible(
                        page,
                        [
                            "button:has-text('Повторить')",
                            "button:has-text('Retry')",
                            "button:has-text('Regenerate')",
                        ],
                        timeout_ms=15_000,
                        project_id=project_id,
                    )
                    if not retry_sel:
                        raise OutseeImageError(
                            "outsee image: не найдена кнопка «Повторить» — "
                            "на странице нет предыдущего результата",
                            context={"gen_id": gen_id},
                        )
                    try:
                        await await_with_cancel(
                            page.locator(retry_sel).first.scroll_into_view_if_needed(
                                timeout=5_000
                            ),
                            project_id,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    pre_hit = await self._detect_outsee_failure(
                        page,
                        queue_mode=_outsee_queue_mode(),
                        prompt_id_prefix=None,
                    )
                    pre_rejected_text = _normalize_pre_failure_baseline(
                        str(pre_hit["text"]) if pre_hit else None,
                    )
                    click_ts = _time.monotonic()
                    net_events.clear()
                    await await_with_cancel(
                        page.locator(retry_sel).first.click(), project_id
                    )
                    logger.info(
                        "outsee.regenerate_image: «Повторить» кликнут, "
                        "queue-mode, gen_id={}",
                        gen_id[:8],
                    )

                    queue_mode = _outsee_queue_mode()
                    img_url = await self._wait_image_url_strict(
                        page,
                        timeout=timeout,
                        baseline_result_img=baseline_result_img,
                        baseline_big_imgs=baseline_big_imgs,
                        baseline_all_srcs=baseline_dom_srcs,
                        net_events=net_events,
                        gen_id=gen_id,
                        pre_rejected_text=pre_rejected_text,
                        prompt_id_prefix=None,
                        project_id=project_id,
                        queue_mode=queue_mode,
                    )
                finally:
                    try:
                        page.remove_listener("response", _on_response)
                    except Exception:  # noqa: BLE001
                        pass

                out_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if _outsee_queue_mode():
                        await _download_via_queue_result(
                            page,
                            img_url=img_url,
                            out_path=out_path,
                            gen_id=gen_id,
                            net_events=net_events,
                            project_id=project_id,
                        )
                    else:
                        await _download_via_context(
                            page, img_url, out_path, project_id=project_id
                        )
                except Exception as e:  # noqa: BLE001
                    raise OutseeDownloadError(
                        "outsee image: скачивание результата (regenerate) упало",
                        context={
                            "gen_id": gen_id,
                            "img_url": img_url,
                            "err": f"{type(e).__name__}: {e}",
                        },
                    ) from e

                _validate_downloaded_image(
                    out_path, gen_id=gen_id, img_url=img_url
                )
                logger.info(
                    "outsee image regenerated → {} (gen_id={})",
                    out_path,
                    gen_id[:8],
                )
                return GenerationResult(
                    file_path=out_path, raw_url=img_url, gen_id=gen_id
                )
            finally:
                if project_id is not None:
                    unregister_active_page(project_id)

    async def retry_image_download(
        self,
        *,
        img_url: str,
        out_path: Path,
        gen_id: str,
        prompt_id_prefix: str | None = None,
        project_id: int | None = None,
        model_slug: str | None = None,
        net_events: list[tuple[float, str]] | None = None,
    ) -> GenerationResult:
        """Повтор скачивания без нового Generate (картинка уже на outsee)."""
        from app.services.outsee_lane import outsee_lane
        from app.services.step_cancel import abort_if_cancelled

        abort_if_cancelled(project_id)
        page_url = _image_page_url(model_slug)
        events = list(net_events or [])
        resolved_url = _resolve_best_download_url(img_url, net_events=events)
        async with outsee_lane(project_id=project_id, op="retry_image_download"):
            page = await self.session.open_page(page_url, reuse=True)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            last_err: Exception | None = None
            try:
                downloaded = False
                # После Generate страница ещё на результате — queue Download
                # первым (как regenerate_image / video).
                if _outsee_queue_mode() and resolved_url:
                    try:
                        await _download_via_queue_result(
                            page,
                            img_url=resolved_url,
                            out_path=out_path,
                            gen_id=gen_id,
                            net_events=events,
                            project_id=project_id,
                        )
                        _validate_downloaded_image(
                            out_path, gen_id=gen_id, img_url=resolved_url
                        )
                        downloaded = True
                    except Exception as qe:  # noqa: BLE001
                        last_err = qe
                        logger.warning(
                            "retry_image_download: queue fail ({}) — cascade",
                            type(qe).__name__,
                        )
                if not downloaded and prompt_id_prefix:
                    await download_saved_image_by_prompt_id(
                        page,
                        prompt_id_prefix=prompt_id_prefix,
                        out_path=out_path,
                        project_id=project_id,
                        gen_id=gen_id,
                        model_slug=model_slug,
                    )
                    downloaded = True
                if not downloaded and resolved_url:
                    await _download_via_context_candidates(
                        page,
                        resolved_url,
                        out_path,
                        net_events=events,
                        project_id=project_id,
                    )
                    _validate_downloaded_image(
                        out_path, gen_id=gen_id, img_url=resolved_url
                    )
            except Exception as e:  # noqa: BLE001
                last_err = e
                # Fallback: URL-first с net_events, если ID-cascade не взял.
                if prompt_id_prefix and resolved_url:
                    try:
                        await _download_via_card_click(
                            page,
                            prompt_id_prefix=prompt_id_prefix,
                            out_path=out_path,
                            project_id=project_id,
                            img_url=resolved_url,
                            net_events=events,
                        )
                        _validate_downloaded_image(
                            out_path, gen_id=gen_id, img_url=resolved_url
                        )
                        last_err = None
                    except Exception as url_err:  # noqa: BLE001
                        last_err = url_err
                        logger.warning(
                            "retry_image_download: URL fallback failed: {}",
                            url_err,
                        )
                if last_err is not None:
                    if isinstance(last_err, OutseeImageError):
                        last_err.context.setdefault("gen_id", gen_id)
                        last_err.context.setdefault("img_url", resolved_url)
                        raise OutseeDownloadError(
                            last_err.reason, context=dict(last_err.context)
                        ) from last_err
                    raise OutseeDownloadError(
                        "outsee image: повторное скачивание упало",
                        context={
                            "gen_id": gen_id,
                            "img_url": resolved_url,
                            "err": f"{type(last_err).__name__}: {last_err}",
                        },
                    ) from last_err
        logger.info(
            "outsee retry_image_download saved → {} (gen_id={})",
            out_path,
            gen_id[:8],
        )
        return GenerationResult(
            file_path=out_path, raw_url=resolved_url, gen_id=gen_id
        )

    async def _wait_button_enabled(
        self, page: Page, selector: str, *, timeout_s: float = 180, project_id: int | None = None
    ) -> None:
        """Ждёт пока кнопка станет активной (не disabled).

        Важно: при активной кнопке НЕ падаем на плашку «запрещённы…» /
        stale-ошибку — иначе Generate никогда не кликается (баг P47-F43).
        Caller снимает pre-click baseline и кликает; живую модерацию ловим
        уже после клика. Fail-fast только если кнопка disabled И есть
        moderation/length.
        """
        from app.services.step_cancel import abort_if_cancelled, sleep_cancellable

        effective_timeout = min(timeout_s, _GENERATE_BUTTON_WAIT_SEC)
        deadline = asyncio.get_event_loop().time() + effective_timeout
        last_log = 0.0
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() < deadline:
            abort_if_cancelled(project_id)
            await _check_outsee_session(page)
            button_enabled = False
            try:
                loc = page.locator(selector).first
                disabled = await loc.get_attribute("disabled")
                aria = await loc.get_attribute("aria-disabled")
                button_enabled = (
                    disabled is None and (aria or "").lower() != "true"
                )
            except Exception:  # noqa: BLE001
                button_enabled = False
            if button_enabled:
                if (asyncio.get_event_loop().time() - start) > 1:
                    logger.info(
                        "outsee: Generate активен спустя {:.0f} сек",
                        asyncio.get_event_loop().time() - start,
                    )
                return
            # Кнопка ещё disabled — только тогда fail-fast по модерации/длине.
            try:
                failure = await self._detect_outsee_failure(page)
                if failure:
                    ftext = str(failure.get("text") or "")
                    if _fail_fast_while_generate_disabled(ftext):
                        logger.warning(
                            "outsee: Generate disabled + {} — не кликаем: {}",
                            _outsee_failure_kind(ftext),
                            ftext[:120],
                        )
                        _raise_outsee_failure(
                            text=ftext,
                            gen_id="",
                            elapsed=asyncio.get_event_loop().time() - start,
                            in_result=bool(failure.get("in_result")),
                        )
            except OutseeImageError:
                raise
            except Exception:  # noqa: BLE001
                pass
            now = asyncio.get_event_loop().time()
            if now - last_log > 15:
                last_log = now
                logger.info(
                    "outsee: жду пока Generate станет активной... ({:.0f} сек)",
                    now - start,
                )
            await sleep_cancellable(1.0, project_id)
        alerts = await _collect_visible_alert_snippets(page)
        msg = _outsee_timeout_message(
            "outsee: интерфейс завис (кнопка Generate неактивна)",
            alerts,
        )
        _log_outsee_error(kind="generate_stuck", text=msg)
        raise PWTimeoutError(msg)

    async def _click_generate_button(
        self,
        page: Page,
        *,
        where: str,
        project_id: int | None = None,
        dumps: list[Path] | None = None,
        context: dict[str, object] | None = None,
        physical_only: bool = False,
    ) -> str:
        """Клик Generate: wait enabled + проверка что генерация стартовала.

        `physical_only=True` (видео): все попытки — реальная мышь по центру
        кнопки; JS/locator.click не используем — outsee veo часто его игнорит.
        """
        from app.services.step_cancel import abort_if_cancelled, await_with_cancel, sleep_cancellable

        abort_if_cancelled(project_id)
        gen_sel = await _first_visible(
            page, GENERATE_BUTTON_SELECTORS, timeout_ms=10_000, project_id=project_id
        )
        if not gen_sel:
            dump_paths: list[Path] = []
            h, p = await _dump_page(page, f"{where}_generate_notfound")
            for x in (h, p):
                if x:
                    dump_paths.append(x)
            if dumps is not None:
                dumps.extend(dump_paths)
            raise OutseeImageError(
                f"outsee {where}: не найдена кнопка Generate",
                context=context or {},
                dumps=dump_paths,
            )
        logger.info("outsee.{}: кнопка Generate найдена ({})", where, gen_sel)
        loc = page.locator(gen_sel).first
        try:
            await await_with_cancel(
                loc.scroll_into_view_if_needed(timeout=5_000), project_id
            )
        except Exception:  # noqa: BLE001
            pass
        await self._wait_button_enabled(
            page, gen_sel, timeout_s=_GENERATE_BUTTON_WAIT_SEC, project_id=project_id
        )
        abort_if_cancelled(project_id)

        for attempt in range(1, 4):
            abort_if_cancelled(project_id)
            use_physical = physical_only or attempt > 1
            if use_physical:
                if attempt > 1 or physical_only:
                    logger.info(
                        "outsee.{}: клик Generate мышью {}/3",
                        where,
                        attempt,
                    )
                await _physical_mouse_click(
                    page, loc, project_id=project_id, label=f"{where} Generate"
                )
            else:
                await await_with_cancel(loc.click(), project_id)
            await sleep_cancellable(1.0, project_id)
            if not await self._generate_button_enabled(page):
                logger.info(
                    "outsee.{}: Generate кликнут (gen busy), жду результат",
                    where,
                )
                return gen_sel
            logger.warning(
                "outsee.{}: Generate всё ещё активна после клика {}/3",
                where,
                attempt,
            )

        dump_paths: list[Path] = []
        h, p = await _dump_page(page, f"{where}_generate_stuck")
        for x in (h, p):
            if x:
                dump_paths.append(x)
        if dumps is not None:
            dumps.extend(dump_paths)
        raise OutseeImageError(
            f"outsee {where}: Generate не запустилась — кнопка осталась "
            "активной после 3 кликов (в т.ч. physical mouse)",
            context={
                **(context or {}),
                "physical_only": physical_only,
                "gen_sel": gen_sel,
            },
            dumps=dump_paths,
        )

    async def _generation_started(self, page: Page) -> bool:
        """True — outsee реально начал генерацию (не только «кликнули»).

        Надёжный критерий: кнопка Generate ЗАБЛОКИРОВАНА (disabled).
        Это единственный сигнал, который outsee держит всё время рендера
        (5–15 мин). Спиннер или текст «генерация» НЕ используем как
        самостоятельный критерий — они появляются и при клике тогла
        «Безлимит/Relax» (анимация переключения), что давало false positive.

        Текстовые/spinner-сигналы используются ТОЛЬКО если Generate-кнопка
        тоже disabled — для диагностики в лог.
        """
        # Самый надёжный сигнал: кнопка Generate заблокирована.
        btn_enabled = await self._generate_button_enabled(page)
        if not btn_enabled:
            try:
                diag = await page.evaluate(
                    """() => {
                        const low = (document.body.innerText || '').toLowerCase();
                        const loadWords = [
                            'генерация', 'генериру', 'generating', 'processing',
                            'подождите', 'loading', 'создаём', 'creating'
                        ];
                        const hasText = loadWords.some(w => low.includes(w));
                        const spin = document.querySelector(
                            '[class*="animate-spin"], [class*="loading"], '
                            + '[data-loading="true"]'
                        );
                        const hasSpinner = !!(spin
                            && spin.getBoundingClientRect().width > 8);
                        return hasText ? 'text' : (hasSpinner ? 'spinner' : 'btn_disabled');
                    }"""
                )
            except Exception:  # noqa: BLE001
                diag = "btn_disabled"
            logger.info("outsee: generation_started signal ({})", diag)
            return True
        return False

    async def _ensure_relax_for_video(
        self,
        page: Page,
        *,
        want_on: bool,
        where: str,
        project_id: int | None = None,
        dumps: list[Path] | None = None,
    ) -> None:
        """Relax/Безлимит для veo — physical click + проверка состояния."""
        from app.services.step_cancel import abort_if_cancelled, sleep_cancellable

        abort_if_cancelled(project_id)
        if not want_on:
            await _toggle_relax(
                page,
                want_on=False,
                where=where,
                dumps=dumps,
                project_id=project_id,
            )
            return

        for attempt in range(1, 4):
            abort_if_cancelled(project_id)
            await _toggle_relax(
                page,
                want_on=True,
                where=where,
                dumps=dumps,
                project_id=project_id,
            )
            limit_sel = await _first_visible(
                page, LIMIT_TOGGLE_SELECTORS, timeout_ms=3_000, project_id=project_id
            )
            if limit_sel:
                state = await _read_limit_toggle_on(page, limit_sel)
                if state is True:
                    logger.info(
                        "outsee.{}: Relax ON подтверждён (Безлимит, попытка {})",
                        where,
                        attempt,
                    )
                    return

            targets = await _scan_toggle_targets(
                page, keywords=["безлимит", "relax"]
            )
            logger.info(
                "outsee.{}: Relax scan → {} целей (попытка {})",
                where,
                len(targets),
                attempt,
            )
            for idx, t in enumerate(targets[:4]):
                cx, cy = float(t["cx"]), float(t["cy"])
                await _cdp_dispatch_click(page, cx, cy, project_id=project_id)
                await _viewport_mouse_click(
                    page, cx, cy, project_id=project_id, label=f"relax#{idx}"
                )
                await sleep_cancellable(0.5, project_id)
                if limit_sel and await _read_limit_toggle_on(page, limit_sel) is True:
                    logger.info(
                        "outsee.{}: Relax ON после scan-click #{}", where, idx
                    )
                    return

            await sleep_cancellable(0.8, project_id)

        dump_paths: list[Path] = []
        h, p = await _dump_page(page, "video_relax_not_on")
        for x in (h, p):
            if x:
                dump_paths.append(x)
        if dumps is not None:
            dumps.extend(dump_paths)
        raise OutseeImageError(
            "outsee video: не удалось включить Relax (Безлимит)",
            context={"where": where, "want_on": want_on},
            dumps=dump_paths,
        )

    async def _scan_generate_click_targets(self, page: Page) -> list[dict[str, Any]]:
        try:
            raw = await page.evaluate(
                """() => {
                    const keys = ['генерир', 'generate', 'создать', 'run'];
                    // Исключаем кнопки Relax/Безлимит/Режим — они тоже
                    // содержат слово «генерир» в описании («может генерировать»),
                    // но НЕ являются кнопкой запуска генерации.
                    const exclude = [
                        'relax', 'безлимит', 'режим', 'дешевле',
                        'cheaper', 'mode', 'качество', 'quality',
                    ];
                    const out = [];
                    for (const el of document.querySelectorAll(
                        'button, [role="button"], a'
                    )) {
                        const text = (el.innerText || el.textContent || '')
                            .trim();
                        const low = text.toLowerCase();
                        if (!keys.some(k => low.includes(k))) continue;
                        // Пропускаем элементы с исключёнными словами
                        if (exclude.some(e => low.includes(e))) continue;
                        // Текст длиннее 40 симв без начала на ключевое слово —
                        // это описание кнопки (напр. «Дешевле, но может
                        // генерировать»), а не сама кнопка «Генерировать».
                        if (text.length > 40
                            && !keys.some(k => low.startsWith(k))) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 36 || r.height < 18) continue;
                        const cs = getComputedStyle(el);
                        if (cs.display === 'none' || cs.visibility === 'hidden') {
                            continue;
                        }
                        if (cs.pointerEvents === 'none') continue;
                        const op = parseFloat(cs.opacity || '1');
                        if (op < 0.15) continue;
                        out.push({
                            text: text.slice(0, 72),
                            disabled: !!el.disabled
                                || el.getAttribute('aria-disabled') === 'true',
                            cx: Math.round(r.x + r.width / 2),
                            cy: Math.round(r.y + r.height / 2),
                            area: Math.round(r.width * r.height),
                        });
                    }
                    out.sort((a, b) => b.area - a.area);
                    return out;
                }"""
            )
            return list(raw or [])
        except Exception:  # noqa: BLE001
            return []

    async def _trigger_generate_video(
        self,
        page: Page,
        *,
        input_sel: str | None,
        project_id: int | None = None,
        dumps: list[Path] | None = None,
        context: dict[str, object] | None = None,
    ) -> None:
        """Запуск генерации veo — несколько механик, пока не пошла генерация."""
        from app.services.step_cancel import (
            abort_if_cancelled,
            await_with_cancel,
            sleep_cancellable,
        )

        abort_if_cancelled(project_id)
        strategies_tried: list[str] = []

        async def _started() -> bool:
            await sleep_cancellable(0.8, project_id)
            return await self._generation_started(page)

        # A0) Прямой клик по кнопке "Генерировать" — ищем по тексту,
        # работает на любом разрешении. Кнопка появляется после ввода
        # промта, поэтому ждём 1с.
        await sleep_cancellable(1.0, project_id)
        for text_v in ("Генерировать", "Generate", "Сгенерировать"):
            abort_if_cancelled(project_id)
            try:
                btn = page.get_by_role(
                    "button", name=text_v, exact=True
                ).first
                if await btn.count() > 0 and await btn.is_visible():
                    box = await btn.bounding_box()
                    if box and box["width"] > 30:
                        cx, cy = (
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        logger.info(
                            "outsee.generate_video: A0 кнопка {!r} "
                            "({:.0f},{:.0f}) — клик мышью",
                            text_v, cx, cy,
                        )
                        await page.mouse.click(cx, cy)
                        strategies_tried.append(f"A0_mouse_{text_v}")
                        if await _started():
                            return
                        await _cdp_dispatch_click(
                            page, cx, cy, project_id=project_id,
                        )
                        strategies_tried.append(f"A0_cdp_{text_v}")
                        if await _started():
                            return
            except Exception:  # noqa: BLE001
                pass

        # A) Скан DOM → клик «Генерировать» (CDP + mouse + JS).
        targets = await self._scan_generate_click_targets(page)
        gen_first = [
            t
            for t in targets
            if "генерир" in str(t.get("text") or "").lower()
            or "generate" in str(t.get("text") or "").lower()
        ]
        ordered = gen_first + [t for t in targets if t not in gen_first]
        logger.info(
            "outsee.generate_video: scan {} кнопок (генерир-first: {})",
            len(targets),
            len(gen_first),
        )
        for idx, t in enumerate(ordered[:8]):
            if t.get("disabled"):
                continue
            cx, cy = float(t["cx"]), float(t["cy"])
            text = str(t.get("text") or "")[:40]
            abort_if_cancelled(project_id)

            await _cdp_dispatch_click(
                page, cx, cy, project_id=project_id
            )
            strategies_tried.append(f"cdp#{idx}")
            logger.info(
                "outsee.generate_video: CDP click #{} ({:.0f},{:.0f}) {!r}",
                idx,
                cx,
                cy,
                text,
            )
            if await _started():
                return

            await _viewport_mouse_click(
                page, cx, cy, project_id=project_id, label=f"gen#{idx}"
            )
            strategies_tried.append(f"mouse#{idx}")
            if await _started():
                return

            try:
                await page.evaluate(
                    """([x, y]) => {
                        const el = document.elementFromPoint(x, y);
                        if (!el) return false;
                        const btn = el.closest(
                            'button, [role="button"], a'
                        ) || el;
                        for (const type of [
                            'pointerdown', 'mousedown', 'mouseup',
                            'pointerup', 'click'
                        ]) {
                            btn.dispatchEvent(new MouseEvent(type, {
                                bubbles: true, cancelable: true,
                                view: window, clientX: x, clientY: y,
                            }));
                        }
                        return true;
                    }""",
                    [cx, cy],
                )
                strategies_tried.append(f"js#{idx}")
                if await _started():
                    logger.info(
                        "outsee.generate_video: старт по JS #{}", idx
                    )
                    return
            except Exception:  # noqa: BLE001
                pass

        # B) Горячие клавиши (после клика по кнопке).
        if input_sel:
            for keys, name in (
                ("Control+Enter", "ctrl+enter"),
                ("Enter", "enter"),
            ):
                abort_if_cancelled(project_id)
                try:
                    loc = page.locator(input_sel).first
                    await await_with_cancel(loc.focus(), project_id)
                    await sleep_cancellable(0.15, project_id)
                    await await_with_cancel(
                        page.keyboard.press(keys), project_id
                    )
                    strategies_tried.append(name)
                    logger.info(
                        "outsee.generate_video: попытка {}", name
                    )
                    if await _started():
                        logger.info(
                            "outsee.generate_video: старт по {}", name
                        )
                        return
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "outsee.generate_video: {} failed: {}", name, e
                    )

        # C) Playwright get_by_role / селекторы (force).
        for label, click_fn in (
            (
                "role_генерировать",
                lambda: page.get_by_role(
                    "button",
                    name=re.compile(r"генерир|generate|создать", re.I),
                ).first.click(force=True, timeout=5_000),
            ),
            (
                "selector",
                lambda: self._click_generate_button(
                    page,
                    where="generate_video",
                    project_id=project_id,
                    dumps=dumps,
                    context=context,
                    physical_only=False,
                ),
            ),
        ):
            abort_if_cancelled(project_id)
            try:
                if label == "selector":
                    await click_fn()
                else:
                    await await_with_cancel(click_fn(), project_id)
                strategies_tried.append(label)
                if await _started():
                    logger.info(
                        "outsee.generate_video: старт по {}", label
                    )
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "outsee.generate_video: {} failed: {}",
                    label,
                    type(e).__name__,
                )

        # D) physical-only по селекторам (последний шанс).
        try:
            await self._click_generate_button(
                page,
                where="generate_video",
                project_id=project_id,
                dumps=dumps,
                context=context,
                physical_only=True,
            )
            strategies_tried.append("physical_selectors")
            if await _started():
                return
        except OutseeImageError:
            pass

        dump_paths: list[Path] = []
        h, p = await _dump_page(page, "video_generate_all_failed")
        for x in (h, p):
            if x:
                dump_paths.append(x)
        if dumps is not None:
            dumps.extend(dump_paths)
        raise OutseeImageError(
            "outsee video: не удалось запустить Generate "
            "(все механики: клавиши, CDP, мышь, JS, role, селекторы)",
            context={
                **(context or {}),
                "strategies": strategies_tried,
                "targets": targets[:5],
            },
            dumps=dump_paths,
        )

    async def _result_img_src(self, page: Page) -> str | None:
        """Src большой картинки из блока «Результат генерации» (или None,
        если блока ещё нет / там плейсхолдер/спиннер)."""
        try:
            return await page.evaluate(
                """() => {
                    const imgs = Array.from(document.querySelectorAll('img'));
                    const keywords = ['Результат генерации', 'Результат', 'Result'];
                    for (const img of imgs) {
                        const r = img.getBoundingClientRect();
                        if (r.width < 200 || r.height < 200) continue;
                        let el = img;
                        for (let i = 0; i < 14 && el; i++) {
                            const t = el.textContent || '';
                            for (const kw of keywords) {
                                if (t.includes(kw)) return img.src || null;
                            }
                            el = el.parentElement;
                        }
                    }
                    return null;
                }"""
            )
        except Exception:  # noqa: BLE001
            return None

    async def _all_big_imgs(self, page: Page) -> list[str]:
        """Все изображения на странице с размером ≥200×200 (визуальный bbox)."""
        try:
            return await page.evaluate(
                """() => {
                    const out = [];
                    for (const img of document.querySelectorAll('img')) {
                        const r = img.getBoundingClientRect();
                        if (r.width >= 200 && r.height >= 200 && img.src) {
                            out.push(img.src);
                        }
                    }
                    return out;
                }"""
            )
        except Exception:  # noqa: BLE001
            return []

    async def _all_img_srcs(self, page: Page) -> list[str]:
        """Все непустые src на странице (для baseline-снимка)."""
        try:
            return await page.evaluate(
                """() => Array.from(document.querySelectorAll('img'))
                          .map(i => i.src).filter(Boolean)"""
            )
        except Exception:  # noqa: BLE001
            return []

    async def _completed_new_imgs(
        self, page: Page, baseline_srcs: set[str]
    ) -> list[str]:
        """Возвращает src всех `<img>` на странице, которые:
          - не были в baseline до старта генерации (сравнение по host+path,
            без `?query` — иначе перевыпуск AWS-подписи на каждом ререндере
            фолсли помечает галерейные thumb'ы как «новые»),
          - уже полностью загружены (img.complete && naturalWidth>0),
          - имеют natural-размер ≥200×200 (отсекает иконки/аватары).
        Список упорядочен в порядке появления в DOM (первый элемент —
        самая верхняя карточка в галерее outsee, обычно самая свежая —
        outsee рендерит результаты сверху-вниз новейшими-первыми).

        ВАЖНО: `baseline_srcs` должен быть множеством НОРМАЛИЗОВАННЫХ
        URL (без query). Колл-сайт обязан использовать `_strip_url_query`
        при формировании baseline."""
        baseline_list = list(baseline_srcs)
        try:
            res = await page.evaluate(
                """(baseline) => {
                    const skip = new Set(baseline);
                    const stripQ = (u) => {
                        if (!u) return '';
                        const hash = u.indexOf('#');
                        if (hash >= 0) u = u.substring(0, hash);
                        const q = u.indexOf('?');
                        if (q >= 0) u = u.substring(0, q);
                        return u;
                    };
                    const out = [];
                    for (const img of document.querySelectorAll('img')) {
                        if (!img.src) continue;
                        const stable = stripQ(img.src);
                        if (skip.has(stable)) continue;
                        if (img.src.startsWith('data:')) continue;
                        if (img.src.includes('/placeholder.svg')) continue;
                        if (!img.complete) continue;
                        if (!img.naturalWidth || img.naturalWidth < 200) continue;
                        if (!img.naturalHeight || img.naturalHeight < 200) continue;
                        out.push(img.src);
                    }
                    return out;
                }""",
                baseline_list,
            )
            return list(res or [])
        except Exception:  # noqa: BLE001
            return []

    async def _completed_new_videos(
        self, page: Page, baseline_urls: set[str]
    ) -> list[str]:
        """Новые URL роликов в DOM (вне baseline, readyState≥2)."""
        baseline_list = list(baseline_urls)
        try:
            res = await page.evaluate(
                """(baseline) => {
                    const skip = new Set(baseline);
                    const stripQ = (u) => {
                        if (!u) return '';
                        const hash = u.indexOf('#');
                        if (hash >= 0) u = u.substring(0, hash);
                        const q = u.indexOf('?');
                        if (q >= 0) u = u.substring(0, q);
                        return u;
                    };
                    const seen = new Set();
                    const out = [];
                    const push = (u) => {
                        if (!u || u.startsWith('data:')) return;
                        const stable = stripQ(u);
                        if (skip.has(stable) || seen.has(stable)) return;
                        seen.add(stable);
                        out.push(u);
                    };
                    for (const v of document.querySelectorAll('video')) {
                        let src = v.currentSrc || v.src || '';
                        if (!src) {
                            const s = v.querySelector('source');
                            if (s && s.src) src = s.src;
                        }
                        if (!src) continue;
                        if (v.readyState < 2) continue;
                        push(src);
                    }
                    document.querySelectorAll(
                        "a[download], a[href*='.mp4']"
                    ).forEach(a => { if (a.href) push(a.href); });
                    return out;
                }""",
                baseline_list,
            )
            return [u for u in (res or []) if _video_url_looks_like_result(u)]
        except Exception:  # noqa: BLE001
            return []

    async def _find_img_by_prompt_id(
        self,
        page: Page,
        id_token: str,
        *,
        max_levels: int = 12,
        limit: int = _GALLERY_ID_SCAN_LIMIT,
    ) -> str | None:
        """Ищет src среди последних `limit` больших thumb'ов галереи."""
        return await find_img_src_by_prompt_id_in_gallery(
            page,
            id_token,
            limit=limit,
            max_levels=max_levels,
        )

    async def _verify_img_url_matches_prompt_id(
        self,
        page: Page,
        img_url: str,
        prompt_id_prefix: str,
        *,
        gen_id: str | None = None,
    ) -> None:
        """Перед скачиванием: URL = карточка с нашим [ID] (≤10 последних thumb)."""
        await verify_img_url_matches_prompt_id_in_gallery(
            page,
            img_url,
            prompt_id_prefix,
            gen_id=gen_id,
        )

    async def _count_id_tokens_in_page(
        self, page: Page, tokens: list[str]
    ) -> dict[str, int]:
        """Возвращает карту {token: количество_вхождений} на странице.

        Сканирует:
          1) `body.innerText` — рендеренный текст;
          2) `<textarea>.value` и `<input>.value` — значения форм,
             которые **не** попадают в innerText. Outsee рендерит
             промт в правой панели через <textarea readonly>, и без
             этой проверки счётчик не растёт после клика → ложное
             «чужая картинка» в `_verify_img_by_clicking`.

        Используется для дифференциальной проверки клика: если outsee
        уже показывает наш `[ID: ...]` где-то на странице (например, в
        панели «генерация в процессе» или в карточке композера) ДО
        клика — простой `includes`-чек даст ложноположительный ответ
        для любой кликнутой картинки. Считаем количество вхождений и
        смотрим РОСТ после клика — рост означает «открылась наша
        карточка из gallery».
        """
        js = """
        ([toks]) => {
            const body = document.body;
            let text = (body && (body.innerText || body.textContent)) || '';
            // Добавляем значения <textarea>/<input> — они не попадают
            // в innerText, но outsee рендерит туда полный промт.
            for (const el of document.querySelectorAll(
                'textarea, input[type=text], input:not([type])'
            )) {
                const v = el && el.value;
                if (v) text += '\\n' + v;
            }
            const result = {};
            for (const tok of toks) {
                if (!tok) { result[tok] = 0; continue; }
                // text.split(tok).length - 1 = количество вхождений
                result[tok] = text.split(tok).length - 1;
            }
            return result;
        }
        """
        try:
            res = await page.evaluate(js, [tokens])
            if isinstance(res, dict):
                return {t: int(res.get(t, 0) or 0) for t in tokens}
            return dict.fromkeys(tokens, 0)
        except Exception:  # noqa: BLE001
            return dict.fromkeys(tokens, 0)

    async def _verify_img_by_clicking(
        self, page: Page, target_src: str, id_token: str
    ) -> bool:
        """Кликает в DOM на `<img>` с src=`target_src`, ждёт появления
        панели «ПРОМПТ» (outsee рисует её только по клику на картинку),
        и проверяет, что в видимом тексте этой панели присутствует
        `id_token` (или его 8-hex-tail).

        Дифференциальная проверка: outsee уже МОЖЕТ показывать наш
        `[ID: ...]` где-то (например, в индикаторе «генерация в процессе»
        с прикреплённым к ней нашим же текущим промтом). Поэтому мы:
          1. Снимаем количество вхождений токенов в body.innerText ДО клика.
          2. Кликаем картинку.
          3. Ждём что количество УВЕЛИЧИТСЯ — это значит что после клика
             в DOM добавилась карточка с этим же токеном (наша картинка
             из gallery).
          4. Если количество не изменилось — клик не открыл нашу карточку,
             это чужая картинка → False.

        Возвращает True/False. После проверки закрываем панель Esc'ом —
        чтобы следующая итерация ждала корректное состояние.
        """
        # Извлекаем 8-hex-tail для liberal-match'а.
        tokens: list[str] = [id_token]
        m = re.search(r"\[ID:\s*([A-Za-z0-9_-]+)\s*\]", id_token)
        if m:
            tokens.append(m.group(1))
        m2 = re.search(r"-([0-9a-fA-F]{8})\]?$", id_token)
        if m2:
            tokens.append(m2.group(1))

        try:
            # 1. Снапшот вхождений ДО клика.
            pre_count = await self._count_id_tokens_in_page(page, tokens)
            pre_total = sum(pre_count.values())

            # 2. Клик по img с нужным src — через JS, потому что обычный
            # locator может не найти элемент если он внутри сложной
            # модалки/canvas-обёртки.
            clicked = await page.evaluate(
                """(targetSrc) => {
                    for (const img of document.querySelectorAll('img')) {
                        if (img.src === targetSrc) {
                            img.scrollIntoView({block:'center'});
                            // Кликабельный родитель — обычно <button>
                            // или <a>, но если нет, кликаем сам img.
                            let target = img;
                            for (let i = 0; i < 4; i++) {
                                if (!target.parentElement) break;
                                const tag = target.tagName?.toLowerCase();
                                if (tag === 'button' || tag === 'a') break;
                                target = target.parentElement;
                            }
                            target.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                target_src,
            )
            if not clicked:
                logger.warning(
                    "_verify_img_by_clicking: не нашёл <img src='{}'> в DOM",
                    target_src[:100],
                )
                return False

            # 3. Ждём пока правая панель отрендерит prompt-текст.
            # Дифференциальная проверка: ждём чтобы количество вхождений
            # хотя бы одного токена УВЕЛИЧИЛОСЬ после клика — это значит
            # что после клика в DOM добавилась карточка с нашим ID.
            # Polling до 5 секунд.
            for _ in range(10):
                await asyncio.sleep(0.5)
                cur_count = await self._count_id_tokens_in_page(page, tokens)
                cur_total = sum(cur_count.values())
                if cur_total > pre_total:
                    logger.info(
                        "_verify_img_by_clicking: token count вырос после "
                        "клика ({} -> {}, pre={}, cur={}), это НАША картинка",
                        pre_total, cur_total, pre_count, cur_count,
                    )
                    return True

            # 4. Количество не выросло — клик не открыл нашу карточку.
            logger.warning(
                "_verify_img_by_clicking: token count НЕ вырос после клика "
                "(pre_total={}, pre={}). Outsee либо показывает наш токен "
                "только в композере (поэтому он попадает в body всегда), "
                "либо это чужая картинка. Считаем что чужая. Diag: {}",
                pre_total, pre_count,
                await self._diag_id_in_page(page, id_token),
            )
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "_verify_img_by_clicking: исключение: {}", e
            )
            return False
        finally:
            # Закрываем панель Escape'ом, чтобы следующая итерация
            # ожидания не зависела от текущего состояния модалки.
            try:
                await page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass

    async def _diag_id_in_page(
        self, page: Page, id_token: str
    ) -> dict[str, Any]:
        """Диагностика: сообщает, встречается ли `id_token` (или его
        составляющие) где-либо в DOM-тексте страницы. Используется,
        когда `_find_img_by_prompt_id` не нашёл совпадения, чтобы понять
        — outsee вообще не показывает наш ID, или показывает, но в
        форме, которую наш JS не находит.
        """
        tokens: list[str] = [id_token]
        m = re.search(r"\[ID:\s*([A-Za-z0-9_-]+)\s*\]", id_token)
        if m:
            tokens.append(m.group(1))
        m2 = re.search(r"-([0-9a-fA-F]{8})\]?$", id_token)
        if m2:
            tokens.append(m2.group(1))

        js = """
        ([tokens]) => {
            const body = document.body;
            let text = (body && (body.innerText || body.textContent)) || '';
            for (const el of document.querySelectorAll(
                'textarea, input[type=text], input:not([type])'
            )) {
                const v = el && el.value;
                if (v) text += '\\n' + v;
            }
            const result = {};
            for (const tok of tokens) {
                result[tok] = text.includes(tok);
            }
            // Также вернём количество <img> в DOM с непустым src
            const imgs = document.querySelectorAll('img');
            let total = 0, complete = 0;
            for (const img of imgs) {
                if (!img.src || img.src.startsWith('data:')) continue;
                total++;
                if (img.complete && img.naturalWidth >= 200) complete++;
            }
            result['__imgs_total'] = total;
            result['__imgs_complete'] = complete;
            return result;
        }
        """
        try:
            res = await page.evaluate(js, [tokens])
            if isinstance(res, dict):
                return res
            return {}
        except Exception:  # noqa: BLE001
            return {}

    async def _wait_image_url_strict(
        self,
        page: Page,
        *,
        timeout: float,
        baseline_result_img: str | None,
        baseline_big_imgs: set[str],
        baseline_all_srcs: set[str],
        net_events: list[tuple[float, str]] | None = None,
        gen_id: str,
        pre_rejected_text: str | None = None,
        prompt_id_prefix: str | None = None,
        project_id: int | None = None,
        queue_mode: bool = False,
        prompt_len: int | None = None,
    ) -> str:
        """Жёсткое ожидание свежей картинки.

        ПРИОРИТЕТ 0 (если передан `prompt_id_prefix`, например
        `[ID: P1-HERO1-V1-349303db]`): ищем в DOM карточку, у которой
        видимый текст содержит этот токен, и возвращаем `<img>` из неё.
        Это самая строгая проверка — она исключает все картинки из
        history-галереи outsee и любые «чужие» резалт-карточки.

        Если `prompt_id_prefix` не передан — работают старые правила:

        1) `<img>` из блока «Результат генерации», у которого src отличается
           от baseline и который полностью загружен (img.complete &&
           naturalWidth >= 200);
        2) либо самая последняя `<img>`, появившаяся в DOM ПОСЛЕ нажатия
           Generate, прошедшая ту же проверку.

        Если передан `net_events` (список (offset_sec, url) от listener'a
        «response», очищенный в момент клика Generate) — выбранный URL
        ДОПОЛНИТЕЛЬНО верифицируется: он должен быть в списке реально
        пришедших по сети image-ответов ПОСЛЕ клика. Это отсекает
        старые картинки из history/кэша, которые могут появиться в DOM при
        ререндере без реальной сетевой загрузки. Если net_events=None или
        пуст — верификация работает по старому (либеральному) пути.

        Никаких «возьму самую большую» — это и был источник косяков
        (приходила постер видосов / старая картинка из кэша). Если за
        timeout условие не сработало — кидаем OutseeImageError с подробным
        контекстом, что было/чего не хватило.
        """
        start = asyncio.get_event_loop().time()
        deadline = start + timeout
        last_log = 0.0
        last_seen_result: str | None = None
        # Кандидат от «старой» логики (baseline + net_events). Когда
        # появляется, бот кликает по нему в браузере чтобы открыть
        # outsee'вскую правую панель «Промпт» и проверить [ID: ...].
        # Если ID совпал — это наша картинка, возвращаем; если нет —
        # это чужая из gallery, добавляем в rejected_candidates и ждём
        # дальше.
        fallback_candidate: str | None = None
        fallback_source: str | None = None  # "result_block" | "new_dom"
        # URL'ы, для которых клик-верификация уже была проведена и
        # ID не совпал. Чтобы не кликать одну и ту же чужую картинку
        # снова и снова.
        # Храним НОРМАЛИЗОВАННЫЕ URL (без query), потому что re-sign
        # на каждом ререндере меняет raw URL, и «та же» чужая
        # картинка в следующем итерейшене click-verify выглядела бы как
        # другой URL.
        rejected_candidates: set[str] = set()

        from app.services.step_cancel import abort_if_cancelled, sleep_cancellable

        failure_baseline = frozenset()
        if pre_rejected_text:
            failure_baseline = frozenset(
                {_normalize_outsee_failure_text(pre_rejected_text)}
            )
        stale_logged: set[str] = set()

        while asyncio.get_event_loop().time() < deadline:
            abort_if_cancelled(project_id)
            now = asyncio.get_event_loop().time()
            elapsed = now - start

            # 0) Fail-fast: ошибка генерации / модерация (до ожидания img).
            if elapsed >= 1.5:
                failure = await self._detect_outsee_failure(
                    page,
                    queue_mode=queue_mode,
                    prompt_id_prefix=prompt_id_prefix,
                )
                if (
                    not failure
                    and queue_mode
                    and prompt_id_prefix
                ):
                    failure = await self._detect_queue_card_failure(
                        page,
                        prompt_id_prefix=prompt_id_prefix,
                    )
                if failure:
                    ftext = failure["text"]
                    in_result = bool(failure.get("in_result"))
                    gen_idle = await self._generate_button_enabled(page)
                    if _outsee_failure_is_stale(
                        ftext,
                        baseline_failure_texts=failure_baseline,
                        in_result=in_result,
                        elapsed=elapsed,
                        gen_idle=gen_idle,
                        queue_mode=queue_mode,
                        prompt_id_prefix=prompt_id_prefix,
                        card_scoped=bool(failure.get("queue_card")),
                    ):
                        stale_key = _normalize_outsee_failure_text(ftext)[:80]
                        if stale_key not in stale_logged:
                            stale_logged.add(stale_key)
                            if _outsee_failure_kind(ftext) == "busy":
                                logger.info(
                                    "_wait_image_url_strict: безлимит занят — "
                                    "жду завершения ({:.0f}с): {}",
                                    elapsed,
                                    ftext[:100],
                                )
                            else:
                                logger.debug(
                                    "_wait_image_url_strict: игнорирую stale "
                                    "плашку (in_result={}, gen_idle={}): {}",
                                    in_result,
                                    gen_idle,
                                    ftext[:80],
                                )
                    else:
                        kind_now = _outsee_failure_kind(ftext)
                        if kind_now == "busy":
                            logger.info(
                                "_wait_image_url_strict: безлимит занят — "
                                "жду завершения ({:.0f}с): {}",
                                elapsed,
                                ftext[:100],
                            )
                        else:
                            logger.info(
                                "_wait_image_url_strict: ошибка outsee за "
                                "{:.0f} сек (in_result={}, gen_idle={}, "
                                "kind={}): {}",
                                elapsed,
                                in_result,
                                gen_idle,
                                kind_now,
                                ftext[:120],
                            )
                            _raise_outsee_failure(
                                text=ftext,
                                gen_id=gen_id,
                                elapsed=elapsed,
                                in_result=in_result,
                                prompt_len=prompt_len,
                            )

            # 1) ВЫСШИЙ приоритет — поиск картинки по `prompt_id_prefix`.
            # Outsee рендерит в карточке результата начало промта, и наш
            # `[ID: P1-HERO1-V1-…]` всегда стоит первой строкой. Если в
            # DOM появилась карточка с НАШИМ ID — берём её картинку,
            # независимо от baseline и порядка. Это полностью отсекает
            # старые/чужие фото из истории outsee.
            if prompt_id_prefix:
                by_id = await self._find_img_by_prompt_id(
                    page, prompt_id_prefix
                )
                if by_id:
                    by_id_norm = _strip_url_query(by_id)
                    fresh_ok = (
                        by_id_norm != baseline_result_img
                        and by_id_norm not in baseline_all_srcs
                        and not any(
                            m in by_id.lower() for m in _UI_ASSET_MARKERS
                        )
                        and not any(
                            m in by_id.lower() for m in _INPUT_REF_MARKERS
                        )
                    )
                    if fresh_ok:
                        resolved = _resolve_best_download_url(
                            by_id, net_events=net_events
                        )
                        logger.info(
                            "_wait_image_url_strict: matched by prompt_id "
                            "{} за {:.0f} сек: {}",
                            prompt_id_prefix,
                            elapsed,
                            resolved[:140],
                        )
                        return resolved

            # 1) Параллельно — отслеживаем кандидата по «старой» логике
            # (baseline + net_events). Сохраняем последнего подходящего
            # в `fallback_candidate`, но НЕ возвращаем сразу: даём шанс
            # ID-верификации найти именно НАШУ карточку. Если ID-поиск
            # за весь timeout ничего не найдёт, возьмём этот кандидат
            # как safety-net (с WARNING в лог).
            current = await self._result_img_src(page)
            last_seen_result = current
            current_norm = _strip_url_query(current) if current else ""
            if (
                current
                and current_norm != baseline_result_img
                and not current.endswith("/placeholder.svg")
                and "data:image" not in current
                and not any(
                    m in current.lower() for m in _INPUT_REF_MARKERS
                )
                and not any(
                    m in current.lower() for m in _UI_ASSET_MARKERS
                )
                and current_norm not in baseline_all_srcs
            ):
                if await self._img_is_loaded(page, current):
                    # Пока net_events пуст — доверяем DOM (новый src вне
                    # baseline). Иначе Outsee может отдать картинку без
                    # image/* response ≥50KB в listener.
                    if (not net_events) or _url_is_fresh(current, net_events):
                        if not prompt_id_prefix:
                            # Без ID-верификации — возвращаем сразу
                            # (старое поведение).
                            logger.info(
                                "_wait_image_url_strict: «Результат генерации» "
                                "за {:.0f} сек: {}",
                                elapsed,
                                current[:140],
                            )
                            return _resolve_best_download_url(
                                current, net_events=net_events
                            )
                        else:
                            # С ID-верификацией — только запоминаем,
                            # если ещё не отвергали этот URL (по норм.).
                            if (
                                _strip_url_query(current)
                                not in rejected_candidates
                            ):
                                fallback_candidate = current
                                fallback_source = "result_block"

            new_srcs = await self._completed_new_imgs(page, baseline_all_srcs)
            if new_srcs:
                clean = [
                    u
                    for u in new_srcs
                    if not any(m in u.lower() for m in _UI_ASSET_MARKERS)
                    and not any(m in u.lower() for m in _INPUT_REF_MARKERS)
                ]
                if net_events:
                    clean = [u for u in clean if _url_is_fresh(u, net_events)]
                # Исключаем уже отвергнутых при ID-верификации (сравнение
                # по нормализованным URL'ам — см. _strip_url_query).
                if prompt_id_prefix:
                    clean = [
                        u for u in clean
                        if _strip_url_query(u) not in rejected_candidates
                    ]
                if clean:
                    # ПИКАЕМ FIRST (не last): outsee рендерит результаты
                    # сверху-вниз новейшими-первыми, поэтому первый элемент DOM
                    # в `new_srcs` — самая свежая карточка. Старый код
                    # брал clean[-1] — last — и в ситуации «все галерейные
                    # thumb'ы выглядят new из-за re-sign URL» это приводило
                    # к скачиванию старой картинки. Теперь baseline нормализован
                    # (без query) + берём лучший (full PNG, не thumb).
                    clean.sort(key=_url_download_priority)
                    chosen = clean[0]
                    if not prompt_id_prefix:
                        if len(clean) > 1 and not net_events:
                            logger.warning(
                                "_wait_image_url_strict: {} новых <img> без "
                                "net_events и без [ID] — ждём однозначный "
                                "результат (не берём из галереи)",
                                len(clean),
                            )
                        elif net_events or len(clean) == 1:
                            logger.info(
                                "_wait_image_url_strict: новая <img> в DOM за "
                                "{:.0f} сек: {} (всего новых: {})",
                                elapsed,
                                chosen[:140],
                                len(clean),
                            )
                            return _resolve_best_download_url(
                                chosen, net_events=net_events, extra_urls=clean
                            )
                    else:
                        # С ID-верификацией — запоминаем как fallback,
                        # но click-verify всё равно сработает (или net_events).
                        fallback_candidate = chosen
                        fallback_source = "new_dom"
                        # Диагностика: сколько «новых» набралось в список —
                        # если больше 1, то это признак re-sign или gallery
                        # refresh — логируем, чтобы было понятно в логе.
                        if len(clean) > 1:
                            logger.info(
                                "_wait_image_url_strict: new_srcs={} (>1) — "
                                "беру первый по DOM (новейший в outsee), "
                                "проверю click/net_events: {}",
                                len(clean), chosen[:120],
                            )

            # 2.7) С prompt_id_prefix НЕ делаем _verify_img_by_clicking в wait:
            # ID почти всегда уже в композере → ложное «чужая», бот крутится
            # до таймаута и уходит в retry вместо download-v3 (10 картинок).
            # Как в TG-боте: после Generate ждём «ген готова» → скачивание C.
            _MIN_SEC_BEFORE_DOWNLOAD_HANDOFF = 6.0
            if prompt_id_prefix and elapsed >= _MIN_SEC_BEFORE_DOWNLOAD_HANDOFF:
                gen_idle = await self._generate_button_enabled(page)
                if gen_idle:
                    by_id = await self._find_img_by_prompt_id(
                        page, prompt_id_prefix
                    )
                    if by_id:
                        logger.info(
                            "_wait_image_url_strict: [ID] в {} последних "
                            "thumb за {:.0f} сек: {}",
                            _GALLERY_ID_SCAN_LIMIT,
                            elapsed,
                            by_id[:120],
                        )
                        return _resolve_best_download_url(
                            by_id, net_events=net_events
                        )

            # 3) diagnostic
            if elapsed - last_log > 15:
                last_log = elapsed
                n_big = len(await self._all_big_imgs(page))
                if prompt_id_prefix:
                    logger.info(
                        "_wait_image_url_strict: ждём... {:.0f} сек, "
                        "result_img={}, big_imgs={}, fallback_candidate={}",
                        elapsed,
                        (current[:80] if current else None),
                        n_big,
                        (fallback_candidate[:80] if fallback_candidate else None),
                    )
                else:
                    logger.info(
                        "_wait_image_url_strict: ждём... {:.0f} сек, "
                        "result_img_src={}, big_imgs_now={} (baseline={})",
                        elapsed,
                        (current[:80] if current else None),
                        n_big,
                        len(baseline_big_imgs),
                    )

            await sleep_cancellable(1.0, project_id)

        # timeout — все кандидаты были отвергнуты ID-верификацией
        # (или вообще не появились). Падаем с диагностикой.
        big_now = set(await self._all_big_imgs(page))
        new_big = big_now - baseline_big_imgs
        all_now_srcs = set(await self._all_img_srcs(page))
        new_dom = all_now_srcs - baseline_all_srcs
        ctx: dict[str, Any] = {
            "gen_id": gen_id,
            "baseline_result_img": baseline_result_img,
            "last_result_img_src": last_seen_result,
            "new_big_imgs": ", ".join(list(new_big)[:3]) or "—",
            "new_dom_srcs_count": len(new_dom),
            "baseline_big_imgs": len(baseline_big_imgs),
            "rejected_count": len(rejected_candidates),
        }
        if prompt_id_prefix:
            ctx["prompt_id_prefix"] = prompt_id_prefix
            ctx["id_diag"] = await self._diag_id_in_page(page, prompt_id_prefix)
            ctx["gallery_id_scan_limit"] = _GALLERY_ID_SCAN_LIMIT
            by_id = await self._find_img_by_prompt_id(page, prompt_id_prefix)
            if by_id:
                logger.warning(
                    "_wait_image_url_strict: timeout {:.0f}с, но [ID] найден "
                    "в {} последних thumb — handoff: {}",
                    timeout,
                    _GALLERY_ID_SCAN_LIMIT,
                    by_id[:120],
                )
                return _resolve_best_download_url(
                    by_id, net_events=net_events
                )
        raise OutseeImageError(
            _outsee_timeout_message(
                f"outsee image: результат не появился за {int(timeout)} сек",
                await _collect_visible_alert_snippets(page),
            ),
            context=ctx,
        )

    async def _clear_reference_upload_slots(
        self,
        page: Page,
        *,
        where: str,
        project_id: int | None = None,
    ) -> None:
        """Снимает превью и очищает все input[type=file] перед новой пачкой рефов."""
        from app.services.step_cancel import sleep_cancellable

        # 0а) Кликаем крестики на превью референсных изображений в UI outsee.
        # set_input_files([]) очищает значение input, но outsee не убирает
        # визуальный превью — нужно кликнуть кнопку X рядом с превью.
        # Best-effort: если крестик не нашёлся — продолжаем как раньше.
        cleared_ui = False
        # Сначала ищем в контейнере «Первый кадр» (start_frame секция)
        _first_frame_kws = ["Первый кадр", "First frame", "Start frame",
                            "Последний кадр", "Last frame"]
        for _kw in _first_frame_kws:
            try:
                _container = page.locator(
                    f"*:has-text('{_kw}')"
                ).last  # самый вложенный элемент с этим текстом
                if await _container.count() == 0:
                    continue
                # Ищем кнопки X/Delete внутри контейнера
                for _btn_sel in [
                    "button:has(svg.lucide-x)",
                    "button:has(svg.lucide-trash-2)",
                    "button:has(svg.lucide-trash)",
                    "button.absolute",
                    "button[aria-label*='удалит' i]",
                    "button[aria-label*='remove' i]",
                ]:
                    _btns = _container.locator(_btn_sel)
                    _n = await _btns.count()
                    if _n > 0:
                        for _bi in range(min(_n, 2)):
                            with contextlib.suppress(Exception):
                                await _btns.nth(_bi).click(timeout=1_500)
                        logger.info(
                            "outsee.{}: кликнут X на превью '{}' ({})",
                            where, _kw, _btn_sel,
                        )
                        cleared_ui = True
                        await sleep_cancellable(0.4, project_id)
                        break
                if cleared_ui:
                    break
            except Exception:  # noqa: BLE001
                continue

        if not cleared_ui:
            # Fallback: любая кнопка X рядом с img-превью в левой панели
            for _rm_sel in [
                ".relative button:has(svg.lucide-x)",
                ".relative button.absolute",
                "div[class*='upload'] button:has(svg)",
                "div[class*='frame'] button:has(svg.lucide-x)",
            ]:
                try:
                    _rm_locs = page.locator(_rm_sel)
                    _n = await _rm_locs.count()
                    if _n > 0:
                        for _bi in range(min(_n, 3)):
                            with contextlib.suppress(Exception):
                                await _rm_locs.nth(_bi).click(timeout=1_200)
                        logger.info(
                            "outsee.{}: fallback X-click на превью ({})",
                            where, _rm_sel,
                        )
                        await sleep_cancellable(0.4, project_id)
                        break
                except Exception:  # noqa: BLE001
                    continue

        # 0б) Очистка всех input[type=file] значений
        # (старый референс мог остаться от предыдущей генерации).
        try:
            base_clear = page.locator("input[type='file']")
            n_clear = await base_clear.count()
        except Exception:  # noqa: BLE001
            n_clear = 0
        if n_clear > 0:
            cleared = 0
            for i in range(n_clear):
                try:
                    await base_clear.nth(i).set_input_files([])
                    cleared += 1
                except Exception:  # noqa: BLE001
                    # некоторые input'ы могут быть недоступны для clear
                    # (например, отвязаны от формы) — пропускаем.
                    pass
            if cleared > 0:
                logger.info(
                    "outsee.{}: очищено {}/{} input[type=file] перед "
                    "загрузкой референсов",
                    where, cleared, n_clear,
                )

    async def _attach_reference_images_robust(
        self,
        page: Page,
        image_paths: list[Path],
        *,
        where: str,
        project_id: int | None = None,
    ) -> int:
        """До 2 референсов: очистка один раз, затем слоты без затирания."""
        from app.services.step_cancel import abort_if_cancelled, sleep_cancellable

        abort_if_cancelled(project_id)
        paths = [p for p in image_paths if p.exists()]
        if not paths:
            return 0

        await self._clear_reference_upload_slots(
            page, where=where, project_id=project_id
        )

        try:
            base = page.locator("input[type='file']")
            count = await base.count()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "outsee.{}: input[type=file].count() упал: {}", where, e
            )
            count = 0
        if count <= 0:
            logger.warning(
                "outsee.{}: нет input[type=file] для {} реф(ов)",
                where, len(paths),
            )
            return 0

        if len(paths) >= 2 and count == 1:
            try:
                await base.first.set_input_files([str(p) for p in paths])
                logger.info(
                    "outsee.{}: {} референсов в один input (multi-file): {}",
                    where,
                    len(paths),
                    [p.name for p in paths],
                )
                await sleep_cancellable(1.0, project_id)
                return len(paths)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "outsee.{}: multi-file в один input не сработал ({}), "
                    "пробую по слотам",
                    where,
                    e,
                )

        attached = 0
        for i, ref_path in enumerate(paths):
            slot = i if count > i else count - 1
            ok = await self._attach_ref_image_robust(
                page,
                ref_path,
                where=f"{where}[{i + 1}/{len(paths)}]",
                project_id=project_id,
                clear_before=False,
                input_index=slot,
            )
            if ok:
                attached += 1

        logger.info(
            "outsee.{}: прикреплено {}/{} референсов",
            where,
            attached,
            len(paths),
        )
        return attached

    async def _attach_ref_image_robust(
        self,
        page: Page,
        image_path: Path,
        *,
        where: str,
        project_id: int | None = None,
        prefer_first: bool = False,
        clear_before: bool = True,
        input_index: int | None = None,
    ) -> bool:
        """Робастная загрузка одного референса в input[type=file] на outsee.io.

        Для нескольких рефов используйте `_attach_reference_images_robust` —
        иначе повторная очистка input'ов затирает предыдущий файл.
        """
        from app.services.step_cancel import abort_if_cancelled, sleep_cancellable

        abort_if_cancelled(project_id)

        if clear_before:
            await self._clear_reference_upload_slots(
                page, where=where, project_id=project_id
            )

        if input_index is None:
            file_sel = await _first_visible(
                page, FILE_UPLOAD_SELECTORS, timeout_ms=2_000, project_id=project_id
            )
            if file_sel:
                try:
                    await page.locator(file_sel).first.set_input_files(
                        str(image_path)
                    )
                    logger.info(
                        "outsee.{}: reference {} загружен в видимый input ({})",
                        where, image_path.name, file_sel,
                    )
                    await sleep_cancellable(1.0, project_id)
                    return True
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "outsee.{}: видимый input set_input_files упал: {}",
                        where, e,
                    )

        try:
            base = page.locator("input[type='file']")
            count = await base.count()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "outsee.{}: locator('input[type=file]').count() упал: {}",
                where, e,
            )
            count = 0
        if count <= 0:
            logger.warning(
                "outsee.{}: input[type=file] не найден в DOM при попытке "
                "загрузить референс {}",
                where, image_path.name,
            )
            return False

        if input_index is not None:
            slot = min(max(input_index, 0), count - 1)
            target = base.nth(slot)
            slot_label = f"index={slot}"
        else:
            pick_first = prefer_first or "start_frame" in where
            target = base.first if pick_first else base.last
            slot_label = "first" if pick_first else "last"

        try:
            await target.set_input_files(str(image_path))
            logger.info(
                "outsee.{}: reference {} загружен в скрытый input "
                "(input[type=file] count={}, слот {})",
                where, image_path.name, count, slot_label,
            )
            await sleep_cancellable(1.0, project_id)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "outsee.{}: set_input_files в скрытый input упал: {} "
                "(всего input[type=file] = {})",
                where, e, count,
            )
            return False

    async def _img_is_loaded(self, page: Page, src: str) -> bool:
        """Проверяет, что `<img>` с таким src уже полностью загружена."""
        try:
            return bool(
                await page.evaluate(
                    """(src) => {
                        const img = Array.from(document.querySelectorAll('img'))
                            .find(i => i.src === src);
                        if (!img) return false;
                        return img.complete && (img.naturalWidth || 0) >= 200;
                    }""",
                    src,
                )
            )
        except Exception:  # noqa: BLE001
            return False

    async def _generate_button_enabled(self, page: Page) -> bool:
        """True, если кнопка Generate сейчас активна (генерация не идёт)."""
        try:
            sel = await _first_visible(
                page,
                GENERATE_BUTTON_SELECTORS[:4],
                timeout_ms=800,
            )
            if not sel:
                return False
            loc = page.locator(sel).first
            disabled = await loc.get_attribute("disabled")
            aria = await loc.get_attribute("aria-disabled")
            return disabled is None and (aria or "").lower() != "true"
        except Exception:  # noqa: BLE001
            return False

    async def _detect_outsee_failure(
        self,
        page: Page,
        *,
        queue_mode: bool = False,
        prompt_id_prefix: str | None = None,
    ) -> dict[str, object] | None:
        """Видимая плашка ошибки outsee: модерация или сбой генерации.

        В queue-mode ошибка часто в левой очереди внутри длинной карточки
        (промт + «запрещённое») — ищем маркеры и в коротких, и в длинных блоках.
        """
        mod_js = list(_OUTSEE_MODERATION_MARKERS)
        # busy тоже детектим (чтобы залогировать «жду»), но kind=busy → stale, не abort.
        gen_js = list(_OUTSEE_GENERATION_ERROR_MARKERS) + list(_OUTSEE_BUSY_MARKERS)
        id_token = ""
        if prompt_id_prefix:
            m = re.search(r"P\d+-F\d+-[a-f0-9]+", prompt_id_prefix, re.I)
            if m:
                id_token = m.group(0)
            elif len(prompt_id_prefix) >= 8:
                id_token = prompt_id_prefix.strip()[:40]
        try:
            raw = await page.evaluate(
                """(args) => {
                    const moderation = args.moderation;
                    const generation = args.generation;
                    const length = args.length || [];
                    const triggers = moderation.concat(generation).concat(length);
                    const queueMode = !!args.queue_mode;
                    const idToken = (args.id_token || '').trim();

                    function isTrulyVisible(el) {
                        const cs = window.getComputedStyle(el);
                        if (cs.display === 'none') return false;
                        if (cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
                        if (parseFloat(cs.opacity) === 0) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return false;
                        if (r.bottom <= 0 || r.right <= 0) return false;
                        if (r.top >= window.innerHeight) return false;
                        if (r.left >= window.innerWidth) return false;
                        let p = el.parentElement;
                        while (p) {
                            const pcs = window.getComputedStyle(p);
                            if (pcs.display === 'none') return false;
                            if (pcs.visibility === 'hidden' || pcs.visibility === 'collapse') return false;
                            if (parseFloat(pcs.opacity) === 0) return false;
                            p = p.parentElement;
                        }
                        return true;
                    }

                    function matchText(t) {
                        const low = t.toLowerCase();
                        for (const tr of triggers) {
                            if (low.includes(tr.toLowerCase())) return true;
                        }
                        return false;
                    }

                    function isVeoHistoryNoise(t) {
                        if (!t) return true;
                        if (/\\[ID:\\s*P\\d+-F\\d+/i.test(t) && /veo/i.test(t)) return true;
                        if (/^Ошибка\\s*Veo/i.test(t)) return true;
                        return false;
                    }

                    function extractSnippet(text) {
                        if (!text || isVeoHistoryNoise(text)) return null;
                        const lines = text.split(/[\\n\\r]+/);
                        for (const line of lines) {
                            const l = line.trim();
                            if (l.length < 8) continue;
                            if (l.startsWith('--no ') || l.startsWith('-- no ')) continue;
                            if (l.toLowerCase().startsWith('no text,')) continue;
                            if (matchText(l)) return l.slice(0, 320);
                        }
                        if (!matchText(text)) return null;
                        return text.trim().slice(0, 320);
                    }

                    function isInHistorySidebar(el) {
                        const r = el.getBoundingClientRect();
                        return r.left < window.innerWidth * 0.36
                            && r.width < window.innerWidth * 0.5;
                    }

                    function idMatches(text) {
                        if (!idToken || idToken.length < 6) return true;
                        const frame = idToken.match(/P\\d+-F\\d+-[a-f0-9]+/i);
                        const core = frame ? frame[0] : idToken;
                        const low = text.toLowerCase();
                        if (low.includes(core.toLowerCase())) return true;
                        return low.includes('[id: ' + core.toLowerCase())
                            || low.includes('[id:' + core.toLowerCase());
                    }

                    function scanRoot(root, inResult, opts) {
                        const scanSidebar = opts && opts.scanSidebar;
                        if (!root) return null;
                        let best = null;
                        let bestLen = Infinity;
                        for (const el of root.querySelectorAll('*')) {
                            const tag = (el.tagName || '').toLowerCase();
                            if (tag === 'textarea' || tag === 'input' || tag === 'script' || tag === 'style' || tag === 'template') continue;
                            if (!isTrulyVisible(el)) continue;
                            if (!scanSidebar && !inResult && isInHistorySidebar(el)) continue;
                            const raw = (el.textContent || '').trim();
                            if (!raw || raw.length < 8) continue;
                            if (!inResult && !idMatches(raw)) continue;
                            const snippet = extractSnippet(raw);
                            if (!snippet) continue;
                            if (snippet.length < bestLen) {
                                best = { text: snippet, in_result: inResult };
                                bestLen = snippet.length;
                            }
                        }
                        return best;
                    }

                    function findResultRoot() {
                        const kws = ['Результат генерации', 'Результат', 'Result'];
                        let best = null;
                        let bestArea = 0;
                        for (const el of document.querySelectorAll('section, div, article, main')) {
                            const t = (el.textContent || '').trim();
                            if (!t || t.length > 1200) continue;
                            for (const kw of kws) {
                                if (!t.includes(kw)) continue;
                                const r = el.getBoundingClientRect();
                                const area = r.width * r.height;
                                if (area > bestArea && r.width >= 120 && r.height >= 60) {
                                    best = el;
                                    bestArea = area;
                                }
                            }
                        }
                        return best;
                    }

                    const resultRoot = findResultRoot();
                    if (resultRoot) {
                        const inPanel = scanRoot(resultRoot, true, { scanSidebar: false });
                        if (inPanel) return inPanel;
                    }
                    if (queueMode) {
                        const inQueue = scanRoot(document.body, false, { scanSidebar: true });
                        if (inQueue) return inQueue;
                    }
                    return scanRoot(document.body, false, { scanSidebar: false });
                }""",
                {
                    "moderation": mod_js,
                    "generation": gen_js,
                    "length": list(_OUTSEE_LENGTH_MARKERS),
                    "queue_mode": queue_mode,
                    "id_token": id_token,
                },
            )
            if isinstance(raw, dict) and raw.get("text"):
                text = str(raw["text"]).strip()
                if text and not _outsee_failure_text_is_noise(text):
                    return {
                        "text": text,
                        "in_result": bool(raw.get("in_result")),
                    }
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _detect_queue_card_failure(
        self,
        page: Page,
        *,
        prompt_id_prefix: str,
    ) -> dict[str, object] | None:
        """Ошибка в карточке очереди: ID в одном узле, «Контент отклонён» — в соседнем."""
        core = _prompt_id_core_token(prompt_id_prefix)
        if not core:
            return None
        mod_js = list(_OUTSEE_MODERATION_MARKERS)
        # busy тоже детектим (чтобы залогировать «жду»), но kind=busy → stale, не abort.
        gen_js = list(_OUTSEE_GENERATION_ERROR_MARKERS) + list(_OUTSEE_BUSY_MARKERS)
        try:
            raw = await page.evaluate(
                """(args) => {
                    const core = (args.core || '').trim();
                    const moderation = args.moderation;
                    const generation = args.generation;
                    const length = args.length || [];
                    const triggers = moderation.concat(generation).concat(length);
                    if (!core) return null;

                    function isTrulyVisible(el) {
                        const cs = window.getComputedStyle(el);
                        if (cs.display === 'none') return false;
                        if (cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
                        if (parseFloat(cs.opacity) === 0) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return false;
                        if (r.bottom <= 0 || r.right <= 0) return false;
                        if (r.top >= window.innerHeight) return false;
                        if (r.left >= window.innerWidth) return false;
                        let p = el.parentElement;
                        while (p) {
                            const pcs = window.getComputedStyle(p);
                            if (pcs.display === 'none') return false;
                            if (pcs.visibility === 'hidden' || pcs.visibility === 'collapse') return false;
                            if (parseFloat(pcs.opacity) === 0) return false;
                            p = p.parentElement;
                        }
                        return true;
                    }

                    function matchText(t) {
                        const low = t.toLowerCase();
                        for (const tr of triggers) {
                            if (low.includes(tr.toLowerCase())) return true;
                        }
                        return false;
                    }

                    function extractSnippet(text) {
                        if (!text) return null;
                        const lines = text.split(/[\\n\\r]+/);
                        for (const line of lines) {
                            const l = line.trim();
                            if (l.length < 8) continue;
                            if (l.startsWith('--no ') || l.startsWith('-- no ')) continue;
                            if (l.toLowerCase().startsWith('no text,')) continue;
                            if (matchText(l)) return l.slice(0, 320);
                        }
                        if (!matchText(text)) return null;
                        return text.trim().slice(0, 320);
                    }

                    function foreignIdCount(text) {
                        const all = text.match(/P\\d+-F\\d+-[a-f0-9]+/gi) || [];
                        const uniq = [...new Set(all.map((s) => s.toLowerCase()))];
                        return uniq.filter((id) => id !== core.toLowerCase()).length;
                    }

                    let anchor = null;
                    let anchorLen = Infinity;
                    const coreLow = core.toLowerCase();
                    for (const el of document.querySelectorAll('*')) {
                        const tag = (el.tagName || '').toLowerCase();
                        if (tag === 'textarea' || tag === 'input' || tag === 'script' || tag === 'style' || tag === 'template') continue;
                        if (!isTrulyVisible(el)) continue;
                        const t = (el.textContent || '').trim();
                        if (!t || !t.toLowerCase().includes(coreLow)) continue;
                        if (t.length >= anchorLen) continue;
                        anchor = el;
                        anchorLen = t.length;
                    }
                    if (!anchor) return null;

                    let card = anchor;
                    for (let depth = 0; depth < 10 && card.parentElement; depth++) {
                        const parent = card.parentElement;
                        const pt = (parent.textContent || '').trim();
                        if (!pt || pt.length > 5000) break;
                        if (foreignIdCount(pt) > 1) break;
                        card = parent;
                    }

                    let best = null;
                    let bestLen = Infinity;
                    for (const el of card.querySelectorAll('*')) {
                        const tag = (el.tagName || '').toLowerCase();
                        if (tag === 'textarea' || tag === 'input' || tag === 'script' || tag === 'style' || tag === 'template') continue;
                        if (!isTrulyVisible(el)) continue;
                        const raw = (el.textContent || '').trim();
                        if (!raw || raw.length < 8 || raw.length > 800) continue;
                        const snippet = extractSnippet(raw);
                        if (!snippet) continue;
                        if (snippet.length < bestLen) {
                            best = snippet;
                            bestLen = snippet.length;
                        }
                    }
                    if (!best) return null;
                    const r = card.getBoundingClientRect();
                    const inResult = r.left >= window.innerWidth * 0.34;
                    return { text: best, in_result: inResult };
                }""",
                {
                    "core": core,
                    "moderation": mod_js,
                    "generation": gen_js,
                    "length": list(_OUTSEE_LENGTH_MARKERS),
                },
            )
            if isinstance(raw, dict) and raw.get("text"):
                text = str(raw["text"]).strip()
                if text and not _outsee_failure_text_is_noise(text):
                    return {
                        "text": text,
                        "in_result": bool(raw.get("in_result")),
                        "queue_card": True,
                    }
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _outsee_failure_text(self, page: Page) -> str | None:
        """Текст видимой плашки ошибки (любой kind) или None."""
        hit = await self._detect_outsee_failure(page)
        if hit:
            return str(hit["text"])
        return None

    async def _collect_outsee_failure_texts(
        self, page: Page, *, exclude_moderation: bool = False
    ) -> frozenset[str]:
        """Все видимые плашки ошибок на странице (baseline перед Generate)."""
        mod_js = list(_OUTSEE_MODERATION_MARKERS)
        # busy тоже детектим (чтобы залогировать «жду»), но kind=busy → stale, не abort.
        gen_js = list(_OUTSEE_GENERATION_ERROR_MARKERS) + list(_OUTSEE_BUSY_MARKERS)
        try:
            raw = await page.evaluate(
                """(markers) => {
                    const moderation = markers.moderation;
                    const generation = markers.generation;
                    const length = markers.length || [];
                    const triggers = moderation.concat(generation).concat(length);
                    const out = [];
                    const seen = new Set();

                    function isTrulyVisible(el) {
                        const cs = window.getComputedStyle(el);
                        if (cs.display === 'none') return false;
                        if (cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
                        if (parseFloat(cs.opacity) === 0) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return false;
                        if (r.bottom <= 0 || r.right <= 0) return false;
                        if (r.top >= window.innerHeight) return false;
                        if (r.left >= window.innerWidth) return false;
                        let p = el.parentElement;
                        while (p) {
                            const pcs = window.getComputedStyle(p);
                            if (pcs.display === 'none') return false;
                            if (pcs.visibility === 'hidden' || pcs.visibility === 'collapse') return false;
                            if (parseFloat(pcs.opacity) === 0) return false;
                            p = p.parentElement;
                        }
                        return true;
                    }

                    function matchText(t) {
                        const low = t.toLowerCase();
                        for (const tr of triggers) {
                            if (low.includes(tr.toLowerCase())) return true;
                        }
                        return false;
                    }

                    function isHistoryNoise(t) {
                        if (!t || t.length > 280) return true;
                        if (/\\[ID:\\s*P\\d+-F\\d+/i.test(t) && /veo/i.test(t)) return true;
                        if (/^Ошибка\\s*Veo/i.test(t)) return true;
                        return false;
                    }

                    function isInHistorySidebar(el) {
                        const r = el.getBoundingClientRect();
                        return r.left < window.innerWidth * 0.36
                            && r.width < window.innerWidth * 0.5;
                    }

                    function scanRootAll(root, inResult) {
                        if (!root) return;
                        for (const el of root.querySelectorAll('*')) {
                            const tag = (el.tagName || '').toLowerCase();
                            if (tag === 'textarea' || tag === 'input' || tag === 'script' || tag === 'style' || tag === 'template') continue;
                            const t = (el.textContent || '').trim();
                            if (!t || t.length > 1000) continue;
                            if (isHistoryNoise(t)) continue;
                            if (!inResult && isInHistorySidebar(el)) continue;
                            if (!matchText(t)) continue;
                            if (!isTrulyVisible(el)) continue;
                            const key = t.slice(0, 300);
                            if (seen.has(key)) continue;
                            seen.add(key);
                            out.push({ text: key, in_result: inResult });
                        }
                    }

                    function findResultRoot() {
                        const kws = ['Результат генерации', 'Результат', 'Result'];
                        let best = null;
                        let bestArea = 0;
                        for (const el of document.querySelectorAll('section, div, article, main')) {
                            const t = (el.textContent || '').trim();
                            if (!t || t.length > 1200) continue;
                            for (const kw of kws) {
                                if (!t.includes(kw)) continue;
                                const r = el.getBoundingClientRect();
                                const area = r.width * r.height;
                                if (area > bestArea && r.width >= 120 && r.height >= 60) {
                                    best = el;
                                    bestArea = area;
                                }
                            }
                        }
                        return best;
                    }

                    const resultRoot = findResultRoot();
                    if (resultRoot) scanRootAll(resultRoot, true);
                    scanRootAll(document.body, false);
                    return out;
                }""",
                {"moderation": mod_js, "generation": gen_js, "length": list(_OUTSEE_LENGTH_MARKERS)},
            )
            if isinstance(raw, list):
                texts: set[str] = set()
                for item in raw:
                    if not isinstance(item, dict) or not item.get("text"):
                        continue
                    raw_text = str(item["text"])
                    if _outsee_failure_text_is_noise(raw_text):
                        continue
                    if exclude_moderation and _outsee_failure_kind(raw_text) == "moderation":
                        continue
                    norm = _normalize_outsee_failure_text(raw_text)
                    if norm:
                        texts.add(norm)
                return frozenset(texts)
        except Exception:  # noqa: BLE001
            pass
        hit = await self._detect_outsee_failure(page)
        if hit:
            raw_text = str(hit["text"])
            if exclude_moderation and _outsee_failure_kind(raw_text) == "moderation":
                return frozenset()
            return frozenset({_normalize_outsee_failure_text(raw_text)})
        return frozenset()

    # ----- VIDEO (veo-3-fast Relax) -----

    async def generate_video(
        self,
        prompt: str,
        out_path: Path,
        *,
        start_frame: Path | None = None,
        aspect_ratio: str = "9:16",
        timeout: float = 900,
        gen_id: str | None = None,
        model_slug: str | None = None,
        resolution: str | None = None,
        quality: str | None = None,
        relax: bool = False,
        prompt_id_prefix: str | None = None,
        project_id: int | None = None,
        duplicate_check_paths: list[Path] | None = None,
    ) -> GenerationResult:
        import uuid as _uuid

        from app.services.step_cancel import abort_if_cancelled

        abort_if_cancelled(project_id)
        gen_id = gen_id or _uuid.uuid4().hex

        http_res = await self._try_http_video(
            prompt=prompt,
            out_path=out_path,
            model_slug=model_slug,
            aspect_ratio=aspect_ratio,
            resolution=resolution or "720p",
            use_unlimited=bool(relax),
            prompt_id_prefix=prompt_id_prefix,
            timeout=float(timeout),
            gen_id=gen_id,
            project_id=project_id,
            reference_image=start_frame,
        )
        if http_res is not None:
            return http_res

        dup_refs = [
            p for p in (duplicate_check_paths or []) if isinstance(p, Path) and p.is_file()
        ]
        if prompt_id_prefix:
            from app.generation_options import prepend_gen_id

            prompt = prepend_gen_id(prompt, prompt_id_prefix)
            mode = "queue" if _outsee_queue_mode() else "gallery-id"
            logger.info(
                "outsee.generate_video: prompt_id_prefix={} mode={}",
                prompt_id_prefix,
                mode,
            )
        _verify_prompt_length_before_send(prompt, where="generate_video")

        from app.services.outsee_lane import outsee_lane

        async with outsee_lane(project_id=project_id, op="generate_video"):
            page_url = _video_page_url(model_slug)
            logger.info(
                "outsee.generate_video: открываю страницу gen_id={} url={}",
                gen_id[:8],
                page_url,
            )
            page = await self.session.open_page(page_url, reuse=True)
            from app.services.step_cancel import (
                register_active_page,
                unregister_active_page,
            )

            if project_id is not None:
                register_active_page(project_id, page)
            try:
                return await self._generate_video_on_page(
                    page,
                    prompt=prompt,
                    out_path=out_path,
                    start_frame=start_frame,
                    aspect_ratio=aspect_ratio,
                    timeout=timeout,
                    gen_id=gen_id,
                    model_slug=model_slug,
                    resolution=resolution,
                    relax=relax,
                    prompt_id_prefix=prompt_id_prefix,
                    project_id=project_id,
                    page_url=page_url,
                    duplicate_check_paths=dup_refs,
                )
            finally:
                if project_id is not None:
                    unregister_active_page(project_id)

    async def _generate_video_on_page(
        self,
        page: Any,
        *,
        prompt: str,
        out_path: Path,
        start_frame: Path | None,
        aspect_ratio: str,
        timeout: float,
        gen_id: str,
        model_slug: str | None,
        resolution: str | None,
        relax: bool,
        prompt_id_prefix: str | None,
        project_id: int | None,
        page_url: str,
        duplicate_check_paths: list[Path] | None = None,
    ) -> GenerationResult:
        """Тело generate_video — зеркало _generate_image_on_page."""
        import time as _time

        from app.services.step_cancel import (
            StepCancelledError,
            abort_if_cancelled,
            await_with_cancel,
            sleep_cancellable,
        )

        abort_if_cancelled(project_id)
        dumps: list[Path] = []
        try:
            await await_with_cancel(
                page.goto(page_url, wait_until="domcontentloaded"), project_id
            )
        except StepCancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "outsee.generate_video: page.goto({}) упал: {} — продолжаю "
                "без явного reload",
                page_url,
                e,
            )
        await await_with_cancel(page.wait_for_load_state("domcontentloaded"), project_id)
        try:
            await await_with_cancel(
                page.wait_for_load_state("networkidle", timeout=15_000), project_id
            )
        except StepCancelledError:
            raise
        except Exception:
            pass
        abort_if_cancelled(project_id)
        logger.info("outsee.generate_video: страница готова")

        baseline_video_urls = {
            _strip_url_query(u)
            for u in await self._all_video_urls_on_page(page)
            if u and _video_url_looks_like_result(u)
        }
        logger.info(
            "outsee.generate_video: baseline video_urls={}",
            len(baseline_video_urls),
        )

        click_ts = _time.monotonic()
        net_events: list[tuple[float, str]] = []

        def _on_response(resp: Any) -> None:
            try:
                if not _is_candidate_video_response(resp):
                    return
                net_events.append((_time.monotonic() - click_ts, resp.url))
            except Exception:  # noqa: BLE001
                pass

        page.on("response", _on_response)

        try:
            pre_rejected_text: str | None = None

            input_sel = await _first_visible(
                page, PROMPT_INPUT_SELECTORS, timeout_ms=60_000, project_id=project_id
            )
            if not input_sel:
                h, p = await _dump_page(page, "video_prompt_input_notfound")
                for x in (h, p):
                    if x:
                        dumps.append(x)
                raise OutseeImageError(
                    "outsee video: не найден ввод промта",
                    context={"gen_id": gen_id},
                    dumps=dumps,
                )
            prompt_loc = page.locator(input_sel).first
            try:
                await await_with_cancel(
                    prompt_loc.scroll_into_view_if_needed(timeout=5_000),
                    project_id,
                )
            except Exception:  # noqa: BLE001
                pass

            # --- Порядок veo: кадр → настройки (physical) → Relax → промт → Generate
            if start_frame is not None:
                if not start_frame.exists():
                    raise OutseeImageError(
                        f"outsee video: start_frame не найден: {start_frame}",
                        context={"gen_id": gen_id},
                        dumps=dumps,
                    )
                attached = await self._attach_ref_image_robust(
                    page,
                    start_frame,
                    where="generate_video[start_frame]",
                    project_id=project_id,
                    prefer_first=True,
                )
                if not attached:
                    h, p = await _dump_page(page, "video_start_frame_notfound")
                    for x in (h, p):
                        if x:
                            dumps.append(x)
                    raise OutseeImageError(
                        "outsee video: не удалось загрузить стартовый кадр",
                        context={"gen_id": gen_id},
                        dumps=dumps,
                    )
                await sleep_cancellable(1.0, project_id)
                logger.info("outsee.generate_video: стартовый кадр загружен")

            if aspect_ratio:
                await _select_aspect_ratio(
                    page,
                    aspect_ratio,
                    where="generate_video",
                    dumps=dumps,
                    project_id=project_id,
                )
                asp_sel = await _first_visible(
                    page,
                    _aspect_selectors(aspect_ratio),
                    timeout_ms=2_000,
                    project_id=project_id,
                )
                if asp_sel:
                    try:
                        await _physical_mouse_click(
                            page,
                            page.locator(asp_sel).first,
                            project_id=project_id,
                            label=f"aspect {aspect_ratio}",
                        )
                    except Exception:  # noqa: BLE001
                        pass

            if resolution:
                res_sel = await _first_visible(
                    page,
                    _resolution_selectors(resolution),
                    timeout_ms=5_000,
                    project_id=project_id,
                )
                if res_sel:
                    try:
                        await _physical_mouse_click(
                            page,
                            page.locator(res_sel).first,
                            project_id=project_id,
                            label=f"resolution {resolution}",
                        )
                        logger.info(
                            "outsee.generate_video: {} physical ({})",
                            resolution,
                            res_sel,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "outsee.generate_video: resolution {}: {}",
                            resolution,
                            e,
                        )

            try:
                await self._ensure_relax_for_video(
                    page,
                    want_on=relax,
                    where="generate_video",
                    project_id=project_id,
                    dumps=dumps,
                )
            except OutseeImageError:
                logger.warning(
                    "outsee.generate_video: Relax не включился — "
                    "продолжаю без него"
                )
            abort_if_cancelled(project_id)

            await await_with_cancel(prompt_loc.click(), project_id)
            await await_with_cancel(prompt_loc.fill(prompt), project_id)
            try:
                await await_with_cancel(prompt_loc.press("Tab"), project_id)
            except Exception:  # noqa: BLE001
                pass
            await _verify_composer_prompt_filled(
                page,
                input_sel,
                expected_prompt=prompt,
                prompt_id_prefix=prompt_id_prefix,
                where="generate_video",
            )
            actual_len = len(await _read_composer_prompt_value(page, input_sel))
            logger.info(
                "outsee.generate_video: промт в поле ввода (отправлено {} симв, "
                "в textarea {} симв)",
                len(prompt),
                actual_len,
            )
            abort_if_cancelled(project_id)

            gen_sel = await _first_visible(
                page,
                GENERATE_BUTTON_SELECTORS,
                timeout_ms=15_000,
                project_id=project_id,
            )
            if not gen_sel:
                h, p = await _dump_page(page, "video_generate_notfound_pre")
                for x in (h, p):
                    if x:
                        dumps.append(x)
                raise OutseeImageError(
                    "outsee video: кнопка Generate не найдена до клика",
                    context={"gen_id": gen_id},
                    dumps=dumps,
                )
            logger.info(
                "outsee.generate_video: Generate на экране ({})", gen_sel
            )
            try:
                await self._wait_button_enabled(
                    page, gen_sel, timeout_s=180, project_id=project_id
                )
            except Exception as e:  # noqa: BLE001
                raise OutseeImageError(
                    "outsee video: Generate не стала активной (промт/кадр/Relax?)",
                    context={
                        "gen_id": gen_id,
                        "gen_sel": gen_sel,
                        "relax": relax,
                        "err": str(e),
                    },
                    dumps=dumps,
                ) from e

            baseline_video_urls = {
                _strip_url_query(u)
                for u in await self._all_video_urls_on_page(page)
                if u and _video_url_looks_like_result(u)
            }
            logger.info(
                "outsee.generate_video: re-baseline перед Generate video_urls={}",
                len(baseline_video_urls),
            )

            baseline_failure_texts = await self._collect_outsee_failure_texts(
                page, exclude_moderation=True
            )
            pre_hit = await self._detect_outsee_failure(
                page,
                queue_mode=_outsee_queue_mode(),
                prompt_id_prefix=prompt_id_prefix,
            )
            pre_rejected_text = _normalize_pre_failure_baseline(
                str(pre_hit["text"]) if pre_hit else None,
                prompt_id_prefix=prompt_id_prefix,
            )
            if baseline_failure_texts:
                logger.info(
                    "outsee.generate_video: pre-click failure baseline "
                    "({} плашек, без модерации)",
                    len(baseline_failure_texts),
                )

            click_ts = _time.monotonic()
            net_events.clear()
            await self._trigger_generate_video(
                page,
                input_sel=input_sel,
                project_id=project_id,
                dumps=dumps,
                context={"gen_id": gen_id, "prompt_id": prompt_id_prefix},
            )
            logger.info(
                "outsee.generate_video: Generate запущен, жду ролик "
                "(gen_id={})",
                gen_id[:8],
            )

            queue_mode = _outsee_queue_mode()
            if queue_mode:
                logger.info(
                    "outsee.generate_video: queue-mode — ждём один новый "
                    "ролик (ID={}), gen_id={}",
                    "да" if prompt_id_prefix else "нет",
                    gen_id[:8],
                )

            from app.services.video_duplicate import find_duplicate_reference

            dup_refs: list[Path] = list(duplicate_check_paths or [])
            rejected_video_urls: set[str] = set()
            video_url: str | None = None
            pick_timeout = max(90.0, timeout / max(_VIDEO_PICK_ATTEMPTS, 1))

            for pick_attempt in range(1, _VIDEO_PICK_ATTEMPTS + 1):
                abort_if_cancelled(project_id)
                try:
                    video_url = await self._wait_video_url_strict(
                        page,
                        timeout=pick_timeout,
                        baseline_video_urls=baseline_video_urls,
                        net_events=net_events,
                        gen_id=gen_id,
                        baseline_failure_texts=baseline_failure_texts,
                        pre_rejected_text=pre_rejected_text,
                        prompt_id_prefix=prompt_id_prefix,
                        project_id=project_id,
                        queue_mode=queue_mode,
                        rejected_video_urls=rejected_video_urls,
                        prompt_len=len(prompt),
                    )
                except (OutseeContentRejectedError, OutseePromptTooLongError) as e:
                    e.dumps = list(dumps)
                    raise
                except OutseeImageError as e:
                    if pick_attempt < _VIDEO_PICK_ATTEMPTS and rejected_video_urls:
                        logger.warning(
                            "outsee.generate_video: pick {}/{} wait failed "
                            "({}), пробую другой ролик",
                            pick_attempt,
                            _VIDEO_PICK_ATTEMPTS,
                            e.reason,
                        )
                        continue
                    h, p = await _dump_page(page, "video_timeout")
                    for x in (h, p):
                        if x:
                            dumps.append(x)
                    e.dumps = list(dumps)
                    raise

                out_path.parent.mkdir(parents=True, exist_ok=True)
                last_dl_err: OutseeImageError | None = None
                for dl_attempt in range(1, _VIDEO_DOWNLOAD_ATTEMPTS + 1):
                    try:
                        await self._download_video_result(
                            page,
                            video_url=video_url,
                            out_path=out_path,
                            gen_id=gen_id,
                            prompt_id_prefix=prompt_id_prefix,
                            project_id=project_id,
                        )
                        _validate_downloaded_video(
                            out_path, gen_id=gen_id, video_url=video_url
                        )
                        last_dl_err = None
                        break
                    except OutseeImageError as e:
                        e.context.setdefault("gen_id", gen_id)
                        e.context.setdefault("video_url", video_url)
                        e.dumps = list(dumps)
                        last_dl_err = e
                        if dl_attempt < _VIDEO_DOWNLOAD_ATTEMPTS:
                            logger.warning(
                                "outsee.generate_video: download {}/{} failed: {}",
                                dl_attempt,
                                _VIDEO_DOWNLOAD_ATTEMPTS,
                                e.reason,
                            )
                            await sleep_cancellable(2.0, project_id)
                if last_dl_err is not None:
                    rejected_video_urls.add(_strip_url_query(video_url))
                    with contextlib.suppress(OSError):
                        out_path.unlink(missing_ok=True)
                    if pick_attempt < _VIDEO_PICK_ATTEMPTS:
                        logger.warning(
                            "outsee.generate_video: pick {}/{} download failed, "
                            "ищу другой URL",
                            pick_attempt,
                            _VIDEO_PICK_ATTEMPTS,
                        )
                        continue
                    raise OutseeDownloadError(
                        last_dl_err.reason,
                        context=dict(last_dl_err.context),
                        dumps=list(dumps),
                    ) from last_dl_err

                dup_of = await find_duplicate_reference(out_path, dup_refs)
                if dup_of is not None:
                    rejected_video_urls.add(_strip_url_query(video_url))
                    with contextlib.suppress(OSError):
                        out_path.unlink(missing_ok=True)
                    logger.warning(
                        "outsee.generate_video: pick {}/{} — дубликат {} "
                        "(url={})",
                        pick_attempt,
                        _VIDEO_PICK_ATTEMPTS,
                        dup_of.name,
                        video_url[:100],
                    )
                    if pick_attempt < _VIDEO_PICK_ATTEMPTS:
                        await sleep_cancellable(1.5, project_id)
                        continue
                    raise OutseeDuplicateVideoError(
                        "outsee video: скачан дубликат предыдущего ролика",
                        context={
                            "gen_id": gen_id,
                            "video_url": video_url,
                            "duplicate_of": str(dup_of),
                        },
                        dumps=list(dumps),
                    )
                break
            else:
                raise OutseeDuplicateVideoError(
                    "outsee video: не найден уникальный ролик в outsee",
                    context={"gen_id": gen_id, "rejected_urls": len(rejected_video_urls)},
                    dumps=list(dumps),
                )
        finally:
            try:
                page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        if video_url is None:
            raise OutseeImageError(
                "outsee video: URL ролика не получен",
                context={"gen_id": gen_id},
                dumps=dumps,
            )

        failure_after = await self._detect_outsee_failure(page)
        if failure_after:
            ftext = failure_after["text"]
            in_result = bool(failure_after.get("in_result"))
            is_new = (
                in_result
                or not pre_rejected_text
                or ftext != pre_rejected_text
            )
            if is_new:
                logger.warning(
                    "outsee.generate_video: post-success failure banner ignored "
                    "(in_result={}, kind={}, gen_id={})",
                    in_result,
                    _outsee_failure_kind(ftext),
                    gen_id[:8],
                )

        logger.info("outsee video saved → {} (gen_id={})", out_path, gen_id[:8])
        gen_sel_done = await _first_visible(
            page, GENERATE_BUTTON_SELECTORS, timeout_ms=5_000, project_id=project_id
        )
        if gen_sel_done:
            try:
                await self._wait_button_enabled(
                    page, gen_sel_done, timeout_s=120, project_id=project_id
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "outsee.generate_video: Generate не стал активным после "
                    "скачивания — продолжаю"
                )
        return GenerationResult(
            file_path=out_path,
            raw_url=video_url,
            gen_id=gen_id,
            dumps=dumps or None,
        )

    async def _download_video_result(
        self,
        page: Page,
        *,
        video_url: str,
        out_path: Path,
        gen_id: str,
        prompt_id_prefix: str | None,
        project_id: int | None,
    ) -> None:
        if _outsee_queue_mode():
            await _download_via_queue_video_result(
                page,
                video_url=video_url,
                out_path=out_path,
                gen_id=gen_id,
                project_id=project_id,
            )
        elif prompt_id_prefix:
            await _download_via_video_card_click(
                page,
                prompt_id_prefix=prompt_id_prefix,
                out_path=out_path,
                video_url=video_url,
                project_id=project_id,
            )
        else:
            await _download_via_context(
                page, video_url, out_path, project_id=project_id
            )

    async def retry_video_download(
        self,
        *,
        video_url: str,
        out_path: Path,
        gen_id: str,
        prompt_id_prefix: str | None = None,
        project_id: int | None = None,
        model_slug: str | None = None,
    ) -> GenerationResult:
        """Повтор скачивания без нового Generate (ролик уже в outsee)."""
        from app.services.outsee_lane import outsee_lane
        from app.services.step_cancel import abort_if_cancelled

        abort_if_cancelled(project_id)
        page_url = _video_page_url(model_slug)
        async with outsee_lane(project_id=project_id, op="retry_video_download"):
            page = await self.session.open_page(page_url, reuse=True)
            try:
                await self._download_video_result(
                    page,
                    video_url=video_url,
                    out_path=out_path,
                    gen_id=gen_id,
                    prompt_id_prefix=prompt_id_prefix,
                    project_id=project_id,
                )
                _validate_downloaded_video(
                    out_path, gen_id=gen_id, video_url=video_url
                )
            except OutseeImageError as e:
                e.context.setdefault("gen_id", gen_id)
                e.context.setdefault("video_url", video_url)
                raise OutseeDownloadError(
                    e.reason,
                    context=dict(e.context),
                ) from e
        out_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "outsee retry_video_download saved → {} (gen_id={})",
            out_path,
            gen_id[:8],
        )
        return GenerationResult(
            file_path=out_path,
            raw_url=video_url,
            gen_id=gen_id,
        )

    async def _find_video_by_prompt_id(
        self,
        page: Page,
        id_token: str,
        *,
        max_levels: int = 12,
    ) -> str | None:
        """Зеркало `_find_img_by_prompt_id`, но для `<video>` / `<source>` /
        `a[download]`. Ищет в DOM карточку с нашим `[ID: ...]` и возвращает
        URL ближайшего видео-элемента.

        Порядок матчинга такой же:
          1) полный `[ID: P*-F*-xxxxxxxx]`,
          2) `P*-F*-xxxxxxxx` (без скобок и `ID:`),
          3) 8-hex-tail (`xxxxxxxx`).
        """
        tokens: list[str] = [id_token]
        m = re.search(r"\[ID:\s*([A-Za-z0-9_-]+)\s*\]", id_token)
        if m:
            inner = m.group(1)
            if inner not in tokens:
                tokens.append(inner)
        m2 = re.search(r"-([0-9a-fA-F]{8})\]?$", id_token)
        if m2:
            tail = m2.group(1)
            if tail and tail not in tokens:
                tokens.append(tail)

        js = """
        ([tokens, maxLevels]) => {
            const hasToken = (el, idToken) => {
                if (!el) return false;
                const t = (el.innerText || el.textContent || '');
                if (t.includes(idToken)) return true;
                const tag = el.tagName && el.tagName.toLowerCase();
                if (tag === 'textarea' || tag === 'input') {
                    const v = el.value || '';
                    if (v.includes(idToken)) return true;
                }
                return false;
            };
            const pickVideoSrc = (root) => {
                // Сначала <video src>, потом <source src>, потом
                // a[download] с .mp4 — последний наименее надёжен,
                // зато подхватывает уже-готовый скачиваемый ролик.
                const videos = root.querySelectorAll('video');
                for (const v of videos) {
                    if (v.src && !v.src.startsWith('data:')) return v.src;
                    const sources = v.querySelectorAll('source');
                    for (const s of sources) {
                        if (s.src && !s.src.startsWith('data:')) return s.src;
                    }
                }
                const links = root.querySelectorAll('a[download], a[href*=".mp4"]');
                for (const a of links) {
                    if (a.href && !a.href.startsWith('data:')) return a.href;
                }
                return null;
            };
            for (const idToken of tokens) {
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    if (!el || !el.children) continue;
                    if (el === document.body || el === document.documentElement) continue;
                    const tag0 = el.tagName && el.tagName.toLowerCase();
                    if (tag0 === 'textarea' || tag0 === 'input') continue;
                    if (!hasToken(el, idToken)) continue;
                    let smallest = el;
                    for (const child of el.children) {
                        if (hasToken(child, idToken)) { smallest = null; break; }
                    }
                    if (smallest) {
                        const deepInputs = el.querySelectorAll('textarea, input');
                        for (const di of deepInputs) {
                            if (di === el) continue;
                            const v = di.value || '';
                            if (v.includes(idToken)) { smallest = null; break; }
                        }
                    }
                    if (!smallest) continue;
                    let cur = smallest;
                    for (let i = 0; i < maxLevels && cur; i++) {
                        const src = pickVideoSrc(cur);
                        if (src) return src;
                        cur = cur.parentElement;
                    }
                }
            }
            return null;
        }
        """
        try:
            res = await page.evaluate(js, [tokens, max_levels])
            if isinstance(res, str) and res:
                return res
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "_find_video_by_prompt_id: ошибка JS-поиска: {}", e
            )
            return None

    async def _all_video_urls_on_page(self, page: Page) -> list[str]:
        try:
            urls = await page.evaluate(
                """() => {
                    const list = [];
                    document.querySelectorAll('video').forEach(v => {
                        if (v.src) list.push(v.src);
                        v.querySelectorAll('source').forEach(s => {
                            if (s.src) list.push(s.src);
                        });
                    });
                    document.querySelectorAll("a[download], a[href*='.mp4']").forEach(a => {
                        if (a.href) list.push(a.href);
                    });
                    return list;
                }"""
            )
            return [u for u in (urls or []) if isinstance(u, str) and u]
        except Exception:  # noqa: BLE001
            return []

    async def _wait_video_url_strict(
        self,
        page: Page,
        *,
        timeout: float,
        baseline_video_urls: set[str],
        net_events: list[tuple[float, str]] | None = None,
        gen_id: str,
        baseline_failure_texts: frozenset[str] | None = None,
        pre_rejected_text: str | None = None,
        prompt_id_prefix: str | None = None,
        project_id: int | None = None,
        queue_mode: bool = False,
        rejected_video_urls: set[str] | None = None,
        prompt_len: int | None = None,
    ) -> str:
        """Жёсткое ожидание свежего ролика — зеркало _wait_image_url_strict."""
        start = asyncio.get_event_loop().time()
        deadline = start + timeout
        last_log = 0.0
        fallback_candidate: str | None = None
        fallback_source: str | None = None
        rejected_candidates: set[str] = {
            _strip_url_query(u) for u in (rejected_video_urls or set()) if u
        }
        _MIN_SEC_BEFORE_HANDOFF = 6.0

        from app.services.step_cancel import abort_if_cancelled, sleep_cancellable

        failure_baseline = baseline_failure_texts or frozenset()
        if pre_rejected_text and not failure_baseline:
            failure_baseline = frozenset(
                {_normalize_outsee_failure_text(pre_rejected_text)}
            )
        stale_logged: set[str] = set()

        while asyncio.get_event_loop().time() < deadline:
            abort_if_cancelled(project_id)
            now = asyncio.get_event_loop().time()
            elapsed = now - start

            if prompt_id_prefix:
                by_id = await self._find_video_by_prompt_id(
                    page, prompt_id_prefix
                )
                if by_id and _video_url_looks_like_result(by_id):
                    by_id_norm = _strip_url_query(by_id)
                    fresh_ok = by_id_norm not in baseline_video_urls
                    if fresh_ok and by_id_norm not in rejected_candidates:
                        if (not net_events) or _url_is_fresh(by_id, net_events):
                            logger.info(
                                "_wait_video_url_strict: matched by prompt_id "
                                "{} за {:.0f} сек: {}",
                                prompt_id_prefix,
                                elapsed,
                                by_id[:140],
                            )
                            return by_id

            new_videos = await self._completed_new_videos(
                page, baseline_video_urls
            )
            if new_videos:
                clean = list(new_videos)
                if net_events:
                    clean = [u for u in clean if _url_is_fresh(u, net_events)]
                if prompt_id_prefix:
                    clean = [
                        u
                        for u in clean
                        if _strip_url_query(u) not in rejected_candidates
                    ]
                if clean:
                    chosen = _first_fresh_video_url(
                        clean, rejected=rejected_candidates
                    )
                    if chosen and not prompt_id_prefix:
                        logger.info(
                            "_wait_video_url_strict: новый ролик в DOM за "
                            "{:.0f} сек: {} (всего новых: {})",
                            elapsed,
                            chosen[:140],
                            len(clean),
                        )
                        return chosen
                    if chosen and _strip_url_query(chosen) not in rejected_candidates:
                        fallback_candidate = chosen
                        fallback_source = "new_dom"
                        if len(clean) > 1:
                            logger.info(
                                "_wait_video_url_strict: new_videos={} (>1) — "
                                "беру первый свежий: {}",
                                len(clean),
                                chosen[:120],
                            )

            if (
                prompt_id_prefix
                and fallback_candidate is not None
                and _strip_url_query(fallback_candidate)
                not in rejected_candidates
            ):
                gen_idle = await self._generate_button_enabled(page)
                if gen_idle and elapsed >= _MIN_SEC_BEFORE_HANDOFF:
                    if (not net_events) or _url_is_fresh(
                        fallback_candidate, net_events
                    ):
                        logger.info(
                            "_wait_video_url_strict: gen завершена, handoff "
                            "download-v10video (source={}, {:.0f} сек)",
                            fallback_source,
                            elapsed,
                        )
                        return fallback_candidate

            if (
                prompt_id_prefix
                and elapsed >= _MIN_SEC_BEFORE_HANDOFF
                and await self._generate_button_enabled(page)
            ):
                idle_srcs = await self._completed_new_videos(
                    page, baseline_video_urls
                )
                if idle_srcs:
                    idle_clean = list(idle_srcs)
                    if net_events:
                        idle_clean = [
                            u for u in idle_clean if _url_is_fresh(u, net_events)
                        ]
                    chosen_idle = _first_fresh_video_url(
                        idle_clean, rejected=rejected_candidates
                    )
                    if chosen_idle:
                        logger.info(
                            "_wait_video_url_strict: gen_idle, handoff "
                            "download-v10video за {:.0f} сек",
                            elapsed,
                        )
                        return chosen_idle

            if elapsed >= 1.5:
                failure = await self._detect_outsee_failure(
                    page,
                    queue_mode=queue_mode,
                    prompt_id_prefix=prompt_id_prefix,
                )
                if (
                    not failure
                    and queue_mode
                    and prompt_id_prefix
                ):
                    failure = await self._detect_queue_card_failure(
                        page,
                        prompt_id_prefix=prompt_id_prefix,
                    )
                if failure:
                    ftext = str(failure["text"])
                    in_result = bool(failure.get("in_result"))
                    gen_idle = await self._generate_button_enabled(page)
                    if _outsee_failure_is_stale(
                        ftext,
                        baseline_failure_texts=failure_baseline,
                        in_result=in_result,
                        elapsed=elapsed,
                        gen_idle=gen_idle,
                        queue_mode=queue_mode,
                        prompt_id_prefix=prompt_id_prefix,
                        card_scoped=bool(failure.get("queue_card")),
                    ):
                        stale_key = _normalize_outsee_failure_text(ftext)[:80]
                        if stale_key not in stale_logged:
                            stale_logged.add(stale_key)
                            log_fn = (
                                logger.warning
                                if elapsed >= 45.0
                                and _outsee_failure_kind(ftext) in ("moderation", "generation")
                                else logger.debug
                            )
                            log_fn(
                                "_wait_video_url_strict: игнорирую stale "
                                "плашку (in_result={}, gen_idle={}): {}",
                                in_result,
                                gen_idle,
                                ftext[:80],
                            )
                    else:
                        kind_now = _outsee_failure_kind(ftext)
                        if kind_now == "busy":
                            logger.info(
                                "_wait_video_url_strict: безлимит занят — "
                                "жду завершения ({:.0f}с): {}",
                                elapsed,
                                ftext[:100],
                            )
                        else:
                            logger.info(
                                "_wait_video_url_strict: ошибка outsee за {:.0f} сек "
                                "(in_result={}, kind={}): {}",
                                elapsed,
                                in_result,
                                kind_now,
                                ftext[:120],
                            )
                            _raise_outsee_failure(
                                text=ftext,
                                gen_id=gen_id,
                                elapsed=elapsed,
                                in_result=in_result,
                                prompt_len=prompt_len,
                            )

            if elapsed - last_log > 15:
                last_log = elapsed
                urls = await self._all_video_urls_on_page(page)
                logger.info(
                    "_wait_video_url_strict: ждём... {:.0f} сек, "
                    "videos_in_dom={}, fallback={}",
                    elapsed,
                    len(urls),
                    (
                        fallback_candidate[:80]
                        if fallback_candidate
                        else None
                    ),
                )

            await sleep_cancellable(1.0, project_id)

        ctx: dict[str, Any] = {
            "gen_id": gen_id,
            "baseline_video_urls": len(baseline_video_urls),
            "rejected_count": len(rejected_candidates),
        }
        if prompt_id_prefix:
            ctx["prompt_id_prefix"] = prompt_id_prefix
            ctx["id_diag"] = await self._diag_id_in_page(page, prompt_id_prefix)
            if await self._generate_button_enabled(page):
                handoff_srcs = await self._completed_new_videos(
                    page, baseline_video_urls
                )
                if handoff_srcs:
                    chosen = _first_fresh_video_url(
                        list(handoff_srcs), rejected=rejected_candidates
                    )
                    if chosen:
                        logger.warning(
                            "_wait_video_url_strict: timeout {:.0f}с, но gen_idle "
                            "и есть новые ролики — handoff: {}",
                            timeout,
                            chosen[:120],
                        )
                        return chosen
        raise OutseeImageError(
            f"outsee video: результат не появился за {int(timeout)} сек",
            context=ctx,
        )

    async def _wait_video_url(
        self,
        page: Page,
        *,
        timeout: float,
        prompt_id_prefix: str | None = None,
        project_id: int | None = None,
    ) -> str:
        """Legacy: recon / без prompt_id — любой mp4 в DOM."""
        baseline = {
            _strip_url_query(u) for u in await self._all_video_urls_on_page(page) if u
        }
        if prompt_id_prefix:
            return await self._wait_video_url_strict(
                page,
                timeout=timeout,
                baseline_video_urls=baseline,
                prompt_id_prefix=prompt_id_prefix,
                gen_id="legacy",
                project_id=project_id,
            )
        deadline = asyncio.get_event_loop().time() + timeout
        from app.services.step_cancel import abort_if_cancelled, sleep_cancellable

        while asyncio.get_event_loop().time() < deadline:
            abort_if_cancelled(project_id)
            for u in await self._all_video_urls_on_page(page):
                if any(tok in u for tok in (".mp4", "blob:", "video", "cdn", "storage")):
                    return u
            await sleep_cancellable(1.5, project_id)
        raise PWTimeoutError("outsee video: результат не появился за отведённое время")


def _prompt_id_search_tokens(prompt_id_prefix: str) -> list[str]:
    """Токены для поиска карточки по `[ID: …]` (включая retry `r2a1` и `-S2`)."""
    tokens: list[str] = [prompt_id_prefix]
    # [ID: P12-F3-a7f2b01c] или [ID: P12-F3-a7f2b01c]-S2
    m = re.search(
        r"\[ID:\s*([A-Za-z0-9_-]+)(?:\s+r\d+a\d+)?\s*\](?:-S2)?",
        prompt_id_prefix,
        re.I,
    )
    if m:
        inner = m.group(1)
        if inner not in tokens:
            tokens.append(inner)
        bracket = f"[ID: {inner}]"
        if bracket not in tokens:
            tokens.append(bracket)
    m2 = re.search(
        r"-([0-9a-fA-F]{8})(?:\s+r\d+a\d+)?(?:\]|-S2|$)",
        prompt_id_prefix,
    )
    if m2:
        tail = m2.group(1)
        if tail and tail not in tokens:
            tokens.append(tail)
    return tokens


def _count_tokens_in_text(text: str, tokens: list[str]) -> int:
    return sum(text.count(tok) for tok in tokens if tok)


async def _recent_big_gallery_img_srcs(
    page: Page, *, limit: int = _GALLERY_ID_SCAN_LIMIT
) -> list[str]:
    """Первые `limit` больших thumb'ов в DOM (outsee: новейшие сверху)."""
    try:
        srcs = await page.evaluate(
            """(limit) => {
                const out = [];
                for (const img of document.querySelectorAll('img')) {
                    const r = img.getBoundingClientRect();
                    if (r.width >= 180 && r.height >= 180 && img.src) {
                        out.push(img.src);
                    }
                }
                return out.slice(0, limit);
            }""",
            limit,
        )
        return [s for s in (srcs or []) if isinstance(s, str) and s]
    except Exception:  # noqa: BLE001
        return []


async def find_img_src_by_prompt_id_in_gallery(
    page: Page,
    id_token: str,
    *,
    limit: int = _GALLERY_ID_SCAN_LIMIT,
    max_levels: int = 12,
) -> str | None:
    """Ищет `<img src>` с нашим `[ID: …]` только среди `limit` верхних thumb."""
    tokens = _prompt_id_search_tokens(id_token)
    js = """
    ([tokens, maxLevels, limit]) => {
        const hasToken = (el, idToken) => {
            if (!el) return false;
            const t = (el.innerText || el.textContent || '');
            if (t.includes(idToken)) return true;
            const tag = el.tagName && el.tagName.toLowerCase();
            if (tag === 'textarea' || tag === 'input') {
                const v = el.value || '';
                if (v.includes(idToken)) return true;
            }
            return false;
        };
        const bigImgs = [];
        for (const img of document.querySelectorAll('img')) {
            const r = img.getBoundingClientRect();
            if (r.width >= 180 && r.height >= 180 && img.src) {
                bigImgs.push(img);
            }
        }
        for (const img of bigImgs.slice(0, limit)) {
            let cur = img;
            for (let i = 0; i < maxLevels && cur; i++) {
                for (const idToken of tokens) {
                    if (hasToken(cur, idToken)) {
                        if (!img.src || img.src.startsWith('data:')) break;
                        if (!img.complete) break;
                        if (!img.naturalWidth || img.naturalWidth < 200) break;
                        return img.src;
                    }
                }
                cur = cur.parentElement;
            }
        }
        return null;
    }
    """
    try:
        res = await page.evaluate(js, [tokens, max_levels, limit])
        if isinstance(res, str) and res:
            return res
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("find_img_src_by_prompt_id_in_gallery: {}", e)
        return None


async def verify_img_url_matches_prompt_id_in_gallery(
    page: Page,
    img_url: str,
    prompt_id_prefix: str,
    *,
    gen_id: str | None = None,
    limit: int = _GALLERY_ID_SCAN_LIMIT,
) -> None:
    """Скачивание только если URL совпадает с [ID] в ≤limit последних thumb."""
    by_id = await find_img_src_by_prompt_id_in_gallery(
        page, prompt_id_prefix, limit=limit
    )
    if not by_id:
        raise OutseeImageError(
            "outsee image: [ID] не найден в {} последних thumb — "
            "скачивание отменено".format(limit),
            context={
                "prompt_id_prefix": prompt_id_prefix,
                "img_url": img_url[:200],
                "gen_id": gen_id,
                "gallery_id_scan_limit": limit,
            },
        )
    if _outsee_image_stable_key(by_id) != _outsee_image_stable_key(img_url):
        raise OutseeImageError(
            "outsee image: URL не совпадает с карточкой [ID] "
            "(проверено {} последних thumb)".format(limit),
            context={
                "prompt_id_prefix": prompt_id_prefix,
                "expected_url": by_id[:200],
                "got_url": img_url[:200],
                "gen_id": gen_id,
                "gallery_id_scan_limit": limit,
            },
        )


def _verify_prompt_length_before_send(full_prompt: str, *, where: str) -> None:
    """Outsee отклоняет или молча обрезает промты длиннее лимита."""
    from app.generation_options import OUTSEE_PROMPT_MAX_CHARS

    n = len(full_prompt)
    if n > OUTSEE_PROMPT_MAX_CHARS:
        raise OutseePromptTooLongError(
            f"outsee: промт {n} симв — лимит outsee {OUTSEE_PROMPT_MAX_CHARS}. "
            "Сожмите image_prompt (шаг «Промты картинок») или дождитесь "
            "GPT-сжатия в retry.",
            context={
                "where": where,
                "prompt_len": n,
                "limit": OUTSEE_PROMPT_MAX_CHARS,
                "error_kind": "length",
            },
        )


async def _read_composer_prompt_value(page: Page, input_sel: str) -> str:
    loc = page.locator(input_sel).first
    try:
        val = await loc.input_value()
        if val and val.strip():
            return val.strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        raw = await loc.evaluate(
            """el => {
                const t = (el.tagName || '').toLowerCase();
                if (t === 'textarea' || t === 'input') return el.value || '';
                return el.innerText || el.textContent || '';
            }"""
        )
        return raw.strip() if isinstance(raw, str) else ""
    except Exception:  # noqa: BLE001
        return ""


async def _verify_composer_prompt_filled(
    page: Page,
    input_sel: str,
    *,
    expected_prompt: str,
    prompt_id_prefix: str | None,
    where: str,
) -> None:
    """После fill(): outsee иногда не принимает длинный промт — не ждём таймаут."""
    from app.generation_options import OUTSEE_PROMPT_MAX_CHARS

    actual = await _read_composer_prompt_value(page, input_sel)
    exp_len = len(expected_prompt)
    if len(actual) < 20:
        raise OutseeImageError(
            "outsee: промт не попал в поле ввода (пусто или слишком коротко)",
            context={
                "where": where,
                "actual_len": len(actual),
                "expected_len": exp_len,
            },
        )
    if prompt_id_prefix:
        tokens = _prompt_id_search_tokens(prompt_id_prefix)
        if not any(tok in actual for tok in tokens if len(tok) >= 6):
            raise OutseeImageError(
                "outsee: ID промта не найден в поле после вставки",
                context={
                    "where": where,
                    "prompt_id_prefix": prompt_id_prefix,
                    "actual_len": len(actual),
                },
            )
    ref_len = min(exp_len, OUTSEE_PROMPT_MAX_CHARS)
    if ref_len >= 200 and len(actual) < int(ref_len * 0.85):
        raise OutseePromptTooLongError(
            f"outsee: промт обрезан outsee ({len(actual)} из {exp_len} симв)",
            context={
                "where": where,
                "actual_len": len(actual),
                "expected_len": exp_len,
                "error_kind": "length",
            },
        )


async def _viewport_mouse_click(
    page: Page,
    x: float,
    y: float,
    *,
    project_id: int | None = None,
    label: str = "",
) -> None:
    from app.services.step_cancel import await_with_cancel

    await await_with_cancel(page.mouse.move(x, y), project_id)
    await asyncio.sleep(0.05)
    await await_with_cancel(
        page.mouse.click(x, y, delay=80), project_id
    )
    logger.info(
        "outsee viewport-click: ({:.0f},{:.0f}){}",
        x,
        y,
        f" — {label}" if label else "",
    )


async def _cdp_dispatch_click(
    page: Page,
    x: float,
    y: float,
    *,
    project_id: int | None = None,
) -> None:
    """Клик через Chrome DevTools Protocol (обходит часть overlay/React)."""
    from app.services.step_cancel import await_with_cancel

    try:
        session = await page.context.new_cdp_session(page)
        for ev_type, button_state in (
            ("mouseMoved", 0),
            ("mousePressed", 1),
            ("mouseReleased", 1),
        ):
            await await_with_cancel(
                session.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": ev_type,
                        "x": x,
                        "y": y,
                        "button": "left",
                        "buttons": button_state,
                        "clickCount": 1,
                    },
                ),
                project_id,
            )
            await asyncio.sleep(0.04)
        logger.info("outsee cdp-click: ({:.0f},{:.0f})", x, y)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "outsee cdp-click failed ({}), fallback mouse", type(e).__name__
        )
        await _viewport_mouse_click(
            page, x, y, project_id=project_id, label="cdp-fallback"
        )


async def _clear_page_text_selection(page: Page) -> None:
    """Снять выделение текста — иначе клик по thumb копирует промт вместо панели."""
    with contextlib.suppress(Exception):
        await page.evaluate(
            """() => {
                const s = window.getSelection();
                if (s && s.rangeCount) s.removeAllRanges();
            }"""
        )


async def _physical_mouse_click(
    page: Page,
    locator: Any,
    *,
    project_id: int | None = None,
    label: str = "",
    prefer_cdp: bool = False,
) -> None:
    """Реальный клик мышью по центру элемента (CDP → Chrome).

    Outsee открывает панель «Промпт» и кнопку Download только на pointer-
    событиях; `element.click()` в JS или «сухой» locator иногда не срабатывает.
    """
    from app.services.step_cancel import await_with_cancel

    await _clear_page_text_selection(page)
    with contextlib.suppress(Exception):
        await await_with_cancel(
            locator.scroll_into_view_if_needed(timeout=2_000),
            project_id,
        )
    box = await locator.bounding_box()
    if not box or box.get("width", 0) < 2 or box.get("height", 0) < 2:
        await await_with_cancel(locator.click(timeout=3_000), project_id)
        logger.info(
            "outsee physical-click: fallback locator.click{}",
            f" ({label})" if label else "",
        )
        return
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    if prefer_cdp:
        await _cdp_dispatch_click(page, x, y, project_id=project_id)
        await _clear_page_text_selection(page)
        logger.info(
            "outsee physical-click: cdp ({:.0f},{:.0f}){}",
            x,
            y,
            f" — {label}" if label else "",
        )
        return
    await page.mouse.move(x, y)
    await asyncio.sleep(0.05)
    await page.mouse.click(x, y)
    await _clear_page_text_selection(page)
    logger.info(
        "outsee physical-click: mouse ({:.0f},{:.0f}){}",
        x,
        y,
        f" — {label}" if label else "",
    )


async def _composer_has_prompt_id(
    page: Page,
    prompt_id_prefix: str,
    *,
    composer_selectors: list[str] | None = None,
) -> bool:
    """ID уже в поле промта композера — галерею кликать не нужно."""
    tokens = _prompt_id_search_tokens(prompt_id_prefix)
    selectors = composer_selectors or PROMPT_INPUT_SELECTORS
    for sel in selectors:
        try:
            val = await _read_composer_prompt_value(page, sel)
        except Exception:  # noqa: BLE001
            continue
        if not val:
            continue
        for tok in tokens:
            if tok and tok in val:
                return True
    return False


async def _gallery_detail_panel_has_id(
    page: Page,
    prompt_id_prefix: str,
) -> bool:
    """После клика по thumb: наш ID в правой панели (НЕ в composer).

    Нельзя использовать `post_count > pre_count` на всей странице — ID уже
    в composer, счётчик не растёт. Нельзя `evaluate(el.click())` — не мышь.
    """
    tokens = _prompt_id_search_tokens(prompt_id_prefix)
    try:
        matched = await page.evaluate(
            """([tokens, composerSels]) => {
                const composer = new Set();
                for (const sel of composerSels) {
                    try {
                        for (const el of document.querySelectorAll(sel))
                            composer.add(el);
                    } catch (e) {}
                }
                function visible(el) {
                    const cs = getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 8 && r.height > 8;
                }
                const midX = window.innerWidth * 0.38;
                function hay(el) {
                    if (!el || composer.has(el) || !visible(el)) return '';
                    return (
                        (el.value || el.innerText || el.textContent || '')
                        + ' ' + (el.getAttribute('data-value') || '')
                        + ' ' + (el.getAttribute('aria-label') || '')
                    ).trim();
                }
                for (const el of document.querySelectorAll(
                    'textarea, input, [contenteditable="true"]'
                )) {
                    const v = hay(el);
                    if (!v) continue;
                    for (const tok of tokens) {
                        if (tok && v.includes(tok)) return true;
                    }
                }
                for (const el of document.querySelectorAll(
                    'section, aside, div[role="dialog"], div[class*="prompt"]'
                )) {
                    if (!visible(el)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.left < midX || r.width < 80) continue;
                    const t = (el.innerText || el.textContent || '').trim();
                    if (t.length < 20 || t.length > 12000) continue;
                    for (const tok of tokens) {
                        if (tok && t.includes(tok)) return true;
                    }
                }
                return false;
            }""",
            [tokens, PROMPT_INPUT_SELECTORS],
        )
        return bool(matched)
    except Exception:  # noqa: BLE001
        return False


async def _page_text_excluding_composer(
    page: Page,
    composer_selectors: list[str] | None = None,
) -> str:
    selectors = composer_selectors or PROMPT_INPUT_SELECTORS
    try:
        res = await page.evaluate(
            """(selectors) => {
                const composer = new Set();
                for (const sel of selectors) {
                    try {
                        for (const el of document.querySelectorAll(sel)) {
                            composer.add(el);
                        }
                    } catch (e) {}
                }
                let text = (document.body && (
                    document.body.innerText || document.body.textContent
                )) || '';
                for (const el of document.querySelectorAll(
                    'textarea, input[type=text], input:not([type])'
                )) {
                    if (composer.has(el)) continue;
                    const v = el && el.value;
                    if (v) text += '\\n' + v;
                }
                return text;
            }""",
            selectors,
        )
        return res if isinstance(res, str) else ""
    except Exception:  # noqa: BLE001
        return ""


async def _count_gallery_video_thumbs(page: Page) -> int:
    """Большие `<video>` в галерее (превью роликов)."""
    try:
        n = await page.evaluate(
            """() => {
                let c = 0;
                for (const v of document.querySelectorAll('video')) {
                    const r = v.getBoundingClientRect();
                    if (r.width >= 120 && r.height >= 120) c++;
                }
                return c;
            }"""
        )
        return int(n) if isinstance(n, int) else 0
    except Exception:  # noqa: BLE001
        return 0


async def _wait_gallery_video_thumbs(
    page: Page,
    *,
    min_count: int = 1,
    timeout_s: float = 45.0,
    project_id: int | None = None,
) -> int:
    from app.services.step_cancel import abort_if_cancelled, sleep_cancellable

    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout_s:
        abort_if_cancelled(project_id)
        n = await _count_gallery_video_thumbs(page)
        if n >= min_count:
            return n
        await sleep_cancellable(1.0, project_id)
    return await _count_gallery_video_thumbs(page)


async def _find_card_by_clicking_videos(
    page: Page,
    *,
    prompt_id_prefix: str,
    limit: int = 10,
    project_id: int | None = None,
) -> Any | None:
    """Перебор первых N роликов в галерее: physical click → ID в панели."""
    from app.services.step_cancel import abort_if_cancelled

    abort_if_cancelled(project_id)
    try:
        srcs: list[str] = await page.evaluate(
            """() => {
                const out = [];
                for (const v of document.querySelectorAll('video')) {
                    const r = v.getBoundingClientRect();
                    if (r.width < 120 || r.height < 120) continue;
                    const src = v.currentSrc || v.src || '';
                    if (src && !src.startsWith('data:')) out.push(src);
                }
                return out;
            }"""
        )
    except Exception:  # noqa: BLE001
        srcs = []

    if not srcs:
        logger.info(
            "_find_card_by_clicking_videos: нет video thumb — fallback на img"
        )
        return await _find_card_by_clicking_images(
            page,
            prompt_id_prefix=prompt_id_prefix,
            limit=limit,
            project_id=project_id,
        )

    logger.info(
        "_find_card_by_clicking_videos: перебор {} роликов (max {})",
        len(srcs),
        limit,
    )

    for idx, src in enumerate(srcs[:limit]):
        abort_if_cancelled(project_id)
        stripped = _strip_url_query(src)
        path_only = re.sub(r"^https?://[^/]+", "", stripped)
        if not path_only:
            continue
        basename = Path(path_only).name
        vid_loc = None
        for fragment in (basename, path_only):
            if not fragment:
                continue
            loc = page.locator(f'video[src*="{fragment}"]').first
            if await loc.count() > 0:
                vid_loc = loc
                break
        if vid_loc is None:
            loc = page.locator("video").nth(idx)
            if await loc.count() == 0:
                continue
            vid_loc = loc

        try:
            await _physical_mouse_click(
                page,
                vid_loc,
                project_id=project_id,
                label=f"gallery video #{idx}",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "_find_card_by_clicking_videos: click video #{} ({})",
                idx,
                type(e).__name__,
            )
            continue

        await asyncio.sleep(0.65)
        if not await _gallery_detail_panel_has_id(page, prompt_id_prefix):
            with contextlib.suppress(Exception):
                await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            continue

        candidate = vid_loc.locator(
            "xpath=ancestor::*[descendant::button"
            "[descendant::svg[contains(@class,'lucide-download')]]][1]"
        )
        if await candidate.count() > 0:
            logger.info(
                "_find_card_by_clicking_videos: НАШЛИ ролик #{} по ID в панели",
                idx,
            )
            return candidate
        with contextlib.suppress(Exception):
            await page.keyboard.press("Escape")

    logger.warning(
        "_find_card_by_clicking_videos: {} роликов без нашего ID — пробую img",
        min(len(srcs), limit),
    )
    return await _find_card_by_clicking_images(
        page,
        prompt_id_prefix=prompt_id_prefix,
        limit=limit,
        project_id=project_id,
    )


async def _download_via_video_card_click(
    page: Page,
    *,
    prompt_id_prefix: str,
    out_path: Path,
    video_url: str | None = None,
    timeout_s: float | None = None,
    project_id: int | None = None,
) -> None:
    """Скачивание veo: прямой URL → physical click по роликам → ID → кнопка ↓."""
    from app.services.step_cancel import abort_if_cancelled, await_with_cancel

    abort_if_cancelled(project_id)
    if timeout_s is None:
        timeout_s = _outsee_download_timeout_s()
    deadline_ms = int(timeout_s * 1000)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if video_url and _video_url_looks_like_result(video_url):
        await _update_download_progress(project_id, "Скачивание… (прямой URL)")
        t0 = asyncio.get_event_loop().time()
        try:
            await _download_via_context(
                page,
                video_url,
                out_path,
                timeout_ms=deadline_ms,
                project_id=project_id,
            )
            _validate_downloaded_video(
                out_path, gen_id=prompt_id_prefix, video_url=video_url
            )
            _log_download_stage(
                stage="direct_url",
                duration_s=asyncio.get_event_loop().time() - t0,
                strategy="url_first",
                media="video",
                project_id=project_id,
            )
            await _update_download_progress(project_id, None)
            logger.info(
                "_download_via_video_card_click: URL-first {} → {}",
                video_url[:120],
                out_path,
            )
            return
        except Exception as e:  # noqa: BLE001
            _log_download_stage(
                stage="direct_url_failed",
                duration_s=asyncio.get_event_loop().time() - t0,
                strategy="url_first",
                media="video",
                project_id=project_id,
                extra=f"err={type(e).__name__}",
            )
            logger.warning(
                "_download_via_video_card_click: URL-first failed ({}), card cascade",
                e,
            )

    await _update_download_progress(project_id, "Скачивание… (поиск карточки)")
    t_search = asyncio.get_event_loop().time()

    n_thumbs = await _wait_gallery_video_thumbs(
        page, min_count=1, timeout_s=45.0, project_id=project_id
    )
    if n_thumbs < 1:
        logger.warning(
            "_download_via_video_card_click: нет video thumb за 45с (id={})",
            prompt_id_prefix,
        )

    card = await _poll_gallery_card(
        lambda: _find_card_by_clicking_videos(
            page,
            prompt_id_prefix=prompt_id_prefix,
            limit=_GALLERY_ID_SCAN_LIMIT,
            project_id=project_id,
        ),
        project_id=project_id,
    )
    _log_download_stage(
        stage="find_card",
        duration_s=asyncio.get_event_loop().time() - t_search,
        strategy="card_click_cascade",
        media="video",
        project_id=project_id,
        extra=f"found={card is not None}",
    )

    if card is None and video_url:
        t_url_click = asyncio.get_event_loop().time()
        card = await _find_card_by_img_url_click(
            page, video_url, project_id=project_id
        )
        _log_download_stage(
            stage="find_card_by_url",
            duration_s=asyncio.get_event_loop().time() - t_url_click,
            strategy="img_url_click",
            media="video",
            project_id=project_id,
            extra=f"found={card is not None}",
        )

    if card is None:
        id_el = page.get_by_text(prompt_id_prefix, exact=False).first
        try:
            await await_with_cancel(
                id_el.wait_for(state="visible", timeout=5_000),
                project_id,
            )
            candidate = id_el.locator(
                "xpath=ancestor::*[descendant::button"
                "[descendant::svg[contains(@class,'lucide-download')]]][1]"
            )
            if await candidate.count() > 0:
                card = candidate
        except PWTimeoutError:
            pass

    if card is None and video_url and _video_url_looks_like_result(video_url):
        t_retry = asyncio.get_event_loop().time()
        logger.warning(
            "_download_via_video_card_click: карточка не найдена, повтор URL {}",
            video_url[:120],
        )
        try:
            await _download_via_context(
                page, video_url, out_path, timeout_ms=deadline_ms, project_id=project_id
            )
            _validate_downloaded_video(
                out_path, gen_id=prompt_id_prefix, video_url=video_url
            )
            _log_download_stage(
                stage="url_fallback",
                duration_s=asyncio.get_event_loop().time() - t_retry,
                strategy="url_after_card_miss",
                media="video",
                project_id=project_id,
            )
            await _update_download_progress(project_id, None)
            return
        except Exception as e:  # noqa: BLE001
            _log_download_stage(
                stage="url_fallback_failed",
                duration_s=asyncio.get_event_loop().time() - t_retry,
                strategy="url_after_card_miss",
                media="video",
                project_id=project_id,
                extra=f"err={type(e).__name__}",
            )
            raise OutseeImageError(
                "outsee video: не смог скачать файл",
                context={
                    "prompt_id_prefix": prompt_id_prefix,
                    "video_url": video_url,
                    "timeout_s": timeout_s,
                    "err": f"{type(e).__name__}: {e}",
                },
            ) from e

    if card is None:
        await _update_download_progress(project_id, None)
        raise OutseeImageError(
            "outsee video: не смог скачать файл",
            context={
                "prompt_id_prefix": prompt_id_prefix,
                "video_url": video_url,
                "timeout_s": timeout_s,
            },
        )

    await _update_download_progress(project_id, "Скачивание… (клик)")
    t_click = asyncio.get_event_loop().time()

    with contextlib.suppress(Exception):
        await card.scroll_into_view_if_needed(timeout=5_000)
    with contextlib.suppress(Exception):
        await card.hover(timeout=5_000)

    download_btn = card.locator("button:has(svg.lucide-download)").first
    try:
        async with page.expect_download(timeout=deadline_ms) as dl_info:
            await _physical_mouse_click(
                page,
                download_btn,
                project_id=project_id,
                label="video download lucide-download",
            )
        download = await dl_info.value
        await await_with_cancel(download.save_as(str(out_path)), project_id)
        _validate_downloaded_video(
            out_path, gen_id=prompt_id_prefix, video_url=video_url or ""
        )
        _log_download_stage(
            stage="browser_download",
            duration_s=asyncio.get_event_loop().time() - t_click,
            strategy="card_click_download",
            media="video",
            project_id=project_id,
        )
        await _update_download_progress(project_id, None)
    except PWTimeoutError as e:
        if video_url and _video_url_looks_like_result(video_url):
            logger.warning(
                "_download_via_video_card_click: download click timeout, URL fallback"
            )
            try:
                await _download_via_context(
                    page, video_url, out_path, timeout_ms=deadline_ms, project_id=project_id
                )
                _validate_downloaded_video(
                    out_path, gen_id=prompt_id_prefix, video_url=video_url
                )
                _log_download_stage(
                    stage="url_after_click_timeout",
                    duration_s=asyncio.get_event_loop().time() - t_click,
                    strategy="url_after_click_timeout",
                    media="video",
                    project_id=project_id,
                )
                await _update_download_progress(project_id, None)
                return
            except Exception as url_e:  # noqa: BLE001
                raise OutseeImageError(
                    "outsee video: не смог скачать файл",
                    context={
                        "prompt_id_prefix": prompt_id_prefix,
                        "timeout_s": timeout_s,
                        "err": f"{type(url_e).__name__}: {url_e}",
                    },
                ) from url_e
        raise OutseeImageError(
            "outsee video: не смог скачать файл",
            context={"prompt_id_prefix": prompt_id_prefix, "timeout_s": timeout_s},
        ) from e

    logger.info(
        "_download_via_video_card_click: сохранил {} (prompt_id={})",
        out_path,
        prompt_id_prefix,
    )


async def _count_big_gallery_imgs(page: Page) -> int:
    try:
        n = await page.evaluate(
            """() => {
                let c = 0;
                for (const img of document.querySelectorAll('img')) {
                    const r = img.getBoundingClientRect();
                    if (r.width >= 180 && r.height >= 180 && img.src) c++;
                }
                return c;
            }"""
        )
        return int(n) if isinstance(n, int) else 0
    except Exception:  # noqa: BLE001
        return 0


async def _wait_gallery_thumbs(
    page: Page,
    *,
    min_count: int = 1,
    timeout_s: float = 45.0,
    project_id: int | None = None,
) -> int:
    """Ждём появления больших thumb'ов в галерее (после gen_idle / net_events)."""
    from app.services.step_cancel import abort_if_cancelled, sleep_cancellable

    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout_s:
        abort_if_cancelled(project_id)
        n = await _count_big_gallery_imgs(page)
        if n >= min_count:
            return n
        await sleep_cancellable(1.0, project_id)
    return await _count_big_gallery_imgs(page)


async def _find_card_by_img_url_click(
    page: Page,
    img_url: str,
    *,
    project_id: int | None = None,
) -> Any | None:
    """Клик по thumb с нашим URL (без ID в панели) — как fallback для hero/frames."""
    from app.services.step_cancel import abort_if_cancelled, await_with_cancel

    abort_if_cancelled(project_id)
    url_path = _strip_url_query(img_url)
    path_only = re.sub(r"^https?://[^/]+", "", url_path)
    basename = Path(path_only).name if path_only else ""
    img_loc = None
    for fragment in (basename, path_only):
        if not fragment:
            continue
        loc = page.locator(f'img[src*="{fragment}"]').first
        if await loc.count() > 0:
            img_loc = loc
            break
    if img_loc is None:
        return None
    try:
        await _physical_mouse_click(
            page,
            img_loc,
            project_id=project_id,
            label="url-matched gallery img",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "_find_card_by_img_url_click: mouse click ({})", type(e).__name__
        )
        return None
    await asyncio.sleep(0.55)
    candidate = img_loc.locator(
        "xpath=ancestor::*[descendant::button"
        "[descendant::svg[contains(@class,'lucide-download')]]][1]"
    )
    if await candidate.count() > 0:
        logger.info(
            "_find_card_by_img_url_click: карточка по img_url ({})",
            (basename or path_only)[-48:],
        )
        return candidate
    # В истории Outsee Download часто в правой панели, не в ancestor thumb.
    panel = await _find_result_panel_card(page, img_url)
    if panel is not None:
        logger.info(
            "_find_card_by_img_url_click: панель «Результат» после клика thumb ({})",
            (basename or path_only)[-48:],
        )
        return panel
    any_btn = page.locator("button:has(svg.lucide-download)").first
    if await any_btn.count() > 0:
        logger.info(
            "_find_card_by_img_url_click: видимая кнопка Download на странице ({})",
            (basename or path_only)[-48:],
        )
        return any_btn
    return None


async def _find_card_by_clicking_images(
    page: Page,
    *,
    prompt_id_prefix: str,
    limit: int = _GALLERY_ID_SCAN_LIMIT,
    project_id: int | None = None,
    img_url: str | None = None,
):
    """Стратегия C из `_download_via_card_click`: outsee может прятать
    наш `[ID: …]` в `<textarea value="...">` или в правой панели «Промпт»,
    которая рендерится ТОЛЬКО по клику на картинку. Поэтому
    `get_by_text` его не находит.

    Алгоритм: берём первые N (по умолчанию 10) больших `<img>` в DOM
    (новые в outsee всегда добавляются сверху галереи), для каждой:
      1) скроллим в видимую часть и кликаем (открывает панель промта);
      2) ждём 600 мс что DOM обновился;
      3) собираем `body.innerText` + значения всех `<textarea>`/`<input>`
         и проверяем содержит ли любой из них токены ID
         (полный prompt_id_prefix, inner без [ID:…], 8-hex-tail);
      4) если матч — это НАША карточка. Возвращаем её ancestor
         (тот же ancestor с кнопкой download, что используется в A/B).
    """
    from app.services.step_cancel import abort_if_cancelled, await_with_cancel

    srcs = await _recent_big_gallery_img_srcs(page, limit=limit)
    if not srcs:
        return None

    logger.info(
        "_find_card_by_clicking_images: перебор {} последних thumb (max {})",
        len(srcs),
        limit,
    )

    for idx, src in enumerate(srcs[:limit]):
        abort_if_cancelled(project_id)
        # Используем уникальный фрагмент src (последние ~80 символов
        # до query) для CSS-селектора.
        stripped = _strip_url_query(src)
        path_only = re.sub(r"^https?://[^/]+", "", stripped)
        if not path_only:
            continue
        basename = Path(path_only).name
        img_loc = None
        for fragment in (basename, path_only):
            loc = page.locator(f'img[src*="{fragment}"]').first
            if await loc.count() > 0:
                img_loc = loc
                break
        if img_loc is None:
            continue

        # CDP-клик по thumb — открывает панель «Промпт» без выделения текста.
        try:
            await _physical_mouse_click(
                page,
                img_loc,
                project_id=project_id,
                label=f"gallery img #{idx}",
                prefer_cdp=True,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "_find_card_by_clicking_images: mouse click img #{} упал ({})",
                idx,
                type(e).__name__,
            )
            continue

        await asyncio.sleep(1.0)

        matched = await _gallery_detail_panel_has_id(page, prompt_id_prefix)

        if (
            not matched
            and img_url
            and _outsee_image_stable_key(src) == _outsee_image_stable_key(img_url)
        ):
            # URL совпал с handoff — НЕ уходим в CDN-only (thumb→403).
            # Возвращаем панель/кнопку Download или сам img для expect_download.
            logger.info(
                "_find_card_by_clicking_images: handoff URL совпал (#{}) — "
                "ищу кнопку «Скачать»",
                idx,
            )
            panel = await _find_result_panel_card(page, src)
            if panel is not None:
                return panel
            any_btn = page.locator("button:has(svg.lucide-download)").first
            if await any_btn.count() > 0:
                return any_btn
            return img_loc

        if not matched:
            # Не наша картинка — закрываем панель Esc'ом и идём дальше.
            with contextlib.suppress(Exception):
                await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            continue

        # Наша! Возвращаем ancestor-карточку с кнопкой download.
        candidate = img_loc.locator(
            "xpath=ancestor::*[descendant::button"
            "[descendant::svg[contains(@class,'lucide-download')]]][1]"
        )
        if await candidate.count() > 0:
            logger.info(
                "_find_card_by_clicking_images: НАШЛА на картинке #{} "
                "(стратегия C — physical mouse + ID в панели)",
                idx,
            )
            # НЕ закрываем панель — пусть hover-target виден.
            return candidate

        # ID совпал, Download в ancestor нет — панель «Результат» / любая кнопка.
        panel = await _find_result_panel_card(page, src)
        if panel is not None:
            logger.info(
                "_find_card_by_clicking_images: match #{} → панель Результат",
                idx,
            )
            return panel
        any_btn = page.locator("button:has(svg.lucide-download)").first
        if await any_btn.count() > 0:
            logger.info(
                "_find_card_by_clicking_images: match #{} → Download на странице",
                idx,
            )
            return any_btn

        # src/ID совпали, но кнопки Download в DOM нет — URL-fallback в download_via_card_click.
        logger.info(
            "_find_card_by_clicking_images: match #{} без download-кнопки → URL handoff",
            idx,
        )
        return img_loc

    logger.warning(
        "_find_card_by_clicking_images: перебрал {} картинок, нашей не нашлось",
        min(len(srcs), limit),
    )
    return None


_GALLERY_ID_PARSE_RE = re.compile(
    r"\[ID:\s*P(\d+)-F(\d+)-([a-f0-9]{8})\](-S2)?",
    re.IGNORECASE,
)


async def discover_prompt_ids_strategy_c(
    page: Page,
    *,
    project_id: int,
    limit: int = _GALLERY_ID_SCAN_LIMIT,
    frame_filter: set[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    """Поиск карточек в галерее — тот же обход, что strategy C ноды.

    Алгоритм идентичен `_find_card_by_clicking_images`:
      `_recent_big_gallery_img_srcs` → physical click → панель → [ID] → Esc.
    Отличие: собираем все [ID] проекта (для recover), а не match одного prefix.
    """
    from app.generation_options import build_gen_id_prefix
    from app.services.step_cancel import abort_if_cancelled

    srcs = await _recent_big_gallery_img_srcs(page, limit=limit)
    if not srcs:
        return []

    logger.info(
        "discover_prompt_ids_strategy_c: перебор {} thumb (как strategy C)",
        len(srcs[:limit]),
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for idx, src in enumerate(srcs[:limit]):
        abort_if_cancelled(project_id)
        stripped = _strip_url_query(src)
        path_only = re.sub(r"^https?://[^/]+", "", stripped)
        if not path_only:
            continue
        basename = Path(path_only).name
        img_loc = None
        for fragment in (basename, path_only):
            loc = page.locator(f'img[src*="{fragment}"]').first
            if await loc.count() > 0:
                img_loc = loc
                break
        if img_loc is None:
            continue
        try:
            await _physical_mouse_click(
                page,
                img_loc,
                project_id=project_id,
                label=f"strategy-c discover #{idx}",
                prefer_cdp=True,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "discover_prompt_ids_strategy_c: click #{} {}",
                idx,
                type(e).__name__,
            )
            continue
        await asyncio.sleep(1.0)

        try:
            panel_text = await _page_text_excluding_composer(page)
        except Exception:  # noqa: BLE001
            panel_text = ""
        try:
            extra = await page.evaluate(
                """() => {
                    let t = '';
                    for (const el of document.querySelectorAll(
                        'textarea, input, [contenteditable="true"]'
                    )) {
                        t += '\\n' + (el.value || el.innerText || '');
                    }
                    return t;
                }"""
            )
            if isinstance(extra, str):
                panel_text = f"{panel_text}\n{extra}"
        except Exception:  # noqa: BLE001
            pass

        for m in _GALLERY_ID_PARSE_RE.finditer(panel_text or ""):
            pid = int(m.group(1))
            if pid != int(project_id):
                continue
            frame = int(m.group(2))
            hex8 = m.group(3).lower()
            shot = 2 if m.group(4) else 1
            if frame_filter is not None and (frame, shot) not in frame_filter:
                continue
            prefix = build_gen_id_prefix(pid, frame, hex8)
            if shot == 2:
                prefix = prefix + "-S2"
            if prefix in seen:
                continue
            seen.add(prefix)
            out.append(
                {
                    "prompt_id_prefix": prefix,
                    "frame_number": frame,
                    "shot": shot,
                    "short_uuid": hex8,
                    "img_src": src,
                }
            )
            logger.info(
                "discover_prompt_ids_strategy_c: #{} → {}",
                idx,
                prefix,
            )

        with contextlib.suppress(Exception):
            await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)

    logger.info(
        "discover_prompt_ids_strategy_c: найдено {} id (project={})",
        len(out),
        project_id,
    )
    return out


async def _download_via_context_candidates(
    page: Page,
    primary_url: str,
    out_path: Path,
    *,
    net_events: list[tuple[float, str]] | None = None,
    project_id: int | None = None,
) -> str:
    """Скачивает по URL, перебирая full PNG вместо thumb."""
    dom_full = await _find_full_png_in_dom(
        page, _outsee_image_stable_key(primary_url)
    )
    extra = [dom_full] if dom_full else None
    candidates = _collect_download_url_candidates(
        primary_url, net_events=net_events, extra_urls=extra
    )
    last_err: Exception | None = None
    for u in candidates:
        if _is_outsee_thumb_url(u):
            logger.warning(
                "_download_via_context_candidates: пропуск thumb {}",
                u[:100],
            )
            continue
        try:
            await _download_via_context(
                page, u, out_path, project_id=project_id
            )
            size = out_path.stat().st_size
            if size < _MIN_IMAGE_BYTES:
                logger.warning(
                    "_download_via_context_candidates: {} — {} B, "
                    "пробую следующий URL",
                    u[:100],
                    size,
                )
                with contextlib.suppress(OSError):
                    out_path.unlink(missing_ok=True)
                continue
            with out_path.open("rb") as f:
                head = f.read(16)
            is_img = (
                head.startswith(_PNG_MAGIC)
                or head.startswith(_JPEG_MAGIC)
                or (head[:4] == _RIFF_MAGIC and head[8:12] == _WEBP_TAG)
            )
            if not is_img:
                logger.warning(
                    "_download_via_context_candidates: {} — не PNG/JPEG/WebP",
                    u[:100],
                )
                with contextlib.suppress(OSError):
                    out_path.unlink(missing_ok=True)
                continue
            logger.info(
                "_download_via_context_candidates: ok {} ({} B)",
                u[:120],
                size,
            )
            return u
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(
                "_download_via_context_candidates: {} ({})",
                u[:100],
                type(e).__name__,
            )
            with contextlib.suppress(OSError):
                if out_path.exists() and out_path.stat().st_size < _MIN_IMAGE_BYTES:
                    out_path.unlink(missing_ok=True)
    raise OutseeImageError(
        "outsee image: не удалось скачать full PNG ни по одному URL",
        context={
            "primary_url": primary_url[:200],
            "candidates": len(candidates),
            "err": f"{type(last_err).__name__}: {last_err}" if last_err else None,
        },
    ) from last_err


async def _find_result_panel_card(page: Page, img_url: str | None) -> Any:
    """Карточка «Результат генерации» с кнопкой Download (queue-mode)."""
    if img_url:
        stripped = _strip_url_query(img_url)
        path_only = re.sub(r"^https?://[^/]+", "", stripped)
        basename = Path(path_only).name if path_only else ""
        for fragment in (basename, path_only):
            if not fragment:
                continue
            img_loc = page.locator(f'img[src*="{fragment}"]').first
            if await img_loc.count() > 0:
                card = img_loc.locator(
                    "xpath=ancestor::*[descendant::button"
                    "[descendant::svg[contains(@class,'lucide-download')]]][1]"
                )
                if await card.count() > 0:
                    return card
    for sel in (
        "xpath=//*[contains(.,'Результат генерации')]"
        "[descendant::button[descendant::svg[contains(@class,'lucide-download')]]]"
        "[1]",
        "xpath=//*[contains(.,'Результат')]"
        "[descendant::button[descendant::svg[contains(@class,'lucide-download')]]]"
        "[1]",
    ):
        loc = page.locator(sel).first
        if await loc.count() > 0:
            return loc
    return None


async def download_image_like_generate(
    page: Page,
    *,
    out_path: Path,
    img_url: str,
    gen_id: str,
    prompt_id_prefix: str | None = None,
    project_id: int | None = None,
    net_events: list[tuple[float, str]] | None = None,
    soft_verify: Any | None = None,
) -> str:
    """Единое скачивание картинки — тот же порядок, что у ноды `generate_image`.

    Порядок (не менять):
      1) queue-mode: `_download_via_queue_result` (CDN/URL → «Скачать» в Результате)
      2) `_download_via_card_click` по `[ID]` (+ img_url)
      3) `download_saved_image_by_prompt_id` (cold cascade C→D→B→A)
      4) `_download_via_context_candidates` по URL
      5) `_validate_downloaded_image`

    Montage recover / history / retry ОБЯЗАНЫ звать эту функцию —
    отдельные D0–D5 механики запрещены.
    """
    resolved = _resolve_best_download_url(img_url or "", net_events=net_events)
    downloaded = False
    if _outsee_queue_mode():
        try:
            await _download_via_queue_result(
                page,
                img_url=resolved or img_url or "",
                out_path=out_path,
                gen_id=gen_id,
                net_events=net_events,
                project_id=project_id,
            )
            downloaded = True
            logger.info(
                "download_image_like_generate: queue Download OK id={}",
                (prompt_id_prefix or gen_id or "")[:40],
            )
        except Exception as qe:  # noqa: BLE001
            logger.warning(
                "download_image_like_generate: queue fail ({}) — "
                "fallback card/cascade id={}",
                type(qe).__name__,
                prompt_id_prefix or "—",
            )
    if not downloaded and prompt_id_prefix:
        if soft_verify is not None and (resolved or img_url):
            try:
                await soft_verify(
                    page,
                    resolved or img_url,
                    prompt_id_prefix,
                    gen_id=gen_id,
                )
            except OutseeImageError as ve:
                logger.warning(
                    "download_image_like_generate: verify soft-fail ({}), "
                    "download cascade id={}",
                    ve.reason[:120] if ve.reason else ve,
                    prompt_id_prefix,
                )
        try:
            await _download_via_card_click(
                page,
                prompt_id_prefix=prompt_id_prefix,
                out_path=out_path,
                project_id=project_id,
                img_url=resolved or img_url or None,
                net_events=net_events,
            )
            downloaded = True
        except OutseeImageError:
            await download_saved_image_by_prompt_id(
                page,
                prompt_id_prefix=prompt_id_prefix,
                out_path=out_path,
                project_id=project_id,
                gen_id=gen_id,
            )
            downloaded = True
    if not downloaded:
        await _download_via_context_candidates(
            page,
            resolved or img_url or "",
            out_path,
            net_events=net_events,
            project_id=project_id,
        )
    used = resolved or img_url or ""
    _validate_downloaded_image(out_path, gen_id=gen_id, img_url=used)
    return used


async def _download_via_queue_result(
    page: Page,
    *,
    img_url: str,
    out_path: Path,
    gen_id: str | None = None,
    net_events: list[tuple[float, str]] | None = None,
    project_id: int | None = None,
    timeout_s: float = 120.0,
) -> None:
    """Queue-mode: одна свежая картинка — CDN/URL, иначе одна кнопка «Скачать» в результате."""
    from app.services.step_cancel import abort_if_cancelled, await_with_cancel

    abort_if_cancelled(project_id)
    deadline_ms = int(timeout_s * 1000)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    dom_full = await _find_full_png_in_dom(page, _outsee_image_stable_key(img_url))
    extra_urls = list(_all_full_png_url_candidates(img_url))
    if dom_full:
        extra_urls.append(dom_full)
    resolved = _resolve_best_download_url(
        img_url,
        net_events=net_events,
        extra_urls=extra_urls or None,
    )
    if resolved:
        try:
            used = await _download_via_context_candidates(
                page,
                resolved,
                out_path,
                net_events=net_events,
                project_id=project_id,
            )
            _validate_downloaded_image(out_path, gen_id=gen_id, img_url=used)
            logger.info(
                "_download_via_queue_result: URL {} → {}",
                used[:120],
                out_path,
            )
            return
        except OutseeImageError as e:
            logger.warning(
                "_download_via_queue_result: URL-first ({}) — кнопка «Скачать»",
                e,
            )

    card = await _find_result_panel_card(page, img_url)
    if card is None:
        raise OutseeImageError(
            "outsee image (queue): нет кнопки «Скачать» в блоке результата",
            context={"gen_id": gen_id, "img_url": img_url[:200]},
        )

    with contextlib.suppress(Exception):
        await card.scroll_into_view_if_needed(timeout=5_000)
    try:
        await card.hover(timeout=5_000)
    except Exception:  # noqa: BLE001
        pass

    download_btn = card.locator("button:has(svg.lucide-download)").first
    if await download_btn.count() == 0:
        download_btn = card.locator(
            "button:has-text('Скачать'), button:has-text('Download')"
        ).first

    try:
        async with page.expect_download(timeout=deadline_ms) as dl_info:
            await _physical_mouse_click(
                page,
                download_btn,
                project_id=project_id,
                label="queue result download",
            )
        download = await dl_info.value
        await await_with_cancel(download.save_as(str(out_path)), project_id)
    except PWTimeoutError as e:
        raise OutseeImageError(
            "outsee image (queue): клик «Скачать» не вызвал download",
            context={"gen_id": gen_id, "err": str(e)},
        ) from e

    _validate_downloaded_image(out_path, gen_id=gen_id, img_url=img_url)
    logger.info("_download_via_queue_result: save {} (gen_id={})", out_path, gen_id)


async def _find_result_panel_video_card(
    page: Page, video_url: str | None
) -> Any:
    """Блок результата veo с кнопкой Download (queue-mode)."""
    if video_url:
        stripped = _strip_url_query(video_url)
        path_only = re.sub(r"^https?://[^/]+", "", stripped)
        basename = Path(path_only).name if path_only else ""
        for fragment in (basename, path_only):
            if not fragment:
                continue
            for tag, attr in (("video", "src"), ("source", "src")):
                loc = page.locator(f'{tag}[{attr}*="{fragment}"]').first
                if await loc.count() > 0:
                    card = loc.locator(
                        "xpath=ancestor::*[descendant::button"
                        "[descendant::svg[contains(@class,'lucide-download')]]][1]"
                    )
                    if await card.count() > 0:
                        return card
    for sel in (
        "xpath=//*[contains(.,'Результат генерации')]"
        "[descendant::button[descendant::svg[contains(@class,'lucide-download')]]]"
        "[1]",
        "xpath=//*[contains(.,'Результат')]"
        "[.//video or .//source]"
        "[descendant::button[descendant::svg[contains(@class,'lucide-download')]]]"
        "[1]",
    ):
        loc = page.locator(sel).first
        if await loc.count() > 0:
            return loc
    return None


async def _download_via_queue_video_result(
    page: Page,
    *,
    video_url: str,
    out_path: Path,
    gen_id: str | None = None,
    project_id: int | None = None,
    timeout_s: float | None = None,
) -> None:
    """Queue-mode video: URL из wait, иначе одна кнопка «Скачать» в результате."""
    from app.services.step_cancel import abort_if_cancelled, await_with_cancel

    abort_if_cancelled(project_id)
    if timeout_s is None:
        timeout_s = _outsee_download_timeout_s()
    deadline_ms = int(timeout_s * 1000)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if video_url and _video_url_looks_like_result(video_url):
        await _update_download_progress(project_id, "Скачивание… (прямой URL)")
        t0 = asyncio.get_event_loop().time()
        try:
            await _download_via_context(
                page,
                video_url,
                out_path,
                timeout_ms=deadline_ms,
                project_id=project_id,
            )
            _validate_downloaded_video(
                out_path, gen_id=gen_id or "", video_url=video_url
            )
            _log_download_stage(
                stage="direct_url",
                duration_s=asyncio.get_event_loop().time() - t0,
                strategy="url_first",
                media="video",
                project_id=project_id,
            )
            await _update_download_progress(project_id, None)
            logger.info(
                "_download_via_queue_video_result: URL {} → {}",
                video_url[:120],
                out_path,
            )
            return
        except Exception as e:  # noqa: BLE001
            _log_download_stage(
                stage="direct_url_failed",
                duration_s=asyncio.get_event_loop().time() - t0,
                strategy="url_first",
                media="video",
                project_id=project_id,
                extra=f"err={type(e).__name__}",
            )
            logger.warning(
                "_download_via_queue_video_result: URL-first ({}) — кнопка",
                e,
            )

    await _update_download_progress(project_id, "Скачивание… (поиск карточки)")
    t_search = asyncio.get_event_loop().time()
    card = await _find_result_panel_video_card(page, video_url)
    _log_download_stage(
        stage="find_result_card",
        duration_s=asyncio.get_event_loop().time() - t_search,
        strategy="queue_result_card",
        media="video",
        project_id=project_id,
        extra=f"found={card is not None}",
    )

    if card is None:
        if video_url and _video_url_looks_like_result(video_url):
            t_fb = asyncio.get_event_loop().time()
            try:
                await _download_via_context(
                    page,
                    video_url,
                    out_path,
                    timeout_ms=deadline_ms,
                    project_id=project_id,
                )
                _validate_downloaded_video(
                    out_path, gen_id=gen_id or "", video_url=video_url
                )
                _log_download_stage(
                    stage="url_fallback",
                    duration_s=asyncio.get_event_loop().time() - t_fb,
                    strategy="url_after_card_miss",
                    media="video",
                    project_id=project_id,
                )
                await _update_download_progress(project_id, None)
                logger.info(
                    "_download_via_queue_video_result: fallback URL → {}",
                    out_path,
                )
                return
            except Exception as e:  # noqa: BLE001
                raise OutseeImageError(
                    "outsee video: не смог скачать файл",
                    context={
                        "gen_id": gen_id,
                        "video_url": (video_url or "")[:200],
                        "err": f"{type(e).__name__}: {e}",
                    },
                ) from e
        raise OutseeImageError(
            "outsee video: не смог скачать файл",
            context={"gen_id": gen_id, "video_url": (video_url or "")[:200]},
        )

    await _update_download_progress(project_id, "Скачивание… (клик)")
    t_click = asyncio.get_event_loop().time()

    with contextlib.suppress(Exception):
        await card.scroll_into_view_if_needed(timeout=5_000)
    with contextlib.suppress(Exception):
        await card.hover(timeout=5_000)

    download_btn = card.locator("button:has(svg.lucide-download)").first
    if await download_btn.count() == 0:
        download_btn = card.locator(
            "button:has-text('Скачать'), button:has-text('Download')"
        ).first

    try:
        async with page.expect_download(timeout=deadline_ms) as dl_info:
            await _physical_mouse_click(
                page,
                download_btn,
                project_id=project_id,
                label="queue video download",
            )
        download = await dl_info.value
        await await_with_cancel(download.save_as(str(out_path)), project_id)
        _validate_downloaded_video(
            out_path, gen_id=gen_id or "", video_url=video_url
        )
        _log_download_stage(
            stage="browser_download",
            duration_s=asyncio.get_event_loop().time() - t_click,
            strategy="queue_result_click",
            media="video",
            project_id=project_id,
        )
        await _update_download_progress(project_id, None)
    except PWTimeoutError as e:
        if video_url and _video_url_looks_like_result(video_url):
            try:
                await _download_via_context(
                    page,
                    video_url,
                    out_path,
                    timeout_ms=deadline_ms,
                    project_id=project_id,
                )
                _validate_downloaded_video(
                    out_path, gen_id=gen_id or "", video_url=video_url
                )
                _log_download_stage(
                    stage="url_after_click_timeout",
                    duration_s=asyncio.get_event_loop().time() - t_click,
                    strategy="url_after_click_timeout",
                    media="video",
                    project_id=project_id,
                )
                await _update_download_progress(project_id, None)
                logger.info(
                    "_download_via_queue_video_result: click timeout, URL → {}",
                    out_path,
                )
                return
            except Exception as url_e:  # noqa: BLE001
                raise OutseeImageError(
                    "outsee video: не смог скачать файл",
                    context={"gen_id": gen_id, "err": f"{type(url_e).__name__}: {url_e}"},
                ) from url_e
        raise OutseeImageError(
            "outsee video: не смог скачать файл",
            context={"gen_id": gen_id, "err": str(e)},
        ) from e

    logger.info(
        "_download_via_queue_video_result: save {} (gen_id={})",
        out_path,
        gen_id,
    )


async def download_saved_image_by_prompt_id(
    page: Page,
    *,
    prompt_id_prefix: str,
    out_path: Path,
    project_id: int | None = None,
    gen_id: str | None = None,
    model_slug: str | None = None,
) -> Path:
    """Рабочий cold-download по `[ID]` — тот же cascade, что после Generate.

    Без `img_url`: не зовём `verify_img_url_matches_prompt_id_in_gallery`
    (на холодной галерее ID часто только в панели после клика → verify
    убивал скачивание до strategy C). Ждём thumbs → `_download_via_card_click`
    без URL → validate по байтам.
    """
    from app.services.step_cancel import abort_if_cancelled

    abort_if_cancelled(project_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    await _wait_gallery_thumbs(
        page, min_count=1, timeout_s=45.0, project_id=project_id
    )
    await _download_via_card_click(
        page,
        prompt_id_prefix=prompt_id_prefix,
        out_path=out_path,
        project_id=project_id,
        # img_url намеренно НЕ передаём — клик-cascade C→D→B→A как в img-шаге
        # когда URL ненадёжен / ID только в панели.
    )
    gid = gen_id or prompt_id_prefix
    _validate_downloaded_image(out_path, gen_id=gid, img_url="")
    logger.info(
        "download_saved_image_by_prompt_id: {} ← {} ({} B)",
        out_path.name,
        prompt_id_prefix,
        out_path.stat().st_size,
    )
    return out_path


async def _download_via_card_click(
    page: Page,
    *,
    prompt_id_prefix: str,
    out_path: Path,
    timeout_s: float | None = None,
    project_id: int | None = None,
    img_url: str | None = None,
    net_events: list[tuple[float, str]] | None = None,
) -> None:
    """Кликает зелёную «↓ Скачать» на карточке результата с нашим
    `[ID: P{...}-F{...}-{8hex}]` и сохраняет реальный финальный файл
    через `page.expect_download()`.

    Преимущество перед старым `_download_via_context(page, img_url, ...)`:
    мы НЕ извлекаем URL из `<img src>` — outsee часто кладёт туда
    плейсхолдер (например `topaz.webp` пока работает upscale, или
    `input_*.png` — ссылку на наш же референс). Реальный финальный
    PNG/JPEG отдаётся ТОЛЬКО при клике по кнопке «Download».

    Стратегии (как в TG-боте / hero): ждём thumbs → C → D → B → A → URL-fallback.
    """
    from app.services.step_cancel import abort_if_cancelled, await_with_cancel

    abort_if_cancelled(project_id)
    if timeout_s is None:
        timeout_s = _outsee_download_timeout_s()
    deadline_ms = int(timeout_s * 1000)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Verify по passive-gallery НЕ должен убивать скачивание: ID часто
    # только в правой панели после клика. Soft: при провале идём в cascade.
    if img_url:
        try:
            await verify_img_url_matches_prompt_id_in_gallery(
                page, img_url, prompt_id_prefix
            )
        except OutseeImageError as ve:
            logger.warning(
                "_download_via_card_click: verify soft-fail ({}), "
                "продолжаю card-click cascade id={}",
                ve.reason[:120] if ve.reason else ve,
                prompt_id_prefix,
            )

    # Быстрый путь: full PNG из net_events / DOM / guess от thumb (оба CDN).
    if img_url:
        await _update_download_progress(project_id, "Скачивание… (прямой URL)")
        t_url = asyncio.get_event_loop().time()
        dom_full = await _find_full_png_in_dom(
            page, _outsee_image_stable_key(img_url)
        )
        extra_urls = list(_all_full_png_url_candidates(img_url))
        if dom_full:
            extra_urls.append(dom_full)
        resolved = _resolve_best_download_url(
            img_url,
            net_events=net_events,
            extra_urls=extra_urls or None,
        )
        if resolved:
            try:
                used = await _download_via_context_candidates(
                    page,
                    resolved,
                    out_path,
                    net_events=net_events,
                    project_id=project_id,
                )
                _validate_downloaded_image(
                    out_path, gen_id=prompt_id_prefix, img_url=used
                )
                _log_download_stage(
                    stage="direct_url",
                    duration_s=asyncio.get_event_loop().time() - t_url,
                    strategy="url_first",
                    media="image",
                    project_id=project_id,
                )
                await _update_download_progress(project_id, None)
                logger.info(
                    "_download_via_card_click: сохранил {} (URL-first, id={}, "
                    "thumb={})",
                    out_path,
                    prompt_id_prefix,
                    _is_outsee_thumb_url(img_url),
                )
                return
            except OutseeImageError as e:
                _log_download_stage(
                    stage="direct_url_failed",
                    duration_s=asyncio.get_event_loop().time() - t_url,
                    strategy="url_first",
                    media="image",
                    project_id=project_id,
                    extra=f"err={e.reason[:80]}",
                )
                logger.warning(
                    "_download_via_card_click: URL-first не удался ({}), card-click",
                    e,
                )

    # ID уже в textarea композера — не кликаем галерею (иначе выделяется текст).
    if img_url and await _composer_has_prompt_id(page, prompt_id_prefix):
        await _update_download_progress(project_id, "Скачивание… (прямой URL)")
        t_composer = asyncio.get_event_loop().time()
        dom_full = await _find_full_png_in_dom(
            page, _outsee_image_stable_key(img_url)
        )
        extra_urls = list(_all_full_png_url_candidates(img_url))
        if dom_full:
            extra_urls.append(dom_full)
        resolved = _resolve_best_download_url(
            img_url,
            net_events=net_events,
            extra_urls=extra_urls or None,
        )
        try:
            used = await _download_via_context_candidates(
                page,
                resolved,
                out_path,
                net_events=net_events,
                project_id=project_id,
            )
            _validate_downloaded_image(
                out_path, gen_id=prompt_id_prefix, img_url=used
            )
            _log_download_stage(
                stage="composer_cdn",
                duration_s=asyncio.get_event_loop().time() - t_composer,
                strategy="composer_id_cdn",
                media="image",
                project_id=project_id,
            )
            await _update_download_progress(project_id, None)
            logger.info(
                "_download_via_card_click: сохранил {} (composer-ID, CDN, id={})",
                out_path,
                prompt_id_prefix,
            )
            return
        except OutseeImageError as e:
            logger.warning(
                "_download_via_card_click: composer-ID CDN не удался ({}), "
                "пробую галерею",
                e,
            )

    card = None  # type: ignore[var-annotated]

    # Галерея часто появляется позже CDN-URL — ждём thumbs (hero и frames).
    # Если thumb URL уже есть (recover) — короткая пауза, не 45с.
    thumbs_wait = 8.0 if img_url else 45.0
    n_thumbs = await _wait_gallery_thumbs(
        page, min_count=1, timeout_s=thumbs_wait, project_id=project_id
    )
    if n_thumbs < 1:
        logger.warning(
            "_download_via_card_click: в галерее нет больших thumb за {}с, "
            "всё равно пробую клики (id={})",
            int(thumbs_wait),
            prompt_id_prefix,
        )

    await _update_download_progress(project_id, "Скачивание… (поиск карточки)")
    t_search = asyncio.get_event_loop().time()

    # Когда thumb URL уже известен (montage recover / wait) — сначала
    # клик по ЭТОМУ thumb и кнопка «Скачать». Стратегия C (до 80 кликов)
    # раньше шла первой и вешала UI на минуты, часто без результата.
    if img_url:
        card = await _find_card_by_img_url_click(
            page, img_url, project_id=project_id
        )
        if card is None:
            url_path = _strip_url_query(img_url)
            path_only = re.sub(r"^https?://[^/]+", "", url_path)
            basename = Path(path_only).name if path_only else ""
            for fragment in (basename, path_only):
                if not fragment:
                    continue
                try:
                    img_locator = page.locator(
                        f'img[src*="{fragment}"]'
                    ).first
                    await await_with_cancel(
                        img_locator.wait_for(state="attached", timeout=5_000),
                        project_id,
                    )
                    candidate = img_locator.locator(
                        "xpath=ancestor::*[descendant::button"
                        "[descendant::svg[contains(@class,'lucide-download')]]][1]"
                    )
                    if await candidate.count() > 0:
                        card = candidate
                        logger.info(
                            "_download_via_card_click: карточка по img_url "
                            "ancestor (стратегия A-first) {}",
                            fragment[-50:],
                        )
                        break
                except (PWTimeoutError, Exception) as e:  # noqa: BLE001
                    logger.debug(
                        "_download_via_card_click: A-first '{}': {}",
                        fragment[-40:],
                        type(e).__name__,
                    )

    # --- C: перебор картинок + ID в панели (если URL не помог).
    if card is None:
        card = await _poll_gallery_card(
            lambda: _find_card_by_clicking_images(
                page,
                prompt_id_prefix=prompt_id_prefix,
                limit=_GALLERY_ID_SCAN_LIMIT,
                project_id=project_id,
                img_url=img_url,
            ),
            project_id=project_id,
        )
    _log_download_stage(
        stage="find_card",
        duration_s=asyncio.get_event_loop().time() - t_search,
        strategy="card_click_cascade",
        media="image",
        project_id=project_id,
        extra=f"found={card is not None}",
    )

    # --- B: get_by_text([ID: …]).
    if card is None:
        id_el = page.get_by_text(prompt_id_prefix, exact=False).first
        try:
            await await_with_cancel(
                id_el.wait_for(state="visible", timeout=5_000),
                project_id,
            )
            candidate = id_el.locator(
                "xpath=ancestor::*[descendant::button"
                "[descendant::svg[contains(@class,'lucide-download')]]][1]"
            )
            if await candidate.count() > 0:
                card = candidate
                logger.info(
                    "_download_via_card_click: карточка найдена "
                    "через get_by_text (стратегия B)"
                )
        except PWTimeoutError:
            logger.warning(
                "_download_via_card_click: стратегия B не сработала"
            )

    if card is None and img_url:
        resolved = _resolve_best_download_url(img_url, net_events=net_events)
        logger.warning(
            "_download_via_card_click: клик по карточке не удался, "
            "скачиваю full PNG по URL (было: {})",
            img_url[:120],
        )
        used = await _download_via_context_candidates(
            page,
            resolved,
            out_path,
            net_events=net_events,
            project_id=project_id,
        )
        logger.info(
            "_download_via_card_click: сохранил {} (URL-fallback, id={}, url={})",
            out_path,
            prompt_id_prefix,
            used[:120],
        )
        return

    if card is None:
        raise OutseeImageError(
            "outsee image: не нашёл карточку с нашим ID "
            "(скачивание по клику невозможно, URL нет)",
            context={
                "prompt_id_prefix": prompt_id_prefix,
                "img_url": img_url,
                "timeout_s": timeout_s,
            },
        )

    try:
        card_tag = await card.evaluate(
            "(el) => (el && el.tagName ? el.tagName.toLowerCase() : '')"
        )
    except Exception:  # noqa: BLE001
        card_tag = ""

    async def _card_img_src() -> str | None:
        try:
            if card_tag == "img":
                src = await card.get_attribute("src")
                return src if isinstance(src, str) and src else None
            src = await card.locator("img").first.get_attribute("src")
            return src if isinstance(src, str) and src else None
        except Exception:  # noqa: BLE001
            return None

    download_btn = card.locator("button:has(svg.lucide-download)").first
    if card_tag == "button":
        download_btn = card
    elif card_tag == "img" or await download_btn.count() == 0:
        # После клика по галерее Download часто в панели, не в ancestor thumb.
        panel = await _find_result_panel_card(page, img_url)
        if panel is not None:
            panel_btn = panel.locator("button:has(svg.lucide-download)").first
            if await panel_btn.count() > 0:
                card = panel
                download_btn = panel_btn
                card_tag = "div"
        if card_tag == "img" or await download_btn.count() == 0:
            page_btn = page.locator("button:has(svg.lucide-download)").first
            if await page_btn.count() > 0:
                download_btn = page_btn
                card = page_btn
                card_tag = "button"
    if card_tag == "img" or await download_btn.count() == 0:
        fallback_url = img_url or await _card_img_src()
        if fallback_url:
            dom_full = await _find_full_png_in_dom(
                page, _outsee_image_stable_key(fallback_url)
            )
            extra_urls = list(_all_full_png_url_candidates(fallback_url))
            if dom_full:
                extra_urls.append(dom_full)
            resolved = _resolve_best_download_url(
                fallback_url,
                net_events=net_events,
                extra_urls=extra_urls or None,
            )
            logger.info(
                "_download_via_card_click: {} — URL-fallback (thumb={})",
                "карточка=<img>" if card_tag == "img" else "нет кнопки Download",
                _is_outsee_thumb_url(fallback_url),
            )
            used = await _download_via_context_candidates(
                page,
                resolved,
                out_path,
                net_events=net_events,
                project_id=project_id,
            )
            _validate_downloaded_image(
                out_path, gen_id=prompt_id_prefix, img_url=used
            )
            logger.info(
                "_download_via_card_click: сохранил {} (no-download-btn, id={})",
                out_path,
                prompt_id_prefix,
            )
            return
        raise OutseeImageError(
            "outsee image: карточка без кнопки «Скачать» и нет img_url",
            context={"prompt_id_prefix": prompt_id_prefix},
        )

    # Не критично — карточка может быть и так в видимой области.
    with contextlib.suppress(Exception):
        await card.scroll_into_view_if_needed(timeout=5_000)

    # 3) Кнопки действий (download/heart/regen/trash) появляются только
    #    при hover на карточку — без этого click может не зарегаться,
    #    т.к. кнопка не actionable (opacity:0/display:none по CSS).
    try:
        await card.hover(timeout=5_000)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "_download_via_card_click: hover упал ({}), всё равно "
            "пробуем кликнуть — Playwright auto-wait может обработать",
            type(e).__name__,
        )

    # 4) Внутри карточки находим именно кнопку с lucide-download SVG.
    #    Эта же иконка живёт в библиотеке lucide-icons — её класс
    #    `lucide-download` стабилен и не зависит от Tailwind-стилей.
    if card_tag == "button":
        download_btn = card
    else:
        nested = card.locator("button:has(svg.lucide-download)").first
        if await nested.count() > 0:
            download_btn = nested
        elif await download_btn.count() == 0:
            download_btn = page.locator("button:has(svg.lucide-download)").first

    await _update_download_progress(project_id, "Скачивание… (клик)")
    t_click = asyncio.get_event_loop().time()

    try:
        async with page.expect_download(timeout=deadline_ms) as dl_info:
            await _physical_mouse_click(
                page,
                download_btn,
                project_id=project_id,
                label="download lucide-download",
            )
        download = await dl_info.value
        await await_with_cancel(download.save_as(str(out_path)), project_id)
        _log_download_stage(
            stage="browser_download",
            duration_s=asyncio.get_event_loop().time() - t_click,
            strategy="card_click_download",
            media="image",
            project_id=project_id,
        )
        await _update_download_progress(project_id, None)
    except Exception as e:  # noqa: BLE001
        # Browser download часто ломается на CDP — CDN с img карточки.
        fallback_url = img_url or await _card_img_src()
        if fallback_url:
            logger.warning(
                "_download_via_card_click: browser Download упал ({}), "
                "CDN fallback {}",
                type(e).__name__,
                fallback_url[:100],
            )
            resolved = _resolve_best_download_url(
                fallback_url, net_events=net_events
            )
            used = await _download_via_context_candidates(
                page,
                resolved,
                out_path,
                net_events=net_events,
                project_id=project_id,
            )
            _validate_downloaded_image(
                out_path, gen_id=prompt_id_prefix, img_url=used
            )
            await _update_download_progress(project_id, None)
            logger.info(
                "_download_via_card_click: сохранил {} (CDN after click fail)",
                out_path,
            )
            return
        raise OutseeImageError(
            "outsee image: не смог скачать файл",
            context={
                "prompt_id_prefix": prompt_id_prefix,
                "timeout_s": timeout_s,
                "err": f"{type(e).__name__}: {e}",
            },
        ) from e

    logger.info(
        "_download_via_card_click: сохранил файл {} (prompt_id={})",
        out_path, prompt_id_prefix,
    )


async def _download_via_context(
    page: Page,
    url: str,
    out_path: Path,
    *,
    timeout_ms: int = 120_000,
    attempts: int = 5,
    project_id: int | None = None,
) -> None:
    """Скачивает файл по URL, используя тот же контекст (cookies/auth) страницы.
    CDN outsee/hailuoai/yandexcloud иногда медленный или даёт кратковременные
    DNS/сетевые сбои — поднимаем таймаут до 120 сек, делаем до 5 попыток с
    экспоненциальным backoff и fallback на httpx."""
    from app.services.step_cancel import abort_if_cancelled, await_with_cancel, sleep_cancellable

    ctx = page.context
    api = ctx.request
    last: Exception | None = None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(1, attempts + 1):
        abort_if_cancelled(project_id)
        try:
            resp = await await_with_cancel(api.get(url, timeout=timeout_ms), project_id)
            if resp.status >= 400:
                raise RuntimeError(f"download {url} failed: HTTP {resp.status}")
            body = await await_with_cancel(resp.body(), project_id)
            out_path.write_bytes(body)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning(
                "_download_via_context: попытка {}/{} упала: {}: {}",
                i,
                attempts,
                type(e).__name__,
                e,
            )
            # Если упал Playwright api.get (DNS/gaierror/контекст), пробуем httpx
            if url and url.startswith("http"):
                try:
                    import httpx

                    async with httpx.AsyncClient(
                        timeout=max(30.0, timeout_ms / 1000.0),
                        follow_redirects=True,
                    ) as hclient:
                        hr = await await_with_cancel(hclient.get(url), project_id)
                        if hr.status_code < 400 and len(hr.content) > 100:
                            out_path.write_bytes(hr.content)
                            logger.info(
                                "_download_via_context: успешно скачано через httpx fallback: {} B",
                                len(hr.content),
                            )
                            return
                except Exception as he:  # noqa: BLE001
                    logger.debug("_download_via_context: httpx fallback failed: {}", he)
            backoff = min(15.0, 2.0 * (1.8 ** (i - 1)))
            await sleep_cancellable(backoff, project_id)
    assert last is not None
    raise last


# ---------- recon util: python -m app.bots.outsee recon-image "prompt" ----------


async def _recon_generate_buttons(kind: str = "video") -> None:
    """Скан кнопок Generate на открытой в Chrome странице outsee.

    Запускай ПОСЛЕ логина в outsee в том же Chrome (CDP :29229).
    Пишет в data/outsee_dumps/: JSON с координатами, PNG, HTML.
  """
    import json
    from datetime import datetime as _dt

    url = settings.outsee_image_url if kind == "image" else settings.outsee_video_url
    dumps_dir = Path(settings.data_dir) / "outsee_dumps"
    dumps_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.utcnow().strftime("%Y%m%d_%H%M%S")
    label = f"recon_generate_{kind}_{ts}"

    async with browser_session() as bs:
        page = await bs.open_page(url, reuse=True)
        await page.wait_for_load_state("domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(3)

        scan = await page.evaluate(
            """() => {
                const keywords = [
                    'генерир', 'создать', 'generate', 'run', 'генерация'
                ];
                const visible = (el) => {
                    const cs = getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 4 && r.height > 4;
                };
                const allButtons = [];
                for (const btn of document.querySelectorAll('button, [role="button"]')) {
                    const text = (btn.innerText || btn.textContent || '').trim();
                    const low = text.toLowerCase();
                    const hit = keywords.some(k => low.includes(k));
                    if (!hit && !btn.getAttribute('data-testid')) continue;
                    const r = btn.getBoundingClientRect();
                    allButtons.push({
                        tag: btn.tagName,
                        text: text.slice(0, 80),
                        disabled: !!btn.disabled,
                        ariaDisabled: btn.getAttribute('aria-disabled'),
                        dataTestid: btn.getAttribute('data-testid'),
                        type: btn.getAttribute('type'),
                        className: (btn.className && btn.className.toString().slice(0, 100)) || '',
                        visible: visible(btn),
                        x: Math.round(r.x), y: Math.round(r.y),
                        w: Math.round(r.width), h: Math.round(r.height),
                        cx: Math.round(r.x + r.width / 2),
                        cy: Math.round(r.y + r.height / 2),
                    });
                }
                return allButtons;
            }"""
        )

        selector_hits: list[dict[str, Any]] = []
        for sel in GENERATE_BUTTON_SELECTORS:
            hit: dict[str, Any] = {"selector": sel, "count": 0, "visible_nth": []}
            try:
                loc = page.locator(sel)
                count = await loc.count()
                hit["count"] = count
                for i in range(min(count, 5)):
                    nth = loc.nth(i)
                    try:
                        vis = await nth.is_visible()
                        dis = await nth.is_disabled()
                        box = await nth.bounding_box()
                        hit["visible_nth"].append({
                            "i": i,
                            "visible": vis,
                            "disabled": dis,
                            "box": box,
                        })
                    except Exception as e:  # noqa: BLE001
                        hit["visible_nth"].append({"i": i, "err": str(e)})
            except Exception as e:  # noqa: BLE001
                hit["error"] = str(e)
            selector_hits.append(hit)

        first_sel = await _first_visible(
            page, GENERATE_BUTTON_SELECTORS, timeout_ms=5_000
        )
        report = {
            "kind": kind,
            "url": page.url,
            "first_visible_selector": first_sel,
            "dom_generate_like_buttons": scan or [],
            "configured_selectors": selector_hits,
        }
        json_path = dumps_dir / f"{label}.json"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        png_path = dumps_dir / f"{label}.png"
        html_path = dumps_dir / f"{label}.html"
        try:
            await page.screenshot(path=str(png_path), full_page=True, timeout=15_000)
        except Exception as e:  # noqa: BLE001
            logger.warning("recon screenshot failed: {}", e)
            png_path = None
        try:
            html_path.write_text(await page.content(), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("recon html failed: {}", e)
            html_path = None

        logger.info("=== recon-generate {} ===", kind)
        logger.info("page url: {}", page.url)
        logger.info("first_visible GENERATE selector: {}", first_sel)
        logger.info("DOM buttons (generate-like): {}", len(scan or []))
        for i, b in enumerate(scan or []):
            logger.info(
                "  [{}] visible={} disabled={} ({},{}) {}×{} text={!r}",
                i,
                b.get("visible"),
                b.get("disabled"),
                b.get("cx"),
                b.get("cy"),
                b.get("w"),
                b.get("h"),
                b.get("text"),
            )
        logger.info("JSON → {}", json_path)
        if png_path:
            logger.info("PNG  → {}", png_path)
        if html_path:
            logger.info("HTML → {}", html_path)
        print(f"\nГотово. Открой файл и пришли в чат:\n  {json_path}")
        if png_path:
            print(f"  {png_path}")


async def _recon(kind: str, prompt: str, start_frame: str | None = None) -> None:
    url = settings.outsee_image_url if kind == "image" else settings.outsee_video_url
    async with browser_session() as bs:
        page = await bs.open_page(url, reuse=True)
        await page.wait_for_load_state("domcontentloaded")
        # Ждём окончания сетевой активности (Next.js гидратация).
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(5)
        # Печатаем найденные элементы для калибровки селекторов
        info = await page.evaluate(
            """() => {
                const q = sel => Array.from(document.querySelectorAll(sel)).slice(0, 8)
                    .map(el => ({
                        tag: el.tagName,
                        id: el.id,
                        cls: el.className && el.className.toString().slice(0, 120),
                        role: el.getAttribute('role'),
                        ariaLabel: el.getAttribute('aria-label'),
                        placeholder: el.getAttribute('placeholder'),
                        text: (el.innerText || '').slice(0, 60),
                    }));
                return {
                    textareas: q('textarea'),
                    contenteditables: q('[contenteditable=\\'true\\']'),
                    buttons: q('button'),
                    fileInputs: q("input[type='file']"),
                    imgs: q('img'),
                    videos: q('video'),
                };
            }"""
        )
        for name, items in info.items():
            logger.info("--- {} ({}) ---", name, len(items))
            for i, it in enumerate(items):
                logger.info("  [{}] {}", i, it)


def _cli() -> None:
    if len(sys.argv) < 2:
        print(
            "usage:\n"
            "  python -m app.bots.outsee recon-generate [video|image]\n"
            "  python -m app.bots.outsee recon-video <prompt> [start_frame]\n"
            "  python -m app.bots.outsee recon-image <prompt>"
        )
        sys.exit(1)
    cmd = sys.argv[1].lower()
    if cmd.startswith("recon-generate"):
        kind = "video"
        if len(sys.argv) > 2 and "image" in sys.argv[2].lower():
            kind = "image"
        asyncio.run(_recon_generate_buttons(kind))
        return
    if len(sys.argv) < 3:
        print("usage: python -m app.bots.outsee recon-image|recon-video <prompt> [start_frame]")
        sys.exit(1)
    prompt = sys.argv[2]
    start = sys.argv[3] if len(sys.argv) > 3 else None
    kind = "image" if "image" in cmd else "video"
    asyncio.run(_recon(kind, prompt, start))


if __name__ == "__main__":
    _cli()
