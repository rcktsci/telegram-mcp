"""Media links (get_media_link tool + GET /files/<code> + media summary).

Covers: link = public_url + /files/<32hex>, empty public_url fails closed,
token scope, no-media error, 404 parity (unknown / expired / missing file),
Content-Type from mime_type, Content-Disposition sanitization, one download
per message (cache hit), expires_at = created_at + ttl, and has_media=true
finding a live photo message via the new compact media summary.
"""

import asyncio
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

import src.mcp.server as server_module
from src.api.ui import ui_app
from src.config import get_settings
from src.db import (
    AttachmentDownloadRepository,
    Chat,
    Message,
    MessageRepository,
    get_session_factory,
)
from src.mcp.auth import set_allowed_ids
from src.telegram.client import (
    TelegramListener,
    _convert_datetime,
    cleanup_media_tmp_files,
    media_kind_of,
    media_summary,
)
from telethon import types as tt

TG_CHAT = 1600

PUBLIC_URL = "http://files.example.com"


# ----- fixtures / fakes ---------------------------------------------------


def _stub_settings(
    public_url=PUBLIC_URL, ttl=300, max_seconds=60, max_mb=200
) -> SimpleNamespace:
    """Minimal Settings stand-in for the tool: media_links + Telegram creds."""
    return SimpleNamespace(
        media_links=SimpleNamespace(
            public_url=public_url,
            ttl_seconds=ttl,
            max_download_seconds=max_seconds,
            max_download_mb=max_mb,
        ),
        telegram=SimpleNamespace(
            api_id=1,
            api_hash="hash",
            sessions=[SimpleNamespace(session_string="session-string")],
        ),
    )


@pytest.fixture()
def media_settings(monkeypatch):
    """Stub get_settings() for the tool; tests tweak fields in place."""
    settings = _stub_settings()
    monkeypatch.setattr("src.config.get_settings", lambda: settings)
    return settings


class FakeClient:
    """get_messages + download_media without network; counts real downloads."""

    def __init__(self, message, slow_seconds: float = 0.0):
        self.message = message
        self.slow_seconds = slow_seconds
        self.downloads = 0

    async def get_messages(self, chat_id, ids):
        return self.message

    async def download_media(self, media, file):
        if self.slow_seconds:
            await asyncio.sleep(self.slow_seconds)
        self.downloads += 1
        return b"file-bytes"


def _listener(client) -> TelegramListener:
    listener = TelegramListener("session-string", 1, "hash")
    listener._client = client
    listener._ready = asyncio.Event()
    listener._ready.set()
    listener._running = True
    return listener


def _use_listener(monkeypatch, listener) -> None:
    async def fake_listener(chat_id):
        return listener

    monkeypatch.setattr("src.mcp.tools._get_listener_for_chat", fake_listener)


def _msg_with_media(kind):
    """Stand-in for a Telethon message with one attachment (media_summary chain)."""
    return SimpleNamespace(
        media=object(),
        photo=kind == "photo",
        document=kind == "document",
        voice=kind == "voice",
        audio=kind == "audio",
        video_note=kind == "video_note",
        sticker=kind == "sticker",
        gif=kind == "animation",
        video=kind == "video",
        file=SimpleNamespace(
            name=None if kind == "photo" else "f.bin",
            mime_type="image/jpeg" if kind == "photo" else "application/octet-stream",
            size=42,
            duration=None,
        ),
    )


NO_MEDIA_MSG = SimpleNamespace(
    media=None, photo=None, document=None, voice=None, audio=None,
    video_note=None, sticker=None, gif=None, video=None, file=None,
)


