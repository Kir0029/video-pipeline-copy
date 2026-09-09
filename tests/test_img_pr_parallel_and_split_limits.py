"""Tests for Phase 2: img_pr parallel batching, 30 parent frames limit, and project_db routing."""
from __future__ import annotations

from pathlib import Path
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db
from app.models import Base, Entity, Frame, FrameStatus, Project, ProjectStatus
from app.project_db import (
    init_project_db,
    project_db_session_scope,
    register_project_data_dir,
)
from app.services.xlsx_step_runners import (
    clamp_parent_frames,
    img_pr_live_streams,
    _load_img_pr_context,
)
from app.settings import settings


def test_img_pr_live_streams_default_four():
    """Default concurrency for text img_pr must be >= 4 to avoid serialized bottleneck."""
    assert img_pr_live_streams(None) >= 4

    p = Project(slug="test-proj", topic="topic")
    p.meta = {}
    assert img_pr_live_streams(p) >= 4

    # Custom override in meta
    p.meta = {"img_pr_streams": 6}
    assert img_pr_live_streams(p) == 6

    # Clamped to max 8
    p.meta = {"img_pr_streams": 20}
    assert img_pr_live_streams(p) == 8


def test_clamp_parent_frames_under_limit():
    """Frames under or equal to 30 must not be modified."""
    frames = [{"закадр": f"Фраза {i}", "длительность": 2.5} for i in range(1, 21)]
    clamped = clamp_parent_frames(frames, max_frames=30)
    assert len(clamped) == 20
    assert clamped == frames

    frames_30 = [{"закадр": f"Фраза {i}", "длительность": 2.0} for i in range(1, 31)]
    clamped_30 = clamp_parent_frames(frames_30, max_frames=30)
    assert len(clamped_30) == 30
    assert clamped_30 == frames_30


def test_clamp_parent_frames_over_limit_preserves_text_and_durations():
    """When GPT returns 50 micro-frames, clamp merges shortest pairs down to 30 without losing words."""
    original_frames = [
        {"закадр": f"Слово{i}", "длительность": 1.0}
        for i in range(1, 51)
    ]
    clamped = clamp_parent_frames(original_frames, max_frames=30)
    assert len(clamped) == 30

    # 100% of the words must be present in strict order
    orig_text = " ".join(f["закадр"] for f in original_frames)
    clamped_text = " ".join(f["закадр"] for f in clamped)
    assert orig_text == clamped_text

    # Total duration must be strictly preserved
    orig_dur = sum(f["длительность"] for f in original_frames)
    clamped_dur = sum(f["длительность"] for f in clamped)
    assert pytest.approx(orig_dur, 0.01) == clamped_dur


def test_clamp_parent_frames_edge_cases():
    assert clamp_parent_frames([]) == []
    assert clamp_parent_frames([{"закадр": "Один"}]) == [{"закадр": "Один"}]


@pytest.mark.asyncio
async def test_img_pr_load_context_reads_from_project_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """_load_img_pr_context must read frames and entities from project.db."""
    proj_dir = tmp_path / "projects" / "test-img-pr"
    proj_dir.mkdir(parents=True, exist_ok=True)
    proj_db_file = proj_dir / "project.db"

    engine = create_async_engine(f"sqlite+aiosqlite:///{proj_db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    pid = 99
    async with factory() as session:
        proj = Project(id=pid, slug="test-img-pr", topic="Topic", general_plan="Epic Plan")
        session.add(proj)
        await session.flush()

        f1 = Frame(
            project_id=pid,
            number=1,
            uuid="u-001",
            voiceover_text="First scene voiceover",
            image_prompt="",  # Needs prompt
        )
        f2 = Frame(
            project_id=pid,
            number=2,
            uuid="u-002",
            voiceover_text="Second scene voiceover",
            image_prompt="Already done",  # Should be skipped
        )
        session.add_all([f1, f2])
        await session.commit()
    await engine.dispose()

    # Register in cache
    from app.project_db import _PROJECT_DIR_CACHE
    _PROJECT_DIR_CACHE[pid] = proj_dir

    p_mock = Project(id=pid, slug="test-img-pr", topic="Topic")
    selected_frames, cards, gp = await _load_img_pr_context(p_mock)

    assert len(selected_frames) == 1
    assert selected_frames[0].uuid == "u-001"
    assert selected_frames[0].number == 1
    assert gp == "Epic Plan"
