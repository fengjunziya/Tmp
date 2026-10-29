"""Small immutable gate for equivalent requirement labels and audio coverage."""
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import timeline
import verify


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


assets = read("runs/deepseek_video/assets.json")
req = read("runs/deepseek_video/requirements_v1.json")
t = read("runs/demo/timeline_v1.json")
t.update(asset_manifest_hash=assets["manifest_hash"], request_ids=req["request_ids"],
         constraint_set_id=req["constraint_set_id"])
_, derived = timeline.check(t, assets, req)
assert derived["computed_duration_ms"] == 10000
bad = copy.deepcopy(t)
bad["clips"][0]["transition_out"] = {"type": "cut", "duration_ms": 0}
try:
    timeline.check(bad, assets, req)
except ValueError:
    pass
else:
    raise AssertionError("Alias support must still reject a real transition mismatch")
probe = {"streams": [{"codec_type": "audio", "duration": "10.0", "start_time": "-5.0"}],
         "format": {"duration": "10.0"}}
assert not verify.media_duration_checks(probe, 10000, 30)["audio_ok"]
print("SEMANTIC_ALIASES_AND_AUDIO_OFFSET_OK")
