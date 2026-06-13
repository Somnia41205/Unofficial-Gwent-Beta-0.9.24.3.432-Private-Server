"""
Minimal GalaxyCommunication service replacement.

Listens on TCP port 9977 and speaks the GOG Galaxy protobuf wire protocol.
Handles only AUTH_INFO_REQUEST (sort=1, type=3) — returns a fake
AUTH_INFO_RESPONSE with user_id/username from users.json and a refresh
token that embeds the user_id so downstream services can identify the user.

Multi-user: assigns users round-robin from users.json. Each new game client
connection gets the next user in the list. The user_id is embedded in the
refresh token as "privateserver_{user_id}" so auth.gog.com/token (server.py)
can return the correct identity.

Wire format per message:
  [2-byte BE header_length] [Header protobuf] [Payload protobuf]

Header fields: sort(1), type(2), size(3), oseq(4), extensions: rseq(100), code(101)
AuthInfoResponse fields: refresh_token(1), environment_type(2), user_id(3 fixed64), user_name(4), region(5)
"""

import struct
import socketserver
import json
import threading
import os

# Path to users.json. The launcher overrides this at runtime
# (comm.USERS_FILE = USERS_JSON). Env override: GWENT_USERS_FILE.
# Default: users.json next to this script.
USERS_FILE = os.environ.get(
    "GWENT_USERS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json"),
)

# Round-robin user assignment
_user_index = 0
_user_lock = threading.Lock()

# ── Minimal protobuf encoding helpers ────────────────────────────────────────
# Only what we need: varint, length-delimited (string/bytes), fixed64

