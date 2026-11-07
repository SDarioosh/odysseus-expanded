import voluptuous as vol
from homeassistant import config_entries

class Flow(config_entries.ConfigFlow, domain="odysseus_sbx"):
    VERSION = 1
    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="Odysseus", data=user_input)
        return self.async_show_form(step_id="user", data_schema=vol.Schema(
            {vol.Required("base_url"): str, vol.Required("token"): str}))
