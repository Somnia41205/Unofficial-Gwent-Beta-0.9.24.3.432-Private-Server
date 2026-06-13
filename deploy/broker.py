import socket
import threading
import hashlib
import base64
import struct
import json
import time

CROWN_TIERS = [6, 18, 42, 66]

def get_crown_cap(crowns_today):
    for tier in CROWN_TIERS:
        if crowns_today < tier:
            return tier
    return CROWN_TIERS[-1]

# galaxy.protocols.webbroker_service proto:
#
#   enum MessageType {
#       UNKNOWN_MESSAGE         = 0;
#       AUTH_REQUEST            = 1;
#       AUTH_RESPONSE           = 2;
#       SUBSCRIBE_TOPIC_REQUEST = 3;
#       SUBSCRIBE_TOPIC_RESPONSE= 4;
#       MESSAGE_FROM_TOPIC      = 5;   <-- push notification type
#   }
#   message SubscribeTopicRequest  { optional string topic   = 1; }
#   message SubscribeTopicResponse { optional string topic   = 1; }  # echo topic, NOT a code
#   message MessageFromTopic {
#       optional string topic   = 1;
#       optional string content = 2;
#       optional uint64 id      = 3;   # notification id (NOT field100)
#   }
#
# The ack-n field100 in the GogPbConnection HEADER is the SDK echoing back the
# notification id from MessageFromTopic.id so the broker knows delivery was confirmed.

connected_clients = []         # list of (conn, subscribed_topics_set, user_id_str_or_None)
clients_lock      = threading.Lock()
server_seq        = 0          # broker's own outgoing sequence counter
server_seq_lock   = threading.Lock()
pending_notifications = []     # queued when gwent-client topic not yet subscribed
pending_lock      = threading.Lock()

_gg_sent_games = set()
_gg_sent_lock  = threading.Lock()

def push_good_game_notifications(payload, target_user_id=None):
    """Send the full GG reward flow: promise, currencies_added, good_game.
    If target_user_id is set, only push to that user's connection.
    If exclude_user_id is in payload, skip that user (the sender)."""
    game_id = payload.get("context", {}).get("game_id", 0)
    sender_name = payload.get("payload", {}).get("sender", {}).get("username", "")
    exclude_uid = payload.get("exclude_user_id")
    # Dedup by (game_id, sender_name) so both players can send GGs
    gg_key = (game_id, sender_name)
    with _gg_sent_lock:
        if gg_key in _gg_sent_games:
            print(f"[BROKER] GG already sent for game_id={game_id} sender={sender_name}, skipping")
            return
        _gg_sent_games.add(gg_key)
    sender  = payload.get("payload", {}).get("sender", {})

    notifications = [
        # 1) reward_promise for good_game_rewards (tells client to expect 1 currency)
        {
            "type":    "reward_promise",
            "context": {"game_id": game_id, "tag": "good_game_rewards"},
            "payload": {"items_added": 0, "currencies_added": 1, "cards_added": 0},
        },
        # 2) currencies_added with the +5 gold
        {
            "type":    "currencies_added",
            "context": {"game_id": game_id, "tag": "good_game_rewards"},
            "payload": {
                "currencies_added": [{"id": 1, "amount_added": 5, "amount_total": 0}]
            },
        },
        # 3) good_game with sender info
        {
            "type":    "good_game",
            "context": {"game_id": game_id},
            "payload": {"sender": sender},
        },
    ]

    # Send to targeted user, or all except excluded sender
    with clients_lock:
        if target_user_id:
            targets = [(conn, topics) for conn, topics, uid in connected_clients
                       if "gwent-client" in topics and uid == str(target_user_id)]
        elif exclude_uid:
            targets = [(conn, topics) for conn, topics, uid in connected_clients
                       if "gwent-client" in topics and uid != str(exclude_uid)]
        else:
            targets = [(conn, topics) for conn, topics, uid in connected_clients
                       if "gwent-client" in topics]

    if not targets:
        print(f"[BROKER] No subscribed clients for GG target={target_user_id} — queuing")
        for n in notifications:
            nid = _next_notif_id()
            n["id"] = nid
            frame = build_push_frame("gwent-client", json.dumps(n), nid)
            with pending_lock:
                pending_notifications.append((frame, target_user_id))
        return

    for target_conn, _ in targets:
        for n in notifications:
            nid = _next_notif_id()
            n_copy = dict(n)
            n_copy["id"] = nid
            json_str = json.dumps(n_copy)
            frame = build_push_frame("gwent-client", json_str, nid)
            print(f"[BROKER] GG push notif_id={nid}: {json_str[:120]}")
            try:
                send_ws_binary(target_conn, frame)
            except Exception as e:
                print(f"[BROKER] GG push send error: {e}")
        time.sleep(0.05)

