"""Проверки качества рефов персонажей (sheet vs cinematic scene).

Harness/оркестратор раньше смотрели только «png есть» + NodeRun=done —
из-за этого c02-сцена в палате проходила как «OK».
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app.services.agent_harness import HarnessCheck  # noqa: TC001
from app.services.excel_characters import (
    is_polluted_character_field,
    parse_persons_sheet,
)


def _db_path(data_dir: Path | None = None, project_id: int | None = None) -> Path:
    if data_dir is not None:
        try:
            pdb = Path(data_dir) / "project.db"
            if pdb.is_file():
                return pdb
        except Exception:  # noqa: BLE001
            pass

    if project_id is not None:
        try:
            from app.project_db import resolve_project_db_path_by_id

            pdb = resolve_project_db_path_by_id(project_id)
            if pdb is not None and pdb.is_file():
                return pdb
        except Exception:  # noqa: BLE001
            pass

    from app import settings as app_settings

    sp = getattr(app_settings.settings, "sqlite_path", None)
    if sp:
        return Path(sp)
    return Path("data") / "state.db"


def png_corners_look_sheet_bg(path: Path) -> bool | None:
    """True если углы почти белые (turnaround sheet). False — скорее сцена."""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    try:
        im = Image.open(path).convert("RGB")
        w, h = im.size
        if w < 8 or h < 8:
            return None
        pts = [
            im.getpixel((1, 1)),
            im.getpixel((w - 2, 1)),
            im.getpixel((1, h - 2)),
            im.getpixel((w - 2, h - 2)),
            im.getpixel((w // 2, 1)),
            im.getpixel((1, h // 2)),
        ]
        whiteish = 0
        for pix in pts:
            r, g, b = int(pix[0]), int(pix[1]), int(pix[2])
            if r >= 210 and g >= 210 and b >= 210:
                whiteish += 1
        return whiteish >= 4
    except Exception:  # noqa: BLE001
        return None


def _latest_hero_artifacts(
    project_id: int, data_dir: Path | None = None
) -> dict[str, dict[str, Any]]:
    """excel_id → последняя meta artifact hero_reference."""
    db_file = _db_path(data_dir=data_dir, project_id=project_id)
    if not db_file.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        db = sqlite3.connect(str(db_file))
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT id, path, meta
            FROM artifacts
            WHERE project_id=? AND kind='hero_reference'
            ORDER BY id DESC
            """,
            (project_id,),
        ).fetchall()
        db.close()
    except Exception:  # noqa: BLE001
        return {}
    import json

    for row in rows:
        meta_raw = row["meta"]
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
        except Exception:  # noqa: BLE001
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        eid = str(meta.get("excel_id") or "").strip()
        if not eid or eid in out:
            continue
        out[eid] = {
            "artifact_id": row["id"],
            "path": row["path"],
            "hero_prompt": str(meta.get("hero_prompt") or ""),
            "used_refs": meta.get("used_refs"),
            "meta": meta,
        }
    return out


def collect_hero_quality_checks(
    project_id: int, data_dir: Path
) -> list[HarnessCheck]:
    """Жёсткие проверки: sheet-контракт персонажей."""
    checks: list[HarnessCheck] = []
    xlsx = data_dir / "project.xlsx"
    if not xlsx.is_file():
        return checks
    try:
        persons = parse_persons_sheet(xlsx)
    except Exception as e:  # noqa: BLE001
        # Нет листа «Персонажи» на ранних шагах — не валим весь harness.
        msg = str(e)
        if "Персонажи" in msg or "нет листа" in msg.lower():
            return [
                HarnessCheck(
                    "excel_hero_present",
                    True,
                    "no Персонажи sheet yet",
                )
            ]
        checks.append(HarnessCheck("excel_hero_parse", False, msg[:200]))
        return checks
    if not persons:
        return [
            HarnessCheck("excel_hero_present", True, "no characters sheet rows")
        ]

    polluted: list[str] = []
    bad_sheet: list[str] = []

    for ch in persons:
        if is_polluted_character_field(ch.name) or is_polluted_character_field(ch.look):
            polluted.append(ch.id)
        png = data_dir / "characters" / f"{ch.id}.png"
        # Реф-вариация (rules=c01): пустой hero_prompt — НОРМА (картинка
        # из рефа + changes_text). Нельзя валить harness — блокирует c02.
        if png.is_file() and not ch.ref_ids:
            bg = png_corners_look_sheet_bg(png)
            if bg is False:
                bad_sheet.append(
                    f"{ch.id}: PNG углы не белые — похоже на сцену, не turnaround sheet"
                )

    checks.append(
        HarnessCheck(
            "excel_hero_fields_sane",
            not polluted,
            "ok" if not polluted else f"polluted: {', '.join(polluted)}",
        )
    )
    # Только предупреждение по base-персонажам; рефы не трогаем.
    checks.append(
        HarnessCheck(
            "hero_sheet_png",
            not bad_sheet,
            "ok" if not bad_sheet else "; ".join(bad_sheet)[:400],
        )
    )
    return checks


def hero_slot_prompt_check(project: Any) -> HarnessCheck | None:
    """Выбранный master в слоте hero — sheet или ошибочно агент реестра."""
    try:
        from app.services.hero_prompt_contract import validate_hero_master_or_error
        from app.services.prompt_library import (
            get_project_prompt,
            resolve_project_prompt_name,
        )

        overrides = getattr(project, "prompt_overrides", None) or {}
        meta = getattr(project, "meta", None)
        meta = meta if isinstance(meta, dict) else {}
        name = resolve_project_prompt_name(overrides, "hero", meta=meta) or ""
        master = get_project_prompt(project, "hero")
        err = validate_hero_master_or_error(master, source_name=str(name))
        if err:
            return HarnessCheck("hero_slot_prompt", False, err[:400])
        return HarnessCheck(
            "hero_slot_prompt",
            True,
            f"ok sheet master={name or 'default'}",
        )
    except Exception as e:  # noqa: BLE001
        return HarnessCheck("hero_slot_prompt", False, str(e)[:200])


def hero_quality_diag_lines(
    project_id: int, data_dir: Path, *, project: Any | None = None
) -> list[str]:
    """Строки для оркестратор-диагностики (живой прогон, не stale telemetry)."""
    lines: list[str] = []
    if project is not None:
        slot = hero_slot_prompt_check(project)
        if slot is not None and not slot.ok:
            lines.append("ПЕРСОНАЖИ — ОШИБКА СЛОТА ПРОМТА:")
            lines.append(f"- {slot.detail}")
            lines.append(
                "- действие: в hero поставь turnaround/model sheet; "
                "агент реестра персонажей — только в excel_gpt"
            )
            return lines

    checks = collect_hero_quality_checks(project_id, data_dir)
    bad = [c for c in checks if not c.ok]
    if not bad:
        n_ok = len(checks)
        return [
            "ПЕРСОНАЖИ (живая проверка sheet-контракта):",
            f"- ok ({n_ok} checks): файлы/промты похожи на turnaround sheet",
        ]
    lines = ["ПЕРСОНАЖИ — ОШИБКИ КАЧЕСТВА (не статус NodeRun):"]
    for c in bad:
        lines.append(f"- {c.name}: {c.detail}")
    lines.append(
        "- действие: проверь слот hero (sheet, не реестр) и "
        "перегенерируй проблемные cNN"
    )
    return lines
