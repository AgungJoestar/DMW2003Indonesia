#!/usr/bin/env python3
# Digimon World 2003 - Patcher Bahasa Indonesia
# Standalone GUI v1.0.1 - responsive/scrollable UI hotfix
# Created By : Frizal Ouryuken

from __future__ import annotations

import base64
import os
import pickle
import queue
import shutil
import struct
import sys
import threading
import traceback
import zlib
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "Digimon World 2003 - Patcher Bahasa Indonesia v1.0.1"
RAW = 2352
UOFF = 24
USIZE = 2048

# Replaced by build_manifest_v05.py into the generated build source.
EMBEDDED_MANIFEST_B85 = None

EF = [0] * 256
EB = [0] * 256
ED = [0] * 256

for i in range(256):
    j = ((i << 1) ^ (0x11D if i & 0x80 else 0)) & 255
    EF[i] = j
    EB[i ^ j] = i

    x = i
    for _ in range(8):
        x = (x >> 1) ^ (0xD8018001 if x & 1 else 0)
    ED[i] = x & 0xFFFFFFFF


def load_manifest():
    if not EMBEDDED_MANIFEST_B85:
        raise RuntimeError("Data terjemahan belum ditanam ke aplikasi.")

    try:
        packed = base64.b85decode(EMBEDDED_MANIFEST_B85.encode("ascii"))
        return pickle.loads(zlib.decompress(packed))
    except Exception as e:
        raise RuntimeError(f"Manifest terjemahan rusak: {e}") from e


