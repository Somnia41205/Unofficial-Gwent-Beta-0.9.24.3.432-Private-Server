#!/usr/bin/env python3
"""
Gwent Beta WebSocket relay + lobby server.

Both clients connect here. The relay:
1. Receives both PlayerAuthenticates messages (TypeID=0xff, CommandID=0x00)
2. Parses each client's ServiceId from the join payload
3. Sends SetupPlayers (TypeID=0xff, CommandID=0x01) to each client with correct
   PlayerID assignment and the PlayerIDs dict containing both players' ServiceIds
4. Forwards all subsequent game messages bidirectionally, rewriting BundleIDs
   so each client sees a monotonically-increasing sequence from "the server"
5. Responds to application-level pings (ff15) with pongs (ff16)
6. Intercepts PlayerInitialized (CommandID=0x02) from each client; when both
   received, sends BroadcastEvent(PlayerReady) then BroadcastEvent(LobbyInitialized)
7. Intercepts PlayerTimeReq (CommandID=0x05) from each client and replies with
   CurrentTimeRes (CommandID=0x06) directly — not forwarded to the peer
8. Sends ConfirmReceivedMessages ACKs for all received game messages

RedBundle wire format: [TypeID][CommandID][Target][TargetPlayerID][BundleID LE32][Payload...]
  TypeID=0xff for all internal commands
  TypeID=0x00 for game commands (GwentCommand)
  Application-level ping: ff 15 00 ff [seq LE32]  (8 bytes)
  Application-level pong: ff 16 00 ff [seq LE32]  (8 bytes)

SetupPlayers payload:
  int   PlayerID            (4-byte LE)
  int   dict_count          (4-byte LE)
  for each entry:
    int   key  (PlayerID)   (4-byte LE)
    ulong value (ServiceId) (8-byte LE)
  string SessionID          (7-bit-encoded length prefix + UTF-8; empty = 0x00)

PlayerTimeReq payload:  double SendTime              (8 bytes)
CurrentTimeRes payload: double RequestSendTime       (8 bytes)
                        double RequestReceiveTime    (8 bytes)
                        double ResponseSendTime      (8 bytes)

BroadcastEvent payload: short  EventType            (2 bytes)
                        int    PlayerID              (4 bytes)
                        short  Status               (2 bytes)
"""
import asyncio
import json
import logging
import os
import socket as _socket
import struct
import sys
import tempfile
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RELAY] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("relay")

try:
    import websockets
    from websockets.server import serve
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets"])
    import websockets
    from websockets.server import serve

HOST = "0.0.0.0"
PORT = 7777

FRAME_PING  = 0x15
FRAME_PONG  = 0x16
MAGIC       = 0xff
TYPE_INTERNAL = 0xff
TYPE_GAME     = 0x00  # GwentCommand TypeID

CMD_PLAYER_AUTHENTICATES    = 0x00
CMD_SETUP_PLAYERS           = 0x01
CMD_PLAYER_INITIALIZED      = 0x02
CMD_PLAYER_TIME_REQ         = 0x05
CMD_CURRENT_TIME_RES        = 0x06
CMD_BROADCAST_EVENT         = 0x08
CMD_CONFIRM_RECEIVED        = 0x14  # ConfirmReceivedMessages (20)
CMD_GAME_FINISHED           = 0x03  # GameFinished: GameServiceID string + IsServerError bool

# ── Relay-private deck-push message (Problem 2 fix) ───────────────────────────
# TypeID=0x42 is unused by any game handler; RedLobbyManager routes it to the
# executor registered by the mod's Patch_OnlineNetworkConnector_Initialize Postfix.
# CommandID=0x01 signals "here is a player's BattleDeck JSON".
# bytes[3] (TargetPlayerID) = the player slot (1=P1, 2=P2) whose deck this is.
# Payload (bytes[8:]) = UTF-8 BattleDeck JSON string.
TYPE_RELAY_DECK_PUSH  = 0x00  # Routed via TypeID=0x00 executor (RedLobbyManager only dispatches 0x00)
CMD_RELAY_DECK        = 0xF0  # must match RELAY_DECK_CMD in GwentBetaModMain.cs

# ── Relay-private playerinfo-push message (vanity fix) ────────────────────────
# TypeID=0x43 is unused by any game handler; routes to the mod's 0x43 executor.
# CommandID=0x01 signals "here is a player's RelayPlayerInfo JSON".
# bytes[3] (TargetPlayerID) = the player slot (1=P1, 2=P2) whose info this is.
# Payload (bytes[8:]) = UTF-8 RelayPlayerInfo JSON string.
TYPE_RELAY_INFO_PUSH  = 0x00  # Routed via TypeID=0x00 executor (RedLobbyManager only dispatches 0x00)
CMD_RELAY_INFO        = 0xF1  # must match RELAY_INFO_CMD in GwentBetaModMain.cs

# ── Client→relay crown report (round-counting fix) ───────────────────────────
# The AUTHORITY client (P1/C1) sends this at game end with the final per-player
# crown counts (= rounds won). The relay intercepts CommandID=0xF2 in pipe(),
# parses the JSON, and does NOT forward it to the peer. CrownsReportCommand in
# GwentBetaModMain.cs sends [Channel byte][UTF-16LE JSON] as bundle.Payload.
# JSON: {"p1":<int>,"p2":<int>,"winner":<1|2|3>,"game_id":<int>}
CMD_RELAY_CROWNS      = 0xF2  # must match CMD_CROWNS_REPORT in GwentBetaModMain.cs

# LobbyEventType enum values (short)
LOBBY_EVENT_LOBBY_INITIALIZED         = 2
LOBBY_EVENT_LOBBY_ENTERED_BY_OTHER    = 7
LOBBY_EVENT_LOBBY_LEFT_BY_OTHER       = 8
LOBBY_EVENT_PLAYER_READY              = 9

# ── EActionID enum (for payload decoding) ────────────────────────────────────
ACTION_NAMES = {
    1: "SyncGameSettings",
    2: "SetupPlayer",
    3: "SetupPlayerDeck",
    10: "SwitchGameState",
    100: "DrawCard",
    101: "MulliganCard",
    102: "PlayCard",
    103: "PassRound",
    104: "EndGame",
    105: "RequestPlayerReady",
    106: "ReportPlayerStatus",
    107: "RequestPlayerFinished",
    108: "TimerStarted",
    109: "TimerElapsed",
    110: "CancelRequest",
}

# ── EPlayerStatus enum ────────────────────────────────────────────────────────
PLAYER_STATUS_NAMES = {
    0: "Loading",
    1: "Ready",
    2: "Active",
    3: "Finished",
    4: "Blocked",
    5: "CompromisedClientState",
}

# ── EPlayerId enum (bit flags) ────────────────────────────────────────────────
PLAYER_ID_NAMES = {
    0: "None",
    1: "P1",
    2: "P2",
    3: "AllPlayers",
}


