#!/usr/bin/env python3
"""
sf2ton.py — Convert a SoundFont (.sf2) to Sega Saturn .TON + .MAP files.

The .TON file contains instrument definitions (voices/layers mapping to SCSP
slot parameters) plus embedded PCM sample data.  The .MAP file tells the
Saturn sound driver where to load the tone bank in the SCSP's 512KB sound RAM.

Usage:
  python3 sf2ton.py input.sf2 [-o output.ton] [--map output.map]
                               [--base-addr 0x30000] [--bank 0]

References:
  - kingshriek's ssfinfo.py/tonext.py (VGMToolbox) — TON format documentation
  - VGMTrans SegSatInstrSet — ADSR conversion tables (from MAME SCSP)
  - Sega Sound Driver Implementation Manual (ST-241)
"""

import struct
import sys
import os
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ── SF2 Parser (minimal, handles what we need) ──────────────────────

def read_sf2_chunks(data: bytes) -> dict:
    """Parse RIFF/SF2 into a dict of chunk name → data."""
    chunks = {}
    if data[:4] != b'RIFF' or data[8:12] != b'sfbk':
        raise ValueError("Not a valid SF2 file")
    pos = 12
    while pos < len(data):
        chunk_id = data[pos:pos+4].decode('ascii', errors='replace')
        chunk_size = struct.unpack('<I', data[pos+4:pos+8])[0]
        if chunk_id == 'LIST':
            list_type = data[pos+8:pos+12].decode('ascii', errors='replace')
            # Recurse into LIST
            inner = read_sf2_chunks_flat(data[pos+12:pos+8+chunk_size])
            chunks.update(inner)
        else:
            chunks[chunk_id] = data[pos+8:pos+8+chunk_size]
        pos += 8 + chunk_size + (chunk_size & 1)  # pad to even
    return chunks


def read_sf2_chunks_flat(data: bytes) -> dict:
    """Parse inner LIST chunks."""
    chunks = {}
    pos = 0
    while pos < len(data):
        chunk_id = data[pos:pos+4].decode('ascii', errors='replace')
        chunk_size = struct.unpack('<I', data[pos+4:pos+8])[0]
        if chunk_id == 'LIST':
            list_type = data[pos+8:pos+12].decode('ascii', errors='replace')
            inner = read_sf2_chunks_flat(data[pos+12:pos+8+chunk_size])
            chunks.update(inner)
        else:
            chunks[chunk_id] = data[pos+8:pos+8+chunk_size]
        pos += 8 + chunk_size + (chunk_size & 1)
    return chunks


@dataclass
class SF2Sample:
    name: str
    start: int      # offset in smpl chunk (in samples)
    end: int
    loop_start: int
    loop_end: int
    sample_rate: int
    original_key: int
    pitch_correction: int  # cents
    sample_type: int       # 1=mono, 2=right, 4=left, 8=linked


@dataclass
class SF2Zone:
    """An instrument zone (key/vel range → sample + generators)."""
    key_lo: int = 0
    key_hi: int = 127
    vel_lo: int = 0
    vel_hi: int = 127
    sample_id: int = -1
    # Generator values (SF2 spec)
    attenuation: float = 0.0    # cB (centibels)
    pan: float = 0.0            # -500..500 (% × 10)
    sample_modes: int = 0       # 0=no loop, 1=loop, 3=loop+release
    root_key: int = -1          # override (-1 = use sample header)
    fine_tune: int = 0          # cents
    coarse_tune: int = 0        # semitones
    # Volume envelope (in timecents, 1200tc = 1 second)
    vol_attack: float = -12000  # timecents
    vol_decay: float = -12000
    vol_sustain: float = 0      # cB attenuation (0 = max sustain)
    vol_release: float = -12000
    # Mod envelope → pitch (cents)
    mod_env_to_pitch: float = 0     # cents (gen 7)
    mod_env_delay: float = -12000   # timecents (gen 25)
    mod_env_attack: float = -12000  # timecents (gen 26)
    mod_env_hold: float = -12000    # timecents (gen 27)
    mod_env_decay: float = -12000   # timecents (gen 28)
    mod_env_sustain: float = 0      # 0.1% units (gen 29) — 0=max, 1000=silence
    mod_env_release: float = -12000 # timecents (gen 30)
    # Vibrato LFO → pitch
    vib_lfo_to_pitch: float = 0     # cents (gen 6)
    vib_lfo_delay: float = -12000   # timecents (gen 23)
    vib_lfo_freq: float = 0         # absolute cents from 8.176 Hz (gen 24)
    # Sample address offsets (in sample points, coarse*32768 already applied)
    start_off: int = 0
    end_off: int = 0
    loop_start_off: int = 0
    loop_end_off: int = 0


def parse_sf2_samples(shdr_data: bytes) -> List[SF2Sample]:
    """Parse shdr chunk into sample list."""
    samples = []
    entry_size = 46
    count = len(shdr_data) // entry_size
    for i in range(count - 1):  # last entry is EOS
        off = i * entry_size
        name = shdr_data[off:off+20].split(b'\x00')[0].decode('ascii', errors='replace')
        start, end, ls, le, sr, key, corr, stype, link = struct.unpack(
            '<IIIIIBbHH', shdr_data[off+20:off+46])
        samples.append(SF2Sample(name, start, end, ls, le, sr, key, corr, stype))
    return samples


# SF2 generator enum values we care about
GEN_KEY_RANGE = 43
GEN_VEL_RANGE = 44
GEN_SAMPLE_ID = 53
GEN_ATTENUATION = 48
GEN_PAN = 17
GEN_SAMPLE_MODES = 54
GEN_ROOT_KEY = 58
GEN_FINE_TUNE = 52
GEN_COARSE_TUNE = 51
GEN_VOL_ATTACK = 34
GEN_VOL_DECAY = 36
GEN_VOL_SUSTAIN = 37
GEN_VOL_RELEASE = 38
GEN_INSTRUMENT = 41
GEN_VIB_LFO_TO_PITCH = 6
GEN_MOD_ENV_TO_PITCH = 7
GEN_VIB_LFO_DELAY = 23
GEN_VIB_LFO_FREQ = 24
GEN_MOD_ENV_DELAY = 25
GEN_MOD_ENV_ATTACK = 26
GEN_MOD_ENV_HOLD = 27
GEN_MOD_ENV_DECAY = 28
GEN_MOD_ENV_SUSTAIN = 29
GEN_MOD_ENV_RELEASE = 30


# Generator defaults from the SF2 2.04 spec (section 8.1.3) for the ones we use.
# Anything not listed defaults to 0.
GEN_DEFAULTS = {
    GEN_VOL_ATTACK: -12000, GEN_VOL_DECAY: -12000, GEN_VOL_RELEASE: -12000,
    GEN_MOD_ENV_DELAY: -12000, GEN_MOD_ENV_ATTACK: -12000, GEN_MOD_ENV_HOLD: -12000,
    GEN_MOD_ENV_DECAY: -12000, GEN_MOD_ENV_RELEASE: -12000,
    GEN_VIB_LFO_DELAY: -12000, GEN_ROOT_KEY: -1,
}

# Generators that are NOT added from the preset level onto instrument values
# (ranges are intersected; the rest are instrument-only per SF2 spec 8.5).
GEN_NON_ADDITIVE = {0, 1, 2, 3, 4, 12, 45, 46, 47, 50, 54, 57, 58,
                    GEN_INSTRUMENT, GEN_SAMPLE_ID, GEN_KEY_RANGE, GEN_VEL_RANGE}


