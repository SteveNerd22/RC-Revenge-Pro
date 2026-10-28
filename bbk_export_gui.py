# bbk_export_gui.py - versione GUI di bbk_export_full_8.py
# Legge i file BB/x/y.BBK direttamente dall'ISO (in memoria, nessun file temporaneo)
# e esporta le piste in una cartella a scelta.
import os, sys, struct, zlib, collections, threading, queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

BASE = 7680        # 0x1E0000 / 256: dove BBLoad carica il blocco VRAM nella memoria grafica
FLIP_V = True      # la PS2 misura V dall'alto, Blender dal basso

WORLDS = [
    "Horror world",
    "Space world",
    "Jungle world",
    "Cartoon world",
    "Creatures world",
    "Pirate world"
]

# Nomi dei tipi di pickup
WN = {0: "RANDOM", 1: "ROCKETS", 2: "BATTERY", 3: "FAKE", 4: "SHOCKWAVE", 5: "WATER", 6: "OIL",
      7: "LIGHTNING", 8: "HEATRAY", 9: "MEGA", 10: "INVINCIBLE", 11: "MINE", 12: "SARGE", 13: "SKULL",
      14: "YELLA", 15: "CONCEPT", 16: "ARMOURCAR", 17: "DUMPER", 18: "MOONROVER", 19: "ROCKETCAR"}

# Materiali di colore per collisioni, IA, start, pickup e oggetti
COL = {
    "col_red": (0.9, 0.1, 0.1),     # Tutte le collisioni standard (ma0, ma1, ma2)
    "col_gid2": (0.1, 0.3, 0.9),    # Acqua (gid2) - Blu
    "ia": (0.1, 0.8, 0.2),
    "ia_centro": (0.4, 1.0, 0.4),
    "start": (1.0, 0.9, 0.1),
    "pickup": (0.1, 0.3, 1.0),
    "pickup_fisso": (0.85, 0.15, 0.85),
    "oggetto": (1.0, 0.5, 0.0)
}

# Altezza della zona di un nodo: l'auto e' "dentro" se |centre.y - auto.y| < ZONE_H (AINodeUpdateRecursive)
ZONE_H = 400

# Colore di ogni priority dei nodi (materiali nodo_pNN)
PRIO_COL = {0: (0.1, 0.8, 0.2), 1: (1.0, 0.9, 0.1), 2: (0.6, 0.6, 0.6), 3: (0.1, 0.9, 0.9),
            4: (0.2, 0.4, 1.0), 5: (1.0, 0.1, 0.1), 6: (1.0, 0.75, 0.4), 7: (1.0, 0.55, 0.2),
            8: (1.0, 0.35, 0.1), 9: (0.6, 0.1, 0.0), 10: (1.0, 0.2, 1.0)}
PRIO_ALTRO = (0.8, 0.8, 0.8)

# Significato delle priority (letto dal codice: AINodeChooseNodeRoute, AIControl, AIUpdateObjectNode)
PRIO_INFO = {
    0: "normale",
    1: "ramo con armi: le AI che hanno gia' un'arma lo evitano (ipotesi: ramo con pickup)",
    2: "non usata nel codice visto (su pista 1: 10 nodi di fila in una curva a U, scopo da capire)",
    3: "ramo di recupero: lo prendono solo le AI nella meta' bassa della classifica",
    4: "ramo di recupero/scorciatoia: dipende da classifica e coppa (coppe alte: lo prendono tutte)",
    5: "ramo vietato alle AI (le AI lo evitano sempre)",
    6: "limita la manetta delle AI a 0x60 (circa 75%)",
    7: "limita la manetta delle AI a 0x40 (circa 50%)",
    8: "limita la manetta delle AI a 0x20 (circa 25%)",
    9: "limita la manetta delle AI a 0x10 (circa 12%)",
    10: "checkpoint di mezzo giro (accende iHalfLapDone)",
}