def load_battle_deck_json(data_json_path: str):
    """Read data.json and return BattleDeck JSON string for the current deck.
    Returns None if no deck found or resolution fails.

    BattleDeck format expected by BattleSetupFactory:
      { "FactionId": "NorthernKingdom",
        "Leader": {"TemplateId": 201595, "Premium": true},
        "Cards": [{"TemplateId": ..., "Premium": ...}, ...],
        "Name": "Cursed" }

    The first card in user_cards is treated as the leader (per game client convention).
    """
    try:
        with open(data_json_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("load_battle_deck_json: could not read %s: %s", data_json_path, e)
        return None

    # Build instance_id -> card_definition lookup
    card_map = {
        c["id"]: c["card_definition"]
        for c in data.get("cards", [])
        if c.get("card_definition")
    }

    # Find the current (active) deck — fall back to first deck
    decks = data.get("decks", [])
    if not decks:
        log.warning("load_battle_deck_json: no decks in data.json")
        return None
    current_deck = next((d for d in decks if d.get("is_current")), decks[0])

    # data.json sometimes uses "NorthernKingdom(s)" but the C# EFactionId enum
    # only has "NorthernRealms". Map any non-canonical strings to the enum values.
    FACTION_ALIASES = {
        "northernkingdom": "NorthernRealms",
        "northernkingdoms": "NorthernRealms",
        "northern_kingdom": "NorthernRealms",
        "northern_kingdoms": "NorthernRealms",
        "northernrealm": "NorthernRealms",
        "northernrealms": "NorthernRealms",
        "neutral": "Neutral",
        "monsters": "Monsters",
        "monster": "Monsters",
        "nilfgaard": "Nilfgaard",
        "scoiatael": "Scoiatael",
        "skellige": "Skellige",
    }

    def normalize_faction(raw):
        if not raw:
            return "Neutral"
        key = raw.replace(" ", "").replace("-", "").lower()
        return FACTION_ALIASES.get(key, raw)

    user_cards = current_deck.get("user_cards", [])
    leader = None
    cards = []
    for i, uc in enumerate(user_cards):
        card_def = card_map.get(uc["id"])
        if not card_def:
            log.debug("load_battle_deck_json: instance id %d not found in card pool", uc["id"])
            continue
        entry = {"TemplateId": card_def["card_template_id"], "Premium": bool(card_def.get("premium", False))}
        if i == 0:
            leader = entry
        else:
            cards.append(entry)

    if leader is None:
        log.warning("load_battle_deck_json: could not resolve leader card")
        return None

    battle_deck = {
        "FactionId": normalize_faction(current_deck.get("faction")),
        "Leader": leader,
        "Cards": cards,
        "Name": current_deck.get("name", "Deck"),
    }
    result = json.dumps(battle_deck, separators=(",", ":"))
    log.info("load_battle_deck_json: resolved deck '%s' faction=%s leader=%d cards=%d",
             battle_deck["Name"], battle_deck["FactionId"], leader["TemplateId"], len(cards))
    return result


# Path to server data.json (same directory as this script)
_DATA_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
# Cached BattleDeck JSON string (loaded once at startup, refreshed per session if None)
_CACHED_BATTLE_DECK_JSON = None  # type: str | None


def get_battle_deck_json():
    """Return cached BattleDeck JSON, loading from data.json if not yet cached."""
    global _CACHED_BATTLE_DECK_JSON
    if _CACHED_BATTLE_DECK_JSON is None:
        _CACHED_BATTLE_DECK_JSON = load_battle_deck_json(_DATA_JSON_PATH)
    return _CACHED_BATTLE_DECK_JSON

def fetch_battle_deck_from_server(service_id):
    """Fetch a user's active BattleDeck JSON from server.py's internal API.
    service_id: the GOG user ID (from PlayerAuthenticates).
    Returns the BattleDeck JSON string, or None on failure."""
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("127.0.0.1", 8444))
        req = f"GET /internal/user_battle_deck/{service_id} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        s.sendall(req.encode())
        resp = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
            if b"\r\n\r\n" in resp:
                header_part, body_part = resp.split(b"\r\n\r\n", 1)
                for line in header_part.decode().split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        cl = int(line.split(":", 1)[1].strip())
                        while len(body_part) < cl:
                            body_part += s.recv(4096)
                        break
                break
        s.close()
        if b"\r\n\r\n" not in resp:
            return None
        _, body = resp.split(b"\r\n\r\n", 1)
        status_line = resp.split(b"\r\n", 1)[0].decode()
        if "200" not in status_line:
            log.warning("fetch_battle_deck_from_server(%s): HTTP %s", service_id, status_line)
            return None
        deck_json = body.decode("utf-8").strip()
        if deck_json:
            log.info("fetch_battle_deck_from_server(%s): got %d bytes", service_id, len(deck_json))
            return deck_json
        return None
    except Exception as e:
        log.warning("fetch_battle_deck_from_server(%s) failed: %s", service_id, e)
        return None


def write_deck_files_for_dll_fallback(p1_deck_json: str = None, p2_deck_json: str = None):
    """
    Write deck JSON to %TEMP%/gwent_relay_deck_<0|1>.json.

    gwent_relay_deck_0.json → P1's deck (game.Settings.P1.Deck)
    gwent_relay_deck_1.json → P2's deck (game.Settings.P2.Deck)

    When p1_deck_json / p2_deck_json are provided (cross-network: each player sent
    their own deck in PlayerInitialized), the correct per-player decks are written.
    When not provided (single-machine fallback), both slots get the same data.json deck.

    These files are read by the mod's LobbyInitialized lambda as a last-resort fallback
    when neither the relay-push cache (TypeID=0x42) nor the local OnStartMatchmaking
    cache has been populated.  The relay-push path is preferred for cross-network play.
    """
    fallback_json = get_battle_deck_json()

    p1_json = p1_deck_json or fallback_json
    p2_json = p2_deck_json or fallback_json

    if not p1_json and not p2_json:
        log.warning("write_deck_files_for_dll_fallback: no deck available for either player — skipping")
        return False

    tmp = tempfile.gettempdir()
    ok = True
    for idx, deck_json in [(0, p1_json), (1, p2_json)]:
        if not deck_json:
            log.warning("write_deck_files_for_dll_fallback: no deck for slot %d — skipping", idx)
            continue
        p = os.path.join(tmp, f"gwent_relay_deck_{idx}.json")
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(deck_json)
            log.info("Wrote deck fallback file: %s (%d bytes)", p, len(deck_json))
        except Exception as e:
            log.warning("Failed to write deck fallback file %s: %s", p, e)
            ok = False
    return ok


def decode_game_action_summary(payload: bytes) -> str:
    """Decode a GwentCommand payload to extract action info.
    GwentCommand payload: [channel 1B][Unicode-encoded StringDataBuffer content...]
    StringDataBuffer format: actionId(int32) networkId(int32) ... action-specific params
    """
    if len(payload) < 5:
        return f"(too short: {len(payload)}B)"

    channel = payload[0]
    # The rest is UTF-16LE encoded string that represents serialized action data
    # StringDataBuffer writes: actionId, networkId, firesTriggers, delay, then action-specific
    # But it's encoded as a string of pipe-separated values
    try:
        text = payload[1:].decode('utf-16-le', errors='replace')
        parts = text.split(';')
        if len(parts) >= 1:
            try:
                action_id = int(parts[0])
                action_name = ACTION_NAMES.get(action_id, f"Unknown({action_id})")
            except (ValueError, IndexError):
                return f"ch={channel} raw={text[:80]}"

            detail = f"ActionID={action_id}({action_name})"

            # For ReportPlayerStatus (106): parts are actionId|networkId|triggers|delay|playerId|status|hash
            if action_id == 106 and len(parts) >= 6:
                try:
                    player_id = int(parts[4])
                    player_name = PLAYER_ID_NAMES.get(player_id, f"?{player_id}")
                    status = int(parts[5])
                    status_name = PLAYER_STATUS_NAMES.get(status, f"?{status}")
                    hash_val = parts[6] if len(parts) >= 7 else ""
                    detail += f" player={player_name} status={status_name}"
                    if hash_val:
                        detail += f" hash={hash_val[:20]}"
                except (ValueError, IndexError):
                    pass

            # For RequestPlayerReady (105) / RequestPlayerFinished (107):
            # parts: actionId|networkId|triggers|delay|requestId|playerId|targetPlayer|canCancel|...
            elif action_id in (105, 107) and len(parts) >= 7:
                try:
                    req_id = int(parts[4])
                    player_id = int(parts[5])
                    target_player = int(parts[6])
                    player_name = PLAYER_ID_NAMES.get(player_id, f"?{player_id}")
                    target_name = PLAYER_ID_NAMES.get(target_player, f"?{target_player}")
                    detail += f" reqId={req_id} player={player_name} target={target_name}"
                except (ValueError, IndexError):
                    pass

            # For SwitchGameState (10):
            elif action_id == 10 and len(parts) >= 6:
                try:
                    from_state = int(parts[4])
                    to_state = int(parts[5])
                    STATE_NAMES = {1: "Init", 2: "DrawCards", 4: "Mulligan", 8: "ChoosePlayer",
                                   16: "RoundStart", 32: "TurnStart", 64: "Turn", 128: "TurnEnd",
                                   256: "RoundEnd", 384: "ClearBoard", 512: "Results"}
                    detail += f" from={STATE_NAMES.get(from_state, from_state)} to={STATE_NAMES.get(to_state, to_state)}"
                except (ValueError, IndexError):
                    pass

            # For SetupPlayer (2):
            elif action_id == 2 and len(parts) >= 5:
                try:
                    player_id = int(parts[4])
                    player_name = PLAYER_ID_NAMES.get(player_id, f"?{player_id}")
                    detail += f" player={player_name}"
                except (ValueError, IndexError):
                    pass

            # DIAG (timeout investigation): always append the raw leading fields so the
            # status-report round-trip (ready/finished/status) can be read empirically
            # regardless of name-map correctness. parts = actionId;networkId;triggers;delay;...
            try:
                detail += " raw=[" + ";".join(parts[:8]) + "]"
            except Exception:
                pass
            return detail
    except Exception as e:
        return f"(decode error: {e})"

    return f"(undecoded {len(payload)}B)"