def _legacy_blob(kind: str) -> dict:
    """raw_json["media"] as stored BEFORE the media-links feature: the full
    telethon to_dict() blob, no "kind". Built from real telethon objects so
    the fixtures match actual DB shapes (no real blobs in the public repo).
    """
    photo = _photo()

    def document(attributes, mime_type="application/octet-stream"):
        return tt.MessageMediaDocument(document=tt.Document(
            id=1, access_hash=2, file_reference=b"", date=None, dc_id=1,
            mime_type=mime_type, size=3, thumbs=None, video_thumbs=None,
            attributes=attributes,
        ))

    builders = {
        "photo": lambda: tt.MessageMediaPhoto(photo=photo, ttl_seconds=None),
        "voice": lambda: document(
            [tt.DocumentAttributeAudio(duration=5, voice=True)], "audio/ogg"
        ),
        "audio": lambda: document(
            [tt.DocumentAttributeAudio(duration=5, voice=False)], "audio/mpeg"
        ),
        "video_note": lambda: document(
            _video_attrs(True),
            "video/mp4",
        ),
        "video": lambda: document(
            _video_attrs(False), "video/mp4"
        ),
        "animation": lambda: document(
            [tt.DocumentAttributeAnimated()] + _video_attrs(False),
            "video/mp4",
        ),
        "sticker": lambda: document(
            [tt.DocumentAttributeSticker(alt="a", stickerset=tt.InputStickerSetEmpty())],
            "image/webp",
        ),
        "document": lambda: document([tt.DocumentAttributeFilename(file_name="f.bin")]),
    }
    # Как в проде: to_dict() хранит bytes, в raw_json они попадают уже
    # после _convert_datetime (base64).
    return _convert_datetime(builders[kind]().to_dict())


def _webpage_blob(with_photo: bool) -> dict:
    webpage = tt.WebPage(
        id=1, url="u", display_url="d", hash=0,
        photo=_photo() if with_photo else None,
    )
    return _convert_datetime(tt.MessageMediaWebPage(webpage=webpage).to_dict())


async def _seed_chat() -> int:
    """One watching chat; returns the internal id used by tools."""
    async with get_session_factory()() as session:
        chat = Chat(chat_id=TG_CHAT, title="media chat", watching=True)
        session.add(chat)
        await session.commit()
        return chat.id


async def _seed_download(
    tmp_path,
    *,
    expires_delta=timedelta(seconds=300),
    write_file=True,
    file_name='report "2026".pdf',
    mime_type="application/pdf",
) -> str:
    code = uuid.uuid4().hex
    path = tmp_path / f"{code}.bin"
    if write_file:
        path.write_bytes(b"%PDF-1.4 test")
    async with get_session_factory()() as session:
        await AttachmentDownloadRepository(session).insert(
            download_code=code,
            chat_id=TG_CHAT,
            message_id=1,
            media_kind="document",
            file_name=file_name,
            mime_type=mime_type,
            size_bytes=12,
            cached_file=str(path),
            expires_at=datetime.now(timezone.utc) + expires_delta,
        )
    return code


async def _request(method: str, url: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ui_app), base_url="http://test"
    ) as client:
        return await client.request(method, url)


# ----- media summary (ingest side) ----------------------------------------


def test_media_summary_shape_and_kinds():
    summary = media_summary(_msg_with_media("video_note"))

    assert set(summary) == {"kind", "mime_type", "size", "file_name", "duration"}
    assert summary["kind"] == "video_note"
    assert media_summary(_msg_with_media("photo"))["kind"] == "photo"
    assert media_summary(_msg_with_media("voice"))["kind"] == "voice"
    assert media_summary(_msg_with_media("animation"))["kind"] == "animation"
    assert media_summary(NO_MEDIA_MSG) is None


def test_kind_priority_specific_over_generic():
    """Real telethon: у кружка истинны И video_note, И video; у mp4-«гифки» —
    И animation (gif), И video. Специфичный вид обязан выигрывать."""
    round_video = _msg_with_media("video_note")
    round_video.video = True
    assert media_summary(round_video)["kind"] == "video_note"

    mp4_gif = _msg_with_media("animation")
    mp4_gif.video = True
    assert media_summary(mp4_gif)["kind"] == "animation"


# ----- media_kind_of (read side: compact summary or legacy blob) ----------


def _photo():
    return tt.Photo(id=1, access_hash=2, file_reference=b"", date=None, dc_id=1, sizes=[])


def _document(mime_type, attributes):
    return tt.Document(
        id=1, access_hash=2, file_reference=b"", date=None, dc_id=1,
        mime_type=mime_type, size=3, thumbs=None, video_thumbs=None,
        attributes=attributes,
    )


