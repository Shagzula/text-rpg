-- Text RPG engine schema (v3). All authoritative game state lives here.
-- Extends the starter rpg_engine.py schema: same table/column names where they
-- existed, plus FKs, CHECKs, JSON columns and the tables the design doc needs.
-- JSON columns are TEXT validated with json_valid(). Foreign keys are enforced
-- (PRAGMA foreign_keys=ON is set on every connection by rpg.db).
-- Content tables (factions, locations, npcs, item_templates, items) have an
-- INTEGER primary key plus a unique TEXT `key` slug. Content files and LLM
-- contracts refer to entities by `key` (e.g. 'item_short_blade', 'npc_mara').
-- Exits are addressed by their destination location key.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS factions (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT,
    traits      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(traits))
);

CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT,
    area_tier   INTEGER NOT NULL DEFAULT 1 CHECK (area_tier >= 1),   -- power tier
    dark        INTEGER NOT NULL DEFAULT 0 CHECK (dark IN (0, 1)),   -- needs a lit light source to see
    parent_id   INTEGER REFERENCES locations(id) ON DELETE SET NULL, -- e.g. room -> dungeon
    faction_id  INTEGER REFERENCES factions(id) ON DELETE SET NULL
);

-- Directed connections between locations.
CREATE TABLE IF NOT EXISTS location_exits (
    from_location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    to_location_id   INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    direction        TEXT NOT NULL,      -- 'north', 'cellar door', ...
    description      TEXT,
    locked           INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    unlock_dc        INTEGER,            -- DC to force/pick, if any
    PRIMARY KEY (from_location_id, direction)
);

CREATE TABLE IF NOT EXISTS characters (
    id          INTEGER PRIMARY KEY,
    key         TEXT UNIQUE,                   -- optional slug, e.g. 'pc_test_rogue'
    name        TEXT NOT NULL,
    is_player   INTEGER NOT NULL DEFAULT 1 CHECK (is_player IN (0, 1)),
    level       INTEGER NOT NULL DEFAULT 1  CHECK (level BETWEEN 1 AND 20),
    str_score   INTEGER NOT NULL DEFAULT 10 CHECK (str_score BETWEEN 1 AND 30),
    dex_score   INTEGER NOT NULL DEFAULT 10 CHECK (dex_score BETWEEN 1 AND 30),
    con_score   INTEGER NOT NULL DEFAULT 10 CHECK (con_score BETWEEN 1 AND 30),
    int_score   INTEGER NOT NULL DEFAULT 10 CHECK (int_score BETWEEN 1 AND 30),
    wis_score   INTEGER NOT NULL DEFAULT 10 CHECK (wis_score BETWEEN 1 AND 30),
    cha_score   INTEGER NOT NULL DEFAULT 10 CHECK (cha_score BETWEEN 1 AND 30),
    max_hp      INTEGER NOT NULL CHECK (max_hp > 0),
    current_hp  INTEGER NOT NULL,
    location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    -- weapon proficiency categories ('simple', 'martial') and/or template keys
    weapon_proficiencies TEXT NOT NULL DEFAULT '["simple"]' CHECK (json_valid(weapon_proficiencies)),
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS character_skills (
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    skill        TEXT NOT NULL,
    proficient   INTEGER NOT NULL DEFAULT 0 CHECK (proficient IN (0, 1)),
    expertise    INTEGER NOT NULL DEFAULT 0 CHECK (expertise IN (0, 1)),
    PRIMARY KEY (character_id, skill)
);

-- Conditions on a character. effect JSON, e.g.
--   {"all_checks": -1, "abilities": {"str": -2}, "skills": {"athletics": -1}}
-- lasting=1: persists until explicitly removed (rest/healing/quest).
-- lasting=0: duration_turns counts down (NULL = until removed by the engine).
CREATE TABLE IF NOT EXISTS character_conditions (
    id             INTEGER PRIMARY KEY,
    character_id   INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    description    TEXT,
    severity       INTEGER NOT NULL DEFAULT 1,
    effect         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(effect)),
    lasting        INTEGER NOT NULL DEFAULT 0 CHECK (lasting IN (0, 1)),
    duration_turns INTEGER,
    source         TEXT,
    expires_on     TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Engine-owned item templates. `key` is the stable slug the LLM must map onto
-- (YAML `id`). subcategory and other category-specific data go in stats JSON.
CREATE TABLE IF NOT EXISTS item_templates (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,           -- 'item_short_blade'
    name        TEXT NOT NULL,
    category    TEXT NOT NULL CHECK (category IN
                  ('weapon', 'armor', 'shield', 'ammo', 'consumable', 'tool', 'utility', 'misc')),
    power_tier  INTEGER NOT NULL DEFAULT 1 CHECK (power_tier >= 1),
    slot        TEXT NOT NULL DEFAULT 'none' CHECK (slot IN
                  ('main_hand', 'off_hand', 'two_hands', 'body', 'head', 'hands', 'feet', 'belt', 'none')),
    weight      REAL NOT NULL DEFAULT 0 CHECK (weight >= 0),     -- lb-equivalent
    value       INTEGER NOT NULL DEFAULT 0 CHECK (value >= 0),   -- base currency units
    stats       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(stats)),
    tags        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags)),
    description TEXT
);

