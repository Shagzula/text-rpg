"""NPC personas (content/npc_persona_schema.md): load + validate YAML, store in
npc_personas, and derive what a band allows. Secrets and insight_tell stay in here;
only `persona_view`, `visible_topics` and `available_trades` are prompt-facing."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import rules, yamlish

BANDS = ("hostile", "unfriendly", "neutral", "friendly", "allied")
BAND_DC_MOD = {"hostile": 5, "unfriendly": 2, "neutral": 0, "friendly": -2, "allied": -5}
DEFAULT_BASE_DC = 12
DEMO_PERSONA_DIR = Path(__file__).resolve().parent / "data" / "npcs"
# Coin trades (engine extension: {gold: N} in gives/wants) are priced by band.
BUY_PRICE_MULT = {"hostile": 1.5, "unfriendly": 1.25, "neutral": 1.0, "friendly": 0.9, "allied": 0.75}
SELL_PRICE_MULT = {"hostile": 0.5, "unfriendly": 0.75, "neutral": 1.0, "friendly": 1.1, "allied": 1.25}


COMBAT_FIELDS = ("defense_dc", "max_hp", "attack_bonus", "damage", "save_bonus")  # -> npcs columns


class PersonaError(ValueError):
    pass


def band_index(band: str) -> int:
    return BANDS.index(band)


def shift_band(band: str, steps: int) -> str:
    return BANDS[max(0, min(len(BANDS) - 1, band_index(band) + steps))]


# ---------------------------------------------------------------------------
# Validation (schema doc: "Load-time validation")
# ---------------------------------------------------------------------------

def validate(p: Dict, template_keys: Iterable[str]) -> Dict:
    keys = set(template_keys)
    where = p.get("key", "?")

    def bad(msg):
        raise PersonaError(f"{where}: {msg}")

    for f in ("key", "name", "voice", "traits", "goals", "base_disposition", "knowledge", "reveal_by_band"):
        if p.get(f) is None:
            bad(f"missing required field {f!r}")
    if not str(p["key"]).startswith("npc_"):
        bad("key must be npc_<slug>")
    v = p["voice"]
    if not isinstance(v, dict) or not v.get("style") or not isinstance(v.get("sample_phrases"), list):
        bad("voice needs style and sample_phrases")
    ds = p.get("dark_sound")
    if ds is not None and (not isinstance(ds, str) or not ds.strip()):
        bad("dark_sound must be a non-empty string")  # v0.7.1: what you hear of them in the dark
    bd = p["base_disposition"]
    if not isinstance(bd, int) or not -100 <= bd <= 100:
        bad("base_disposition must be an int in -100..100")
    knowledge, secrets = p.get("knowledge") or {}, p.get("secrets") or {}
    for k, t in {**knowledge, **secrets}.items():
        if not isinstance(t, dict) or not t.get("label") or not t.get("text"):
            bad(f"topic {k!r} needs label and text")
    for k, t in {**knowledge, **secrets, **(p.get("trades") or {})}.items():
        al = t.get("aliases") if isinstance(t, dict) else None
        if al is not None and (not isinstance(al, list) or not all(isinstance(x, str) and x.strip() for x in al)):
            bad(f"{k!r} aliases must be a list of non-empty strings")
    if set(knowledge) & set(secrets):
        bad("knowledge and secrets share one key namespace")
    for k, s in secrets.items():
        if s.get("min_band") not in BANDS + ("never",):
            bad(f"secret {k!r} min_band must be a band or 'never'")
    rbb = p["reveal_by_band"]
    if not isinstance(rbb, dict) or set(rbb) != set(BANDS):
        bad("reveal_by_band needs exactly the five bands")
    combat = p.get("combat")
    if combat is not None:
        if not isinstance(combat, dict) or set(combat) - set(COMBAT_FIELDS):
            bad(f"combat accepts only {list(COMBAT_FIELDS)}")
        for f, v in combat.items():
            if f == "damage":
                try:
                    rules.roll_dice(v)
                except ValueError:
                    bad(f"combat.damage {v!r} is not a dice expression")
            elif not isinstance(v, int) or (f in ("max_hp", "defense_dc") and v <= 0):
                bad(f"combat.{f} must be an int" + (" > 0" if f in ("max_hp", "defense_dc") else ""))
    gifts = p.get("gifts")
    if gifts is not None:  # v0.6.1 engine extension: {template key: disposition gain 1..50}
        if not isinstance(gifts, dict):
            bad("gifts must be a map of item template key -> int")
        for k, v in (gifts or {}).items():
            if k not in keys:
                bad(f"gifts lists unknown item template {k!r}")
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 50:
                bad(f"gifts.{k} must be an int 1..50")
    trades = p.get("trades") or {}
    for band, entry in rbb.items():
        entry = entry or {}
        for topic in entry.get("topics") or []:
            if topic not in knowledge and topic not in secrets:
                bad(f"reveal_by_band.{band} lists unknown topic {topic!r}")
            if topic in secrets:
                mb = secrets[topic]["min_band"]
                if mb == "never":
                    bad(f"secret {topic!r} is 'never' but listed at {band}")
                if band_index(band) < band_index(mb):
                    bad(f"secret {topic!r} listed at {band}, below its min_band {mb}")
        for tr in entry.get("trades") or []:
            if tr not in trades:
                bad(f"reveal_by_band.{band} lists unknown trade {tr!r}")
    for tk, tr in trades.items():
        if not tr.get("label"):
            bad(f"trade {tk!r} needs a label")
        for side in ("gives", "wants"):
            for e in tr.get(side) or []:
                if "gold" in e:
                    if not isinstance(e["gold"], int) or e["gold"] <= 0:
                        bad(f"trade {tk!r} {side}: gold must be a positive int")
                elif e.get("item") not in keys:
                    bad(f"trade {tk!r} {side}: unknown item template {e.get('item')!r}")
                elif not isinstance(e.get("qty", 1), int) or e.get("qty", 1) < 1:
                    bad(f"trade {tk!r} {side}: qty must be a positive int")
                elif e.get("name") is not None and (side != "gives" or any(ch.isdigit() for ch in str(e["name"]))):
                    bad(f"trade {tk!r} {side}: optional name (gives only) must be digit-free")
    return p


def load_file(path, template_keys) -> Dict:
    return validate(yamlish.load(path), template_keys)


def install(conn, persona: Dict) -> int:
    """Join a validated persona to its npcs row by key: set name/description/traits, the
    base disposition and any `combat` stats, and store the full persona (engine-only) in npc_personas."""
    row = conn.execute("SELECT id FROM npcs WHERE key = ?", (persona["key"],)).fetchone()
    if row is None:
        raise PersonaError(f"{persona['key']}: no npcs row with that key")
    conn.execute("UPDATE npcs SET name = ?, description = COALESCE(?, description), traits = ?, disposition = ? "
                 "WHERE id = ?", (persona["name"], persona.get("description"), json.dumps(persona["traits"]),
                                  persona["base_disposition"], row["id"]))
    combat = persona.get("combat") or {}
    for f in COMBAT_FIELDS:  # optional combat block overrides the seeded defaults
        if f in combat:
            conn.execute(f"UPDATE npcs SET {f} = ? WHERE id = ?", (combat[f], row["id"]))
    if "max_hp" in combat:
        conn.execute("UPDATE npcs SET current_hp = NULL WHERE id = ?", (row["id"],))  # NULL = full
    conn.execute("INSERT OR REPLACE INTO npc_personas (npc_id, persona) VALUES (?, ?)",
                 (row["id"], json.dumps(persona)))
    return row["id"]


def install_dir(conn, directory=DEMO_PERSONA_DIR) -> List[str]:
    keys = [r[0] for r in conn.execute("SELECT key FROM item_templates")]
    out = []
    for f in sorted(Path(directory).glob("npc_*.yaml")):
        p = load_file(f, keys)
        if conn.execute("SELECT 1 FROM npcs WHERE key = ?", (p["key"],)).fetchone():
            install(conn, p)
            out.append(p["key"])
    return out


def get(conn, npc_id) -> Optional[Dict]:
    row = conn.execute("SELECT persona FROM npc_personas WHERE npc_id = ?", (npc_id,)).fetchone()
    return json.loads(row[0]) if row else None


# ---------------------------------------------------------------------------
# Derived at runtime
# ---------------------------------------------------------------------------

def _upto(p: Dict, band: str, field: str) -> List[str]:
    out: List[str] = []
    for b in BANDS[: band_index(band) + 1]:
        for k in ((p["reveal_by_band"].get(b) or {}).get(field) or []):
            if k not in out:
                out.append(k)
    return out


def available_topics(p: Dict, band: str) -> List[str]:
    return _upto(p, band, "topics")


def available_trades(p: Dict, band: str) -> List[str]:
    return _upto(p, band, "trades")


def topic(p: Dict, key: str) -> Optional[Dict]:
    return (p.get("knowledge") or {}).get(key) or (p.get("secrets") or {}).get(key)


def is_secret(p: Dict, key: str) -> bool:
    return key in (p.get("secrets") or {})


def topic_visible(p: Dict, key: str, band: str) -> bool:
    """Shown in context `topics`: every public topic, and secrets whose min_band <= band."""
    if key in (p.get("knowledge") or {}):
        return True
    s = (p.get("secrets") or {}).get(key)
    return bool(s) and s["min_band"] != "never" and band_index(band) >= band_index(s["min_band"])


def visible_topics(p: Optional[Dict], band: str) -> List[Dict]:
    if not p:
        return []
    keys = list(p.get("knowledge") or {}) + list(p.get("secrets") or {})
    out = []
    for k in keys:
        if topic_visible(p, k, band):
            e = {"key": k, "label": topic(p, k)["label"]}
            if topic(p, k).get("aliases"):
                e["aliases"] = list(topic(p, k)["aliases"])
            out.append(e)
    return out


def social_dc(p: Dict, skill: str, band: str) -> int:
    soc = p.get("social") or {}
    return int(soc.get("base_dc", DEFAULT_BASE_DC)) + int(soc.get(skill, 0) or 0) + BAND_DC_MOD[band]


def persona_view(p: Dict) -> Dict:
    """Public persona for the voice prompt: no secrets, no insight_tell, no knowledge text."""
    return {"key": p["key"], "name": p["name"],
            "voice": {"style": p["voice"]["style"], "sample_phrases": list(p["voice"]["sample_phrases"])},
            "traits": list(p["traits"]), "goals": list(p["goals"])}


def secret_texts(p: Optional[Dict]) -> List[str]:
    if not p:
        return []
    return [s["text"] for s in (p.get("secrets") or {}).values()]


def band_of(disposition: int) -> str:
    return rules.disposition_band(disposition)