def _real_message(media) -> tt.Message:
    """Настоящий telethon.tl.custom.Message: оракул строится на нём, а не на
    SimpleNamespace, чтобы паритет запись/чтение проверялся на реальных
    свойствах (Message.photo/.document/.video...), как в проде."""
    return tt.Message(
        id=1, peer_id=tt.PeerUser(1), date=datetime(2026, 10, 24, tzinfo=timezone.utc),
        message="", media=media,
    )


def _doc_media(mime_type, attributes):
    return tt.MessageMediaDocument(document=_document(mime_type, attributes))


def _webpage_media(photo=None, document=None):
    return tt.MessageMediaWebPage(webpage=tt.WebPage(
        id=1, url="u", display_url="d", hash=0, photo=photo, document=document,
    ))


def _video_attrs(round_message):
    return [tt.DocumentAttributeVideo(duration=5, w=1, h=1, round_message=round_message)]

# (имя, media, ожидаемый kind). Ожидание для пустых оболочек — None, но его
# даёт media_summary: паритет-проверка ниже ловит любое расхождение.
ORACLE_CASES = [
    ("photo", lambda: tt.MessageMediaPhoto(photo=_photo(), ttl_seconds=None), "photo"),
    ("video", lambda: _doc_media("video/mp4", _video_attrs(False)), "video"),
    ("video_note", lambda: _doc_media("video/mp4", _video_attrs(True)), "video_note"),
    ("animation", lambda: _doc_media(
        "video/mp4", [tt.DocumentAttributeAnimated()] + _video_attrs(False)), "animation"),
    ("voice", lambda: _doc_media(
        "audio/ogg", [tt.DocumentAttributeAudio(duration=5, voice=True)]), "voice"),
    ("audio", lambda: _doc_media(
        "audio/mpeg", [tt.DocumentAttributeAudio(duration=5, voice=False)]), "audio"),
    ("sticker", lambda: _doc_media(
        "image/webp",
        [tt.DocumentAttributeSticker(alt="a", stickerset=tt.InputStickerSetEmpty())]), "sticker"),
    ("document", lambda: _doc_media(
        "application/octet-stream", [tt.DocumentAttributeFilename(file_name="f.bin")]), "document"),
    ("webpage_photo", lambda: _webpage_media(photo=_photo()), "photo"),
    ("webpage_video_doc", lambda: _webpage_media(
        document=_document("video/mp4", _video_attrs(False))), "video"),
    ("webpage_photo_and_video_doc", lambda: _webpage_media(
        photo=_photo(), document=_document("video/mp4", _video_attrs(False))), "video"),
    ("webpage_photo_and_plain_doc", lambda: _webpage_media(
        photo=_photo(),
        document=_document("application/octet-stream",
                           [tt.DocumentAttributeFilename(file_name="f.bin")])), "photo"),
    ("webpage_plain_doc", lambda: _webpage_media(
        document=_document("application/octet-stream",
                           [tt.DocumentAttributeFilename(file_name="f.bin")])), "document"),
    ("webpage_empty", lambda: _webpage_media(), None),
    ("photo_empty", lambda: tt.MessageMediaPhoto(photo=tt.PhotoEmpty(id=1), ttl_seconds=None), None),
    ("document_empty", lambda: tt.MessageMediaDocument(document=tt.DocumentEmpty(id=1)), None),
]


@pytest.mark.parametrize("kind", [
    "photo", "voice", "video_note", "animation", "audio", "sticker",
    "video", "document",
])
def test_media_kind_of_legacy_blob_per_form(kind):
    assert media_kind_of(_legacy_blob(kind)) == kind


def test_media_kind_of_legacy_webpage():
    assert media_kind_of(_webpage_blob(with_photo=True)) == "photo"
    assert media_kind_of(_webpage_blob(with_photo=False)) is None


def test_media_kind_of_unknown_and_non_dict_is_none():
    assert media_kind_of({"_": "MessageMediaGeo", "geo": {"_": "GeoPoint"}}) is None
    assert media_kind_of({"mime_type": "image/jpeg"}) is None
    assert media_kind_of("not a dict") is None
    assert media_kind_of(None) is None


def test_media_kind_of_compact_summary_wins_over_legacy_fields():
    blob = _legacy_blob("video")
    blob["kind"] = "photo"  # сводка записана позже — её kind главнее атрибутов
    assert media_kind_of(blob) == "photo"