def make_pong(ping_msg: bytes) -> bytes:
    pong = bytearray(ping_msg)
    pong[1] = FRAME_PONG
    return bytes(pong)


def is_ping(msg: bytes) -> bool:
    return (len(msg) == 8
            and msg[0] == MAGIC
            and msg[1] == FRAME_PING
            and msg[3] == MAGIC)


def is_join(msg: bytes) -> bool:
    """PlayerAuthenticates: TypeID=0xff, CommandID=0x00"""
    return (len(msg) >= 8
            and msg[0] == TYPE_INTERNAL
            and msg[1] == CMD_PLAYER_AUTHENTICATES)


def is_player_initialized(msg: bytes) -> bool:
    """PlayerInitialized: TypeID=0xff, CommandID=0x02"""
    return (len(msg) >= 8
            and msg[0] == TYPE_INTERNAL
            and msg[1] == CMD_PLAYER_INITIALIZED)


def is_time_req(msg: bytes) -> bool:
    """PlayerTimeReq: TypeID=0xff, CommandID=0x05"""
    return (len(msg) >= 16  # 8 header + 8 double
            and msg[0] == TYPE_INTERNAL
            and msg[1] == CMD_PLAYER_TIME_REQ)


def is_confirm_received(msg: bytes) -> bool:
    """ConfirmReceivedMessages: TypeID=0xff, CommandID=0x14"""
    return (len(msg) >= 8
            and msg[0] == TYPE_INTERNAL
            and msg[1] == CMD_CONFIRM_RECEIVED)


def is_game_command(msg: bytes) -> bool:
    """Game action: TypeID=0x00"""
    return len(msg) >= 8 and msg[0] == TYPE_GAME


def get_seq(msg: bytes) -> int:
    return struct.unpack_from("<I", msg, 4)[0]


def rewrite_bundle_id(msg: bytes, new_id: int) -> bytes:
    """Replace bytes [4:8] (BundleID LE32) with new_id."""
    buf = bytearray(msg)
    struct.pack_into("<I", buf, 4, new_id)
    return bytes(buf)


def read_string_from(data: bytes, offset: int):
    """Read a .NET BinaryWriter string (7-bit encoded length + UTF-8 bytes).
    Returns (string_value, new_offset)."""
    length = 0
    shift = 0
    while offset < len(data):
        b = data[offset]
        offset += 1
        length |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    s = data[offset:offset + length].decode("utf-8", errors="replace")
    return s, offset + length


def read_dict_from(data: bytes, offset: int):
    """Read a .NET RedBinaryWriter Dictionary<string,string>.
    Format: int32 count, then count * (7bit-string key, 7bit-string value).
    Returns (dict, new_offset)."""
    if offset + 4 > len(data):
        return {}, offset
    count = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    d = {}
    for _ in range(count):
        key, offset = read_string_from(data, offset)
        val, offset = read_string_from(data, offset)
        d[key] = val
    return d, offset


def parse_player_authenticates(msg: bytes):
    """Parse PlayerAuthenticates from raw wire message.
    Returns (name, service_id, access_key) or None on failure."""
    if len(msg) < 8:
        return None
    payload = msg[8:]
    try:
        offset = 0
        name, offset = read_string_from(payload, offset)
        if offset + 8 > len(payload):
            return None
        service_id = struct.unpack_from("<Q", payload, offset)[0]
        offset += 8
        access_key, offset = read_string_from(payload, offset)
        return name, service_id, access_key
    except Exception as e:
        log.warning("Failed to parse PlayerAuthenticates: %s", e)
        return None


def parse_player_initialized(msg: bytes):
    """Parse PlayerInitialized (CMD=0x02) from raw wire message.
    Format: Name (7bit+utf8), ServiceId (ulong LE8), Params (Dictionary<string,string>).
    Returns (name, service_id, params_dict) or None on failure."""
    if len(msg) < 8:
        return None
    payload = msg[8:]
    try:
        offset = 0
        name, offset = read_string_from(payload, offset)
        if offset + 8 > len(payload):
            return None
        service_id = struct.unpack_from("<Q", payload, offset)[0]
        offset += 8
        params, offset = read_dict_from(payload, offset)
        return name, service_id, params
    except Exception as e:
        log.warning("Failed to parse PlayerInitialized: %s", e)
        return None


def write_7bit_encoded_int(value: int) -> bytes:
    """Encode an int using .NET BinaryWriter 7-bit encoding."""
    result = bytearray()
    while value > 0x7f:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    result.append(value & 0x7f)
    return bytes(result)


def write_string(s: str) -> bytes:
    """Encode a string as .NET BinaryWriter 7-bit-length-prefixed UTF-8."""
    enc = s.encode("utf-8")
    return write_7bit_encoded_int(len(enc)) + enc


def write_dict(d: dict) -> bytes:
    """Encode a Dictionary<string,string> as RedBinaryWriter.Write(dict).
    Format: int32 count, then count * (7bit-string key, 7bit-string value)."""
    buf = bytearray()
    buf += struct.pack("<i", len(d))
    for k, v in d.items():
        buf += write_string(k)
        buf += write_string(v)
    return bytes(buf)


def build_player_initialized_payload(name: str, service_id: int, params: dict) -> bytes:
    """Build PlayerInitialized (CMD=0x02) payload.
    Format: Name (7bit+utf8), ServiceId (ulong LE8), Params (Dictionary<string,string>)."""
    buf = bytearray()
    buf += write_string(name)
    buf += struct.pack("<Q", service_id)
    buf += write_dict(params)
    return bytes(buf)


def build_setup_players_payload(player_id: int, player_ids: dict, session_id: str = "") -> bytes:
    """
    Build SetupPlayers payload matching RedBinaryWriter output:
      Write(int PlayerID)
      Write(Dictionary<int,ulong> PlayerIDs)  -> 4-byte count + (4-byte int + 8-byte ulong) each
      Write(string SessionID)                 -> 7-bit length + UTF-8
    """
    buf = bytearray()
    buf += struct.pack("<i", player_id)
    buf += struct.pack("<i", len(player_ids))
    for k, v in player_ids.items():
        buf += struct.pack("<i", k)
        buf += struct.pack("<Q", v)
    encoded = session_id.encode("utf-8")
    buf += write_7bit_encoded_int(len(encoded))
    buf += encoded
    return bytes(buf)


def build_red_bundle(command_id: int, payload: bytes, bundle_id: int = 1,
                     target: int = 0, target_player_id: int = 0) -> bytes:
    """Build a full RedBundle wire message."""
    header = bytes([
        TYPE_INTERNAL,
        command_id,
        target,
        target_player_id & 0xff
    ]) + struct.pack("<I", bundle_id)
    return header + payload


def build_broadcast_event(event_type: int, bundle_id: int,
                           player_id: int = 0, status: int = 0) -> bytes:
    """
    BroadcastEvent (CommandID=0x08) payload:
      short EventType  (2 bytes LE)
      int   PlayerID   (4 bytes LE)
      short Status     (2 bytes LE)
    """
    payload = struct.pack("<hih", event_type, player_id, status)
    return build_red_bundle(CMD_BROADCAST_EVENT, payload, bundle_id=bundle_id)


def build_confirm_received(last_bundle_id: int, ack_bundle_id: int,
                           should_send_unreceived: bool = False) -> bytes:
    """
    ConfirmReceivedMessages (CommandID=0x14) payload:
      uint LastReceivedBundleId (4 bytes LE)
      bool ShouldSendUnreceived (1 byte)
    ack_bundle_id: the relay's own outgoing bundleId for this ACK message.
    """
    payload = struct.pack("<IB", last_bundle_id, 1 if should_send_unreceived else 0)
    return build_red_bundle(CMD_CONFIRM_RECEIVED, payload, bundle_id=ack_bundle_id)


