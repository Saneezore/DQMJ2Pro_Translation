#!/usr/bin/env python3
"""Interactive patcher for DQMJ2P Pro ROM.

Applies mandatory translation-required patches (grow_*) and any optional
gameplay patches you select from a menu.

Usage:
    apply_patches.py
    apply_patches.py --rom Pro_ROM
"""
import argparse, math, os, struct, subprocess, sys, tempfile
import re
from pathlib import Path

BLZ = str(Path(__file__).parent / 'blz.exe')

ARM9_BASE        = 0x02000000
ARM9_PLAINTEXT   = 0x4000
ARM9_MODPARAMS   = 0xb68
ARM9_COMPEND_OFF = 0x14
NITRO_TRAILER    = bytes.fromhex('2106c0de680b000000000000')

Y9_ENTRY_SIZE    = 32
Y9_FLAGS_OFF     = 28
Y9_COMPRESS_FLAG = 0x01 << 24

OV_RAM_BASE = 0x021d7240


def apply_experimental_wifi(data: bytearray, module: str):
    count = 0
    for match in re.finditer(rb'[ -~]{6,}\x00', bytes(data)):
        old = match.group()
        if b'nintendowifi.net' not in old:
            continue
        new = old.replace(b'https://', b'http://').ljust(len(old), b'\0')
        data[match.start():match.end()] = new
        count += 1
    expected = {'arm9': 16, 'ov006': 1}[module]
    if count != expected:
        raise ValueError(f'{module}: unexpected WFC string count {count}, expected {expected}')
    if module != 'arm9':
        return
    edits = (
        (0xBA0B4, '0a00000a', '0a0000ea'),
        (0xD1F30,
         '08402de944309fe50000e0e3140083e50010a0e3081083e534009fe50820a0e3041083e587dbfceb24109fe524309fe57800a0e30820a0e3',
         '24009fe524109fe5001080e5041080e51c009fe50110a0e3141080e5041080e50810a0e3081080e51eff2fe190701d0233080c1f70701d02'),
    )
    for offset, before_hex, after_hex in edits:
        before, after = bytes.fromhex(before_hex), bytes.fromhex(after_hex)
        if data[offset:offset + len(before)] != before:
            raise ValueError(f'Unexpected Wi-Fi code at ARM9 offset {offset:#x}')
        data[offset:offset + len(before)] = after

# RetroAchievements anti-cheat marker.  The chosen bit is the otherwise-unused
# Rigor Mortex XY "seen" flag in the checksummed save data.  A marked ROM sets
# it whenever the game's main save-data accessor is used; the next normal save
# therefore makes the marker persistent.
CHEAT_TAINT_HOOK   = 0x020633BC
CHEAT_TAINT_CAVE   = 0x020DC708
CHEAT_TAINT_ADDR   = 0x021C0AB8
CHEAT_TAINT_MASK   = 0x20
SAVE_PAYLOAD_BASE  = 0x021BD51C

# Cosmetic fixes intentionally do not taint a save.
CHEAT_TAINT_PATCHES = frozenset({
    'xp_mult',
    'full_reserve_exp',
    'scout_offense',
    'scout_penalty',
    'synthesis_level',
    'synthesis_polarity',
})


# ── BLZ helpers ───────────────────────────────────────────────────────────────