@pytest.mark.parametrize("name, media, expected", ORACLE_CASES)
def test_media_kind_of_matches_media_summary_on_real_messages(name, media, expected):
    """Сквозная защита от расхождения на настоящем telethon Message:
    media_summary (запись на лету) и media_kind_of (чтение легаси-блоба
    из to_dict()) обязаны дать одинаковый kind. Пустые оболочки (WebPageEmpty,
    PhotoEmpty, DocumentEmpty) дают None с обеих сторон."""
    message = _real_message(media())
    blob = message.to_dict()["media"]
    summary_kind = (media_summary(message) or {}).get("kind")
    assert media_kind_of(blob) == summary_kind
    assert media_kind_of(blob) == expected


async def test_edit_path_keeps_media_kind(db):
    """MessageEdited пишет в raw_json полный to_dict(); сводка должна ставиться
    и там, иначе у отредактированной строки kind пропадает (свип с равным
    edit_date её больше не обновит)."""
    internal = await _seed_chat()
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        session.add(Message(
            chat_id=TG_CHAT, message_id=9, date=now, text="orig",
            raw_json={"media": media_summary(_msg_with_media("photo"))},
        ))
        await session.commit()

    # Как в обработчике MessageEdited: to_dict() + подписанная сводка.
    raw = {"message_id": 9, "text": "edited", "edit_date": "2026-10-24T00:00:00+00:00"}
    raw["media"] = media_summary(_msg_with_media("photo"))
    async with get_session_factory()() as session:
        assert await MessageRepository(session).update_text(TG_CHAT, 9, "edited", raw)

    found = await server_module.get_message_tool(chat_id=internal, message_id=9)

    assert found["text"] == "edited"
    assert found["media_kind"] == "photo"


async def test_has_media_true_finds_live_photo_message(db):
    """Realtime messages now store the compact summary, so has_media=true finds them."""
    internal = await _seed_chat()
    summary = media_summary(_msg_with_media("photo"))
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        session.add_all([
            Message(chat_id=TG_CHAT, message_id=7, date=now, raw_json={"media": summary}),
            Message(chat_id=TG_CHAT, message_id=8, date=now, text="plain"),
        ])
        await session.commit()

    found = await server_module.read_messages_tool(chat_id=internal, has_media=True)

    assert [m["message_id"] for m in found["messages"]] == [7]
    assert found["messages"][0]["has_media"] is True
    assert found["messages"][0]["media_kind"] == "photo"


async def test_has_media_finds_legacy_blob_row_and_reports_kind(db):
    """Rows saved before the media-links feature keep the full telethon blob
    in raw_json["media"] (no "kind"): has_media still finds them, and
    media_kind comes from media_kind_of instead of being null."""
    internal = await _seed_chat()
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        session.add(Message(
            chat_id=TG_CHAT, message_id=11, date=now,
            raw_json={"media": _legacy_blob("video_note")},
        ))
        await session.commit()

    found = await server_module.read_messages_tool(chat_id=internal, has_media=True)

    assert [m["message_id"] for m in found["messages"]] == [11]
    assert found["messages"][0]["has_media"] is True
    assert found["messages"][0]["media_kind"] == "video_note"


async def test_search_messages_reports_kind_for_legacy_blob(db):
    """Legacy rows (full telethon blob, no "kind") must surface the right
    media_kind in search too: the SQL projection pulls the media node and
    media_kind_of parses it; a media-less row keeps None."""
    set_allowed_ids(None)  # search is scope-filtered; другой тест оставляет ∅
    internal = await _seed_chat()
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        session.add_all([
            Message(
                chat_id=TG_CHAT, message_id=21, date=now,
                raw_json={"media": _legacy_blob("photo")},
            ),
            Message(chat_id=TG_CHAT, message_id=20, date=now, text="plain"),
        ])
        await session.commit()

    found = await server_module.search_messages_tool(
        start_datetime="2026-01-01T00:00:00+00:00"
    )

    kinds = {m["message_id"]: m["media_kind"] for m in found["messages"]}
    assert kinds == {21: "photo", 20: None}