def _encode_varint(value):
    """Encode an unsigned varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)

def _encode_field_varint(field_number, value):
    """Encode a varint field (wire type 0)."""
    tag = (field_number << 3) | 0
    return _encode_varint(tag) + _encode_varint(value)

def _encode_field_string(field_number, value):
    """Encode a length-delimited field (wire type 2)."""
    tag = (field_number << 3) | 2
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return _encode_varint(tag) + _encode_varint(len(encoded)) + encoded

def _encode_field_fixed64(field_number, value):
    """Encode a fixed64 field (wire type 1)."""
    tag = (field_number << 3) | 1
    return _encode_varint(tag) + struct.pack("<Q", value)

# ── Protobuf decoding helpers (minimal, for Header) ─────────────────────────

def _decode_varint(data, pos):
    """Decode a varint, return (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, pos

def _decode_header(data):
    """Decode Header protobuf fields we care about."""
    fields = {}
    pos = 0
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            value, pos = _decode_varint(data, pos)
            fields[field_number] = value
        elif wire_type == 1:  # fixed64
            value = struct.unpack_from("<Q", data, pos)[0]
            pos += 8
            fields[field_number] = value
        elif wire_type == 2:  # length-delimited
            length, pos = _decode_varint(data, pos)
            fields[field_number] = data[pos:pos + length]
            pos += length
        elif wire_type == 5:  # fixed32
            value = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            fields[field_number] = value
        else:
            break  # unknown wire type, stop
    return fields

# ── Build response frames ────────────────────────────────────────────────────

def _build_header(sort, msg_type, payload_size, oseq=None, rseq=None, code=200):
    """Build a Header protobuf."""
    buf = b""
    buf += _encode_field_varint(1, sort)       # sort
    buf += _encode_field_varint(2, msg_type)   # type
    buf += _encode_field_varint(3, payload_size)  # size
    if oseq is not None:
        buf += _encode_field_varint(4, oseq)   # oseq
    if rseq is not None:
        buf += _encode_field_varint(100, rseq)  # rseq (extension)
    if code is not None:
        buf += _encode_field_varint(101, code)  # code (extension)
    return buf

def _build_frame(header_bytes, payload_bytes):
    """Wrap header + payload into a wire frame."""
    header_len = len(header_bytes)
    return struct.pack(">H", header_len) + header_bytes + payload_bytes

def _idtype_user(raw_id):
    """Apply GOG Galaxy IDType::User encoding: top byte = 0x02."""
    return (2 << 56) | (raw_id & 0x00FFFFFFFFFFFFFF)

def build_auth_info_response(user_id_int, username, refresh_token, rseq=None):
    """Build a complete AUTH_INFO_RESPONSE frame."""
    # AuthInfoResponse: refresh_token(1), environment_type(2), user_id(3 fixed64), user_name(4), region(5)
    # user_id must be wrapped with IDType::User encoding (top byte = 0x02)
    payload = b""
    payload += _encode_field_string(1, refresh_token)
    payload += _encode_field_varint(2, 0)  # ENVIRONMENT_PRODUCTION = 0
    payload += _encode_field_fixed64(3, _idtype_user(user_id_int))
    payload += _encode_field_string(4, username)
    payload += _encode_field_varint(5, 0)  # REGION_WORLD_WIDE = 0

    header = _build_header(
        sort=1,
        msg_type=4,  # AUTH_INFO_RESPONSE
        payload_size=len(payload),
        rseq=rseq,
        code=200
    )
    return _build_frame(header, payload)

def build_overlay_state_notification():
    """Build OVERLAY_STATE_CHANGE_NOTIFICATION (type=58) with state=INITIALIZED(3)."""
    payload = _encode_field_varint(1, 3)  # OVERLAY_STATE_INITIALIZED = 3
    header = _build_header(
        sort=1,
        msg_type=58,  # OVERLAY_STATE_CHANGE_NOTIFICATION
        payload_size=len(payload),
        code=None
    )
    return _build_frame(header, payload)

# ── Load user config ─────────────────────────────────────────────────────────

def load_users():
    """Load the user list from users.json."""
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[COMM] Warning: could not load {USERS_FILE}: {e}")
        return [{"id": "50069134988124048", "username": "Player"}]

def get_next_user():
    """Assign the next user round-robin from users.json."""
    global _user_index
    users = load_users()
    if not users:
        return {"id": 50069134988124048, "username": "Player"}
    with _user_lock:
        user = users[_user_index % len(users)]
        _user_index += 1
    return {
        "id": int(user.get("id", "50069134988124048")),
        "username": user.get("username", "Player"),
    }

# ── TCP handler ──────────────────────────────────────────────────────────────

class CommServiceHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = f"{self.client_address[0]}:{self.client_address[1]}"
        print(f"[COMM] Client connected: {client}")

        sock = self.request
        assigned_user = None  # Assigned once on first AUTH_INFO_REQUEST
        try:
            while True:
                # Read 2-byte header length
                h_len_buf = self._recv_exact(2)
                if not h_len_buf:
                    break
                h_len = struct.unpack(">H", h_len_buf)[0]

                # Read header protobuf
                h_buf = self._recv_exact(h_len)
                if not h_buf:
                    break
                header = _decode_header(h_buf)

                sort = header.get(1, 0)
                msg_type = header.get(2, 0)
                payload_size = header.get(3, 0)
                oseq = header.get(4)

                # Read payload
                p_buf = b""
                if payload_size > 0:
                    p_buf = self._recv_exact(payload_size)
                    if not p_buf:
                        break

                print(f"[COMM] Received: sort={sort} type={msg_type} size={payload_size} oseq={oseq}")

                if sort == 1 and msg_type == 3:
                    # AUTH_INFO_REQUEST → AUTH_INFO_RESPONSE
                    # Assign user once per connection, reuse on retries
                    if assigned_user is None:
                        assigned_user = get_next_user()
                    user_cfg = assigned_user
                    print(f"[COMM] AUTH_INFO_REQUEST → responding with user_id={user_cfg['id']} username={user_cfg['username']}")

                    # Send overlay state notification first (like Comet does)
                    overlay_frame = build_overlay_state_notification()
                    sock.sendall(overlay_frame)

                    # Embed user_id in refresh token so auth.gog.com/token can identify the user
                    fake_refresh = f"privateserver_{user_cfg['id']}"
                    response = build_auth_info_response(
                        user_id_int=user_cfg["id"],
                        username=user_cfg["username"],
                        refresh_token=fake_refresh,
                        rseq=oseq
                    )
                    sock.sendall(response)

                elif sort == 1 and msg_type == 1:
                    # LIBRARY_INFO_REQUEST → LIBRARY_INFO_RESPONSE (stub)
                    # Return empty location, UPDATE_COMPLETE
                    payload = _encode_field_string(1, "") + _encode_field_varint(2, 3)
                    h = _build_header(sort=1, msg_type=2, payload_size=len(payload), rseq=oseq, code=200)
                    sock.sendall(_build_frame(h, payload))
                    print(f"[COMM] LIBRARY_INFO_REQUEST → stub response")

                elif sort == 1 and msg_type == 49:
                    # START_GAME_SESSION_REQUEST → START_GAME_SESSION_RESPONSE (stub)
                    payload = b""
                    h = _build_header(sort=1, msg_type=50, payload_size=0, rseq=oseq, code=200)
                    sock.sendall(_build_frame(h, payload))
                    print(f"[COMM] START_GAME_SESSION_REQUEST → stub response")

                elif sort in (1, 2):
                    # Unknown message on a REQUEST/RESPONSE subprotocol:
                    #   sort=1 CommunicationService, sort=2 webbroker.
                    # These expect a reply. The SDK marks them as requests with a
                    # timeout (e.g. webbroker SUBSCRIBE_TOPIC sort=2 type=3 wants
                    # sort=2 type=4) and matches the response by requestId, so we
                    # send an empty OK with type+1 (which is the correct response
                    # type for the request/response pairs: 3->4, etc.).
                    resp_type = msg_type + 1
                    h = _build_header(sort=sort, msg_type=resp_type, payload_size=0, rseq=oseq, code=200)
                    sock.sendall(_build_frame(h, b""))
                    print(f"[COMM] Unknown sort={sort} type={msg_type} → stub OK (type={resp_type})")

                else:
                    # OVERLAY subprotocols multiplexed over 9977:
                    #   sort=3 overlay-for-service, sort=6 overlay-for-peer,
                    #   sort=7 overlay-for-client.
                    # These carry NOTIFICATIONS (e.g. sort=6 type=6
                    # OVERLAY_INITIALIZED), which the real GalaxyCommunication /
                    # comet do NOT respond to (comet returns Ignored => writes
                    # nothing). Our old catch-all replied type+1 to EVERYTHING,
                    # so it sent an unsolicited sort=6 type=7 back; the SDK has
                    # no handler for overlay type 7, logs "Failed to handle
                    # request: sort=6, type=7", and tears down the 9977 + broker
                    # connections ~1s later ("service interrupted"). Intermittent
                    # because the SDK only emits sort=6 type=6 on some launches
                    # (overlay init timing). FIX: stay SILENT on overlay sorts —
                    # do NOT reply. See GalaxyPeer.236_serviceinterrupted.log.
                    print(f"[COMM] Overlay notification sort={sort} type={msg_type} → ignored (no reply, matches comet)")

        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except Exception as e:
            print(f"[COMM] Error handling client: {e}")
        finally:
            print(f"[COMM] Client disconnected: {client}")

    def _recv_exact(self, n):
        """Receive exactly n bytes."""
        data = bytearray()
        while len(data) < n:
            chunk = self.request.recv(n - len(data))
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data)


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ── Windows Service support ─────────────────────────────────────────────────
# When run with `--install`, registers as the "GalaxyCommunication" service.
# When run with `--uninstall`, removes it.
# When started by the SCM (no args, or via `net start`), runs as a service.
# When run with `--standalone` (or no special args in a console), runs normally.