def _blz_run(args_extra: list, data: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as tf:
        tf.write(data); tmp = tf.name
    try:
        creationflags = 0
        startupinfo = None

        if sys.platform.startswith("win"):
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        r = subprocess.run(
            [BLZ] + args_extra + [tmp],
            capture_output=True,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        if r.returncode not in (0, 1):
            sys.exit(f'blz {args_extra} failed:\n{r.stderr.decode()}')
        return Path(tmp).read_bytes()
    finally:
        os.unlink(tmp)


def overlay_decompress(path: Path) -> bytearray:
    return bytearray(_blz_run(['-d'], path.read_bytes()))


def overlay_compress(dec: bytes) -> bytes:
    return _blz_run(['-eo'], dec)


def arm9_decompress(path: Path) -> bytearray:
    raw = path.read_bytes()
    if not raw.endswith(NITRO_TRAILER):
        sys.exit(f'{path}: missing Nitro trailer — not a compressed arm9.bin')
    return bytearray(_blz_run(['-d'], raw[:-len(NITRO_TRAILER)]))


def arm9_compress(dec: bytearray) -> bytes:
    prefix    = bytearray(dec[:ARM9_PLAINTEXT])
    comp_body = _blz_run(['-eo'], bytes(dec[ARM9_PLAINTEXT:]))
    new_end   = ARM9_BASE + ARM9_PLAINTEXT + len(comp_body)
    struct.pack_into('<I', prefix, ARM9_MODPARAMS + ARM9_COMPEND_OFF, new_end)
    return bytes(prefix) + comp_body + NITRO_TRAILER


def update_y9(y9_path: Path, overlay_id: int, new_size: int):
    y9 = bytearray(y9_path.read_bytes())
    off = overlay_id * Y9_ENTRY_SIZE + Y9_FLAGS_OFF
    old_flags = struct.unpack_from('<I', y9, off)[0]
    new_flags = Y9_COMPRESS_FLAG | (new_size & 0xFFFFFF)
    struct.pack_into('<I', y9, off, new_flags)
    y9_path.write_bytes(y9)
    print(f'  y9.bin overlay {overlay_id}: flags 0x{old_flags:08x} → 0x{new_flags:08x} '
          f'(size 0x{new_size:x})')


# ── patch logic (each mutates dec in-place) ───────────────────────────────────

def _encode_mov_imm(rd, value):
    for rot in range(16):
        v = ((value << (2*rot)) | (value >> (32 - 2*rot))) & 0xFFFFFFFF if rot else value
        if v <= 0xFF:
            return 0xE3A00000 | (rd << 12) | (rot << 8) | v
    raise ValueError(f'0x{value:x} not encodable as ARM rotated imm8')


def _bl_encode(src_ram: int, tgt_ram: int) -> int:
    return 0xEB000000 | (((tgt_ram - src_ram - 8) // 4) & 0xFFFFFF)


def apply_grow_msg_pool(dec: bytearray, pool_size: int):
    off = 0x02026EC8 - ARM9_BASE
    new_inst = _encode_mov_imm(1, pool_size)
    cur = struct.unpack_from('<I', dec, off)[0]
    struct.pack_into('<I', dec, off, new_inst)
    print(f'  grow_msg_pool: 0x02026EC8: 0x{cur:08x} → 0x{new_inst:08x}  '
          f'(pool 0 size → 0x{pool_size:x})')


def apply_grow_actionhelp(dec: bytearray):
    # No binary patch needed if msg_actionhelp fits the original 0x3000 buffer.
    # Direct-calling 0x0205d9e8 corrupts battle monster/action state.
    print("  grow_actionhelp: using original 0x3000 actionhelp buffer")


def apply_cheat_taint(dec: bytearray):
    """Make gameplay-option use permanently visible to RetroAchievements.

    The hook replaces the tiny accessor at 0x020633bc.  Its trampoline preserves
    the accessor's original return value while ORing the marker into save-backed
    RAM.  It never clears the marker.
    """
    hook_off = CHEAT_TAINT_HOOK - ARM9_BASE
    cave_off = CHEAT_TAINT_CAVE - ARM9_BASE
    expected_hook = (0xE59F0000, 0xE12FFF1E)
    current_hook = struct.unpack_from('<II', dec, hook_off)
    if current_hook != expected_hook:
        sys.exit(
            f'cheat_taint: unexpected code at 0x{CHEAT_TAINT_HOOK:08x}: '
            f'{current_hook[0]:08x} {current_hook[1]:08x}'
        )

    cave_size = 0x20
    if cave_off < 0 or cave_off + cave_size > len(dec):
        sys.exit('cheat_taint: ARM9 code cave is outside the decompressed image')
    if any(dec[cave_off:cave_off + cave_size]):
        sys.exit(f'cheat_taint: code cave at 0x{CHEAT_TAINT_CAVE:08x} is not empty')

    # ldr r1, [pc, #0x10]       ; marker byte address
    # ldrb r2, [r1]
    # orr r2, r2, #0x20
    # strb r2, [r1]
    # ldr r0, [pc, #4]          ; original accessor return value
    # bx lr
    # .word CHEAT_TAINT_ADDR, SAVE_PAYLOAD_BASE
    cave_words = (
        0xE59F1010,
        0xE5D12000,
        0xE3822020,
        0xE5C12000,
        0xE59F0004,
        0xE12FFF1E,
        CHEAT_TAINT_ADDR,
        SAVE_PAYLOAD_BASE,
    )
    struct.pack_into('<8I', dec, cave_off, *cave_words)
    branch = 0xEA000000 | (((CHEAT_TAINT_CAVE - CHEAT_TAINT_HOOK - 8) // 4) & 0xFFFFFF)
    struct.pack_into('<I', dec, hook_off, branch)
    print(
        f'  cheat_taint: gameplay options mark RAM 0x{CHEAT_TAINT_ADDR:08x} '
        f'with mask 0x{CHEAT_TAINT_MASK:02x}'
    )



def apply_xp_mult(dec: bytearray, mult: float):
    off = 0x021e3c4c - OV_RAM_BASE
    n = max(-4, min(8, round(math.log2(max(mult, 1e-9)))))
    operand12 = (abs(n) << 7) | ((0 if n >= 0 else 1) << 5)
    new_inst  = (0xE084 << 16) | 0x4000 | operand12
    cur = struct.unpack_from('<I', dec, off)[0]
    struct.pack_into('<I', dec, off, new_inst)
    print(f'  xp_mult: {mult}× → nearest {2.0**n}× '
          f'({"LSL" if n >= 0 else "LSR"} #{abs(n)}): '
          f'0x{cur:08x} → 0x{new_inst:08x}')


def apply_full_reserve_exp(dec: bytearray):
    """Give substitutes and Monster Pen monsters the main party's full EXP."""
    guards = (
        (0x021E3B74, 0xEBF98508),
        (0x021E3C90, 0xE1A08004),
    )
    patches = (
        (0x021E4B74, 0xE5950038, 0xE5952030),
        (0x021E4B7C, 0xE280100B, 0xEA000002),
        (0x021E4C90, 0xE5900038, 0xE5903030),
        (0x021E4C98, 0xE2801017, 0xEA000002),
    )

    for addr, expected in guards:
        off = addr - OV_RAM_BASE
        current = struct.unpack_from('<I', dec, off)[0]
        if current != expected:
            sys.exit(
                f'full_reserve_exp: unexpected guard at 0x{addr:08x}: '
                f'0x{current:08x} (expected 0x{expected:08x})'
            )

    for addr, expected, new in patches:
        off = addr - OV_RAM_BASE
        current = struct.unpack_from('<I', dec, off)[0]
        if current != expected:
            sys.exit(
                f'full_reserve_exp: unexpected code at 0x{addr:08x}: '
                f'0x{current:08x} (expected 0x{expected:08x})'
            )
        struct.pack_into('<I', dec, off, new)
        print(f'  full_reserve_exp: 0x{addr:08x}: 0x{current:08x} → 0x{new:08x}')


def apply_xvariant_suffix(dec: bytearray):
    off = 0x02045028 - ARM9_BASE
    cur = struct.unpack_from('<I', dec, off)[0]
    struct.pack_into('<I', dec, off, 0xEAFFFFFF)
    print(f'  xvariant branch: 0x{cur:08x} → 0xEAFFFFFF')
    off = 0x0204502c - ARM9_BASE
    for i, w in enumerate([
        0xE1A02006, 0xE3A00002, 0xE3A01000, 0xEB00630C,
        0xE1A01000, 0xE1A00004, 0xEB00631C,
        0xE3550000, 0x0A000009,
        0xE1A00006, 0xEBFFFF4A, 0xE0850000, 0xE1A02000,
        0xE3A00002, 0xE3A01001, 0xEB006300,
        0xE1A01000, 0xE1A00004, 0xEB006320,
    ]):
        struct.pack_into('<I', dec, off + i*4, w)
    print(f'  xvariant block: 19 instructions rewritten at 0x0204502c')
    for off, new in [
        (0x0203507C - ARM9_BASE, 0xE1D510F6),
        (0x02035080 - ARM9_BASE, 0xE1D520F4),
        (0x020350A8 - ARM9_BASE, 0xE1D510FE),
        (0x020350AC - ARM9_BASE, 0xE1D520FC),
    ]:
        cur = struct.unpack_from('<I', dec, off)[0]
        struct.pack_into('<I', dec, off, new)
        print(f'  xvariant synth: 0x{ARM9_BASE+off:08x}: 0x{cur:08x} → 0x{new:08x}')


def apply_gender_icons(nftr_path: Path):
    CELL = 96
    tiles_dir = Path(__file__).resolve().parent / 'icon_tiles'
    targets = {594: 'plus.bin', 595: 'minus.bin', 596: 'neutral.bin'}
    tiles = {}
    for gi, fname in targets.items():
        p = tiles_dir / fname
        if not p.exists():
            print(f'  WARNING: {p} not found — skipping gender icons'); return
        d = p.read_bytes()
        if len(d) != CELL:
            print(f'  WARNING: {fname} wrong size — skipping gender icons'); return
        tiles[gi] = d
    font = bytearray(nftr_path.read_bytes())
    i = font.find(b'PLGC')
    if i < 0:
        print('  WARNING: PLGC block not found — skipping gender icons'); return
    base = i + 0x10
    for gi, tile in tiles.items():
        off = base + gi * CELL
        font[off:off + CELL] = tile
        print(f'  gender_icons: glyph {gi} @ 0x{off:x} ← {targets[gi]}')
    nftr_path.write_bytes(bytes(font))
    print(f'  gender_icons: wrote {nftr_path}')


def apply_scout_offense(dec: bytearray):
    off = 0x021fbae8 - OV_RAM_BASE
    cur = struct.unpack_from('<I', dec, off)[0]
    struct.pack_into('<I', dec, off, 0xE1A00000)
    print(f'  scout_offense: 0x021fbae8: 0x{cur:08x} → 0xE1A00000 (NOP)')


def apply_scout_penalty(dec: bytearray):
    off = 0x021fa3f8 - OV_RAM_BASE
    cur = struct.unpack_from('<I', dec, off)[0]
    struct.pack_into('<I', dec, off, 0xE3A01001)
    print(f'  scout_penalty: 0x021fa3f8: 0x{cur:08x} → 0xE3A01001 (MOV r1,#1)')


def apply_synthesis_level(dec: bytearray, level: int):
    for off in (0x048B4C, 0x0494E4, 0x0494EC, 0x049A18):
        cur = struct.unpack_from('<I', dec, off)[0]
        new = (cur & 0xFFFFFF00) | (level & 0xFF)
        struct.pack_into('<I', dec, off, new)
        print(f'  synthesis_level: file+0x{off:x}: 0x{cur:08x} → 0x{new:08x}')


def apply_synthesis_polarity(dec: bytearray):
    for off, new in [
        (0x0484DC, 0xE3A00002), (0x0484F0, 0xE3A00002),
        (0x0485C4, 0xE3A00002), (0x0485D8, 0xE3A00002),
        (0x048B54, 0xE3A00002), (0x049484, 0xE3A00002),
        (0x049498, 0xE3A00000), (0x048530, 0xE3A00000),
        (0x048B74, 0xE3A00000), (0x04860C, 0xE3A00000),
        (0x049A2C, 0xE3A01001), (0x0494F4, 0xE3A01001),
    ]:
        cur = struct.unpack_from('<I', dec, off)[0]
        struct.pack_into('<I', dec, off, new)
        print(f'  synthesis_polarity: file+0x{off:x}: 0x{cur:08x} → 0x{new:08x}')


# ── patch definitions ─────────────────────────────────────────────────────────

def _parse_hex_int(s): return int(s, 0)

PATCHES = [
    dict(key='grow_msg_pool',      label='msg Pool Size Fix',
         required=True,  target='arm9',
         param=dict(default=0x35000, fmt=hex, parse=_parse_hex_int,
                    validate=lambda v: v > 0 and v % 0x1000 == 0,
                    validate_msg='must be a positive multiple of 0x1000')),

    dict(key='grow_actionhelp',    label='actionhelp Message Fix',
         required=True,  target='ov0001', param=None),

    dict(key='xp_mult',            label='Set XP Multiplier',
         required=False, target='ov0001',
         param=dict(default=2.0, fmt=str, parse=float,
                    validate=lambda v: 0.0625 <= v <= 256.0,
                    validate_msg='must be between 0.0625 and 256.0')),

    dict(key='full_reserve_exp',   label='Give Substitutes and Monster Pen Full Party EXP',
         required=False, target='ov0001', param=None),

    dict(key='xvariant_suffix',    label='Apply X/XY Variant Suffix Fix',
         required=False, target='arm9',   param=None),

    dict(key='gender_icons',       label='Replace Gender Icons with Polarity',
         required=False, target='nftr',   param=None),

    dict(key='scout_offense',      label='Make "Took offense" NOT Disable Scouting',
         required=False, target='ov0001', param=None),

    dict(key='scout_penalty',      label='Remove Multiple Species Owned Check From Scouting',
         required=False, target='ov0001', param=None),

    dict(key='synthesis_level',    label='Set Minimum Synthesis Level',
         required=False, target='ov0000',
         param=dict(default=10, fmt=str, parse=int,
                    validate=lambda v: 1 <= v <= 99,
                    validate_msg='must be between 1 and 99')),

    dict(key='synthesis_polarity', label='Remove Synthesis Polarity Requirement',
         required=False, target='ov0000', param=None),
]


# ── text menu ─────────────────────────────────────────────────────────────────

def _print_menu(state: dict) -> None:
    print()
    print('DQMJ2P Pro ROM Patcher — patch selection')
    print('─' * 50)
    for i, p in enumerate(PATCHES):
        s    = state[p['key']]
        mark = '*' if s['selected'] else ' '
        vstr = ': ' + p['param']['fmt'](s['value']) if p['param'] else ''
        req  = '  (required)' if p['required'] else ''
        print(f'  {i+1:2d}. [{mark}] {p["label"]}{vstr}{req}')
    print()
    print('  Enter a number to toggle/edit, A to apply all, Q to quit.')


def run_menu() -> dict:
    state = {p['key']: {'selected': p['required'],
                         'value':    p['param']['default'] if p['param'] else None}
             for p in PATCHES}

    while True:
        _print_menu(state)
        try:
            raw = input('> ').strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit('\nAborted.')

        if raw.lower() == 'q':
            sys.exit('Aborted.')
        if raw.lower() == 'a':
            break

        try:
            idx = int(raw) - 1
        except ValueError:
            print('  Enter a number, A, or Q.')
            continue
        if not (0 <= idx < len(PATCHES)):
            print(f'  Number must be 1–{len(PATCHES)}.')
            continue

        p = PATCHES[idx]
        s = state[p['key']]

        if p['param']:
            pd      = p['param']
            default = pd['fmt'](s['value'])
            try:
                val_str = input(f'  {p["label"]} [{default}]: ').strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if not val_str:
                if not p['required']:
                    s['selected'] = True
                continue
            try:
                v = pd['parse'](val_str)
            except (ValueError, OverflowError):
                print('  Invalid value — not changed.')
                continue
            if not pd['validate'](v):
                print(f'  {pd["validate_msg"]} — not changed.')
                continue
            s['value'] = v
            if not p['required']:
                s['selected'] = True
        elif p['required']:
            print(f'  {p["label"]} is required and cannot be disabled.')
        else:
            s['selected'] = not s['selected']

    return state


# ── file discovery ────────────────────────────────────────────────────────────

def _find_one(rom_dir: Path, name: str) -> Path | None:
    """Return the first file named *name* found anywhere under rom_dir."""
    return next(rom_dir.rglob(name), None)


def find_rom(rom_dir: Path) -> dict:
    searches = {
        'arm9':   'arm9.bin',
        'ov0000': 'overlay_0000.bin',
        'ov0001': 'overlay_0001.bin',
        'y9':     'y9.bin',
        'nftr':   'font_16x16.NFTR',
    }
    found, missing = {}, []
    for key, name in searches.items():
        path = _find_one(rom_dir, name)
        if path:
            found[key] = path
        else:
            missing.append(name)
    if missing:
        print('Warning: files not found:', ', '.join(missing))
    return found


def _input_path(prompt: str, default: str = '') -> Path:
    try:
        raw = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit('\nAborted.')
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1]
    return Path(raw if raw else default)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--rom', default=None,
                    help='ROM directory (default: prompted interactively)')
    args = ap.parse_args()

    if not os.path.isfile(BLZ):
        sys.exit(f'blz.exe not found at {BLZ}')

    if args.rom:
        rom_dir = Path(args.rom)
    else:
        rom_dir = _input_path('Enter ROM directory [default: Pro_ROM]: ', default='Pro_ROM')
    if not rom_dir.is_dir():
        sys.exit(f'directory not found: {rom_dir}')
    files = find_rom(rom_dir)

    state = run_menu()

    def sel(key): return state[key]['selected']
    def val(key): return state[key]['value']

    taint_save = any(sel(key) for key in CHEAT_TAINT_PATCHES)

    print()
    print('Applying patches...')
    print()

    # ── arm9 ──────────────────────────────────────────────────────────────────
    if 'arm9' in files:
        print('arm9.bin:')
        arm9_path = files['arm9']
        dec = arm9_decompress(arm9_path)
        apply_grow_msg_pool(dec, val('grow_msg_pool'))
        if taint_save:
            apply_cheat_taint(dec)
        if sel('xvariant_suffix'):
            apply_xvariant_suffix(dec)
        final = arm9_compress(dec)
        arm9_path.write_bytes(final)
        print(f'  wrote {arm9_path} ({len(final):#x} bytes)')
        print()
    else:
        if taint_save:
            sys.exit('arm9.bin not found — refusing to create an unmarked gameplay-patched ROM')
        print('WARNING: arm9.bin not found — skipping arm9 patches')

    # ── overlay_0001 ──────────────────────────────────────────────────────────
    if 'ov0001' in files and 'y9' in files:
        print('overlay_0001.bin:')
        ov1  = files['ov0001']
        orig = ov1.stat().st_size
        dec  = overlay_decompress(ov1)
        apply_grow_actionhelp(dec)
        if sel('xp_mult'):       apply_xp_mult(dec, val('xp_mult'))
        if sel('full_reserve_exp'): apply_full_reserve_exp(dec)
        if sel('scout_offense'): apply_scout_offense(dec)
        if sel('scout_penalty'): apply_scout_penalty(dec)
        comp = overlay_compress(bytes(dec))
        ov1.write_bytes(comp)
        print(f'  wrote {ov1} ({len(comp):#x} bytes, was {orig:#x})')
        if len(comp) != orig:
            update_y9(files['y9'], 1, len(comp))
        else:
            print('  compressed size unchanged — y9.bin overlay 1 not modified')
        print()
    else:
        miss = [n for n, k in [('overlay_0001.bin', 'ov0001'), ('y9.bin', 'y9')]
                if k not in files]
        print(f'WARNING: {", ".join(miss)} not found — skipping overlay_0001 patches')

    # ── overlay_0000 ──────────────────────────────────────────────────────────
    if sel('synthesis_level') or sel('synthesis_polarity'):
        if 'ov0000' in files and 'y9' in files:
            print('overlay_0000.bin:')
            ov0  = files['ov0000']
            orig = ov0.stat().st_size
            dec  = overlay_decompress(ov0)
            if sel('synthesis_level'):    apply_synthesis_level(dec, val('synthesis_level'))
            if sel('synthesis_polarity'): apply_synthesis_polarity(dec)
            comp = overlay_compress(bytes(dec))
            ov0.write_bytes(comp)
            print(f'  wrote {ov0} ({len(comp):#x} bytes, was {orig:#x})')
            if len(comp) != orig:
                update_y9(files['y9'], 0, len(comp))
            else:
                print('  compressed size unchanged — y9.bin overlay 0 not modified')
            print()
        else:
            miss = [n for n, k in [('overlay_0000.bin', 'ov0000'), ('y9.bin', 'y9')]
                    if k not in files]
            print(f'WARNING: {", ".join(miss)} not found — skipping overlay_0000 patches')

    # ── NFTR ──────────────────────────────────────────────────────────────────
    if sel('gender_icons'):
        if 'nftr' in files:
            print('font_16x16.NFTR:')
            apply_gender_icons(files['nftr'])
            print()
        else:
            print('WARNING: font_16x16.NFTR not found — skipping gender icons')

    print('Done.')


if __name__ == '__main__':
    main()
