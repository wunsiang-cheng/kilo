import pytest

from kilo.models.session import MessageRole
from kilo.storage.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(db_path=tmp_path / "kilo.db")
    await database.init()
    return database


async def test_session_round_trip_with_blocks(db):
    session = await db.create_session(title=" Title ", metadata={"a": 1})
    blocks = [{"type": "text", "text": "學"}]
    await db.add_message(session.id, MessageRole.ASSISTANT, content="hi", blocks=blocks)
    stored = await db.get_session(session.id)
    assert stored.title == "Title" and stored.metadata == {"a": 1}
    assert [(m.role, m.content, m.blocks) for m in stored.messages] == [(MessageRole.ASSISTANT, "hi", blocks)]


async def test_untitled_session_gets_a_generated_title(db):
    session = await db.create_session()
    assert session.title == f"Session {session.id}"


async def test_missing_session_is_none(db):
    assert await db.get_session("sess_x") is None
    assert await db.update_session("sess_x", title="t") is None
    assert await db.delete_session("sess_x") is False


async def test_metadata_update_merges(db):
    session = await db.create_session(metadata={"a": 1})
    updated = await db.update_session(session.id, metadata={"b": 2})
    assert updated.metadata == {"a": 1, "b": 2}


async def test_delete_session_removes_messages(db):
    session = await db.create_session()
    await db.add_message(session.id, MessageRole.USER, content="hi")
    assert await db.delete_session(session.id)
    assert await db.get_messages(session.id) == []


async def test_list_sessions_most_recently_updated_first(db):
    first = await db.create_session()
    second = await db.create_session()
    await db.add_message(first.id, MessageRole.USER, content="hi")
    assert [s.id for s in await db.list_sessions()] == [first.id, second.id]


async def test_get_messages_since_id(db):
    session = await db.create_session()
    await db.add_message(session.id, MessageRole.USER, content="one")
    middle = await db.add_message(session.id, MessageRole.USER, content="two")
    await db.add_message(session.id, MessageRole.USER, content="three")
    messages = await db.get_messages(session.id, since_id=middle.id)
    assert [m.content for m in messages] == ["two", "three"]


async def test_data_survives_a_new_database_object(db, tmp_path):
    session = await db.create_session(title="kept")
    again = Database(db_path=tmp_path / "kilo.db")
    await again.init()
    assert (await again.get_session(session.id)).title == "kept"