_SERVICE_NAME = "GalaxyCommunication"
_SERVICE_DISPLAY = "GalaxyCommunication"
_SERVICE_DESC = "Gwent Beta private server GalaxyCommunication replacement"

def _run_server():
    """Start the TCP server (shared by both service and standalone modes)."""
    srv = ThreadedTCPServer(("127.0.0.1", 9977), CommServiceHandler)
    return srv

def _try_service_mode():
    """Attempt to run as a Windows service using pywin32. Returns False if
    pywin32 is not installed or we're not being launched by the SCM."""
    try:
        import win32serviceutil
        import win32service
        import win32event
        import servicemanager
    except ImportError:
        return False

    class GalaxyCommService(win32serviceutil.ServiceFramework):
        _svc_name_ = _SERVICE_NAME
        _svc_display_name_ = _SERVICE_DISPLAY
        _svc_description_ = _SERVICE_DESC

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.server = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)
            if self.server:
                self.server.shutdown()

        def SvcDoRun(self):
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                                  servicemanager.PYS_SERVICE_STARTED,
                                  (self._svc_name_, ''))
            self.server = _run_server()
            # Run in a thread so we can wait for stop event
            t = threading.Thread(target=self.server.serve_forever, daemon=True)
            t.start()
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
            self.server.shutdown()

    return GalaxyCommService


