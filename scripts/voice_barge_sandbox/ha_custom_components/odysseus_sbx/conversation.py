import json, logging
import aiohttp
from homeassistant.components import conversation
from homeassistant.components.conversation import AssistantContent, async_get_result_from_chat_log
from homeassistant.const import MATCH_ALL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([OdysseusAgent(entry)])

class OdysseusAgent(conversation.ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = None
    def __init__(self, entry):
        self.entry = entry
        self._attr_unique_id = entry.entry_id
    @property
    def supported_languages(self):
        return MATCH_ALL
    async def _async_handle_message(self, user_input, chat_log):
        url = self.entry.data["base_url"].rstrip("/") + "/chat/completions"
        body = {"model": "odysseus", "stream": True, "user": chat_log.conversation_id,
                "messages": [{"role": "user", "content": user_input.text}]}
        text = ""
        _LOGGER.warning("agent: asking Odysseus %r", user_input.text)
        async with async_get_clientsession(self.hass).post(
                url, json=body, headers={"Authorization": "Bearer " + self.entry.data["token"]},
                timeout=aiohttp.ClientTimeout(total=300)) as res:
            async for raw in res.content:
                line = raw.decode().strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                for c in json.loads(line[6:]).get("choices") or []:
                    text += (c.get("delta") or {}).get("content") or ""
        _LOGGER.warning("agent: Odysseus answered %r", text)
        chat_log.async_add_assistant_content_without_tools(
            AssistantContent(agent_id=self.entity_id, content=text))
        return async_get_result_from_chat_log(user_input, chat_log)
