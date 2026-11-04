"""Sandbox stand-ins for two integrations the owner runs (not installed here):

- Extended OpenAI Conversation: a conversation agent that asks Odysseus's voice endpoint
  (OpenAI chat completions, streamed) and waits for the whole answer, as that one does.
- VACA: the Wyoming satellite also sends "pipeline-ended" to the device on every run's
  RUN_END (custom_components/vaca/assist_satellite.py, on_pipeline_event). Done here by
  wrapping the core Wyoming satellite's handler.
"""
import logging
from homeassistant.components import assist_pipeline
from homeassistant.components.wyoming import assist_satellite as wy
from wyoming.event import Event

_LOGGER = logging.getLogger(__name__)
DOMAIN = "odysseus_sbx"

_orig = wy.WyomingAssistSatellite.on_pipeline_event


def _on_pipeline_event(self, event):
    if event.type == assist_pipeline.PipelineEventType.RUN_END and self._client is not None:
        self.config_entry.async_create_background_task(
            self.hass, self._client.write_event(Event(type="pipeline-ended")), "pipeline ended")
    _orig(self, event)


wy.WyomingAssistSatellite.on_pipeline_event = _on_pipeline_event


async def async_setup_entry(hass, entry):
    await hass.config_entries.async_forward_entry_setups(entry, ["conversation"])
    return True


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, ["conversation"])
