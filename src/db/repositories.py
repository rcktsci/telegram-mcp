from datetime import datetime
from typing import Optional
import re
import uuid

from sqlalchemy import select, func, and_, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AttachmentDownload, AuthToken, Chat, Message


class ChatRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(self) -> list[Chat]:
        result = await self.session.execute(select(Chat))
        return list(result.scalars().all())

    async def get_by_chat_id(self, chat_id: int) -> Optional[Chat]:
        result = await self.session.execute(select(Chat).where(Chat.chat_id == chat_id))
        return result.scalar_one_or_none()

    async def create(self, chat_id: int, title: Optional[str] = None, is_private: bool = False, is_group: bool = False) -> Chat:
        chat = Chat(chat_id=chat_id, title=title, is_private=is_private, is_group=is_group)
        self.session.add(chat)
        await self.session.commit()
        await self.session.refresh(chat)
        return chat

    async def get_watching_chats(self, user_id: Optional[int] = None) -> list[Chat]:
        query = select(Chat).where(Chat.watching == True)
        if user_id:
            from sqlalchemy import or_
            query = query.where(
                or_(Chat.user_id == user_id, Chat.user_id == None)
            )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def upsert_chats(self, chats: list[dict], watching: bool = False, user_id: Optional[int] = None) -> None:
        from sqlalchemy.dialects.postgresql import insert
        from src.db.models import Chat
        import logging
        logger = logging.getLogger(__name__)

        if not chats:
            logger.info("upsert_chats: no chats to insert")
            return

        total_batches = (len(chats) + 9) // 10
        logger.info(f"UPSERT: starting {len(chats)} chats, {total_batches} batches of 10")

        for i in range(0, len(chats), 10):
            batch_num = i // 10 + 1
            batch = chats[i:i+10]
            logger.debug(f"UPSERT: batch {batch_num}/{total_batches} - {len(batch)} chats")
            try:
                if user_id is not None:
                    # Stamp the owning account on INSERT only; on conflict do not
                    # reassign (two accounts may share a chat — first owner wins).
                    batch = [{**row, "user_id": user_id} for row in batch]
                stmt = insert(Chat).values(batch)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['chat_id'],
                    set_={
                        'title': stmt.excluded.title,
                        'is_group': stmt.excluded.is_group,
                        'is_private': stmt.excluded.is_private,
                    }
                )
                logger.debug(f"UPSERT: batch {batch_num} - executing...")
                await self.session.execute(stmt)
                logger.debug(f"UPSERT: batch {batch_num} - execute done")
            except Exception as e:
                logger.error(f"UPSERT: batch {batch_num} failed: {e}")
                import traceback
                logger.error(traceback.format_exc())
                raise
        
        logger.info(f"UPSERT: all {total_batches} batches queued, committing...")
        await self.session.commit()
        logger.info(f"UPSERT: committed successfully")

    async def update_message_bounds(self, chat_id: int, min_id: int, max_id: int, min_date: Optional[datetime] = None, max_date: Optional[datetime] = None) -> None:
        """Atomically widen the message bounds (Go backport).

        A single UPDATE with LEAST/GREATEST replaces the previous
        select→compare→save round-trips, removing the lost-update race
        between concurrent writers. Postgres LEAST/GREATEST ignore NULLs,
        so a NULL bound (or a NULL date arg) simply keeps/supplies the
        other side — bounds only ever grow, same invariant as before.
        """
        from sqlalchemy import text
        await self.session.execute(text("""
            UPDATE chats SET
                min_message_id   = LEAST(min_message_id, :min_id),
                max_message_id   = GREATEST(max_message_id, :max_id),
                min_message_date = LEAST(min_message_date, :min_date),
                max_message_date = GREATEST(max_message_date, :max_date),
                updated_at       = NOW()
            WHERE chat_id = :chat_id
        """), {
            "chat_id": chat_id,
            "min_id": min_id,
            "max_id": max_id,
            "min_date": min_date,
            "max_date": max_date,
        })
        await self.session.commit()

    async def get_history_sweep_upto(self, chat_id: int) -> Optional[int]:
        """Resume checkpoint of the history sweep (None = sweep never ran)."""
        result = await self.session.execute(
            select(Chat.history_sweep_upto).where(Chat.chat_id == chat_id)
        )
        return result.scalar_one_or_none()

    async def update_history_sweep_upto(self, chat_id: int, upto: int) -> None:
        """Advance the sweep checkpoint monotonically (GREATEST ignores NULL).

        Only the sweep calls this; the listener and the existing loaders must
        never move it.
        """
        from sqlalchemy import text
        await self.session.execute(text("""
            UPDATE chats SET
                history_sweep_upto = GREATEST(history_sweep_upto, :upto),
                updated_at         = NOW()
            WHERE chat_id = :chat_id
        """), {"chat_id": chat_id, "upto": upto})
        await self.session.commit()

    async def update_topics(self, chat_id: int, topics) -> None:
        from src.db.models import Chat
        from sqlalchemy import update
        stmt = update(Chat).where(Chat.chat_id == chat_id).values(topics=topics)
        await self.session.execute(stmt)
        await self.session.commit()

    async def add_topic(self, chat_id: int, topic_id: int, name: str) -> bool:
        """Merge a single topic into chats.topics. Returns False if the chat is unknown."""
        import json

        from sqlalchemy import text
        result = await self.session.execute(text("""
            UPDATE chats
            SET topics = COALESCE(
                CASE WHEN jsonb_typeof(topics) = 'object' THEN topics END,
                '{}'::jsonb
            ) || CAST(:topic AS jsonb)
            WHERE chat_id = :chat_id
        """), {"topic": json.dumps({str(topic_id): name}), "chat_id": chat_id})
        await self.session.commit()
        return result.rowcount > 0


class MessageRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_chat_and_message_id(self, chat_id: int, message_id: int) -> Optional[Message]:
        result = await self.session.execute(
            select(Message).where(
                and_(Message.chat_id == chat_id, Message.message_id == message_id)
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        chat_id: int,
        message_id: int,
        sender_id: Optional[int] = None,
        sender_username: Optional[str] = None,
        sender_name: Optional[str] = None,
        text: Optional[str] = None,
        date: Optional[datetime] = None,
        raw_json: Optional[dict] = None,
        topic_id: Optional[int] = None,
        topic_name: Optional[str] = None,
    ) -> Optional[Message]:
        """Idempotent insert (Go backport).

        The realtime worker and the history loader can race on the same
        (chat_id, message_id). ON CONFLICT DO NOTHING keeps the original
        row and returns None instead of raising IntegrityError.
        """
        stmt = insert(Message).values(
            chat_id=chat_id,
            message_id=message_id,
            sender_id=sender_id,
            sender_username=sender_username,
            sender_name=sender_name,
            text=text,
            date=date,
            raw_json=raw_json,
            topic_id=topic_id,
            topic_name=topic_name,
        ).on_conflict_do_nothing(index_elements=["chat_id", "message_id"]).returning(Message)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one_or_none()

    async def create_batch(self, messages: list[dict], session=None) -> int:
        if not messages:
            return 0
        
        for attempt in range(3):
            try:
                stmt = insert(Message).values(messages)
                # Edit-freshness policy: a conflicting batch copy refreshes the
                # stored row only when it carries a strictly NEWER edit_date.
                # Covers edits missed while the realtime watcher was off: the
                # sweep refetches the overlap zone and refreshes the stale row.
                # Telegram edits are monotonic; edit_date lives in raw_json as
                # an ISO string written by client._convert_datetime — a plain
                # lexicographic compare is correct and cannot fail the cast.
                # No edit_date in the incoming copy → first writer wins, and
                # equal/older edit_dates never overwrite (skipped, counted 0).
                # RETURNING covers freshness-updates as well as new inserts,
                # so the returned count is inserts+updates: the sweep's
                # "new messages" log counts a stale-row refresh as saved.
                stmt = stmt.on_conflict_do_update(
                    index_elements=['chat_id', 'message_id'],
                    set_={
                        'text': stmt.excluded.text,
                        'raw_json': stmt.excluded.raw_json,
                        'is_edited': True,
                    },
                    where=text(
                        "COALESCE(excluded.raw_json->>'edit_date', '') > "
                        "COALESCE(messages.raw_json->>'edit_date', '')"
                    ),
                )
                result = await session.execute(stmt.returning(Message.id))
                await session.commit()
                return len(result.fetchall())
            except Exception as e:
                if attempt < 2 and "connection" in str(e).lower():
                    import asyncio
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
                raise
        
        return 0

    async def get_message_id_bounds(self, chat_id: int) -> tuple[Optional[int], Optional[int]]:
        result = await self.session.execute(
            select(
                func.min(Message.message_id),
                func.max(Message.message_id)
            ).where(Message.chat_id == chat_id)
        )
        row = result.one()
        return row[0], row[1]

    async def get_message_date_bounds(self, chat_id: int) -> tuple[Optional[datetime], Optional[datetime]]:
        result = await self.session.execute(
            select(
                func.min(Message.date),
                func.max(Message.date)
            ).where(Message.chat_id == chat_id)
        )
        row = result.one()
        return row[0], row[1]

    async def read_messages(
        self,
        chat_id: int,
        start_date: Optional[datetime] = None,
        start_message_id: Optional[int] = None,
        limit: int = 100,
        sender_id: Optional[int] = None,
        search_regex: Optional[str] = None,
    ) -> list[Message]:
        query = select(Message).where(Message.chat_id == chat_id)

        if start_date:
            query = query.where(Message.date >= start_date)
        if start_message_id:
            query = query.where(Message.message_id >= start_message_id)
        if sender_id:
            query = query.where(Message.sender_id == sender_id)
        if search_regex:
            safe_regex = re.escape(search_regex)
            query = query.where(Message.text.op("~")(safe_regex))

        query = query.order_by(Message.date.desc()).limit(limit)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_topic_ids_for_chat(self, chat_id: int) -> set:
        result = await self.session.execute(
            select(Message.topic_id).where(
                and_(Message.chat_id == chat_id, Message.topic_id.isnot(None))
            )
        )
        return {r[0] for r in result.fetchall()}

    async def update_text(self, chat_id: int, message_id: int, new_text: str, raw_json=None) -> bool:
        result = await self.session.execute(
            select(Message).where(
                and_(Message.chat_id == chat_id, Message.message_id == message_id)
            )
        )
        message = result.scalar_one_or_none()
        if message:
            message.text = new_text
            message.is_edited = True
            if raw_json is not None:
                message.raw_json = raw_json
            await self.session.commit()
            return True
        return False

    async def mark_deleted(self, chat_id: int, message_id: int) -> bool:
        result = await self.session.execute(
            select(Message).where(
                and_(Message.chat_id == chat_id, Message.message_id == message_id)
            )
        )
        message = result.scalar_one_or_none()
        if message:
            message.is_deleted = True
            await self.session.commit()
            return True
        return False


class AuthTokenRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, comment: str, folder_ids: list[int], chat_ids: list[int]) -> AuthToken:
        token = AuthToken(
            token=str(uuid.uuid4()),
            comment=comment,
            folder_ids=folder_ids,
            chat_ids=chat_ids,
        )
        self.session.add(token)
        await self.session.commit()
        await self.session.refresh(token)
        return token

    async def get_all(self) -> list[AuthToken]:
        # Newest first: the UI table must not jump after updates.
        result = await self.session.execute(
            select(AuthToken).order_by(AuthToken.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_token(self, token: str) -> Optional[AuthToken]:
        result = await self.session.execute(select(AuthToken).where(AuthToken.token == token))
        return result.scalar_one_or_none()

    async def update(self, token_id: int, comment: str, folder_ids: list[int], chat_ids: list[int]) -> Optional[AuthToken]:
        """Full replacement of comment/folder_ids/chat_ids (API PATCH contract)."""
        result = await self.session.execute(select(AuthToken).where(AuthToken.id == token_id))
        token = result.scalar_one_or_none()
        if token:
            token.comment = comment
            token.folder_ids = folder_ids
            token.chat_ids = chat_ids
            await self.session.commit()
            await self.session.refresh(token)
            return token
        return None

    async def delete(self, token_id: int) -> bool:
        result = await self.session.execute(select(AuthToken).where(AuthToken.id == token_id))
        token = result.scalar_one_or_none()
        if token:
            await self.session.delete(token)
            await self.session.commit()
            return True
        return False


class AttachmentDownloadRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def insert(
        self,
        download_code: str,
        chat_id: int,
        message_id: int,
        media_kind: str,
        cached_file: str,
        expires_at: datetime,
        created_at: Optional[datetime] = None,
        file_name: Optional[str] = None,
        mime_type: Optional[str] = None,
        size_bytes: Optional[int] = None,
    ) -> AttachmentDownload:
        row = AttachmentDownload(
            download_code=download_code,
            chat_id=chat_id,
            message_id=message_id,
            media_kind=media_kind,
            cached_file=cached_file,
            expires_at=expires_at,
            created_at=created_at,
            file_name=file_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def get_by_code(self, download_code: str) -> Optional[AttachmentDownload]:
        result = await self.session.execute(
            select(AttachmentDownload).where(AttachmentDownload.download_code == download_code)
        )
        return result.scalar_one_or_none()