# ---------------- lettore minimale di ISO9660 ----------------
class Iso:
    """Legge file da un'immagine ISO9660 (settori da 2048 byte, oppure raw 2352)."""

    def __init__(self, path):
        self.f = open(path, "rb")
        for ss, off in ((2048, 0), (2352, 16), (2352, 24)):
            self.ss, self.off = ss, off
            self.f.seek(16 * ss + off)
            pvd = self.f.read(2048)
            if len(pvd) >= 190 and pvd[0] == 1 and pvd[1:6] == b"CD001":
                break
        else:
            self.f.close()
            raise ValueError("Il file non sembra un'immagine ISO9660 valida.")
        self.root = (struct.unpack_from("<I", pvd, 156 + 2)[0], struct.unpack_from("<I", pvd, 156 + 10)[0])

    def close(self):
        self.f.close()

    def _read(self, lba, size):
        if self.ss == 2048:
            self.f.seek(lba * 2048)
            return self.f.read(size)
        out = bytearray()
        n = (size + 2047) // 2048
        for k in range(n):
            self.f.seek((lba + k) * self.ss + self.off)
            out += self.f.read(2048)
        return bytes(out[:size])

    def _list(self, lba, size):
        data = self._read(lba, size)
        i, res = 0, {}
        while i < len(data):
            ln = data[i]
            if ln == 0:
                i = (i // 2048 + 1) * 2048
                continue
            extent = struct.unpack_from("<I", data, i + 2)[0]
            fsize = struct.unpack_from("<I", data, i + 10)[0]
            flags = data[i + 25]
            nlen = data[i + 32]
            raw = data[i + 33:i + 33 + nlen]
            i += ln
            if raw in (b"\x00", b"\x01"):
                continue
            name = raw.decode("ascii", "replace").split(";")[0].upper()
            res[name] = (extent, fsize, bool(flags & 2))
        return res

    def read_file(self, path):
        """path tipo 'BB/1/2.BBK'. Ritorna i byte, oppure None se non esiste."""
        parts = [p.upper() for p in path.split("/") if p]
        lba, size = self.root
        for n, part in enumerate(parts):
            ent = self._list(lba, size).get(part)
            if ent is None:
                return None
            lba, size, is_dir = ent
            if n < len(parts) - 1 and not is_dir:
                return None
        return self._read(lba, size)

    def has_dir(self, name):
        ent = self._list(*self.root).get(name.upper())
        return bool(ent and ent[2])


# ---------------- ordine dei pixel nella memoria grafica della PS2 ----------------
BT32 = [[0, 1, 4, 5, 16, 17, 20, 21], [2, 3, 6, 7, 18, 19, 22, 23],
        [8, 9, 12, 13, 24, 25, 28, 29], [10, 11, 14, 15, 26, 27, 30, 31]]
BT8 = BT32
BT4 = [[0, 2, 8, 10], [1, 3, 9, 11], [4, 6, 12, 14], [5, 7, 13, 15],
       [16, 18, 24, 26], [17, 19, 25, 27], [20, 22, 28, 30], [21, 23, 29, 31]]

def w32(x, y):
    return BT32[(y >> 3) & 3][(x >> 3) & 7] * 64 + (x & 1) + 2 * (y & 1) + 4 * ((x >> 1) & 3) + 16 * ((y >> 1) & 3)

def col8(x, y):
    c, r = y >> 2, y & 3
    v = 2 * (x >> 3) + 4 * (x & 1) + 16 * ((x >> 1) & 3) + 8 * (r & 1) + (r >> 1)
    if r >> 1:
        v ^= 32
    if c & 1:
        v = (v + 64) ^ 32
    return v + 128 * (c >> 1)

def col4(x, y):
    c, r = y >> 2, y & 3
    v = 8 * (x & 1) + 32 * ((x >> 1) & 3) + 2 * (x >> 3) + 16 * (r & 1) + (r >> 1)
    if r >> 1:
        v ^= 64
    if c & 1:
        v = (v + 128) ^ 64
    return v + 256 * (c >> 1)

def png_save(path, w, h, rows, use_alpha):
    processed_rows = []
    for r in rows:
        if not use_alpha:
            row_bytes = bytearray()
            for i in range(0, len(r), 4):
                row_bytes.extend(r[i:i+3])
            processed_rows.append(bytes(row_bytes))
        else:
            processed_rows.append(r)

    raw = b"".join(b"\x00" + pr for pr in processed_rows)
    color_type = 6 if use_alpha else 2

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

def build_gs(vram):
    gs = bytearray(len(vram))
    for c in range(len(vram) // 0x4000):
        src = vram[c * 0x4000:(c + 1) * 0x4000]
        dst = c * 0x4000
        for y in range(64):
            for x in range(64):
                d = dst + ((y >> 5) * 2048 + w32(x, y & 31)) * 4
                s = (y * 64 + x) * 4
                gs[d:d + 4] = src[s:s + 4]
    return gs

def rd(gs, off, n):
    return gs[off:off + n] if 0 <= off <= len(gs) - n else bytes(n)

def decode_tex(gs, t, c):
    wd, ht, fmt, tbp, clut = t
    if c:
        (t0, t1), _ = c.most_common(1)[0]
        tbw, cbp, csa = ((t0 >> 14) & 63) or 1, (t1 >> 5) & 0x3FFF, (t1 >> 24) & 31
    else:
        tbw, cbp, csa = max(1, (wd + 63) // 64), clut, 0
    base_b = (tbp - BASE) * 256
    if fmt == 0:
        rows, amax, amin = [], 0, 255
        for y in range(ht):
            row = bytearray()
            for x in range(wd):
                r, g, b, a = rd(gs, base_b + (((y >> 5) * tbw + (x >> 6)) * 2048 + w32(x & 63, y & 31)) * 4, 4)
                amax, amin = max(amax, a), min(amin, min(255, a * 2))
                row += bytes((r, g, b, min(255, a * 2)))
            rows.append(bytes(row))
        return rows, (amax > 4 and amin < 255)
    ppr = max(1, tbw >> 1)
    rawpal = []
    for e in range(256 if fmt == 19 else 16):
        ee = e if fmt == 19 else csa * 16 + e
        X = (ee & 7) | (((ee >> 4) & 1) << 3)
        Y = ((ee >> 3) & 1) | ((ee >> 5) << 1)
        rawpal.append(tuple(rd(gs, (cbp - BASE) * 256 + w32(X, Y) * 4, 4)))
    pal = [bytes((r, g, b, min(255, a * 2))) for r, g, b, a in rawpal]
    use_alpha = max(p[3] for p in rawpal) > 4 and min(p[3] for p in pal) < 255
    rows = []
    for y in range(ht):
        row = bytearray()
        for x in range(wd):
            if fmt == 19:
                o = base_b + BT8[(y >> 4) & 3][(x >> 4) & 7] * 256 + 8192 * ((y >> 6) * ppr + (x >> 7)) + col8(x & 15, y & 15)
                v = rd(gs, o, 1)[0]
            else:
                off = col4(x & 31, y & 15)
                o = base_b + BT4[(y >> 4) & 7][(x >> 5) & 3] * 256 + 8192 * ((y >> 7) * ppr + (x >> 7)) + (off >> 1)
                v = rd(gs, o, 1)[0]
                v = (v >> 4) if off & 1 else (v & 15)
            row += pal[v]
        rows.append(bytes(row))
    return rows, use_alpha

def load(data):
    dsz, pmax, pcurr = struct.unpack_from("<III", data, 0x14D4)
    vsz = struct.unpack_from("<I", data, 0x11B8 + 8)[0]
    dram = vram = None
    i = 0x14EC
    while i + 4 <= len(data):
        size = struct.unpack_from("<I", data, i)[0]
        if size == 0 or i + 4 + size > len(data):
            break
        try:
            out = zlib.decompressobj().decompress(data[i + 4:i + 4 + size])
        except zlib.error:
            out = b""
        if len(out) == dsz and dram is None:
            dram = out
        elif len(out) == vsz and vram is None:
            vram = out
        i += 4 + size
    return dram, pcurr, vram

def payload(dram, pos, qwc):
    w2, w3 = struct.unpack_from("<2I", dram, pos + 8)
    stream = struct.pack("<2I", w2, w3) + dram[pos + 16: pos + 16 + qwc * 16]
    i, mem = 0, {}
    while i + 4 <= len(stream):
        code = struct.unpack_from("<I", stream, i)[0]
        i += 4
        cmd, num, imm = (code >> 24) & 0x7F, (code >> 16) & 0xFF, code & 0xFFFF
        if cmd >= 0x60 and (cmd & 0xF) == 12:
            n = min(num or 256, (len(stream) - i) // 16)
            addr = imm & 0x3FF
            for k in range(n):
                mem[addr + k] = struct.unpack_from("<4I", stream, i + 16 * k)
            i += n * 16
    return mem

def fl(words, n):
    return struct.unpack("<%df" % n, struct.pack("<%dI" % n, *words[:n]))

def get_output_dir(N, out_root):
    world_idx = (N - 1) % 6
    world_name = WORLDS[world_idx]
    if N <= 24:
        mode = "normal"
        sub_idx = (N - 1) // 6
    else:
        mode = "reverse"
        sub_idx = (N - 25) // 6
    folder_num = sub_idx + 1
    return os.path.join(out_root, world_name, str(folder_num), mode)

def export(N, with_tex, iso, out_root):
    raw = iso.read_file("BB/%d/%d.BBK" % (N // 10, N % 10))
    if raw is None:
        return "%02d file non trovato nell'ISO" % N
    dram, pcurr, vram = load(raw)
    if dram is None:
        return "%02d blocco dRAM non trovato" % N
    root = len(dram) - 0x70

    def rel(p):
        return p - pcurr if pcurr <= p < pcurr + len(dram) else None

    def ptr(field):
        return rel(struct.unpack_from("<I", dram, root + field)[0])

    u32 = lambda o: struct.unpack_from("<I", dram, o)[0]

    pa = rel(u32(root))
    numTex, numTri = struct.unpack_from("<2i", dram, pa + 56)
    pTex, pTri = struct.unpack_from("<2I", dram, pa + 68)
    pTex, pTri = rel(pTex), rel(pTri)
    pAlpha = rel(u32(pa + 84))

    tbp2tex, texinfo, texlist = {}, {}, {}
    for k in range(numTex):
        t = rel(u32(pTex + 4 * k))
        wd, ht, fmt = struct.unpack_from("<3i", dram, t + 8)
        clut = struct.unpack_from("<i", dram, t + 0x14)[0]
        idx0 = struct.unpack_from("<i", dram, t + 0x2C)[0]
        tbp2tex.setdefault(idx0, k)
        texinfo[k] = (wd, ht, fmt)
        texlist[k] = (wd, ht, fmt, idx0, clut)

    packs, pos = [], pTri
    while True:
        w0 = u32(pos)
        qwc, idv = w0 & 0xFFFF, (w0 >> 28) & 7
        if idv not in (1, 6, 7):
            break
        packs.append((pos, qwc))
        pos += 16 + qwc * 16
        if idv in (6, 7):
            break

    tris, unknown = [], 0
    tex0 = collections.defaultdict(collections.Counter)
    for pos, qwc in packs:
        mem = payload(dram, pos, qwc)
        curr_idx = 0
        while curr_idx in mem:
            h = mem[curr_idx]
            num_tris = h[2]
            if num_tris == 0:
                break
            tex = tbp2tex.get(h[0] & 0x3FFF, -1)
            if tex < 0:
                unknown += 1
            tex0[h[0] & 0x3FFF][(h[0], h[1])] += 1
            n = min(num_tris, (len(mem) - (curr_idx + 4)) // 10)
            for t in range(n):
                b = curr_idx + 4 + 10 * t
                try:
                    nrm = fl(mem[b], 3)
                    P = [fl(mem[b + 1 + j], 3) for j in range(3)]
                    UV = [fl(mem[b + 4 + j], 2) for j in range(3)]
                    C = [tuple(min(1.0, x / 128.0) for x in mem[b + 7 + j][:3]) for j in range(3)]
                except KeyError:
                    break
                tris.append((tex, P, UV, C, nrm))
            curr_idx = curr_idx + 4 + 10 * num_tris
    tris.sort(key=lambda t: t[0])

    atris = []
    if pAlpha is not None:
        na = struct.unpack_from("<i", dram, pAlpha)[0]
        if not (0 <= na <= 100000):
            na = 0
        for k in range(na):
            b = pAlpha + 0x10 + k * 0xE0
            if b + 0xE0 > len(dram):
                break
            q = [struct.unpack_from("<4I", dram, b + 16 * i) for i in range(14)]
            tex0[q[0][0] & 0x3FFF][(q[0][0], q[0][1])] += 1
            atris.append((tbp2tex.get(q[0][0] & 0x3FFF, -1), [fl(q[5 + j], 3) for j in range(3)],
                          [fl(q[8 + j], 2) for j in range(3)],
                          [tuple(min(1.0, x / 128.0) for x in q[11 + j][:3]) for j in range(3)], fl(q[4], 3)))
    atris.sort(key=lambda t: t[0])

    out_dir = get_output_dir(N, out_root)
    os.makedirs(out_dir, exist_ok=True)
    name = "%02d" % N
    used = sorted(set(t[0] for t in tris + atris if t[0] >= 0))
    tex_ok, tex_alpha = {}, set()
    if with_tex and vram is not None and used:
        gs = build_gs(vram)
        tdir = os.path.join(out_dir, name + "_tex")
        os.makedirs(tdir, exist_ok=True)
        for k in used:
            wd, ht, fmt, tbp, clut = texlist[k]
            if fmt not in (0, 19, 20):
                continue
            try:
                rows, use_alpha = decode_tex(gs, texlist[k], tex0.get(tbp))
            except Exception:
                continue
            png_save(os.path.join(tdir, "tex_%03d.png" % k), wd, ht, rows, use_alpha)
            tex_ok[k] = "%s_tex/tex_%03d.png" % (name, k)
            if use_alpha:
                tex_alpha.add(k)

    verts, vindex = [], {}
    groups = collections.OrderedDict()

    def V(x, y, z):
        k = (round(x, 2), round(y, 2), round(z, 2))
        if k not in vindex:
            vindex[k] = len(verts) + 1
            verts.append(k)
        return vindex[k]

    def face(group, mat, ids):
        groups.setdefault((group, mat), []).append(ids)

    def cube(group, mat, cx, cy, cz, s):
        h = s / 2.0
        c = [V(cx + dx * h, cy + dy * h, cz + dz * h) for dx in (-1, 1) for dy in (-1, 1) for dz in (-1, 1)]
        for q in ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)):
            face(group, mat, [c[j] for j in q])

    info_log = []

    g = ptr(0x14)
    npoly = polys = 0
    if g is not None:
        npoly = u32(g + 0x30)
        polys = rel(u32(g + 0x4C))
        for k in range(npoly):
            b = polys + k * 0x60
            nv, gid = struct.unpack_from("<2B", dram, b + 0x50)
            if nv not in (3, 4):
                continue
            ids = []
            for j in range(nv):
                x, z, h_val = struct.unpack_from("<3f", dram, b + 0x10 + j * 0x10)
                ids.append(V(x, h_val, z))
            face("collisioni", "col_gid2" if gid == 2 else "col_red", ids)
        info_log.append("coll %d" % npoly)

    prio_count = collections.Counter()
    lines = collections.OrderedDict()
    prio_seen = set()
    ai = ptr(0x3C)
    if ai is not None:
        ns = struct.unpack_from("<h", dram, ai)[0]
        base = ai + 4

        def idx(p):
            # next/prev nel file sono puntatori: 0 = nessun nodo, altrimenti base + indice * 0x6C
            if p == 0:
                return None
            o = p - pcurr - base
            return o // 0x6C if o % 0x6C == 0 and 0 <= o // 0x6C < ns else None

        nodes = []
        for k in range(ns):
            o = base + k * 0x6C
            prio = struct.unpack_from("<B", dram, o)[0]
            fdist = struct.unpack_from("<i", dram, o + 4)[0]
            prv = [idx(v) for v in struct.unpack_from("<2I", dram, o + 8)]
            nxt = [idx(v) for v in struct.unpack_from("<2I", dram, o + 0x10)]
            p = struct.unpack_from("<8h", dram, o + 0x18)
            c = struct.unpack_from("<3h", dram, o + 0x28)
            onwater = struct.unpack_from("<b", dram, o + 0x6B)[0]
            nodes.append({"prio": prio, "fdist": fdist, "prev": prv, "next": nxt, "A": p[0:3], "B": p[4:7],
                          "c": c, "onwater": onwater})
        total_lap = struct.unpack_from("<i", dram, base + ns * 0x6C + 4)[0] if base + ns * 0x6C + 8 <= len(dram) else 0

        # zona del nodo, come la calcola AINodeCreateLinkInfo: rettangolo (x, z) che contiene i due estremi
        # del cancello di questo nodo e i due del nodo successivo (nei bivi conta l'ULTIMO next valido).
        # I bounds salvati nel file sono tutti uguali (riempitivo) e il gioco li sovrascrive al caricamento.
        for n in nodes:
            nx = [i for i in n["next"] if i is not None]
            if nx:
                m = nodes[nx[-1]]
                xs = [n["A"][0], n["B"][0], m["A"][0], m["B"][0]]
                zs = [n["A"][2], n["B"][2], m["A"][2], m["B"][2]]
                n["zone"] = (min(xs), max(xs), min(zs), max(zs))
            else:
                n["zone"] = None

        def box_lines(grp, mat, x0, x1, y0, y1, z0, z1):
            c8 = [V(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
            for a, b in ((0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7)):
                if c8[a] != c8[b]:
                    lines.setdefault((grp, mat), []).append((c8[a], c8[b]))

        for k, n in enumerate(nodes):
            pr = n["prio"]
            prio_seen.add(pr)
            prio_count[pr] += 1
            mat = "nodo_p%02d" % pr
            A, B, c = n["A"], n["B"], n["c"]
            face("nodi_ia_p%02d" % pr, mat, [V(*A), V(*B), V(B[0], B[1] - 150, B[2]), V(A[0], A[1] - 150, A[2])])
            cube("nodi_ia_centri_p%02d" % pr, mat, c[0], c[1], c[2], 60)
            if n["zone"]:
                x0, x1, z0, z1 = n["zone"]
                box_lines("zona_p%02d_n%03d" % (pr, k), mat, x0, x1, c[1] - ZONE_H, c[1] + ZONE_H, z0, z1)

        # quanta strada (poligoni ma0, non acqua) ha il centro dentro almeno una zona
        road = cov_ok = 0
        if g is not None:
            for k in range(npoly):
                b = polys + k * 0x60
                nv, gid, ma, mat_ = struct.unpack_from("<4B", dram, b + 0x50)
                if nv not in (3, 4) or ma != 0 or gid == 2:
                    continue
                vs = [struct.unpack_from("<3f", dram, b + 0x10 + j * 0x10) for j in range(nv)]
                x, z, y = (sum(v[i] for v in vs) / nv for i in range(3))
                road += 1
                for n in nodes:
                    if n["zone"] and n["zone"][0] <= x <= n["zone"][1] and n["zone"][2] <= z <= n["zone"][3] \
                            and n["c"][1] - ZONE_H < y < n["c"][1] + ZONE_H:
                        cov_ok += 1
                        break

        # report testuale per pista
        fmt = lambda v: "-" if v is None else str(v)
        with open(os.path.join(out_dir, name + "_nodi.txt"), "w") as rf:
            rf.write("Pista %d - %d nodi IA - lunghezza del giro (nodeTotalDist) %d\n" % (N, ns, total_lap))
            rf.write("Zona di un nodo = rettangolo x,z + altezza centre.y +-%d. Conta solo il CENTRO dell'auto.\n" % ZONE_H)
            rf.write("next/prev = numeri di nodo ('-' = nessuno). Un nodo con due next e' un BIVIO.\n\n")
            rf.write("Priority trovate in questa pista:\n")
            for pr in sorted(prio_count):
                rf.write("  %2d  x%-4d %s\n" % (pr, prio_count[pr], PRIO_INFO.get(pr, "sconosciuta")))
            rf.write("\n idx prio  next0 next1  prev0 prev1 acqua finishDist   centro(x,y,z)         zona x0..x1 / z0..z1        dim x*z   largh.cancello\n")
            for k, n in enumerate(nodes):
                c = n["c"]
                if n["zone"]:
                    x0, x1, z0, z1 = n["zone"]
                    zs_txt = "%6d..%-6d / %6d..%-6d  %5d*%-5d" % (x0, x1, z0, z1, x1 - x0, z1 - z0)
                else:
                    zs_txt = "nessuna zona (nodo senza next)"
                gw = ((n["A"][0] - n["B"][0]) ** 2 + (n["A"][2] - n["B"][2]) ** 2) ** 0.5
                rf.write("%4d %4d  %5s %5s  %5s %5s  %4d  %9d   %6d %6d %6d   %s   %7.0f\n" % (
                    k, n["prio"], fmt(n["next"][0]), fmt(n["next"][1]), fmt(n["prev"][0]), fmt(n["prev"][1]),
                    n["onwater"], n["fdist"], c[0], c[1], c[2], zs_txt, gw))
        info_log.append("nodi %d, bivi %d, strada dentro le zone %.0f%% prio %s" % (
            ns, sum(1 for n in nodes if n["next"][1] is not None), 100.0 * cov_ok / max(1, road),
            dict(sorted(prio_count.items()))))

    sp = ptr(0x30)
    if sp is not None:
        for k in range(8):
            x, y, z, _ = struct.unpack_from("<4h", dram, sp + k * 8)
            cube("partenza", "start", x, y, z, 120)
        info_log.append("start 8")

    pp = ptr(0x38)
    if pp is not None:
        cnt = u32(pp + 4)
        cnt = cnt if 0 < cnt <= 200 else 0
        n_pick = fissi = 0
        for k in range(cnt):
            o = pp + 16 + k * 16
            if o + 16 > len(dram):
                break
            x, y, z, t = struct.unpack_from("<4h", dram, o)
            if t == 0:
                cube("pickup", "pickup", x, y, z, 90)
            else:
                cube("pickup_fisso_%s" % WN.get(t, t), "pickup_fisso", x, y, z, 140)
                fissi += 1
            n_pick += 1
        info_log.append("pickup %d (fissi %d)" % (n_pick, fissi))

    ts = ptr(0x04)
    if ts is not None and g is not None:
        gx0 = struct.unpack_from("<f", dram, g)[0]
        gz0 = struct.unpack_from("<f", dram, g + 8)[0]
        gx1 = struct.unpack_from("<f", dram, g + 0xC)[0]
        gz1 = struct.unpack_from("<f", dram, g + 0x14)[0]
        nobj = u32(ts)
        shown = 0
        if 0 < nobj <= 2000:
            for k in range(nobj):
                r = ts + 4 + k * 0x3C
                if r + 0x3C > len(dram):
                    break
                w_vals = struct.unpack_from("<15i", dram, r)
                x, y, z = w_vals[4] / 256.0, w_vals[5] / 256.0, w_vals[6] / 256.0
                if abs(x) < 5 and abs(z) < 5:
                    continue
                if gx0 - 2000 <= x <= gx1 + 2000 and gz0 - 2000 <= z <= gz1 + 2000:
                    cube("oggetti", "oggetto", x, y, z, 110)
                    shown += 1
        info_log.append("oggetti %d/%d" % (shown, nobj))

    with open(os.path.join(out_dir, name + ".mtl"), "w") as f:
        for m, (r, g_col, b) in COL.items():
            f.write("newmtl %s\nKd %.2f %.2f %.2f\nd 1\n\n" % (m, r, g_col, b))
        for pr in sorted(prio_seen):
            r, g_col, b = PRIO_COL.get(pr, PRIO_ALTRO)
            f.write("newmtl nodo_p%02d\nKd %.2f %.2f %.2f\nd 1\n\n" % (pr, r, g_col, b))
        for k in used:
            wd, ht, fmt = texinfo[k]
            f.write("newmtl tex_%03d\n# %dx%d fmt %d\n" % (k, wd, ht, fmt))
            if k in tex_ok:
                f.write("Kd 1 1 1\nmap_Kd %s\n" % tex_ok[k])
                if k in tex_alpha:
                    f.write("map_d %s\n" % tex_ok[k])
            else:
                f.write("Kd 0.6 0.6 0.6\n")
            f.write("\n")
        f.write("newmtl tex_sconosciuta\nKd 0.5 0.5 0.5\n\n")

    with open(os.path.join(out_dir, name + ".obj"), "w") as f:
        f.write("# RC Revenge Pro - Pista completa %d\nmtllib %s.mtl\n\n" % (N, name))
        vi = ni = 0
        for gname, lst in (("visibile", tris), ("trasparenti", atris)):
            f.write("g %s\n" % gname)
            cur = object()
            for tex, P, UV, C, nrm in lst:
                if tex != cur:
                    cur = tex
                    f.write("usemtl %s\n" % ("tex_%03d" % tex if tex >= 0 else "tex_sconosciuta"))
                for j in range(3):
                    f.write("v %.3f %.3f %.3f %.3f %.3f %.3f\n" % (P[j] + C[j]))
                    f.write("vt %.5f %.5f\n" % (UV[j][0], (1.0 - UV[j][1]) if FLIP_V else UV[j][1]))
                f.write("vn %.4f %.4f %.4f\n" % nrm)
                ni += 1
                f.write("f %d/%d/%d %d/%d/%d %d/%d/%d\n" % (
                    vi + 1, vi + 1, ni, vi + 2, vi + 2, ni, vi + 3, vi + 3, ni))
                vi += 3
        if verts:
            f.write("\n# --- Altri elementi di gioco (Collisioni, IA, Pickup, Oggetti) ---\n")
            for v in verts:
                f.write("v %.2f %.2f %.2f\n" % v)
            for (grp, mat), faces in groups.items():
                f.write("g %s\nusemtl %s\n" % (grp, mat))
                for ids in faces:
                    f.write("f " + " ".join(str(i_ + vi) for i_ in ids) + "\n")
            for (grp, mat), segs in sorted(lines.items(), key=lambda kv: kv[0][0]):
                f.write("g %s\nusemtl %s\n" % (grp, mat))
                for a, b in segs:
                    f.write("l %d %d\n" % (a + vi, b + vi))

    tot = len(tris) + len(atris)
    return "%02d -> [%s] scatole %d | triangoli %d + trasparenti %d = %d / dichiarati %d | texture %d/%d | %s" % (
        N, out_dir, len(packs), len(tris), len(atris), tot, numTri, len(tex_ok), len(used), ", ".join(info_log))


# ---------------- interfaccia grafica ----------------
def parse_ids(text):
    """'' -> 1..48; '1 5 10-12' o '1,5,10-12' -> [1,5,10,11,12]"""
    text = text.replace(",", " ").strip()
    if not text:
        return list(range(1, 49))
    ids = []
    for tok in text.split():
        if "-" in tok:
            a, b = tok.split("-", 1)
            ids.extend(range(int(a), int(b) + 1))
        else:
            ids.append(int(tok))
    bad = [i for i in ids if not 1 <= i <= 48]
    if bad:
        raise ValueError("Numeri pista fuori range (1-48): %s" % bad)
    return ids


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RC Revenge Pro - Export tracks from ISO")
        self.geometry("720x520")
        self.minsize(620, 420)
        self.q = queue.Queue()
        self.worker = None
        self.stop_flag = threading.Event()

        self.iso_var = tk.StringVar()
        self.out_var = tk.StringVar()
        self.ids_var = tk.StringVar()
        self.notex_var = tk.BooleanVar(value=False)

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(5, weight=1)

        ttk.Label(frm, text="ISO File:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.iso_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="Browse...", command=self.pick_iso).grid(row=0, column=2)

        ttk.Label(frm, text="Export folder:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="Browse...", command=self.pick_out).grid(row=1, column=2)

        ttk.Label(frm, text="Tracks (blank = all):").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.ids_var).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Label(frm, text="e.g. 1 5 10-12", foreground="gray").grid(row=2, column=2, sticky="w")

        ttk.Checkbutton(frm, text="No textures (faster)", variable=self.notex_var).grid(
            row=3, column=1, sticky="w", padx=6)

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="ew", pady=8)
        self.start_btn = ttk.Button(btns, text="Extract", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Cancel", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(btns, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=6)

        logf = ttk.Frame(frm)
        logf.grid(row=5, column=0, columnspan=3, sticky="nsew")
        self.log = tk.Text(logf, height=10, wrap="word", state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.after(100, self.poll)

    def pick_iso(self):
        p = filedialog.askopenfilename(title="Select game ISO",
                                       filetypes=[("Disc images", "*.iso *.bin *.img"), ("All files", "*.*")])
        if p:
            self.iso_var.set(p)

    def pick_out(self):
        p = filedialog.askdirectory(title="Select export folder")
        if p:
            self.out_var.set(p)

    def write_log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self):
        iso_path, out_root = self.iso_var.get().strip(), self.out_var.get().strip()
        if not os.path.isfile(iso_path):
            messagebox.showerror("Error", "Please select a valid ISO file.")
            return
        if not out_root:
            messagebox.showerror("Error", "Please select the export folder.")
            return
        try:
            ids = parse_ids(self.ids_var.get())
        except ValueError as e:
            messagebox.showerror("Error", "Invalid track list: %s" % e)
            return
        self.stop_flag.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(maximum=len(ids), value=0)
        self.write_log("--- Starting: %d tracks ---" % len(ids))
        self.worker = threading.Thread(target=self.run_job,
                                       args=(iso_path, out_root, ids, not self.notex_var.get()), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag.set()
        self.stop_btn.configure(state="disabled")
        self.write_log("Cancellation requested: finishing current track...")

    def run_job(self, iso_path, out_root, ids, with_tex):
        iso = None
        try:
            iso = Iso(iso_path)
            if not iso.has_dir("BB"):
                self.q.put(("log", "'BB' folder not found in ISO: is this the right game?"))
                return
            for n, N in enumerate(ids, 1):
                if self.stop_flag.is_set():
                    self.q.put(("log", "Interrupted."))
                    break
                try:
                    self.q.put(("log", export(N, with_tex, iso, out_root)))
                except Exception as e:
                    self.q.put(("log", "%02d ERROR: %s" % (N, e)))
                self.q.put(("progress", n))
            else:
                self.q.put(("log", "\nDone! Files sorted into world folders."))
        except Exception as e:
            self.q.put(("log", "ERROR: %s" % e))
        finally:
            if iso:
                iso.close()
            self.q.put(("done", None))

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.write_log(val)
                elif kind == "progress":
                    self.progress.configure(value=val)
                elif kind == "done":
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self.poll)


if __name__ == "__main__":
    App().mainloop()