async def test_get_message_reports_kind_for_legacy_blob(db):
    """get_message on a pre-feature row (raw_json["media"] is the full telethon
    blob, no "kind") must report the right media_kind, not null."""
    internal = await _seed_chat()
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        session.add(Message(
            chat_id=TG_CHAT, message_id=13, date=now,
            raw_json={"media": _legacy_blob("animation")},
        ))
        await session.commit()

    found = await server_module.get_message_tool(chat_id=internal, message_id=13)

    assert found["media_kind"] == "animation"


# ----- get_media_link tool ------------------------------------------------


async def test_link_is_public_url_plus_files_32hex(db, monkeypatch, tmp_path, media_settings):
    internal = await _seed_chat()
    client = FakeClient(_msg_with_media("photo"))
    _use_listener(monkeypatch, _listener(client))
    monkeypatch.setattr("src.telegram.client.MEDIA_CACHE_DIR", str(tmp_path))

    result = await server_module.get_media_link_tool(chat_id=internal, message_id=5)

    code = result["url"].rsplit("/", 1)[-1]
    assert result["url"] == f"{PUBLIC_URL}/files/{code}"
    assert re.fullmatch(r"[0-9a-f]{32}", code)
    assert result["media_kind"] == "photo"
    assert result["mime_type"] == "image/jpeg"
    assert result["size_bytes"] == 42
    assert result["cached"] is False

    async with get_session_factory()() as session:
        row = await AttachmentDownloadRepository(session).get_by_code(code)
    # telegram chat_id (как в messages.chat_id), не внутренний из list_chats.
    assert row.chat_id == TG_CHAT and row.chat_id != internal
    assert row.message_id == 5


async def test_empty_public_url_fails_closed(db, monkeypatch, media_settings):
    media_settings.media_links.public_url = ""
    internal = await _seed_chat()
    client = FakeClient(_msg_with_media("photo"))
    _use_listener(monkeypatch, _listener(client))

    result = await server_module.get_media_link_tool(chat_id=internal, message_id=5)

    assert "error" in result
    assert "public_url" in result["error"]
    assert "url" not in result


async def test_chat_out_of_scope_forbidden(db):
    await _seed_chat()
    set_allowed_ids(frozenset())  # token without rights: everything forbidden

    result = await server_module.get_media_link_tool(chat_id=1, message_id=5)

    assert result == {"error": "forbidden: chat 1 not allowed for this token"}


async def test_no_media_error(db, monkeypatch, media_settings):
    internal = await _seed_chat()
    _use_listener(monkeypatch, _listener(FakeClient(NO_MEDIA_MSG)))

    result = await server_module.get_media_link_tool(chat_id=internal, message_id=5)

    assert result == {"error": "No media found in message"}


async def test_second_call_serves_from_cache(db, monkeypatch, tmp_path, media_settings):
    internal = await _seed_chat()
    client = FakeClient(_msg_with_media("photo"))
    _use_listener(monkeypatch, _listener(client))
    monkeypatch.setattr("src.telegram.client.MEDIA_CACHE_DIR", str(tmp_path))

    first = await server_module.get_media_link_tool(chat_id=internal, message_id=5)
    second = await server_module.get_media_link_tool(chat_id=internal, message_id=5)

    assert client.downloads == 1  # повторно из Telegram не качаем
    assert second["cached"] is True
    assert second["url"] != first["url"]  # новая строка/код, тот же файл


async def test_expires_at_equals_created_at_plus_ttl(db, monkeypatch, tmp_path, media_settings):
    media_settings.media_links.ttl_seconds = 77
    internal = await _seed_chat()
    _use_listener(monkeypatch, _listener(FakeClient(_msg_with_media("photo"))))
    monkeypatch.setattr("src.telegram.client.MEDIA_CACHE_DIR", str(tmp_path))

    result = await server_module.get_media_link_tool(chat_id=internal, message_id=5)

    async with get_session_factory()() as session:
        row = await AttachmentDownloadRepository(session).get_by_code(
            result["url"].rsplit("/", 1)[-1]
        )
    assert row.expires_at - row.created_at == timedelta(seconds=77)


