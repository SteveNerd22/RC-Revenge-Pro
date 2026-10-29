# bbk_node_gui.py - versione GUI di bbk_node_range.py (finestra dei nodi, ricerca locale + globale)
#
# - Legge la pista direttamente dall'ISO (BB/x/y.BBK) oppure da un file .BBK scelto a mano.
# - Mostra TUTTI i nodi della pista visti dall'alto (x, z; l'altezza e' ignorata) oppure in vista lineare.
# - Evidenzia i nodi trovati dalla ricerca LOCALE (L), GLOBALE (G) o da entrambe (LG).
# - Click su un nodo = diventa il nodo corrente e la simulazione viene rifatta da li'.
# - Rotellina = zoom, trascinamento = sposta la vista.
#
# La simulazione e' la stessa di bbk_node_range.py:
#    LOCALE  = AINodeBackTrackRecursive(cur, 8) + AINodeForwardTrackRecursive(cur, 0x10), iRange = 0x10
#    GLOBALE = AINodeNearestInRange: nodi con |iFinishDist - iFinishDist(cur)| < soglia
# Sono nodi CANDIDATI: in gioco il centro della macchina deve anche stare dentro la zona del nodo.
import os, json, struct, zlib, heapq
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

TRACK_NAMES = [
    "01 - Trials from the Crypt", "02 - Cars Attacks!", "03 - Temple of Vroom", "04 - HillTop Hairpins", "05 - It came to have a race", "06 - Cut-Throttle Island",
    "07 - I know what you drove last summer", "08 - 20:1 A Space Odd RC", "09 - Racers of the Lost Park", "10 - Calamity Canyon", "11 - Attack of the Killer Toy Motors", "12 - Pile-Ups of the High Seas",
    "13 - Trials from the Crypt - the director's cut", "14 - Cars Attacks! Again!", "15 - Temple of Vroom - The awakening", "16 - HillTop Hairpins - The Special Edition", "17 - It returned to have another race", "18 - Cut-Throttle Island - Uncut",
    "19 - I still know what you drove last summer", "20 - 20:1 A Space Odd RC - Digitally remastered", "21 - Racers of the Lost Park - Collector's edition", "22 - Calamity Canyon - Live and dangerous", "23 - Return of the Killer Toy Motors", "24 - Pile-Ups of the High Seas - The Legacy",
]
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".bbk_node_gui.json")

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
        for k in range((size + 2047) // 2048):
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


# ---------------- lettura pista ----------------
def load_nodes(data):
    """Dal contenuto di un .BBK ritorna (nodi, nodeTotalDist). Solleva ValueError se qualcosa non torna."""
    if len(data) < 0x14EC:
        raise ValueError("File troppo corto: non sembra un .BBK.")
    dsz, pmax, pcurr = struct.unpack_from("<III", data, 0x14D4)
    dram = None
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
        i += 4 + size
    if dram is None:
        raise ValueError("Blocco dRAM non trovato nel file.")
    root = len(dram) - 0x70
    ai = struct.unpack_from("<I", dram, root + 0x3C)[0] - pcurr
    if not (0 <= ai < len(dram) - 4):
        raise ValueError("Puntatore ai nodi non valido (questa pista non ha nodi IA?).")
    ns = struct.unpack_from("<h", dram, ai)[0]
    base = ai + 4
    if ns <= 0 or base + ns * 0x6C > len(dram):
        raise ValueError("Numero di nodi non valido: %d" % ns)

    def idx(v):
        if v == 0:
            return None
        o = v - pcurr - base
        return o // 0x6C if o % 0x6C == 0 and 0 <= o // 0x6C < ns else None

    nodes = []
    for k in range(ns):
        o = base + k * 0x6C
        p = struct.unpack_from("<8h", dram, o + 0x18)
        nodes.append({
            "prio": struct.unpack_from("<B", dram, o)[0],
            "fdist": struct.unpack_from("<i", dram, o + 4)[0],
            "prev": [idx(v) for v in struct.unpack_from("<2I", dram, o + 8)],
            "next": [idx(v) for v in struct.unpack_from("<2I", dram, o + 0x10)],
            "A": p[0:3], "B": p[4:7],
            "c": struct.unpack_from("<3h", dram, o + 0x28),
            "water": struct.unpack_from("<b", dram, o + 0x6B)[0] == -1,
        })
    total = 0
    if base + ns * 0x6C + 8 <= len(dram):
        total = struct.unpack_from("<i", dram, base + ns * 0x6C + 4)[0]
    return nodes, total


# ---------------- ricerca locale (giocatore) ----------------
def local_walk(nodes, cur):
    vis = {}

    def upd(n, count, d, pos):
        vis.setdefault(n, set()).add(pos)
        count -= 1
        if count == 0:
            return
        nx, pv = nodes[n]["next"], nodes[n]["prev"]
        if d == 1:
            if nx[1] is None:
                n2 = nx[0]
                if n2 is None:
                    return
            else:
                if nx[0] is not None:
                    upd(nx[0], count, 1, pos + 1)
                n2 = nx[1]
            upd(n2, count, 1, pos + 1)
        else:
            for q in pv:
                if q is not None:
                    upd(q, count, 2, pos - 1)

    def back(n, count, rng, pos):
        base = pos
        while True:
            count -= 1
            if count == 0:
                upd(n, rng, 1, pos)
                return
            if nodes[n]["prev"][0] is None:
                upd(n, rng, 1, pos)
            else:
                back(nodes[n]["prev"][0], count, rng, pos - 1)
            n = nodes[n]["prev"][1]
            pos = base - 1
            if n is None:
                return

    def fwd(n, count, rng, pos):
        base = pos
        while True:
            count -= 1
            if count == 0:
                upd(n, rng, 2, pos)
                return
            if nodes[n]["next"][0] is not None:
                fwd(nodes[n]["next"][0], count, rng, pos + 1)
            n = nodes[n]["next"][1]
            pos = base + 1
            if n is None:
                return

    back(cur, 8, 0x10, 0)
    fwd(cur, 0x10, 0x10, 0)
    return vis


def global_set(nodes, cur, soglia):
    ref = nodes[cur]["fdist"]
    return {j for j, n in enumerate(nodes) if abs(ref - n["fdist"]) < soglia}


def net_steps(nodes, cur):
    best = {cur: (0, 0, 0)}
    pq = [(0, 0, 0, cur)]
    while pq:
        d, an, net, a = heapq.heappop(pq)
        if best.get(a, (1 << 30,))[:2] < (d, an):
            continue
        for key, delta in (("next", 1), ("prev", -1)):
            for b in nodes[a][key]:
                if b is None:
                    continue
                nn = net + delta
                cand = (d + 1, abs(nn), nn)
                if b not in best or cand[:2] < best[b][:2]:
                    best[b] = cand
                    heapq.heappush(pq, (cand[0], cand[1], cand[2], b))
    return {j: v[2] for j, v in best.items()}


def simulate(nodes, cur, soglia):
    w = nodes[cur]["water"]
    stepsG = net_steps(nodes, cur)
    walk = {j: p for j, p in local_walk(nodes, cur).items() if nodes[j]["water"] == w}
    L = set(walk)
    G = {j for j in global_set(nodes, cur, soglia) if nodes[j]["water"] == w}
    return {"walk": walk, "L": L, "G": G, "stepsG": stepsG}


def build_report(title, nodes, total, cur, soglia, sim, verso, show_fdist):
    c = nodes[cur]
    walk, L, G, stepsG = sim["walk"], sim["L"], sim["G"], sim["stepsG"]
    U = L | G
    out = []
    out.append("%s - nodo %d (fdist %d, priority %d, fondo %s)" %
               (title, cur, c["fdist"], c["prio"], "acqua" if c["water"] else "terra"))
    out.append("%d nodi, nodeTotalDist %d" % (len(nodes), total))
    biv = [i for i, n in enumerate(nodes) if n["next"][1] is not None]
    uni = [i for i, n in enumerate(nodes) if n["prev"][1] is not None]
    out.append("bivi (due successivi): %s" % (biv or "-"))
    out.append("rientri (due precedenti): %s" % (uni or "-"))

    righe = {}
    for j, ps in walk.items():
        for p in ps:
            righe.setdefault(p, {}).setdefault(j, set()).add("L")
    for j in G:
        if j in stepsG:
            righe.setdefault(stepsG[j], {}).setdefault(j, set()).add("G")

    def estremi_tag(t):
        ps = [p for p, d in righe.items() if any(t in tg for tg in d.values())]
        return (min(ps), max(ps)) if ps else None

    out.append("")
    out.append("RIEPILOGO (passo 0 = nodo corrente, + avanti, - indietro)")
    for nome, t, S in (("SOLO LOCALE ", "L", L), ("SOLO GLOBALE", "G", G)):
        e = estremi_tag(t)
        out.append("  %s  da %+d a %+d   (%d nodi)" % (nome, e[0], e[1], len(S)) if e
                   else "  %s  nessun nodo" % nome)
    if righe:
        out.append("  UNIONE       da %+d a %+d   (%d nodi)" % (min(righe), max(righe), len(U)))
    out.append("  (globale = fdist da %d a %d)" % (c["fdist"] - soglia + 1, c["fdist"] + soglia - 1))
    if c["fdist"] - soglia < 0 or c["fdist"] + soglia > total:
        out.append("  ATTENZIONE: la fascia esce da [0, nodeTotalDist]; ipotesi dal codice: la distanza non")
        out.append("  e' circolare, quindi i nodi oltre il traguardo non sono coperti dalla globale.")

    out.append("")
    out.append("PER OGNI PASSO   (L = locale, G = globale, LG = entrambe)")
    for p in sorted(righe):
        d = righe[p]
        parts = []
        for j in sorted(d):
            t = "LG" if d[j] == {"L", "G"} else next(iter(d[j]))
            if show_fdist:
                parts.append("%d(%s, %+d)" % (j, t, nodes[j]["fdist"] - c["fdist"]))
            else:
                parts.append("%d(%s)" % (j, t))
        out.append("  %+4d   %s" % (p, "  ".join(parts)))

    if verso:
        out.append("")
        out.append("ATTERRAGGI")
        for j in verso:
            if not 0 <= j < len(nodes):
                out.append("  nodo %d: fuori range" % j)
                continue
            pl = ", ".join("%+d" % p for p in sorted(walk.get(j, ()))) or "-"
            sg = "%+d" % stepsG[j] if (j in G and j in stepsG) else "-"
            out.append("  nodo %d: diff.fdist %+d -> locale %s (passi %s), globale %s (passo %s)%s" % (
                j, nodes[j]["fdist"] - c["fdist"], "SI" if j in L else "no", pl,
                "SI" if j in G else "no", sg, "" if (j in L or j in G) else "    => NON agganciato"))
    out.append("")
    out.append("Sono nodi CANDIDATI: in gioco il centro della macchina deve anche essere dentro la zona del nodo.")
    return "\n".join(out)


# ---------------- disposizione dei nodi ----------------
def layout_top(nodes, invert_z):
    """Vista dall'alto: x orizzontale, z verticale (l'altezza y e' ignorata)."""
    sgn = -1.0 if invert_z else 1.0
    return [(float(n["c"][0]), sgn * float(n["c"][2])) for n in nodes]


def layout_linear(nodes, total):
    """Vista lineare: x = fdist, i rami (secondo successivo di un bivio) su una riga sotto."""
    order = sorted(range(len(nodes)), key=lambda i: nodes[i]["fdist"])
    lane, forced = {}, {}
    for i in order:
        if i in forced:
            l = forced[i]
        else:
            pl = [lane[p] for p in nodes[i]["prev"] if p is not None and p in lane]
            l = min(pl) if pl else 0
        lane[i] = l
        if nodes[i]["next"][1] is not None:
            forced.setdefault(nodes[i]["next"][1], l + 1)
    top = max(total, max((n["fdist"] for n in nodes), default=1), 1)
    sx = 1000.0 / top
    return [(nodes[i]["fdist"] * sx, lane.get(i, 0) * 40.0) for i in range(len(nodes))]


# ---------------- interfaccia ----------------
COL_L, COL_G, COL_LG = "#1e88e5", "#fb8c00", "#43a047"
COL_CUR, COL_OTHER, COL_VERSO = "#e53935", "#b0b4ba", "#d81b60"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RC Revenge Pro - Node viewer")
        self.geometry("1180x720")
        self.minsize(900, 560)

        self.nodes, self.total, self.title_txt = [], 0, ""
        self.cur, self.sim, self.verso = 0, None, []
        self.pos = []
        self.view = (1.0, 0.0, 0.0)        # scale, offset x, offset y (screen = world * scale + offset)
        self.screen = []
        self._press = None
        self._need_fit = False

        self.iso_var = tk.StringVar()
        self.map_var = tk.StringVar(value=TRACK_NAMES[0])
        self.reverse_var = tk.BooleanVar(value=False)
        self.node_var = tk.StringVar(value="0")
        self.soglia_var = tk.StringVar(value="11000")
        self.verso_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="top")
        self.num_var = tk.BooleanVar(value=False)
        self.fd_var = tk.BooleanVar(value=False)
        self.gate_var = tk.BooleanVar(value=False)
        self.inv_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Select ISO and track, then press Load.")

        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                self.iso_var.set(json.load(f).get("iso", ""))
        except Exception:
            pass

        top = ttk.Frame(self, padding=(10, 8, 10, 2))
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="ISO (or .BBK file):").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.iso_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Browse...", command=self.pick_iso).grid(row=0, column=2)
        ttk.Label(top, text="Track:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        row1 = ttk.Frame(top)
        row1.grid(row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=(4, 0))
        ttk.Combobox(row1, textvariable=self.map_var, state="readonly", width=34,
                     values=TRACK_NAMES).pack(side="left")
        ttk.Checkbutton(row1, text="Reverse", variable=self.reverse_var).pack(side="left", padx=10)
        ttk.Button(row1, text="Load", command=self.load_map).pack(side="left", padx=8)

        opt = ttk.Frame(self, padding=(10, 2, 10, 2))
        opt.pack(fill="x")
        ttk.Label(opt, text="Node:").pack(side="left")
        self.node_spin = ttk.Spinbox(opt, textvariable=self.node_var, from_=0, to=0, width=6,
                                     command=self.on_node_entry)
        self.node_spin.pack(side="left", padx=(4, 10))
        self.node_spin.bind("<Return>", lambda e: self.on_node_entry())
        ttk.Label(opt, text="Global threshold:").pack(side="left")
        e = ttk.Entry(opt, textvariable=self.soglia_var, width=7)
        e.pack(side="left", padx=(4, 10))
        e.bind("<Return>", lambda ev: self.recompute())
        e.bind("<FocusOut>", lambda ev: self.recompute())
        ttk.Label(opt, text="Direction (landing nodes):").pack(side="left")
        e = ttk.Entry(opt, textvariable=self.verso_var, width=16)
        e.pack(side="left", padx=(4, 10))
        e.bind("<Return>", lambda ev: self.recompute())
        e.bind("<FocusOut>", lambda ev: self.recompute())

        opt2 = ttk.Frame(self, padding=(10, 0, 10, 4))
        opt2.pack(fill="x")
        ttk.Radiobutton(opt2, text="Top-down", variable=self.mode_var, value="top",
                        command=self.relayout).pack(side="left")
        ttk.Radiobutton(opt2, text="Linear", variable=self.mode_var, value="lin",
                        command=self.relayout).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(opt2, text="Invert Z", variable=self.inv_var, command=self.relayout).pack(side="left")
        ttk.Checkbutton(opt2, text="Numbers", variable=self.num_var, command=self.redraw).pack(side="left", padx=6)
        ttk.Checkbutton(opt2, text="Diff. fdist", variable=self.fd_var, command=self.on_fdist).pack(side="left")
        ttk.Checkbutton(opt2, text="Gates", variable=self.gate_var, command=self.redraw).pack(side="left", padx=6)
        ttk.Button(opt2, text="Fit view", command=self.fit_and_draw).pack(side="left", padx=6)

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        self.canvas = tk.Canvas(pw, background="#fafafa", highlightthickness=1, highlightbackground="#bbb")
        pw.add(self.canvas, weight=3)
        rf = ttk.Frame(pw)
        pw.add(rf, weight=2)
        self.text = tk.Text(rf, wrap="none", font=("Courier New", 9), state="disabled", width=48)
        sy = ttk.Scrollbar(rf, command=self.text.yview)
        sx = ttk.Scrollbar(rf, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self.text.pack(side="left", fill="both", expand=True)

        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(10, 2)).pack(fill="x")

        c = self.canvas
        c.bind("<Configure>", self.on_configure)
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Motion>", self.on_hover)
        c.bind("<MouseWheel>", self.on_wheel)
        c.bind("<Button-4>", lambda e: self.zoom(e.x, e.y, 1.2))
        c.bind("<Button-5>", lambda e: self.zoom(e.x, e.y, 1 / 1.2))

    # ----- file / loading -----
    def pick_iso(self):
        p = filedialog.askopenfilename(
            title="Select game ISO (or a .BBK file)",
            filetypes=[("ISO or BBK", "*.iso *.bin *.img *.bbk *.BBK"), ("All files", "*.*")])
        if p:
            self.iso_var.set(p)

    def load_map(self):
        path = self.iso_var.get().strip()
        if not os.path.isfile(path):
            messagebox.showerror("Error", "Please select a valid ISO (or .BBK) file.")
            return
        try:
            if path.lower().endswith(".bbk"):
                with open(path, "rb") as f:
                    data = f.read()
                title = os.path.basename(path)
            else:
                track_idx = TRACK_NAMES.index(self.map_var.get()) + 1
                if self.reverse_var.get():
                    N = track_idx + 24
                else:
                    N = track_idx
                iso = Iso(path)
                try:
                    data = iso.read_file("BB/%d/%d.BBK" % (N // 10, N % 10))
                finally:
                    iso.close()
                if data is None:
                    raise ValueError("BB/%d/%d.BBK not found in ISO: is this the right game?" % (N // 10, N % 10))
                title = TRACK_NAMES[track_idx - 1] + (" (Reverse)" if self.reverse_var.get() else "")
            nodes, total = load_nodes(data)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"iso": path}, f)
        except Exception:
            pass
        self.nodes, self.total, self.title_txt = nodes, total, title
        self.cur = 0
        self.node_var.set("0")
        self.node_spin.configure(to=len(nodes) - 1)
        self.status_var.set("%s: %d nodes loaded. Click a node to select it." % (title, len(nodes)))
        self.relayout(fit=True)
        self.recompute()

    # ----- simulation -----
    def get_soglia(self):
        try:
            return max(1, int(self.soglia_var.get()))
        except ValueError:
            return 11000

    def parse_verso(self):
        res = []
        for tok in self.verso_var.get().replace(",", " ").split():
            if tok.lstrip("-").isdigit():
                res.append(int(tok))
        return res

    def on_node_entry(self):
        if not self.nodes:
            return
        try:
            n = int(self.node_var.get())
        except ValueError:
            return
        if 0 <= n < len(self.nodes):
            self.cur = n
            self.recompute()
        else:
            self.node_var.set(str(self.cur))

    def on_fdist(self):
        self.recompute()

    def recompute(self):
        if not self.nodes:
            return
        self.node_var.set(str(self.cur))
        self.verso = self.parse_verso()
        self.sim = simulate(self.nodes, self.cur, self.get_soglia())
        rep = build_report(self.title_txt, self.nodes, self.total, self.cur, self.get_soglia(),
                           self.sim, self.verso, self.fd_var.get())
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", rep)
        self.text.configure(state="disabled")
        self.redraw()

    # ----- drawing -----
    def relayout(self, fit=False):
        if not self.nodes:
            return
        if self.mode_var.get() == "top":
            self.pos = layout_top(self.nodes, self.inv_var.get())
        else:
            self.pos = layout_linear(self.nodes, self.total)
        if fit or True:
            self.fit_and_draw()

    def fit_and_draw(self):
        if not self.nodes:
            return
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w < 50 or h < 50:            # window not yet drawn: postponing
            self._need_fit = True
            return
        xs = [p[0] for p in self.pos]
        ys = [p[1] for p in self.pos]
        dx, dy = max(xs) - min(xs), max(ys) - min(ys)
        m = 40
        s = min((w - 2 * m) / dx if dx > 0 else 1e9, (h - 2 * m) / dy if dy > 0 else 1e9)
        if s >= 1e8:
            s = 1.0
        ox = w / 2 - s * (min(xs) + max(xs)) / 2
        oy = h / 2 - s * (min(ys) + max(ys)) / 2
        self.view = (s, ox, oy)
        self._need_fit = False
        self.redraw()

    def on_configure(self, ev):
        if self._need_fit:
            self.fit_and_draw()
        else:
            self.redraw()

    def redraw(self):
        c = self.canvas
        c.delete("all")
        if not self.nodes or not self.pos:
            c.create_text(20, 20, anchor="nw", fill="#777",
                          text="No track loaded.")
            return
        s, ox, oy = self.view
        self.screen = [(p[0] * s + ox, p[1] * s + oy) for p in self.pos]
        nodes, sim = self.nodes, self.sim
        L = sim["L"] if sim else set()
        G = sim["G"] if sim else set()
        U = L | G
        verso = set(self.verso)

        # gates (segment A-B of each node), top-down view only
        if self.gate_var.get() and self.mode_var.get() == "top":
            sgn = -1.0 if self.inv_var.get() else 1.0
            for n in nodes:
                a = (n["A"][0] * s + ox, sgn * n["A"][2] * s + oy)
                b = (n["B"][0] * s + ox, sgn * n["B"][2] * s + oy)
                c.create_line(a[0], a[1], b[0], b[1], fill="#d7dbe0")

        # connections
        for i, n in enumerate(nodes):
            for j in n["next"]:
                if j is None:
                    continue
                if self.mode_var.get() == "lin" and nodes[j]["fdist"] < n["fdist"]:
                    continue        # lap closing connection: in linear view it would cross the whole track
                hot = i in U and j in U
                x0, y0 = self.screen[i]
                x1, y1 = self.screen[j]
                c.create_line(x0, y0, x1, y1, fill="#555" if hot else "#c4c8cc",
                              width=2 if hot else 1, arrow="last", arrowshape=(7, 8, 3))

        def dot(i, r, fill, outline, width=1):
            x, y = self.screen[i]
            c.create_oval(x - r, y - r, x + r, y + r, fill=fill, outline=outline, width=width)

        # normal nodes, then highlighted, then current
        for i, n in enumerate(nodes):
            if i not in U and i != self.cur:
                dot(i, 3, COL_OTHER, "#1565c0" if n["water"] else "#8a8e94")
        for i in sorted(U):
            if i == self.cur:
                continue
            col = COL_LG if (i in L and i in G) else (COL_L if i in L else COL_G)
            dot(i, 5, col, "#1565c0" if nodes[i]["water"] else "#222")
        for i in verso:
            if 0 <= i < len(nodes):
                dot(i, 9, "", COL_VERSO, 2)
        if 0 <= self.cur < len(nodes):
            dot(self.cur, 8, COL_CUR, "#7f0000", 2)

        # labels
        show_all = self.num_var.get()
        base = nodes[self.cur]["fdist"] if 0 <= self.cur < len(nodes) else 0
        for i, n in enumerate(nodes):
            if show_all or i in U or i == self.cur or i in verso:
                lab = str(i)
                if self.fd_var.get() and (i in U or i == self.cur):
                    lab += " (%+d)" % (n["fdist"] - base)
                x, y = self.screen[i]
                c.create_text(x + 8, y - 8, text=lab, anchor="sw", font=("Arial", 8),
                              fill="#111" if (i in U or i == self.cur) else "#666")

        # legend
        y = 10
        for col, txt in ((COL_CUR, "current node"), (COL_L, "L local"), (COL_G, "G global"),
                         (COL_LG, "LG both"), (COL_OTHER, "other nodes")):
            c.create_oval(10, y, 20, y + 10, fill=col, outline="#444")
            c.create_text(26, y + 5, text=txt, anchor="w", font=("Arial", 9), fill="#222")
            y += 16
        c.create_oval(10, y, 20, y + 10, outline=COL_VERSO, width=2)
        c.create_text(26, y + 5, text="direction", anchor="w", font=("Arial", 9), fill="#222")
        c.create_text(c.winfo_width() - 8, c.winfo_height() - 6, anchor="se", fill="#888", font=("Arial", 8),
                      text="wheel = zoom, drag = pan, click = select node")

    # ----- mouse interaction -----
    def nearest(self, x, y, maxd=12):
        best, bd = None, maxd * maxd
        for i, (sx, sy) in enumerate(self.screen):
            d = (sx - x) ** 2 + (sy - y) ** 2
            if d <= bd:
                best, bd = i, d
        return best

    def on_press(self, e):
        s, ox, oy = self.view
        self._press = (e.x, e.y, ox, oy, False)

    def on_drag(self, e):
        if not self._press:
            return
        x0, y0, ox, oy, moved = self._press
        if not moved and abs(e.x - x0) + abs(e.y - y0) < 4:
            return
        self._press = (x0, y0, ox, oy, True)
        s = self.view[0]
        self.view = (s, ox + e.x - x0, oy + e.y - y0)
        self.redraw()

    def on_release(self, e):
        p, self._press = self._press, None
        if not p or p[4] or not self.nodes:
            return
        i = self.nearest(e.x, e.y)
        if i is not None:
            self.cur = i
            self.recompute()

    def on_hover(self, e):
        if not self.nodes or self._press:
            return
        i = self.nearest(e.x, e.y)
        if i is None:
            return
        n, sim = self.nodes[i], self.sim
        tags = []
        if sim:
            if i in sim["L"]:
                tags.append("L steps " + ",".join("%+d" % p for p in sorted(sim["walk"][i])))
            if i in sim["G"] and i in sim["stepsG"]:
                tags.append("G step %+d" % sim["stepsG"][i])
        fmt = lambda v: "-" if v is None else str(v)
        self.status_var.set("Node %d | fdist %d | priority %d | %s | next %s,%s | prev %s,%s%s" % (
            i, n["fdist"], n["prio"], "water" if n["water"] else "land",
            fmt(n["next"][0]), fmt(n["next"][1]), fmt(n["prev"][0]), fmt(n["prev"][1]),
            ("  |  " + "  ".join(tags)) if tags else ""))

    def zoom(self, x, y, f):
        s, ox, oy = self.view
        ns = min(max(s * f, 1e-4), 1e4)
        f = ns / s
        self.view = (ns, x - (x - ox) * f, y - (y - oy) * f)
        self.redraw()

    def on_wheel(self, e):
        self.zoom(e.x, e.y, 1.2 if e.delta > 0 else 1 / 1.2)


if __name__ == "__main__":
    App().mainloop()