def resource_path(name: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / name
    return Path(__file__).resolve().parent / name


def edc(data):
    x = 0
    for b in data:
        x = (x >> 8) ^ ED[(x ^ b) & 255]
    return x & 0xFFFFFFFF


def ecc(src, mc, nc, mm, mi):
    size = mc * nc
    dst = bytearray(mc * 2)

    for major in range(mc):
        idx = (major >> 1) * mm + (major & 1)
        a = b = 0

        for _ in range(nc):
            t = src[idx]
            idx += mi
            if idx >= size:
                idx -= size
            a ^= t
            b ^= t
            a = EF[a]

        a = EB[EF[a] ^ b]
        dst[major] = a
        dst[major + mc] = a ^ b

    return bytes(dst)


def regen(sec):
    s = bytearray(sec)

    if len(s) != RAW or s[15] != 2 or (s[18] & 0x20):
        raise ValueError("Sektor game bukan Mode2 Form1 yang didukung.")

    s[2072:2076] = edc(s[16:2072]).to_bytes(4, "little")

    hdr = bytes(s[12:16])
    s[12:16] = b"\0" * 4
    s[2076:2248] = ecc(s[12:2076], 86, 24, 2, 86)
    s[2248:2352] = ecc(s[12:2248], 52, 43, 86, 88)
    s[12:16] = hdr

    return bytes(s)


def read_res(path: Path, lba: int, n: int):
    out = bytearray()

    with path.open("rb") as f:
        for x in range(lba, lba + n):
            f.seek(x * RAW)
            s = f.read(RAW)

            if len(s) != RAW or s[15] != 2 or (s[18] & 0x20):
                raise RuntimeError(
                    f"Resource game tidak valid pada LBA {x}."
                )

            out += s[UOFF:UOFF + USIZE]

    return bytes(out)


def parse_pack(data):
    if len(data) < 4:
        raise ValueError("PACK terlalu kecil.")

    first = struct.unpack_from("<I", data, 0)[0]

    if not first or first % 4 or first > len(data):
        raise ValueError("Format PACK tidak dikenali.")

    n = first // 4
    offs = list(struct.unpack_from(f"<{n}I", data, 0))
    ent = []

    for i, start in enumerate(offs):
        if start == 0:
            ent.append(None)
            continue

        if start >= len(data):
            raise ValueError("Offset PACK tidak valid.")

        end = len(data)
        for nxt in offs[i + 1:]:
            if nxt:
                end = nxt
                break

        if end < start or end > len(data):
            raise ValueError("Batas PACK tidak valid.")

        ent.append(data[start:end])

    return ent


def build_pack(entries):
    h = len(entries) * 4
    body = bytearray()
    offs = [0] * len(entries)

    for i, e in enumerate(entries):
        if e is None:
            continue

        while (h + len(body)) % 4:
            body.append(0)

        offs[i] = h + len(body)
        body += e

    return struct.pack(f"<{len(entries)}I", *offs) + body


def parse_lang(data):
    if len(data) < 4:
        raise ValueError("LANG terlalu kecil.")

    n = struct.unpack_from("<I", data, 0)[0]

    if n <= 0 or 4 + n * 4 > len(data):
        raise ValueError("Format LANG tidak dikenali.")

    offs = list(struct.unpack_from(f"<{n}I", data, 4))
    out = []

    for start in offs:
        if start >= len(data):
            raise ValueError("Offset LANG tidak valid.")

        nul = data.find(b"\0", start)
        if nul < 0:
            raise ValueError("String LANG tidak memiliki terminator.")

        out.append(data[start:nul + 1])

    return out


def trim_lang_entry(data):
    try:
        n = struct.unpack_from("<I", data, 0)[0]
        if n <= 0 or 4 + n * 4 > len(data):
            return data

        offs = list(struct.unpack_from(f"<{n}I", data, 4))
        if not offs:
            return data

        start = offs[-1]
        if start >= len(data):
            return data

        nul = data.find(b"\0", start)
        if nul < 0:
            return data

        return data[:nul + 1]
    except Exception:
        return data


def build_lang(blobs):
    h = 4 + len(blobs) * 4
    body = bytearray()
    offs = []

    for b in blobs:
        while (h + len(body)) % 4:
            body.append(0)

        offs.append(h + len(body))
        body += b

    return (
        struct.pack("<I", len(blobs))
        + struct.pack(f"<{len(blobs)}I", *offs)
        + body
    )


def write_res(path: Path, lba: int, payload: bytes, n: int):
    cap = n * USIZE

    if len(payload) > cap:
        raise RuntimeError(
            f"Data terjemahan terlalu besar ({len(payload)} > {cap} byte)."
        )

    data = payload + bytes(cap - len(payload))

    with path.open("r+b") as f:
        for i, x in enumerate(range(lba, lba + n)):
            f.seek(x * RAW)
            s = bytearray(f.read(RAW))

            if len(s) != RAW:
                raise RuntimeError(f"Gagal membaca sektor LBA {x}.")

            s[UOFF:UOFF + USIZE] = data[i * USIZE:(i + 1) * USIZE]
            s = regen(s)

            f.seek(x * RAW)
            f.write(s)


def find_input_cue(src: Path):
    exact = src.with_suffix(".cue")
    if exact.is_file():
        return exact

    cues = []
    for pattern in ("*.cue", "*.CUE"):
        for c in src.parent.glob(pattern):
            if c not in cues:
                cues.append(c)

    return cues[0] if len(cues) == 1 else None


def make_cue(src_bin: Path, out_bin: Path):
    out_cue = out_bin.with_suffix(".cue")
    original = find_input_cue(src_bin)

    if original:
        raw = original.read_bytes()
        text = None

        for candidate in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(candidate)
                break
            except UnicodeDecodeError:
                pass

        if text is None:
            raise RuntimeError("CUE original tidak dapat dibaca.")

        lines = text.splitlines()
        file_lines = [
            i for i, line in enumerate(lines)
            if line.lstrip().upper().startswith("FILE ")
        ]

        if not file_lines:
            raise RuntimeError(
                "CUE original ditemukan tetapi baris FILE tidak dikenali."
            )

        selected = None
        src_name = src_bin.name.lower()

        for i in file_lines:
            line = lines[i]
            if src_name in line.lower():
                selected = i
                break

        if selected is None:
            if len(file_lines) == 1:
                selected = file_lines[0]
            else:
                raise RuntimeError(
                    "CUE memiliki beberapa FILE dan BIN yang dipilih "
                    "tidak dapat ditentukan dengan aman."
                )

        line = lines[selected]
        indent = line[:len(line) - len(line.lstrip())]
        lines[selected] = f'{indent}FILE "{out_bin.name}" BINARY'

        out_cue.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8"
        )
        return out_cue

    out_cue.write_text(
        f'FILE "{out_bin.name}" BINARY\n'
        '  TRACK 01 MODE2/2352\n'
        '    INDEX 01 00:00:00\n',
        encoding="utf-8"
    )
    return out_cue


