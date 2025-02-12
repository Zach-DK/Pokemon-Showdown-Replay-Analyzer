import requests
from bs4 import BeautifulSoup
import json
import re

def parse_who_field(who_field):
    """
    Given a string like "p2a: Volcarona", split it into (slot, nick).
    If no colon is present, return the whole string as slot and an empty nickname.
    """
    if ":" in who_field:
        slot, nick = who_field.split(":", 1)
        return slot.strip(), nick.strip()
    else:
        return who_field.strip(), ""

def normalize_species(s):
    """
    Remove trailing gender markers (e.g., ", M" or ", F") so that species are printed without them.
    """
    s = s.strip()
    return re.sub(r",[ ]*[MF]$", "", s)

def base_species(s):
    """
    Return the 'base' species name (without forme information) from a species string.
    """
    s = normalize_species(s)
    exceptions = {"Porygon-Z", "Ho-Oh"}
    if s in exceptions:
        return s
    if '-' in s:
        return s.split('-')[0]
    return s

def key_to_species(unique_key):
    """
    Given a unique key of the form "p2a (Dragonite, M)",
    extract the species portion and normalize it.
    """
    m = re.search(r"\((.*?)\)", unique_key)
    if m:
        return normalize_species(m.group(1))
    return normalize_species(unique_key)