def build_deck_push(player_slot: int, deck_json: str, bundle_id: int) -> bytes:
    """
    Build a relay-private deck-push bundle (TypeID=0x42, CommandID=0x01).
    player_slot: 1 for P1, 2 for P2.
    deck_json:   UTF-8 BattleDeck JSON to deliver.
    Payload = UTF-8 deck JSON bytes starting at byte offset 8.
    The receiving mod executor reads bytes[8:] as the deck JSON.
    """
    payload = deck_json.encode("utf-8")
    header = bytes([
        TYPE_RELAY_DECK_PUSH,       # TypeID  (0x42)
        CMD_RELAY_DECK,              # CommandID (0x01)
        0x00,                        # Target (unused)
        player_slot & 0xff,          # TargetPlayerID = player slot
    ]) + struct.pack("<I", bundle_id)
    return header + payload


def build_info_push(player_slot: int, info_json: str, bundle_id: int) -> bytes:
    """
    Build a relay-private playerinfo-push bundle (TypeID=0x43, CommandID=0x01).
    player_slot: 1 for P1, 2 for P2.
    info_json:   UTF-8 RelayPlayerInfo JSON to deliver.
    Payload = UTF-8 info JSON bytes starting at byte offset 8.
    The receiving mod executor reads bytes[8:] as the info JSON.
    """
    payload = info_json.encode("utf-8")
    header = bytes([
        TYPE_RELAY_INFO_PUSH,       # TypeID  (0x43)
        CMD_RELAY_INFO,              # CommandID (0x01)
        0x00,                        # Target (unused)
        player_slot & 0xff,          # TargetPlayerID = player slot
    ]) + struct.pack("<I", bundle_id)
    return header + payload


def build_current_time_res(request_send_time: float, request_receive_time: float,
                            response_send_time: float, bundle_id: int) -> bytes:
    """
    CurrentTimeRes (CommandID=0x06) payload:
      double RequestSendTime    (8 bytes LE)
      double RequestReceiveTime (8 bytes LE)
      double ResponseSendTime   (8 bytes LE)
    """
    payload = struct.pack("<ddd", request_send_time, request_receive_time, response_send_time)
    return build_red_bundle(CMD_CURRENT_TIME_RES, payload, bundle_id=bundle_id)


# ── Shared pairing state ────────────────────────────────────────────────────

def build_game_finished(game_service_id: str, bundle_id: int, is_server_error: bool = False) -> bytes:
    """Build a GameFinished (CommandID=0x03) bundle.

    Mirrors RedNetwork.GameFinished.GetBytes():
      string GameServiceID   (.NET BinaryWriter 7-bit-length-prefixed UTF-8)
      bool   IsServerError   (1 byte, 0=false)

    Fires LobbyEventType.GameFinished in RedLobbyManager, which calls
    AppGameOutcomeState.OnEndGameDataReceived and sets GameLogId = int(GameServiceID).
    Must be sent to both clients so each enters the reward state machine.
    """
    payload = write_string(game_service_id) + bytes([1 if is_server_error else 0])
    return build_red_bundle(CMD_GAME_FINISHED, payload, bundle_id=bundle_id)

_pending_ws    = None
_pending_event = None
_lock = asyncio.Lock()


async def recv_join(ws, label) -> bytes:
    """Drain pings (respond with pongs) until we get the join message."""
    async for msg in ws:
        if not isinstance(msg, bytes):
            continue
        if is_ping(msg):
            seq = get_seq(msg)
            log.info("%s ping seq=%d (before pair) → pong", label, seq)
            await ws.send(make_pong(msg))
            continue
        if is_join(msg):
            log.info("%s join received (%d bytes) [%s]", label, len(msg), msg[:24].hex())
            return msg
        log.info("%s unknown frame type=0x%02x (%d bytes) before join",
                 label, msg[1] if len(msg) > 1 else 0, len(msg))


TARGET_SERVER        = 0x00
TARGET_TARGET_PLAYER = 0x01
TARGET_ALL_PLAYERS   = 0x02
TARGET_ALL_PLAYERS2  = 0x03  # observed in the wild as broadcast


def trigger_broker_profile_progress(rounds_won: int = 1, game_id: int = 0,
                                    user_id: str = None, won: bool = None):
    """Notify server.py of a regular match win so it can update XP/crowns in data.json
    and then trigger broker.py to push a profile_progress notification.
    Calls the /internal/game_finish endpoint on server.py (HTTP, no TLS, port 8444).
    rounds_won: how many rounds the local player won (1 or 2), used for crown pieces.
    user_id: GOG user ID string to target the correct per-user data file.
    """
    try:
        body = {"rounds_won": rounds_won, "game_id": game_id}
        if won is not None:
            body["won"] = bool(won)
        if user_id:
            body["user_id"] = str(user_id)
        payload = json.dumps(body)
        header = (
            f"POST /internal/game_finish HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"\r\n"
        )
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", 8444))
        s.sendall(header.encode() + payload.encode())
        s.recv(256)
        s.close()
        log.info("GAME FINISH trigger sent: rounds_won=%d won=%s game_id=%d",
                 rounds_won, won, game_id)
    except Exception as e:
        log.warning("GAME FINISH trigger failed: %s", e)


async def _fire_game_finish_from_crowns(init_tracker, game_id, game_id_str,
                                        p1c, p1_won, p2c, p2_won):
    """Inject GameFinished to both clients and fire the per-player game_finish
    trigger using AUTHORITATIVE crown counts reported by the mod.

    p1c / p2c : final crowns (= rounds won, 0/1/2) for slot P1 / P2.
    p1_won / p2_won : whether that slot won the match (from the Winner bitflag).
    Slot P1 maps to service_id_1, slot P2 maps to service_id_2.
    """
    await asyncio.sleep(0.5)  # let the last game action reach both clients first
    ws1_ = init_tracker['ws1']
    ws2_ = init_tracker['ws2']
    ctr1_ = init_tracker['ctr1']
    ctr2_ = init_tracker['ctr2']
    async with ctr1_['lock']:
        ctr1_['val'] += 1
        bid1 = ctr1_['val']
    async with ctr2_['lock']:
        ctr2_['val'] += 1
        bid2 = ctr2_['val']
    gf1 = build_game_finished(game_id_str, bid1)
    gf2 = build_game_finished(game_id_str, bid2)
    try:
        await ws1_.send(gf1)
        log.info("GameFinished -> C1 bundleId=%d game_id=%s", bid1, game_id_str)
    except Exception as e:
        log.warning("GameFinished send to C1 failed: %s", e)
    try:
        await ws2_.send(gf2)
        log.info("GameFinished -> C2 bundleId=%d game_id=%s", bid2, game_id_str)
    except Exception as e:
        log.warning("GameFinished send to C2 failed: %s", e)
    _loop = asyncio.get_event_loop()
    sid1 = str(init_tracker.get('service_id_1', ''))
    sid2 = str(init_tracker.get('service_id_2', ''))
    # P1 -> service_id_1, P2 -> service_id_2. rounds_won = that player's crowns.
    await _loop.run_in_executor(
        None, trigger_broker_profile_progress, p1c, game_id, sid1, p1_won)
    await _loop.run_in_executor(
        None, trigger_broker_profile_progress, p2c, game_id, sid2, p2_won)


