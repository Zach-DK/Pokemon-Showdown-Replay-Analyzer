import streamlit as st
import requests
from bs4 import BeautifulSoup
import json
import re

# --- Helper Functions ---

def parse_who_field(who_field):
    """
    Given something like "p2a: Volcarona",
    split it into (slot, nick).
    """
    slot, nick = who_field.split(":", 1)
    return slot.strip(), nick.strip()

def normalize_species(s):
    """
    Remove trailing gender markers (e.g., ", M" or ", F") so that species
    are printed without them.
    """
    s = s.strip()
    return re.sub(r",[ ]*[MF]$", "", s)

def key_to_species(unique_key):
    """
    Given a unique key of the form "p2a (Dragonite, M)",
    extract the species portion and normalize it.
    """
    m = re.search(r"\((.*?)\)", unique_key)
    if m:
        return normalize_species(m.group(1))
    return normalize_species(unique_key)

def format_stats_table(species_stats_list):
    """
    Given a list of species_stats (dictionaries), return a list of dictionaries
    suitable for st.table.
    """
    rows = []
    for rec in species_stats_list:
        rows.append({
            "Species": rec["species"],
            "Direct Kills": rec["direct_kills"],
            "Passive Kills": rec["passive_kills"],
            "Direct Deaths": rec["direct_deaths"],
            "Passive Deaths": rec["passive_deaths"]
        })
    return rows

# --- Replay Analysis Function ---

