import base64
import logging
import mimetypes
import time
from pathlib import Path

from db.repositories import contact_repo, message_repo, tag_repo, usage_repo

logger = logging.getLogger(__name__)


class TagRegistry:
    """Global tag registry backed by SQLite tags table."""

    def __init__(self):
        self._tags: dict[str, dict] = {}
        self._load()

    def _load(self):
        self._tags = tag_repo.get_all()

    def save(self):
        pass  # Each mutation already commits to DB

    def all(self) -> dict[str, dict]:
        return dict(self._tags)

    def create(self, name: str, color: str) -> bool:
        if name in self._tags:
            return False
        if tag_repo.create(name, color):
            self._tags[name] = {"color": color}
            return True
        return False

    def update(self, old_name: str, *, new_name: str | None = None, color: str | None = None) -> bool:
        if old_name not in self._tags:
            return False
        if not tag_repo.update(old_name, new_name=new_name, color=color):
            return False
        if color:
            self._tags[old_name]["color"] = color
        if new_name and new_name != old_name:
            self._tags[new_name] = self._tags.pop(old_name)
        return True

    def delete(self, name: str) -> bool:
        if name not in self._tags:
            return False
        if tag_repo.delete(name):
            del self._tags[name]
            return True
        return False

    def get(self, name: str) -> dict | None:
        return self._tags.get(name)