async def pipe(src, dst, label, dst_ctr: dict, src_ctr: dict, init_tracker: dict,
               src_player_id: int):
    """
    Relay src→dst, intercepting:
      - App-level pings (ff15)     → pong back to src
      - PlayerTimeReq (ff05)       → reply with CurrentTimeRes to src
      - ConfirmReceivedMessages    → log and drop (no forwarding)
      - PlayerInitialized (ff02)   → track; when both received, trigger lobby init sequence
      - TargetPlayer msgs for src  → drop (authority applied locally)
      - Everything else            → rewrite BundleID, stamp sender, forward to dst
      After each forwarded/dropped data message, send ConfirmReceivedMessages ACK to src.

    dst_ctr / src_ctr: {'val': int, 'lock': asyncio.Lock()}
      Each destination has ONE counter shared across all concurrent writers.
      dst_ctr is used when forwarding to dst; src_ctr is used when sending ACKs to src.
    src_player_id: the PlayerID assigned to the src client (1 or 2)
    """
    msg_count = 0
    last_received_from_src = 0  # track highest bundleId received from src for ACKs
    try:
        async for msg in src:
            if not isinstance(msg, bytes):
                await dst.send(msg)
                continue

            if is_ping(msg):
                seq = get_seq(msg)
                log.info("%s ping seq=%d → pong", label, seq)
                await src.send(make_pong(msg))

            elif is_player_initialized(msg):
                log.info("%s PlayerInitialized intercepted (%d bytes) [%s]",
                         label, len(msg), msg[:16].hex())
                # Parse and store name + params (Deck, PlayerInfo) for this player
                parsed_init = parse_player_initialized(msg)
                if parsed_init:
                    p_name, p_sid, p_params = parsed_init
                    deck_present = "Deck" in p_params
                    info_present = "PlayerInfo" in p_params
                    log.info("%s PlayerInitialized: name=%r serviceId=%d hasDeck=%s hasPlayerInfo=%s params_keys=%s",
                             label, p_name, p_sid, deck_present, info_present, list(p_params.keys()))
                    # GOG matchmaking path never sets Params["Deck"].
                    # The mod's OnStartMatchmaking patch appends each client's deck to
                    # gwent_relay_deck_queue.json in the order they click "Find Game":
                    #   queue[0] → first  client to queue → C1/P1
                    #   queue[1] → second client to queue → C2/P2
                    # This works even on same-machine testing where both clients share
                    # %TEMP% and have the same ServiceId (same GOG account).
                    # Fallback chain: queue → deck_0.json → data.json
                    # ── PlayerInfo queue read ─────────────────────────────────────────
                    # Read the playerinfo queue written by the mod's OnStartMatchmaking
                    # patch.  Stored in init_tracker for _send_lobby_init_sequence.
                    if not info_present:
                        info_json_val = None
                        try:
                            tmp_pi = tempfile.gettempdir()
                            pi_queue_path = os.path.join(tmp_pi, "gwent_relay_playerinfo_queue.json")
                            pi_queue_index = src_player_id - 1
                            if os.path.exists(pi_queue_path):
                                with open(pi_queue_path, "r", encoding="utf-8") as _pif:
                                    pi_queue = json.loads(_pif.read())
                                if isinstance(pi_queue, list) and len(pi_queue) > pi_queue_index:
                                    candidate = pi_queue[pi_queue_index].strip() if pi_queue[pi_queue_index] else ""
                                    if candidate:
                                        info_json_val = candidate
                                        log.info("%s Injected PlayerInfo from queue[%d] (%d bytes)",
                                                 label, pi_queue_index, len(info_json_val))
                        except Exception as _pie:
                            log.warning("%s Could not read playerinfo queue: %s", label, _pie)
                        if info_json_val:
                            p_params = dict(p_params)  # copy before mutating (may already be copied for deck)
                            p_params["PlayerInfo"] = info_json_val

                    if not deck_present:
                        deck_json = None
                        # Fetch from server.py API using player's serviceId
                        deck_json = await asyncio.get_event_loop().run_in_executor(
                            None, fetch_battle_deck_from_server, p_sid)
                        if deck_json:
                            log.info("%s Fetched Deck from server API for serviceId=%d (%d bytes)",
                                     label, p_sid, len(deck_json))
                        else:
                            # Fallback: try local temp files (same-machine testing)
                            tmp = tempfile.gettempdir()
                            queue_index = src_player_id - 1
                            queue_path = os.path.join(tmp, "gwent_relay_deck_queue.json")
                            try:
                                if os.path.exists(queue_path):
                                    with open(queue_path, "r", encoding="utf-8") as _qf:
                                        queue = json.loads(_qf.read())
                                    if isinstance(queue, list) and len(queue) > queue_index:
                                        candidate = queue[queue_index].strip() if queue[queue_index] else ""
                                        if candidate:
                                            deck_json = candidate
                                            log.info("%s Injected Deck from queue[%d] (%d bytes)", label, queue_index, len(deck_json))
                            except Exception as _e:
                                log.warning("%s Could not read deck queue: %s", label, _e)
                            if not deck_json:
                                deck_json = get_battle_deck_json()
                                if deck_json:
                                    log.info("%s Injected Deck from data.json (last-resort fallback)", label)
                                else:
                                    log.warning("%s No deck available from any source", label)
                        if deck_json:
                            p_params = dict(p_params)  # copy before mutating
                            p_params["Deck"] = deck_json
                    async with init_tracker['lock']:
                        init_tracker[f'name_{src_player_id}'] = p_name
                        init_tracker[f'service_id_{src_player_id}'] = p_sid
                        init_tracker[f'params_{src_player_id}'] = p_params
                else:
                    log.warning("%s Failed to parse PlayerInitialized params", label)
                    async with init_tracker['lock']:
                        init_tracker[f'params_{src_player_id}'] = {}
                async with init_tracker['lock']:
                    if not init_tracker.get('init_' + label):
                        init_tracker['init_' + label] = True
                        init_tracker['count'] += 1
                    count = init_tracker['count']
                    already_sent = init_tracker.get('lobby_init_sent', False)
                if count >= 2 and not already_sent:
                    async with init_tracker['lock']:
                        if not init_tracker.get('lobby_init_sent', False):
                            init_tracker['lobby_init_sent'] = True
                            do_send = True
                        else:
                            do_send = False
                    if do_send:
                        log.info("Both PlayerInitialized received — starting lobby init sequence")
                        await _send_lobby_init_sequence(init_tracker)

            elif is_time_req(msg):
                # Reply with CurrentTimeRes instead of silently dropping
                incoming_bundle_id = struct.unpack_from("<I", msg, 4)[0]
                send_time = struct.unpack_from("<d", msg, 8)[0]
                now = time.time()
                async with src_ctr['lock']:
                    src_ctr['val'] += 1
                    reply_bid = src_ctr['val']
                time_res = build_current_time_res(send_time, now, now, reply_bid)
                log.info("%s TimeReq sendTime=%.3f → CurrentTimeRes bundleId=%d",
                         label, send_time, reply_bid)
                await src.send(time_res)

            elif is_confirm_received(msg):
                # Client ACKing our messages — just log it
                if len(msg) >= 13:
                    acked_bid = struct.unpack_from("<I", msg, 8)[0]
                    should_resend = msg[12] if len(msg) > 12 else 0
                    log.debug("%s ConfirmReceived lastBundleId=%d shouldResend=%d",
                             label, acked_bid, should_resend)

            else:
                # Route based on Target and TargetPlayerID fields.
                # Header: [TypeID][CommandID][Target][TargetPlayerID][BundleID x4]
                type_id = msg[0]
                cmd_id = msg[1] if len(msg) > 1 else 0
                target = msg[2] if len(msg) > 2 else 0
                target_player = msg[3] if len(msg) > 3 else 0
                incoming_bundle_id = struct.unpack_from("<I", msg, 4)[0]

                # Track highest received bundleId for ACKs
                if incoming_bundle_id > last_received_from_src:
                    last_received_from_src = incoming_bundle_id

                # Decode game action payload for logging
                action_info = ""
                if type_id == TYPE_GAME and len(msg) > 8:
                    action_info = " | " + decode_game_action_summary(msg[8:])

                # ── Crown report from the authority client (CommandID=0xF2) ──
                # The authority client (P1/C1) sends the final per-player crowns
                # at game end. This is the AUTHORITATIVE round-count source: we
                # award crowns = rounds actually won (0/1/2) per player, and we
                # do NOT forward this message to the peer.
                if type_id == TYPE_GAME and cmd_id == CMD_RELAY_CROWNS:
                    msg_count += 1
                    log.info("%s CrownsReport received (%d bytes)", label, len(msg))
                    try:
                        # Payload at msg[8:] is [Channel byte][UTF-16LE JSON].
                        payload = msg[8:]
                        if payload:
                            crown_json = payload[1:].decode('utf-16-le', errors='replace')
                            crowns = json.loads(crown_json)
                            p1c = int(crowns.get('p1', 0))
                            p2c = int(crowns.get('p2', 0))
                            winner = int(crowns.get('winner', 0))  # 1=P1, 2=P2, 3=both
                            # clamp crowns to the best-of-3 range
                            p1c = max(0, min(2, p1c))
                            p2c = max(0, min(2, p2c))
                            async with init_tracker['lock']:
                                already = init_tracker.get('game_finish_triggered')
                                if not already:
                                    init_tracker['game_finish_triggered'] = True
                                    do_trigger = True
                                else:
                                    do_trigger = False
                            if do_trigger:
                                game_id = init_tracker.get('game_id', 0)
                                game_id_str = str(game_id)
                                # winner bitflag: P1 won if bit 1 set, P2 if bit 2 set.
                                p1_won = bool(winner & 1)
                                p2_won = bool(winner & 2)
                                log.info("%s CrownsReport: P1=%d(won=%s) P2=%d(won=%s) game_id=%s",
                                         label, p1c, p1_won, p2c, p2_won, game_id_str)
                                # Fire as a background task so this pipe keeps
                                # draining/ACKing while the 0.5s settle delay runs.
                                asyncio.ensure_future(_fire_game_finish_from_crowns(
                                    init_tracker, game_id, game_id_str,
                                    p1c, p1_won, p2c, p2_won))
                            else:
                                log.info("%s CrownsReport ignored (game_finish already triggered)", label)
                    except (ValueError, IndexError, UnicodeDecodeError, KeyError) as e:
                        log.warning("%s CrownsReport parse failed: %s", label, e)
                    # Do NOT forward; fall through to send the ACK so the client
                    # does not resend this report.
                elif target == TARGET_TARGET_PLAYER and target_player == src_player_id:
                    # Authority sends this action addressed to itself (P1-targeted from C1).
                    # Already applied locally — drop it.
                    msg_count += 1
                    log.info("%s DROP self-targeted msg#%d: %d bytes target=%d targetPlayer=%d "
                             "bundleId=%d type=0x%02x cmd=0x%02x%s",
                             label, msg_count, len(msg), target, target_player,
                             incoming_bundle_id, type_id, cmd_id, action_info)
                else:
                    # Forward to dst, rewriting bundleId and stamping src_player_id
                    async with dst_ctr['lock']:
                        dst_ctr['val'] += 1
                        new_id = dst_ctr['val']
                    msg_count += 1
                    buf = bytearray(msg)
                    struct.pack_into("<I", buf, 4, new_id)  # rewrite bundleId
                    buf[3] = src_player_id & 0xff           # stamp sender PlayerID
                    rewritten = bytes(buf)
                    log.info("%s FWD msg#%d: %d bytes target=%d targetPlayer=%d→%d "
                             "bundleId=%d→%d type=0x%02x cmd=0x%02x%s",
                             label, msg_count, len(msg), target, target_player,
                             src_player_id, incoming_bundle_id, new_id,
                             type_id, cmd_id, action_info)
                    await dst.send(rewritten)

                    # Track rounds completed via SwitchGameState(10) to RoundEnd(256).
                    # The authority sends ALL game actions so src_player_id is always 1;
                    # we cannot determine who passed from the pipe direction.
                    # In best-of-3 Gwent the winner always wins exactly 2 rounds.
                    if type_id == TYPE_GAME and len(msg) > 8:
                        try:
                            text2 = msg[9:].decode('utf-16-le', errors='replace')
                            parts2 = text2.split(';')
                            if len(parts2) >= 6 and int(parts2[0]) == 10:  # SwitchGameState
                                to_state = int(parts2[5])
                                if to_state == 256:  # RoundEnd
                                    prev = init_tracker.get('rounds_completed', 0)
                                    init_tracker['rounds_completed'] = prev + 1
                                    log.info("%s SwitchGameState->RoundEnd: rounds_completed=%d",
                                             label, init_tracker['rounds_completed'])
                        except (ValueError, IndexError, UnicodeDecodeError):
                            pass

                    # Detect game end: PassRound (103) where parts[3]==512 (Results flag).
                    # Format: actionId;networkId;triggers;delay  e.g. "103;84;32;512;"
                    #
                    # This is a FALLBACK only. The authoritative round/crown source
                    # is the mod's CrownsReport (CommandID=0xF2), which fires
                    # _fire_game_finish_from_crowns with the real per-player crowns.
                    # We give that report a grace window; if it never arrives (e.g.
                    # an older client DLL that doesn't send it), we fire here with a
                    # best-of-3 heuristic. This is intentionally only a safety net so
                    # old clients still complete their outcome screen.
                    if (type_id == TYPE_GAME and len(msg) > 8
                            and not init_tracker.get('game_finish_triggered')
                            and not init_tracker.get('game_finish_fallback_scheduled')):
                        try:
                            text = msg[9:].decode('utf-16-le', errors='replace')
                            parts = text.split(';')
                            is_game_over = False
                            if len(parts) >= 4 and int(parts[0]) == 103:  # PassRound
                                if int(parts[3]) == 512:  # delay field = Results
                                    is_game_over = True
                            if is_game_over:
                                async with init_tracker['lock']:
                                    if not init_tracker.get('game_finish_fallback_scheduled'):
                                        init_tracker['game_finish_fallback_scheduled'] = True
                                        do_schedule = True
                                    else:
                                        do_schedule = False
                                if do_schedule:
                                    game_id     = init_tracker.get('game_id', 0)
                                    game_id_str = str(game_id)
                                    log.info("%s PassRound->Results detected; scheduling fallback "
                                             "(awaiting CrownsReport) game_id=%s", label, game_id_str)

                                    async def _delayed_fallback():
                                        # Wait for the authoritative CrownsReport.
                                        await asyncio.sleep(2.0)
                                        async with init_tracker['lock']:
                                            if init_tracker.get('game_finish_triggered'):
                                                fire = False
                                            else:
                                                init_tracker['game_finish_triggered'] = True
                                                fire = True
                                        if not fire:
                                            log.info("Fallback skipped: CrownsReport already handled game_id=%s",
                                                     game_id_str)
                                            return
                                        # Winner slot unknown here; use a conservative
                                        # heuristic (one side 2 crowns/win, other 1/loss)
                                        # so old clients still get a sane outcome screen.
                                        log.warning("CrownsReport not received; firing FALLBACK "
                                                    "rewards (heuristic) game_id=%s", game_id_str)
                                        await _fire_game_finish_from_crowns(
                                            init_tracker, game_id, game_id_str,
                                            2, True, 1, False)
                                    asyncio.ensure_future(_delayed_fallback())
                        except (ValueError, IndexError, UnicodeDecodeError):
                            pass

                # Send ConfirmReceivedMessages ACK back to src for every data message.
                # This prevents the client's ShouldResend=true logic from retransmitting
                # unACK'd game actions, which would cause duplicate processing or floods.
                async with src_ctr['lock']:
                    src_ctr['val'] += 1
                    ack_bid = src_ctr['val']
                ack_msg = build_confirm_received(last_received_from_src, ack_bid)
                await src.send(ack_msg)
                log.debug("%s ACK → lastReceived=%d ackBundleId=%d",
                          label, last_received_from_src, ack_bid)

    except websockets.exceptions.ConnectionClosed as e:
        log.info("%s closed after %d msgs (%s)", label, msg_count, e)
    except Exception as e:
        log.error("%s error: %s: %s", label, type(e).__name__, e)