def analyze_replay(replayURL):
    # Get JSON data
    url = replayURL + '.json'
    response = requests.get(url)
    if not response.ok:
        return "Error: Could not retrieve replay. Please check the URL."
    soup = BeautifulSoup(response.text, 'html.parser')
    try:
        data = json.loads(soup.text)
    except json.JSONDecodeError:
        return "Error: The response was not valid JSON. Check the replay URL."

    # Extract player names
    p1_name = data["players"][0]
    p2_name = data["players"][1]

    log_text = data["log"]
    all_lines = log_text.split("\n")

    # Look for the win line (to get the winner)
    winner = None
    for line in all_lines:
        if line.startswith("|win|"):
            parts = line.split("|")
            if len(parts) >= 3:
                winner = parts[2]
            else:
                winner = parts[1]
            break
    if winner is None:
        winner = "Unknown"

    # Remove chat, join, leave lines
    log_lines = [line for line in all_lines
                 if not line.startswith("|c|") and not line.startswith("|j|") and not line.startswith("|l|")]

    # --- Build Team Rosters from |poke| Lines ---
    p1_roster = []
    p2_roster = []
    for line in log_lines:
        if line.startswith("|poke|p1|"):
            parts = line.split("|")
            if len(parts) >= 4:
                species = parts[3].strip()
                if species and species not in p1_roster:
                    p1_roster.append(species)
        elif line.startswith("|poke|p2|"):
            parts = line.split("|")
            if len(parts) >= 4:
                species = parts[3].strip()
                if species and species not in p2_roster:
                    p2_roster.append(species)

    # --- Build nickname_map from switch, detailschange, and drag events ---
    nickname_map = {}
    switch_regex = re.compile(r"^\|switch\|(p[12][ab]?)\: (.*?)\|(.*?)(?:\||\,|$)")
    detailschange_regex = re.compile(r"^\|detailschange\|(p[12][ab]?)\: (.*?)\|(.*)$")
    drag_regex = re.compile(r"^\|drag\|(p[12][ab]?)\: (.*?)\|(.*?)(?:\||$)")
    for line in log_lines:
        sw = switch_regex.match(line)
        if sw:
            slot, nick, species = sw.group(1), sw.group(2), sw.group(3).strip()
            nickname_map[f"{slot} ({nick})"] = species
        dc = detailschange_regex.match(line)
        if dc:
            slot, nick, species = dc.group(1), dc.group(2), dc.group(3).strip()
            nickname_map[f"{slot} ({nick})"] = species
        dr = drag_regex.match(line)
        if dr:
            slot, nick, species = dr.group(1), dr.group(2), dr.group(3).strip()
            nickname_map[f"{slot} ({nick})"] = species

    # --- Add Pokémon That Never Enter the Field ---
    p1_slots = ['p1a', 'p1b', 'p1c', 'p1d', 'p1e', 'p1f']
    p2_slots = ['p2a', 'p2b', 'p2c', 'p2d', 'p2e', 'p2f']
    for i, species in enumerate(p1_roster):
        key = f"{p1_slots[i]} ({species})"
        if key not in nickname_map:
            nickname_map[key] = species
    for i, species in enumerate(p2_roster):
        key = f"{p2_slots[i]} ({species})"
        if key not in nickname_map:
            nickname_map[key] = species

    # --- Split Log into Turns ---
    turns = []
    current_turn = []
    turn_number = 0
    for line in log_lines:
        if line.startswith("|turn|"):
            if current_turn:
                turns.append((turn_number, current_turn))
            current_turn = []
            try:
                turn_number = int(line.split("|")[2])
            except ValueError:
                turn_number = 0
        else:
            current_turn.append(line)
    if current_turn:
        turns.append((turn_number, current_turn))
    battle_turns = turns[-1][0] if turns else 0

    # --- Tracking Kills and Sources ---
    kills = []  # (inflictor_key, fainted_key, turn, cause_of_death)
    status_sources = {}   # e.g. {"psn": {victim_key: inflictor_key}, "Whirlpool": {...}, ...}
    ability_owners = {}   # e.g. {"Flame Body": "p2a (Volcarona)"}
    hazard_sources = {
        "Stealth Rock": {},
        "Spikes": {},
        "Toxic Spikes": {},
        "Sticky Web": {}
    }
    move_to_status = {
        "Will-O-Wisp": "brn",
        "Toxic": "psn",
        "Thunder Wave": "par"
    }
    partial_trap_moves = {
        "Salt Cure",
        "Bind",
        "Clamp",
        "Fire Spin",
        "G-Max Centiferno",
        "G-Max Sandblast",
        "Infestation",
        "Magma Storm",
        "Sand Tomb",
        "Snap Trap",
        "Thunder Cage",
        "Whirlpool",
        "Wrap"
    }
    last_move_user = None
    last_move_name = None

    for tnum, turn_lines in turns:
        for line in turn_lines:
            # (A) Ability detection
            if line.startswith("|-ability|"):
                parts = line.split("|")
                if len(parts) >= 4:
                    who_field = parts[2]
                    ability_name = parts[3].strip()
                    slot, nick = parse_who_field(who_field)
                    owner_key = f"{slot} ({nick})"
                    ability_owners[ability_name] = owner_key

            # (B) Moves used
            if line.startswith("|move|"):
                parts = line.split("|")
                if len(parts) >= 5:
                    attacker_field = parts[2]
                    move_used = parts[3].strip()
                    target_field = parts[4]
                    att_slot, att_nick = parse_who_field(attacker_field)
                    attacker_key = f"{att_slot} ({att_nick})"
                    last_move_user = attacker_key
                    last_move_name = move_used

                    if move_used in hazard_sources:
                        target_side = target_field.split(":")[0].replace("a", "").replace("b", "")
                        hazard_sources[move_used][target_side] = attacker_key

                    if move_used in partial_trap_moves:
                        tar_slot, tar_nick = parse_who_field(target_field)
                        victim_key = f"{tar_slot} ({tar_nick})"
                        if move_used not in status_sources:
                            status_sources[move_used] = {}
                        status_sources[move_used][victim_key] = attacker_key

            # (C) Status lines
            if line.startswith("|-status|"):
                parts = line.split("|")
                if len(parts) >= 4:
                    victim_field = parts[2]
                    inflicted_status = parts[3].strip()
                    if inflicted_status == "tox":
                        inflicted_status = "psn"
                    vic_slot, vic_nick = parse_who_field(victim_field)
                    victim_key = f"{vic_slot} ({vic_nick})"
                    inflictor_key = None
                    from_match = re.search(r'\[from\]\s*(p[12][ab]?:\s*[^|]+|ability:\s*[^|]+)', line)
                    if from_match:
                        from_text = from_match.group(1).strip()
                        if from_text.startswith("p") and ":" in from_text:
                            s, n = parse_who_field(from_text)
                            inflictor_key = f"{s} ({n})"
                        elif from_text.startswith("ability:"):
                            ability_name = from_text.replace("ability:", "").strip()
                            inflictor_key = ability_owners.get(ability_name)
                    if not inflictor_key and last_move_name in move_to_status:
                        if inflicted_status == move_to_status[last_move_name]:
                            inflictor_key = last_move_user
                    if inflictor_key:
                        if inflicted_status not in status_sources:
                            status_sources[inflicted_status] = {}
                        status_sources[inflicted_status][victim_key] = inflictor_key

            # (D) Faint lines
            if line.startswith("|-damage|"):
                parts = line.split("|")
                if len(parts) >= 4:
                    hp_part = parts[3].strip()
                    if hp_part.startswith("0 fnt"):
                        fainted_field = parts[2]
                        faint_slot, faint_nick = parse_who_field(fainted_field)
                        fainted_key = f"{faint_slot} ({faint_nick})"
                        cause_match = re.findall(r'\[from\]\s*([^\|]+)', line)
                        if cause_match:
                            cause_of_death = cause_match[0].strip()
                        else:
                            cause_of_death = "Direct Attack"
                        if cause_of_death == "tox":
                            cause_of_death = "psn"
                        if "item: Life Orb" in cause_of_death:
                            cause_of_death = "lifeorb"
                        for trap_move in partial_trap_moves:
                            if (trap_move in cause_of_death) or (f"move: {trap_move}" in cause_of_death):
                                cause_of_death = trap_move
                                break
                        inflictor_key = None
                        if cause_of_death in hazard_sources:
                            side = faint_slot[:-1]
                            inflictor_key = hazard_sources[cause_of_death].get(side)
                        elif cause_of_death in partial_trap_moves or cause_of_death in status_sources:
                            if cause_of_death in status_sources:
                                inflictor_key = status_sources[cause_of_death].get(fainted_key)
                        if not inflictor_key and cause_of_death == "Direct Attack":
                            inflictor_key = last_move_user
                        kills.append((inflictor_key, fainted_key, tnum, cause_of_death))

    # Prepare kill feed text.
    kill_feed = "==== KILL FEED ====\n"
    for attacker_key, victim_key, turn, cause in kills:
        victim_species = normalize_species(nickname_map.get(victim_key, key_to_species(victim_key)))
        if attacker_key:
            attacker_species = normalize_species(nickname_map.get(attacker_key, key_to_species(attacker_key)))
            kill_feed += f"Turn {turn}: {victim_species} fainted from {cause} inflicted by {attacker_species}.\n"
        else:
            kill_feed += f"Turn {turn}: {victim_species} fainted from {cause}.\n"

    battle_info = f"\nBattle lasted {battle_turns} turns.\nWinner: {winner}.\n"

    final_output = kill_feed + battle_info

    # Build stats from nickname_map-based stats.
    stats = {}
    for unique_key, species in nickname_map.items():
        stats[unique_key] = {
            "species": normalize_species(species),
            "direct_kills": 0,
            "passive_kills": 0,
            "direct_deaths": 0,
            "passive_deaths": 0
        }
    for attacker_key, victim_key, turn, cause in kills:
        # Passive if cause is in the union of standard passive causes and our partial trap moves.
        passive_set = {"Stealth Rock", "Spikes", "Toxic Spikes", "Sticky Web", "brn", "psn", "Salt Cure", "lifeorb"} | partial_trap_moves
        kill_type = "passive" if cause in passive_set else "direct"
        if attacker_key:
            if attacker_key not in stats:
                stats[attacker_key] = {
                    "species": normalize_species(nickname_map.get(attacker_key, key_to_species(attacker_key))),
                    "direct_kills": 0,
                    "passive_kills": 0,
                    "direct_deaths": 0,
                    "passive_deaths": 0
                }
            stats[attacker_key][f"{kill_type}_kills"] += 1
        if victim_key not in stats:
            stats[victim_key] = {
                "species": normalize_species(nickname_map.get(victim_key, key_to_species(victim_key))),
                "direct_kills": 0,
                "passive_kills": 0,
                "direct_deaths": 0,
                "passive_deaths": 0
            }
        stats[victim_key][f"{kill_type}_deaths"] += 1

    # Aggregate stats by team and normalized species.
    species_stats = {}
    for unique_key, record in stats.items():
        team = unique_key.split("(")[0].strip()[:2]  # "p1" or "p2"
        species = record["species"]
        key = (team, species)
        if key not in species_stats:
            species_stats[key] = {"species": species, "direct_kills": 0, "passive_kills": 0, "direct_deaths": 0, "passive_deaths": 0}
        species_stats[key]["direct_kills"] += record["direct_kills"]
        species_stats[key]["passive_kills"] += record["passive_kills"]
        species_stats[key]["direct_deaths"] += record["direct_deaths"]
        species_stats[key]["passive_deaths"] += record["passive_deaths"]

    p1_species_stats = [v for k, v in species_stats.items() if k[0] == "p1"]
    p2_species_stats = [v for k, v in species_stats.items() if k[0] == "p2"]

    def find_team_slot(species, roster):
        normalized_roster = [normalize_species(x) for x in roster]
        try:
            return normalized_roster.index(normalize_species(species))
        except ValueError:
            return 999999

    p1_species_stats.sort(key=lambda rec: find_team_slot(rec["species"], p1_roster))
    p2_species_stats.sort(key=lambda rec: find_team_slot(rec["species"], p2_roster))

    p1_table = []
    p2_table = []
    for rec in p1_species_stats:
        p1_table.append({
            "Species": rec["species"],
            "Direct Kills": rec["direct_kills"],
            "Passive Kills": rec["passive_kills"],
            "Direct Deaths": rec["direct_deaths"],
            "Passive Deaths": rec["passive_deaths"]
        })
    for rec in p2_species_stats:
        p2_table.append({
            "Species": rec["species"],
            "Direct Kills": rec["direct_kills"],
            "Passive Kills": rec["passive_kills"],
            "Direct Deaths": rec["direct_deaths"],
            "Passive Deaths": rec["passive_deaths"]
        })

    return final_output, p1_table, p2_table, p1_name, p2_name

# --- Streamlit App ---

st.title("Pokémon Showdown Replay Analyzer")
st.markdown("Enter a Pokémon Showdown replay URL below and click **Analyze**.")

replay_url = st.text_input("Replay URL", "")

if st.button("Analyze"):
    if replay_url.strip() == "":
        st.error("Please enter a valid replay URL.")
    else:
        result = analyze_replay(replay_url.strip())
        if isinstance(result, str):
            st.error(result)
        else:
            final_output, p1_table, p2_table, p1_name, p2_name = result
            st.subheader("Battle Summary")
            st.text(final_output)
            st.subheader(f"{p1_name}'s Stats")
            st.table(p1_table)
            st.subheader(f"{p2_name}'s Stats")
            st.table(p2_table)
