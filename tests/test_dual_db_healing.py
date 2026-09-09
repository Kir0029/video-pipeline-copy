"""Tests for Phase 1: Dual-DB healing, default workflow self-healing, and DB routing."""
from __future__ import annotations

from pathlib import Path
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db
from app.models import Base, Frame, FrameStatus, Project, ProjectStatus, Workflow
from app.project_db import (
    init_project_db,
    resolve_project_db_path_by_id,
    sync_project_row_to_master_db,
)
from app.services.agent_harness import _db_path as harness_db_path, _load_frame_prompt_rows
from app.services.hero_quality import _db_path as hero_db_path
from app.services.run_sync import _get_default_workflow_id
from app.settings import settings


@pytest.fixture
async def master_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "state.db"
    monkeypatch.setattr(settings, "sqlite_path", db_file)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(app.db, "SessionLocal", factory)
    monkeypatch.setattr(app.db, "engine", engine)
    async with factory() as session:
        yield session, tmp_path, db_file
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_default_workflow_id_existing(master_db):
    session, _, _ = master_db
    wf = Workflow(name="Custom Workflow", is_default=True, nodes=[], edges=[])
    session.add(wf)
    await session.commit()
    await session.refresh(wf)

    wf_id = await _get_default_workflow_id(session)
    assert wf_id == wf.id


@pytest.mark.asyncio
async def test_get_default_workflow_id_fallback_when_none_marked_default(master_db):
    session, _, _ = master_db
    wf = Workflow(name="Unmarked Workflow", is_default=False, nodes=[], edges=[])
    session.add(wf)
    await session.commit()
    await session.refresh(wf)

    wf_id = await _get_default_workflow_id(session)
    assert wf_id == wf.id
    # Should have healed by marking is_default=True
    await session.refresh(wf)
    assert wf.is_default is True


@pytest.mark.asyncio
async def test_get_default_workflow_id_auto_seeds_when_empty(master_db):
    session, _, _ = master_db
    # Empty table
    wf_id = await _get_default_workflow_id(session)
    assert wf_id is not None
    assert wf_id > 0

    res = await session.execute(select(Workflow).where(Workflow.id == wf_id))
    wf = res.scalar_one_or_none()
    assert wf is not None
    assert wf.is_default is True


def test_harness_and_hero_db_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    proj_dir = data_dir / "projects" / "proj_1"
    proj_dir.mkdir(parents=True, exist_ok=True)
    proj_db = proj_dir / "project.db"
    proj_db.touch()

    monkeypatch.setattr(settings, "data_dir", data_dir)
    default_db = tmp_path / "state.db"
    monkeypatch.setattr(settings, "sqlite_path", default_db)

    # With data_dir pointing to proj_dir
    assert harness_db_path(data_dir=proj_dir) == proj_db
    assert hero_db_path(data_dir=proj_dir) == proj_db

    # With project_id registered in cache
    from app.project_db import _PROJECT_DIR_CACHE
    _PROJECT_DIR_CACHE[42] = proj_dir
    assert harness_db_path(project_id=42) == proj_db
    assert hero_db_path(project_id=42) == proj_db

    # Fallback to default
    assert harness_db_path() == default_db
    assert hero_db_path() == default_db


@pytest.mark.asyncio
async def test_load_frame_prompt_rows_reads_project_db(tmp_path: Path):
    proj_dir = tmp_path / "test_proj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    proj_db_file = proj_dir / "project.db"

    engine = create_async_engine(f"sqlite+aiosqlite:///{proj_db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        proj = Project(slug="p1", topic="Topic 1", title="Proj 1")
        session.add(proj)
        await session.flush()
        pid = proj.id

        f = Frame(
            project_id=pid,
            number=1,
            voiceover_text="Hello world voiceover",
            image_prompt="A cinematic portrait of a knight",
            animation_prompt="Slow pan",
        )
        session.add(f)
        await session.commit()
    await engine.dispose()

    # Load using _load_frame_prompt_rows with data_dir (synchronous)
    rows, err = _load_frame_prompt_rows(pid, data_dir=proj_dir)
    assert not err, f"Error: {err}"
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] == "Hello world voiceover"
    assert rows[0][2] == "A cinematic portrait of a knight"
    assert rows[0][3] == "Slow pan"


@pytest.mark.asyncio
async def test_sync_project_row_to_master_db(master_db):
    session, tmp_path, db_file = master_db
    # Create master project row
    p_master = Project(slug="proj-slug", topic="Topic Master", title="Master Name", status=ProjectStatus.planning, meta={"v": 1})
    session.add(p_master)
    await session.commit()
    await session.refresh(p_master)
    pid = p_master.id

    # Create project in isolated project.db
    proj_dir = tmp_path / "projects" / "proj-slug"
    proj_dir.mkdir(parents=True, exist_ok=True)
    proj_db = proj_dir / "project.db"

    p_engine = create_async_engine(f"sqlite+aiosqlite:///{proj_db}")
    async with p_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    p_factory = async_sessionmaker(p_engine, expire_on_commit=False)

    async with p_factory() as p_session:
        p_child = Project(id=pid, slug="proj-slug", topic="Topic Master", title="Updated Name", status=ProjectStatus.assembled, meta={"v": 2, "montage_draft": ["shot1"]})
        p_session.add(p_child)
        await p_session.commit()
        await p_session.refresh(p_child)

        # Call async sync
        await sync_project_row_to_master_db(p_child)
    await p_engine.dispose()

    # Verify master was updated
    await session.refresh(p_master)
    assert p_master.status == ProjectStatus.assembled
    assert p_master.meta.get("v") == 2
    assert p_master.meta.get("montage_draft") == ["shot1"]