-- Item instances. Exactly one owner: a character (inventory) or a location
-- (lying in the scene, can be taken).
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY,
    key          TEXT NOT NULL UNIQUE,          -- 'item_short_blade', then 'item_short_blade_2', ...
    template_id  INTEGER NOT NULL REFERENCES item_templates(id),
    character_id INTEGER REFERENCES characters(id) ON DELETE CASCADE,
    location_id  INTEGER REFERENCES locations(id) ON DELETE CASCADE,
    display_name TEXT,                          -- custom name, e.g. "Grandpa's rusty shovel"
    quantity     INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    equipped     INTEGER NOT NULL DEFAULT 0 CHECK (equipped IN (0, 1)),
    uses_left    INTEGER,                       -- NULL = full (template stats.uses)
    timer        INTEGER,                       -- turns left on a timed state (e.g. a lit light)
    state        TEXT,                          -- free-form entity state: 'sealed', 'open', 'struck'
    out_of_reach INTEGER NOT NULL DEFAULT 0 CHECK (out_of_reach IN (0, 1)),
    notes        TEXT,
    acquired_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    CHECK ((character_id IS NULL) != (location_id IS NULL)),
    CHECK (equipped = 0 OR character_id IS NOT NULL)
);

-- Starter-compatible name: items carried by characters.
CREATE VIEW IF NOT EXISTS inventory AS
    SELECT * FROM items WHERE character_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS npcs (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT,
    location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    faction_id  INTEGER REFERENCES factions(id) ON DELETE SET NULL,
    traits      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(traits)),
    disposition INTEGER NOT NULL DEFAULT 0 CHECK (disposition BETWEEN -100 AND 100),
    defense_dc  INTEGER NOT NULL DEFAULT 10,    -- attack DC (AC-like)
    attack_bonus INTEGER NOT NULL DEFAULT 2,    -- its d20 bonus when attacking
    damage      TEXT NOT NULL DEFAULT '1d4',    -- its damage dice
    save_bonus  INTEGER NOT NULL DEFAULT 0,     -- its d20 bonus on saves
    max_hp      INTEGER NOT NULL DEFAULT 4 CHECK (max_hp > 0),
    current_hp  INTEGER,                        -- NULL = max_hp
    out_of_reach INTEGER NOT NULL DEFAULT 0 CHECK (out_of_reach IN (0, 1)),
    is_major    INTEGER NOT NULL DEFAULT 0 CHECK (is_major IN (0, 1)),
    alive       INTEGER NOT NULL DEFAULT 1 CHECK (alive IN (0, 1)),
    notes       TEXT
);

-- Player <-> NPC/faction standing (kept from starter).
CREATE TABLE IF NOT EXISTS relationships (
    id           INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    target_type  TEXT NOT NULL CHECK (target_type IN ('npc', 'faction')),
    target_id    INTEGER NOT NULL,
    value        INTEGER NOT NULL DEFAULT 0,
    notes        TEXT
);

-- Key/value world flags (quest state, events). value is JSON.
CREATE TABLE IF NOT EXISTS world_flags (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL CHECK (json_valid(value)),
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Save metadata. One SQLite file = one campaign; this row describes it.
CREATE TABLE IF NOT EXISTS save_games (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    character_id INTEGER REFERENCES characters(id) ON DELETE SET NULL,
    turn         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Conditions on NPCs (blinded by flash powder, ...). duration_turns NULL = until removed.
CREATE TABLE IF NOT EXISTS npc_conditions (
    id             INTEGER PRIMARY KEY,
    npc_id         INTEGER NOT NULL REFERENCES npcs(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    effect         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(effect)),
    duration_turns INTEGER,
    source         TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Narrator output per turn, for the {recent_narrations} context block.
CREATE TABLE IF NOT EXISTS narration_log (
    id           INTEGER PRIMARY KEY,
    character_id INTEGER REFERENCES characters(id) ON DELETE CASCADE,
    turn         INTEGER NOT NULL,
    text         TEXT NOT NULL,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_items_character     ON items(character_id);
CREATE INDEX IF NOT EXISTS idx_items_location      ON items(location_id);
CREATE INDEX IF NOT EXISTS idx_conditions_character ON character_conditions(character_id);
CREATE INDEX IF NOT EXISTS idx_npcs_location        ON npcs(location_id);