if __name__ == "__main__":
    import sys

    if "--install" in sys.argv:
        # Install as Windows service
        try:
            import win32serviceutil
            import win32service
            # Find the Python executable
            python_exe = sys.executable
            script_path = os.path.abspath(__file__)

            # Remove any existing service first
            try:
                win32serviceutil.RemoveService(_SERVICE_NAME)
            except Exception:
                pass

            # Get the service class
            SvcClass = _try_service_mode()
            if not SvcClass:
                print("[COMM] ERROR: pywin32 is required. Install with: pip install pywin32")
                sys.exit(1)

            win32serviceutil.InstallService(
                SvcClass._svc_reg_class_ if hasattr(SvcClass, '_svc_reg_class_') else
                f"{os.path.splitext(script_path)[0]}.{SvcClass.__name__}",
                _SERVICE_NAME,
                _SERVICE_DISPLAY,
                startType=win32service.SERVICE_DEMAND_START,
                exeName=python_exe,
                exeArgs=f'"{script_path}"',
                description=_SERVICE_DESC,
            )
            print(f"[COMM] Service '{_SERVICE_NAME}' installed successfully.")
            print(f"[COMM] Start with: net start {_SERVICE_NAME}")
        except ImportError:
            print("[COMM] ERROR: pywin32 is required. Install with: pip install pywin32")
            sys.exit(1)
        except Exception as e:
            print(f"[COMM] Install failed: {e}")
            # Fallback: use sc create with pythonservice.exe
            import traceback; traceback.print_exc()
            sys.exit(1)

    elif "--uninstall" in sys.argv:
        try:
            import win32serviceutil
            win32serviceutil.RemoveService(_SERVICE_NAME)
            print(f"[COMM] Service '{_SERVICE_NAME}' removed.")
        except ImportError:
            # Fallback to sc delete
            os.system(f'sc delete {_SERVICE_NAME}')
            print(f"[COMM] Service '{_SERVICE_NAME}' removed via sc.")
        except Exception as e:
            print(f"[COMM] Uninstall failed: {e}")

    elif "--standalone" in sys.argv or sys.stdin is not None and sys.stdin.isatty():
        # Running from a console — standalone mode
        server = _run_server()
        print("[COMM] GalaxyCommunication replacement listening on 127.0.0.1:9977")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("[COMM] Shutting down")
            server.shutdown()

    else:
        # Launched by SCM — try service mode
        SvcClass = _try_service_mode()
        if SvcClass:
            try:
                import servicemanager
                import win32serviceutil
                win32serviceutil.HandleCommandLine(SvcClass)
            except Exception:
                # Fallback to standalone if service dispatch fails
                server = _run_server()
                server.serve_forever()
        else:
            # pywin32 not available, just run standalone
            server = _run_server()
            print("[COMM] GalaxyCommunication replacement listening on 127.0.0.1:9977")
            server.serve_forever()
