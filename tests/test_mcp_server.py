"""MCP server surface: tools and prompts must be registered after the mcp 2.x
migration (FastMCP -> MCPServer). Guards the rename and the moved transport
options (host/stateless/json_response now live in run(), not the constructor).
"""

import src.mcp.server as server_module
from src.db import Chat, get_session_factory


async def test_all_tools_registered():
    tools = await server_module.mcp_server.list_tools()
    names = {t.name for t in tools}
    assert {
        "list_chats",
        "read_messages",
        "search_messages",
        "get_chat_members",
        "get_media_link",
        "get_message",
    } <= names, f"missing tools: {names}"


async def test_guide_prompt_registered():
    prompts = await server_module.mcp_server.list_prompts()
    names = {p.name for p in prompts}
    assert "telegram_mcp_guide" in names, f"missing prompt: {names}"


async def test_list_chats_tool_returns_dict_wrapper(db):
    """A bare list return serializes as one text block per element (glued
    pretty-printed objects, not valid JSON): the tool must wrap it in a dict.
    """
    async with get_session_factory()() as session:
        session.add(Chat(chat_id=900, title="t", watching=True))
        session.add(Chat(chat_id=901, title="u", watching=True))
        await session.commit()

    result = await server_module.list_chats_tool()

    assert set(result) == {"count", "chats"}
    assert result["count"] == len(result["chats"])
    assert result["count"] >= 2
    assert all(not isinstance(chat, str) for chat in result["chats"])
