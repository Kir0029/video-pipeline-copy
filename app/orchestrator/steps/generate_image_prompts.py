"""Шаг img_pr: промты картинок через apply-ops (DB SoT)."""

from __future__ import annotations

from aiogram import Bot
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Frame, FrameStatus, Project, ProjectStatus
from app.services.step_cancel import StepCancelledError, raise_if_cancelled
from app.services.vo_shot_expand import is_shot_child
from app.storage import for_project as _sheet_for_project


def is_parent_grid_frame(fr: Frame) -> bool:
    """True для базового родительского кадра сетки (не дочерний шот покрытия)."""
    if is_shot_child(fr):
        return False
    attrs = fr.attrs if isinstance(fr.attrs, dict) else {}
    if attrs.get("is_shot") is True or attrs.get("shot_child") is True:
        return False
    if str(attrs.get("role") or "").lower() == "shot":
        return False
    return True


def _frames_needing_image_prompt(frames: list[Frame]) -> list[Frame]:
    return [
        fr
        for fr in frames
        if (fr.voiceover_text or "").strip()
        and not (fr.image_prompt or "").strip()
        and is_parent_grid_frame(fr)
    ]


def _frames_with_image_prompt(frames: list[Frame]) -> list[Frame]:
    return [
        fr
        for fr in frames
        if (fr.voiceover_text or "").strip()
        and (fr.image_prompt or "").strip()
        and is_parent_grid_frame(fr)
    ]