async def test_oversize_media_refused_without_download(db, monkeypatch, tmp_path, media_settings):
    media_settings.media_links.max_download_mb = 1
    internal = await _seed_chat()
    big = _msg_with_media("document")
    big.file.size = 5 * 1024 * 1024
    client = FakeClient(big)
    _use_listener(monkeypatch, _listener(client))

    result = await server_module.get_media_link_tool(chat_id=internal, message_id=5)

    assert "max_download_mb" in result["error"]
    assert client.downloads == 0


async def test_slow_download_refused_by_timeout(db, monkeypatch, tmp_path, media_settings):
    media_settings.media_links.max_download_seconds = 0.01  # тул передаёт значение как есть
    internal = await _seed_chat()
    client = FakeClient(_msg_with_media("photo"), slow_seconds=0.5)
    _use_listener(monkeypatch, _listener(client))
    monkeypatch.setattr("src.telegram.client.MEDIA_CACHE_DIR", str(tmp_path))

    result = await server_module.get_media_link_tool(chat_id=internal, message_id=5)

    assert "max_download_seconds" in result["error"]
    assert client.downloads == 0


async def test_parallel_same_message_calls_leave_valid_cache(
    db, monkeypatch, tmp_path, media_settings
):
    """Гонка двух параллельных get_media_link одного сообщения: уникальные
    tmp-имена снимают её — оба вызова успешны, кэш на месте и валиден,
    tmp-файлов не осталось. Двойное скачивание допустимо (байты те же)."""
    internal = await _seed_chat()
    # slow_seconds держит оба скачивания в полёте, пока второй вызов проходит
    # exists-проверку: без этого первый успевает записать кэш раньше.
    client = FakeClient(_msg_with_media("photo"), slow_seconds=0.2)
    _use_listener(monkeypatch, _listener(client))
    monkeypatch.setattr("src.telegram.client.MEDIA_CACHE_DIR", str(tmp_path))

    # Дискриминатор гонки: os.replace протоколирует имя исходного файла.
    # Новый код пишет два разных tmp-имени, старый с общим filepath + ".tmp" —
    # одно, и тест обязан падать на проверке различия.
    recorded: list[str] = []
    real_replace = os.replace

    def recording_replace(src, dst):
        if src.endswith(".tmp"):  # только tmp нашего кэша: счётчик не зависит от чужих replace
            recorded.append(src)
        return real_replace(src, dst)

    monkeypatch.setattr("src.telegram.client.os.replace", recording_replace)

    results = await asyncio.gather(
        server_module.get_media_link_tool(chat_id=internal, message_id=5),
        server_module.get_media_link_tool(chat_id=internal, message_id=5),
    )

    assert all("error" not in r for r in results)
    assert client.downloads == 2  # exists-проверки оба прошли до первой записи
    assert len(recorded) == 2
    assert recorded[0] != recorded[1]  # у каждого вызова своё tmp-имя
    cached = [p for p in tmp_path.iterdir() if not p.name.endswith(".tmp")]
    assert len(cached) == 1
    assert cached[0].read_bytes() == b"file-bytes"
    assert not list(tmp_path.glob("*.tmp"))


async def test_interrupted_write_leaves_no_tmp(db, monkeypatch, tmp_path, media_settings):
    """Прерванный вызов (исключение на os.replace) не оставляет свой tmp."""
    internal = await _seed_chat()
    _use_listener(monkeypatch, _listener(FakeClient(_msg_with_media("photo"))))
    monkeypatch.setattr("src.telegram.client.MEDIA_CACHE_DIR", str(tmp_path))

    def boom(src, dst):
        raise OSError("interrupted")

    monkeypatch.setattr("src.telegram.client.os.replace", boom)

    result = await server_module.get_media_link_tool(chat_id=internal, message_id=5)

    assert "error" in result
    assert not list(tmp_path.glob("*.tmp"))


def test_startup_cleanup_removes_tmp_files_only(tmp_path):
    """Очистка на старте: *.tmp от прерванных вызовов удаляются,
    обычные файлы кэша не трогаются."""
    stale = tmp_path / "1600_5.0123abcd.tmp"
    stale.write_bytes(b"half-written")
    keep = tmp_path / "1600_5.jpg"
    keep.write_bytes(b"file-bytes")

    removed = cleanup_media_tmp_files(str(tmp_path))

    assert removed == 1
    assert not list(tmp_path.glob("*.tmp"))
    assert keep.read_bytes() == b"file-bytes"


