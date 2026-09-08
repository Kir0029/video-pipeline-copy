"""Генерация звуков сопровождения (SFX) по плану ``sfx_plan``.

Провайдеры (по порядку):
1. ElevenLabs SFX API (``ELEVENLABS_API_KEY``) — ИИ-генерация по EN-промту.
2. Локальный синтез (чистый Python, wave) — офлайн-фолбэк: whoosh/hit/
   riser/ambience/transition/stinger/foley.

Приём результата: ``verify_audio_file`` (ffprobe + RMS) — брак не уходит
в монтаж. Файлы: ``data/videos/<slug>/sfx/sfx_NN_<kind>.wav|mp3``.
Чекпоинты per-event в ``meta.ai_jobs["sfx_files"]`` — повтор без переделки.
"""

from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project
from app.services.ai_result_io import (
    ai_job_checkpoint,
    save_ai_job_checkpoint,
    verify_audio_file,
)
from app.services.sfx_plan import SfxEvent

_SAMPLE_RATE = 44100


# ── Локальный синтез (офлайн, детерминированный) ──────────────────────────


def _write_wav(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SAMPLE_RATE)
        frames = b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples
        )
        w.writeframes(frames)


def _one_pole_lowpass(samples: list[float], alpha: float) -> list[float]:
    out: list[float] = []
    prev = 0.0
    for x in samples:
        prev = prev + alpha * (x - prev)
        out.append(prev)
    return out


def _noise(n: int, rng: random.Random) -> list[float]:
    return [rng.uniform(-1.0, 1.0) for _ in range(n)]


def _synth(kind: str, duration: float, *, seed: int = 42) -> list[float]:
    """Моно-сэмплы [-1..1] для вида звука (44.1kHz)."""
    rng = random.Random(seed)
    n = max(int(_SAMPLE_RATE * duration), 100)
    t = [i / _SAMPLE_RATE for i in range(n)]

    if kind == "whoosh":
        # Шум с набегающей/спадающей амплитудой + подъём фильтра.
        env = [math.sin(math.pi * i / n) ** 1.5 for i in range(n)]
        raw = _noise(n, rng)
        swept: list[float] = []
        prev = 0.0
        for i, x in enumerate(raw):
            alpha = 0.02 + 0.25 * (i / n)  # фильтр открывается к середине
            prev = prev + alpha * (x - prev)
            swept.append(prev * env[i])
        return swept

    if kind == "hit":
        # Низкий удар 55 Гц с эксп. затуханием + шумовой щелчок (≤ 1.0).
        out: list[float] = []
        for ti in t:
            decay = math.exp(-6.0 * ti / max(duration, 0.1))
            thump = math.sin(2 * math.pi * 55 * ti) * decay
            click = rng.uniform(-1, 1) * math.exp(-40 * ti) * 0.5
            out.append(max(-1.0, min(1.0, (thump * 0.8 + click) * 0.8)))
        return out

    if kind == "riser":
        # Подъём 200→1200 Гц + шум, амплитуда растёт.
        out = []
        phase = 0.0
        for i in range(n):
            f = 200 + 1000 * (i / n)
            phase += 2 * math.pi * f / _SAMPLE_RATE
            tone = math.sin(phase)
            noise = rng.uniform(-0.3, 0.3)
            amp = (i / n) ** 1.5
            out.append((tone * 0.5 + noise) * amp * 0.8)
        return out

    if kind == "ambience":
        # Коричневый шум, мягкий, с медленными fade in/out.
        raw = _noise(n, rng)
        brown: list[float] = []
        acc = 0.0
        for x in raw:
            acc = (acc + 0.02 * x) / 1.02
            brown.append(acc * 3.5)
        brown = _one_pole_lowpass(brown, 0.08)
        fade = min(n // 8, _SAMPLE_RATE)
        for i in range(n):
            g = 1.0
            if i < fade:
                g = i / fade
            elif i > n - fade:
                g = (n - i) / fade
            brown[i] *= g * 0.5
        return brown

    if kind == "transition":
        # Спад 900→150 Гц, быстрый.
        out = []
        phase = 0.0
        for i in range(n):
            f = 900 - 750 * (i / n)
            phase += 2 * math.pi * f / _SAMPLE_RATE
            env = math.sin(math.pi * i / n) ** 1.2
            out.append(math.sin(phase) * env * 0.6)
        return out

    if kind == "stinger":
        # Аккорд A4+E5+немного шума, быстрое затухание.
        out = []
        for ti in t:
            decay = math.exp(-3.5 * ti / max(duration, 0.1))
            chord = (
                math.sin(2 * math.pi * 440 * ti)
                + math.sin(2 * math.pi * 659.25 * ti) * 0.7
                + math.sin(2 * math.pi * 220 * ti) * 0.5
            )
            out.append(chord / 2.2 * decay * 0.85)
        return out

    # foley — серия коротких шумовых постукиваний.
    out = [0.0] * n
    taps = max(2, int(duration * 3))
    for k in range(taps):
        center = int(n * (k + 0.5) / taps)
        length = min(int(_SAMPLE_RATE * 0.05), n - center)
        for j in range(length):
            out[center + j] += rng.uniform(-1, 1) * math.exp(-30 * j / _SAMPLE_RATE) * 0.7
    return _one_pole_lowpass(out, 0.3)


# ── ElevenLabs SFX API (опционально) ──────────────────────────────────────


async def _elevenlabs_sfx(prompt: str, duration: float, out_path: Path) -> Path:
    import time
    import httpx

    from app.services.api_tracker_hook import record_api_call
    from app.settings import settings

    key = settings.elevenlabs_api_key
    if not key:
        raise RuntimeError("no elevenlabs key")

    from app.services.api_tracker_hook import check_api_allowed
    check_api_allowed(provider="elevenlabs", model="sound-generation")

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.elevenlabs.io/v1/sound-generation",
                headers={"xi-api-key": key},
                json={
                    "text": prompt[:450],
                    "duration_seconds": min(max(duration, 0.5), 22.0),
                    "prompt_influence": 0.5,
                },
            )
            dur = time.time() - t0
            if resp.status_code != 200:
                record_api_call(
                    provider="ElevenLabs",
                    model="eleven-sound-generation",
                    call_type="audio",
                    duration_sec=dur,
                    status_code=resp.status_code,
                    error_message=resp.text[:200],
                    project_source="sfx_gen",
                )
                raise RuntimeError(f"11labs sfx {resp.status_code}: {resp.text[:200]}")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(resp.content)
            record_api_call(
                provider="ElevenLabs",
                model="eleven-sound-generation",
                call_type="audio",
                duration_sec=dur,
                cost_usd=0.01,
                media_count=1,
                status_code=200,
                project_source="sfx_gen",
                metadata={"prompt": prompt[:100], "duration": duration},
            )
    except Exception as e:
        if not isinstance(e, RuntimeError):
            record_api_call(
                provider="ElevenLabs",
                model="eleven-sound-generation",
                call_type="audio",
                duration_sec=time.time() - t0,
                status_code=500,
                error_message=str(e)[:200],
                project_source="sfx_gen",
            )
        raise
    return out_path