async def _send_lobby_init_sequence(tracker: dict):
    """
    After both clients have sent PlayerInitialized, simulate server behaviour:
      1. BroadcastEvent(PlayerReady=9)                → sets State = NetworkState.Online
      2. BroadcastEvent(LobbyEnteredByOtherClient=7)  → pops any reconnect logic pause
      3. DeckPush(TypeID=0x42): own deck to self       → mod caches own deck (slot)
      4. DeckPush(TypeID=0x42): opponent deck to self  → mod caches opponent deck (slot)
      5. BroadcastEvent(LobbyInitialized=2)            → fires LobbyInitialized → game starts

    Steps 3-4 are the Problem 2 fix for cross-network multiplayer.  Each client
    receives both decks from the relay just before LobbyInitialized fires, so the
    LobbyInitialized lambda can load game.Settings.P1/P2.Deck from the in-process
    DeckCache without touching %TEMP% files.

    Uses locked counters so bundleIds are contiguous with any concurrent forwarded messages.
    """
    ws1  = tracker['ws1']
    ws2  = tracker['ws2']
    ctr1 = tracker['ctr1']  # dict with 'val' and 'lock'
    ctr2 = tracker['ctr2']

    # Grab the deck JSON collected from each player's PlayerInitialized params.
    # These were injected by the relay's pipe() handler: if the client didn't
    # include a Deck param (GOG path), the relay injected it from data.json.
    params1 = tracker.get('params_1', {})
    params2 = tracker.get('params_2', {})
    deck1_json = params1.get('Deck')  # P1's deck JSON (or None)
    deck2_json = params2.get('Deck')  # P2's deck JSON (or None)

    if not deck1_json:
        log.warning("_send_lobby_init_sequence: no deck for P1 — deck push will be skipped for P1")
    if not deck2_json:
        log.warning("_send_lobby_init_sequence: no deck for P2 — deck push will be skipped for P2")

    # Grab the PlayerInfo JSON injected from the playerinfo queue.
    info1_json = params1.get('PlayerInfo')  # P1's RelayPlayerInfo JSON (or None)
    info2_json = params2.get('PlayerInfo')  # P2's RelayPlayerInfo JSON (or None)

    if not info1_json:
        log.warning("_send_lobby_init_sequence: no PlayerInfo for P1 — info push will be skipped for P1")
    if not info2_json:
        log.warning("_send_lobby_init_sequence: no PlayerInfo for P2 — info push will be skipped for P2")

    # ── Re-write %TEMP% fallback files with correct per-player decks ──────────
    # Now that we have both players' decks, overwrite the same-deck stub that
    # was written at SetupPlayers time.  This ensures the file-based fallback
    # path in LobbyInitialized also has the correct decks.
    # (On the relay host these are the canonical files; on remote clients the
    # TypeID=0x42 push fills DeckCache directly without needing these files.)
    write_deck_files_for_dll_fallback(
        p1_deck_json=deck1_json,
        p2_deck_json=deck2_json,
    )

    # ── Send sequence to each client ──────────────────────────────────────────
    for ws, ctr, label, own_slot, opp_slot, own_deck, opp_deck, own_info, opp_info in [
        (ws1, ctr1, "C1", 1, 2, deck1_json, deck2_json, info1_json, info2_json),  # C1=P1
        (ws2, ctr2, "C2", 2, 1, deck2_json, deck1_json, info2_json, info1_json),  # C2=P2
    ]:
        # 1. PlayerReady — sets State = Online
        async with ctr['lock']:
            ctr['val'] += 1
            bid = ctr['val']
        ready_msg = build_broadcast_event(LOBBY_EVENT_PLAYER_READY, bundle_id=bid)
        log.info("Sending BroadcastEvent(PlayerReady=9) to %s bundleId=%d", label, bid)
        try:
            await ws.send(ready_msg)
        except Exception as e:
            log.warning("Failed to send PlayerReady to %s: %s", label, e)

        # 2. LobbyEnteredByOtherClient — pops any reconnect logic pause on authority.
        # The real GOG server sends this when the other player joins the lobby.
        async with ctr['lock']:
            ctr['val'] += 1
            bid = ctr['val']
        entered_msg = build_broadcast_event(LOBBY_EVENT_LOBBY_ENTERED_BY_OTHER, bundle_id=bid)
        log.info("Sending BroadcastEvent(LobbyEnteredByOtherClient=7) to %s bundleId=%d", label, bid)
        try:
            await ws.send(entered_msg)
        except Exception as e:
            log.warning("Failed to send LobbyEnteredByOtherClient to %s: %s", label, e)

        # 3. DeckPush — send OWN deck (slot = own_slot) to this client.
        #    The mod's DeckCache.P1DeckJson / P2DeckJson will be set accordingly.
        #    This is belt-and-suspenders: the OnStartMatchmaking patch already
        #    cached the local deck; this ensures cross-network clients also have it.
        if own_deck:
            async with ctr['lock']:
                ctr['val'] += 1
                bid = ctr['val']
            deck_push = build_deck_push(own_slot, own_deck, bid)
            log.info("Sending DeckPush(slot=%d) OWN to %s bundleId=%d (%d bytes json)",
                     own_slot, label, bid, len(own_deck))
            try:
                await ws.send(deck_push)
            except Exception as e:
                log.warning("Failed to send own DeckPush to %s: %s", label, e)
        else:
            log.warning("Skipping own DeckPush to %s: no deck available for slot %d", label, own_slot)

        # 4. DeckPush — send OPPONENT deck (slot = opp_slot) to this client.
        #    This is the core of the cross-network fix: each client receives the
        #    other player's deck from the relay so it can populate game.Settings.P2.Deck
        #    (from C1's perspective) or game.Settings.P1.Deck (from C2's perspective).
        if opp_deck:
            async with ctr['lock']:
                ctr['val'] += 1
                bid = ctr['val']
            deck_push = build_deck_push(opp_slot, opp_deck, bid)
            log.info("Sending DeckPush(slot=%d) OPPONENT to %s bundleId=%d (%d bytes json)",
                     opp_slot, label, bid, len(opp_deck))
            try:
                await ws.send(deck_push)
            except Exception as e:
                log.warning("Failed to send opponent DeckPush to %s: %s", label, e)
        else:
            log.warning("Skipping opponent DeckPush to %s: no deck available for slot %d", label, opp_slot)

        # 5. InfoPush — send OWN PlayerInfo (slot = own_slot) to this client.
        if own_info:
            async with ctr['lock']:
                ctr['val'] += 1
                bid = ctr['val']
            info_push = build_info_push(own_slot, own_info, bid)
            log.info("Sending InfoPush(slot=%d) OWN to %s bundleId=%d (%d bytes json)",
                     own_slot, label, bid, len(own_info))
            try:
                await ws.send(info_push)
            except Exception as e:
                log.warning("Failed to send own InfoPush to %s: %s", label, e)
        else:
            log.warning("Skipping own InfoPush to %s: no PlayerInfo for slot %d", label, own_slot)

        # 6. InfoPush — send OPPONENT PlayerInfo (slot = opp_slot) to this client.
        if opp_info:
            async with ctr['lock']:
                ctr['val'] += 1
                bid = ctr['val']
            info_push = build_info_push(opp_slot, opp_info, bid)
            log.info("Sending InfoPush(slot=%d) OPPONENT to %s bundleId=%d (%d bytes json)",
                     opp_slot, label, bid, len(opp_info))
            try:
                await ws.send(info_push)
            except Exception as e:
                log.warning("Failed to send opponent InfoPush to %s: %s", label, e)
        else:
            log.warning("Skipping opponent InfoPush to %s: no PlayerInfo for slot %d", label, opp_slot)

        # 8. LobbyInitialized — triggers game launch.
        #    Sent last so DeckCache and PlayerInfoCache are fully populated before
        #    the LobbyInitialized lambda runs.
        async with ctr['lock']:
            ctr['val'] += 1
            bid = ctr['val']
        init_msg = build_broadcast_event(LOBBY_EVENT_LOBBY_INITIALIZED, bundle_id=bid)
        log.info("Sending BroadcastEvent(LobbyInitialized=2) to %s bundleId=%d", label, bid)
        try:
            await ws.send(init_msg)
        except Exception as e:
            log.warning("Failed to send LobbyInitialized to %s: %s", label, e)