def output_paths(src: Path):
    folder = src.parent / "patched"
    stem = src.stem + " [Bahasa Indonesia]"
    return folder / (stem + ".bin"), folder / (stem + ".cue")


def unpack_translation_pair(pair):
    """
    Manifest v1.0 used: (expected_english, target)
    Manifest v1.0.1 uses: (expected_english, target, previous_targets)

    Keeping support for the old two-item form makes the runtime tolerant
    of older manifests during development.
    """
    if len(pair) == 2:
        expected_blob, target_blob = pair
        previous = ()
    else:
        expected_blob, target_blob, previous = pair
        previous = tuple(previous)

    return expected_blob, target_blob, previous


def apply_direct_lang(current, resource_name, spec):
    blobs = parse_lang(current)
    applied = 0
    updated = 0
    already = 0
    conflicts = []
    changed = False

    for string_index, pair in sorted(spec["strings"].items()):
        expected_blob, target_blob, previous_targets = unpack_translation_pair(pair)

        if string_index < 0 or string_index >= len(blobs):
            raise RuntimeError(
                f"{resource_name}: string {string_index} tidak ditemukan."
            )

        current_blob = blobs[string_index]

        if current_blob == target_blob:
            already += 1
            continue

        if current_blob == expected_blob:
            blobs[string_index] = target_blob
            applied += 1
            changed = True
            continue

        if current_blob in previous_targets:
            blobs[string_index] = target_blob
            updated += 1
            changed = True
            continue

        conflicts.append((-1, string_index))

    # Important for mod compatibility: if nothing changed, return exact bytes.
    if not changed:
        return current, applied, updated, already, conflicts, 0

    rebuilt = build_lang(blobs)
    return rebuilt, applied, updated, already, conflicts, 1


def apply_resource(current, resource_name, spec):
    entries = parse_pack(current)

    applied = 0
    updated = 0
    already = 0
    conflicts = []
    changed_entries = set()

    for entry_index, replacements in sorted(spec["entries"].items()):
        if (
            entry_index < 0
            or entry_index >= len(entries)
            or entries[entry_index] is None
        ):
            raise RuntimeError(
                f"{resource_name}: PACK entry {entry_index} tidak ditemukan."
            )

        blobs = parse_lang(entries[entry_index])
        changed = False

        for string_index, pair in sorted(replacements.items()):
            expected_blob, target_blob, previous_targets = unpack_translation_pair(pair)

            if string_index < 0 or string_index >= len(blobs):
                raise RuntimeError(
                    f"{resource_name}: string {string_index} tidak ditemukan "
                    f"pada PACK entry {entry_index}."
                )

            current_blob = blobs[string_index]

            if current_blob == target_blob:
                already += 1
                continue

            if current_blob == expected_blob:
                blobs[string_index] = target_blob
                applied += 1
                changed = True
                continue

            if current_blob in previous_targets:
                blobs[string_index] = target_blob
                updated += 1
                changed = True
                continue

            conflicts.append((entry_index, string_index))

        if changed:
            rebuilt = build_lang(blobs)
            verify = parse_lang(rebuilt)

            for string_index, pair in replacements.items():
                _expected_blob, target_blob, _previous_targets = unpack_translation_pair(pair)
                if blobs[string_index] == target_blob and verify[string_index] != target_blob:
                    raise RuntimeError(
                        f"{resource_name}: verifikasi gagal pada "
                        f"entry {entry_index}, string {string_index}."
                    )

            entries[entry_index] = rebuilt
            changed_entries.add(entry_index)

    # Do not canonicalize/rebuild a user's resource if no string changed.
    if not changed_entries:
        return current, applied, updated, already, conflicts, 0

    # Only now trim trailing allocation padding before rebuilding the PACK.
    nonempty = [i for i, e in enumerate(entries) if e is not None]
    if nonempty:
        last = nonempty[-1]
        entries[last] = trim_lang_entry(entries[last])

    return (
        build_pack(entries),
        applied,
        updated,
        already,
        conflicts,
        len(changed_entries),
    )


