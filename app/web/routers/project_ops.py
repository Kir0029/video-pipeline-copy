"""Операции проекта для веб-студии: xlsx, ассеты, пауза, сброс шага."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Artifact, ArtifactKind, Project, ProjectStatus
from app.services.event_bus import publish_project_event
from app.services.project_control import pause_project as pause_project_svc
from app.services.project_control import resume_project as resume_project_svc
from app.services.project_control import stop_project_running
from app.services.reset_step import reset_step
from app.services.run_sync import (
    ensure_run_for_project,
    reset_nodes_from_step,
    sync_run_for_project,
    _get_default_workflow_id,
)
from app.services.chatgpt_xlsx import sync_project_xlsx
from app.settings import settings
from app.storage import ProjectSheet
from app.project_db import sync_project_row_to_master_db
from app.web.deps import get_project_session, get_session
from app.web.project_dto import project_to_detail
from app.web.schemas import ProjectDetail

router = APIRouter(prefix="/projects", tags=["project-ops"])


def _project_or_404(project: Project | None) -> Project:
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.post("/{project_id}/pause", response_model=ProjectDetail)
async def pause_project(
    project_id: int, session: AsyncSession = Depends(get_session)
) -> Project:
    p = _project_or_404(await session.get(Project, project_id))
    await pause_project_svc(session, p)
    await session.commit()
    await sync_run_for_project(project_id)
    await session.refresh(p)
    await publish_project_event(project_id, event_type="project_updated", payload={"paused": True})
    return p


@router.post("/{project_id}/resume", response_model=ProjectDetail)
async def resume_project(
    project_id: int, session: AsyncSession = Depends(get_session)
) -> Project:
    p = _project_or_404(await session.get(Project, project_id))
    await resume_project_svc(session, p)
    await session.commit()
    await session.refresh(p)
    await publish_project_event(project_id, event_type="project_updated", payload={"resumed": True})
    return p


@router.post("/{project_id}/continue")
async def continue_project(
    project_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    """Снять stop/паузу и продвинуть проект на следующий шаг (если *_ready)."""
    from app.orchestrator.auto_advance import continue_project_pipeline

    p = _project_or_404(await session.get(Project, project_id))
    info = await continue_project_pipeline(session, p, bot=None)
    await session.commit()
    await session.refresh(p)
    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={"continue": True, **info},
    )
    return {"project": project_to_detail(p), **info}


@router.post("/{project_id}/stop")
async def stop_project(
    project_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    # Сразу cancel advance/GPT — до любого ожидания SQLite (первый клик ⏹).
    from app.services.step_cancel import request_stop

    request_stop(project_id)
    p = _project_or_404(await session.get(Project, project_id))
    info = await stop_project_running(session, p)
    if not info["ok"]:
        raise HTTPException(status_code=400, detail=info["message"])
    await session.commit()
    await sync_run_for_project(project_id)
    await session.refresh(p)
    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={"stopped": True, "message": info["message"]},
    )
    return {
        "project": project_to_detail(p),
        "message": info["message"],
        "generation_still_active": info["generation_still_active"],
        "xlsx_stopped": info["xlsx_stopped"],
    }


@router.post("/{project_id}/finish/images")
async def finish_missing_images(
    project_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    """Доделка картинок: frame_NNN_*.png без файла → generating_images."""
    from app.services.finish_missing import trigger_finish_missing_images

    p = _project_or_404(await session.get(Project, project_id))
    info = await trigger_finish_missing_images(session, p)
    await sync_run_for_project(project_id, session=session)
    await session.commit()
    await session.refresh(p)
    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={"finish_missing": "images", **info},
    )
    return {**info, "project": project_to_detail(p)}


@router.post("/{project_id}/finish/animation-prompts")
async def resume_animation_prompts(
    project_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    """Догонка промтов анимации: plan R48 → БД → generating_animation_prompts."""
    from app.services.finish_missing import trigger_resume_animation_prompts

    p = _project_or_404(await session.get(Project, project_id))
    info = await trigger_resume_animation_prompts(session, p)
    await sync_run_for_project(project_id, session=session)
    await session.commit()
    await session.refresh(p)
    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={"finish_missing": "animation_prompts", **info},
    )
    return {**info, "project": project_to_detail(p)}


@router.post("/{project_id}/finish/videos")
async def finish_missing_videos(
    project_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    """Доделка видео: clip_NNN_*.mp4 без файла → generating_videos."""
    from app.services.finish_missing import trigger_finish_missing_videos

    p = _project_or_404(await session.get(Project, project_id))
    info = await trigger_finish_missing_videos(session, p)
    await sync_run_for_project(project_id, session=session)
    await session.commit()
    await session.refresh(p)
    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={"finish_missing": "videos", **info},
    )
    return {**info, "project": project_to_detail(p)}


@router.post("/{project_id}/mass-lanes/parse-topics")
async def parse_mass_topics_xlsx(
    project_id: int,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Парсит любой xlsx (построчно темы) и сохраняет очередь на родителе (правило B)."""
    import tempfile

    from app.storage.mass_topics import parse_topics_xlsx
    from app.services.mass_factory import apply_topics_upload

    parent = _project_or_404(await session.get(Project, project_id))
    suffix = Path(file.filename or "topics.xlsx").suffix or ".xlsx"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="пустой файл")
        tmp_path.write_bytes(content)
        try:
            topics = parse_topics_xlsx(tmp_path)
        except Exception as exc:
            logger.warning("mass-lanes/parse-topics: parse failed for #{}: {}", project_id, exc)
            raise HTTPException(
                status_code=400,
                detail=f"не удалось прочитать Excel: {exc}",
            ) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    if not topics:
        raise HTTPException(status_code=400, detail="в Excel не найдено тем (построчно)")

    try:
        result = await apply_topics_upload(
            session,
            parent,
            topics=topics,
            filename=file.filename or "topics.xlsx",
        )
        await session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        await session.rollback()
        logger.exception("mass-lanes/parse-topics failed for project #{}", project_id)
        raise HTTPException(
            status_code=500,
            detail=f"ошибка сохранения Excel: {exc}",
        ) from exc

    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={"mass_excel_upload": result.get("count"), "revision": result.get("revision")},
    )
    return {"topics": topics, "cards": [], "count": len(topics), **result}


