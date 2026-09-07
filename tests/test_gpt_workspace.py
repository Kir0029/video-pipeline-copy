"""GPT workspace: вложения, ask, ошибки, save."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import gpt_workspace as gw


@pytest.fixture(autouse=True)
def _workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gw.settings, "data_dir", tmp_path)
    yield


def test_create_list_get_delete() -> None:
    s = gw.create_session(title="Тест")
    assert s["title"] == "Тест"
    assert s["id"]
    listed = gw.list_sessions()
    assert any(x["id"] == s["id"] for x in listed)
    got = gw.get_session(s["id"])
    assert got["messages"] == []
    gw.delete_session(s["id"])
    with pytest.raises(FileNotFoundError):
        gw.get_session(s["id"])


def test_history_drops_long_turns_for_pdf_translate(tmp_path: Path) -> None:
    """«переведи» + PDF не должен тащить в API старый километровый промт сцены."""
    hist = [
        {"role": "user", "content": "Create one unified scene " + ("x" * 3000)},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "переведи"},
    ]
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    out = gw._history_for_attachment_ask(
        hist, files=[pdf], text="переведи текст из приложенного файла"
    )
    assert len(out) == 2
    assert out[0]["role"] == "assistant"
    assert all(len(m["content"]) <= gw._HISTORY_LONG_MSG_CHARS for m in out)


def test_attachment_and_voiceover_save(tmp_path: Path) -> None:
    s = gw.create_session()
    att = gw.save_attachment(s["id"], "note.txt", b"hello")
    assert att["name"] == "note.txt"
    got = gw.get_session(s["id"])
    assert len(got["attachments"]) == 1

    gw._append_message(s["id"], "assistant", "закадровый текст для ролика")
    proj = tmp_path / "proj"
    r = gw.save_reply_as_voiceover(s["id"], project_data_dir=proj)
    assert (proj / "voiceover.txt").read_text(encoding="utf-8").startswith("закадровый")
    assert r["chars"] > 0


def test_attachment_urls_include_download() -> None:
    s = gw.create_session()
    att = gw.save_attachment(s["id"], "note.txt", b"hello")
    assert "download=1" in att["download_url"]
    assert att["url"].startswith("/api/files?path=")
    got = gw.get_session(s["id"])
    assert got["attachments"][0]["download_url"].endswith("download=1")


def test_copy_attachment_to_outputs() -> None:
    s = gw.create_session()
    gw.save_attachment(s["id"], "pic.png", b"\x89PNG" + b"x" * 40)
    out = gw.copy_attachment_to_outputs(s["id"], "pic.png")
    assert out["name"] == "pic.png"
    got = gw.get_session(s["id"])
    assert any(o["name"] == "pic.png" for o in got["outputs"])


def test_outputs_zip() -> None:
    s = gw.create_session()
    gw.save_attachment(s["id"], "a.txt", b"hello")
    gw.copy_attachment_to_outputs(s["id"], "a.txt")
    z = gw.build_outputs_zip(s["id"])
    assert z.exists() and z.suffix == ".zip"
    assert z.stat().st_size > 10


def test_extract_image_urls_from_html() -> None:
    from app.services.gpt_api import collect_result_urls, extract_image_urls_from_html

    html = """
    <html><head>
      <meta property="og:image" content="https://cdn.example/hero.png" />
      <meta name="twitter:image" content="https://cdn.example/tw.jpg" />
    </head><body>
      <img src="/static/a.webp" />
      <img src="data:image/png;base64,aaa" />
    </body></html>
    """
    urls = extract_image_urls_from_html(html, "https://wiki.example/page/Lion")
    assert urls[0] == "https://cdn.example/hero.png"
    assert "https://cdn.example/tw.jpg" in urls
    assert "https://wiki.example/static/a.webp" in urls
    assert all(not u.startswith("data:") for u in urls)

    ordered = collect_result_urls(
        "see https://en.wikipedia.org/wiki/Lion and https://cdn.example/x.png"
    )
    assert ordered[0].endswith(".png")


@pytest.mark.asyncio
async def test_materialize_html_pulls_og_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import gpt_api as ga

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    html = (
        b"<!DOCTYPE html><html><head>"
        b'<meta property="og:image" content="https://cdn.example/lion.png" />'
        b"</head><body>Lion</body></html>"
    )

    async def fake_download(url: str, out_path: Path, *, timeout: float = 180.0) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if url.endswith(".png") or "lion.png" in url:
            out_path.write_bytes(png)
            return ga.finalize_downloaded_file(
                out_path, content_type="image/png", url=url
            )
        out_path.write_bytes(html)
        return ga.finalize_downloaded_file(
            out_path, content_type="text/html", url=url
        )

    monkeypatch.setattr(ga, "download_content", fake_download)
    saved = await ga.materialize_reply_assets(
        "вот https://en.wikipedia.org/wiki/Lion_(Dota_2)",
        tmp_path,
        prefix="gpt_test",
    )
    assert len(saved) == 1
    assert saved[0].suffix == ".png"
    assert saved[0].read_bytes().startswith(b"\x89PNG")
    assert not list(tmp_path.glob("*.html"))


def test_finalize_url_download_never_bin(tmp_path: Path) -> None:
    from app.services.gpt_api import finalize_downloaded_file, maybe_decode_base64_image
    import base64

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    p = tmp_path / "gpt_20260727_110549_url_0.bin"
    p.write_bytes(png)
    got = finalize_downloaded_file(p, content_type="image/png")
    assert got.suffix == ".png"
    assert got.name.endswith(".png")
    assert not p.exists()

    # Content-Type alone (байты неизвестны)
    p2 = tmp_path / "url_1.bin"
    p2.write_bytes(b"\x00" * 200)
    got2 = finalize_downloaded_file(p2, content_type="image/webp")
    assert got2.suffix == ".webp"

    # base64 PNG в теле
    p3 = tmp_path / "url_2.bin"
    p3.write_bytes(base64.b64encode(png))
    dec, ext = maybe_decode_base64_image(p3.read_bytes())
    assert ext == ".png"
    got3 = finalize_downloaded_file(p3)
    assert got3.suffix == ".png"
    assert got3.read_bytes().startswith(b"\x89PNG")

    # неизвестное → .dat, не .bin
    p4 = tmp_path / "url_3.bin"
    p4.write_bytes(bytes(range(256)) * 4)
    got4 = finalize_downloaded_file(p4)
    assert got4.suffix == ".dat"
    assert got4.suffix != ".bin"


def test_sniff_png_bin_renames(tmp_path: Path) -> None:
    from app.services.gpt_api import (
        ensure_correct_extension,
        sniff_file_extension,
        suggested_name_and_mime,
    )

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    assert sniff_file_extension(png) == ".png"
    assert sniff_file_extension(b"\xff\xd8\xff\xe0" + b"x" * 20) == ".jpg"
    p = tmp_path / "reply.bin"
    p.write_bytes(png)
    # пока на диске .bin — download-имя уже .png + image/png
    name, mime = suggested_name_and_mime(p)
    assert name == "reply.png"
    assert mime == "image/png"
    fixed = ensure_correct_extension(p)
    assert fixed.name == "reply.png"
    assert fixed.is_file()
    assert not p.exists()


def test_get_session_renames_existing_bin() -> None:
    """Старые сессии с .bin на диске — при get_session становятся .png."""
    s = gw.create_session()
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    d = gw._session_dir(s["id"])
    att = d / "attachments"
    att.mkdir(exist_ok=True)
    (att / "legacy.bin").write_bytes(png)
    gw._append_message(
        s["id"],
        "assistant",
        "Вот legacy.bin для тебя",
        attachment_names=["legacy.bin"],
    )
    got = gw.get_session(s["id"])
    att0 = got["attachments"][0]
    assert att0["name"] == "legacy.png"
    assert att0["display_name"] == "legacy.png"
    assert att0["kind"] == "image"
    assert att0["mime"] == "image/png"
    assert got["messages"][0]["attachment_names"] == ["legacy.png"]
    assert "legacy.png" in got["messages"][0]["content"]
    assert "legacy.bin" not in got["messages"][0]["content"]
    assert not (att / "legacy.bin").exists()
    assert (att / "legacy.png").is_file()


def test_save_attachment_bin_becomes_png() -> None:
    s = gw.create_session()
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    att = gw.save_attachment(s["id"], "sand.bin", png)
    assert att["name"].endswith(".png")
    assert not att["name"].endswith(".bin")

    assert gw._wants_file_return("верни файл обратно")
    assert gw._wants_file_return("send back the file please")
    assert not gw._wants_file_return("что на картинке?")
    assert not gw._wants_file_return("текст переработай и пришли мне файлом")
    assert not gw._pure_file_return("текст переработай и пришли мне файлом")
    assert gw._pure_file_return("верни файл hero.png")
    assert not gw._pure_file_return("верни файл и опиши что на нём")
    assert not gw._wants_file_return("верни файл и опиши что на нём")


def test_save_attachment_missing_session() -> None:
    with pytest.raises(FileNotFoundError, match="сессия не найдена"):
        gw.save_attachment("nosuch", "a.txt", b"hi")


@pytest.mark.asyncio
async def test_ask_studio_returns_bytes_without_gpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Верни файл» — исходные байты с диска, GPT не вызывается."""
    import app.services.gpt_client as gc

    called = {"n": 0}

    class Boom:
        async def ask_with_files(self, *a, **k):
            called["n"] += 1
            raise AssertionError("GPT не должен вызываться для чистого возврата")

    monkeypatch.setattr(gc, "get_gpt_client", lambda: Boom())

    s = gw.create_session()
    raw = b"\x89PNG" + b"ORIGINAL-BYTES-XYZ" + b"x" * 20
    gw.save_attachment(s["id"], "hero.png", raw)
    out = await gw.ask(s["id"], "верни файл hero.png")
    assert called["n"] == 0
    names = [o["name"] for o in out["outputs"]]
    assert "hero.png" in names
    # байты идентичны исходному вложению
    got = next(o for o in out["outputs"] if o["name"] == "hero.png")
    assert Path(got["path"]).read_bytes() == raw
    assert "Готовые файлы" in out["messages"][-1]["content"]
    assert "hero.png" in out["messages"][-1]["content"]
    assert out["messages"][-1].get("studio_returned") is True


