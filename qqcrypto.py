"""QQ Music QRC (word-by-word lyrics) decryption.

QRC blobs are hex-encoded, decrypted with QQ Music's "buggy DES" in a 3DES-EDE
arrangement (per 8-byte ECB block, fixed 24-byte key), then zlib-inflated to
enhanced-LRC-style text with per-word timings.

This is a clean pure-Python port of the algorithm, so it runs on the Pi's ARM
Linux (the usual reference ships a Windows-only QQMusicCommon.dll). The DES is
QQ's *buggy* variant — a handful of S-box entries differ from FIPS-46 DES, which
is exactly why a stock DES library won't decrypt QRC. Algorithm + constants from
the MIT-licensed C# reference WXRIW/QQMusicDecoder
(https://github.com/WXRIW/QQMusicDecoder), itself derived from the public DES
standard.
"""
from functools import lru_cache
from zlib import decompress

_ENCRYPT = 1
_DECRYPT = 0

# QQ's "buggy" DES S-boxes: standard FIPS-46 tables EXCEPT for a few flipped
# entries (e.g. sbox2 has 15 where DES has 14; sbox4 has a doubled 10). Those
# bugs are load-bearing — they define QQ's cipher, so they must be reproduced
# verbatim.
_SBOX = (
    (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
     0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
     15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
     3, 13, 4, 7, 15, 2, 8, 15, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
     13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
     13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
     1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
     13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
     3, 15, 0, 6, 10, 10, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
     14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
     11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
     10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
     4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
     13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
     6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
     1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
     2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
)


def _bit_of_bytes(data, index, shift):
    """Bit `index` of an 8-byte block (DES bit numbering), moved to `shift`."""
    return ((data[(index // 32) * 4 + 3 - (index % 32) // 8] >> (7 - index % 8)) & 1) << shift


def _bit_r(value, index, shift):
    return ((value >> (31 - index)) & 1) << shift


def _bit_l(value, index, shift):
    return ((value << index) & 0x80000000) >> shift


def _sbox_index(a):
    return (a & 32) | ((a & 31) >> 1) | ((a & 1) << 4)


def _initial_permutation(block):
    left = 0
    right = 0
    # DES IP split into the two 32-bit halves.
    ip_left = (57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
               61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7)
    ip_right = (56, 48, 40, 32, 24, 16, 8, 0, 58, 50, 42, 34, 26, 18, 10, 2,
                60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6)
    for i, src in enumerate(ip_left):
        left |= _bit_of_bytes(block, src, 31 - i)
    for i, src in enumerate(ip_right):
        right |= _bit_of_bytes(block, src, 31 - i)
    return left, right


def _inverse_permutation(s0, s1):
    out = bytearray(8)
    # DES IP^-1, byte-by-byte (matching the reference's output byte order).
    order = (3, 2, 1, 0, 7, 6, 5, 4)
    for k, byte_idx in enumerate(order):
        base = 7 - k
        out[byte_idx] = (
            _bit_r(s1, base, 7) | _bit_r(s0, base, 6) |
            _bit_r(s1, base + 8, 5) | _bit_r(s0, base + 8, 4) |
            _bit_r(s1, base + 16, 3) | _bit_r(s0, base + 16, 2) |
            _bit_r(s1, base + 24, 1) | _bit_r(s0, base + 24, 0))
    return out


def _feistel(state, key):
    t1 = (_bit_l(state, 31, 0) | ((state & 0xf0000000) >> 1) | _bit_l(state, 4, 5) |
          _bit_l(state, 3, 6) | ((state & 0x0f000000) >> 3) | _bit_l(state, 8, 11) |
          _bit_l(state, 7, 12) | ((state & 0x00f00000) >> 5) | _bit_l(state, 12, 17) |
          _bit_l(state, 11, 18) | ((state & 0x000f0000) >> 7) | _bit_l(state, 16, 23))
    t2 = (_bit_l(state, 15, 0) | ((state & 0x0000f000) << 15) | _bit_l(state, 20, 5) |
          _bit_l(state, 19, 6) | ((state & 0x00000f00) << 13) | _bit_l(state, 24, 11) |
          _bit_l(state, 23, 12) | ((state & 0x000000f0) << 11) | _bit_l(state, 28, 17) |
          _bit_l(state, 27, 18) | ((state & 0x0000000f) << 9) | _bit_l(state, 0, 23))
    block = ((t1 >> 24) & 0xff, (t1 >> 16) & 0xff, (t1 >> 8) & 0xff,
             (t2 >> 24) & 0xff, (t2 >> 16) & 0xff, (t2 >> 8) & 0xff)
    block = [block[i] ^ key[i] for i in range(6)]
    state = ((_SBOX[0][_sbox_index(block[0] >> 2)] << 28) |
             (_SBOX[1][_sbox_index(((block[0] & 0x03) << 4) | (block[1] >> 4))] << 24) |
             (_SBOX[2][_sbox_index(((block[1] & 0x0f) << 2) | (block[2] >> 6))] << 20) |
             (_SBOX[3][_sbox_index(block[2] & 0x3f)] << 16) |
             (_SBOX[4][_sbox_index(block[3] >> 2)] << 12) |
             (_SBOX[5][_sbox_index(((block[3] & 0x03) << 4) | (block[4] >> 4))] << 8) |
             (_SBOX[6][_sbox_index(((block[4] & 0x0f) << 2) | (block[5] >> 6))] << 4) |
             _SBOX[7][_sbox_index(block[5] & 0x3f)])
    p = (15, 6, 19, 20, 28, 11, 27, 16, 0, 14, 22, 25, 4, 17, 30, 9,
         1, 7, 23, 13, 31, 26, 2, 8, 18, 12, 29, 5, 21, 10, 3, 24)
    result = 0
    for i, src in enumerate(p):
        result |= _bit_l(state, src, i)
    return result


def _crypt_block(block, schedule):
    s0, s1 = _initial_permutation(block)
    for rnd in range(15):
        s0, s1 = s1, _feistel(s1, schedule[rnd]) ^ s0
    s0 = _feistel(s1, schedule[15]) ^ s0
    return _inverse_permutation(s0, s1)


def _key_schedule(key, mode):
    schedule = [[0] * 6 for _ in range(16)]
    shifts = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
    perm_c = (56, 48, 40, 32, 24, 16, 8, 0, 57, 49, 41, 33, 25, 17, 9, 1,
              58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35)
    perm_d = (62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5,
              60, 52, 44, 36, 28, 20, 12, 4, 27, 19, 11, 3)
    compression = (13, 16, 10, 23, 0, 4, 2, 27, 14, 5, 20, 9, 22, 18, 11, 3,
                   25, 7, 15, 6, 26, 19, 12, 1, 40, 51, 30, 36, 46, 54, 29, 39,
                   50, 44, 32, 47, 43, 48, 38, 55, 33, 52, 45, 41, 49, 35, 28, 31)
    c = sum(_bit_of_bytes(key, perm_c[i], 31 - i) for i in range(28))
    d = sum(_bit_of_bytes(key, perm_d[i], 31 - i) for i in range(28))
    for i in range(16):
        # NB the & 0xfffffff0 mask (only the top 28 bits matter) is QQ's variant.
        c = ((c << shifts[i]) | (c >> (28 - shifts[i]))) & 0xfffffff0
        d = ((d << shifts[i]) | (d >> (28 - shifts[i]))) & 0xfffffff0
        target = 15 - i if mode == _DECRYPT else i
        for j in range(24):
            schedule[target][j // 8] |= _bit_r(c, compression[j], 7 - (j % 8))
        for j in range(24, 48):
            schedule[target][j // 8] |= _bit_r(d, compression[j] - 27, 7 - (j % 8))
    return schedule


@lru_cache(maxsize=8)
def _triple_setup(key, mode):
    if mode == _ENCRYPT:
        return (_key_schedule(key[0:], _ENCRYPT),
                _key_schedule(key[8:], _DECRYPT),
                _key_schedule(key[16:], _ENCRYPT))
    return (_key_schedule(key[16:], _DECRYPT),
            _key_schedule(key[8:], _ENCRYPT),
            _key_schedule(key[0:], _DECRYPT))


def _triple_crypt(block, schedules):
    for sched in schedules:
        block = _crypt_block(block, sched)
    return block


# Fixed 24-byte key QQ uses for QRC (3DES-EDE, applied in DECRYPT mode).
QRC_KEY = b"!@#)(*$%123ZXC!@!@#)(NHL"


def qrc_decrypt(encrypted) -> str | None:
    """Decrypt a QRC blob (hex string, or raw bytes) to its plain enhanced-LRC
    text, or None on any failure (bad hex / not QRC / inflate error)."""
    try:
        raw = bytearray.fromhex(encrypted) if isinstance(encrypted, str) \
            else bytearray(encrypted)
    except (ValueError, TypeError):
        return None
    if not raw or len(raw) % 8 != 0:
        return None
    schedules = _triple_setup(QRC_KEY, _DECRYPT)
    out = bytearray()
    for i in range(0, len(raw), 8):
        out += _triple_crypt(raw[i:i + 8], schedules)
    try:
        return decompress(bytes(out)).decode("utf-8", "replace")
    except Exception:
        return None
