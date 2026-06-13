#!/usr/bin/env python3
"""
Local DNS proxy for Gwent Beta Private Server.

Resolves *.gog.com domains to the configured server IP.
Forwards all other queries to the real upstream DNS.

Usage:
    python dns_proxy.py --server-ip 192.168.1.100
    python dns_proxy.py --server-ip 192.168.1.100 --upstream 8.8.8.8

The machine's DNS must be pointed at 127.0.0.1 for this to take effect.
The launcher handles this automatically.
"""

import argparse
import socket
import struct
import sys

# Domains to redirect to the private server
REDIRECT_DOMAINS = {
    "gwent-quests.gog.com",
    "presence.gog.com",
    "seawolf-config.gog.com",
    "seawolf-deck.gog.com",
    "seawolf-inventory.gog.com",
    "seawolf-shop.gog.com",
    "seawolf-rankings.gog.com",
    "seawolf-profile.gog.com",
    "seawolf-rewards.gog.com",
    "seawolf-matchmaking.gog.com",
    "seawolf-games-log.gog.com",
    "remote-config.gog.com",
    "notifications-pusher.gog.com",
    "users.gog.com",
    "auth.gog.com",
}


def parse_dns_query(data):
    """Parse a DNS query packet, return (qname, qtype, qclass, question_end_offset)."""
    if len(data) < 12:
        return None, None, None, None
    # Skip header (12 bytes), parse question section
    offset = 12
    labels = []
    while offset < len(data):
        length = data[offset]
        if length == 0:
            offset += 1
            break
        offset += 1
        labels.append(data[offset:offset + length].decode("ascii", errors="replace"))
        offset += length
    qname = ".".join(labels)
    if offset + 4 > len(data):
        return qname, None, None, None
    qtype = struct.unpack("!H", data[offset:offset + 2])[0]
    qclass = struct.unpack("!H", data[offset + 2:offset + 4])[0]
    return qname, qtype, qclass, offset + 4


def build_response(query_data, qname, server_ip):
    """Build a DNS A-record response pointing qname to server_ip."""
    # Copy transaction ID and set response flags
    tid = query_data[:2]
    flags = b"\x81\x80"  # Standard response, no error
    qdcount = b"\x00\x01"
    ancount = b"\x00\x01"
    nscount = b"\x00\x00"
    arcount = b"\x00\x00"

    header = tid + flags + qdcount + ancount + nscount + arcount

    # Reconstruct the question section
    question = b""
    for label in qname.split("."):
        question += bytes([len(label)]) + label.encode("ascii")
    question += b"\x00"  # Root label
    question += b"\x00\x01"  # Type A
    question += b"\x00\x01"  # Class IN

    # Answer section (using name pointer to question)
    answer = b"\xc0\x0c"  # Pointer to name in question section
    answer += b"\x00\x01"  # Type A
    answer += b"\x00\x01"  # Class IN
    answer += struct.pack("!I", 60)  # TTL = 60 seconds
    answer += b"\x00\x04"  # RDLENGTH = 4
    answer += socket.inet_aton(server_ip)  # IP address

    return header + question + answer


def forward_query(data, upstream_dns, timeout=3.0):
    """Forward a DNS query to the upstream server and return the response."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(data, (upstream_dns, 53))
        response, _ = sock.recvfrom(4096)
        sock.close()
        return response
    except Exception:
        return None


def run_dns_proxy(server_ip, upstream_dns="8.8.8.8", listen_port=53, listen_addr="127.0.0.1"):
    """Run the DNS proxy server."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((listen_addr, listen_port))
    except PermissionError:
        print(f"[DNS] ERROR: Cannot bind to port {listen_port}. Run as Administrator.", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"[DNS] ERROR: Cannot bind to {listen_addr}:{listen_port}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[DNS] Proxy listening on {listen_addr}:{listen_port}")
    print(f"[DNS] Redirecting *.gog.com -> {server_ip}")
    print(f"[DNS] Upstream DNS: {upstream_dns}")

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            qname, qtype, qclass, qend = parse_dns_query(data)
            if qname is None:
                continue

            qname_lower = qname.lower()

            # Wildcard: redirect ANY *.gog.com (and bare gog.com) to our
            # server, matching the Windows launcher's blanket .gog.com
            # redirect. The explicit REDIRECT_DOMAINS set proved fragile
            # (each newly-seen gog subdomain -- presence, gwent-quests, ...
            # -- escaped to the real dead server, timed out, and the game
            # turned that into a ConnectionLost/"service interrupted").
            # A suffix match catches every gog host the game ever uses.
            if qname_lower == "gog.com" or qname_lower.endswith(".gog.com"):
                if qtype == 1:  # Type A — return our IP
                    response = build_response(data, qname, server_ip)
                    sock.sendto(response, addr)
                elif qend:
                    # Block AAAA (28) and other queries for our domains
                    # Return empty response (NXDOMAIN-like) to prevent IPv6 leaks
                    tid = data[:2]
                    flags = b"\x81\x80"  # Standard response, no error, 0 answers
                    empty_resp = tid + flags + data[4:6] + b"\x00\x00\x00\x00\x00\x00" + data[12:qend]
                    sock.sendto(empty_resp, addr)
            else:
                # Forward to upstream
                response = forward_query(data, upstream_dns)
                if response:
                    sock.sendto(response, addr)
        except Exception as e:
            print(f"[DNS] Error handling query: {e}")


def main():
    parser = argparse.ArgumentParser(description="Gwent Beta DNS Proxy")
    parser.add_argument("--server-ip", required=True, help="IP address of the Gwent private server")
    parser.add_argument("--upstream", default="8.8.8.8", help="Upstream DNS server (default: 8.8.8.8)")
    parser.add_argument("--port", type=int, default=53, help="Listen port (default: 53)")
    parser.add_argument("--bind", default="127.0.0.1", help="Listen address (default: 127.0.0.1)")
    args = parser.parse_args()

    run_dns_proxy(args.server_ip, args.upstream, args.port, args.bind)


if __name__ == "__main__":
    main()
