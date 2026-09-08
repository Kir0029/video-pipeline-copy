"""Аудио-агенты: метки слов, план SFX, генерация, микс, ИИ I/O слой."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.ai_result_io import (
    ai_job_checkpoint,
    drop_ai_job_checkpoint,
    parse_json_object,
    save_ai_job_checkpoint,
    text_job,
    verify_audio_file,
)
from app.services.audio_marks import build_marks_from_words
from app.services.sfx_gen import _synth, generate_sfx_files
from app.services.sfx_mix import SfxInput, build_mux_audio_args
from app.services.sfx_plan import (
    SfxEvent,
    _parse_events,
    frame_timeline,
    load_sfx_plan,
    validate_events,
)
from app.services.whisper import WordTS


def _frame(number: int, vo: str, dur: float | None = None):
    return SimpleNamespace(
        number=number,
        voiceover_text=vo,
        duration_seconds=dur,
        meaning="",
        attrs={},
    )


# ── audio_marks: сопоставление ASR ↔ текст ────────────────────────────────


def test_word_marks_align_exact() -> None:
    frames = [
        _frame(1, "бог мёртв и мы", 4.0),
        _frame(2, "сами отвечаем за смысл", 4.0),
    ]
    words = [
        WordTS("бог", 0.0, 0.5, 0.9),
        WordTS("мёртв", 0.5, 1.1, 0.9),
        WordTS("и", 1.1, 1.3, 0.9),
        WordTS("мы", 1.3, 1.6, 0.9),
        WordTS("сами", 2.0, 2.5, 0.9),
        WordTS("отвечаем", 2.5, 3.1, 0.9),
        WordTS("за", 3.1, 3.3, 0.9),
        WordTS("смысл", 3.3, 3.8, 0.9),
    ]
    marks, spans, report = build_marks_from_words(frames, words, audio_duration=3.8)

    assert len(marks) == 8
    assert [m.text for m in marks] == [
        "бог", "мёртв", "и", "мы", "сами", "отвечаем", "за", "смысл",
    ]
    # Привязка к кадрам: первые 4 слова → кадр 1, остальные → кадр 2.
    assert [m.frame_number for m in marks[:4]] == [1, 1, 1, 1]
    assert [m.frame_number for m in marks[4:]] == [2, 2, 2, 2]
    # Метки по времени — из ASR.
    assert marks[0].start == 0.0 and marks[0].end == 0.5
    assert marks[-1].start == 3.3 and marks[-1].end == 3.8
    # Spans кадров монотонны и покрывают свои слова.
    assert spans[0].frame_number == 1 and spans[0].start_ts == 0.0
    assert spans[1].frame_number == 2 and spans[1].start_ts >= spans[0].start_ts
    assert report.coverage == 1.0
    assert report.monotonic is True
    assert report.drift_s == 0.0
    assert not report.problems


def test_word_marks_asr_mismatch_interpolated() -> None:
    """ASR проглотил слово → токен сценария интерполируется, отчёт честный."""
    frames = [_frame(1, "раз два три четыре", 4.0)]
    words = [
        WordTS("раз", 0.0, 0.4, 0.9),
        WordTS("два", 0.4, 0.8, 0.9),
        WordTS("четыре", 0.8, 1.4, 0.9),  # «три» не распознано
    ]
    marks, _spans, report = build_marks_from_words(frames, words, audio_duration=1.4)
    assert len(marks) == 4
    assert any(m.interpolated for m in marks)
    assert report.coverage < 1.0
    assert report.monotonic is True


def test_word_marks_empty_input() -> None:
    _m, _s, report = build_marks_from_words([_frame(1, "текст", 3.0)], [])
    assert report.problems  # пустой вход — проблема, не падение


# ── sfx_plan: парсинг и валидация ─────────────────────────────────────────


def _ev(**kw) -> SfxEvent:
    base = {
        "frame_number": 1,
        "t_start": 1.0,
        "duration": 1.5,
        "kind": "whoosh",
        "prompt": "deep whoosh",
        "gain": 0.5,
        "duck": False,
    }
    base.update(kw)
    return SfxEvent(**base)


def test_sfx_plan_validate_ok() -> None:
    events = [_ev(), _ev(frame_number=2, t_start=5.0, kind="hit")]
    assert validate_events(events, total_duration=10.0) == []


def test_sfx_plan_validate_bad() -> None:
    assert validate_events([], total_duration=10.0)  # пустой план
    assert validate_events([_ev(kind="laser")], total_duration=10.0)  # вид не из каталога
    assert validate_events([_ev(t_start=99.0)], total_duration=10.0)  # вне таймлайна
    assert validate_events([_ev(duration=0.1)], total_duration=10.0)  # слишком короткий
    assert validate_events([_ev(gain=2.0)], total_duration=10.0)  # gain вне диапазона
    assert validate_events([_ev(prompt=" ")], total_duration=10.0)  # пустой промт
    # Плотность: ближе 0.8с
    dense = [_ev(t_start=1.0), _ev(t_start=1.2)]
    assert validate_events(dense, total_duration=10.0)
    # >3 событий на кадр
    many = [_ev(t_start=1.0 + i * 1.0) for i in range(4)]
    assert validate_events(many, total_duration=20.0)


def test_sfx_plan_parse_sorts_and_rounds() -> None:
    payload = {
        "events": [
            {"frame_number": 2, "t_start": 5.12345, "duration": 2.0, "kind": "HIT", "prompt": "x"},
            {"frame_number": 1, "t_start": 0.5, "duration": 1.0, "kind": "riser", "prompt": "y"},
        ]
    }
    events = _parse_events(payload)
    assert [e.frame_number for e in events] == [1, 2]  # отсортировано по времени
    assert events[0].kind == "riser"
    assert events[1].t_start == 5.123


def test_frame_timeline_cumulative() -> None:
    frames = [_frame(1, "a", 2.0), _frame(2, "b", 4.0), _frame(3, "c", None)]
    tl = frame_timeline(frames)
    assert tl[0]["start"] == 0.0 and tl[0]["end"] == 2.0
    assert tl[1]["start"] == 2.0 and tl[1]["end"] == 6.0
    assert tl[2]["start"] == 6.0  # None → дефолт 3.0


def test_load_sfx_plan_from_checkpoint() -> None:
    project = SimpleNamespace(meta={}, data_dir=None)
    assert load_sfx_plan(project) is None
    save_ai_job_checkpoint(
        project,
        "sfx_plan",
        {"events": [{"frame_number": 1, "t_start": 0.5, "duration": 1.0, "kind": "hit", "prompt": "p"}]},
    )
    plan = load_sfx_plan(project)
    assert plan is not None and plan[0].kind == "hit"
    drop_ai_job_checkpoint(project, "sfx_plan")
    assert ai_job_checkpoint(project, "sfx_plan") is None


# ── sfx_gen: локальный синтез + верификация ───────────────────────────────


def test_local_synth_all_kinds() -> None:
    from app.services.sfx_plan import SFX_KINDS

    for kind in SFX_KINDS:
        samples = _synth(kind, 1.0, seed=7)
        assert len(samples) == 44100
        assert all(-1.0 <= s <= 1.0 for s in samples)
        assert max(abs(s) for s in samples) > 0.05  # не тишина


@pytest.mark.asyncio
async def test_generate_sfx_files_local(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.settings import settings
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    project = SimpleNamespace(id=1, meta={}, data_dir=tmp_path)
    session = MagicMock()
    session.flush = MagicMock(return_value=None)

    async def _flush():
        return None

    session.flush = _flush
    events = [_ev(t_start=0.5, duration=0.8), _ev(frame_number=2, t_start=3.0, kind="hit", duration=0.6)]
    files = await generate_sfx_files(session, project, events)
    assert len(files) == 2
    for rec in files:
        p = rec["path"]
        assert verify_audio_file(__import__("pathlib").Path(p)) == []
        assert rec["provider"] == "local_synth"
    # Чекпоинт записан — повтор без переделки.
    assert ai_job_checkpoint(project, "sfx_files") is not None


# ── sfx_mix: граф микса ───────────────────────────────────────────────────


def test_mix_voice_only() -> None:
    args, fc = build_mux_audio_args(
        bgm_path=None, bgm_gain=0.0, output_duration=10.0, tail=0.0, sfx=[]
    )
    assert args == [] and fc is None


def test_mix_bgm_only() -> None:
    from pathlib import Path

    args, fc = build_mux_audio_args(
        bgm_path=Path("/tmp/bgm.mp3"), bgm_gain=0.25, output_duration=10.0, tail=0.0, sfx=[]
    )
    assert "-stream_loop" in args
    assert fc is not None and "amix=inputs=2" in fc and "[bgm]" in fc


def test_mix_sfx_positions_and_duck(tmp_path) -> None:
    from app.services.sfx_gen import _write_wav as write_wav

    pa = tmp_path / "a.wav"
    pb = tmp_path / "b.wav"
    write_wav(pa, _synth("whoosh", 0.5))
    write_wav(pb, _synth("ambience", 0.5))
    sfx = [
        SfxInput(path=pa, t_start=1.25, gain=0.5, kind="whoosh"),
        SfxInput(path=pb, t_start=4.0, gain=0.175, kind="ambience"),
    ]
    args, fc = build_mux_audio_args(
        bgm_path=None, bgm_gain=0.0, output_duration=10.0, tail=0.0, sfx=sfx
    )
    assert fc is not None
    assert "adelay=1250|1250" in fc  # позиция по метке
    assert "adelay=4000|4000" in fc
    assert "amix=inputs=3" in fc  # VO + 2 SFX
    # Каждый sfx — отдельный -i
    assert args.count("-i") == 2


# ── ai_result_io: text_job + parse_json_object ────────────────────────────


def test_parse_json_object_variants() -> None:
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('проза ```json\n{"a": 2}\n``` хвост') == {"a": 2}
    with pytest.raises(ValueError):
        parse_json_object("нет json вообще")


@pytest.mark.asyncio
async def test_text_job_retry_then_checkpoint() -> None:
    project = SimpleNamespace(id=1, meta={})
    calls: list[str] = []

    async def fake_gpt(text, *, timeout, project_id=None):
        calls.append(text)
        if len(calls) == 1:
            return '{"events": []}'  # пустой план → валидация не пройдёт
        return '{"events": [{"frame_number": 1, "t_start": 1.0, "duration": 1.0, "kind": "hit", "prompt": "p", "gain": 0.5}]}'

    with patch("app.services.gpt_client.gpt_ask_fresh", side_effect=fake_gpt):
        result = await text_job(
            project,
            name="sfx_plan",
            prompt="PROMPT",
            parse=parse_json_object,
            validate=lambda p: validate_events(_parse_events(p), total_duration=10.0),
        )
    assert result.attempts == 2
    assert result.problems_log  # первая попытка зафиксирована
    assert "ОШИБКИ ПРОШЛОЙ ПОПЫТКИ" in calls[1]  # фидбек ушёл в повтор
    # Чекпоинт сохранён — второй вызов без GPT.
    assert ai_job_checkpoint(project, "sfx_plan") is not None
    with patch("app.services.gpt_client.gpt_ask_fresh", side_effect=AssertionError("no GPT")):
        again = await text_job(
            project,
            name="sfx_plan",
            prompt="PROMPT",
            parse=parse_json_object,
            validate=lambda p: [],
        )
    assert again.attempts == 0