def patch_game(src: Path, out: Path, progress):
    manifest = load_manifest()
    resources = manifest["resources"]
    total_strings = manifest["total_strings"]

    progress(3, "Memeriksa file game...")

    prepared = []
    total_applied = 0
    total_updated = 0
    total_already = 0
    all_conflicts = []
    changed_blocks = 0

    resource_items = sorted(resources.items())
    total_resources = len(resource_items)

    for pos, (resource_name, spec) in enumerate(resource_items, 1):
        sectors = spec["sectors"]
        copies = spec["lbas"]

        if not copies:
            raise RuntimeError(
                f"{resource_name}: lokasi resource tidak tersedia."
            )

        resource_outputs = []

        for lba in copies:
            current = read_res(src, lba, sectors)

            try:
                if spec.get("kind") == "direct":
                    rebuilt, applied, updated, already, conflicts, changed = apply_direct_lang(
                        current, resource_name, spec
                    )
                else:
                    rebuilt, applied, updated, already, conflicts, changed = apply_resource(
                        current, resource_name, spec
                    )
            except Exception as e:
                raise RuntimeError(
                    f"Gagal memproses {resource_name} pada LBA {lba}: {e}"
                ) from e

            cap = sectors * USIZE
            if len(rebuilt) > cap:
                raise RuntimeError(
                    f"{resource_name} pada LBA {lba} melebihi alokasi disc: "
                    f"{len(rebuilt)} / {cap} byte."
                )

            resource_outputs.append((lba, rebuilt))
            total_applied += applied
            total_updated += updated
            total_already += already
            changed_blocks += changed

            for entry_idx, string_idx in conflicts:
                if entry_idx < 0:
                    all_conflicts.append(
                        f"{resource_name}: string {string_idx}"
                    )
                else:
                    all_conflicts.append(
                        f"{resource_name}: entry {entry_idx}, string {string_idx}"
                    )

        prepared.append((resource_name, sectors, resource_outputs))

        pct = 5 + int(45 * pos / max(1, total_resources))
        progress(
            pct,
            f"Menyiapkan resource {pos}/{total_resources}: {resource_name}"
        )

    progress(52, "Membuat folder patched...")
    out.parent.mkdir(parents=True, exist_ok=True)

    progress(57, "Membuat salinan BIN...")
    shutil.copy2(src, out)

    try:
        for pos, (resource_name, sectors, outputs) in enumerate(prepared, 1):
            for lba, payload in outputs:
                write_res(out, lba, payload, sectors)

            pct = 60 + int(28 * pos / max(1, len(prepared)))
            progress(
                pct,
                f"Menulis resource {pos}/{len(prepared)}: {resource_name}"
            )

        progress(90, "Memverifikasi EDC/ECC...")
        with out.open("rb") as f:
            for resource_name, sectors, outputs in prepared:
                for lba, _payload in outputs:
                    for x in range(lba, lba + sectors):
                        f.seek(x * RAW)
                        s = f.read(RAW)

                        if regen(s) != s:
                            raise RuntimeError(
                                f"EDC/ECC tidak valid pada LBA {x} "
                                f"({resource_name})."
                            )

        progress(97, "Membuat file CUE...")
        cue = make_cue(src, out)

    except Exception:
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass

        try:
            out.with_suffix(".cue").unlink(missing_ok=True)
        except Exception:
            pass

        raise

    progress(100, "Patch selesai!")

    return {
        "bin": out,
        "cue": cue,
        "total": total_strings,
        "applied": total_applied,
        "updated": total_updated,
        "already": total_already,
        "conflicts": all_conflicts,
        "resources": len(resources),
        "changed_blocks": changed_blocks,
    }


