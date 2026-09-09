import socket
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models import Frame, FrameStatus, Project, ProjectStatus
from app.orchestrator.steps.generate_image_prompts import (
    _frames_needing_image_prompt,
    _frames_with_image_prompt,
    _finish_success,
    is_parent_grid_frame,
)
from app.orchestrator.steps.generate_images import is_transient_network_error
from app.bots.outsee import OutseeContentRejectedError, OutseeImageError


def test_is_parent_grid_frame():
    # Parent frame
    p1 = Frame(id=1, number=1, voiceover_text="Hello", attrs={})
    assert is_parent_grid_frame(p1) is True

    # Explicit vo_parent
    p2 = Frame(id=2, number=2, voiceover_text="World", attrs={"camera_subdivide": {"role": "vo_parent"}})
    assert is_parent_grid_frame(p2) is True

    # Child coverage shot
    c1 = Frame(id=3, number=3, voiceover_text="Shot 1", attrs={"camera_subdivide": {"role": "shot"}})
    assert is_parent_grid_frame(c1) is False

    # Child shot via is_shot
    c2 = Frame(id=4, number=4, voiceover_text="Shot 2", attrs={"is_shot": True})
    assert is_parent_grid_frame(c2) is False

    # Child shot via shot_child
    c3 = Frame(id=5, number=5, voiceover_text="Shot 3", attrs={"shot_child": True})
    assert is_parent_grid_frame(c3) is False

    # Child shot via role="shot"
    c4 = Frame(id=6, number=6, voiceover_text="Shot 4", attrs={"role": "shot"})
    assert is_parent_grid_frame(c4) is False


def test_frames_needing_image_prompt_filters_child_shots():
    frames = []
    # 5 parent frames without prompt
    for i in range(1, 6):
        frames.append(Frame(id=i, number=i, voiceover_text=f"VO {i}", image_prompt=None, attrs={}))

    # 10 child shots without prompt
    for i in range(6, 16):
        frames.append(Frame(
            id=i,
            number=i,
            voiceover_text=f"Child VO {i}",
            image_prompt=None,
            attrs={"camera_subdivide": {"role": "shot"}},
        ))

    # 2 parent frames that already have prompt
    for i in range(16, 18):
        frames.append(Frame(
            id=i,
            number=i,
            voiceover_text=f"VO {i}",
            image_prompt="A beautiful sunset",
            attrs={},
        ))

    needing = _frames_needing_image_prompt(frames)
    assert len(needing) == 5
    assert [f.number for f in needing] == [1, 2, 3, 4, 5]

    with_prompt = _frames_with_image_prompt(frames)
    assert len(with_prompt) == 2
    assert [f.number for f in with_prompt] == [16, 17]


@pytest.mark.asyncio
async def test_finish_success_ignores_child_shots():
    session = AsyncMock()
    project = Project(id=42, slug="test-p4", status=ProjectStatus.generating_image_prompts)

    # All parent frames have image_prompt filled
    parents = [
        Frame(id=1, number=1, voiceover_text="Part 1", image_prompt="Prompt 1", attrs={}),
        Frame(id=2, number=2, voiceover_text="Part 2", image_prompt="Prompt 2", attrs={}),
    ]
    # Child frames do NOT have image_prompt
    children = [
        Frame(id=3, number=3, voiceover_text="Child 1", image_prompt="", attrs={"camera_subdivide": {"role": "shot"}}),
        Frame(id=4, number=4, voiceover_text="Child 2", image_prompt=None, attrs={"is_shot": True}),
    ]
    all_frames = parents + children

    with patch("app.services.agent_harness.harness_gate_or_raise", new_callable=AsyncMock):
        await _finish_success(session, project, all_frames)

    assert project.status == ProjectStatus.image_prompts_ready
    for p in parents:
        assert p.status == FrameStatus.image_prompt_ready


def test_is_transient_network_error():
    # Direct DNS error
    dns_err = socket.gaierror("getaddrinfo failed")
    assert is_transient_network_error(dns_err) is True

    # OutseeImageError wrapping DNS error
    wrapped_dns = OutseeImageError("Failed to fetch image: getaddrinfo failed")
    assert is_transient_network_error(wrapped_dns) is True

    # Nested exception
    inner = ConnectionResetError("connection reset by peer")
    outer = Exception("Outsee generate failed")
    outer.__cause__ = inner
    assert is_transient_network_error(outer) is True

    # Gateway timeout
    gw_timeout = RuntimeError("504 Gateway Time-out from storage.yandexcloud.net")
    assert is_transient_network_error(gw_timeout) is True

    # Non-transient errors
    mod_rejected = OutseeContentRejectedError("Content rejected by moderation policy")
    assert is_transient_network_error(mod_rejected) is False

    val_err = ValueError("Invalid prompt format")
    assert is_transient_network_error(val_err) is False


def test_ref_hosting_fallback_hosts():
    # Check hosts list logic when yandex storage is configured
    with patch("app.bots.yandex_storage.yandex_storage_configured", return_value=True):
        from app.bots import outsee_http
        assert hasattr(outsee_http, "_host_via_uguu")
        assert hasattr(outsee_http, "_host_via_litterbox")
        assert hasattr(outsee_http, "_host_via_catbox")
        assert hasattr(outsee_http, "_host_via_0x0")