def test_startup_cleanup_resolves_cache_dir_at_call_time(tmp_path, monkeypatch):
    """cache_dir по умолчанию разрешается в момент вызова, не на импорте:
    подмена MEDIA_CACHE_DIR действует на вызов без аргумента."""
    stale = tmp_path / "1600_5.deadbeef.tmp"
    stale.write_bytes(b"half-written")
    monkeypatch.setattr("src.telegram.client.MEDIA_CACHE_DIR", str(tmp_path))

    assert cleanup_media_tmp_files() == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_startup_cleanup_missing_dir_returns_zero(tmp_path):
    """Несуществующий каталог: 0 и без исключений — старт не роняем."""
    assert cleanup_media_tmp_files(str(tmp_path / "does-not-exist")) == 0


# ----- GET /files/<code> ---------------------------------------------------


async def test_unknown_code_404(db):
    resp = await _request("GET", f"/files/{'0' * 32}")

    assert resp.status_code == 404


async def test_non_hex_code_404():
    """Код, не проходящий regex, режется до похода в БД."""
    resp = await _request("GET", "/files/XYZ")

    assert resp.status_code == 404


async def test_expired_code_404(db, tmp_path):
    code = await _seed_download(tmp_path, expires_delta=timedelta(seconds=-1))

    expired = await _request("GET", f"/files/{code}")
    unknown = await _request("GET", f"/files/{'0' * 32}")

    assert expired.status_code == 404
    # Один и тот же 404: истёкший код не отличим от отсутствующей строки.
    assert expired.json() == unknown.json()


async def test_row_without_file_404_not_500(db, tmp_path):
    code = await _seed_download(tmp_path, write_file=False)

    resp = await _request("GET", f"/files/{code}")

    assert resp.status_code == 404


async def test_served_file_content_type(db, tmp_path):
    code = await _seed_download(tmp_path)

    resp = await _request("GET", f"/files/{code}")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content == b"%PDF-1.4 test"


async def test_mime_type_with_crlf_falls_back_to_octet_stream(db, tmp_path):
    """mime_type задаёт отправитель документа: CRLF-значение не должно ломать
    ответ (h11 отвергает такой заголовок) — fallback на octet-stream."""
    code = await _seed_download(tmp_path, mime_type="application/pdf\r\nX-Injected: 1")

    resp = await _request("GET", f"/files/{code}")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"


async def test_content_disposition_sanitized(db, tmp_path):
    code = await _seed_download(tmp_path, file_name='we"rd\r\nname.pdf')

    resp = await _request("GET", f"/files/{code}")

    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    # CR/LF и кавычки из file_name вырезаны; внешние кавычки добавляет starlette.
    assert disposition.split("filename=", 1)[1] == '"werdname.pdf"'
    assert "\r" not in disposition and "\n" not in disposition


def test_media_links_env_override(monkeypatch):
    monkeypatch.setenv("MEDIA_LINKS_TTL_SECONDS", "77")
    get_settings.cache_clear()
    try:
        assert get_settings().media_links.ttl_seconds == 77
        assert get_settings().media_links.public_url == ""
    finally:
        get_settings.cache_clear()


def test_media_links_empty_env_falls_through_to_yaml(tmp_path, monkeypatch):
    """Пустая переменная = «не задано»: она не перебивает config.yml
    (копия .env.example с MEDIA_LINKS_PUBLIC_URL= не должна убивать yaml)."""
    monkeypatch.setenv("MEDIA_LINKS_PUBLIC_URL", "")
    monkeypatch.setenv("MEDIA_LINKS_TTL_SECONDS", "")
    (tmp_path / "config.yml").write_text(
        'media_links:\n  public_url: "http://yaml.example.com"\n  ttl_seconds: 555\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        assert get_settings().media_links.public_url == "http://yaml.example.com"
        assert get_settings().media_links.ttl_seconds == 555
    finally:
        get_settings.cache_clear()


def test_media_links_env_beats_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LINKS_PUBLIC_URL", "http://env.example.com")
    (tmp_path / "config.yml").write_text(
        'media_links:\n  public_url: "http://yaml.example.com"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        assert get_settings().media_links.public_url == "http://env.example.com"
    finally:
        get_settings.cache_clear()