async def handler(ws):
    global _pending_ws, _pending_event

    log.info("Client connected from %s", ws.remote_address)

    async with _lock:
        if _pending_ws is None:
            _pending_ws    = ws
            _pending_event = asyncio.Event()
            my_event       = _pending_event
            am_first       = True
            log.info("First client — waiting for partner")
        else:
            partner        = _pending_ws
            partner_event  = _pending_event
            _pending_ws    = None
            _pending_event = None
            am_first       = False
            log.info("Second client — pairing now")

    if am_first:
        try:
            await my_event.wait()
        except Exception:
            async with _lock:
                if _pending_ws is ws:
                    _pending_ws = None
                    _pending_event = None
            return
        try:
            await ws.wait_closed()
        except Exception:
            pass
        log.info("First client disconnected")
        return

    # ── Second client — we orchestrate the session ──────────────────────────
    ws1 = partner   # first to connect  (will be Player 1 / host)
    ws2 = ws        # second to connect (will be Player 2 / client)

    partner_event.set()

    # ── Step 1: collect both join messages ──────────────────────────────────
    log.info("Collecting join messages from both clients...")
    try:
        join1_task = asyncio.create_task(recv_join(ws1, "C1"))
        join2_task = asyncio.create_task(recv_join(ws2, "C2"))
        join1, join2 = await asyncio.gather(join1_task, join2_task)
    except Exception as e:
        log.error("Failed to receive joins: %s", e)
        return

    # ── Step 2: parse ServiceIds ────────────────────────────────────────────
    parsed1 = parse_player_authenticates(join1)
    parsed2 = parse_player_authenticates(join2)

    def _sid_from_access_key(access_key):
        """Extract real user ID from access_key like 'host-key-<uid>' or 'client-key-<uid>'.
        Only returns a value if the suffix looks like a real GOG user ID (> 1e12).
        Regular matchmaking uses 'client-key-<ticket_id>' where ticket_id is a small
        integer — those must NOT override the binary serviceId or XP/broker notifications
        will target the wrong user."""
        if access_key and ("-key-" in access_key):
            try:
                val = int(access_key.split("-key-", 1)[1])
                if val > 1_000_000_000_000:  # real GOG user ID, not a ticket ID
                    return val
            except ValueError:
                pass
        return None

    service_id_1 = parsed1[1] if parsed1 else 1
    service_id_2 = parsed2[1] if parsed2 else 2

    if parsed1:
        log.info("C1: name=%s serviceId=%d accessKey=%s", parsed1[0], parsed1[1], parsed1[2])
        # access_key encodes the real user ID — use it when available (friend match
        # path sends wrong serviceId in the binary PlayerAuthenticates field)
        sid_from_key1 = _sid_from_access_key(parsed1[2])
        if sid_from_key1 and sid_from_key1 != service_id_1:
            log.info("C1: overriding serviceId %d -> %d from accessKey", service_id_1, sid_from_key1)
            service_id_1 = sid_from_key1
    else:
        log.warning("C1: could not parse PlayerAuthenticates, using dummy serviceId=1")

    if parsed2:
        log.info("C2: name=%s serviceId=%d accessKey=%s", parsed2[0], parsed2[1], parsed2[2])
        sid_from_key2 = _sid_from_access_key(parsed2[2])
        if sid_from_key2 and sid_from_key2 != service_id_2:
            log.info("C2: overriding serviceId %d -> %d from accessKey", service_id_2, sid_from_key2)
            service_id_2 = sid_from_key2
    else:
        log.warning("C2: could not parse PlayerAuthenticates, using dummy serviceId=2")

    # ── Step 3: send SetupPlayers to each client ────────────────────────────
    # Do NOT write deck fallback files here. The mod's OnStartMatchmaking patch
    # already wrote the player's chosen deck to gwent_relay_deck_0.json before
    # connecting to the relay. Writing it here with the stale data.json deck
    # would overwrite the correct deck and break PlayerInitialized injection.
    # The correct per-player decks are written in _send_lobby_init_sequence()
    # after both PlayerInitialized messages arrive.

    # Server-side BundleID counters: start at 1 for SetupPlayers.
    player_ids = {1: service_id_1, 2: service_id_2}

    payload1 = build_setup_players_payload(1, player_ids, "")
    payload2 = build_setup_players_payload(2, player_ids, "")

    setup1 = build_red_bundle(command_id=CMD_SETUP_PLAYERS, payload=payload1, bundle_id=1)
    setup2 = build_red_bundle(command_id=CMD_SETUP_PLAYERS, payload=payload2, bundle_id=1)

    log.info("Sending SetupPlayers to C1 (PlayerID=1): %s", setup1.hex())
    log.info("Sending SetupPlayers to C2 (PlayerID=2): %s", setup2.hex())

    await ws1.send(setup1)
    await ws2.send(setup2)

    # BundleID counters for each destination — dict with shared lock so concurrent
    # pipes (forward from peer + loopback from authority) stay in sequence.
    # SetupPlayers used bundleId=1, so next relay-originated msg starts at 2.
    ctr1 = {'val': 1, 'lock': asyncio.Lock()}  # counter for messages TO ws1 (C1/P1)
    ctr2 = {'val': 1, 'lock': asyncio.Lock()}  # counter for messages TO ws2 (C2/P2)

    # Shared tracker for PlayerInitialized interception and lobby init sequence.
    init_tracker = {
        'count': 0,
        'ws1': ws1,
        'ws2': ws2,
        'ctr1': ctr1,
        'ctr2': ctr2,
        'lock': asyncio.Lock(),
        'game_id': int(time.time()),  # numeric match ID sent as GameServiceID via GameFinished
        'service_id_1': service_id_1,
        'service_id_2': service_id_2,
    }

    # ── Step 4: bidirectional relay ──────────────────────────────────────────
    # ws1=P1 (authority/Server), ws2=P2 (client).
    # C1→C2 pipe: forwards P2-targeted msgs to C2 (dst_ctr=ctr2),
    #             sends ACKs back to C1 (src_ctr=ctr1).
    # C2→C1 pipe: forwards all msgs to C1 (dst_ctr=ctr1),
    #             sends ACKs back to C2 (src_ctr=ctr2).
    log.info("Relay active: C1 <-> C2")
    await asyncio.gather(
        pipe(ws1, ws2, "C1→C2", dst_ctr=ctr2, src_ctr=ctr1,
             init_tracker=init_tracker, src_player_id=1),
        pipe(ws2, ws1, "C2→C1", dst_ctr=ctr1, src_ctr=ctr2,
             init_tracker=init_tracker, src_player_id=2),
    )
    log.info("Relay session ended")

    # Both clients disconnected. If a clean game end already fired the reward
    # trigger (CrownsReport or PassRound fallback), there is nothing to do.
    # Otherwise the session ended WITHOUT a known result (e.g. both clients
    # vanished mid-match): we deliberately do NOT grant phantom rewards here,
    # because we have no per-player serviceId or crown counts and would otherwise
    # write a bogus 2-crown win to the default data file. Just log it.
    if init_tracker.get('lobby_init_sent') and not init_tracker.get('game_finish_triggered'):
        game_id = init_tracker.get('game_id', 0)
        log.warning("Relay session ended with NO game-finish trigger "
                    "(no CrownsReport, no PassRound->Results) game_id=%d — "
                    "skipping reward grant (unknown result)", game_id)