def main():
    # Define explosion moves that should be treated specially.
    explosion_moves = {"Explosion", "Self Destruct", "Final Gambit", "Misty Explosion"}
    
    while True:
        replayURL = input("\nPlease enter the replay URL (or 'x' to exit): ")
        if replayURL.lower() == "x":
            break

        url = replayURL + '.json'
        response = requests.get(url)
        if not response.ok:
            print("Error: Could not retrieve replay. Please check the URL.\n")
            continue

        soup = BeautifulSoup(response.text, 'html.parser')
        try:
            data = json.loads(soup.text)
        except json.JSONDecodeError:
            print("Error: The response was not valid JSON. Check the replay URL.\n")
            continue

        # Extract player names
        p1_name = data["players"][0]
        p2_name = data["players"][1]

        log_text = data["log"]
        all_lines = log_text.split("\n")

        # Look for the win line
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

        # Build team rosters (order as indicated by the |poke| lines)
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

        # Build the nickname_map from switch, detailschange, and drag events.
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

        # Add Pokémon that never enter the field (from the |poke| lines)
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

        # Optional: Bail out if any disallowed Pokémon are present.
        disallowed = {"Zoroark", "Zoroark-Hisui", "Zorua", "Zorua-Hisui"}
        if any(normalize_species(species) in disallowed for species in nickname_map.values()):
            print("There's a Zoroark, this tool will not work")
            continue

        # Split log into turns
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

        # Tracking for kills and sources.
        # Each kill is a tuple: (inflictor_key, fainted_key, turn, cause_of_death)
        kills = []
        status_sources = {}   # e.g., {"psn": {victim_key: inflictor_key}, ...}
        ability_owners = {}   # e.g., {"Flame Body": "p2a (Volcarona)"}
        hazard_sources = {
            "Stealth Rock": {},
            "Spikes": {},
            "Toxic Spikes": {},
            "Sticky Web": {}
        }
        move_to_status = {
            # Burn moves
            "Beak Blast": "brn",
            "Blaze Kick": "brn",
            "Blazing Torque": "brn",
            "Blue Flare": "brn",
            "Burning Bulwark": "brn",
            "Burning Jealousy": "brn",
            "Ember": "brn",
            "Fire Blast": "brn",
            "Fire Fang": "brn",
            "Fire Punch": "brn",
            "Flame Wheel": "brn",
            "Flamethrower": "brn",
            "Flare Blitz": "brn",
            "Fling": "brn",  # Fling can burn depending on the held item
            "Heat Wave": "brn",
            "Ice Burn": "brn",
            "Infernal Parade": "brn",
            "Inferno": "brn",
            "Lava Plume": "brn",
            "Matcha Gotcha": "brn",
            "Psycho Shift": "brn",
            "Pyro Ball": "brn",
            "Sacred Fire": "brn",
            "Sandsear Storm": "brn",
            "Scald": "brn",
            "Scorching Sands": "brn",
            "Searing Shot": "brn",
            "Secret Power": "brn",
            "Shadow Fire": "brn",
            "Sizzly Slide": "brn",
            "Steam Eruption": "brn",
            "Tri Attack": "brn",
            "Will-O-Wisp": "brn",

            # Poison moves
            "Baneful Bunker": "psn",
            "Barb Barrage": "psn",
            "Cross Poison": "psn",
            "Dire Claw": "psn",
            "Fling": "psn",  # Fling can poison depending on the held item
            "G-Max Befuddle": "psn",
            "G-Max Malodor": "psn",
            "G-Max Stun Shock": "psn",
            "Gunk Shot": "psn",
            "Malignant Chain": "psn",
            "Mortal Spin": "psn",
            "Noxious Torque": "psn",
            "Poison Fang": "psn",
            "Poison Gas": "psn",
            "Poison Jab": "psn",
            "Poison Powder": "psn",
            "Poison Sting": "psn",
            "Poison Tail": "psn",
            "Psycho Shift": "psn",
            "Secret Power": "psn",
            "Shell Side Arm": "psn",
            "Sludge": "psn",
            "Sludge Bomb": "psn",
            "Sludge Wave": "psn",
            "Smog": "psn",
            "Toxic": "psn",
            "Toxic Spikes": "psn",
            "Toxic Thread": "psn",
            "Twineedle": "psn",

            # Paralysis moves
            "Thunder Wave": "par"
        }

        partial_trap_moves = {
            "Salt Cure", "Bind", "Clamp", "Fire Spin", "G-Max Centiferno",
            "G-Max Sandblast", "Infestation", "Magma Storm", "Sand Tomb",
            "Snap Trap", "Thunder Cage", "Whirlpool", "Wrap"
        }
        last_move_user = None
        last_move_name = None

        # Process each turn. At the start of each turn, clear last-move info.
        for tnum, turn_lines in turns:
            last_move_user = None
            last_move_name = None
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
                        last_move_user = f"{att_slot} ({att_nick})"
                        last_move_name = move_used
                        # Process hazard moves and trap moves as before.
                        if move_used in hazard_sources:
                            target_side = target_field.split(":")[0].replace("a", "").replace("b", "")
                            hazard_sources[move_used][target_side] = last_move_user
                        if move_used in partial_trap_moves:
                            tar_slot, tar_nick = parse_who_field(target_field)
                            victim_key = f"{tar_slot} ({tar_nick})"
                            if move_used not in status_sources:
                                status_sources[move_used] = {}
                            status_sources[move_used][victim_key] = last_move_user

                # (C) Status lines (tracking poison and other statuses)
                if line.startswith("|-status|"):
                    parts = line.split("|")
                    if len(parts) >= 4:
                        victim_field = parts[2]
                        inflicted_status = parts[3].strip()
                        # Normalize toxic to psn.
                        if inflicted_status == "tox":
                            inflicted_status = "psn"
                        vic_slot, vic_nick = parse_who_field(victim_field)
                        victim_key = f"{vic_slot} ({vic_nick})"
                        inflictor_key = None
                        # Look for a [from] clause (which may mention a move, ability, or a Pokémon).
                        from_match = re.search(r'\[from\]\s*(p[12][ab]?:\s*[^|]+|ability:\s*[^|]+|move:\s*[^|]+)', line)
                        if from_match:
                            from_text = from_match.group(1).strip()
                            if from_text.startswith("p") and ":" in from_text:
                                s, n = parse_who_field(from_text)
                                inflictor_key = f"{s} ({n})"
                            elif from_text.startswith("move:"):
                                # If a move is specified, assume the last move user is the attacker.
                                inflictor_key = last_move_user
                            elif from_text.startswith("ability:"):
                                ability_name = from_text.replace("ability:", "").strip()
                                inflictor_key = ability_owners.get(ability_name)
                        # If no inflictor was found and the last move is in move_to_status, use last_move_user.
                        if not inflictor_key and last_move_name in move_to_status:
                            if inflicted_status == move_to_status[last_move_name]:
                                inflictor_key = last_move_user
                        # For poison, record the source in status_sources["psn"].
                        if inflicted_status == "psn":
                            if "psn" not in status_sources:
                                status_sources["psn"] = {}
                            status_sources["psn"][victim_key] = inflictor_key
                # (D) Faint lines from damage events
                if line.startswith("|-damage|"):
                    parts = line.split("|")
                    if len(parts) >= 4:
                        hp_part = parts[3].strip()
                        if hp_part.startswith("0 fnt"):
                            fainted_field = parts[2].strip()
                            if ":" in fainted_field:
                                faint_slot, faint_nick = parse_who_field(fainted_field)
                                fainted_key = f"{faint_slot} ({faint_nick})"
                            else:
                                print(f"Warning: Skipping malformed |-damage| line: {line}")
                                continue
                            inflictor_key = None
                            cause_match = re.findall(r'\[from\]\s*([^\|]+)', line)
                            if cause_match:
                                cause_of_death = cause_match[0].strip()
                            else:
                                cause_of_death = "Direct Attack"
                            if cause_of_death == "tox":
                                cause_of_death = "psn"
                            if "item: Life Orb" in cause_of_death:
                                cause_of_death = "Life Orb"
                            for trap_move in partial_trap_moves:
                                if (trap_move in cause_of_death) or (f"move: {trap_move}" in cause_of_death):
                                    cause_of_death = trap_move
                                    break
                            # --- Explosion handling ---
                            if any(expl in cause_of_death for expl in explosion_moves):
                                # If the cause string already mentions an explosion move, use that.
                                for expl in explosion_moves:
                                    if expl in cause_of_death:
                                        cause_of_death = expl
                                        break
                                inflictor_key = None
                            elif cause_of_death == "Direct Attack" and last_move_name in explosion_moves:
                                explosion_move = last_move_name
                                explosion_attacker_slot = last_move_user.split(" ")[0] if last_move_user else None
                                cause_of_death = explosion_move
                                if explosion_attacker_slot and faint_slot != explosion_attacker_slot:
                                    inflictor_key = last_move_user
                                else:
                                    inflictor_key = None
                            elif cause_of_death == "Direct Attack":
                                inflictor_key = last_move_user
                            # --- Poison handling ---
                            # If the cause is psn (from toxic or tox) try to look up the source from status_sources.
                            if cause_of_death == "psn" and "psn" in status_sources:
                                source = status_sources["psn"].get(fainted_key)
                                if source:
                                    inflictor_key = source
                            # --- End Explosion/Poison handling ---
                            kills.append((inflictor_key, fainted_key, tnum, cause_of_death))
                # (E) Also process faint lines that begin with "|faint|"
                if line.startswith("|faint|"):
                    parts = line.split("|")
                    if len(parts) >= 2:
                        fainted_field = parts[1].strip()
                        if ":" in fainted_field:
                            faint_slot, faint_nick = parse_who_field(fainted_field)
                            fainted_key = f"{faint_slot} ({faint_nick})"
                        else:
                            print(f"Warning: Skipping malformed |faint| line: {line}")
                            continue
                        # For pure faint lines there is no [from] text.
                        if last_move_name in explosion_moves:
                            explosion_move = last_move_name
                            explosion_attacker_slot = last_move_user.split(" ")[0] if last_move_user else None
                            cause_of_death = explosion_move
                            if explosion_attacker_slot and faint_slot != explosion_attacker_slot:
                                inflictor_key = last_move_user
                            else:
                                inflictor_key = None
                        else:
                            cause_of_death = "Direct Attack"
                            inflictor_key = last_move_user
                        kills.append((inflictor_key, fainted_key, tnum, cause_of_death))
            # End of turn: Clear last move data so explosion/poison info does not leak to later turns.
            last_move_name = None
            last_move_user = None

        # Print Kill Feed with normalized species names
        print("\n==== KILL FEED ====")
        for attacker_key, victim_key, turn, cause in kills:
            victim_species = normalize_species(nickname_map.get(victim_key, key_to_species(victim_key)))
            if attacker_key:
                attacker_species = normalize_species(nickname_map.get(attacker_key, key_to_species(attacker_key)))
                print(f"Turn {turn}: {victim_species} fainted from {cause} inflicted by {attacker_species}.")
            else:
                print(f"Turn {turn}: {victim_species} fainted from {cause}.")

        # Build final stats from the nickname_map-based stats dictionary.
        stats = {}
        for unique_key, species in nickname_map.items():
            stats[unique_key] = {
                "species": normalize_species(species),
                "direct_kills": 0,
                "passive_kills": 0,
                "direct_deaths": 0,
                "passive_deaths": 0
            }
        # When aggregating, include all explosion moves in the set considered passive.
        passive_causes = {"Stealth Rock", "Spikes", "Toxic Spikes", "Sticky Web",
                          "brn", "psn", "Salt Cure", "Life Orb"} | explosion_moves | partial_trap_moves
        for attacker_key, victim_key, turn, cause in kills:
            kill_type = "passive" if cause in passive_causes else "direct"
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

        # Aggregate stats by team using full species names (with forme distinctions)
        species_stats_full = {}
        for unique_key, record in stats.items():
            team = unique_key.split("(")[0].strip()[:2]  # "p1" or "p2"
            species = record["species"]
            key = (team, species)
            if key not in species_stats_full:
                species_stats_full[key] = {"species": species, "direct_kills": 0, "passive_kills": 0,
                                           "direct_deaths": 0, "passive_deaths": 0}
            species_stats_full[key]["direct_kills"] += record["direct_kills"]
            species_stats_full[key]["passive_kills"] += record["passive_kills"]
            species_stats_full[key]["direct_deaths"] += record["direct_deaths"]
            species_stats_full[key]["passive_deaths"] += record["passive_deaths"]

        p1_species_stats_full = [v for k, v in species_stats_full.items() if k[0] == "p1"]
        p2_species_stats_full = [v for k, v in species_stats_full.items() if k[0] == "p2"]

        def find_team_slot(species, roster):
            normalized_roster = [normalize_species(x) for x in roster]
            try:
                return normalized_roster.index(normalize_species(species))
            except ValueError:
                return 999999

        p1_species_stats_full.sort(key=lambda rec: find_team_slot(rec["species"], p1_roster))
        p2_species_stats_full.sort(key=lambda rec: find_team_slot(rec["species"], p2_roster))

        # Aggregate stats by base species (forme labels removed)
        base_stats = {}
        for unique_key, record in stats.items():
            team = unique_key.split("(")[0].strip()[:2]
            base = base_species(record["species"])
            key = (team, base)
            if key not in base_stats:
                base_stats[key] = {"species": base, "direct_kills": 0, "passive_kills": 0,
                                   "direct_deaths": 0, "passive_deaths": 0}
            base_stats[key]["direct_kills"] += record["direct_kills"]
            base_stats[key]["passive_kills"] += record["passive_kills"]
            base_stats[key]["direct_deaths"] += record["direct_deaths"]
            base_stats[key]["passive_deaths"] += record["passive_deaths"]

        p1_base_stats = [v for k, v in base_stats.items() if k[0] == "p1"]
        p2_base_stats = [v for k, v in base_stats.items() if k[0] == "p2"]

        def find_team_slot_base(species, roster):
            normalized_roster = [base_species(normalize_species(x)) for x in roster]
            try:
                return normalized_roster.index(base_species(species))
            except ValueError:
                return 999999

        p1_base_stats.sort(key=lambda rec: find_team_slot_base(rec["species"], p1_roster))
        p2_base_stats.sort(key=lambda rec: find_team_slot_base(rec["species"], p2_roster))

        def print_table(title, species_list):
            print(f"\n=== {title} ===")
            header = f"{'Species':<30} {'DK':<2} {'PK':<2} {'DD':<2} {'PD':<2}"
            print(header)
            print("-" * len(header))
            for rec in species_list:
                print(f"{rec['species']:<30} {rec['direct_kills']:<2} {rec['passive_kills']:<2} {rec['direct_deaths']:<2} {rec['passive_deaths']:<2}")

        print_table(f"{p1_name}'s stats (Base Species)", p1_base_stats)
        print_table(f"{p2_name}'s stats (Base Species)", p2_base_stats)

        print(f"\nBattle lasted {battle_turns} turns.")
        print(f"Winner: {winner}.\n")

if __name__ == "__main__":
    main()
