#!/usr/bin/env python3
r"""
DW2003 Indonesian Patcher v1.0 manifest builder.

Put these files in ONE folder:
  all_strings.xlsx
  logo.png
  dw2003_patcher_standalone_v100_template.py
  build_manifest_v100.py
  build_exe_v100.bat

The builder:
1. Reads all non-empty translations from all_strings.xlsx.
2. Finds the matching original language resource in the raw disc dump.
3. Scans the original BIN once to discover every copy/LBA of those resources.
4. Reads the compiled translated LANG files from ddw3/build.
5. Stores expected-English + Indonesian blobs for conflict-safe patching.
6. Writes dw2003_patcher_standalone_v100_built.py without modifying the template.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import pickle
import re
import struct
import sys
import zipfile
import zlib
from collections import defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
XLSX = HERE / "all_strings.xlsx"
TEMPLATE = HERE / "dw2003_patcher_standalone_v100_template.py"
BUILT = HERE / "dw2003_patcher_standalone_v100_built.py"

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

HEADER_ALIASES = {
    "relpath": ["relpath"],
    "index": ["index"],
    "translation": [
        "translation",
        "translation (isi ini)",
        "terjemahan",
        "terjemahan (isi ini)",
    ],
}


def norm_header(s):
    return " ".join(str(s).strip().lower().split())


def find_header(headers, logical_name):
    normalized = [norm_header(h) for h in headers]

    for alias in HEADER_ALIASES[logical_name]:
        alias = norm_header(alias)
        if alias in normalized:
            return normalized.index(alias)

    return None


def col_index(ref):
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0

    for c in letters:
        n = n * 26 + (ord(c) - 64)

    return n - 1


def read_xlsx_rows(path):
    with zipfile.ZipFile(path) as z:
        shared = []

        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))

            for si in root.findall("m:si", NS):
                shared.append(
                    "".join(
                        t.text or ""
                        for t in si.iterfind(".//m:t", NS)
                    )
                )

        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))

        relmap = {
            r.attrib["Id"]: r.attrib["Target"]
            for r in rels
        }

        sheet = wb.find("m:sheets/m:sheet", NS)

        if sheet is None:
            raise RuntimeError("Workbook tidak memiliki worksheet.")

        rid = sheet.attrib[
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        ]

        target = relmap[rid]

        if not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")

        root = ET.fromstring(z.read(target))
        rows = []

        for row in root.findall(".//m:sheetData/m:row", NS):
            vals = {}

            for c in row.findall("m:c", NS):
                ref = c.attrib.get("r", "A1")
                idx = col_index(ref)
                typ = c.attrib.get("t")

                if typ == "inlineStr":
                    isel = c.find("m:is", NS)
                    val = (
                        ""
                        if isel is None
                        else "".join(
                            t.text or ""
                            for t in isel.iterfind(".//m:t", NS)
                        )
                    )
                else:
                    v = c.find("m:v", NS)
                    raw = "" if v is None or v.text is None else v.text

                    if typ == "s" and raw != "":
                        val = shared[int(raw)]
                    else:
                        val = raw

                vals[idx] = val

            if vals:
                maxidx = max(vals)
                rows.append(
                    [vals.get(i, "") for i in range(maxidx + 1)]
                )

        return rows


def parse_pack(data):
    first = struct.unpack_from("<I", data, 0)[0]

    if not first or first % 4 or first > len(data):
        raise RuntimeError("Format PACK original tidak dikenali.")

    n = first // 4
    offs = list(struct.unpack_from(f"<{n}I", data, 0))
    entries = []

    for i, start in enumerate(offs):
        if start == 0:
            entries.append(None)
            continue

        end = len(data)

        for nxt in offs[i + 1:]:
            if nxt:
                end = nxt
                break

        entries.append(data[start:end])

    return entries


def parse_lang_exact(data):
    n = struct.unpack_from("<I", data, 0)[0]

    if n <= 0 or 4 + n * 4 > len(data):
        raise RuntimeError("Format LANG tidak dikenali.")

    offs = list(struct.unpack_from(f"<{n}I", data, 4))
    out = []

    for start in offs:
        nul = data.find(b"\0", start)

        if nul < 0:
            raise RuntimeError("LANG string has no terminator.")

        out.append(data[start:nul + 1])

    return out


def extract_raw_resource(path):
    raw = path.read_bytes()

    if len(raw) % 2352 == 0 and len(raw) >= 2352:
        sectors = len(raw) // 2352
        logical = bytearray()

        for i in range(sectors):
            sec = raw[i * 2352:(i + 1) * 2352]

            if len(sec) != 2352:
                raise RuntimeError(f"Sektor raw rusak: {path}")

            # Mode2 Form1 user data.
            logical += sec[24:24 + 2048]

        return bytes(logical), sectors

    if len(raw) % 2048 == 0 and len(raw) >= 2048:
        return raw, len(raw) // 2048

    raise RuntimeError(
        f"Ukuran raw resource tidak dikenali: {path} ({len(raw)} bytes)"
    )


def looks_like_repo(path):
    return (
        (path / "build" / "lang_file" / "dw2003" / "eng").is_dir()
        and (path / "lang_file" / "dw2003").is_dir()
    )


def auto_repo():
    candidates = []

    for p in [HERE, *HERE.parents]:
        candidates.extend([
            p / "ddw3",
            p,
        ])

    seen = set()

    for c in candidates:
        c = c.resolve()

        if c in seen:
            continue
        seen.add(c)

        if looks_like_repo(c):
            return c

    return None


def auto_original_bin():
    candidates = []

    for p in [HERE, *HERE.parents]:
        candidates.extend([
            p / "original" / "Digimon World 2003.bin",
            p / "Digimon World 2003.bin",
        ])

    for c in candidates:
        if c.is_file():
            return c.resolve()

    return None


def auto_raw_root():
    candidates = [
        Path(r"G:\dw2003_original_raw"),
        Path(r"D:\Project\digi23trans\dw2003_original_raw"),
    ]

    for p in [HERE, *HERE.parents]:
        candidates.extend([
            p / "dw2003_original_raw",
            p / "original_raw",
        ])

    for c in candidates:
        if c.is_dir():
            return c.resolve()

    return None


def parse_translation_rows(rows):
    if not rows:
        raise RuntimeError("Workbook kosong.")

    headers = [str(x).strip() for x in rows[0]]

    rel_col = find_header(headers, "relpath")
    idx_col = find_header(headers, "index")
    tr_col = find_header(headers, "translation")

    missing = []

    if rel_col is None:
        missing.append("relpath")
    if idx_col is None:
        missing.append("index")
    if tr_col is None:
        missing.append("translation")

    if missing:
        raise RuntimeError(
            f"Kolom Excel tidak ditemukan: {missing}\n"
            f"Header yang ditemukan: {headers}"
        )

    print("Detected columns:")
    print(f"  relpath     -> {headers[rel_col]}")
    print(f"  index       -> {headers[idx_col]}")
    print(f"  translation -> {headers[tr_col]}")
    print()

    # resource key ->
    #   {"kind":"direct","strings":set(...)}
    # or
    #   {"kind":"pack","entries":{entry:set(...)}}
    grouped = {}
    total = 0

    for row in rows[1:]:
        def get(col):
            if col >= len(row) or row[col] is None:
                return ""
            return str(row[col])

        rel = get(rel_col).replace("\\", "/").strip()
        tr = get(tr_col)

        if not tr.strip():
            continue

        parts = [p for p in rel.split("/") if p]

        try:
            eng_pos = next(
                i for i, p in enumerate(parts)
                if p.lower() == "eng"
            )
            tail = parts[eng_pos + 1:]
        except StopIteration:
            tail = parts

        if not tail:
            raise RuntimeError(f"Relpath kosong/tidak valid: {rel}")

        raw_index = get(idx_col).strip()
        try:
            string_index = int(float(raw_index))
        except ValueError:
            raise RuntimeError(
                f"Index string tidak valid pada {rel}: {raw_index!r}"
            )

        # Root-level TOML, e.g. esnameet.toml:
        # this is a direct LangFile on disc, NOT a PACK entry.
        if len(tail) == 1:
            m = re.fullmatch(r"(.+)\.toml", tail[0], re.I)
            if not m:
                raise RuntimeError(f"Relpath direct LANG tidak valid: {rel}")

            resource_key = m.group(1).lower()
            spec = grouped.setdefault(
                resource_key,
                {"kind": "direct", "strings": set()}
            )

            if spec["kind"] != "direct":
                raise RuntimeError(f"Tipe resource bentrok: {resource_key}")

            spec["strings"].add(string_index)
            total += 1
            continue

        # Folder form, e.g. esdmg700/0.toml:
        # folder is the PACK resource, numeric TOML is PACK entry.
        file_name = tail[-1]
        m = re.fullmatch(r"(\d+)\.toml", file_name, re.I)

        if not m:
            raise RuntimeError(
                f"Nama TOML dalam resource PACK tidak memakai index numerik: {rel}"
            )

        resource_parts = tail[:-1]
        resource_key = "/".join(resource_parts).lower()
        entry_index = int(m.group(1))

        spec = grouped.setdefault(
            resource_key,
            {"kind": "pack", "entries": defaultdict(set)}
        )

        if spec["kind"] != "pack":
            raise RuntimeError(f"Tipe resource bentrok: {resource_key}")

        spec["entries"][entry_index].add(string_index)
        total += 1

    return grouped, total

def build_raw_file_index(raw_root):
    by_stem = defaultdict(list)

    for p in raw_root.rglob("*"):
        if p.is_file() and p.suffix.lower() == ".bin":
            by_stem[p.stem.lower()].append(p)

    return by_stem


def choose_raw_resource(resource_key, by_stem):
    stem = Path(resource_key).name.lower()
    choices = by_stem.get(stem, [])

    if not choices:
        raise RuntimeError(
            f"Raw original untuk resource {resource_key} tidak ditemukan."
        )

    if len(choices) == 1:
        return choices[0]

    # Prefer COUNTRY/ENG if duplicate stems exist.
    eng_choices = [
        p for p in choices
        if "/country/eng/" in str(p).replace("\\", "/").lower()
    ]

    if len(eng_choices) == 1:
        return eng_choices[0]

    raise RuntimeError(
        f"Raw resource ambigu untuk {resource_key}:\n"
        + "\n".join(str(x) for x in choices[:20])
    )


def translated_lang_path(repo, resource_key, entry_index):
    rel = Path(*resource_key.split("/"))
    return (
        repo
        / "build"
        / "lang_file"
        / "dw2003"
        / "eng"
        / rel
        / f"{entry_index}.bin"
    )


def scan_original_bin(bin_path, resource_logicals):
    """
    Scan the original BIN once.

    resource_logicals:
      {resource_key: (logical_bytes, sector_count)}

    Returns:
      {resource_key: [lba, ...]}
    """

    first_hash = defaultdict(list)

    for key, (logical, sectors) in resource_logicals.items():
        if not logical or sectors <= 0:
            raise RuntimeError(f"Resource kosong: {key}")

        h = hashlib.sha1(logical[:2048]).digest()
        first_hash[h].append(key)

    found = defaultdict(list)
    file_size = bin_path.stat().st_size

    if file_size % 2352 != 0:
        raise RuntimeError(
            "BIN original bukan raw BIN 2352-byte sector."
        )

    total_sectors = file_size // 2352

    with bin_path.open("rb") as f:
        lba = 0

        while lba < total_sectors:
            sec = f.read(2352)

            if len(sec) != 2352:
                break

            if sec[15] != 2 or (sec[18] & 0x20):
                lba += 1
                continue

            payload = sec[24:24 + 2048]
            h = hashlib.sha1(payload).digest()
            candidates = first_hash.get(h)

            if candidates:
                for key in candidates:
                    logical, sectors = resource_logicals[key]

                    if lba + sectors > total_sectors:
                        continue

                    chunks = [payload]
                    cur = f.tell()

                    ok = True

                    for _ in range(1, sectors):
                        s = f.read(2352)

                        if (
                            len(s) != 2352
                            or s[15] != 2
                            or (s[18] & 0x20)
                        ):
                            ok = False
                            break

                        chunks.append(s[24:24 + 2048])

                    f.seek(cur)

                    if ok and b"".join(chunks) == logical:
                        found[key].append(lba)

            lba += 1

    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--original-bin", type=Path)
    parser.add_argument("--raw-root", type=Path)
    args = parser.parse_args()

    if not XLSX.is_file():
        raise SystemExit(
            f"all_strings.xlsx tidak ditemukan.\n"
            f"Taruh file Excel di folder yang sama:\n{HERE}"
        )

    if not TEMPLATE.is_file():
        raise SystemExit(f"Template tidak ditemukan: {TEMPLATE}")

    repo = args.repo.resolve() if args.repo else auto_repo()
    original_bin = (
        args.original_bin.resolve()
        if args.original_bin
        else auto_original_bin()
    )
    raw_root = (
        args.raw_root.resolve()
        if args.raw_root
        else auto_raw_root()
    )

    if repo is None:
        raise SystemExit(
            "Folder repo ddw3 tidak ditemukan otomatis.\n"
            "Jalankan dengan --repo D:\\Project\\digi23trans\\ddw3"
        )

    if original_bin is None:
        raise SystemExit(
            "BIN original tidak ditemukan otomatis.\n"
            "Jalankan dengan --original-bin \"D:\\...\\Digimon World 2003.bin\""
        )

    if raw_root is None:
        raise SystemExit(
            "Folder dw2003_original_raw tidak ditemukan otomatis.\n"
            "Jalankan dengan --raw-root G:\\dw2003_original_raw"
        )

    print("===== PATH =====")
    print("Excel       :", XLSX)
    print("Repo        :", repo)
    print("Original BIN:", original_bin)
    print("Raw dump    :", raw_root)
    print()

    rows = read_xlsx_rows(XLSX)
    grouped, total_rows = parse_translation_rows(rows)

    print("===== TRANSLATION =====")
    print("Non-empty translation rows :", total_rows)
    print("Resources referenced        :", len(grouped))
    print()

    raw_index = build_raw_file_index(raw_root)

    original_data = {}
    resource_logicals = {}
    raw_paths = {}

    print("===== ORIGINAL RESOURCES =====")

    for pos, resource_key in enumerate(sorted(grouped), 1):
        raw_path = choose_raw_resource(resource_key, raw_index)
        logical, sectors = extract_raw_resource(raw_path)

        raw_paths[resource_key] = raw_path
        original_data[resource_key] = logical
        resource_logicals[resource_key] = (logical, sectors)

        print(
            f"[{pos:03}/{len(grouped):03}] "
            f"{resource_key:<24} {sectors:>3} sectors  {raw_path.name}"
        )

    print()
    print("===== SCAN ORIGINAL BIN =====")
    print("Scanning once; this can take a while...")

    found = scan_original_bin(original_bin, resource_logicals)

    missing_locations = [
        key for key in grouped
        if not found.get(key)
    ]

    if missing_locations:
        print()
        print("ERROR: resource berikut tidak ditemukan di original BIN:")
        for key in missing_locations:
            print("  -", key)
        raise SystemExit(
            "Resource mapping belum lengkap. "
            "Jangan build EXE sebelum semua resource ditemukan."
        )

    print()
    for key in sorted(grouped):
        print(
            f"{key:<28} -> LBA "
            + ", ".join(str(x) for x in found[key])
        )

    manifest_resources = {}
    total_strings = 0

    print()
    print("===== BUILD FULL MANIFEST =====")

    for pos, resource_key in enumerate(sorted(grouped), 1):
        logical, sectors = resource_logicals[resource_key]
        group_spec = grouped[resource_key]

        if group_spec["kind"] == "direct":
            expected_blobs = parse_lang_exact(logical)

            translated_path = (
                repo
                / "build"
                / "lang_file"
                / "dw2003"
                / "eng"
                / f"{resource_key}.bin"
            )

            if not translated_path.is_file():
                raise SystemExit(
                    f"Compiled direct LANG tidak ditemukan:\n"
                    f"{translated_path}\n\n"
                    "Pastikan all_strings.xlsx sudah direinject lalu jalankan zbuild."
                )

            target_blobs = parse_lang_exact(translated_path.read_bytes())
            replacements = {}

            for string_index in sorted(group_spec["strings"]):
                if (
                    string_index < 0
                    or string_index >= len(expected_blobs)
                    or string_index >= len(target_blobs)
                ):
                    raise SystemExit(
                        f"{resource_key}: string index {string_index} di luar batas."
                    )

                expected = expected_blobs[string_index]
                target = target_blobs[string_index]

                if expected == target:
                    print(
                        f"WARNING: translation tidak mengubah bytes: "
                        f"{resource_key} string {string_index}"
                    )

                replacements[string_index] = (expected, target)
                total_strings += 1

            manifest_resources[resource_key] = {
                "kind": "direct",
                "lbas": found[resource_key],
                "sectors": sectors,
                "strings": replacements,
            }

            count = len(replacements)

        else:
            pack_entries = parse_pack(logical)
            spec_entries = {}

            for entry_index, string_indexes in sorted(group_spec["entries"].items()):
                if (
                    entry_index < 0
                    or entry_index >= len(pack_entries)
                    or pack_entries[entry_index] is None
                ):
                    raise SystemExit(
                        f"{resource_key}: original PACK entry "
                        f"{entry_index} tidak ditemukan."
                    )

                expected_blobs = parse_lang_exact(
                    pack_entries[entry_index]
                )

                translated_path = translated_lang_path(
                    repo,
                    resource_key,
                    entry_index
                )

                if not translated_path.is_file():
                    raise SystemExit(
                        f"Compiled LANG tidak ditemukan:\n"
                        f"{translated_path}\n\n"
                        "Pastikan all_strings.xlsx sudah direinject lalu jalankan zbuild."
                    )

                target_blobs = parse_lang_exact(
                    translated_path.read_bytes()
                )

                replacements = {}

                for string_index in sorted(string_indexes):
                    if (
                        string_index < 0
                        or string_index >= len(expected_blobs)
                        or string_index >= len(target_blobs)
                    ):
                        raise SystemExit(
                            f"{resource_key}/{entry_index}: "
                            f"string index {string_index} di luar batas."
                        )

                    expected = expected_blobs[string_index]
                    target = target_blobs[string_index]

                    if expected == target:
                        print(
                            f"WARNING: translation tidak mengubah bytes: "
                            f"{resource_key}/{entry_index} string {string_index}"
                        )

                    replacements[string_index] = (
                        expected,
                        target
                    )
                    total_strings += 1

                spec_entries[entry_index] = replacements

            manifest_resources[resource_key] = {
                "kind": "pack",
                "lbas": found[resource_key],
                "sectors": sectors,
                "entries": spec_entries,
            }

            count = sum(len(v) for v in spec_entries.values())

        print(
            f"[{pos:03}/{len(grouped):03}] "
            f"{resource_key}: "
            f"{count} strings, "
            f"{len(found[resource_key])} copy/copies, "
            f"type={group_spec['kind']}"
        )

    manifest = {
        "format": 1,
        "game": "Digimon World 2003",
        "language": "Bahasa Indonesia",
        "total_strings": total_strings,
        "resources": manifest_resources,
    }

    packed = zlib.compress(
        pickle.dumps(manifest, protocol=4),
        9
    )
    encoded = base64.b85encode(packed).decode("ascii")

    source = TEMPLATE.read_text(encoding="utf-8")
    marker = "EMBEDDED_MANIFEST_B85 = None"

    if marker not in source:
        raise SystemExit(
            "Marker EMBEDDED_MANIFEST_B85 tidak ditemukan di template."
        )

    source = source.replace(
        marker,
        "EMBEDDED_MANIFEST_B85 = " + repr(encoded),
        1
    )

    BUILT.write_text(source, encoding="utf-8")

    print()
    print("===== DONE =====")
    print("Translated strings embedded :", total_strings)
    print("Resources embedded          :", len(manifest_resources))
    print("Compressed manifest size    :", len(packed), "bytes")
    print("Generated source            :", BUILT)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDibatalkan.")
        sys.exit(130)