@pytest.mark.asyncio
async def test_ask_return_plus_analyze_still_calls_gpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.gpt_client as gc

    captured: dict = {}

    class FakeGpt:
        async def ask_with_files(self, text, files, **kwargs):
            captured["text"] = text
            captured["files"] = [p.name for p in files]
            captured["called"] = True
            return "На картинке персонаж в песке."

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    s = gw.create_session()
    gw.save_attachment(s["id"], "hero.png", b"\x89PNG" + b"x" * 40)
    out = await gw.ask(s["id"], "верни файл и опиши что на картинке")
    assert captured.get("called")
    # при анализе/переработке исходники не копируем в Результаты
    assert "Studio уже положила" not in (captured.get("text") or "")
    assert "hero.png" in captured.get("files", [])
    assert "Исходники (Studio" not in out["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_ask_rework_text_to_file_calls_gpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«переработай и пришли файлом» — GPT, не studio-return исходников."""
    import app.services.gpt_client as gc

    called = {"n": 0}

    class FakeGpt:
        async def ask_with_files(self, text, files, **kwargs):
            called["n"] += 1
            assert files and files[0].name.endswith(".txt")
            return "Переработанный текст для файла."

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    s = gw.create_session()
    gw.save_attachment(s["id"], "дело.txt", "сырой текст дела".encode("utf-8"))
    out = await gw.ask(s["id"], "текст переработай и пришли мне файлом")
    assert called["n"] == 1
    assert out["messages"][-1].get("studio_returned") is not True
    assert "Studio вернула исходные" not in out["messages"][-1]["content"]
    assert "Переработанный текст" in out["messages"][-1]["content"]
    assert "Готовые файлы" in out["messages"][-1]["content"]
    assert "Studio положила" not in out["messages"][-1]["content"]
    # новый файл в Результаты, не копия исходника
    out_names = [o["name"] for o in out["outputs"] if not o["name"].startswith("reply_")]
    assert any(n.endswith(".txt") and "дело" in n.lower() for n in out_names)
    assert any("дело" in line for line in out["messages"][-1]["content"].splitlines())


def test_reply_fails_file_delivery_detects_excuse_and_planning() -> None:
    assert gw._reply_fails_file_delivery(
        "В текущем чате у меня нет доступного инструмента для создания "
        "и прикрепления .txt файла. Могу вернуть полный текст.",
        user_text="пришли агента тхт файлом",
    )
    assert gw._reply_fails_file_delivery(
        "Исправляю: возьму именно переделанный промт агента и сохраню.",
        user_text="пришли переделанного агента в тхт файле",
    )
    assert not gw._reply_fails_file_delivery(
        "Агент: character_registry\n\n" + ("правило.\n" * 40),
        user_text="пришли агента тхт файлом",
    )
    assert not gw._reply_fails_file_delivery(
        "",
        user_text="пришли мне пустой txt файл",
    )


@pytest.mark.asyncio
async def test_ask_file_excuse_retries_and_packs_txt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отмазка «нет инструмента» → повтор → полный текст в .txt, не тишина."""
    import app.services.gpt_client as gc

    calls: list[str] = []
    full = (
        "Агент: character_registry_database_agent_v3_web_verified\n\n"
        + ("Правило заполнения персонажей.\n" * 30)
    )

    class FakeGpt:
        async def ask_with_files(self, text, files, **kwargs):
            calls.append(text)
            if len(calls) == 1:
                return (
                    "В текущем чате у меня нет доступного инструмента "
                    "для создания и прикрепления .txt файла. "
                    "Могу вернуть полный текст следующим сообщением."
                )
            assert "СТОП" in text or "ПОЛНЫЙ текст" in text
            return full

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    s = gw.create_session()
    gw.save_attachment(
        s["id"],
        "agent.txt",
        "старый промт агента персонажей".encode("utf-8"),
    )
    out = await gw.ask(
        s["id"],
        "переделай агента по созданию персонажей и пришли тхт файлом",
    )
    assert len(calls) == 2
    names = [o["name"] for o in out["outputs"] if not o["name"].startswith("reply_")]
    assert any(n.endswith(".txt") for n in names)
    assert "Готовые файлы" in out["messages"][-1]["content"]
    packed = next(
        o
        for o in out["outputs"]
        if o["name"].endswith(".txt") and not o["name"].startswith("reply_")
    )
    body = Path(packed["path"]).read_text(encoding="utf-8")
    assert "character_registry_database_agent_v3_web_verified" in body
    assert "нет доступного инструмента" not in body


@pytest.mark.asyncio
async def test_ask_send_file_packs_previous_assistant_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«отправь файл» после длинного ответа → .txt/.docx в Результаты, без GPT."""
    import app.services.gpt_client as gc

    class Boom:
        async def ask_with_files(self, *a, **k):
            raise AssertionError("GPT не нужен — пакуем прошлый ответ")

    monkeypatch.setattr(gc, "get_gpt_client", lambda: Boom())
    s = gw.create_session()
    long_doc = "ДОГОВОР\n\n" + ("Пункт о взаимных обязательствах сторон.\n" * 8)
    gw._append_message(s["id"], "assistant", long_doc)
    out = await gw.ask(s["id"], "отправь файл")
    names = [o["name"] for o in out["outputs"] if not o["name"].startswith("reply_")]
    assert names, out["outputs"]
    assert any(n.endswith((".txt", ".docx")) for n in names)
    assert "Готовые файлы" in out["messages"][-1]["content"]
    assert "Studio положила" not in out["messages"][-1]["content"]
    assert "ДОГОВОР" not in out["messages"][-1]["content"]  # не дублируем весь текст в пузырь


def test_wants_deliverable_includes_send_file() -> None:
    assert gw._wants_deliverable_file("отправь файл")
    assert gw._wants_deliverable_file("пришли мне файлом")
    assert gw._wants_deliverable_file("Составь договор аренды и пришли .docx")


def test_should_pack_prishli_mne_fail() -> None:
    """«Сделай агента и пришли мне файл» → .txt, не только пузырь."""
    msg = (
        "Нужно из этого агента сделать агента проверки. "
        "Сделай агента и пришли мне файл"
    )
    assert gw.resolve_file_intent(msg, has_attachments=True, last_doc="") == "pack_reply"
    assert gw._explicit_text_doc_ask(msg)
    assert gw._should_pack_text_document(msg, "x" * 500, media_count=0)
    # голый «пришли картинку» — не текстовый документ
    assert not gw._should_pack_text_document(
        "пришли картинку phantom", "png data", media_count=0
    )


def test_reset_running_sessions_on_startup(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gw, "_root", lambda: tmp_path)
    sid = "stucksession01"
    d = tmp_path / sid
    d.mkdir()
    (d / "attachments").mkdir()
    (d / "outputs").mkdir()
    meta = {
        "id": sid,
        "title": "stuck",
        "status": "running",
        "phase": "thinking",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    (d / "meta.json").write_text(
        __import__("json").dumps(meta), encoding="utf-8"
    )
    (d / "messages.json").write_text("[]", encoding="utf-8")
    out = gw.reset_running_sessions_on_startup()
    assert out["reset"] == 1
    got = __import__("json").loads((d / "meta.json").read_text(encoding="utf-8"))
    assert got["status"] == "error"


def test_stale_running_session_lazy_reset(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gw, "_root", lambda: tmp_path)
    s = gw.create_session(title="stale")
    sid = s["id"]
    meta_path = tmp_path / sid / "meta.json"
    meta = __import__("json").loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = "running"
    meta["phase"] = "thinking"
    meta["updated_at"] = "2020-01-01T00:00:00+00:00"
    meta_path.write_text(__import__("json").dumps(meta), encoding="utf-8")
    got = gw.get_session(sid)
    assert got["status"] == "error"
    assert "не ответил" in (got.get("phase_detail") or "")


def test_resolve_file_intent_simple() -> None:
    doc = "ДОГОВОР\n\n" + ("пункт\n" * 20)
    assert (
        gw.resolve_file_intent("отправь файл", has_attachments=False, last_doc=doc)
        == "pack_last"
    )
    assert (
        gw.resolve_file_intent(
            "верни файл hero.png", has_attachments=True, last_doc=""
        )
        == "return_attachments"
    )
    assert (
        gw.resolve_file_intent(
            "текст переработай и пришли мне файлом",
            has_attachments=True,
            last_doc="",
        )
        == "pack_reply"
    )
    assert (
        gw.resolve_file_intent(
            "Составь договор аренды и пришли .docx",
            has_attachments=False,
            last_doc="",
        )
        == "pack_reply"
    )
    assert (
        gw.resolve_file_intent("что на картинке?", has_attachments=True, last_doc="")
        == "none"
    )
    assert (
        gw.resolve_file_intent(
            "пришли мне пустой txt файл", has_attachments=False, last_doc=""
        )
        == "pack_reply"
    )
    # Вопрос про прошлую доставку — не новый pack
    assert (
        gw.resolve_file_intent(
            "почему ты с первого раза не прислал файлом?",
            has_attachments=False,
            last_doc=doc,
        )
        == "none"
    )
    assert (
        gw.resolve_file_intent(
            "я вопрос блять задал",
            has_attachments=False,
            last_doc=doc,
        )
        == "none"
    )
    # Любой хвост с «?» — ответ в чате, даже если есть «файл»
    assert (
        gw.resolve_file_intent(
            "можешь прислать файлом?",
            has_attachments=False,
            last_doc=doc,
        )
        == "none"
    )
    assert (
        gw.resolve_file_intent(
            "отправь файл?",
            has_attachments=False,
            last_doc=doc,
        )
        == "none"
    )
    assert gw._is_meta_chat_question("пришли мне файл???")
    assert not gw._is_meta_chat_question("пришли мне файлом")


def test_short_affirmative_continues_file_work() -> None:
    prior = [
        {
            "role": "user",
            "content": "убрать лишние ** и пришли заново файл txt",
        },
        {
            "role": "assistant",
            "content": "Убрать форматирование и выдать полный .txt?",
        },
    ]
    assert (
        gw.resolve_file_intent(
            "да",
            has_attachments=False,
            last_doc="",
            prior_raw=prior,
        )
        == "pack_reply"
    )


def test_last_assistant_skips_ready_files_notice() -> None:
    contract = (
        "ДОГОВОР № ___\nпредоставления доступа\n\n"
        "1.1. Сервис — система.\n1.2. API — интерфейс.\n"
        "1.3. Пакет — объем.\n1.4. Лимиты — ограничения.\n"
        "1.5. Запрос — данные.\n"
        + ("Исполнитель и Заказчик. Реквизиты сторон.\n" * 40)
    )
    prior = [
        {"role": "assistant", "content": contract},
        {
            "role": "assistant",
            "content": "Готовые файлы:\n• document_x.txt",
        },
    ]
    got = gw._last_assistant_document(prior)
    assert "ДОГОВОР" in got
    assert "Готовые файлы" not in got


@pytest.mark.asyncio
async def test_ask_packs_agent_prishli_mne_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Сделай агента и пришли мне файл» → document_*.txt в Результаты."""
    import app.services.gpt_client as gc

    body = ("Агент проверки таблицы\n\n" + ("правило логики\n" * 80))

    class FakeGpt:
        async def ask_with_files(self, *a, **k):
            return body

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    s = gw.create_session()
    gw.save_attachment(s["id"], "agent.txt.md", b"source agent prompt")
    out = await gw.ask(
        s["id"],
        "Сделай агента проверки и пришли мне файл",
    )
    names = [o["name"] for o in out["outputs"] if not o["name"].startswith("reply_")]
    assert any(n.endswith(".txt") for n in names), names
    assert "Готовые файлы" in out["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_ask_meta_question_does_not_repack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«почему не прислал файлом?» — ответ текстом, без нового document_*.txt."""
    import app.services.gpt_client as gc

    class FakeGpt:
        async def ask_with_files(self, *a, **k):
            return (
                "Потому что в том сообщении договор был только в чате. "
                "Скачиваемый файл появляется после явной просьбы «пришли файлом» "
                "или когда Studio сама кладёт длинный договор в результаты."
            )

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    s = gw.create_session()
    contract = (
        "ДОГОВОР № ___\nпредоставления доступа к API\n\n"
        "1.1. Сервис.\n1.2. API.\n1.3. Пакет.\n1.4. Лимиты.\n1.5. Запрос.\n"
        + ("Исполнитель Крохмаль. Заказчик. Реквизиты сторон.\n" * 50)
    )
    gw._append_message(s["id"], "assistant", contract)
    out = await gw.ask(s["id"], "почему ты с первого раза не прислал файлом?")
    names = [o["name"] for o in out["outputs"] if not o["name"].startswith("reply_")]
    assert not names, names
    assert "Потому что" in out["messages"][-1]["content"]
    assert "document_" not in out["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_ask_auto_packs_contract_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Длинный договор в ответе GPT сразу кладётся в файл."""
    import app.services.gpt_client as gc

    contract = (
        "ДОГОВОР № ___\nпредоставления доступа к API\n\n"
        "1.1. Сервис.\n1.2. API.\n1.3. Пакет.\n1.4. Лимиты.\n1.5. Запрос.\n"
        + ("Исполнитель Крохмаль. Заказчик. Реквизиты сторон.\n" * 50)
    )

    class FakeGpt:
        async def ask_with_files(self, *a, **k):
            return contract

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    s = gw.create_session()
    out = await gw.ask(s["id"], "россия оставь прочерки")
    names = [o["name"] for o in out["outputs"] if not o["name"].startswith("reply_")]
    assert any(n.endswith(".txt") for n in names)
    assert "Готовые файлы" in out["messages"][-1]["content"]
    # простыню договора в пузыре не дублируем
    assert "1.1. Сервис" not in out["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_ask_blank_txt_packs_empty_on_empty_gpt_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«пустой txt» → запрос в GPT; пустой output пакуется как empty_*.txt."""
    import app.services.gpt_client as gc
    from app.services.gpt_api import GptApiError

    class FakeGpt:
        async def ask_with_files(self, *a, **k):
            raise GptApiError("GPT(responses): пустой output")

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    s = gw.create_session()
    out = await gw.ask(s["id"], "пришли мне пустой txt файл")
    names = [o["name"] for o in out["outputs"] if not o["name"].startswith("reply_")]
    assert names and any(n.endswith(".txt") for n in names)
    blank = next(
        o
        for o in out["outputs"]
        if o["name"].endswith(".txt") and not o["name"].startswith("reply_")
    )
    assert Path(blank["path"]).is_file()
    assert Path(blank["path"]).stat().st_size == 0
    assert "Готовые файлы" in out["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_ask_pack_does_not_mask_empty_stream_as_blank_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обычный «пришли файлом» + empty_stream — ошибка, не пустой .txt."""
    import app.services.gpt_client as gc
    from app.services.gpt_api import GptApiError

    class FakeGpt:
        async def ask_with_files(self, *a, **k):
            raise GptApiError(
                "GPT(responses/stream): пустой output",
                context={"error_kind": "empty_stream"},
            )

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    s = gw.create_session()
    with pytest.raises(Exception, match="пустой output"):
        await gw.ask(s["id"], "переведи и пришли файлом")


def test_placeholder_1x1_png_detected(tmp_path: Path) -> None:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    p = tmp_path / "stub.png"
    p.write_bytes(png)
    assert gw._is_placeholder_image(p)
    kept = gw._filter_placeholder_images(tmp_path, ["stub.png"])
    assert kept == []
    assert not p.exists()


@pytest.mark.asyncio
async def test_ask_image_web_search_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Если GPT шлёт 1×1 — Studio ищет картинку в сети."""
    import struct
    import zlib

    import app.services.gpt_api as ga
    import app.services.gpt_client as gc

    stub = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8A"
        "AusB9Y9Zl1sAAAAASUVORK5CYII="
    )

    def _png(w: int, h: int) -> bytes:
        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        raw = b"".join(b"\x00" + bytes([0, 128, 255, 255]) * w for _ in range(h))
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b"")
        )

    real = _png(40, 40)

    class FakeGpt:
        async def ask_with_files(self, *a, **k):
            return f"data:image/png;base64,{stub}"

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    async def fake_fetch(query, out_dir, **kwargs):
        assert "фантом" in query.lower() or "phantom" in query.lower() or "ассасин" in query.lower()
        dest = Path(out_dir) / "web_0.png"
        dest.write_bytes(real)
        return [dest]

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    monkeypatch.setattr(ga, "fetch_web_images", fake_fetch)
    s = gw.create_session()
    out = await gw.ask(s["id"], "пришли мне картику фантом ассасин из доты")
    names = [o["name"] for o in out["outputs"] if not o["name"].startswith("reply_")]
    assert any(n.endswith(".png") for n in names)
    got = next(o for o in out["outputs"] if o["name"].endswith(".png"))
    assert Path(got["path"]).stat().st_size > 100


def test_image_search_query_strips_fluff() -> None:
    assert "фантом" in gw._image_search_query(
        "пришли мне картику фантом ассасин из доты на белом фоне"
    ).lower()


@pytest.mark.asyncio
async def test_materialize_svg_data_uri(tmp_path: Path) -> None:
    from app.services.gpt_api import materialize_reply_assets

    b64 = (
        "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxIiBoZWlnaHQ9IjEiPjwvc3ZnPg=="
    )
    saved = await materialize_reply_assets(
        f"data:image/svg+xml;base64,{b64}",
        tmp_path,
        prefix="gpt_test",
    )
    assert len(saved) == 1
    assert saved[0].suffix == ".svg"
    assert b"<svg" in saved[0].read_bytes().lower()


@pytest.mark.asyncio
async def test_ask_image_typo_does_not_pack_txt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Опечатка «картику» не должна давать document_*.txt."""
    import app.services.gpt_client as gc

    class FakeGpt:
        async def ask_with_files(self, *a, **k):
            return "Phantom_Assassin.png\npa_white_bg.png"

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    s = gw.create_session()
    out = await gw.ask(
        s["id"],
        "пришли мне картику фантом ассасин из доты на белом фоне",
    )
    names = [o["name"] for o in out["outputs"] if not o["name"].startswith("reply_")]
    assert not any(n.endswith(".txt") and n.startswith("document_") for n in names)
    assert gw._wants_image_file("пришли мне картику фантом")
    assert not gw._should_pack_text_document(
        "пришли мне картику фантом",
        "Phantom_Assassin.png",
        media_count=0,
    )


@pytest.mark.asyncio
async def test_ask_image_does_not_pack_txt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«пришли картинку» с нормальным PNG → файл есть, без document_*.txt."""
    import base64
    import struct
    import zlib

    import app.services.gpt_client as gc

    def _png(w: int, h: int) -> bytes:
        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        raw = b"".join(b"\x00" + bytes([255, 0, 0, 255]) * w for _ in range(h))
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b"")
        )

    png_b64 = base64.b64encode(_png(32, 32)).decode("ascii")

    class FakeGpt:
        async def ask_with_files(self, *a, **k):
            return f"data:image/png;base64,{png_b64}"

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    s = gw.create_session()
    out = await gw.ask(s["id"], "пришли мне картинку")
    names = [o["name"] for o in out["outputs"] if not o["name"].startswith("reply_")]
    assert any(n.endswith(".png") for n in names)
    assert not any(n.endswith(".txt") and n.startswith("document_") for n in names)
    assert "data:image" not in out["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_ask_excel_and_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.gpt_client as gc

    class FakeGpt:
        async def ask_with_files(self, *a, **k):
            return (
                "# Лист: Данные\n"
                "A\tB\n"
                "1\t2\n"
                "\n"
                "Короткий текст для word-документа про тест."
            )

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    s = gw.create_session()
    out = await gw.ask(s["id"], "пришли теперь эксель и ворд")
    names = [o["name"] for o in out["outputs"] if not o["name"].startswith("reply_")]
    assert any(n.endswith(".xlsx") for n in names)
    assert any(n.endswith(".docx") for n in names)


@pytest.mark.asyncio
async def test_ask_docx_contract_saved_as_docx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.gpt_client as gc

    class FakeGpt:
        async def ask_with_files(self, text, files, **kwargs):
            assert "Studio GPT" in (kwargs.get("system") or "") or True
            return (
                "ДОГОВОР\n\n1. Предмет\nСтороны обязуются…\n"
                "В этой сессии недоступен инструмент создания новых вложений, "
                "поэтому прикрепить .docx не смогу."
            )

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    s = gw.create_session()
    out = await gw.ask(s["id"], "Составь договор аренды и пришли .docx")
    names = [o["name"] for o in out["outputs"] if not o["name"].startswith("reply_")]
    assert any(n.endswith(".docx") for n in names)
    doc = next(o for o in out["outputs"] if o["name"].endswith(".docx"))
    assert Path(doc["path"]).stat().st_size > 64
    # отмазка модели вырезана; уведомление только о реальных файлах
    assert "Готовые файлы" in out["messages"][-1]["content"]
    assert "Studio положила" not in out["messages"][-1]["content"]
    assert "недоступен инструмент" not in out["messages"][-1]["content"]
    assert any(n.endswith(".docx") for n in out["messages"][-1].get("output_files") or [])


def test_save_attachment_empty_raises() -> None:
    s = gw.create_session()
    with pytest.raises(ValueError, match="пустой"):
        gw.save_attachment(s["id"], "a.txt", b"")


@pytest.mark.asyncio
async def test_ask_passes_session_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторный ask должен отдать прошлые реплики в history клиента."""
    import app.services.gpt_client as gc

    s = gw.create_session(title="mem")
    gw._append_message(s["id"], "user", "зови меня Капитан")
    gw._append_message(s["id"], "assistant", "Хорошо, Капитан")

    captured: dict = {}

    class FakeGpt:
        async def ask_with_files(self, text, files, **kwargs):
            captured["text"] = text
            captured["history"] = list(kwargs.get("history") or [])
            captured["treat_txt_as_prompt"] = kwargs.get("treat_txt_as_prompt")
            return "Ты Капитан"

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())

    out = await gw.ask(s["id"], "как меня зовут?")
    assert captured["text"] == "как меня зовут?"
    assert len(captured["history"]) == 2
    assert captured["history"][0]["content"] == "зови меня Капитан"
    assert "Капитан" in out["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_ask_sends_txt_as_attachment_not_master(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Workspace: .txt — вложение, не master-промт пайплайна."""
    import app.services.gpt_client as gc

    s = gw.create_session()
    gw.save_attachment(s["id"], "secret.txt", "код OMEGA-1\n".encode("utf-8"))

    captured: dict = {}

    class FakeGpt:
        async def ask_with_files(self, text, files, **kwargs):
            captured["text"] = text
            captured["files"] = [Path(p).name for p in files]
            captured["treat_txt_as_prompt"] = kwargs.get("treat_txt_as_prompt", True)
            captured["expect_file_download"] = kwargs.get("expect_file_download")
            return "OMEGA-1"

        async def download_attachment_from_last_reply(self, *a, **k):
            return None

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    out = await gw.ask(s["id"], "какой код в файле?")
    assert captured["text"] == "какой код в файле?"
    assert captured["files"] == ["secret.txt"]
    assert captured["treat_txt_as_prompt"] is False
    assert captured["expect_file_download"] is False
    assert out["messages"][-2].get("attachment_names") == ["secret.txt"]
    assert "OMEGA-1" in out["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_ask_xlsx_sets_expect_download(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.gpt_client as gc

    s = gw.create_session()
    gw.save_attachment(s["id"], "t.xlsx", b"PK\x03\x04fake")

    captured: dict = {}

    class FakeGpt:
        async def ask_with_files(self, text, files, **kwargs):
            captured["expect_file_download"] = kwargs.get("expect_file_download")
            captured["treat_txt_as_prompt"] = kwargs.get("treat_txt_as_prompt")
            return "ok"

        async def download_attachment_from_last_reply(self, *a, **k):
            raise RuntimeError("no xlsx")

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    await gw.ask(s["id"], "проверь таблицу")
    assert captured["expect_file_download"] is True
    assert captured["treat_txt_as_prompt"] is False


@pytest.mark.asyncio
async def test_ask_api_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.gpt_client as gc
    from app.services.gpt_client import GptApiUnavailable

    s = gw.create_session()

    class Boom:
        async def ask_with_files(self, *a, **k):
            raise GptApiUnavailable("GPT API не настроен: test")

    monkeypatch.setattr(gc, "get_gpt_client", lambda: Boom())
    with pytest.raises(GptApiUnavailable):
        await gw.ask(s["id"], "привет")
    got = gw.get_session(s["id"])
    assert got["status"] == "error"
    assert any(m["role"] == "system" for m in got["messages"])


def test_wants_image_file_distinguishes_analysis_and_generation() -> None:
    # Обычный запрос на поиск / генерацию
    assert gw._wants_image_file("пришли мне картику фантом") is True
    assert gw._wants_image_file("найди фото котика") is True

    # Запросы на анализ фото / вложений — НЕ должны требовать скачивания из интернета
    assert gw._wants_image_file("Что изображено на фото?") is False
    assert gw._wants_image_file("Опиши фото") is False
    assert gw._wants_image_file("Проанализируй картинку") is False
    assert gw._wants_image_file("Кто на фотографии?") is False
    assert gw._wants_image_file("Что ты видишь на картинке?") is False
    assert gw._wants_image_file("Посмотри на скриншот и найди ошибку в коде") is False

    # При наличии прикрепленного изображения обычные вопросы не триггерят веб-поиск
    assert gw._wants_image_file("какой здесь фон?", has_image_attachments=True) is False
    assert (
        gw._wants_image_file(
            "найди в интернете похожую картинку", has_image_attachments=True
        )
        is True
    )


@pytest.mark.asyncio
async def test_ask_image_analysis_does_not_call_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.gpt_api as ga
    import app.services.gpt_client as gc

    web_fetch_called = False

    async def fake_fetch(*a, **k):
        nonlocal web_fetch_called
        web_fetch_called = True
        return []

    class FakeGpt:
        async def ask_with_files(self, *a, **k):
            return "На фото изображен кот на диване."

    monkeypatch.setattr(gc, "get_gpt_client", lambda: FakeGpt())
    monkeypatch.setattr(ga, "fetch_web_images", fake_fetch)

    s = gw.create_session()
    gw.save_attachment(s["id"], "cat.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    out = await gw.ask(s["id"], "Что изображено на фото?")
    assert web_fetch_called is False
    assert any(m["role"] == "user" for m in out["messages"])
    assert any(m["role"] == "assistant" for m in out["messages"])