def open_folder(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path.parent))
        elif sys.platform == "darwin":
            os.system(f'open "{path.parent}"')
        else:
            os.system(f'xdg-open "{path.parent}"')
    except Exception as e:
        messagebox.showerror("Gagal membuka folder", str(e))


class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title(APP_TITLE)

        # Responsive sizing: do not force a tall window on laptop displays.
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        width = min(820, max(640, sw - 80))
        height = min(720, max(500, sh - 120))
        x = max(0, (sw - width) // 2)
        y = max(0, (sh - height) // 2)

        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(620, 480)

        self.bin_var = tk.StringVar()
        self.status = tk.StringVar(value="Pilih file BIN Digimon World 2003.")
        self.detail = tk.StringVar(
            value="Hasil patch akan dibuat otomatis di subfolder patched."
        )
        self.pct = tk.DoubleVar(value=0)
        self.q = queue.Queue()
        self.result = None
        self.logo_image = None

        self.ui()
        self.after(100, self.poll)

    def ui(self):
        style = ttk.Style(self)
        try:
            style.configure("Title.TLabel", font=("Segoe UI", 13, "bold"))
            style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
            style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"))
        except Exception:
            pass

        # Scrollable body: on a small laptop no controls can become unreachable.
        shell = ttk.Frame(self)
        shell.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(shell, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical",
                                  command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.outer = ttk.Frame(self.canvas, padding=(22, 14, 22, 16))
        self.canvas_window = self.canvas.create_window(
            (0, 0), window=self.outer, anchor="nw"
        )
        self.outer.bind("<Configure>", self._update_scrollregion)
        self.canvas.bind("<Configure>", self._resize_canvas_window)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_mousewheel_linux)
        self.canvas.bind_all("<Button-5>", self._on_mousewheel_linux)

        header = ttk.Frame(self.outer)
        header.pack(fill="x")

        logo_path = resource_path("logo.png")
        if logo_path.is_file():
            try:
                raw_logo = tk.PhotoImage(file=str(logo_path))
                divisor = 5 if self.winfo_screenheight() < 800 else 4
                self.logo_image = raw_logo.subsample(divisor, divisor)
                ttk.Label(header, image=self.logo_image).pack(
                    anchor="center", pady=(0, 5)
                )
            except Exception:
                ttk.Label(header, text="Digimon World 2003",
                          style="Title.TLabel").pack(anchor="center")
        else:
            ttk.Label(header, text="Digimon World 2003",
                      style="Title.TLabel").pack(anchor="center")

        ttk.Label(header, text="Patcher Bahasa Indonesia",
                  style="Title.TLabel").pack(anchor="center", pady=(0, 2))
        ttk.Label(header, text="Created By : Frizal Ouryuken",
                  font=("Segoe UI", 9, "italic")).pack(anchor="center", pady=(0, 1))

        tip = tk.Label(
            header,
            text="Traktir Saya Cendol : https://trakteer.id/frizal_ouryuken/tip",
            font=("Segoe UI", 9, "underline"),
            fg="#0563C1", cursor="hand2"
        )
        tip.pack(anchor="center", pady=(0, 2))
        tip.bind("<Button-1>",
                 lambda _e: self.open_url("https://trakteer.id/frizal_ouryuken/tip"))

        ttk.Label(header, text="v1.0.1",
                  font=("Segoe UI", 8)).pack(anchor="center", pady=(0, 9))

        source_box = ttk.LabelFrame(
            self.outer, text="  1. Pilih File Game  ", padding=(12, 9)
        )
        source_box.pack(fill="x", pady=(0, 8))
        ttk.Label(
            source_box,
            text="Pilih file .BIN Digimon World 2003 yang ingin dipatch."
        ).pack(anchor="w", pady=(0, 5))

        source_row = ttk.Frame(source_box)
        source_row.pack(fill="x")
        self.e = ttk.Entry(source_row, textvariable=self.bin_var)
        self.e.pack(side="left", fill="x", expand=True)
        self.b = ttk.Button(source_row, text="Pilih BIN...", command=self.choose)
        self.b.pack(side="left", padx=(7, 0))

        status_box = ttk.LabelFrame(
            self.outer, text="  2. Status Patch  ", padding=(12, 9)
        )
        status_box.pack(fill="x", pady=(0, 8))
        ttk.Label(status_box, textvariable=self.status,
                  style="Status.TLabel").pack(anchor="w")
        self.detail_label = ttk.Label(
            status_box, textvariable=self.detail, justify="left"
        )
        self.detail_label.pack(anchor="w", fill="x", pady=(4, 7))

        progress_row = ttk.Frame(status_box)
        progress_row.pack(fill="x")
        ttk.Progressbar(progress_row, maximum=100,
                        variable=self.pct).pack(side="left", fill="x", expand=True)
        self.pl = ttk.Label(progress_row, text="0%", width=5, anchor="e")
        self.pl.pack(side="left", padx=(8, 0))

        action_box = ttk.LabelFrame(
            self.outer, text="  3. Mulai Patch  ", padding=(12, 9)
        )
        action_box.pack(fill="x", pady=(0, 8))

        action_row = ttk.Frame(action_box)
        action_row.pack(fill="x")
        action_row.columnconfigure(0, weight=3)
        action_row.columnconfigure(1, weight=1)

        self.pb = ttk.Button(
            action_row, text="PATCH BAHASA INDONESIA",
            command=self.start, state="disabled", style="Primary.TButton"
        )
        self.pb.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.fb = ttk.Button(
            action_row, text="Buka Folder Hasil",
            command=self.open_result, state="disabled"
        )
        self.fb.grid(row=0, column=1, sticky="ew")

        self.safe_label = ttk.Label(
            action_box,
            text=("File BIN/CUE asli tidak akan diubah. "
                  "Hasil dibuat sebagai salinan baru di subfolder patched."),
            justify="left"
        )
        self.safe_label.pack(anchor="w", fill="x", pady=(7, 0))

        footer = ttk.Frame(self.outer)
        footer.pack(fill="x", pady=(2, 0))
        ttk.Separator(footer).pack(fill="x", pady=(0, 6))
        ttk.Label(
            footer,
            text="Digimon World 2003 Bahasa Indonesia • v1.0.1",
            font=("Segoe UI", 8)
        ).pack(anchor="center")

        self.after_idle(self._refresh_wraplengths)

    def _update_scrollregion(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_canvas_window(self, event):
        self.canvas.itemconfigure(self.canvas_window, width=max(1, event.width))
        self._refresh_wraplengths()

    def _refresh_wraplengths(self):
        try:
            usable = max(300, self.canvas.winfo_width() - 80)
            self.detail_label.configure(wraplength=usable)
            self.safe_label.configure(wraplength=usable)
        except Exception:
            pass

    def _on_mousewheel(self, event):
        delta = int(-1 * (event.delta / 120))
        if delta:
            self.canvas.yview_scroll(delta, "units")

    def _on_mousewheel_linux(self, event):
        if event.num == 4:
            self.canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(1, "units")

    def open_url(self, url):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            messagebox.showerror("Gagal membuka link", str(e))

    def choose(self):
        fn = filedialog.askopenfilename(
            title="Pilih Digimon World 2003 BIN",
            filetypes=[
                ("PlayStation BIN", "*.bin"),
                ("Semua file", "*.*")
            ]
        )

        if not fn:
            return

        src = Path(fn)
        out, _ = output_paths(src)

        self.bin_var.set(fn)
        self.pct.set(0)
        self.pl.config(text="0%")
        self.status.set("BIN siap dipatch.")
        self.detail.set(
            f"File dipilih:\n{src}\n\nHasil akan disimpan di:\n{out.parent}"
        )
        self.pb.config(state="normal")
        self.fb.config(state="disabled")

    def busy(self, value):
        state = "disabled" if value else "normal"
        self.b.config(state=state)
        self.e.config(state=state)
        self.pb.config(state=state)

    def start(self):
        src = Path(self.bin_var.get().strip())

        if not src.is_file():
            messagebox.showerror(
                "File tidak ditemukan",
                "Pilih file BIN yang valid."
            )
            return

        out, cue = output_paths(src)

        if out.exists() or cue.exists():
            if not messagebox.askyesno(
                "Hasil sudah ada",
                "File hasil di folder patched sudah ada.\n\nTimpa hasil lama?"
            ):
                return

            try:
                out.unlink(missing_ok=True)
                cue.unlink(missing_ok=True)
            except Exception as e:
                messagebox.showerror(
                    "Tidak dapat menimpa hasil",
                    str(e)
                )
                return

        self.busy(True)
        self.fb.config(state="disabled")
        self.pct.set(2)
        self.pl.config(text="2%")
        self.status.set("Memulai patch...")
        self.detail.set(
            "Sedang menyiapkan patch Bahasa Indonesia.\n"
            "Jangan tutup aplikasi sampai proses selesai."
        )

        threading.Thread(
            target=self.worker,
            args=(src, out),
            daemon=True
        ).start()

    def worker(self, src, out):
        try:
            def cb(p, t):
                self.q.put(("progress", (p, t)))

            result = patch_game(src, out, cb)
            self.q.put(("done", result))

        except Exception as e:
            self.q.put(
                (
                    "error",
                    (
                        str(e),
                        traceback.format_exc()
                    )
                )
            )

    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()

                if kind == "progress":
                    p, t = data
                    self.pct.set(p)
                    self.pl.config(text=f"{int(p)}%")
                    self.status.set(t)

                elif kind == "done":
                    self.busy(False)
                    self.result = data["bin"]
                    self.fb.config(state="normal")
                    self.status.set("Patch Bahasa Indonesia berhasil.")

                    conflicts = data["conflicts"]

                    summary = (
                        f"{data['applied']} teks baru diterapkan • "
                        f"{data['updated']} teks v1.0 diperbarui • "
                        f"{data['already']} sudah terbaru • "
                        f"{len(conflicts)} konflik dilewati\n"
                        f"{data['resources']} resource diproses"
                    )

                    self.detail.set(
                        f"{summary}\n\n"
                        f"Siap dimainkan:\n{data['cue']}"
                    )
                    self.after_idle(self._update_scrollregion)
                    self.after(80, lambda: self.canvas.yview_moveto(1.0))

                    if conflicts:
                        preview = "\n".join(conflicts[:12])
                        if len(conflicts) > 12:
                            preview += (
                                f"\n... dan {len(conflicts) - 12} konflik lainnya."
                            )

                        messagebox.showwarning(
                            "Patch selesai dengan konflik",
                            "Patch berhasil, tetapi beberapa teks telah "
                            "dimodifikasi oleh mod lain sehingga tidak ditimpa.\n\n"
                            + preview
                        )
                    else:
                        messagebox.showinfo(
                            "Patch Berhasil",
                            "Patch Bahasa Indonesia berhasil!\n\n"
                            + summary
                            + "\n\nBIN + CUE tersedia di folder patched.\n"
                              "File game asli tidak diubah."
                        )

                elif kind == "error":
                    self.busy(False)
                    msg, tb = data
                    self.status.set("Patch gagal.")
                    self.detail.set(
                        "Terjadi masalah saat melakukan patch.\n\n" + msg
                    )
                    self.after_idle(self._update_scrollregion)
                    self.after(80, lambda: self.canvas.yview_moveto(1.0))

                    messagebox.showerror(
                        "Patch gagal",
                        msg
                    )

        except queue.Empty:
            pass

        self.after(100, self.poll)

    def open_result(self):
        if self.result:
            open_folder(self.result)


if __name__ == "__main__":
    App().mainloop()