@router.get("/{project_id}/mass-factory/status")
async def mass_factory_status_endpoint(
    project_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.services.mass_factory import mass_factory_status

    parent = _project_or_404(await session.get(Project, project_id))
    return await mass_factory_status(session, parent)


@router.post("/{project_id}/mass-lanes/start")
async def start_mass_lanes(
    project_id: int,
    payload: dict,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Очередь видео: родитель-шаблон, генерация в дочерних проектах."""
    from app.services.mass_factory import start_mass_queue
    from app.web.routers.projects import _slugify

    template = _project_or_404(await session.get(Project, project_id))
    meta_template = dict(template.meta or {})
    topics: list[str] = [str(t).strip() for t in (payload.get("topics") or []) if str(t).strip()]
    if not topics:
        meta_topics = meta_template.get("mass_queue_topics") or meta_template.get("mass_excel_topics")
        if isinstance(meta_topics, list):
            topics = [str(t).strip() for t in meta_topics if str(t).strip()]
    if not topics:
        bindings = meta_template.get("excel_lane_bindings")
        if isinstance(bindings, list):
            ordered = sorted(
                [b for b in bindings if isinstance(b, dict)],
                key=lambda b: int(b.get("topic_index") or 0),
            )
            for b in ordered:
                t = str(b.get("topic") or "").strip()
                if t:
                    topics.append(t)
    try:
        result = await start_mass_queue(session, template, topics=topics or None, slugify=_slugify)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("mass-lanes/start failed for project #{}", project_id)
        raise HTTPException(status_code=500, detail=f"ошибка запуска очереди: {exc}") from exc

    await session.commit()
    return {
        "created": [{"id": result["started_id"], "topic": result["topic"]}],
        "count": 1,
        "queue_size": result["queue_size"],
        "remaining": result["remaining"],
        "started_id": result["started_id"],
    }


@router.post("/{project_id}/steps/{step_code}/reset", response_model=ProjectDetail)
async def reset_project_step(
    project_id: int,
    step_code: str,
    session: AsyncSession = Depends(get_session),
) -> Project:
    p = _project_or_404(await session.get(Project, project_id))
    try:
        summary = await reset_step(session, p, step_code)
        if p.data_dir:
            from app.project_db import resolve_project_db_path, project_session_scope
            db_path = resolve_project_db_path(p.data_dir)
            if db_path.exists():
                try:
                    async with project_session_scope(p.data_dir) as p_sess:
                        p_in_proj = await p_sess.get(Project, project_id)
                        if p_in_proj:
                            await reset_step(p_sess, p_in_proj, step_code)
                            await p_sess.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("reset_project_step #{}: reset project.db failed: {}", project_id, exc)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if summary.get("error"):
        raise HTTPException(status_code=400, detail=str(summary["error"]))
    wf_id = await _get_default_workflow_id()
    if wf_id is not None:
        await ensure_run_for_project(project_id, wf_id)
    await reset_nodes_from_step(session, project_id, step_code)
    await session.flush()
    await session.commit()
    await session.refresh(p)
    from app.project_db import sync_project_row_to_project_db
    try:
        await sync_project_row_to_project_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reset_project_step #{}: sync to project.db failed: {}", project_id, exc)
    await sync_run_for_project(project_id)
    await session.refresh(p)
    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={"reset_step": step_code},
    )
    return p


@router.get("/{project_id}/excel-hero")
async def get_excel_hero(
    project_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    """Текущее состояние excel-hero в project.meta (если есть)."""
    p = _project_or_404(await session.get(Project, project_id))
    meta = dict(p.meta or {})
    cfg = meta.get("excel_hero") or {}
    chars = cfg.get("characters") if isinstance(cfg, dict) else None
    return {"loaded": bool(chars), "characters": chars or []}


@router.post("/{project_id}/excel-hero/load")
async def load_excel_hero(
    project_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    """Загрузить персонажей в meta['excel_hero']: Entity (SoT) → fallback Excel.

    После этого шаг hero пойдёт по excel-ветке (`_run_excel`), беря данные
    из meta, без необходимости заполнять hero_descriptions/hero_count.
    """
    from sqlalchemy import select

    from app.models import Entity
    from app.services.excel_characters import (
        characters_from_entities,
        parse_persons_sheet,
    )

    p = _project_or_404(await session.get(Project, project_id))
    ents = list(
        (
            await session.execute(
                select(Entity)
                .where(
                    Entity.project_id == p.id,
                    Entity.type == "character",
                )
                .order_by(Entity.sort_key, Entity.id)
            )
        ).scalars().all()
    )
    chars = characters_from_entities(ents)
    source = "entity"
    if not chars:
        xlsx = p.data_dir / "project.xlsx"
        if not xlsx.exists():
            raise HTTPException(
                status_code=404,
                detail=(
                    "в Базе нет персонажей (Entity) и нет project.xlsx "
                    f"по пути {xlsx}"
                ),
            )
        try:
            chars = parse_persons_sheet(xlsx)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status_code=400,
                detail=f"не удалось распарсить лист «Персонажи»: {e}",
            ) from e
        source = "xlsx"
    if not chars:
        raise HTTPException(
            status_code=400,
            detail="нет заполненных персонажей ни в Базе, ни на листе «Персонажи»",
        )
    meta = dict(p.meta or {})
    meta["excel_hero"] = {
        "characters": [c.to_dict() for c in chars],
        "source": source,
    }
    p.meta = meta
    p.updated_at = datetime.utcnow()
    await session.commit()
    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={"excel_hero": len(chars), "source": source},
    )
    return {
        "loaded": True,
        "count": len(chars),
        "source": source,
        "characters": [c.to_dict() for c in chars],
    }


@router.delete("/{project_id}/excel-hero", status_code=204)
async def clear_excel_hero(
    project_id: int, session: AsyncSession = Depends(get_session)
) -> None:
    """Убрать excel_hero — hero пойдёт по обычной ветке (hero_descriptions)."""
    p = _project_or_404(await session.get(Project, project_id))
    meta = dict(p.meta or {})
    if "excel_hero" in meta:
        del meta["excel_hero"]
        p.meta = meta
        p.updated_at = datetime.utcnow()
        await session.commit()
        await publish_project_event(
            project_id, event_type="project_updated", payload={"excel_hero": 0}
        )


@router.get("/{project_id}/xlsx")
async def download_xlsx(
    project_id: int,
    node_key: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    p = _project_or_404(await session.get(Project, project_id))
    from app.services.node_xlsx_snapshot import resolve_display_xlsx_path

    xlsx, snapshot_name = resolve_display_xlsx_path(p, node_key)
    if not xlsx.exists() and snapshot_name is None:
        sheet = ProjectSheet(file_path=xlsx)
        sheet.ensure_initialized(project_id=p.id, slug=p.slug)
    if not xlsx.exists():
        raise HTTPException(status_code=404, detail="project.xlsx not found")
    if snapshot_name and str(snapshot_name).startswith("upload:"):
        fname = Path(str(snapshot_name).split(":", 1)[-1]).name
    elif snapshot_name:
        fname = Path(str(snapshot_name)).name
    else:
        fname = f"{p.slug}-project.xlsx"
    return FileResponse(
        path=str(xlsx),
        filename=fname,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.post("/{project_id}/xlsx/reload", response_model=ProjectDetail)
async def reload_xlsx(
    project_id: int, session: AsyncSession = Depends(get_session)
) -> Project:
    p = _project_or_404(await session.get(Project, project_id))
    xlsx = p.data_dir / "project.xlsx"
    if not xlsx.exists():
        raise HTTPException(status_code=404, detail="project.xlsx not found")
    await sync_project_xlsx(session, p, xlsx)
    await session.commit()
    await session.refresh(p)
    await publish_project_event(project_id, event_type="project_updated", payload={"xlsx": "reloaded"})
    return p


@router.post("/{project_id}/xlsx/upload", response_model=ProjectDetail)
async def upload_xlsx(
    project_id: int,
    file: UploadFile = File(...),
    node_key: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> Project:
    p = _project_or_404(await session.get(Project, project_id))
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="need .xlsx file")
    dest = p.data_dir / "project.xlsx"
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    import tempfile

    from app.services.xlsx_versioning import validate_xlsx

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    validation_err = validate_xlsx(tmp_path)
    if validation_err is not None:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=validation_err)
    tmp_path.replace(dest)
    if node_key:
        from app.services.node_xlsx_snapshot import clear_bound_snapshot

        clear_bound_snapshot(p, node_key)
    await sync_project_xlsx(session, p, dest)
    await session.commit()
    await session.refresh(p)
    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={"xlsx": "uploaded", "node_key": node_key},
    )
    return p


def _cell_str(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _trim_trailing_empty_cols(rows: list[list[str]]) -> list[list[str]]:
    """Drop trailing all-empty columns — do NOT pad to max_cols."""
    if not rows:
        return rows
    width = max((len(r) for r in rows), default=0)
    last = -1
    for c in range(width):
        if any((r[c] if c < len(r) else "").strip() for r in rows):
            last = c
    if last < 0:
        return [[] for _ in rows]
    return [(r + [""] * (last + 1 - len(r)))[: last + 1] for r in rows]


def _trim_trailing_empty_rows(rows: list[list[str]]) -> list[list[str]]:
    end = len(rows)
    while end > 0 and not any(str(c).strip() for c in rows[end - 1]):
        end -= 1
    return rows[:end]


def _col_letter(index0: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    n = index0 + 1
    letters: list[str] = []
    while n:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


@router.get("/{project_id}/xlsx/preview")
async def preview_xlsx(
    project_id: int,
    sheet: str | None = Query(None),
    max_rows: int = Query(80, ge=1, le=2000),
    max_cols: int = Query(40, ge=1, le=500),
    start_row: int = Query(1, ge=1, le=5000),
    row: int | None = Query(None, ge=1, le=5000),
    raw: bool = Query(False),
    node_key: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    p = _project_or_404(await session.get(Project, project_id))
    from app.services.node_xlsx_snapshot import resolve_display_xlsx_path

    xlsx, snapshot_name = resolve_display_xlsx_path(p, node_key)
    if not xlsx.exists() and snapshot_name is None:
        sheet_obj = ProjectSheet(file_path=xlsx)
        sheet_obj.ensure_initialized(project_id=p.id, slug=p.slug)
    if not xlsx.exists():
        from app.services.db_virtual_xlsx import build_virtual_project_sheets

        v_sheets = await build_virtual_project_sheets(session, p)
        sheet_names = list(v_sheets.keys())
        active = sheet if sheet in sheet_names else (sheet_names[0] if sheet_names else "план")
        all_v_rows = v_sheets.get(active, [])
        
        # Выбираем срез строк
        start_idx = max(0, start_row - 1)
        end_idx = min(len(all_v_rows), start_idx + max_rows)
        v_slice = all_v_rows[start_idx:end_idx] if all_v_rows else []
        
        # Обрезаем ширину до max_cols
        processed_rows = []
        for r in v_slice:
            processed_rows.append(r[:max_cols])
            
        processed_rows = _trim_trailing_empty_rows(processed_rows)
        processed_rows = _trim_trailing_empty_cols(processed_rows)
        
        width = max((len(r) for r in processed_rows), default=0)
        col_letters = [_col_letter(i) for i in range(width)]
        
        return {
            "path": str(xlsx),
            "sheets": sheet_names,
            "active_sheet": active,
            "headers": [],
            "rows": processed_rows,
            "cells": [],
            "start_row": start_row,
            "col_letters": col_letters,
            "truncated_rows": len(all_v_rows) > end_idx,
            "truncated_cols": False,
            "sheet_max_row": len(all_v_rows),
            "sheet_max_col": width,
            "node_key": node_key,
            "xlsx_snapshot": snapshot_name,
        }
    from openpyxl import load_workbook

    wb = load_workbook(xlsx, read_only=True, data_only=True)
    sheets = wb.sheetnames
    active = sheet if sheet in sheets else (sheets[0] if sheets else "")

    if row is not None and active:
        ws = wb[active]
        row_iter = ws.iter_rows(
            min_row=row, max_row=row, max_col=max_cols, values_only=True
        )
        row_vals = next(row_iter, None)
        cells = [_cell_str(c) for c in row_vals] if row_vals is not None else []
        while cells and not cells[-1].strip():
            cells.pop()
        wb.close()
        return {
            "path": str(xlsx),
            "sheets": sheets,
            "active_sheet": active,
            "row": row,
            "cells": cells,
            "col_letters": [_col_letter(i) for i in range(len(cells))],
            "node_key": node_key,
            "xlsx_snapshot": snapshot_name,
        }

    headers: list[str] = []
    rows: list[list[str]] = []
    sheet_max_row = 0
    sheet_max_col = 0
    truncated_rows = False
    truncated_cols = False
    if active:
        ws = wb[active]
        # read_only: dimensions may be None — fall back to requested window.
        dim_row = int(ws.max_row or 0)
        dim_col = int(ws.max_column or 0)
        sheet_max_row = dim_row
        sheet_max_col = dim_col
        end_row = start_row + max_rows - 1
        if dim_row > 0:
            end_row = min(end_row, dim_row)
        scan_cols = max_cols
        if dim_col > 0:
            truncated_cols = dim_col > max_cols
            scan_cols = min(max_cols, dim_col)
        else:
            scan_cols = max_cols

        collected = 0
        hit_row_cap = False
        for i, row_vals in enumerate(
            ws.iter_rows(
                min_row=start_row,
                max_row=end_row,
                max_col=scan_cols,
                values_only=True,
            )
        ):
            cells = [_cell_str(c) for c in (row_vals or ())]
            # Keep natural width — never pad up to max_cols (that broke the UI banner).
            if len(cells) > scan_cols:
                cells = cells[:scan_cols]
                truncated_cols = True
            if raw:
                rows.append(cells)
                collected += 1
            elif i == 0:
                headers = cells
                continue
            else:
                rows.append(cells)
                collected += 1
            if collected >= max_rows:
                hit_row_cap = True
                break

        last_loaded = start_row + len(rows) - 1 if rows else start_row - 1
        if hit_row_cap:
            truncated_rows = True
        elif dim_row > 0 and last_loaded < dim_row:
            truncated_rows = True

        rows = _trim_trailing_empty_rows(rows)
        rows = _trim_trailing_empty_cols(rows)
        if headers:
            headers_trimmed = _trim_trailing_empty_cols([headers])
            headers = headers_trimmed[0] if headers_trimmed else []
            width = max((len(r) for r in rows), default=len(headers))
            if len(headers) < width:
                headers = headers + [""] * (width - len(headers))
            else:
                headers = headers[:width]

    wb.close()
    width = max((len(r) for r in rows), default=len(headers))
    col_letters = [_col_letter(i) for i in range(width)]
    return {
        "path": str(xlsx),
        "sheets": sheets,
        "active_sheet": active,
        "headers": headers if not raw else [],
        "rows": rows,
        "start_row": start_row,
        "col_letters": col_letters,
        "truncated_rows": truncated_rows,
        "truncated_cols": truncated_cols,
        "sheet_max_row": sheet_max_row,
        "sheet_max_col": sheet_max_col,
        "node_key": node_key,
        "xlsx_snapshot": snapshot_name,
    }


@router.get("/{project_id}/montage-board")
async def montage_board(
    project_id: int,
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Сетка монтажа: озвучка, персонажи, shot1/2 картинки и видео по кадрам."""
    p = _project_or_404(await session.get(Project, project_id))
    from app.services.montage_board import build_montage_board

    try:
        board = await build_montage_board(session, p)
        # get_project_session не коммитит сам — без commit кэш R15/meta откатывается,
        # и каждый GET снова пишет сотни кадров → database is locked + Failed to fetch.
        await session.commit()
        return board
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("GET /montage-board failed project_id={}", project_id)
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось собрать монтаж: {type(e).__name__}: {e}",
        ) from e


@router.post("/{project_id}/montage-board/queue")
async def montage_board_save_queue(
    project_id: int,
    body: dict = Body(...),
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Сохранить очередь pending_ops на сервер (без запуска apply).

    Иначе очередь живёт только в React state и слетает при refresh/рестарте.
    """
    from app.services.montage_board_apply_job import get_apply_job
    from app.services.montage_board_meta import (
        montage_meta,
        public_board_meta,
        set_montage_meta,
        should_accept_queue_save,
    )

    p = _project_or_404(await session.get(Project, project_id))
    ops = list(body.get("pending_ops") or [])
    # Нормализуем: только известные типы + валидный frame.
    cleaned: list[dict] = []
    for raw in ops:
        if not isinstance(raw, dict):
            continue
        t = str(raw.get("type") or "")
        if not t.startswith(("image_", "video_")):
            continue
        try:
            fr = int(raw.get("frame_number"))
        except (TypeError, ValueError):
            continue
        if fr < 1:
            continue
        shot = 2 if raw.get("shot") == 2 else 1
        item: dict = {"type": t, "frame_number": fr, "shot": shot}
        if isinstance(raw.get("prompt"), str) and raw["prompt"].strip():
            item["prompt"] = raw["prompt"]
        if isinstance(raw.get("correction"), str) and raw["correction"].strip():
            item["correction"] = raw["correction"]
        cleaned.append(item)

    board = montage_meta(p)
    existing = list(board.get("pending_ops") or [])
    job = get_apply_job(p)
    accept, rejected = should_accept_queue_save(
        cleaned=cleaned,
        existing=existing,
        apply_running=job.get("status") == "running",
        force_clear=bool(body.get("force_clear")),
    )
    if not accept:
        return {
            "ok": False,
            "rejected": rejected,
            "pending_ops": existing,
            "meta": public_board_meta(board),
            "message": (
                "Apply ещё идёт — очередь не перезаписываем более коротким списком"
                if rejected == "apply_running"
                else "Пустая очередь не затирает существующую (нужен force_clear)"
            ),
        }

    board["pending_ops"] = cleaned
    if isinstance(body.get("video_trims"), dict):
        board["video_trims"] = body["video_trims"]
    set_montage_meta(p, board)
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", exc)
    return {"ok": True, "pending_ops": cleaned, "meta": public_board_meta(board)}


@router.post("/{project_id}/montage-board/apply")
async def montage_board_apply(
    project_id: int,
    body: dict = Body(...),
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Сохранить trim и выполнить очередь regen (без remount)."""
    from app.services.montage_board import build_montage_board
    from app.services.montage_board_apply import apply_montage_board
    from app.services.montage_board_apply_job import get_apply_job, spawn_apply_job

    p = _project_or_404(await session.get(Project, project_id))
    ops = list(body.get("pending_ops") or [])
    trims = body.get("video_trims")

    if ops:
        from app.services.step_cancel import clear_stop

        clear_stop(project_id)
        job = get_apply_job(p)
        if job.get("status") == "running":
            board = await build_montage_board(session, p)
            return {
                "started": False,
                "already_running": True,
                "ok": False,
                "job": job,
                "meta": board["meta"],
            }
        # Сразу пишем running в meta — иначе первый poll UI видит старый
        # done/error и мгновенно показывает «Генерация завершена».
        from datetime import datetime, timezone

        from app.services.montage_board_meta import montage_meta, set_montage_meta

        board = montage_meta(p)
        board["apply_job"] = {
            "status": "running",
            "error": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "total_ops": len(ops),
            "done_ops": 0,
        }
        set_montage_meta(p, board)
        await session.commit()
        try:
            await sync_project_row_to_master_db(p)
        except Exception as exc:  # noqa: BLE001
            logger.debug("sync_project_row_to_master_db failed: {}", exc)
        spawn_apply_job(project_id, video_trims=trims, pending_ops=ops)
        return {
            "started": True,
            "ok": True,
            "job": {"status": "running", "total_ops": len(ops)},
            "message": f"Генерация {len(ops)} операций запущена в фоне",
        }

    result = await apply_montage_board(
        session,
        p,
        video_trims=trims,
        pending_ops=ops,
    )
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", exc)
    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={
            "montage_board_apply": True,
            "status": "done" if result.get("ok") else "error",
            "ok": result.get("ok"),
            "errors": result.get("errors"),
        },
    )
    return result


@router.get("/{project_id}/montage-board/apply-status")
async def montage_board_apply_status(
    project_id: int,
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    from app.services.montage_board_apply_job import get_apply_job

    p = _project_or_404(await session.get(Project, project_id))
    return {"job": get_apply_job(p)}


@router.post("/{project_id}/montage-board/montage")
async def montage_board_montage(
    project_id: int,
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Кнопка «Монтаж» — remount-video в фоне (озвучка + FFmpeg)."""
    import asyncio

    from app.services.montage_board_montage_job import get_montage_job, spawn_montage_job

    p = _project_or_404(await session.get(Project, project_id))
    from app.services.project_control import clear_user_stop_gate
    from app.services.step_cancel import clear_stop

    clear_user_stop_gate(p)
    clear_stop(project_id)
    await session.flush()
    job = get_montage_job(p)
    if job.get("status") == "running":
        return {"started": False, "already_running": True, "job": job}
    spawn_montage_job(project_id)
    return {"started": True, "job": {"status": "running"}}


@router.get("/{project_id}/montage-board/montage-status")
async def montage_board_montage_status(
    project_id: int,
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    from app.services.montage_board_montage_job import get_montage_job

    p = _project_or_404(await session.get(Project, project_id))
    return {"job": get_montage_job(p)}


@router.post("/{project_id}/montage-board/recover-outsee")
async def montage_board_recover_outsee(
    project_id: int,
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Запускает фоновый скан Outsee → сохранение/замена кадров (кнопка не зависает)."""
    p = _project_or_404(await session.get(Project, project_id))
    from app.services.montage_board_meta import public_board_meta, montage_meta
    from app.services.montage_outsee_recover_job import (
        get_recover_job,
        spawn_recover_job,
    )
    from app.services.outsee_lane import outsee_lane_busy
    from app.services.step_cancel import clear_stop

    job = get_recover_job(p)
    if job.get("status") == "running":
        return {
            "started": False,
            "already_running": True,
            "ok": False,
            "job": job,
            "meta": public_board_meta(montage_meta(p)),
            "message": "Уже забираем правки из Outsee",
        }
    if outsee_lane_busy():
        raise HTTPException(
            status_code=409,
            detail=(
                "Outsee занят Generate/apply — дождитесь окончания и нажмите снова"
            ),
        )
    clear_stop(project_id)
    spawn_recover_job(project_id)
    return {
        "started": True,
        "ok": True,
        "job": {"status": "running"},
        "message": "Забираем правки из Outsee в фоне",
        "meta": public_board_meta(montage_meta(p)),
    }


@router.get("/{project_id}/montage-board/recover-outsee-status")
async def montage_board_recover_outsee_status(
    project_id: int,
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    from app.services.montage_outsee_recover_job import get_recover_job

    p = _project_or_404(await session.get(Project, project_id))
    return {"job": get_recover_job(p)}


@router.post("/{project_id}/montage-board/swap-shots")
async def montage_board_swap_shots(
    project_id: int,
    frame_number: int = Query(..., ge=1),
    kind: str = Query("both", pattern="^(image|video|both)$"),
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Поменять местами shot1 ↔ shot2 (картинки и/или видео + промты)."""
    from app.services.montage_board_assets import swap_shot_media

    p = _project_or_404(await session.get(Project, project_id))
    result = await swap_shot_media(
        session, p, frame_number, kind=kind  # type: ignore[arg-type]
    )
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", exc)
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("reason") or "нечего менять",
        )
    return result


@router.post("/{project_id}/montage-board/swap-slots")
async def montage_board_swap_slots(
    project_id: int,
    kind: str = Query(..., pattern="^(image|video)$"),
    a_frame: int = Query(..., ge=1),
    a_shot: int = Query(..., ge=1, le=2),
    b_frame: int = Query(..., ge=1),
    b_shot: int = Query(..., ge=1, le=2),
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Обмен двух слотов одного типа между любыми кадрами (кнопка↔кнопка в UI)."""
    from app.services.montage_board_assets import swap_media_slots

    p = _project_or_404(await session.get(Project, project_id))
    result = await swap_media_slots(
        session,
        p,
        kind=kind,  # type: ignore[arg-type]
        a_frame=a_frame,
        a_shot=a_shot,
        b_frame=b_frame,
        b_shot=b_shot,
    )
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", exc)
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("reason") or "не удалось поменять местами",
        )
    return result


@router.post("/{project_id}/montage-board/move-image")
async def montage_board_move_image(
    project_id: int,
    from_frame: int = Query(..., ge=1),
    from_shot: int = Query(..., ge=1, le=2),
    to_frame: int = Query(..., ge=1),
    to_shot: int = Query(..., ge=1, le=2),
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Перенести картинку в другой слот (в т.ч. пустой); если цель занята — swap."""
    from app.services.montage_board_assets import move_scene_image

    p = _project_or_404(await session.get(Project, project_id))
    result = await move_scene_image(
        session,
        p,
        from_frame=from_frame,
        from_shot=from_shot,
        to_frame=to_frame,
        to_shot=to_shot,
    )
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", exc)
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("reason") or "не удалось перенести",
        )
    return result


@router.post("/{project_id}/montage-board/delete-image")
async def montage_board_delete_image(
    project_id: int,
    frame_number: int = Query(..., ge=1),
    shot: int = Query(1, ge=1, le=2),
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    from app.services.montage_board_assets import delete_scene_image
    from app.services.montage_board_meta import mark_stale_videos, montage_meta, set_montage_meta

    p = _project_or_404(await session.get(Project, project_id))
    deleted = await delete_scene_image(session, p, frame_number, shot=shot)
    board = montage_meta(p)
    mark_stale_videos(board, frame_number, shot=shot)
    set_montage_meta(p, board)
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", exc)
    return {"ok": deleted, "frame_number": frame_number, "shot": shot}


@router.post("/{project_id}/montage-board/delete-video")
async def montage_board_delete_video(
    project_id: int,
    frame_number: int = Query(..., ge=1),
    shot: int = Query(1, ge=1, le=2),
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    from app.services.montage_board_assets import delete_scene_video

    p = _project_or_404(await session.get(Project, project_id))
    deleted = await delete_scene_video(session, p, frame_number, shot=shot)
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", exc)
    return {"ok": deleted, "frame_number": frame_number, "shot": shot}


@router.post("/{project_id}/montage-board/upload-image")
async def montage_board_upload_image(
    project_id: int,
    frame_number: int = Query(..., ge=1),
    shot: int = Query(1, ge=1, le=2),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    from app.services.montage_board_assets import save_scene_image_upload
    from app.services.montage_board_meta import mark_stale_videos, montage_meta, set_montage_meta

    p = _project_or_404(await session.get(Project, project_id))
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="пустой файл")
    suffix = Path(file.filename or "upload.png").suffix or ".png"
    path = await save_scene_image_upload(
        session, p, frame_number, shot=shot, content=content, suffix=suffix
    )
    board = montage_meta(p)
    mark_stale_videos(board, frame_number, shot=shot)
    set_montage_meta(p, board)
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", exc)
    return {
        "ok": True,
        "path": str(path),
        "preview_url": f"/api/files?path={path}",
        "frame_number": frame_number,
        "shot": shot,
    }


@router.post("/{project_id}/montage-board/upload-video")
async def montage_board_upload_video(
    project_id: int,
    frame_number: int = Query(..., ge=1),
    shot: int = Query(1, ge=1, le=2),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    from app.services.montage_board_assets import save_scene_video_upload
    from app.services.montage_board_meta import clear_stale_video, montage_meta, set_montage_meta

    p = _project_or_404(await session.get(Project, project_id))
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="пустой файл")
    suffix = Path(file.filename or "upload.mp4").suffix or ".mp4"
    path = await save_scene_video_upload(
        session, p, frame_number, shot=shot, content=content, suffix=suffix
    )
    board = montage_meta(p)
    clear_stale_video(board, frame_number, shot)
    set_montage_meta(p, board)
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", exc)
    return {
        "ok": True,
        "path": str(path),
        "preview_url": f"/api/files?path={path}",
        "frame_number": frame_number,
        "shot": shot,
    }


@router.post("/{project_id}/montage-board/upload-voice")
async def montage_board_upload_voice(
    project_id: int,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Сохраняет озвучку как ``audio/voice_full.<ext>`` — так её видит монтаж/assemble."""
    from sqlalchemy.orm.attributes import flag_modified

    p = _project_or_404(await session.get(Project, project_id))
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="пустой файл")
    audio_dir = p.data_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "voice.mp3").suffix.lower() or ".mp3"
    if suffix not in {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}:
        suffix = ".mp3"
    # Каноническое имя, которое find_voice_full_on_disk / remount подхватывают.
    dest = audio_dir / f"voice_full{suffix}"
    dest.write_bytes(content)
    meta = dict(p.meta or {})
    meta["montage_voice_path"] = str(dest)
    p.meta = meta
    flag_modified(p, "meta")
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", exc)
    return {"ok": True, "path": str(dest), "filename": dest.name}


@router.post("/{project_id}/montage-board/upload-music")
async def montage_board_upload_music(
    project_id: int,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Сохраняет BGM как ``music/bgm.<ext>`` — канон для resolve_bgm."""
    from sqlalchemy.orm.attributes import flag_modified

    p = _project_or_404(await session.get(Project, project_id))
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="пустой файл")
    music_dir = p.data_dir / "music"
    music_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "bgm.mp3").suffix.lower() or ".mp3"
    if suffix not in {".mp3", ".wav", ".m4a", ".ogg", ".flac"}:
        suffix = ".mp3"
    dest = music_dir / f"bgm{suffix}"
    dest.write_bytes(content)
    meta = dict(p.meta or {})
    meta["bgm_path"] = str(dest)
    p.meta = meta
    flag_modified(p, "meta")
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", exc)
    return {"ok": True, "path": str(dest), "filename": dest.name}


@router.get("/{project_id}/assets")
async def list_project_assets(
    project_id: int,
    kind: str = Query("all", pattern="^(all|hero|items|images|videos|audio|final|text)$"),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    p = _project_or_404(await session.get(Project, project_id))
    out: list[dict] = []

    # DB artifacts
    if kind in ("all", "hero", "items", "images", "videos", "audio", "final"):
        from sqlalchemy import select

        arts = (
            await session.execute(select(Artifact).where(Artifact.project_id == project_id))
        ).scalars().all()
        kind_map = {
            "hero": {ArtifactKind.hero_reference},
            "items": {ArtifactKind.item_reference},
            "images": {ArtifactKind.scene_image},
            "videos": {ArtifactKind.scene_video},
            "audio": {ArtifactKind.audio, ArtifactKind.subtitle},
            "final": {ArtifactKind.final_video},
        }
        for a in arts:
            if kind != "all" and a.kind not in kind_map.get(kind, set()):
                continue
            rel = _rel_path(a.path) if a.path else None
            out.append(
                {
                    "source": "artifact",
                    "id": a.uuid,
                    "kind": a.kind.value if hasattr(a.kind, "value") else str(a.kind),
                    "path": a.path,
                    "preview_url": f"/api/artifacts/{a.uuid}/file" if a.uuid else None,
                    "frame_id": a.frame_id,
                    "meta": a.meta or {},
                }
            )

    # Disk scan (fallback)
    base = p.data_dir
    subdirs = {
        "hero": ["characters", "hero"],
        "items": ["items", "objects"],
        "images": ["scenes", "images"],
        "videos": ["videos", "clips"],
        "audio": ["audio", "subs", "music", "sfx"],
        "final": ["final"],
        "text": [],
    }
    if kind in ("all", "text"):
        for name in ("voiceover.txt", "script.txt", "general_plan.txt"):
            fp = base / name
            if fp.exists():
                out.append(
                    {
                        "source": "file",
                        "id": name,
                        "kind": "text",
                        "path": str(fp),
                        "preview_url": f"/api/files?path={fp}",
                        "label": name,
                    }
                )
        plan = base / "project.xlsx"
        if plan.exists():
            out.append(
                {
                    "source": "file",
                    "id": "project.xlsx",
                    "kind": "xlsx",
                    "path": str(plan),
                    "preview_url": None,
                    "label": "Таблица проекта",
                }
            )

    scan_kind = kind if kind in subdirs else None
    if scan_kind or kind == "all":
        keys = [scan_kind] if scan_kind else list(subdirs.keys())
        for k in keys:
            for sub in subdirs.get(k, []):
                d = base / sub
                if not d.is_dir():
                    continue
                for fp in sorted(d.rglob("*")):
                    if not fp.is_file():
                        continue
                    if fp.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".wav", ".mp3"}:
                        continue
                    rel = _rel_path(str(fp))
                    out.append(
                        {
                            "source": "file",
                            "id": rel,
                            "kind": k,
                            "path": str(fp),
                            "preview_url": f"/api/files?path={fp}",
                            "label": fp.name,
                        }
                    )
    return out


@router.post("/{project_id}/assets/hero/replace")
async def replace_hero_image(
    project_id: int,
    file: UploadFile = File(...),
    replace_path: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Заменить reference-картинку персонажа (файл в data/.../characters/)."""
    p = _project_or_404(await session.get(Project, project_id))
    if not file.filename:
        raise HTTPException(status_code=400, detail="empty filename")
    ext = Path(file.filename).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(status_code=400, detail="need image file (.png, .jpg, .webp)")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")

    chars_dir = p.data_dir / "characters"
    chars_dir.mkdir(parents=True, exist_ok=True)

    dest: Path
    if replace_path:
        candidate = Path(replace_path)
        if not candidate.is_absolute():
            candidate = Path(settings.data_dir) / replace_path
        try:
            candidate.resolve().relative_to(p.data_dir.resolve())
        except ValueError as e:
            raise HTTPException(status_code=400, detail="replace_path outside project") from e
        dest = candidate
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        stem = Path(file.filename).stem or "hero"
        dest = chars_dir / f"{stem}{ext}"

    dest.write_bytes(content)
    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={"hero_replaced": str(dest.name)},
    )
    rel = _rel_path(str(dest))
    return {
        "path": str(dest),
        "preview_url": f"/api/files?path={dest}",
        "id": rel,
    }


def _rel_path(path: str | None) -> str:
    if not path:
        return ""
    try:
        p = Path(path)
        data = Path(settings.data_dir).resolve()
        return str(p.resolve().relative_to(data))
    except Exception:
        return path


@router.patch("/{project_id}/excel-gpt/{node_key}")
async def patch_excel_gpt_config(
    project_id: int,
    node_key: str,
    payload: dict,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.gpt_operator import patch_operator_config

    p = _project_or_404(await session.get(Project, project_id))
    # Новые поля оператора (+ legacy) через единый patch.
    op_keys = {
        "label",
        "inputSource",
        "uploadedFileName",
        "uploadedFileNames",
        "uploadedPreviewUrl",
        "slotIndex",
        "workMode",
        "role",
        "outputMode",
        "emitKinds",
        "useSnapshot",
        "takeFromEdges",
        "transport",
        "checkMode",
        "checkFix",
        "checkPromptSource",
    }
    if any(
        k in payload
        for k in (
            "role",
            "outputMode",
            "emitKinds",
            "useSnapshot",
            "takeFromEdges",
            "transport",
            "uploadedFileNames",
            "checkMode",
            "checkFix",
            "checkPromptSource",
        )
    ):
        resolved = patch_operator_config(p, node_key, {k: payload[k] for k in op_keys if k in payload})
        flag_modified(p, "meta")
        await session.commit()
        return {"ok": True, "config": resolved.get("config") or {}, "resolve": resolved}

    meta = dict(p.meta or {})
    configs = dict(meta.get("excel_gpt_nodes") or {})
    cur = dict(configs.get(node_key) or {})
    for key in (
        "label",
        "inputSource",
        "uploadedFileName",
        "uploadedPreviewUrl",
        "slotIndex",
        "workMode",
    ):
        if key in payload:
            cur[key] = payload[key]
    configs[node_key] = cur
    meta["excel_gpt_nodes"] = configs
    meta["active_excel_gpt_node_key"] = node_key
    p.meta = meta
    flag_modified(p, "meta")
    await session.commit()
    return {"ok": True, "config": cur}


@router.get("/{project_id}/gpt-operator/{node_key}/resolve")
async def gpt_operator_resolve(
    project_id: int,
    node_key: str,
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    from app.services.gpt_operator import resolve_operator

    p = _project_or_404(await session.get(Project, project_id))
    return resolve_operator(p, node_key)


@router.patch("/{project_id}/gpt-operator/{node_key}")
async def gpt_operator_patch(
    project_id: int,
    node_key: str,
    payload: dict,
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    import asyncio

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.gpt_operator import patch_operator_config

    p = _project_or_404(await session.get(Project, project_id))
    body = dict(payload) if isinstance(payload, dict) else {}
    body.setdefault("transport", "api")
    resolved = patch_operator_config(p, node_key, body)
    flag_modified(p, "meta")
    # Пока воркер держит SQLite (hero/GPT/Outsee), commit может ждать/падать
    # database is locked — UI «Пульт» тогда висит с disabled кнопками.
    last_err: Exception | None = None
    for attempt in range(1, 6):
        try:
            await session.commit()
            last_err = None
            break
        except OperationalError as e:
            last_err = e
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            await session.rollback()
            # Перечитать и заново применить patch после rollback.
            p = _project_or_404(await session.get(Project, project_id))
            resolved = patch_operator_config(p, node_key, body)
            flag_modified(p, "meta")
            await asyncio.sleep(0.35 * attempt)
    if last_err is not None:
        raise HTTPException(
            status_code=503,
            detail=(
                "БД занята воркером пайплайна (hero/картинки/GPT). "
                "Подождите 10–30 с и нажмите роль ещё раз — бэкенд жив."
            ),
        ) from last_err
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[#{}] gpt_operator_patch sync to master: {}", project_id, exc)
    return {"ok": True, "resolve": resolved}


@router.patch("/{project_id}/canvas-edges/{edge_id}")
async def patch_canvas_edge_kind(
    project_id: int,
    edge_id: str,
    payload: dict,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.gpt_operator import normalize_edge_kind, set_edge_kind_in_canvas

    p = _project_or_404(await session.get(Project, project_id))
    kind = normalize_edge_kind((payload or {}).get("kind"))
    updated = set_edge_kind_in_canvas(p, edge_id, kind)
    if updated is None:
        raise HTTPException(status_code=404, detail="edge not found in canvas_graph")
    flag_modified(p, "meta")
    await session.commit()
    return {"ok": True, "edge": updated}


@router.post("/{project_id}/excel-gpt/{node_key}/upload")
async def upload_excel_gpt_file(
    project_id: int,
    node_key: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.excel_gpt_node import (
        is_allowed_upload_filename,
        node_config,
        upload_dir,
        upload_file_path,
    )
    from app.services.gpt_operator import save_check_agent_file

    p = _project_or_404(await session.get(Project, project_id))
    if not file.filename:
        raise HTTPException(status_code=400, detail="need filename")
    safe_name = Path(file.filename).name
    if not is_allowed_upload_filename(safe_name):
        raise HTTPException(
            status_code=400,
            detail="нужен файл .xlsx/.txt/.md/.json/.csv/.pdf/.png/.jpg/.webp/.mp4/.webm…",
        )
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")

    ext = Path(safe_name).suffix.lower()
    cfg_before = node_config(p, node_key)
    # Режим «Проверка»: .txt/.md = критерии агента, не входные данные.
    # Иначе «загрузил промт» лежит во вложениях, а отчёт всё ещё по старому builtin.
    if bool(cfg_before.get("checkMode")) and ext in {".txt", ".md"}:
        try:
            result = save_check_agent_file(
                p, node_key, original_name=safe_name, content=content
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        flag_modified(p, "meta")
        await session.commit()
        return {
            "ok": True,
            "fileName": result["fileName"],
            "path": result["path"],
            "chars": result.get("chars"),
            "usedAsCheckAgent": True,
            "isImage": False,
            "preview_url": None,
            "uploadedFileNames": list(
                (result.get("resolve") or {}).get("config", {}).get("uploadedFileNames")
                or []
            ),
            "resolve": result.get("resolve"),
        }

    dest_dir = upload_dir(p, node_key)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_file_path(p, node_key, safe_name)
    dest.write_bytes(content)
    # mtime новее старого снимка → preview берёт upload
    try:
        import os
        import time

        now = time.time()
        os.utime(dest, (now, now))
    except OSError:
        pass

    is_image = ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    is_xlsx = ext in {".xlsx", ".xlsm", ".xls"}
    preview = (
        f"/api/files?path={dest}"
        if is_image or ext in {".mp4", ".webm", ".txt", ".md"}
        else None
    )

    if is_xlsx:
        from app.services.node_xlsx_snapshot import clear_bound_snapshot

        clear_bound_snapshot(p, node_key)

    meta = dict(p.meta or {})
    configs = dict(meta.get("excel_gpt_nodes") or {})
    cur = dict(configs.get(node_key) or {})
    cur["inputSource"] = "image" if is_image else "upload"
    cur["uploadedFileName"] = safe_name
    prev_names = [str(x) for x in (cur.get("uploadedFileNames") or []) if x]
    if is_xlsx:
        # Подмена Excel: один актуальный файл; со стрелок больше не берём —
        # иначе в списке/GPT остаётся старый project.xlsx.
        names = [
            n
            for n in prev_names
            if Path(n).suffix.lower() not in {".xlsx", ".xlsm", ".xls"}
        ]
        names.append(safe_name)
        cur["takeFromEdges"] = False
        # Удалить старые xlsx в папке uploads этой ноды (кроме нового).
        for old in list(dest_dir.iterdir()):
            if (
                old.is_file()
                and old.suffix.lower() in {".xlsx", ".xlsm", ".xls"}
                and old.name != safe_name
            ):
                try:
                    old.unlink()
                except OSError:
                    pass
    else:
        names = list(prev_names)
        if safe_name not in names:
            names.append(safe_name)
    cur["uploadedFileNames"] = names
    cur["uploadedPreviewUrl"] = preview
    cur["transport"] = "api"
    cur["label"] = safe_name
    configs[node_key] = cur
    meta["excel_gpt_nodes"] = configs
    meta["active_excel_gpt_node_key"] = node_key
    p.meta = meta
    flag_modified(p, "meta")
    await session.commit()
    return {
        "ok": True,
        "fileName": safe_name,
        "path": str(dest),
        "isImage": is_image,
        "preview_url": preview,
        "uploadedFileNames": names,
        "usedAsCheckAgent": False,
        "replacedXlsx": is_xlsx,
        "takeFromEdges": False if is_xlsx else cur.get("takeFromEdges", True),
        "inputSource": cur.get("inputSource"),
    }


@router.post("/{project_id}/gpt-operator/{node_key}/check-agent")
async def upload_check_agent_file(
    project_id: int,
    node_key: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Загрузить .txt/.md агента проверки (режим «Готовый агент»)."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.gpt_operator import save_check_agent_file

    p = _project_or_404(await session.get(Project, project_id))
    if not file.filename:
        raise HTTPException(status_code=400, detail="need filename")
    content = await file.read()
    try:
        result = save_check_agent_file(
            p, node_key, original_name=file.filename, content=content
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    flag_modified(p, "meta")
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[#{}] upload_check_agent_file sync to master: {}", project_id, exc)
    return result


@router.delete("/{project_id}/gpt-operator/{node_key}/check-agent")
async def delete_check_agent_file(
    project_id: int,
    node_key: str,
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Сбросить загруженный агент → снова builtin из prompts/check_operator."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.gpt_operator import clear_check_agent_file

    p = _project_or_404(await session.get(Project, project_id))
    result = clear_check_agent_file(p, node_key)
    flag_modified(p, "meta")
    await session.commit()
    try:
        await sync_project_row_to_master_db(p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[#{}] delete_check_agent_file sync to master: {}", project_id, exc)
    return result


@router.get("/{project_id}/gpt-operator/{node_key}/check-agent")
async def get_check_agent_file(
    project_id: int,
    node_key: str,
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Текст агента проверки (свой файл или builtin) — кнопка «Просмотр» в Studio."""
    from app.services.gpt_operator import load_check_agent_view

    p = _project_or_404(await session.get(Project, project_id))
    view = load_check_agent_view(p, node_key)
    if view is None:
        raise HTTPException(
            status_code=404,
            detail="агент не найден — загрузите .txt или проведите стрелку от результата",
        )
    return view


@router.get("/{project_id}/gpt-operator/{node_key}/check-prompt-preview")
async def get_check_prompt_preview(
    project_id: int,
    node_key: str,
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Финальный master-промт проверки ровно как при запуске — просмотр в Studio."""
    from pathlib import Path

    from app.orchestrator.steps.enrich_xlsx import _get_accompanying_text
    from app.services.excel_gpt_node import EXCEL_GPT_STEP_CODE
    from app.services.gpt_operator import (
        append_vision_hint_for_upstream,
        assemble_check_agent_prompt,
        assemble_check_master_prompt,
        collect_source_prompts,
        operator_config,
        resolve_check_report_format,
        resolve_operator,
        sanitize_check_reviewer_notes,
    )

    p = _project_or_404(await session.get(Project, project_id))
    cfg = operator_config(p, node_key)
    if not bool(cfg.get("checkMode")):
        raise HTTPException(status_code=400, detail="у ноды не включена «Проверка»")
    check_fix = bool(cfg.get("checkFix", True))
    cps = str(cfg.get("checkPromptSource") or "upstream").strip().lower()
    if cps not in ("upstream", "agent"):
        cps = "upstream"

    resolved = resolve_operator(p, node_key)
    img_ext = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    has_vision = any(
        Path(str(f.get("name") or f.get("path") or "")).suffix.lower() in img_ext
        for f in (resolved.get("files") or [])
        if f.get("ok")
    )
    db_sot = not has_vision
    accompanying = _get_accompanying_text(p, EXCEL_GPT_STEP_CODE)
    reviewer_notes = sanitize_check_reviewer_notes(accompanying or "")
    report_fmt, _ = resolve_check_report_format(p, node_key)

    if cps == "agent":
        master, agent_step = assemble_check_agent_prompt(
            p,
            node_key,
            check_fix=check_fix,
            reviewer_notes=reviewer_notes,
            report_format=report_fmt,
            db_sot=db_sot,
        )
        source = f"agent:{agent_step}" if agent_step else "agent"
    else:
        sources = collect_source_prompts(p, node_key)
        ok_sources = [s for s in sources if s.get("ok")]
        if not ok_sources:
            raise HTTPException(
                status_code=404, detail="нет исходного промта для проверки"
            )
        master = assemble_check_master_prompt(
            ok_sources,
            check_fix=check_fix,
            reviewer_notes=reviewer_notes,
            report_format=report_fmt,
            db_sot=db_sot,
        )
        source = ",".join(str(s.get("nodeKey") or "") for s in ok_sources)
    master = append_vision_hint_for_upstream(p, node_key, master)
    return {
        "text": master,
        "chars": len(master),
        "mode": "fix" if check_fix else "report_only",
        "checkPromptSource": cps,
        "source": source,
        "dbSot": db_sot,
    }


@router.get("/{project_id}/gpt-operator/{node_key}/source-prompt")
async def get_gpt_operator_source_prompt(
    project_id: int,
    node_key: str,
    source: str,
    session: AsyncSession = Depends(get_project_session),
) -> dict:
    """Текст мастер-промта ноды-источника (критерии проверки) — просмотр в Studio."""
    from app.services.gpt_operator import collect_source_prompts

    p = _project_or_404(await session.get(Project, project_id))
    for s in collect_source_prompts(p, node_key):
        if str(s.get("nodeKey") or "") == source:
            if not s.get("ok"):
                raise HTTPException(
                    status_code=404,
                    detail=str(s.get("error") or "промт не прочитан"),
                )
            return {
                "nodeKey": source,
                "variant": s.get("variant"),
                "chars": int(s.get("chars") or 0),
                "text": str(s.get("text") or ""),
            }
    raise HTTPException(status_code=404, detail="источник не найден")


@router.post("/{project_id}/excel-gpt/remap-keys")
async def remap_excel_gpt_keys(
    project_id: int,
    payload: dict,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.excel_gpt_node import remap_node_keys_in_meta

    p = _project_or_404(await session.get(Project, project_id))
    raw = payload.get("mapping")
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="mapping dict required")
    mapping = {str(k): str(v) for k, v in raw.items() if k and v}
    remapped = remap_node_keys_in_meta(p, mapping)
    flag_modified(p, "meta")
    await session.commit()
    return {"ok": True, "remapped": remapped}


@router.get("/{project_id}/storage/{node_key}/resolve")
async def storage_resolve(
    project_id: int,
    node_key: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Прочитать/досинковать файлы хранилища без UPDATE projects (anti-lock)."""
    from app.services.storage_node import resolve_storage

    p = _project_or_404(await session.get(Project, project_id))
    # touch_project_meta=False: иначе GET держит write-lock и UI лагает на GPT.
    return resolve_storage(p, node_key, touch_project_meta=False)


@router.patch("/{project_id}/storage/{node_key}")
async def storage_patch(
    project_id: int,
    node_key: str,
    payload: dict,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.storage_node import patch_config, resolve_storage

    p = _project_or_404(await session.get(Project, project_id))
    body = dict(payload) if isinstance(payload, dict) else {}
    cfg = patch_config(p, node_key, body)
    flag_modified(p, "meta")
    await session.commit()
    return {"ok": True, "config": cfg, "resolve": resolve_storage(p, node_key, auto_sync=False)}


@router.post("/{project_id}/storage/{node_key}/sync")
async def storage_sync(
    project_id: int,
    node_key: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Забрать файлы с входящих стрелок в папку этой ноды."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.storage_node import sync_from_edges

    p = _project_or_404(await session.get(Project, project_id))
    result = sync_from_edges(p, node_key)
    flag_modified(p, "meta")
    await session.commit()
    return {"ok": True, **result}


@router.post("/{project_id}/storage/{node_key}/upload")
async def storage_upload(
    project_id: int,
    node_key: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.services.storage_node import resolve_storage, save_upload

    p = _project_or_404(await session.get(Project, project_id))
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="пустой файл")
    dest = save_upload(p, node_key, file.filename or "upload.bin", raw)
    return {
        "ok": True,
        "fileName": dest.name,
        "path": str(dest),
        "resolve": resolve_storage(p, node_key),
    }


@router.delete("/{project_id}/storage/{node_key}/files")
async def storage_clear_files(
    project_id: int,
    node_key: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.services.storage_node import clear_storage, resolve_storage

    p = _project_or_404(await session.get(Project, project_id))
    n = clear_storage(p, node_key)
    return {"ok": True, "removed": n, "resolve": resolve_storage(p, node_key)}


@router.get("/{project_id}/storage/{node_key}/download.zip")
async def storage_download_zip(
    project_id: int,
    node_key: str,
    session: AsyncSession = Depends(get_session),
):
    """Скачать все файлы хранилища одним zip."""
    from fastapi.responses import FileResponse

    from app.services.storage_node import build_storage_zip

    p = _project_or_404(await session.get(Project, project_id))
    try:
        path = build_storage_zip(p, node_key)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in node_key)[:40]
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"storage_{safe_key}.zip",
    )


@router.post("/parents/disable-auto-mode")
async def disable_auto_mode_all_parents(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Выключить автопродвижение у всех родительских проектов (воркер перестанет сам жать шаги)."""
    from app.services.mass_factory import mass_parent_id

    rows = (await session.execute(select(Project))).scalars().all()
    disabled = 0
    for p in rows:
        if mass_parent_id(p) is not None:
            continue
        if p.auto_mode:
            p.auto_mode = False
            disabled += 1
            await publish_project_event(
                p.id,
                event_type="project_updated",
                payload={"auto_mode": False},
            )
    await session.commit()
    return {"parents_total": sum(1 for p in rows if mass_parent_id(p) is None), "disabled": disabled}


@router.get("/audio-align/methods")
async def audio_align_methods_list() -> dict:
    """Список методик разбора аудио → R15 (A/B в Studio)."""
    from app.services.audio_align_methods import list_align_methods

    return {"methods": list_align_methods()}


@router.get("/{project_id}/asr-words")
async def project_asr_words(
    project_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Word-level транскрибация из БД (таблица asr_words)."""
    from app.services.asr_words_store import asr_words_to_dicts, load_project_asr_words

    _project_or_404(await session.get(Project, project_id))
    rows = await load_project_asr_words(session, project_id)
    words = asr_words_to_dicts(rows)
    run_uuid = words[0]["run_uuid"] if words else None
    backend = words[0]["backend"] if words else None
    return {
        "project_id": project_id,
        "count": len(words),
        "run_uuid": run_uuid,
        "backend": backend,
        "words": words,
    }


@router.post("/{project_id}/audio-align")
async def audio_align_start(
    project_id: int,
    method: str = Query("auto"),
    force_asr: bool = Query(False),
    run_assemble: bool = Query(True),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Кнопка «Разбор аудио» — выбранная методика → R15 → assemble в фоне."""
    from app.services.audio_align_job import get_audio_align_job, spawn_audio_align_job
    from app.services.audio_align_methods import resolve_align_method
    from app.services.project_control import clear_user_stop_gate
    from app.services.step_cancel import clear_stop

    try:
        method_id = resolve_align_method(method)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    p = _project_or_404(await session.get(Project, project_id))
    job = get_audio_align_job(p)
    if job.get("status") == "running":
        return {"started": False, "already_running": True, "job": job}

    clear_user_stop_gate(p)
    clear_stop(project_id)
    await session.flush()
    spawn_audio_align_job(
        project_id,
        method=method_id,
        force_asr=force_asr,
        run_assemble=run_assemble,
    )
    return {
        "started": True,
        "job": {"status": "running", "method": method_id},
    }


@router.get("/{project_id}/audio-align/status")
async def audio_align_status(
    project_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.services.audio_align_job import get_audio_align_job

    p = _project_or_404(await session.get(Project, project_id))
    last = None
    if isinstance(p.meta, dict):
        last = p.meta.get("audio_align_last")
    return {"job": get_audio_align_job(p), "last": last}


@router.post("/{project_id}/remount-video")
async def remount_project_video(
    project_id: int,
    audio_only: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Перемонтаж: синхрон xlsx→кадры, Whisper по озвучке, новая сборка (видеоклипы не трогаем)."""
    from app.services.remount_video import remount_video

    p = _project_or_404(await session.get(Project, project_id))
    result = await remount_video(session, p, run_assemble=not audio_only)
    await session.commit()
    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={
            "remount_video": True,
            "done": result.get("done"),
            "error": result.get("error"),
            "final_video": result.get("final_video"),
        },
    )
    if result.get("error") and not result.get("done"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/restore-original-voiceover")
async def restore_all_parents_voiceover(
    dry_run: bool = Query(False),
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Восстановить исходный voiceover у всех родительских проектов."""
    from app.services.voiceover_recovery import restore_all_parent_voiceovers

    summary = await restore_all_parent_voiceovers(
        session, dry_run=dry_run, force=force
    )
    if summary.get("restored"):
        for row in summary.get("results", []):
            if row.get("restored"):
                await publish_project_event(
                    int(row["project_id"]),
                    event_type="project_updated",
                    payload={"voiceover_restored": True, "source": row.get("source")},
                )
    return summary


@router.post("/{project_id}/restore-original-voiceover")
async def restore_project_voiceover(
    project_id: int,
    dry_run: bool = Query(False),
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Восстановить исходный voiceover одного проекта."""
    from app.services.voiceover_recovery import restore_original_voiceover

    from app.services.mass_factory import mass_parent_id
    from app.services.voiceover_recovery import is_parent_project

    p = _project_or_404(await session.get(Project, project_id))
    if not is_parent_project(p):
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "child_project_skipped",
                "mass_parent_id": mass_parent_id(p),
                "hint": "восстановление только для родительских проектов",
            },
        )
    result = await restore_original_voiceover(
        session, p, dry_run=dry_run, force=force
    )
    if result.get("restored"):
        await session.commit()
        await publish_project_event(
            project_id,
            event_type="project_updated",
            payload={"voiceover_restored": True, "source": result.get("source")},
        )
    return result


@router.get("/{project_id}/original-voiceover-preview")
async def preview_original_voiceover(
    project_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Показать, откуда будет взят исходный voiceover (без записи)."""
    from app.services.voiceover_recovery import find_original_voiceover

    from app.services.voiceover_recovery import is_parent_project

    p = _project_or_404(await session.get(Project, project_id))
    if not is_parent_project(p):
        raise HTTPException(
            status_code=400,
            detail="preview только для родительских проектов",
        )
    cand = await find_original_voiceover(session, p)
    if cand is None:
        return {
            "project_id": project_id,
            "found": False,
            "preview": None,
            "source": None,
            "chars": 0,
        }
    return {
        "project_id": project_id,
        "found": True,
        "source": cand.source,
        "chars": len(cand.text),
        "preview": cand.text[:500],
    }


@router.get("/{project_id}/harness/status")
async def harness_status(
    project_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Агентский снимок ops_telemetry + хвост events.jsonl."""
    from app.services.agent_harness import harness_status_payload

    p = _project_or_404(await session.get(Project, project_id))
    return harness_status_payload(p)


@router.post("/{project_id}/harness/verify")
async def harness_verify(
    project_id: int,
    allow_repair: bool = Query(False),
    include_http: bool = Query(True),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Проверить disk/R48/node_runs + HTTP artifacts/assets/xlsx; опц. soft-start одного шага."""
    from app.services.agent_harness import HARNESS_FORBIDDEN_STEPS, run_harness_verify

    p = _project_or_404(await session.get(Project, project_id))
    if allow_repair:
        # Extra guard: never repair forbidden via query tricks inside service
        pass
    report = await run_harness_verify(
        session,
        p,
        allow_repair=allow_repair,
        include_http=include_http,
    )
    await session.commit()
    await session.refresh(p)
    await publish_project_event(
        project_id,
        event_type="harness_verify",
        payload={"ok": report.ok, "next_action": report.next_action},
    )
    out = report.to_dict()
    out["forbidden_steps"] = sorted(HARNESS_FORBIDDEN_STEPS)
    return out