class ContactMemory:
    """Persistent per-contact memory backed by SQLite.

    Maintains an in-memory cache of contact metadata for fast access.
    Messages and usage are stored directly in SQLite (not cached in memory).
    """

    def __init__(self, phone: str, default_ai_enabled: bool = True):
        self.phone = phone
        self._default_ai_enabled = default_ai_enabled
        self.id: int | None = None
        self.info: dict = {"name": "", "email": "", "profession": "", "company": "", "address": "", "observations": []}
        self.tags: list[str] = []
        self.ai_enabled: bool = True
        self.is_group: bool = False
        self.group_name: str = ""
        self.is_archived: bool = False
        self.archived_by_app: bool = False
        self.can_send: bool = True
        self.unread_count: int = 0
        self.unread_ai_count: int = 0
        self.created_at: float = time.time()
        self.updated_at: float = time.time()
        self._load()

    def _load(self):
        data = contact_repo.get_or_create(self.phone, default_ai_enabled=self._default_ai_enabled)
        self.id = data["id"]
        self.ai_enabled = data["ai_enabled"]
        self.is_group = data["is_group"]
        self.group_name = data["group_name"]
        self.is_archived = data["is_archived"]
        self.archived_by_app = data["archived_by_app"]
        self.can_send = data.get("can_send", True)
        self.unread_count = data["unread_count"]
        self.unread_ai_count = data["unread_ai_count"]
        self.created_at = data["created_at"]
        self.updated_at = data["updated_at"]

        # Load info fields
        observations = contact_repo.get_observations(self.id)
        self.info = {
            "name": data["name"],
            "email": data["email"],
            "profession": data["profession"],
            "company": data["company"],
            "address": data["address"],
            "observations": observations,
        }

        # Load tags
        self.tags = tag_repo.get_contact_tags(self.id)

    @property
    def messages(self) -> list[dict]:
        """Lazy-load all messages from SQLite."""
        return message_repo.get_all(self.id)

    def save(self):
        """Persist current contact metadata to SQLite."""
        self.updated_at = time.time()
        contact_repo.update(
            self.id,
            name=self.info.get("name", ""),
            email=self.info.get("email", ""),
            profession=self.info.get("profession", ""),
            company=self.info.get("company", ""),
            address=self.info.get("address", ""),
            ai_enabled=1 if self.ai_enabled else 0,
            is_group=1 if self.is_group else 0,
            group_name=self.group_name,
            is_archived=1 if self.is_archived else 0,
            can_send=1 if self.can_send else 0,
            unread_count=self.unread_count,
            unread_ai_count=self.unread_ai_count,
        )

    def add_message(self, role: str, content: str, *,
                    media_type: str | None = None, media_path: str | None = None,
                    status: str | None = None, msg_id: str | None = None,
                    reply_to_msg_id: str | None = None,
                    media_state: str | None = None):
        message_repo.add(
            self.id, role, content,
            media_type=media_type, media_path=media_path,
            status=status, msg_id=msg_id, reply_to_msg_id=reply_to_msg_id,
            media_state=media_state,
        )
        # Touch updated_at
        contact_repo.update(self.id)

    def get_unread_msg_ids(self) -> list[str]:
        """Return unread message IDs from the database."""
        from sqlalchemy import select

        from db.engine import get_engine
        from db.tables import unread_msg_ids
        with get_engine().connect() as conn:
            rows = conn.execute(
                select(unread_msg_ids.c.msg_id).where(unread_msg_ids.c.contact_id == self.id)
            ).all()
        return [r.msg_id for r in rows]

    def increment_unread(self, msg_id: str | None = None):
        self.unread_count += 1
        contact_repo.increment_unread(self.id, msg_id)

    def increment_unread_ai(self):
        self.unread_ai_count += 1
        contact_repo.increment_unread_ai(self.id)

    def mark_mention(self):
        """Flag that the bot was @mentioned in this group (unread)."""
        contact_repo.set_mention(self.id)

    def mark_as_read(self) -> list[str]:
        """Reset unread count and return the list of unread msg_ids (for read receipts)."""
        msg_ids = contact_repo.mark_as_read(self.id)
        self.unread_count = 0
        self.unread_ai_count = 0
        return msg_ids

    def mark_user_messages_as_read(self) -> list[str]:
        """Reset only unread_count (user messages), preserving unread_ai_count."""
        msg_ids = contact_repo.mark_user_messages_as_read(self.id)
        self.unread_count = 0
        return msg_ids

    def mark_as_unread(self):
        """Mark this contact as unread — re-light the in-app green badge."""
        contact_repo.mark_as_unread(self.id)
        if self.unread_count < 1:
            self.unread_count = 1

    def set_ai_enabled(self, enabled: bool):
        self.ai_enabled = enabled
        contact_repo.update(self.id, ai_enabled=1 if enabled else 0)

    def set_tags(self, tags: list[str]):
        self.tags = list(tags)
        tag_repo.set_contact_tags(self.id, self.tags)

    def add_tag(self, tag_name: str):
        if tag_name not in self.tags:
            self.tags.append(tag_name)
            tag_repo.add_contact_tag(self.id, tag_name)

    def remove_tag(self, tag_name: str):
        if tag_name in self.tags:
            self.tags.remove(tag_name)
            tag_repo.remove_contact_tag(self.id, tag_name)

    def get_context_messages(self, limit: int) -> list[dict]:
        """Return the last N messages formatted for the LLM (without ts).

        For the most recent image message from the user, include a base64 data
        URI so the vision model can see it.  Older images are replaced with a
        placeholder to keep token usage reasonable.
        """
        recent = message_repo.get_context(self.id, limit)

        # Find the index of the last user image message that still needs the
        # binary inlined.  If the content already has a textual description
        # (added by the image_transcription pipeline), keep it as text — the
        # main LLM may not support image input.
        last_image_idx = -1
        for i in range(len(recent) - 1, -1, -1):
            m = recent[i]
            if m.get("media_type") == "image" and m["role"] == "user":
                if "[Descrição da imagem]:" not in (m.get("content") or ""):
                    last_image_idx = i
                break

        result: list[dict] = []
        for i, m in enumerate(recent):
            mt = m.get("media_type")
            if m["role"] == "private_note":
                content = m.get("content", "")
                result.append({"role": "user",
                               "content": f"[Nota privada do operador]: {content}"})
            elif mt == "image" and m["role"] == "user":
                # A discarded/removed image has no media_path: the saved text
                # (description or placeholder) is all the model gets.
                if i == last_image_idx and m.get("media_path"):
                    content = _build_image_content(m.get("media_path", ""), m.get("content", ""))
                else:
                    content = m.get("content") or "[Imagem enviada pelo contato]"
                result.append({"role": m["role"], "content": content})
            else:
                content = m.get("content", "")
                if m["role"] == "assistant" and m.get("status") == "operator":
                    content = f"[Mensagem do operador humano]: {content}"
                result.append({"role": m["role"], "content": content})
        return result

    def set_wa_name(self, wa_name: str) -> None:
        """Set contact name from WhatsApp pushName if no manual name exists."""
        current = self.info.get("name", "")
        if current and not current.startswith("~"):
            return
        new_name = f"~{wa_name}"
        if current != new_name:
            self.info["name"] = new_name
            contact_repo.update(self.id, name=new_name)

    def update_info(self, **kwargs):
        """Update contact info fields. Only overwrites non-empty values."""
        fields_to_update = {}
        for key in ("name", "email", "profession", "company", "address"):
            val = kwargs.get(key, "")
            if val:
                self.info[key] = val
                fields_to_update[key] = val
        if fields_to_update:
            contact_repo.update(self.id, **fields_to_update)
        observation = kwargs.get("observation", "")
        if observation and observation not in self.info.get("observations", []):
            self.info.setdefault("observations", []).append(observation)
            contact_repo.add_observation(self.id, observation)

    def add_usage(self, call_type: str, model: str,
                  prompt_tokens: int, completion_tokens: int,
                  total_tokens: int, cost_usd: float, *,
                  cached_tokens: int | None = None, saved_usd: float | None = None) -> None:
        usage_repo.add(self.id, call_type, model, prompt_tokens,
                       completion_tokens, total_tokens, cost_usd,
                       cached_tokens=cached_tokens, saved_usd=saved_usd)

    def get_usage_summary(self, start_ts: float | None = None,
                          end_ts: float | None = None) -> dict:
        """Return aggregated usage stats for this contact."""
        return usage_repo.summary(self.id, start_ts, end_ts)

    def get_info_summary(self) -> str:
        """Format contact info for injection into system prompt."""
        parts = []
        if self.info.get("name"):
            parts.append(f"Nome: {self.info['name']}")
        if self.info.get("email"):
            parts.append(f"Email: {self.info['email']}")
        if self.info.get("profession"):
            parts.append(f"Profissão: {self.info['profession']}")
        if self.info.get("company"):
            parts.append(f"Empresa: {self.info['company']}")
        if self.info.get("address"):
            parts.append(f"Endereço: {self.info['address']}")
        for obs in self.info.get("observations", []):
            parts.append(f"Obs: {obs}")
        return "\n".join(parts)

    def get_tags_summary(self) -> str:
        """Return comma-separated list of tag names for prompt injection."""
        return ", ".join(self.tags) if self.tags else ""


def _build_image_content(media_path: str, caption: str = "") -> list[dict] | str:
    """Build an OpenAI vision content array from a local image file.

    Returns a plain placeholder string if the file cannot be read.
    """
    try:
        p = Path(media_path)
        if not p.is_absolute():
            # Resolve relative to project root
            p = Path(__file__).resolve().parent.parent / p
        if not p.exists():
            return caption or "[Imagem enviada pelo contato]"
        data = p.read_bytes()
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        b64 = base64.b64encode(data).decode()
        parts: list[dict] = [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
        if caption:
            parts.append({"type": "text", "text": caption})
        else:
            parts.append({"type": "text", "text": "O contato enviou esta imagem."})
        return parts
    except Exception:
        return caption or "[Imagem enviada pelo contato]"