async def _reload_frames(session: AsyncSession, project_id: int) -> list[Frame]:
    return list(
        (
            await session.execute(
                select(Frame)
                .where(Frame.project_id == project_id)
                .order_by(Frame.number)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


async def _finish_success(
    session: AsyncSession, project: Project, frames: list[Frame]
) -> None:
    filled = _frames_with_image_prompt(frames)
    still_missing = _frames_needing_image_prompt(frames)
    if still_missing:
        missing_nums = [fr.number for fr in still_missing]
        raise RuntimeError(
            f"GPT не заполнил image_prompt для кадров: {missing_nums}"
        )
    if not filled:
        raise RuntimeError("GPT не заполнил ни одного image_prompt")

    from app.services.agent_harness import harness_gate_or_raise

    await harness_gate_or_raise(session, project, step="img_pr")

    for fr in filled:
        fr.status = FrameStatus.image_prompt_ready
    project.status = ProjectStatus.image_prompts_ready
    await session.flush()
    await session.commit()

    from app.services import img_pr_batches as ipb

    try:
        ipb.clear_checkpoint(project.data_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("[#{}] clear img_pr checkpoint: {}", project.id, e)

    logger.info(
        "[#{}] generate_image_prompts complete: {} промтов (DB)",
        project.id,
        len(filled),
    )


async def run(session: AsyncSession, project: Project, bot: Bot) -> None:
    if project.status is not ProjectStatus.generating_image_prompts:
        return
    logger.info("[#{}] generate_image_prompts (db-first) starting", project.id)

    from app.services import db_v2

    await db_v2.backfill_project_v2(session, project)

    frames = await _reload_frames(session, project.id)
    if not frames:
        raise RuntimeError("нет кадров — нечего составлять промты")

    need = _frames_needing_image_prompt(frames)
    if len(need) > 30:
        logger.warning(
            "[#{}] generate_image_prompts: parent frames count {} > 30, clamping to 30",
            project.id,
            len(need),
        )
        need = need[:30]
    if not need:
        # Уже всё в DB (прошлый apply успел, а шаг упал на snapshot/greenlet).
        if _frames_with_image_prompt(frames):
            logger.info(
                "[#{}] generate_image_prompts: prompts already in DB — finish",
                project.id,
            )
            await _finish_success(session, project, frames)
            return
        raise RuntimeError("нет кадров с закадром — нечего составлять промты")

    uuid_map = "\n".join(
        f"кадр {fr.number} = {fr.uuid}" for fr in need if fr.uuid
    )
    if not uuid_map.strip():
        raise RuntimeError("у кадров нет uuid — backfill не сработал")

    sheet = _sheet_for_project(project)
    try:
        sheet.ensure_initialized(project_id=project.id, slug=project.slug)
    except Exception as e:  # noqa: BLE001
        logger.warning("[#{}] ensure_initialized: {}", project.id, e)

    cancelled = False
    last_err: Exception | None = None
    # Отпустить write-txn на время GPT — иначе SQLite lock / UI 500.
    await session.commit()
    for attempt in range(1, 3):
        try:
            raise_if_cancelled(project.id)
        except StepCancelledError as e:
            logger.info(
                "[#{}] generate_image_prompts: {} — выхожу", project.id, e
            )
            cancelled = True
            break
        try:
            from app.db import SessionLocal
            from app.services import db_apply, xlsx_step_runners as xsr

            result = await xsr.run_img_pr_xlsx(
                project,
                n_frames=len(need),
                project_id=project.id,
                uuid_map_text=uuid_map,
            )
            ops = list(result.apply_ops or [])
            if ops:
                from app.services.node_write_contract import filter_ops_for_node

                ops = filter_ops_for_node(ops, node_kind="img_pr")
            if not ops and not result.ops_applied_inline:
                raise RuntimeError("пустой apply-ops после GPT")
            # Один apply в конце (не по батчам) — отдельная короткая сессия.
            if ops and not result.ops_applied_inline:
                import asyncio

                last_apply_err: Exception | None = None
                for apply_try in range(1, 6):
                    try:
                        from app.project_db import (
                            project_db_session_scope,
                            sync_project_row_to_master_db,
                        )

                        async with project_db_session_scope(project.id) as apply_session:
                            proj = await apply_session.get(Project, project.id)
                            if proj is None:
                                raise RuntimeError(
                                    "project gone during img_pr apply"
                                )
                            await db_apply.apply_ops(
                                apply_session,
                                proj,
                                ops,
                                export_xlsx=False,
                                node_kind="img_pr",
                            )
                            await apply_session.commit()
                            await sync_project_row_to_master_db(proj)
                        last_apply_err = None
                        break
                    except Exception as apply_err:  # noqa: BLE001
                        last_apply_err = apply_err
                        msg = str(apply_err).lower()
                        locked = (
                            "database is locked" in msg
                            or "database locked" in msg
                        )
                        if not locked or apply_try >= 5:
                            raise
                        wait_s = min(2 * apply_try, 10)
                        logger.warning(
                            "[#{}] img_pr final apply locked try {}/5 — sleep {}s",
                            project.id,
                            apply_try,
                            wait_s,
                        )
                        await asyncio.sleep(wait_s)
                if last_apply_err is not None:
                    raise last_apply_err
                logger.info(
                    "[#{}] generate_image_prompts: single apply ops={}",
                    project.id,
                    len(ops),
                )
            else:
                logger.info(
                    "[#{}] generate_image_prompts: ops уже в DB (inline), "
                    "пропуск повторного apply (ops={})",
                    project.id,
                    len(ops),
                )

            # Apply в другой сессии — подтянуть project/frames без expire_all
            # (expire_all + lazy project.data_dir → MissingGreenlet).
            await session.refresh(project)
            try:
                from app.services.node_xlsx_snapshot import snapshot_and_bind_node_xlsx

                await snapshot_and_bind_node_xlsx(
                    session, project, node_type="image_prompts"
                )
            except Exception as snap_err:  # noqa: BLE001
                logger.warning(
                    "[#{}] image_prompts snapshot failed: {}",
                    project.id,
                    snap_err,
                )

            frames = await _reload_frames(session, project.id)
            need = _frames_needing_image_prompt(frames)
            if not need:
                break
            missing = [fr.number for fr in need]
            raise RuntimeError(
                f"после apply-ops промты не заполнены (попытка {attempt}): "
                f"кадры {missing[:20]}{'…' if len(missing) > 20 else ''} "
                f"(осталось {len(missing)}; чекпоинт img_pr сохранит прогресс)"
            )
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(
                "[#{}] generate_image_prompts attempt {} failed: {}",
                project.id,
                attempt,
                e,
            )
            if attempt >= 2:
                raise RuntimeError(
                    f"generate_image_prompts: не удалось получить промты: {e}"
                ) from e

    if cancelled:
        try:
            await session.refresh(project)
        except Exception:  # noqa: BLE001
            pass
        return

    frames = await _reload_frames(session, project.id)
    await _finish_success(session, project, frames)
    _ = last_err