# ── Protobuf helpers ──────────────────────────────────────────────────────────

def encode_varint(value):
    result = b""
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            result += bytes([bits | 0x80])
        else:
            result += bytes([bits])
            break
    return result

def encode_field_varint(field_num, value):
    return encode_varint((field_num << 3) | 0) + encode_varint(value)

def encode_field_bytes(field_num, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return encode_varint((field_num << 3) | 2) + encode_varint(len(value)) + value

def encode_field_uint64(field_num, value):
    # uint64 on the wire is the same as varint (wire type 0)
    return encode_field_varint(field_num, value)

def parse_varint(data, pos):
    result = 0
    shift  = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return result, pos

def parse_pb_fields(data):
    """Return dict of {field_num: value}. Bytes fields stay as bytes."""
    fields = {}
    pos = 0
    while pos < len(data):
        tag, pos = parse_varint(data, pos)
        field_num  = tag >> 3
        wire_type  = tag & 0x07
        if wire_type == 0:                      # varint
            val, pos = parse_varint(data, pos)
            fields[field_num] = val
        elif wire_type == 2:                    # length-delimited (bytes / string)
            length, pos = parse_varint(data, pos)
            val = data[pos:pos+length]
            pos += length
            fields[field_num] = val
        else:
            break   # unknown wire type — stop parsing
    return fields


# ── WebSocket helpers ─────────────────────────────────────────────────────────

def send_ws_binary(conn, payload_bytes):
    length = len(payload_bytes)
    if length <= 125:
        header = bytes([0x82, length])
    elif length <= 65535:
        header = bytes([0x82, 126]) + struct.pack(">H", length)
    else:
        header = bytes([0x82, 127]) + struct.pack(">Q", length)
    conn.sendall(header + payload_bytes)

def decode_ws_frames(buf):
    frames = []
    while len(buf) >= 2:
        opcode      = buf[0] & 0x0F
        masked      = (buf[1] & 0x80) != 0
        payload_len = buf[1] & 0x7F
        offset      = 2
        if payload_len == 126:
            if len(buf) < 4: break
            payload_len = struct.unpack(">H", buf[2:4])[0]
            offset = 4
        elif payload_len == 127:
            if len(buf) < 10: break
            payload_len = struct.unpack(">Q", buf[2:10])[0]
            offset = 10
        total = offset + (4 if masked else 0) + payload_len
        if len(buf) < total:
            break
        if masked:
            mask    = buf[offset:offset+4]
            offset += 4
            payload = bytes(buf[offset+i] ^ mask[i % 4] for i in range(payload_len))
        else:
            payload = buf[offset:offset+payload_len]
        frames.append((opcode, payload))
        buf = buf[total:]
    return frames, buf

def do_ws_handshake(conn, request_bytes):
    request = request_bytes.decode("utf-8", errors="replace")
    key = None
    path = ""
    for line in request.split("\r\n"):
        if line.startswith("GET "):
            path = line.split(" ")[1]
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    if not key:
        return False, ""
    magic  = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    accept = base64.b64encode(hashlib.sha1((key + magic).encode()).digest()).decode()
    conn.sendall((
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    ).encode())
    return True, path


# ── Frame builders ────────────────────────────────────────────────────────────

def _next_server_seq():
    global server_seq
    with server_seq_lock:
        seq = server_seq
        server_seq += 1
    return seq

def build_subscribe_response(request_oseq, topic):
    """Build a SUBSCRIBE_TOPIC_RESPONSE (type=4).

    Frame layout:  [2-byte header_len][header protobuf][content protobuf]

    Per gog.protocols.pb.proto, the Header is:
        field1 sort       (Subprotocol — always 2 / MESSAGE_SORT for webbroker)
        field2 type       (Message type — 4 for SUBSCRIBE_TOPIC_RESPONSE)
        field3 size       (Payload size in bytes)
        field4 oseq       (broker's own sequence number, increments per frame)
    plus Response extensions:
        field100 rseq     (the oseq of the request being responded to)
        field101 code     (default 200 OK)

    Content = SubscribeTopicResponse { string topic = 1; }
    """
    content = encode_field_bytes(1, topic)         # SubscribeTopicResponse.topic
    header = (
        encode_field_varint(1, 2)                 +  # sort = MESSAGE_SORT
        encode_field_varint(2, 4)                 +  # type = SUBSCRIBE_TOPIC_RESPONSE
        encode_field_varint(3, len(content))      +  # size = payload bytes
        encode_field_varint(4, _next_server_seq())+  # oseq
        encode_field_varint(100, request_oseq)    +  # rseq → request.oseq
        encode_field_varint(101, 200)                # code = OK
    )
    return int.to_bytes(len(header), 2, "big") + header + content

def build_push_frame(topic, json_str, notif_id):
    """Build a MESSAGE_FROM_TOPIC (type=5) push notification.

    Header:
        field1 sort = 2 (MESSAGE_SORT)
        field2 type = 5 (MESSAGE_FROM_TOPIC)
        field3 size = payload byte length
        field4 oseq = broker's incrementing sequence

    Content = MessageFromTopic:
        field1 topic    (e.g. "gwent-client")
        field2 content  (json payload)
        field3 id       (uint64 notification id)
    """
    content = (
        encode_field_bytes(1, topic)        +   # MessageFromTopic.topic
        encode_field_bytes(2, json_str)     +   # MessageFromTopic.content
        encode_field_uint64(3, notif_id)        # MessageFromTopic.id
    )
    header = (
        encode_field_varint(1, 2)                 +   # sort = MESSAGE_SORT
        encode_field_varint(2, 5)                 +   # type = MESSAGE_FROM_TOPIC
        encode_field_varint(3, len(content))      +   # size = payload bytes
        encode_field_varint(4, _next_server_seq())    # oseq
    )
    return int.to_bytes(len(header), 2, "big") + header + content


# ── Inbound frame handler ─────────────────────────────────────────────────────

def handle_pb_frame(conn, payload, client_type, subscribed_topics, connection_user_id=None):
    """Handle one binary protobuf broker frame from the client."""
    print(f"[BROKER] RX hex={payload.hex()} client={client_type}")
    try:
        if len(payload) < 2:
            return

        header_len  = int.from_bytes(payload[:2], "big")
        header_data = payload[2:2+header_len]
        content_data= payload[2+header_len:]

        # Header field meanings per gog.protocols.pb.proto:
        #   field1 sort   (subprotocol, MESSAGE_SORT=2 for webbroker)
        #   field2 type   (MessageType)
        #   field3 size   (payload length in bytes)
        #   field4 oseq   (sender's sequence number)
        #   field100 rseq (response → matches request.oseq, extension)
        h = parse_pb_fields(header_data)
        sort       = h.get(1, 0)
        msg_type   = h.get(2, 0)
        size       = h.get(3, 0)
        client_oseq= h.get(4, 0)
        rseq       = h.get(100, 0)

        print(f"[BROKER] HEADER sort={sort} type={msg_type} size={size} "
              f"oseq={client_oseq} rseq={rseq}")

        if msg_type == 5:
            # Anything inbound with type=5 must be an SDK ACK (ack-n capability).
            # rseq carries the notification oseq the SDK is acknowledging.
            print(f"[BROKER] SDK ACK for oseq={rseq}")
            return

        if msg_type == 1:
            # AUTH_REQUEST — auth token is in the URL for this SDK version,
            # so we just acknowledge with an empty AUTH_RESPONSE (type=2).
            auth_content = b""
            auth_header = (
                encode_field_varint(1, 2)                  +  # sort = MESSAGE_SORT
                encode_field_varint(2, 2)                  +  # AUTH_RESPONSE
                encode_field_varint(3, len(auth_content))  +  # size = 0
                encode_field_varint(4, _next_server_seq()) +  # oseq
                encode_field_varint(100, client_oseq)      +  # rseq = req.oseq
                encode_field_varint(101, 200)                 # code = OK
            )
            resp = int.to_bytes(len(auth_header), 2, "big") + auth_header + auth_content
            send_ws_binary(conn, resp)
            print(f"[BROKER] Sent AUTH_RESPONSE rseq={client_oseq}")
            return

        if msg_type == 3:
            # SUBSCRIBE_TOPIC_REQUEST
            c = parse_pb_fields(content_data)
            topic_bytes = c.get(1, b"")
            topic = topic_bytes.decode("utf-8", errors="replace") if isinstance(topic_bytes, bytes) else topic_bytes

            resp = build_subscribe_response(client_oseq, topic)
            send_ws_binary(conn, resp)
            print(f"[BROKER] Sent SUBSCRIBE_TOPIC_RESPONSE: client={client_type} topic={topic!r} rseq={client_oseq}")
            print(f"[BROKER] TX hex={resp.hex()}")

            subscribed_topics.add(topic)

            # If this is the gwent-client topic, flush any queued notifications
            if topic == "gwent-client" and client_type == "GwentSDK":
                with pending_lock:
                    # Filter: only flush notifications targeted at this user (or untargeted)
                    mine = []
                    remaining = []
                    for item in pending_notifications:
                        if isinstance(item, tuple):
                            frame, tuid = item
                            if tuid is None or str(tuid) == str(connection_user_id):
                                mine.append(frame)
                            else:
                                remaining.append(item)
                        else:
                            mine.append(item)  # legacy plain frame
                    pending_notifications[:] = remaining
                if mine:
                    print(f"[BROKER] Flushing {len(mine)} queued notifications for user={connection_user_id} in 1s...")
                    def _flush(c, frames):
                        time.sleep(1)
                        for frame in frames:
                            try:
                                send_ws_binary(c, frame)
                                print(f"[BROKER] Flushed queued frame")
                            except Exception as e:
                                print(f"[BROKER] Flush error: {e}")
                    threading.Thread(target=_flush, args=(conn, mine), daemon=True).start()
            return

        print(f"[BROKER] Unhandled msg_type={msg_type}")

    except Exception as e:
        import traceback
        print(f"[BROKER] handle_pb_frame error: {e}")
        traceback.print_exc()


# ── Notification push ─────────────────────────────────────────────────────────

# Seed from the current ms timestamp so IDs never collide across broker
# restarts. The client's GwentNotificationCache (Assembly-CSharp/GwentUnity/
# GwentNotificationCache.cs) silently drops any notification whose id it has
# seen before — if we restart the broker mid-session and reuse low ids, every
# subsequent reward notification is filtered out and the rewards screen sits
# on its loading spinner forever.
_notif_id_counter = int(time.time() * 1000)
_notif_id_lock    = threading.Lock()

def _next_notif_id():
    global _notif_id_counter
    with _notif_id_lock:
        nid = _notif_id_counter
        _notif_id_counter += 1
    return nid

def push_notification(notification_dict, target_user_id=None):
    """Send a single notification to subscribed GwentSDK clients.

    If target_user_id is specified, only push to that user's connection.
    Otherwise push to all subscribed clients (legacy behavior).

    notification_dict must already be the full notification object (will be
    JSON-serialised here).  The outer 'id' field is overridden with a
    monotonically increasing broker-assigned id so the values are consistent
    across the header field100 and MessageFromTopic.id.
    """
    notif_id = _next_notif_id()
    notification_dict = dict(notification_dict)   # shallow copy
    notification_dict["id"] = notif_id
    json_str = json.dumps(notification_dict)
    topic    = "gwent-client"

    frame = build_push_frame(topic, json_str, notif_id)
    print(f"[BROKER] Push notif_id={notif_id} target={target_user_id or 'ALL'}: {json_str[:100]}")
    print(f"[BROKER] TX hex={frame.hex()}")

    with clients_lock:
        if target_user_id:
            targets = [(conn, topics) for conn, topics, uid in connected_clients
                       if "gwent-client" in topics and uid == str(target_user_id)]
        else:
            targets = [(conn, topics) for conn, topics, uid in connected_clients
                       if "gwent-client" in topics]

    if targets:
        for conn, _ in targets:
            try:
                send_ws_binary(conn, frame)
            except Exception as e:
                print(f"[BROKER] Push send error: {e}")
    else:
        print(f"[BROKER] No subscribed clients for user={target_user_id or 'ALL'} — queuing notification")
        with pending_lock:
            pending_notifications.append((frame, target_user_id))


# ── Client handler ────────────────────────────────────────────────────────────

GWENT_CLIENT_ID = "48242550540196492"
COMM_SERVICE_ID = "46899977096215655"

def handle_client(conn, addr):
    print(f"[BROKER] Connected from {addr}")
    subscribed_topics = set()
    client_type       = "Unknown"
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk

        ok, path = do_ws_handshake(conn, data)
        if not ok:
            print("[BROKER] Handshake failed")
            return

        if GWENT_CLIENT_ID in path:
            client_type = "GwentSDK"
        elif COMM_SERVICE_ID in path:
            client_type = "CommService"

        print(f"[BROKER] Handshake OK: {addr} path={path!r} type={client_type}")

        # Extract user_id from path: /clients/{client_id}/users/{user_id}/push
        import re as _re
        _uid_match = _re.search(r"/users/(\d+)/", path)
        connection_user_id = _uid_match.group(1) if _uid_match else None
        if connection_user_id:
            print(f"[BROKER] User ID from path: {connection_user_id}")

        if client_type == "GwentSDK":
            with clients_lock:
                connected_clients.append((conn, subscribed_topics, connection_user_id))

            # The Galaxy SDK passes its auth token in the URL query string, so
            # we never see an AUTH_REQUEST. Send an unsolicited AUTH_RESPONSE
            # right after the handshake so the native dispatcher considers the
            # session "authenticated" before any subscribe traffic starts.
            try:
                auth_content = b""
                oseq = _next_server_seq()
                auth_header = (
                    encode_field_varint(1, 2)                 +  # sort = MESSAGE_SORT
                    encode_field_varint(2, 2)                 +  # AUTH_RESPONSE
                    encode_field_varint(3, len(auth_content)) +  # size = 0
                    encode_field_varint(4, oseq)              +  # oseq
                    encode_field_varint(101, 200)                # code = OK
                )
                resp = int.to_bytes(len(auth_header), 2, "big") + auth_header + auth_content
                send_ws_binary(conn, resp)
                print(f"[BROKER] Sent unsolicited AUTH_RESPONSE oseq={oseq}")
            except Exception as e:
                print(f"[BROKER] AUTH_RESPONSE send error: {e}")

        buf = b""
        while True:
            try:
                conn.settimeout(30.0)
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                frames, buf = decode_ws_frames(buf)
                for opcode, payload in frames:
                    if opcode == 0x9:               # ping → pong
                        conn.sendall(bytes([0x8A, 0x00]))
                    elif opcode == 0x8:             # close
                        print(f"[BROKER] CLOSE frame from {addr}")
                        return
                    elif opcode == 0x2:             # binary protobuf
                        handle_pb_frame(conn, payload, client_type, subscribed_topics, connection_user_id)
                    elif opcode == 0x1:
                        print(f"[BROKER] Text frame (unexpected): "
                              f"{payload.decode('utf-8', errors='replace')[:80]}")
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[BROKER] Recv error from {addr}: {e}")
                break
    except Exception as e:
        print(f"[BROKER] Client error from {addr}: {e}")
    finally:
        with clients_lock:
            connected_clients[:] = [(c, t, u) for c, t, u in connected_clients if c is not conn]
        try:
            conn.close()
        except Exception:
            pass
        print(f"[BROKER] Disconnected {addr}")


# ── Notification sequence ─────────────────────────────────────────────────────

def send_accomplishment_notifications(game_id=0, currencies=None, items=None,
                                       cards=None, currency_totals=None,
                                       xp_before=0, xp_after=0,
                                       level_before=1, level_after=1,
                                       cells_before=0, cells_after=0,
                                       crowns_before=0, crowns_after=0,
                                       wins_today=1, target_user_id=None):
    """Push the notifications that follow a casual multiplayer match.

    For multiplayer casual the outcome screen keys MatchRewardData by the
    numeric game_id from GameLogId (set via the GameFinished lobby event).
    MatchRewardManager.ProcessNotification routes by context.game_id when
    present, so all contexts must use {"game_id": game_id} — NOT {"type": ...}.

    The accomplishment_activated promise is intentionally omitted: it is only
    consumed by AppOutcomeAccomplishmentWaitingState, which is only created for
    challenge/holiday wins.  For casual multiplayer the outcome screen creates
    LevelState and RewardState only (SetupOutcomeScreenForRegularGame), so
    nobody watches AccomplishmentActivated.

    CheckAllRewardsCompletion requires three packs to complete:
      - ExperienceRewardPack    (profile_progress_reward promise + profile_progress)
      - CrownPiecesRewardPack   (tier_progress_reward promise + profile_progress)
      - GameFinishedSpecial     (game_finish_reward promise)
    """
    currencies      = currencies      or []
    items           = items           or []
    cards           = cards           or []
    currency_totals = currency_totals or {}

    ts  = int(time.time() * 1000)
    ctx = {"game_id": game_id}

    notifications = [
        {
            "id": ts,
            "type": "reward_promise",
            "context": dict(ctx, tag="profile_progress_reward"),
            "payload": {"items_added": 0, "currencies_added": 0, "cards_added": 0},
        },
        {
            "id": ts+1,
            "type": "reward_promise",
            "context": dict(ctx, tag="tier_progress_reward"),
            "payload": {"items_added": 0, "currencies_added": 0, "cards_added": 0},
        },
        {
            "id": ts+2,
            "type": "reward_promise",
            "context": dict(ctx, tag="game_finish_reward"),
            "payload": {"items_added": 0, "currencies_added": 0, "cards_added": 0},
        },
        {
            "id": ts+3,
            "type": "profile_progress",
            "context": ctx,
            "payload": {
                "level_change":         {"from": level_before,  "to": level_after},
                "experience_change":    {"from": xp_before,     "to": xp_after},
                "cells_change":         {"from": cells_before,  "to": cells_after},
                "crown_pieces":         {"from": crowns_before, "to": crowns_after},
                "win_of_day_number":    wins_today,
                "small_reward_reached": crowns_after >= 2,
                "tier_complete":        crowns_after >= get_crown_cap(crowns_before),
            },
        },
    ]

    # ── Currencies ──
    if currencies:
        notifications.append({
            "id": ts+4,
            "type": "currencies_added",
            "context": dict(ctx, tag="game_finish_reward"),
            "payload": {
                "currencies_added": [
                    {
                        "id":           c["id"],
                        "amount_added": c["amount"],
                        "amount_total": int(currency_totals.get(str(c["id"]), 0)),
                    }
                    for c in currencies
                ]
            },
        })

    # ── Items (kegs/vanity) ──
    if items:
        notifications.append({
            "id": ts+5,
            "type": "items_added",
            "context": dict(ctx, tag="game_finish_reward"),
            "payload": {
                "items_added": [
                    {
                        "id":              it["id"],
                        "item_definition": {"id": it["item_def_id"]},
                        "state":           "New",
                    }
                    for it in items
                ]
            },
        })

    # ── Cards ──
    if cards:
        notifications.append({
            "id": ts+6,
            "type": "cards_added",
            "context": dict(ctx, tag="game_finish_reward"),
            "payload": {
                "cards_added": [
                    {
                        "id": card.get("id", ts+6),
                        "card_definition": {
                            "id":               card["card_definition_id"],
                            "card_template_id": card["card_definition_id"] // 100,
                            "rarity":           card.get("rarity", 1),
                            "premium":          card.get("premium", False),
                            "is_deleted":       False,
                        },
                    }
                    for card in cards
                ]
            },
        })

    # The native Galaxy SDK occasionally drops pushes if they arrive while it
    # is still settling its subscription state. A small lead-in delay plus a
    # generous gap between frames makes dispatch deterministic in practice.
    time.sleep(0.25)
    for n in notifications:
        push_notification(n, target_user_id=target_user_id)
        time.sleep(0.2)


def send_challenge_accomplishment_notifications(
        acc_type, currencies=None, items=None, cards=None, currency_totals=None,
        xp_before=0, xp_after=0, level_before=1, level_after=1,
        cells_before=0, cells_after=0, crowns_before=0, crowns_after=0,
        wins_today=1, target_user_id=None):
    """Push the notifications that follow a CHALLENGE / holiday-event win.

    Unlike casual multiplayer (which keys MatchRewardData by the numeric
    game_id from GameFinished), the challenge outcome screen fetches its
    reward data via MatchRewardManager.GetRewardForAccomplishment(accomplishment)
    -- i.e. keyed by the accomplishment NAME. On the wire that means the
    notification Context must carry {"type": <accomplishment>} and NO game_id,
    so MatchRewardManager.ProcessNotification routes by Context.Type.

    Crucially, AppOutcomeAccomplishmentState sits in its WaitingState until the
    AccomplishmentRewardPack (RewardSource.AccomplishmentActivated) completes.
    That pack only completes when it receives a reward_promise tagged
    "accomplishment_activated" (plus matching *_added notifications if the
    promise declares any goods). Omitting it -- as the casual path does -- is
    exactly what left challenge screens hanging. So we always emit the
    accomplishment_activated promise here, declaring the goods counts, and then
    the matching currencies/items/cards notifications.

    We also emit the Experience (profile_progress_reward) and CrownPieces
    (tier_progress_reward) packs plus the profile_progress payload, all keyed by
    the same {"type": acc_type} context, because the challenge outcome screen
    also creates a LevelState that consumes them.
    """
    currencies      = currencies      or []
    items           = items           or []
    cards           = cards           or []
    currency_totals = currency_totals or {}

    ts  = int(time.time() * 1000)
    ctx = {"type": acc_type}

    n_curr = len(currencies)
    n_item = len(items)
    n_card = len(cards)

    notifications = [
        # Experience + CrownPieces packs (same as casual, but keyed by type).
        {
            "id": ts,
            "type": "reward_promise",
            "context": dict(ctx, tag="profile_progress_reward"),
            "payload": {"items_added": 0, "currencies_added": 0, "cards_added": 0},
        },
        {
            "id": ts+1,
            "type": "reward_promise",
            "context": dict(ctx, tag="tier_progress_reward"),
            "payload": {"items_added": 0, "currencies_added": 0, "cards_added": 0},
        },
        # The accomplishment_activated promise -- THIS is what the challenge
        # outcome screen's WaitingState blocks on. Declare the goods counts so
        # AccomplishmentRewardPack.IsComplete() knows how many *_added to expect.
        {
            "id": ts+2,
            "type": "reward_promise",
            "context": dict(ctx, tag="accomplishment_activated"),
            "payload": {
                "items_added":      n_item,
                "currencies_added": n_curr,
                "cards_added":      n_card,
            },
        },
        # GameFinishedSpecial promise (kept for parity with the casual flow).
        {
            "id": ts+3,
            "type": "reward_promise",
            "context": dict(ctx, tag="game_finish_reward"),
            "payload": {"items_added": 0, "currencies_added": 0, "cards_added": 0},
        },
        {
            "id": ts+4,
            "type": "profile_progress",
            "context": ctx,
            "payload": {
                "level_change":         {"from": level_before,  "to": level_after},
                "experience_change":    {"from": xp_before,     "to": xp_after},
                "cells_change":         {"from": cells_before,  "to": cells_after},
                "crown_pieces":         {"from": crowns_before, "to": crowns_after},
                "win_of_day_number":    wins_today,
                "small_reward_reached": crowns_after >= 2,
                "tier_complete":        crowns_after >= get_crown_cap(crowns_before),
            },
        },
    ]

    # The accomplishment reward goods, tagged accomplishment_activated so they
    # land in the AccomplishmentRewardPack and complete it.
    if currencies:
        notifications.append({
            "id": ts+5,
            "type": "currencies_added",
            "context": dict(ctx, tag="accomplishment_activated"),
            "payload": {
                "currencies_added": [
                    {
                        "id":           c["id"],
                        "amount_added": c["amount"],
                        "amount_total": int(currency_totals.get(str(c["id"]), 0)),
                    }
                    for c in currencies
                ]
            },
        })
    if items:
        notifications.append({
            "id": ts+6,
            "type": "items_added",
            "context": dict(ctx, tag="accomplishment_activated"),
            "payload": {
                "items_added": [
                    {
                        "id":              it["id"],
                        "item_definition": {"id": it["item_def_id"]},
                        "state":           "New",
                    }
                    for it in items
                ]
            },
        })
    if cards:
        notifications.append({
            "id": ts+7,
            "type": "cards_added",
            "context": dict(ctx, tag="accomplishment_activated"),
            "payload": {
                "cards_added": [
                    {
                        "id": card.get("id", ts+7),
                        "card_definition": {
                            "id":               card["card_definition_id"],
                            "card_template_id": card["card_definition_id"] // 100,
                            "rarity":           card.get("rarity", 1),
                            "premium":          card.get("premium", False),
                            "is_deleted":       False,
                        },
                    }
                    for card in cards
                ]
            },
        })

    time.sleep(0.25)
    for n in notifications:
        push_notification(n, target_user_id=target_user_id)
        time.sleep(0.2)


# ── Trigger server ────────────────────────────────────────────────────────────

import sys as _sys
WS_PORT      = int(_sys.argv[1]) if len(_sys.argv) > 1 else 8445
TRIGGER_PORT = int(_sys.argv[2]) if len(_sys.argv) > 2 else 8446

def push_friends_notification(target_user_id, notification_data):
    """Push a notification on the 'friends' topic to a specific user."""
    try:
        nid = _next_notif_id()
        notification_data["id"] = nid
        json_str = json.dumps(notification_data)
        frame = build_push_frame("friends", json_str, nid)
        print(f"[BROKER] Pushing friends notification to user {target_user_id}: {json_str[:200]}", flush=True)
        with clients_lock:
            targets = [(conn, topics) for conn, topics, uid in connected_clients
                       if "friends" in topics and uid == str(target_user_id)]
            print(f"[BROKER] Friends topic lookup: {len(targets)} targets found, connected={len(connected_clients)}", flush=True)
            for c, t, u in connected_clients:
                print(f"[BROKER]   client uid={u} topics={t}", flush=True)
        if not targets:
            print(f"[BROKER] No client subscribed to 'friends' for user {target_user_id} — queuing", flush=True)
            with pending_lock:
                pending_notifications.append((frame, str(target_user_id)))
            return
        for target_conn, _ in targets:
            try:
                send_ws_binary(target_conn, frame)
                print(f"[BROKER] Sent friends notification to user {target_user_id}", flush=True)
            except Exception as e:
                print(f"[BROKER] Failed to send friends notification: {e}", flush=True)
    except Exception as e:
        import traceback
        print(f"[BROKER] push_friends_notification CRASH: {e}", flush=True)
        traceback.print_exc()


def notification_trigger_server():
    """Tiny HTTP server that server.py POSTs to after a match ends.
    Body JSON: {"game_id": <int>, ...xp/crown fields...}
    game_id must match the numeric GameServiceID the relay sent via GameFinished."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", TRIGGER_PORT))
    s.listen(5)
    print(f"[BROKER] Trigger server on 0.0.0.0:{TRIGGER_PORT}")
    while True:
        conn, addr = s.accept()
        print(f"[BROKER] Trigger connection from {addr}")
        try:
            conn.settimeout(5.0)
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" not in data:
                    continue
                header_part, body_part = data.split(b"\r\n\r\n", 1)
                content_length = 0
                for line in header_part.decode("utf-8", errors="replace").split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip())
                        break
                if len(body_part) >= content_length:
                    break
            _, body_bytes = data.split(b"\r\n\r\n", 1)
            payload = json.loads(body_bytes.decode("utf-8"))

            # Route by notification type when present (e.g. good_game from GG endpoint).
            notif_type = payload.get("type", "")
            acc_type   = payload.get("acc_type", "")
            target_uid = payload.get("target_user_id")  # optional: target a specific user
            if notif_type == "friend_invite":
                print(f"[BROKER] Trigger friend_invite target={target_uid}: {payload}")
                threading.Thread(
                    target=push_friends_notification,
                    args=(target_uid, payload.get("notification", {})),
                    daemon=True,
                ).start()
            elif notif_type == "good_game":
                print(f"[BROKER] Trigger good_game target={target_uid}: {payload}")
                threading.Thread(
                    target=push_good_game_notifications,
                    args=(payload, target_uid),
                    daemon=True,
                ).start()
            elif acc_type:
                # CHALLENGE / holiday-event win from /users/{id}/accomplishments.
                # These are keyed by accomplishment NAME (Context.Type), not
                # game_id, and MUST include the accomplishment_activated pack or
                # the challenge outcome screen hangs in its waiting state.
                currencies      = payload.get("currencies", []) or []
                items           = payload.get("items",      []) or []
                cards           = payload.get("cards",      []) or []
                currency_totals = payload.get("currency_totals", {}) or {}
                xp_before       = payload.get("xp_before",    0)
                xp_after        = payload.get("xp_after",     0)
                level_before    = payload.get("level_before", 1)
                level_after     = payload.get("level_after",  1)
                cells_before    = payload.get("cells_before", 0)
                cells_after     = payload.get("cells_after",  0)
                crowns_before   = payload.get("crowns_before", 0)
                crowns_after    = payload.get("crowns_after",  0)
                wins_today      = payload.get("wins_today",    1)
                print(f"[BROKER] Trigger accomplishment={acc_type!r} target={target_uid} "
                      f"currencies={currencies} items={items} cards={cards} "
                      f"xp={xp_before}->{xp_after} level={level_before}->{level_after} "
                      f"crowns={crowns_before}->{crowns_after} wins_today={wins_today}")
                threading.Thread(
                    target=send_challenge_accomplishment_notifications,
                    args=(acc_type, currencies, items, cards, currency_totals,
                          xp_before, xp_after, level_before, level_after,
                          cells_before, cells_after, crowns_before, crowns_after,
                          wins_today, target_uid),
                    daemon=True,
                ).start()
            else:
                # Standard post-match reward trigger from /internal/game_finish.
                game_id         = int(payload.get("game_id", 0) or 0)
                currencies      = payload.get("currencies", []) or []
                items           = payload.get("items",      []) or []
                cards           = payload.get("cards",      []) or []
                currency_totals = payload.get("currency_totals", {}) or {}
                xp_before       = payload.get("xp_before",    0)
                xp_after        = payload.get("xp_after",     0)
                level_before    = payload.get("level_before", 1)
                level_after     = payload.get("level_after",  1)
                cells_before    = payload.get("cells_before", 0)
                cells_after     = payload.get("cells_after",  0)
                crowns_before   = payload.get("crowns_before", 0)
                crowns_after    = payload.get("crowns_after",  0)
                wins_today      = payload.get("wins_today",    1)
                print(f"[BROKER] Trigger game_id={game_id} target={target_uid} "
                      f"currencies={currencies} items={items} cards={cards} "
                      f"xp={xp_before}->{xp_after} level={level_before}->{level_after} "
                      f"crowns={crowns_before}->{crowns_after} wins_today={wins_today}")
                threading.Thread(
                    target=send_accomplishment_notifications,
                    args=(game_id, currencies, items, cards, currency_totals,
                          xp_before, xp_after, level_before, level_after,
                          cells_before, cells_after, crowns_before, crowns_after,
                          wins_today, target_uid),
                    daemon=True,
                ).start()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        except Exception as e:
            print(f"[BROKER] Trigger error: {e}")
            try:
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            except Exception:
                pass
        finally:
            conn.close()


# ── Main ──────────────────────────────────────────────────────────────────────

broker_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
broker_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
broker_sock.bind(("0.0.0.0", WS_PORT))
broker_sock.listen(5)
print(f"[BROKER] WebSocket broker on 0.0.0.0:{WS_PORT}")

threading.Thread(target=notification_trigger_server, daemon=True).start()

while True:
    conn, addr = broker_sock.accept()
    threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