# ── Генерация плана событий ───────────────────────────────────────────────


async def generate_sfx_files(
    session: AsyncSession,
    project: Project,
    events: list[SfxEvent],
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Сгенерировать файлы для событий плана. Возвращает sfx_files-записи."""
    from app.settings import settings

    if not events:
        raise RuntimeError("sfx_gen: пустой план — сначала планировщик (sfx_plan)")

    sfx_dir = project.data_dir / "sfx"
    sfx_dir.mkdir(parents=True, exist_ok=True)

    existing = ai_job_checkpoint(project, "sfx_files")
    done_by_idx: dict[int, dict[str, Any]] = {}
    if isinstance(existing, dict) and isinstance(existing.get("files"), list):
        for rec in existing["files"]:
            if isinstance(rec, dict) and isinstance(rec.get("idx"), int):
                done_by_idx[rec["idx"]] = rec

    use_api = bool(settings.elevenlabs_api_key)
    files: list[dict[str, Any]] = []
    problems: list[str] = []

    for idx, ev in enumerate(events):
        if not force and idx in done_by_idx:
            rec = done_by_idx[idx]
            if Path(str(rec.get("path") or "")).is_file():
                files.append(rec)
                continue
        ext = "mp3" if use_api else "wav"
        out_path = sfx_dir / f"sfx_{idx:02d}_{ev.kind}.{ext}"
        actual_provider = "elevenlabs" if use_api else "local_synth"
        try:
            if use_api:
                await _elevenlabs_sfx(ev.prompt, ev.duration, out_path)
            else:
                samples = _synth(ev.kind, ev.duration, seed=1000 + idx)
                _write_wav(out_path, samples)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[#{}] sfx_gen: событие {} ({}) упало: {} — локальный синтез",
                project.id,
                idx,
                ev.kind,
                e,
            )
            out_path = sfx_dir / f"sfx_{idx:02d}_{ev.kind}.wav"
            samples = _synth(ev.kind, ev.duration, seed=1000 + idx)
            _write_wav(out_path, samples)
            actual_provider = "local_synth"

        errs = verify_audio_file(out_path, expect_duration=ev.duration, duration_tol=0.6)
        if errs:
            problems.extend(errs)
            logger.warning("[#{}] sfx_gen verify: {}", project.id, errs)
            continue
        files.append(
            {
                "idx": idx,
                "path": str(out_path.resolve()),
                "frame_number": ev.frame_number,
                "t_start": ev.t_start,
                "duration": ev.duration,
                "kind": ev.kind,
                "gain": ev.gain,
                "duck": ev.duck,
                "provider": actual_provider,
            }
        )

    if not files:
        raise RuntimeError(f"sfx_gen: ни один файл не прошёл верификацию: {problems}")

    save_ai_job_checkpoint(project, "sfx_files", {"files": files})
    await session.flush()
    logger.info(
        "[#{}] sfx_gen: {} файлов ({}), проблем {}",
        project.id,
        len(files),
        "elevenlabs" if use_api else "local_synth",
        len(problems),
    )
    return files


def load_sfx_files(project: Project) -> list[dict[str, Any]]:
    """Сгенерированные sfx-файлы из чекпоинта (только существующие на диске)."""
    cached = ai_job_checkpoint(project, "sfx_files")
    if not isinstance(cached, dict) or not isinstance(cached.get("files"), list):
        return []
    out: list[dict[str, Any]] = []
    for rec in cached["files"]:
        if isinstance(rec, dict) and Path(str(rec.get("path") or "")).is_file():
            out.append(rec)
    return out