def _parse_zone_lists(hdr: bytes, rec_size: int, bag_field_off: int,
                      bag_data: bytes, gen_data: bytes, link_gen: int):
    """Parse inst/ibag/igen or phdr/pbag/pgen into
    [(name, header_record, [zone_gens, ...]), ...].

    Each zone is a dict gen_id -> signed 16-bit amount.  If the first zone has no
    link generator (sampleID for instruments, instrument for presets) it is the
    *global* zone, and its generators are used as defaults for every other zone.
    """
    bags = [struct.unpack_from('<H', bag_data, i * 4)[0] for i in range(len(bag_data) // 4)]
    gens = [struct.unpack_from('<Hh', gen_data, i * 4) for i in range(len(gen_data) // 4)]
    count = len(hdr) // rec_size - 1  # last record is the terminator
    out = []
    for i in range(count):
        rec = hdr[i * rec_size:(i + 1) * rec_size]
        name = rec[:20].split(b'\x00')[0].decode('ascii', errors='replace')
        b0 = struct.unpack_from('<H', hdr, i * rec_size + bag_field_off)[0]
        b1 = struct.unpack_from('<H', hdr, (i + 1) * rec_size + bag_field_off)[0]
        glob = {}
        zones = []
        for bi in range(b0, min(b1, len(bags))):
            g0 = bags[bi]
            g1 = bags[bi + 1] if bi + 1 < len(bags) else len(gens)
            z = {}
            for gid, val in gens[g0:min(g1, len(gens))]:
                z[gid] = val
            if link_gen not in z:
                if bi == b0:
                    glob = z  # global zone
                continue      # other link-less zones are ignored per spec
            zones.append(z)
        out.append((name, rec, [{**glob, **z} for z in zones]))
    return out


def _gen_range(z: dict, gid: int) -> Tuple[int, int]:
    v = z.get(gid)
    # A 0..0 range is unplayable (velocity 0 is note-off, key 0 is never used);
    # some editors write it to mean "unset", so treat it as the full range.
    if v is None or v == 0:
        return 0, 127
    v &= 0xFFFF
    return v & 0xFF, (v >> 8) & 0xFF


def _zone_from_gens(g: dict, key: Tuple[int, int], vel: Tuple[int, int]) -> SF2Zone:
    def get(gid):
        return g.get(gid, GEN_DEFAULTS.get(gid, 0))

    z = SF2Zone()
    z.key_lo, z.key_hi = key
    z.vel_lo, z.vel_hi = vel
    z.sample_id = g[GEN_SAMPLE_ID] & 0xFFFF
    z.attenuation = get(GEN_ATTENUATION)
    z.pan = get(GEN_PAN)
    z.sample_modes = get(GEN_SAMPLE_MODES) & 3
    root = get(GEN_ROOT_KEY)
    z.root_key = root if 0 <= root <= 127 else -1
    z.fine_tune = get(GEN_FINE_TUNE)
    z.coarse_tune = get(GEN_COARSE_TUNE)
    z.vol_attack = get(GEN_VOL_ATTACK)
    z.vol_decay = get(GEN_VOL_DECAY)
    z.vol_sustain = get(GEN_VOL_SUSTAIN)
    z.vol_release = get(GEN_VOL_RELEASE)
    z.mod_env_to_pitch = get(GEN_MOD_ENV_TO_PITCH)
    z.mod_env_delay = get(GEN_MOD_ENV_DELAY)
    z.mod_env_attack = get(GEN_MOD_ENV_ATTACK)
    z.mod_env_hold = get(GEN_MOD_ENV_HOLD)
    z.mod_env_decay = get(GEN_MOD_ENV_DECAY)
    z.mod_env_sustain = get(GEN_MOD_ENV_SUSTAIN)
    z.mod_env_release = get(GEN_MOD_ENV_RELEASE)
    z.vib_lfo_to_pitch = get(GEN_VIB_LFO_TO_PITCH)
    z.vib_lfo_delay = get(GEN_VIB_LFO_DELAY)
    z.vib_lfo_freq = get(GEN_VIB_LFO_FREQ)
    # Sample address offsets: fine (gens 0-3) + coarse * 32768 (gens 4, 12, 45, 50)
    z.start_off = get(0) + 32768 * get(4)
    z.end_off = get(1) + 32768 * get(12)
    z.loop_start_off = get(2) + 32768 * get(45)
    z.loop_end_off = get(3) + 32768 * get(50)
    return z


def parse_sf2_presets(chunks: dict) -> List[Tuple[str, int, int, List[SF2Zone]]]:
    """Parse presets and resolve them down to playable zones.

    Returns [(name, program, bank, [SF2Zone, ...]), ...].  Handles instrument and
    preset global zones, presets that reference several instruments, preset-level
    generator offsets (added onto instrument values) and key/velocity range
    intersection between preset and instrument zones.
    """
    for c in ('inst', 'ibag', 'igen', 'phdr', 'pbag', 'pgen'):
        if not chunks.get(c):
            raise ValueError(f"Missing '{c}' chunk in SF2")

    insts = _parse_zone_lists(chunks['inst'], 22, 20, chunks['ibag'], chunks['igen'],
                              GEN_SAMPLE_ID)
    presets = _parse_zone_lists(chunks['phdr'], 38, 24, chunks['pbag'], chunks['pgen'],
                                GEN_INSTRUMENT)
    result = []
    for name, rec, pzones in presets:
        prog, bank = struct.unpack_from('<HH', rec, 20)
        zones = []
        for pz in pzones:
            inst_idx = pz[GEN_INSTRUMENT] & 0xFFFF
            if inst_idx >= len(insts):
                continue
            pk = _gen_range(pz, GEN_KEY_RANGE)
            pv = _gen_range(pz, GEN_VEL_RANGE)
            for iz in insts[inst_idx][2]:
                ik = _gen_range(iz, GEN_KEY_RANGE)
                iv = _gen_range(iz, GEN_VEL_RANGE)
                key = (max(pk[0], ik[0]), min(pk[1], ik[1]))
                vel = (max(pv[0], iv[0]), min(pv[1], iv[1]))
                if key[0] > key[1] or vel[0] > vel[1]:
                    continue
                g = dict(iz)
                for gid, val in pz.items():
                    if gid not in GEN_NON_ADDITIVE:
                        g[gid] = g.get(gid, GEN_DEFAULTS.get(gid, 0)) + val
                zones.append(_zone_from_gens(g, key, vel))
        result.append((name, prog, bank, zones))
    return result

# ── SCSP ADSR Conversion ────────────────────────────────────────────

# Attack rate times in ms (from MAME SCSP, indexed by AR*2)
AR_TIMES = [
    100000,100000,8100.0,6900.0,6000.0,4800.0,4000.0,3400.0,3000.0,2400.0,
    2000.0,1700.0,1500.0,1200.0,1000.0,860.0,760.0,600.0,500.0,430.0,
    380.0,300.0,250.0,220.0,190.0,150.0,130.0,110.0,95.0,76.0,
    63.0,55.0,47.0,38.0,31.0,27.0,24.0,19.0,15.0,13.0,
    12.0,9.4,7.9,6.8,6.0,4.7,3.8,3.4,3.0,2.4,
    2.0,1.8,1.6,1.3,1.1,0.93,0.85,0.65,0.53,0.44,
    0.40,0.35,0.0,0.0
]

# Decay/release rate times in ms
DR_TIMES = [
    100000,100000,118200.0,101300.0,88600.0,70900.0,59100.0,50700.0,
    44300.0,35500.0,29600.0,25300.0,22200.0,17700.0,14800.0,12700.0,
    11100.0,8900.0,7400.0,6300.0,5500.0,4400.0,3700.0,3200.0,
    2800.0,2200.0,1800.0,1600.0,1400.0,1100.0,920.0,790.0,
    690.0,550.0,460.0,390.0,340.0,270.0,230.0,200.0,
    170.0,140.0,110.0,98.0,85.0,68.0,57.0,49.0,
    43.0,34.0,28.0,25.0,22.0,18.0,14.0,12.0,
    11.0,8.5,7.1,6.1,5.4,4.3,3.6,3.1
]


def timecents_to_ms(tc: float) -> float:
    """Convert SF2 timecents to milliseconds."""
    if tc <= -12000:
        return 0.0
    return 1000.0 * (2.0 ** (tc / 1200.0))


def ms_to_ar(ms: float) -> int:
    """Find the closest SCSP attack rate for a given time in ms."""
    # AR=0,1 are essentially infinite, AR=31 is instant
    if ms <= 0:
        return 31
    # VGMTrans applies a 0.625 factor: attack_time = ARTimes[ar*2] * 0.625
    ms_adjusted = ms / 0.625
    best = 2
    best_diff = abs(AR_TIMES[2] - ms_adjusted)
    for i in range(2, 62):
        diff = abs(AR_TIMES[i] - ms_adjusted)
        if diff < best_diff:
            best_diff = diff
            best = i
    # AR register value = index / 2
    return max(2, min(31, best // 2))


def ms_to_dr(ms: float) -> int:
    """Find the closest SCSP decay/release rate for a given time in ms."""
    if ms <= 0:
        return 31
    if ms > 100000:
        return 0
    best = 2
    best_diff = abs(DR_TIMES[2] - ms)
    for i in range(2, 64):
        diff = abs(DR_TIMES[i] - ms)
        if diff < best_diff:
            best_diff = diff
            best = i
    return max(0, min(31, best // 2))


def cb_to_tl(cb: float) -> int:
    """Convert centibels attenuation to SCSP Total Level (0-255).
    TL ≈ 0.375 dB/step, so 1 cB = 0.1 dB ≈ 0.267 TL steps."""
    if cb <= 0:
        return 0
    tl = int(round(cb * 0.1 / 0.375))
    return max(0, min(255, tl))


def sf2_pan_to_dipan(pan: float) -> int:
    """Convert SF2 pan (-500..+500) to SCSP DIPAN (0-31).
    DIPAN: bit4=direction, bits3:0=attenuation.
    0x00=full left, 0x0F=center-ish, 0x1F=full right... actually
    DIPAN 0=center, 0x0F=hard left attenuated, 0x1F=hard right attenuated.
    This is a simplification."""
    # Simple linear mapping
    if pan <= -500:
        return 0x1F  # full left
    if pan >= 500:
        return 0x0F  # full right
    # Center = 0
    if abs(pan) < 50:
        return 0
    if pan < 0:
        atten = int((-pan / 500.0) * 15)
        return 0x10 | min(15, atten)
    else:
        atten = int((pan / 500.0) * 15)
        return min(15, atten)


def sustain_cb_to_dl(cb: float) -> int:
    """Convert SF2 sustain attenuation (cB) to SCSP Decay Level (0-31).
    DL=0 means sustain at max volume, DL=31 means sustain at silence."""
    if cb <= 0:
        return 0  # max sustain
    if cb >= 1000:
        return 31  # silence
    return min(31, int(cb / 32.0))


def build_peg_entry(zone: 'SF2Zone') -> bytes:
    """Build a 10-byte PEG entry from SF2 mod envelope → pitch parameters.

    PEG format (from ssfinfo.py):
      [0] DLY   — delay before envelope starts
      [1] OL    — offset level (initial pitch, signed, in semitones-ish units)
      [2] AR    — attack rate (signed, higher = faster)
      [3] AL    — attack level (signed, pitch target after attack)
      [4] DR    — decay rate
      [5] DL    — decay level (pitch target after decay)
      [6] SR    — sustain→release transition rate
      [7] SL    — sustain level
      [8] RR    — release rate
      [9] RL    — release level (pitch target after release, typically 0)

    Levels are in ~semitone units (exact scale is driver-dependent).
    Rates: higher magnitude = faster. Range roughly -128..127.
    """
    depth = zone.mod_env_to_pitch  # cents
    if abs(depth) < 10:
        return bytes(10)  # no meaningful pitch envelope

    # Scale depth to PEG level units (~semitones, clamped to signed byte)
    # The driver uses these as pitch offsets; 1 unit ≈ 1 semitone (100 cents)
    level_scale = depth / 100.0
    al = max(-128, min(127, int(round(level_scale))))  # attack target
    sl = 0   # sustain at zero offset
    rl = 0   # release target

    # Convert timecents to rate values.
    # PEG rates: higher = faster. Range: signed byte (-128..127).
    # In practice, positive values 1-127 are used for forward rates.
    # ~1 = very slow (~10s), ~127 = instant
    def tc_to_peg_rate(tc):
        ms = timecents_to_ms(tc)
        if ms <= 0:
            return 127
        if ms > 10000:
            return 1
        # Map 1ms→127, 10000ms→1 using log scale
        # log10(1)=0→127, log10(10000)=4→1
        return max(1, min(127, int(127 - 31.5 * math.log10(ms))))

    dly_ms = timecents_to_ms(zone.mod_env_delay)
    dly = max(0, min(255, int(dly_ms / 10.0)))  # ~10ms per unit

    ar = tc_to_peg_rate(zone.mod_env_attack)
    dr = tc_to_peg_rate(zone.mod_env_decay)
    sr = 0   # sustain holds
    rr = tc_to_peg_rate(zone.mod_env_release)

    # Sustain level of mod env: 0=full, 1000=zero
    # This scales the decay target between AL and 0
    sus_frac = 1.0 - min(1.0, zone.mod_env_sustain / 1000.0)
    dl = max(-128, min(127, int(round(al * sus_frac))))

    peg = struct.pack('bbbbbbbbbB',
                      dly & 0x7F,   # DLY (unsigned in practice)
                      al,           # OL — start offset (jump to attack level immediately)
                      ar, 0,        # AR, AL=0 (attack goes toward 0)
                      dr, dl,       # DR, DL (decay to sustain pitch)
                      sr, sl,       # SR, SL (sustain rate/level)
                      rr, rl & 0xFF)  # RR, RL (release)
    return peg


def build_plfo_entry(zone: 'SF2Zone') -> bytes:
    """Build a 4-byte PLFO entry from SF2 vibrato LFO parameters.

    PLFO format and timing (reversed from the 1.x/2.0x sound driver by VGMTrans,
    SegSatInstrSet.cpp):
      [0] DLY  — delay:      seconds = DLY^2 / (16 * R)
      [1] AMP  — slope:      depth_cents = AMP^2 * FRQ^2 / (8192*256/100)
      [2] FRQ  — period:     freq_hz = 32 * R / FRQ^2   (bigger = slower)
      [3] FADE — fade-in (0 = none)
    where R is the driver's PLFO update rate (~128.2 Hz).
    """
    depth = abs(zone.vib_lfo_to_pitch)  # cents
    if depth < 5:
        return bytes(4)  # no meaningful vibrato

    rate = PLFO_DRIVER_HZ
    freq_hz = 8.176 * (2.0 ** (zone.vib_lfo_freq / 1200.0))
    frq = int(round(math.sqrt(32.0 * rate / max(freq_hz, 0.01))))
    frq = max(1, min(255, frq))
    amp = int(round(math.sqrt(depth * (8192 * 256 / 100.0)) / frq))
    amp = max(1, min(255, amp))
    dly_s = timecents_to_ms(zone.vib_lfo_delay) / 1000.0
    dly = max(0, min(255, int(round(math.sqrt(dly_s * 16 * rate)))))
    return struct.pack('BBBB', dly, amp, frq, 0)

# ── TON Builder ─────────────────────────────────────────────────────

@dataclass
class TonLayer:
    """32-byte layer entry matching SCSP slot parameters."""
    start_note: int = 0
    end_note: int = 127
    base_note: int = 60
    fine_tune: int = 0
    # SCSP slot params
    sa: int = 0            # 20-bit sample start address (byte offset)
    lsa: int = 0           # loop start (samples from SA)
    lea: int = 0           # loop end (samples from SA)
    pcm8b: int = 0         # 0=16-bit, 1=8-bit
    lpctl: int = 0         # 0=no loop, 1=forward
    ar: int = 31           # attack rate (0-31)
    d1r: int = 0           # decay 1 rate
    d2r: int = 0           # decay 2 rate
    dl: int = 0            # decay level (sustain)
    rr: int = 14           # release rate
    tl: int = 0            # total level (attenuation)
    krs: int = 0           # key rate scaling
    disdl: int = 7         # direct send level
    dipan: int = 0         # direct pan
    # Unused for basic conversion
    sbctl: int = 0
    ssctl: int = 0
    mdl: int = 0
    mdxsl: int = 0
    mdysl: int = 0
    oct: int = 0
    fns: int = 0
    isel: int = 0
    imxl: int = 0
    velocity_id: int = 0
    peg_id: int = 0
    plfo_id: int = 0

    def pack(self) -> bytes:
        """Pack into 32 bytes matching TON layer format."""
        b = bytearray(0x20)
        b[0x00] = self.start_note & 0xFF
        b[0x01] = self.end_note & 0xFF
        # byte 2: PEON(7), PLON(6), FMCB(5), SBCTL(2:1), SSCTL high(0)
        peon = 1 if self.peg_id > 0 else 0
        plon = 1 if self.plfo_id > 0 else 0
        b[0x02] = (peon << 7) | (plon << 6) | (self.sbctl & 3) << 1 | (self.ssctl >> 1) & 1
        # byte 3: SSCTL low, LPCTL, PCM8B, SA high
        b[0x03] = ((self.ssctl & 1) << 7) | ((self.lpctl & 3) << 5) | \
                  ((self.pcm8b & 1) << 4) | ((self.sa >> 16) & 0xF)
        struct.pack_into('>H', b, 0x04, self.sa & 0xFFFF)
        struct.pack_into('>H', b, 0x06, self.lsa & 0xFFFF)
        struct.pack_into('>H', b, 0x08, self.lea & 0xFFFF)
        # byte A: D2R[4:0] << 3 | D1R[4:2]
        b[0x0A] = ((self.d2r & 0x1F) << 3) | ((self.d1r >> 2) & 0x7)
        # byte B: D1R[1:0] << 6 | EGHOLD << 5 | AR[4:0]
        b[0x0B] = ((self.d1r & 0x3) << 6) | (self.ar & 0x1F)
        # byte C: LPSLNK << 6 | KRS[3:0] << 2 | DL[4:3]  (SCSP slot reg 0x0A)
        b[0x0C] = ((self.krs & 0xF) << 2) | ((self.dl >> 3) & 0x3)
        # byte D: DL[2:0] << 5 | RR[4:0]
        b[0x0D] = ((self.dl & 0x7) << 5) | (self.rr & 0x1F)
        # byte E: MWH=0, MWE=0, MWL=0, STWINH=0, SDIR=0
        b[0x0E] = 0
        b[0x0F] = self.tl & 0xFF
        # bytes 10-11: MDL, MDXSL, MDYSL
        b[0x10] = (self.mdl << 4) | ((self.mdxsl >> 2) & 0xF)
        b[0x11] = ((self.mdxsl & 3) << 6) | (self.mdysl & 0x3F)
        # bytes 12-13: OCT[3:0] << 3 | FNS[9:8], FNS[7:0]  (SCSP slot reg 0x10).
        # The driver computes pitch from base_note/fine_tune and ignores these;
        # we leave them 0 like commercial TONs do.
        b[0x12] = ((self.oct & 0xF) << 3) | ((self.fns >> 8) & 0x3)
        b[0x13] = self.fns & 0xFF
        # bytes 14-15: LFO (all zero for basic conversion)
        b[0x14] = 0
        b[0x15] = 0
        b[0x16] = 0
        # byte 17: ISEL, IMXL
        b[0x17] = ((self.isel & 0xF) << 3) | (self.imxl & 0x7)
        # byte 18: DISDL, DIPAN
        b[0x18] = ((self.disdl & 0x7) << 5) | (self.dipan & 0x1F)
        b[0x19] = self.base_note & 0xFF
        b[0x1A] = struct.pack('b', max(-128, min(127, self.fine_tune)))[0]
        # bytes 1B-1C: FM (unused)
        b[0x1B] = 0
        b[0x1C] = 0
        b[0x1D] = self.velocity_id
        b[0x1E] = self.peg_id
        b[0x1F] = self.plfo_id
        return bytes(b)


@dataclass
class TonVoice:
    """Voice entry: header + layers."""
    bend_range: int = 2
    portamento: int = 0
    volume_bias: int = 0
    layers: List[TonLayer] = field(default_factory=list)

    def pack(self) -> bytes:
        hdr = bytearray(4)
        hdr[0] = self.bend_range & 0xF
        hdr[1] = self.portamento
        hdr[2] = struct.pack('b', len(self.layers) - 1)[0]
        hdr[3] = struct.pack('b', self.volume_bias)[0]
        return bytes(hdr) + b''.join(l.pack() for l in self.layers)


def build_ton(voices: List[TonVoice], pcm_data: bytes,
              peg_entries: Optional[List[bytes]] = None,
              plfo_entries: Optional[List[bytes]] = None) -> bytes:
    """Build a complete .TON file.

    Layer SA values are taken as offsets into pcm_data and rebased onto the
    file here.  The same TonVoice object may appear several times in `voices`
    (aliases for missing programs); it is stored once and pointed to repeatedly.
    """
    # Default mixer: all channels at EFSDL=0 (no DSP effect send)
    mixer = bytes([0x00] * 0x12)

    # Default velocity table: maps MIDI velocity to TL attenuation.
    # The VL table is a piecewise-linear curve with 4 segments:
    #   (slope0, point0, level0, slope1, point1, level1, slope2, point2, level2, slope3)
    # 'level' = TL attenuation (0=loudest, 127=silent).
    # Default curve: low velocity -> moderate atten, high velocity -> low atten.
    vl = struct.pack('bbBbbBbbBb', 25, 16, 54, 9, 49, 102, 19, 93, 122, 43)

    # PEG table: entry 0 = no envelope, additional entries from SF2 mod envelopes
    if peg_entries:
        peg = b''.join(peg_entries)
    else:
        peg = bytes([0x00] * 0x0A)

    # PLFO table: entry 0 = no LFO, additional entries from SF2 vibrato LFO
    if plfo_entries:
        plfo = b''.join(plfo_entries)
    else:
        plfo = bytes([0x00] * 0x04)

    # Calculate offsets
    header_size = 8 + len(voices) * 2  # 4 fixed offsets + voice offsets
    mixer_offset = header_size
    vl_offset = mixer_offset + len(mixer)
    peg_offset = vl_offset + len(vl)
    plfo_offset = peg_offset + len(peg)

    # Pack voices and calculate their offsets
    voice_offset = plfo_offset + len(plfo)
    unique = []
    voice_offsets = []
    placed = {}  # id(voice) -> offset
    cur_offset = voice_offset
    for v in voices:
        if id(v) not in placed:
            placed[id(v)] = cur_offset
            unique.append(v)
            cur_offset += 4 + 0x20 * len(v.layers)
        voice_offsets.append(placed[id(v)])
    if cur_offset > 0xFFFF:
        raise ValueError(f"TON header/voice data is {cur_offset} bytes; offsets are "
                         f"16-bit (max 65535). Reduce the number of presets/zones.")

    # PCM data follows voices
    pcm_offset = cur_offset
    if pcm_offset + len(pcm_data) > 0xFFFFF:
        raise ValueError("TON exceeds the 20-bit sample address range (1 MB)")
    for v in unique:
        for layer in v.layers:
            layer.sa += pcm_offset
    voice_data = [v.pack() for v in unique]

    # Build header
    hdr = bytearray()
    hdr += struct.pack('>H', mixer_offset)
    hdr += struct.pack('>H', vl_offset)
    hdr += struct.pack('>H', peg_offset)
    hdr += struct.pack('>H', plfo_offset)
    for off in voice_offsets:
        hdr += struct.pack('>H', off)

    # Assemble file
    ton = bytearray()
    ton += hdr
    ton += mixer
    ton += vl
    ton += peg
    ton += plfo
    for vd in voice_data:
        ton += vd
    ton += pcm_data

    return bytes(ton)


def build_map_entry(entry_type: int, bank: int, addr: int, size: int) -> bytes:
    """Build a single 8-byte MAP entry."""
    entry = bytearray(8)
    entry[0] = ((entry_type & 0xF) << 4) | (bank & 0xF)
    entry[1:4] = struct.pack('>I', addr)[1:]      # 24-bit address
    entry[4] = 0x80                                # transfer complete
    entry[5:8] = struct.pack('>I', size)[1:]       # 24-bit size
    return bytes(entry)


def build_map(tone_addr: int, tone_size: int,
              seq_addr: int, seq_size: int, bank: int = 1,
              extra_tones: Optional[List[Tuple[int, int, int]]] = None) -> bytes:
    """Build a .MAP file compatible with Saturn SGL sound driver.

    The SGL driver (sddrvs) expects bank 0 to hold its default tone/seq
    data at fixed addresses.  User music goes in bank 1+.

    The SaturnRingLib SEQ sample uses:
      MAP_CMD_SEQ = 0x11 (SEQ bank 1)
      MAP_CMD_TON = 0x01 (TONE bank 1)
    """
    entries = bytearray()

    # Bank 0 defaults (required by SGL sound driver)
    # These addresses match the standard SGL layout
    entries += build_map_entry(3, 0, 0x00C000, 0x010040)  # DSP_RAM
    entries += build_map_entry(2, 0, 0x01C040, 0x000540)  # DSP_PROG
    entries += build_map_entry(1, 0, 0x024580, 0x00016E)  # SEQ bank 0
    entries += build_map_entry(0, 0, 0x0246EE, 0x0088EC)  # TONE bank 0

    # User music (bank 1)
    entries += build_map_entry(1, bank, seq_addr, seq_size)   # SEQ
    entries += build_map_entry(0, bank, tone_addr, tone_size)  # TONE
    # Further tone banks (e.g. GM drum kits in bank 2)
    for tbank, taddr, tsize in (extra_tones or []):
        entries += build_map_entry(0, tbank, taddr, tsize)

    # Terminator
    entries += b'\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF'

    return bytes(entries)


# ── Conversion Logic ────────────────────────────────────────────────

# With OCT=FNS=0 the SCSP steps through a sample at 44100 Hz.  The sound driver
# computes OCT/FNS itself from (note - base_note, fine_tune); it does NOT read
# the OCT/FNS bytes of the layer (kingshriek, ssfinfo.py v0.06).  So sample-rate
# compensation has to be folded into base_note / fine_tune.
SCSP_RATE = 44100
# Driver PLFO/PEG tick rate: SCSP timer A at 44100 / (2 * (255 - 0xD4)) Hz, / 4.
PLFO_DRIVER_HZ = 44100.0 / (2 * (255 - 0xD4)) / 4.0
# LSA/LEA are 16-bit sample counts relative to SA.
MAX_SAMPLE_POINTS = 0xFFFF
# Velocity used to pick between velocity-split zones (TON layers have no velocity range).
REPRESENTATIVE_VELOCITY = 100
# Voices addressable by a SEQ program change (7-bit).
MAX_TON_VOICES = 128
# GM layout (--gm-banks / mid2seq -g): melodic programs in tone bank 1,
# drum kits in this tone bank.  Must match GM_DRUM_TONE_BANK in mid2seq.c.
GM_DRUM_TONE_BANK = 2


def _read_pcm(raw: bytes, a: int, b: int) -> List[int]:
    if b <= a:
        return []
    return list(struct.unpack_from(f'<{b - a}h', raw, a * 2))


def convert_sample(raw_16le: bytes, smp: SF2Sample, z: SF2Zone,
                   warn) -> Optional[Tuple[bytes, int, int, bool]]:
    """Cut one zone's sample out of the SF2 smpl chunk and convert it to SCSP
    16-bit big-endian PCM.  Returns (pcm, lsa, lea, looped) or None.

    - Zone sample-address offset generators are applied.
    - Looped samples are cut at the loop end and followed by a copy of the loop
      start sample, so LEA (exclusive, like SF2's loop end) has a correct guard
      sample for the SCSP's interpolation.  Data after the loop is never played.
    - One-shot samples get a short fade-out: with LPCTL=0 the SCSP hard-stops
      the slot at LEA, which clicks if the waveform isn't at zero there.
    """
    total = len(raw_16le) // 2
    start = max(0, min(smp.start + z.start_off, total))
    end = max(start, min(smp.end + z.end_off, total))
    ls = smp.loop_start + z.loop_start_off
    le = smp.loop_end + z.loop_end_off
    if end - start < 2:
        return None

    looped = z.sample_modes in (1, 3)
    if looped and not (start <= ls < le <= end and le - ls >= 2):
        warn(f"sample '{smp.name}': bad loop {ls - start}..{le - start} "
             f"(length {end - start}), using one-shot")
        looped = False
    if looped and le - start > MAX_SAMPLE_POINTS:
        warn(f"sample '{smp.name}': loop end {le - start} exceeds 16-bit LEA, "
             f"truncating to one-shot")
        looped = False

    if looped:
        data = _read_pcm(raw_16le, start, le)
        data.append(data[ls - start])
        lsa, lea = ls - start, le - start
    else:
        n = min(end - start, MAX_SAMPLE_POINTS)
        if n < end - start:
            warn(f"sample '{smp.name}': {end - start} points exceeds 16-bit LEA, truncated")
        data = _read_pcm(raw_16le, start, start + n)
        fade = min(len(data) // 4, max(8, (smp.sample_rate or SCSP_RATE) * 4 // 1000))
        for i in range(fade):
            k = len(data) - fade + i
            data[k] = int(data[k] * (fade - 1 - i) / fade)
        data.append(0)
        lsa, lea = 0, n

    return struct.pack(f'>{len(data)}h', *data), lsa, lea, looped


def compute_pitch(z: SF2Zone, smp: SF2Sample, rate_comp: bool) -> Tuple[int, int, bool]:
    """Return (base_note, fine_tune_byte, clamped).

    The driver plays note n of a layer at 44100 * 2^((n - base_note)/12 + ft_cents/1200).
    fine_tune is signed, 128 units = 50 cents (VGMTrans; matches commercial TONs).
    """
    root = z.root_key if z.root_key >= 0 else smp.original_key
    if root > 127:
        root = 60  # SF2 spec: 255 = unpitched/unknown
    tune_cents = z.coarse_tune * 100 + z.fine_tune + smp.pitch_correction
    eff = root - tune_cents / 100.0
    if rate_comp and smp.sample_rate > 0:
        eff += 12.0 * math.log2(SCSP_RATE / smp.sample_rate)
    base = int(round(eff))
    clamped = not (0 <= base <= 127)
    base = max(0, min(127, base))
    cents = (base - eff) * 100.0
    ft = max(-128, min(127, int(round(cents * 128.0 / 50.0))))
    return base, ft, clamped


def sf2_to_ton(sf2_path: str, base_addr: int = 0x30000,
               no_pitch_comp: bool = False, peg: bool = False,
               no_plfo: bool = False, bank: int = 0,
               atten_scale: float = 0.4, all_banks: bool = False,
               gm_banks: bool = False):
    """Convert SF2 to TON.

    Returns (ton_data, preset_info, voice_map, extra_banks):
      - default: one SF2 bank, TON voice index == MIDI program number
        (the driver does no remapping); voice_map is empty.
      - all_banks: every SF2 bank incl. drum kits merged into one TON bank of
        distinct voices; voice_map = [(sf2_bank, program, ton_voice, name)].
      - gm_banks: fixed GM layout, no voice map needed.  The file holds two
        TON banks back to back: tone bank 1 = 128 melodic programs, tone bank
        GM_DRUM_TONE_BANK = 128 drum kits.  extra_banks = [(bank, offset, size)]
        describes the second one for the MAP.  Use with `mid2seq -g`.
    """
    with open(sf2_path, 'rb') as f:
        sf2_data = f.read()

    chunks = read_sf2_chunks(sf2_data)
    samples = parse_sf2_samples(chunks['shdr'])
    presets = parse_sf2_presets(chunks)
    raw_pcm = chunks['smpl']  # 16-bit LE samples

    print(f"[sf2] {len(samples)} samples, {len(presets)} presets")

    warnings = []

    def warn(msg):
        if msg not in warnings:
            warnings.append(msg)

    class Pool:
        """PCM + PEG/PLFO tables of one TON bank (SA and table ids are per TON)."""
        def __init__(self):
            self.chunks = []
            self.size = 0
            self.cache = {}  # (sample_id, offsets, looped) -> (pcm_offset, lsa, lea, looped)
            self.peg_table = [bytes(10)]  # index 0 = no pitch envelope
            self.plfo_table = [bytes(4)]  # index 0 = no pitch LFO
            self.peg_cache, self.plfo_cache = {}, {}

        def add_pcm(self, pcm: bytes) -> int:
            off = self.size
            self.chunks.append(pcm)
            self.size += len(pcm)
            return off

    def table_id(table, cache, data):
        if data == bytes(len(data)):
            return 0
        if data not in cache:
            cache[data] = len(table)
            table.append(data)
        return cache[data]

    pool = Pool()  # the pool build_voice currently writes into

    def build_voice(name: str, zones: List[SF2Zone]) -> TonVoice:
        # TON layers have no velocity range: keep only zones that sound at a typical velocity
        vzones = [z for z in zones if z.vel_lo <= REPRESENTATIVE_VELOCITY <= z.vel_hi] or zones

        voice = TonVoice(bend_range=2)
        for z in vzones:
            if z.sample_id >= len(samples):
                continue
            smp = samples[z.sample_id]
            if smp.sample_type & 0x8000:
                warn(f"sample '{smp.name}' is a ROM sample, skipped")
                continue
            key = (z.sample_id, z.start_off, z.end_off, z.loop_start_off,
                   z.loop_end_off, z.sample_modes in (1, 3))
            if key not in pool.cache:
                conv = convert_sample(raw_pcm, smp, z, warn)
                if conv is None:
                    pool.cache[key] = None
                else:
                    pcm, lsa, lea, looped = conv
                    pool.cache[key] = (pool.add_pcm(pcm), lsa, lea, looped)
            if pool.cache[key] is None:
                continue
            pcm_off, lsa, lea, looped = pool.cache[key]

            base_note, ft, clamped = compute_pitch(z, smp, not no_pitch_comp)
            if clamped:
                warn(f"'{name}' sample '{smp.name}' ({smp.sample_rate} Hz): base note out of "
                     f"range, pitch will be wrong")

            peg_id = (table_id(pool.peg_table, pool.peg_cache, build_peg_entry(z))
                      if peg else 0)
            plfo_id = (0 if no_plfo else
                       table_id(pool.plfo_table, pool.plfo_cache, build_plfo_entry(z)))

            voice.layers.append(TonLayer(
                start_note=z.key_lo,
                end_note=z.key_hi,
                base_note=base_note,
                fine_tune=ft,
                sa=pcm_off,  # relative to PCM start; build_ton rebases it
                lsa=lsa,
                lea=lea,
                pcm8b=0,
                lpctl=1 if looped else 0,
                ar=ms_to_ar(timecents_to_ms(z.vol_attack)),
                d1r=ms_to_dr(timecents_to_ms(z.vol_decay)),
                d2r=0,
                dl=sustain_cb_to_dl(z.vol_sustain),
                rr=ms_to_dr(timecents_to_ms(z.vol_release)),
                tl=cb_to_tl(z.attenuation * atten_scale),
                disdl=7,
                dipan=sf2_pan_to_dipan(z.pan),
                peg_id=peg_id,
                plfo_id=plfo_id,
            ))
        return voice

    def finish(voices: List[TonVoice], label: str) -> bytes:
        all_pcm = b''.join(pool.chunks)
        data = build_ton(voices, all_pcm, peg_entries=pool.peg_table,
                         plfo_entries=pool.plfo_table)
        n_unique = len({id(v) for v in voices})
        print(f"[ton] {label}{len(data)} bytes ({len(voices)} voices, {n_unique} unique, "
              f"{len(all_pcm)} bytes PCM, {len(pool.peg_table)} PEG entries, "
              f"{len(pool.plfo_table)} PLFO entries)")
        return data

    voice_map = []    # (sf2_bank, program, voice_index, name) — only for all_banks
    extra_banks = []  # (map_tone_bank, offset_in_file, size) — only for gm_banks

    if gm_banks:
        voices, preset_info = _layout_gm_melodic(presets, build_voice, warn)
        melodic = finish(voices, "melodic (tone bank 1): ")
        pool = Pool()
        drums, drum_info = _layout_gm_drums(presets, build_voice, warn, pool.add_pcm)
        drum_data = finish(drums, f"drums (tone bank {GM_DRUM_TONE_BANK}): ")
        drum_off = (len(melodic) + 3) & ~3
        ton_data = melodic + bytes(drum_off - len(melodic)) + drum_data
        extra_banks.append((GM_DRUM_TONE_BANK, drum_off, len(drum_data)))
        preset_info += [(f"[drum] {n}", p) for n, p in drum_info]
    else:
        if all_banks:
            voices, preset_info, voice_map = _layout_all_banks(presets, build_voice, warn)
        else:
            voices, preset_info = _layout_one_bank(presets, bank, build_voice, warn)
        ton_data = finish(voices, "")

    for w in warnings:
        print(f"[warn] {w}")
    return ton_data, preset_info, voice_map, extra_banks


def _layout_one_bank(presets, bank, build_voice, warn):
    """Voice index == MIDI program number for one SF2 bank (plain mid2seq usage).
    Gaps in the preset list are filled with aliases of another voice."""
    voices_by_prog = {}
    names_by_prog = {}
    for name, prog, pbank, zones in sorted(presets, key=lambda p: (p[2], p[1])):
        if pbank != bank or prog > 127 or prog in voices_by_prog:
            continue
        voice = build_voice(name, zones)
        if voice.layers:
            voices_by_prog[prog] = voice
            names_by_prog[prog] = name
        else:
            warn(f"preset '{name}' (prog {prog}) has no usable zones")

    if not voices_by_prog:
        raise ValueError(f"No usable instruments found in SF2 bank {bank}")

    fallback = min(voices_by_prog)
    voices = []
    preset_info = []
    for prog in range(max(voices_by_prog) + 1):
        if prog in voices_by_prog:
            voices.append(voices_by_prog[prog])
            preset_info.append((names_by_prog[prog], prog))
            print(f"  Voice {prog:3d}: '{names_by_prog[prog]}' — "
                  f"{len(voices_by_prog[prog].layers)} layers")
        else:
            voices.append(voices_by_prog[fallback])
            preset_info.append((f"(missing, alias of {fallback})", prog))
            print(f"  Voice {prog:3d}: (no preset) -> alias of voice {fallback}")
    return voices, preset_info


def _pick_presets(candidates, build_voice, warn):
    """candidates: {prog: [(name, bank, zones), ...]} in preference order.
    Builds the first usable preset per program -> {prog: (name, bank, voice)}."""
    chosen = {}
    for prog in sorted(candidates):
        for name, bank, zones in candidates[prog]:
            voice = build_voice(name, zones)
            if voice.layers:
                chosen[prog] = (name, bank, voice)
                break
            warn(f"preset '{name}' (bank {bank} prog {prog}) has no usable zones")
    return chosen


def _fill_128(chosen, fallback_for, label):
    """Expand {prog: (name, bank, voice)} to exactly 128 voices so any GM SEQ can
    address any program safely; missing programs alias fallback_for(prog)."""
    voices, info = [], []
    for prog in range(128):
        if prog in chosen:
            name, bank, voice = chosen[prog]
            voices.append(voice)
            info.append((name if bank in (0, 128) else f"{name} (from bank {bank})", prog))
        else:
            src = fallback_for(prog)
            voices.append(chosen[src][2])
            info.append((f"(missing, alias of {label} {src})", prog))
    return voices, info


def _layout_gm_melodic(presets, build_voice, warn):
    """Tone bank 1 of the GM layout: voice = GM program.  Bank 0 presets first;
    a program missing from bank 0 is taken from another melodic bank, then from
    the same GM instrument family (8 programs), then the lowest program."""
    cands = {}
    for name, prog, bank, zones in sorted(presets, key=lambda p: (p[2] != 0, p[2], p[1])):
        if bank < 128 and prog <= 127:
            cands.setdefault(prog, []).append((name, bank, zones))
    chosen = _pick_presets(cands, build_voice, warn)
    if not chosen:
        raise ValueError("No usable melodic presets found in SF2")

    def fallback(prog):
        family = [p for p in range(prog & ~7, (prog & ~7) + 8) if p in chosen]
        if family:
            return min(family, key=lambda p: abs(p - prog))
        return min(chosen)

    voices, info = _fill_128(chosen, fallback, "program")
    n_missing = sum(1 for p in range(128) if p not in chosen)
    print(f"  melodic: {len(chosen)} programs"
          + (f", {n_missing} missing (aliased)" if n_missing else ""))
    return voices, info


def _layout_gm_drums(presets, build_voice, warn, add_pcm):
    """Drum tone bank of the GM layout: voice = kit program (GS numbering:
    0 Standard, 8 Room, 16 Power, 24 Electronic, 25 TR-808, 32 Jazz, 40 Brush,
    48 Orchestra, 56 SFX).  Missing kits alias the standard kit.  A SoundFont
    without kits gets a silent placeholder so the bank still exists."""
    cands = {}
    for name, prog, bank, zones in sorted(presets, key=lambda p: (p[2] != 128, p[2], p[1])):
        if bank >= 128 and prog <= 127:
            cands.setdefault(prog, []).append((name, bank, zones))
    chosen = _pick_presets(cands, build_voice, warn)
    if not chosen:
        warn("SF2 has no drum kits (bank 128); drum tone bank is silent")
        silent = TonVoice(layers=[TonLayer(sa=add_pcm(bytes(8)), lsa=0, lea=2,
                                           tl=255, disdl=0, rr=31)])
        chosen = {0: ("(silent)", 128, silent)}
    std = 0 if 0 in chosen else min(chosen)
    voices, info = _fill_128(chosen, lambda prog: std, "kit")
    print(f"  drums: kits {sorted(chosen)}")
    return voices, info


def _voice_key(v: TonVoice) -> bytes:
    """Byte identity of a voice (layer SAs are still PCM-relative here)."""
    return v.pack()


def _layout_all_banks(presets, build_voice, warn, max_voices: int = MAX_TON_VOICES):
    """Put every SF2 bank (melodic variations + drum kits) into one TON bank.

    Identical voices are stored once, so the TON holds only the distinct
    instruments.  Priority when space runs out: bank 0, then drum kits
    (bank >= 128), then the other variation banks.  The returned voice map
    (sf2 bank, program -> TON voice) is what `mid2seq -m` uses to rewrite
    program changes / channel 10 drums.
    """
    def priority(p):
        name, prog, bank, _ = p
        return (0 if bank == 0 else 1 if bank >= 128 else 2, bank, prog)

    voices = []           # unique voices, index = TON voice number
    by_key = {}           # voice bytes -> index
    voice_map = []
    preset_info = []
    seen = set()
    dropped = []
    for name, prog, bank, zones in sorted(presets, key=priority):
        if prog > 127 or (bank, prog) in seen:
            continue
        seen.add((bank, prog))
        voice = build_voice(name, zones)
        if not voice.layers:
            warn(f"preset '{name}' (bank {bank} prog {prog}) has no usable zones")
            continue
        key = _voice_key(voice)
        if key not in by_key:
            if len(voices) >= max_voices:
                dropped.append(f"{bank}:{prog} '{name}'")
                continue
            by_key[key] = len(voices)
            voices.append(voice)
            preset_info.append((name, len(voices) - 1))
        idx = by_key[key]
        voice_map.append((bank, prog, idx, name))

    if not voices:
        raise ValueError("No usable instruments found in SF2")
    if dropped:
        warn(f"{len(dropped)} presets didn't fit in {max_voices} TON voices and will fall back "
             f"to bank 0 / the default kit: " + ", ".join(dropped[:10]) +
             (" ..." if len(dropped) > 10 else ""))

    banks = sorted({b for b, _, _, _ in voice_map})
    print(f"  {len(voice_map)} presets from banks {banks} -> {len(voices)} distinct TON voices")
    return voices, preset_info, voice_map


def write_voice_map(path: str, voice_map) -> None:
    """Write the text voice map consumed by `mid2seq -m`."""
    with open(path, 'w', encoding='ascii', errors='replace', newline='\n') as f:
        f.write("# sf2ton voice map: <sf2_bank> <program> <ton_voice> <preset name>\n")
        f.write("# Bank 128 = drum kits (MIDI channel 10). Used by: mid2seq -m <this file>\n")
        for bank, prog, idx, name in sorted(voice_map):
            f.write(f"{bank} {prog} {idx} {name}\n")

def ton_extent(data: bytes, base: int = 0) -> Optional[int]:
    """End offset (relative to base) of the TON starting at `base`: the last
    byte any layer can play, including the guard sample after LEA.  None if
    the header doesn't look like a TON."""
    if base + 10 > len(data):
        return None
    mixer, vl, peg, plfo, v0 = struct.unpack_from('>5H', data, base)
    if not (8 < mixer <= v0 and mixer <= vl <= peg <= plfo <= v0) or (mixer - 8) % 2:
        return None
    end = v0
    for i in range((mixer - 8) // 2):
        vo = struct.unpack_from('>H', data, base + 8 + 2 * i)[0]
        if base + vo + 4 > len(data):
            return None
        n = struct.unpack_from('b', data, base + vo + 2)[0] + 1
        end = max(end, vo + 4 + 0x20 * n)
        for li in range(max(0, n)):
            lo = base + vo + 4 + 0x20 * li
            if lo + 0x20 > len(data):
                return None
            l = data[lo:lo + 0x20]
            sa = ((l[3] & 0xF) << 16) | struct.unpack_from('>H', l, 4)[0]
            lea = struct.unpack_from('>H', l, 8)[0]
            bps = 1 if (l[3] >> 4) & 1 else 2
            end = max(end, sa + bps * (lea + 1))
    return end


def find_second_ton(data: bytes) -> Optional[int]:
    """Offset of the drum TON in a --gm-banks file (4-byte aligned after bank 1)."""
    end = ton_extent(data)
    if end is None:
        return None
    off = (end + 3) & ~3
    return off if off < len(data) and ton_extent(data, off) is not None else None


# ── CLI ─────────────────────────────────────────────────────────────

def parse_size_str(val_str: str) -> int:
    """Parse size string with optional k/m suffixes, hex, or auto/max."""
    val = val_str.strip().lower()
    if val in ('auto', 'max'):
        return -1  # sentinel for auto/max
    if val.startswith('0x'):
        return int(val, 16)
    multiplier = 1
    if val.endswith('kb'):
        multiplier = 1024
        val = val[:-2]
    elif val.endswith('k'):
        multiplier = 1024
        val = val[:-1]
    elif val.endswith('mb'):
        multiplier = 1024 * 1024
        val = val[:-2]
    elif val.endswith('m'):
        multiplier = 1024 * 1024
        val = val[:-1]
    elif val.endswith('b'):
        val = val[:-1]
    return int(val) * multiplier


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Convert SoundFont (.sf2) to Saturn .TON + .MAP')
    parser.add_argument('input', help='Input .sf2 or .ton file')
    parser.add_argument('-o', '--output', help='Output .ton file (when converting .sf2)')
    parser.add_argument('--map', help='Output .map file')
    parser.add_argument('--seq', help='SEQ file (to compute MAP addresses or size)')
    parser.add_argument('--seq-size', type=str, default=None,
                        help="Reserved size for SEQ in sound RAM (e.g. 64k, 128k, 0x10000, or 'max'/'auto'). "
                             "Defaults to SEQ file size if --seq given, all remaining free sound RAM in ton-first, "
                             "or 64KB (clamped) in seq-first.")
    parser.add_argument('--layout', choices=['seq-first', 'ton-first'], default='seq-first',
                        help="Memory layout order in Sound RAM: 'seq-first' (default, SEQ at base_addr) "
                             "or 'ton-first' (TON at base_addr, SEQ immediately after). "
                             "'ton-first' is recommended for large SEQ files or when swapping songs at runtime.")
    parser.add_argument('--ton-first', action='store_true',
                        help="Shorthand for --layout ton-first.")
    parser.add_argument('--base-addr', type=lambda x: int(x, 0), default=0x02CFDC,
                        help='Base address for bank 1 data in sound RAM (default: 0x02CFDC, after SGL bank 0)')
    parser.add_argument('--no-pitch-comp', action='store_true',
                        help='Disable sample rate pitch compensation (folded into base note/fine tune)')
    parser.add_argument('--peg', action='store_true',
                        help='Experimental: generate pitch envelopes from the SF2 mod envelope. '
                             'The PEG table units are not fully reverse-engineered, so this is off by default')
    parser.add_argument('--no-peg', action='store_true', help=argparse.SUPPRESS)  # old flag, now default
    parser.add_argument('--no-plfo', action='store_true',
                        help='Disable pitch LFO generation from SF2 vibrato LFO')
    parser.add_argument('--atten-scale', type=float, default=0.4,
                        help='Multiplier for SF2 initialAttenuation (default 0.4, the EMU/FluidSynth '
                             'convention most SoundFonts are authored against; 1.0 = strict spec)')
    parser.add_argument('--sf2-bank', type=int, default=0,
                        help='SF2 preset bank to convert (default 0; 128 = GM drum kits)')
    parser.add_argument('--all-banks', action='store_true',
                        help='Merge every SF2 bank (variations + drum kits) into one TON bank of '
                             'distinct voices and write a voice map for "mid2seq -m"')
    parser.add_argument('--voice-map',
                        help='Output voice map path for --all-banks (default: <output>.vmap)')
    parser.add_argument('--gm-banks', action='store_true',
                        help='Fixed GM layout, no voice map: tone bank 1 = 128 melodic programs, '
                             f'tone bank {GM_DRUM_TONE_BANK} = 128 drum kits, stored back to back in '
                             'one .ton file with a MAP entry for each. Any SEQ made with "mid2seq -g" '
                             'plays with any TON made this way. With a .ton input, re-detects the '
                             'drum bank for the MAP')
    args = parser.parse_args()
    if args.gm_banks and args.all_banks:
        parser.error('--gm-banks and --all-banks are mutually exclusive')

    voice_map = []
    extra_banks = []
    is_ton_input = args.input.lower().endswith('.ton')
    if is_ton_input:
        with open(args.input, 'rb') as f:
            ton_data = f.read()
        preset_info = []
        print(f"[ton] Loaded existing TON: {args.input} ({len(ton_data)} bytes)")
        if args.gm_banks:
            drum_off = find_second_ton(ton_data)
            if drum_off is None:
                parser.error(f'{args.input} has no second (drum) tone bank; '
                             f'was it made with --gm-banks?')
            extra_banks.append((GM_DRUM_TONE_BANK, drum_off, len(ton_data) - drum_off))
            print(f"[ton] Drum tone bank found at file offset 0x{drum_off:X}")
    else:
        if not args.output:
            args.output = os.path.splitext(args.input)[0] + '.ton'

        ton_data, preset_info, voice_map, extra_banks = sf2_to_ton(
            args.input, args.base_addr,
            no_pitch_comp=args.no_pitch_comp,
            peg=args.peg and not args.no_peg,
            no_plfo=args.no_plfo, bank=args.sf2_bank,
            atten_scale=args.atten_scale, all_banks=args.all_banks,
            gm_banks=args.gm_banks)

        with open(args.output, 'wb') as f:
            f.write(ton_data)
        print(f"[out] {args.output} ({len(ton_data)} bytes)")

        if args.all_banks:
            vmap_path = args.voice_map or os.path.splitext(args.output)[0] + '.vmap'
            write_voice_map(vmap_path, voice_map)
            print(f"[out] {vmap_path} ({len(voice_map)} entries) — convert MIDIs with: "
                  f"mid2seq -m {vmap_path} song.mid song.seq")
        if args.gm_banks:
            print("[gm] Convert MIDIs with: mid2seq -g song.mid song.seq")

    layout = 'ton-first' if args.ton_first else args.layout
    ton_size = len(ton_data)
    SOUND_RAM_SIZE = 512 * 1024  # 524288 bytes

    requested_seq_size = None
    if args.seq_size is not None:
        try:
            requested_seq_size = parse_size_str(args.seq_size)
        except Exception as e:
            parser.error(f"Invalid --seq-size format: {args.seq_size} ({e})")

    actual_seq_file_size = 0
    if args.seq:
        if not os.path.isfile(args.seq):
            raise FileNotFoundError(f"SEQ file not found: {args.seq}")
        actual_seq_file_size = os.path.getsize(args.seq)
        print(f"[seq] {args.seq}: {actual_seq_file_size} bytes")

    if layout == 'ton-first':
        ton_addr = args.base_addr
        raw_seq_addr = ton_addr + ton_size
        seq_addr = (raw_seq_addr + 3) & ~3  # align to 4-byte boundary
        avail_for_seq = max(0, SOUND_RAM_SIZE - seq_addr)

        if requested_seq_size == -1:  # 'auto' or 'max'
            seq_size = avail_for_seq
        elif requested_seq_size is not None:
            seq_size = requested_seq_size
            if actual_seq_file_size > 0 and actual_seq_file_size > seq_size:
                seq_size = actual_seq_file_size
        elif actual_seq_file_size > 0:
            seq_size = actual_seq_file_size
        else:
            # Default for ton-first: allocate all remaining sound RAM to SEQ
            seq_size = avail_for_seq
            print(f"[map] (ton-first) No --seq or --seq-size specified; allocated all remaining Sound RAM ({seq_size} bytes / {seq_size/1024:.1f} KB) to SEQ")
    else:  # seq-first
        seq_addr = args.base_addr
        # Maximum ton_addr that allows TON to fit within 512KB Sound RAM, aligned to 4 bytes
        max_ton_addr = (SOUND_RAM_SIZE - ton_size) & ~3
        avail_for_seq = max(0, max_ton_addr - seq_addr)

        if requested_seq_size == -1:  # 'auto' or 'max'
            seq_size = avail_for_seq
        elif requested_seq_size is not None:
            seq_size = requested_seq_size
            if actual_seq_file_size > 0 and actual_seq_file_size > seq_size:
                seq_size = actual_seq_file_size
        elif actual_seq_file_size > 0:
            seq_size = actual_seq_file_size
        else:
            # Default for seq-first: reserve 64 KB (or remaining RAM if smaller) instead of 0
            default_buf = 64 * 1024
            seq_size = min(default_buf, avail_for_seq)
            print(f"[map] (seq-first) No --seq or --seq-size specified; reserved {seq_size} bytes ({seq_size/1024:.1f} KB) for SEQ")

        raw_ton_addr = seq_addr + seq_size
        ton_addr = (raw_ton_addr + 3) & ~3  # align to 4-byte boundary

    map_output = args.map if args.map else os.path.splitext(args.output if not is_ton_input else args.input)[0] + '.map'
    # In GM mode the file holds several tone banks: bank 1 is the first part only.
    bank1_size = min((off for _, off, _ in extra_banks), default=ton_size)
    extra_tones = [(b, ton_addr + off, size) for b, off, size in extra_banks]
    map_data = build_map(ton_addr, bank1_size, seq_addr, seq_size, bank=1,
                         extra_tones=extra_tones)
    with open(map_output, 'wb') as f:
        f.write(map_data)
    print(f"[out] {map_output} ({len(map_data)} bytes)")

    end_addr = max(ton_addr + ton_size, seq_addr + seq_size)
    ram_pct = (end_addr / SOUND_RAM_SIZE) * 100
    free_ram = SOUND_RAM_SIZE - end_addr

    def print_tones():
        print(f"  Bank 1 TONE     : 0x{ton_addr:06X}..0x{ton_addr + bank1_size:06X} ({bank1_size} bytes / {bank1_size/1024:.1f} KB)")
        for b, addr, size in extra_tones:
            print(f"  Bank {b} TONE     : 0x{addr:06X}..0x{addr + size:06X} ({size} bytes / {size/1024:.1f} KB)"
                  f"  [same file, offset 0x{addr - ton_addr:X}]")

    print(f"\n[ram] SCSP Sound RAM layout (512 KB total, limit 0x080000):")
    print(f"  Bank 0 Defaults : 0x000000..0x{args.base_addr:06X} ({args.base_addr} bytes / {args.base_addr/1024:.1f} KB)")
    if layout == 'ton-first':
        print_tones()
        print(f"  Bank 1 SEQ      : 0x{seq_addr:06X}..0x{seq_addr + seq_size:06X} ({seq_size} bytes / {seq_size/1024:.1f} KB)")
    else:
        print(f"  Bank 1 SEQ      : 0x{seq_addr:06X}..0x{seq_addr + seq_size:06X} ({seq_size} bytes / {seq_size/1024:.1f} KB)")
        print_tones()
    print(f"  End of used RAM : 0x{end_addr:06X} ({end_addr} bytes, {ram_pct:.1f}% of 512KB)")

    if free_ram >= 0:
        print(f"  Free Sound RAM  : {free_ram} bytes ({free_ram/1024:.1f} KB)")
    else:
        print(f"  [WARNING] Sound RAM overflow by {-free_ram} bytes! TON + SEQ exceeds 512KB.")

    if actual_seq_file_size > 0 and actual_seq_file_size > seq_size:
        print(f"  [WARNING] Provided SEQ file ({actual_seq_file_size} B) exceeds MAP entry size ({seq_size} B)!")

    # Print voice mapping for reference
    if voice_map:
        print("\nVoice mapping (SF2 bank:program -> TON voice):")
        for bank, prog, idx, name in sorted(voice_map):
            print(f"  {bank:3d}:{prog:3d} -> Voice {idx:3d}: {name}")
    elif preset_info:
        print("\nVoice mapping (MIDI program -> TON voice):")
        for i, (name, prog) in enumerate(preset_info):
            print(f"  Program {prog:3d} -> Voice {prog if args.gm_banks else i}: {name}")


if __name__ == '__main__':
    main()