async def _log_handshake(*args):
    """DIAGNOSTIC (additive, behavior-neutral): log the raw incoming WebSocket
    upgrade request so we can see WHY the websockets library 400s the game's
    handshake under wine (works on Windows, intermittently fails under wine —
    same server/library, so the difference is in the bytes the wine client
    sends). Returns None so the normal handshake proceeds unchanged.

    websockets changed the process_request signature across versions:
      * v10:        (path: str, request_headers: Headers)
      * v11/v12/v13:(connection, request)  where request has .path/.headers
    We accept *args and introspect to stay version-agnostic.
    """
    try:
        path = None
        headers = None
        if len(args) == 2:
            a, b = args
            # New API: (connection, request)
            if hasattr(b, "headers"):
                path = getattr(b, "path", None)
                headers = b.headers
            else:
                # Old API: (path, request_headers)
                path = a
                headers = b
        if headers is not None:
            try:
                hdr_items = headers.raw_items() if hasattr(headers, "raw_items") else list(headers.items())
            except Exception:
                hdr_items = list(headers.items()) if hasattr(headers, "items") else []
            log.info("[HANDSHAKE] path=%r from request", path)
            for k, v in hdr_items:
                log.info("[HANDSHAKE]   %s: %s", k, v)
        else:
            log.info("[HANDSHAKE] (could not introspect request; args=%d)", len(args))
    except Exception as e:
        log.warning("[HANDSHAKE] logging failed: %s", e)
    return None  # proceed with the normal handshake


async def main():
    log.info("Gwent relay+server starting on ws://%s:%d", HOST, PORT)

    # Clear any deck/playerinfo queues left over from a previous run so clients
    # that queue after the relay starts always write fresh entries at indices 0 and 1.
    for _qname in ("gwent_relay_deck_queue.json", "gwent_relay_playerinfo_queue.json"):
        _qp = os.path.join(tempfile.gettempdir(), _qname)
        try:
            if os.path.exists(_qp):
                os.remove(_qp)
                log.info("Cleared stale queue: %s", _qp)
        except Exception as _e:
            log.warning("Could not clear queue %s: %s", _qname, _e)

    deck_json = get_battle_deck_json()
    if deck_json:
        log.info("Deck pre-loaded (%d bytes)", len(deck_json))
    else:
        log.warning("No deck loaded from data.json -- games will have empty decks")
    async with serve(handler, HOST, PORT, ping_interval=None,
                     process_request=_log_handshake):
        log.info("Ready -- waiting for two clients")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
