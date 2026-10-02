"""Start the live cockpit on the remote Home Assistant workstation."""

import os
import runpy
from pathlib import Path


ROOT = Path(r"C:\Users\piste\SmartHome_project")
values = [
    item.strip()
    for item in (ROOT / "Tokens.txt").read_text(encoding="utf-8").splitlines()
    if item.strip()
]
if len(values) < 2:
    raise RuntimeError("Tokens.txt muss Home-Assistant-Token und Bridge-Secret enthalten.")

os.environ.update(
    HOME_ASSISTANT_URL="https://ha.pistelok.de",
    HOME_ASSISTANT_TOKEN=values[0],
    HOME_ASSISTANT_ALLOW_WRITES="true",
    # The bridge is published through the authenticated Cloudflare route;
    # the Windows cockpit cannot resolve the add-on's internal hostname.
    SPOTIFY_BRIDGE_URL="https://spotify.pistelok.de",
    SPOTIFY_BRIDGE_SECRET=values[1],
    COCKPIT_HOST="0.0.0.0",
    COCKPIT_PORT="8767",
)
os.environ["HOME_ASSISTANT_ENTITIES_JSON"] = (
    '{"wohnzimmerlicht":"light.h618c",'
    '"schlafzimmerlicht":"light.h618c_2",'
    '"wohnzimmerheizung":"climate.wohnzimmer_wohnung_wohnzimmer",'
    '"schlafzimmerheizung":"climate.schlafzimmer_wohnung_schlafzimmer",'
    '"kuechenheizung":"climate.kuche_wohnung_kuche",'
    '"badezimmerheizung":"climate.badezimmer_wohnung_badezimmer",'
    '"bueroheizung":"climate.buro_wohnung_buro"}'
)
os.environ["HOME_ASSISTANT_ALEXA_DEVICES_JSON"] = (
    '{"media_player.andreas_echo_show":"06f25968892533eb622b790c7ee78654",'
    '"media_player.echo_spot_schlafzimmer":"7b1a4f89137989fe076343201c8a35b9",'
    '"media_player.echo_dot_kuche":"ef2d7aa097170950d87494f76a3c80ec",'
    '"media_player.echo_dot_badezimmer":"e786f736efe4362dd7fb0b6840976f05",'
    '"media_player.echo_dot_buro":"872894fd47a53de17ed0c398112c826c"}'
)
os.chdir(ROOT)
runpy.run_path("dashboard/server.py", run_name="__main__")
