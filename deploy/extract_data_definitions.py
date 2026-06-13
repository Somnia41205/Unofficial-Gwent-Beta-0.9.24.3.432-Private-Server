#!/usr/bin/env python3
"""
Extract the card data-definition files from YOUR OWN Gwent 0.9.24.3 client.

This project does NOT distribute CD PROJEKT RED's game data. The definition
files (Templates.xml, Abilities.xml, etc.) ship inside the game client, in a
zip at Gwent_Data/StreamingAssets/data_definitions. This script reads that zip
from a copy of the client you already own and unpacks the definitions into the
server's Data_definitions/ folder so the server can serve cards.

The data never leaves your machine - it's copied from your own install.

Usage:
    python extract_data_definitions.py
    python extract_data_definitions.py "D:\\path\\to\\Gwent The Witcher Card Game"
    python extract_data_definitions.py --out ./Data_definitions
"""

import os
import sys
import zipfile
import argparse

WANTED_XML = {
    "Templates.xml", "Abilities.xml", "CardAudio.xml", "Categories.xml",
    "Artists.xml", "Personalities.xml", "Summations.xml",
}

SEARCH_PATHS = [
    r"C:\Program Files (x86)\GOG Galaxy\Games\Gwent The Witcher Card Game",
    r"C:\Program Files\GOG Galaxy\Games\Gwent The Witcher Card Game",
    r"C:\GOG Games\Gwent The Witcher Card Game",
    r"D:\GOG Games\Gwent The Witcher Card Game",
    r"D:\Games\Gwent The Witcher Card Game",
    r"E:\GOG Games\Gwent The Witcher Card Game",
    r"C:\Program Files (x86)\Gwent The Witcher Card Game",
    os.path.expanduser("~/.wine/drive_c/GOG Games/Gwent The Witcher Card Game"),
    os.path.expanduser("~/Games/gwent/Gwent The Witcher Card Game"),
]


def is_client(path):
    return (os.path.isfile(os.path.join(path, "Gwent.exe"))
            and os.path.isfile(os.path.join(
                path, "Gwent_Data", "Managed", "Assembly-CSharp.dll")))


def find_client(explicit=None):
    if explicit:
        if is_client(explicit):
            return explicit
        sys.exit("ERROR: no valid Gwent install at: " + explicit +
                 "\n       (expected Gwent.exe and Gwent_Data/Managed/Assembly-CSharp.dll)")
    for p in SEARCH_PATHS:
        if is_client(p):
            return p
    return None


def definitions_zip(client_path):
    z = os.path.join(client_path, "Gwent_Data", "StreamingAssets", "data_definitions")
    if not os.path.isfile(z):
        sys.exit("ERROR: data_definitions not found at:\n       " + z +
                 "\n       Is this a full 0.9.24.3 install?")
    return z


def extract(zip_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    extracted, localization = [], 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            base = os.path.basename(name)
            if base in WANTED_XML and "/" not in name.strip("/"):
                with zf.open(name) as src:
                    data = src.read()
                with open(os.path.join(out_dir, base), "wb") as dst:
                    dst.write(data)
                extracted.append(base)
            elif name.lower().startswith("localization/") and name.lower().endswith(".csv"):
                target = os.path.join(out_dir, "Localization", os.path.basename(name))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                localization += 1
    return extracted, localization


def run_extraction(client_arg, out_dir):
    client = find_client(client_arg)
    if not client:
        sys.exit(
            "ERROR: could not auto-detect a Gwent 0.9.24.3 install.\n"
            "       Pass the path explicitly, e.g.:\n"
            '           --setup "C:\\path\\to\\Gwent The Witcher Card Game"\n'
            "       You must supply your own legally-obtained copy of the game.")
    print("[+] Client:      " + client)
    zpath = definitions_zip(client)
    print("[+] Definitions: " + zpath)
    print("[+] Output:      " + out_dir)
    xmls, loc = extract(zpath, out_dir)
    if "Templates.xml" not in xmls:
        sys.exit("ERROR: Templates.xml was not found inside data_definitions - "
                 "extraction incomplete. Is this the 0.9.24.3 client?")
    print("[+] Extracted %d definition XML(s): %s" % (len(xmls), ", ".join(sorted(xmls))))
    if loc:
        print("[+] Extracted %d localization file(s)." % loc)
    print("[+] Done. The server can now load card definitions.")
    return True


def main():
    ap = argparse.ArgumentParser(description="Extract Gwent card definitions from your own client.")
    ap.add_argument("client", nargs="?", help="Path to your Gwent install folder")
    default_out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Data_definitions")
    ap.add_argument("--out", default=default_out, help="Output folder")
    args = ap.parse_args()
    run_extraction(args.client, args.out)


if __name__ == "__main__":
    main()
