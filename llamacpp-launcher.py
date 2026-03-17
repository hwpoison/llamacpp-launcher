import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import json, os, re, subprocess, platform
from datetime import datetime

# ── Persistencia ──────────────────────────────────────────────────────────────
DATA_FILE = os.path.join(os.path.expanduser("~"), ".llama_launcher.json")
DEFAULT_DATA = {
    "bin_path": "", "gguf_path": "", "commands": [],
    "params": {"ngl": "", "ctx": "", "temp": "", "threads": "", "n": "", "reasoning": "auto"},
    "params_enabled": {"ngl": False, "ctx": False, "temp": False, "threads": False, "n": False, "reasoning": False},
    "help_cache": {},   # {"/path/to/binary": [{"flag": "--foo", "desc": "..."}]}
}

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in DEFAULT_DATA.items():
                    if isinstance(v, dict):
                        data.setdefault(k, {})
                        for kk, vv in v.items():
                            data[k].setdefault(kk, vv)
                    else:
                        data.setdefault(k, v)
                return data
        except Exception:
            pass
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_DATA.items()}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── Terminal ───────────────────────────────────────────────────────────────────
def run_in_terminal(bin_path: str, command: str):
    cwd    = bin_path if bin_path and os.path.isdir(bin_path) else None
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(
                f'cmd.exe /k {command}',
                cwd=cwd,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                shell=False
            )
        elif system == "Darwin":
            cd_part = f"cd '{cwd}' && " if cwd else ""
            osa = f'tell application "Terminal" to do script "{cd_part}{command}"'
            subprocess.Popen(["osascript", "-e", osa])
        else:
            cd_part = f"cd '{cwd}' && " if cwd else ""
            inner   = f"{cd_part}{command}; exec bash"
            for _t, args in [
                ("gnome-terminal", ["gnome-terminal", "--", "bash", "-c", inner]),
                ("konsole",        ["konsole",        "-e",  "bash", "-c", inner]),
                ("xfce4-terminal", ["xfce4-terminal", "-e",  f'bash -c "{inner}"']),
                ("xterm",          ["xterm",          "-e",  f'bash -c "{inner}"']),
            ]:
                try:
                    subprocess.Popen(args); return
                except FileNotFoundError:
                    continue
            raise RuntimeError("No terminal emulators found.")
    except Exception:
        raise

# ── Command building ──────────────────────────────────────────────────────────
def inject_flag(cmd: str, flag: str, value: str) -> str:
    """Replace <flag> <val> if it exists, otherwise append. Lambda prevents backslash interpretation."""
    pattern = rf'{re.escape(flag)}\s+\S+'
    repl    = f'{flag} {value}'
    if re.search(pattern, cmd):
        return re.sub(pattern, lambda _: repl, cmd)
    return cmd.rstrip() + f' {flag} {value}'

def inject_model_arg(cmd: str, gguf_full_path: str) -> str:
    pattern = r'-m\s+(?:"[^"]*"|\'[^\']*\'|\S+)'
    repl    = f'-m "{gguf_full_path}"'
    if re.search(pattern, cmd):
        return re.sub(pattern, lambda _: repl, cmd)
    return cmd.rstrip() + f' -m "{gguf_full_path}"'

def build_final_cmd(base_cmd: str, gguf_path: str, params: dict, params_enabled: dict) -> str:
    cmd = base_cmd
    if gguf_path:
        cmd = inject_model_arg(cmd, gguf_path)
    flag_map = {"ngl": "-ngl", "ctx": "-c", "temp": "--temp", "threads": "-t", "n": "-n"}
    for key, flag in flag_map.items():
        if params_enabled.get(key) and params.get(key, "").strip():
            cmd = inject_flag(cmd, flag, params[key].strip())
    # Reasoning param (string value: on/off/auto)
    if params_enabled.get("reasoning") and params.get("reasoning", "").strip():
        cmd = inject_flag(cmd, "--reasoning", params["reasoning"].strip())
    return cmd

# ── Binary --help parser ─────────────────────────────────────────────────────
def parse_help_flags(bin_path: str, binary: str) -> list:
    """Run binary --help and return ALL flag variants found (short and long).
    Each entry: {"flag": str, "desc": str, "aliases": [str]}.
    Example line:   -t,  --threads N   number of CPU threads
    Yields both "-t" and "--threads" entries sharing the same desc.
    """
    exe = binary.strip()
    if not exe:
        return []
    if bin_path and os.path.isdir(bin_path):
        candidate = os.path.join(bin_path, exe)
        if os.path.isfile(candidate):
            exe = candidate

    try:
        result = subprocess.run(
            [exe, "--help"],
            capture_output=True, text=True,
            timeout=10, cwd=bin_path if bin_path and os.path.isdir(bin_path) else None
        )
        output = result.stdout + result.stderr
    except Exception:
        return []

    flags = []
    seen  = set()

    # Match a flag-group line: 1-8 spaces, then flag tokens, then 2+ spaces, then desc
    # Flag tokens look like: "-t", "--threads", "-ngl", "--n-gpu-layers", etc.
    # They may be comma/space separated before the description
    line_pat = re.compile(
        r'^\s{0,10}'           # leading indent
        r'((?:-{1,2}[\w][\w\-]*'  # first flag token
        r'(?:[,\s]+(?:-{1,2}[\w][\w\-]*))*'  # optional extra flag tokens
        r'(?:\s+[<\[A-Z][^\s]{0,20})?'  # optional meta-var like <N>, [on|off], N
        r')\s{2,}'             # separator (2+ spaces)
        r'(.+)',               # description
        re.MULTILINE
    )

    for m in line_pat.finditer(output):
        raw  = m.group(1).strip()
        desc = m.group(2).strip()
        # Extract all flag tokens from the raw group (tokens starting with -)
        tokens = [t.rstrip(",") for t in re.split(r'[\s,]+', raw)
                  if t.startswith("-") and re.match(r'-{1,2}[\w]', t)]
        if not tokens:
            continue
        # Build aliases list (all tokens for this line)
        aliases = [t for t in tokens if re.match(r'-{1,2}[\w][\w\-]*$', t)]
        for token in aliases:
            if token not in seen:
                seen.add(token)
                flags.append({"flag": token, "desc": desc, "aliases": aliases})

    return flags


# ── GGUF helpers ──────────────────────────────────────────────────────────────
def fmt_size(path: str) -> str:
    try:
        b = os.path.getsize(path)
        for unit in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f}{unit}"
            b /= 1024
        return f"{b:.1f}TB"
    except Exception:
        return "?"

def find_gguf_files(folder: str):
    if not folder or not os.path.isdir(folder):
        return []
    try:
        return sorted(f for f in os.listdir(folder) if f.lower().endswith(".gguf"))
    except PermissionError:
        return []

# ── Paleta ────────────────────────────────────────────────────────────────────
IS_WIN    = platform.system() == "Windows"
BG        = "#0e0e0f"
BG2       = "#18181b"
BG3       = "#27272a"
ACCENT    = "#f97316"
ACCENT2   = "#fb923c"
FG        = "#f4f4f5"
FG2       = "#a1a1aa"
FG_DIM    = "#52525b"
GREEN     = "#4ade80"
YELLOW    = "#fbbf24"
RED       = "#f87171"
CYAN      = "#67e8f9"
BORDER    = "#3f3f46"
SEL_BG    = "#292524"
FONT_MONO = ("Consolas", 9)  if IS_WIN else ("Menlo", 9)
FONT_UI   = ("Segoe UI", 10) if IS_WIN else ("Helvetica Neue", 10)
FONT_TINY = ("Segoe UI", 8)  if IS_WIN else ("Helvetica Neue", 8)
FONT_LOG  = ("Consolas", 9)  if IS_WIN else ("Menlo", 9)

# ── App ────────────────────────────────────────────────────────────────────────
class LlamaLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.data            = load_data()
        self._selected_index = None
        self._selected_gguf  = None
        self._gguf_scan_job  = None
        self._preview_job    = None
        self._all_gguf_files  = []   # full list for filter
        self._help_cache      = {}   # {binary_name: [{"flag","desc"}]}
        self._ac_popup        = None  # active autocomplete Toplevel
        self._ac_job          = None  # after() id for debounce

        self.title("LlamaCPP Launcher")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(1020, 680)

        self._apply_style()
        self._build_ui()
        self._refresh_list()
        self._refresh_gguf_list()

        if self.data["commands"]:
            self.listbox.selection_set(0)
            self._on_select()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Warm in-memory cache from persisted data
        self._help_cache = {k: v for k, v in self.data.get("help_cache", {}).items()}
        self.log("info", "Launcher started  v4")
        self.log("info", f"Config: {DATA_FILE}")
        if self._help_cache:
            self.log("info", f"Flag cache: {list(self._help_cache.keys())}")

    # ── Estilos ───────────────────────────────────────────────────────────────
    def _apply_style(self):
        s = ttk.Style(self)
        s.theme_use("default")
        s.configure("TFrame",      background=BG)
        s.configure("Card.TFrame", background=BG2)
        s.configure("Params.TFrame", background=BG2)
        s.configure("TLabel",      background=BG,  foreground=FG,     font=FONT_UI)
        s.configure("Sub.TLabel",  background=BG2, foreground=FG2,    font=FONT_UI)
        s.configure("Par.TLabel",  background=BG2, foreground=FG2,    font=FONT_TINY)
        s.configure("Dim.TLabel",  background=BG,  foreground=FG_DIM, font=FONT_TINY)
        s.configure("Cap.TLabel",  background=BG,  foreground=FG2,
                    font=(FONT_UI[0], 8, "bold") if IS_WIN else (FONT_UI[0], 9, "bold"))
        s.configure("Head.TLabel", background=BG, foreground=ACCENT,
                    font=(FONT_UI[0], 13, "bold"))
        s.configure("TCheckbutton", background=BG2, foreground=FG2,
                    font=FONT_TINY, focuscolor=BG2)
        s.map("TCheckbutton", background=[("active", BG2)])
        for nm, bg_c, fg_c in [("Run", ACCENT, BG), ("Sec", BG3, FG), ("Del", BG3, RED)]:
            s.configure(f"{nm}.TButton",
                background=bg_c, foreground=fg_c, font=FONT_UI,
                borderwidth=0, focuscolor=bg_c, padding=(10, 6))
        s.map("Run.TButton", background=[("active", ACCENT2),   ("pressed", "#ea6c0a")])
        s.map("Sec.TButton", background=[("active", BORDER),    ("pressed", BG2)])
        s.map("Del.TButton", background=[("active", "#3f1515"), ("pressed", "#2a0a0a")])
        s.configure("TSpinbox",
            fieldbackground=BG3, foreground=FG, insertcolor=FG,
            arrowcolor=FG2, bordercolor=BORDER, font=FONT_MONO)

    # ── Layout ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # Row 0: Header
        hdr = ttk.Frame(self, padding=(18, 14, 18, 0))
        hdr.grid(row=0, column=0, sticky="ew")
        ttk.Label(hdr, text="🦙 LlamaCPP Launcher", style="Head.TLabel").pack(side="left")

        # Row 1: Body (3 columns)
        body = ttk.Frame(self, padding=(18, 12, 18, 8))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1, minsize=170)
        body.columnconfigure(2, weight=3, minsize=300)
        body.columnconfigure(4, weight=1, minsize=220)
        body.rowconfigure(1, weight=1)

        self._build_commands_panel(body)
        tk.Frame(body, bg=BORDER, width=1).grid(row=0, column=1, rowspan=3, sticky="ns", padx=12)
        self._build_editor_panel(body)
        tk.Frame(body, bg=BORDER, width=1).grid(row=0, column=3, rowspan=3, sticky="ns", padx=12)
        self._build_gguf_panel(body)

        # Row 2: sep
        tk.Frame(self, bg=BORDER, height=1).grid(row=2, column=0, sticky="ew", padx=18)

        # Row 3: Quick params
        self._build_params_panel()

        # Row 4: sep
        tk.Frame(self, bg=BORDER, height=1).grid(row=4, column=0, sticky="ew", padx=18)

        # Row 5: Preview
        self._build_preview_panel()

        # Row 6: sep
        tk.Frame(self, bg=BORDER, height=1).grid(row=6, column=0, sticky="ew", padx=18)

        # Row 7: Footer paths
        self._build_footer()

        # Row 8: sep
        tk.Frame(self, bg=BORDER, height=1).grid(row=8, column=0, sticky="ew", padx=18)

        # Row 9: Log
        self._build_log_panel()

    # ── Panel: comandos ───────────────────────────────────────────────────────
    def _build_commands_panel(self, parent):
        ttk.Label(parent, text="COMMANDS", style="Cap.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4))

        lf = tk.Frame(parent, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
        lf.grid(row=1, column=0, sticky="nsew")
        lf.rowconfigure(0, weight=1); lf.columnconfigure(0, weight=1)

        self.listbox = tk.Listbox(
            lf, bg=BG2, fg=FG, selectbackground=SEL_BG, selectforeground=ACCENT,
            activestyle="none", relief="flat", bd=0, font=FONT_UI,
            highlightthickness=0, cursor="hand2")
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sb = tk.Scrollbar(lf, orient="vertical", command=self.listbox.yview,
                          bg=BG3, troughcolor=BG2, width=8)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self._on_select())
        self.listbox.bind("<Double-Button-1>",  lambda e: self._run_command())

        br = ttk.Frame(parent)
        br.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(br, text="+ New",    style="Sec.TButton", command=self._new_command).pack(side="left", padx=(0, 3))
        ttk.Button(br, text="⧉ Duplicate", style="Sec.TButton", command=self._duplicate_command).pack(side="left", padx=(0, 3))
        ttk.Button(br, text="Delete",     style="Del.TButton", command=self._delete_command).pack(side="left")

    # ── Panel: editor ─────────────────────────────────────────────────────────
    def _build_editor_panel(self, parent):
        ttk.Label(parent, text="EDITOR", style="Cap.TLabel").grid(
            row=0, column=2, sticky="w", pady=(0, 4))

        editor = ttk.Frame(parent, style="Card.TFrame", padding=14)
        editor.grid(row=1, column=2, sticky="nsew")
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(3, weight=1)

        ttk.Label(editor, text="Name", style="Sub.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 3))
        self.name_var = tk.StringVar()
        self.name_entry = tk.Entry(
            editor, textvariable=self.name_var, bg=BG3, fg=FG,
            insertbackground=FG, relief="flat", bd=0, font=FONT_UI,
            highlightbackground=BORDER, highlightthickness=1)
        self.name_entry.grid(row=1, column=0, sticky="ew", pady=(0, 10), ipady=6, padx=1)

        ttk.Label(editor, text="Base command", style="Sub.TLabel").grid(
            row=2, column=0, sticky="w", pady=(0, 3))
        self.cmd_text = tk.Text(
            editor, bg=BG3, fg=FG, insertbackground=FG,
            relief="flat", bd=0, font=FONT_MONO, wrap="word", undo=True,
            highlightbackground=BORDER, highlightthickness=1, padx=8, pady=8)
        self.cmd_text.grid(row=3, column=0, sticky="nsew", pady=(0, 10), padx=1)
        self.cmd_text.bind("<KeyRelease>",  self._on_cmd_keyrelease)
        self.cmd_text.bind("<FocusOut>",    lambda e: self._hide_autocomplete())
        self.cmd_text.bind("<Escape>",      lambda e: self._hide_autocomplete())

        eb = ttk.Frame(editor, style="Card.TFrame")
        eb.grid(row=4, column=0, sticky="ew")
        ttk.Button(eb, text="Save", style="Sec.TButton",
                   command=self._save_command).pack(side="left", padx=(0, 8))
        ttk.Button(eb, text="▶  Run", style="Run.TButton",
                   command=self._run_command).pack(side="right")

    # ── Panel: GGUF ───────────────────────────────────────────────────────────
    def _build_gguf_panel(self, parent):
        ttk.Label(parent, text=".GGUF MODELS", style="Cap.TLabel").grid(
            row=0, column=4, sticky="w", pady=(0, 4))

        # Filtro
        filter_frame = tk.Frame(parent, bg=BG)
        filter_frame.grid(row=0, column=4, sticky="e", pady=(0, 4))
        self.gguf_filter_var = tk.StringVar()
        fe = tk.Entry(filter_frame, textvariable=self.gguf_filter_var,
                      bg=BG3, fg=FG2, insertbackground=FG,
                      relief="flat", bd=0, font=FONT_TINY,
                      highlightbackground=BORDER, highlightthickness=1, width=16)
        fe.pack(side="left", ipady=3, padx=1)
        fe.insert(0, "Search…")
        fe.bind("<FocusIn>",  lambda e: fe.delete(0, "end") if fe.get() == "Search…" else None)
        fe.bind("<FocusOut>", lambda e: fe.insert(0, "Search…") if not fe.get() else None)
        self.gguf_filter_var.trace_add("write", lambda *_: self._apply_gguf_filter())

        gf = tk.Frame(parent, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
        gf.grid(row=1, column=4, sticky="nsew")
        gf.rowconfigure(0, weight=1); gf.columnconfigure(0, weight=1)

        self.gguf_listbox = tk.Listbox(
            gf, bg=BG2, fg=FG, selectbackground=SEL_BG, selectforeground=ACCENT,
            activestyle="none", relief="flat", bd=0, font=FONT_MONO,
            highlightthickness=0, cursor="hand2")
        self.gguf_listbox.grid(row=0, column=0, sticky="nsew")
        sb2 = tk.Scrollbar(gf, orient="vertical", command=self.gguf_listbox.yview,
                           bg=BG3, troughcolor=BG2, width=8)
        sb2.grid(row=0, column=1, sticky="ns")
        self.gguf_listbox.configure(yscrollcommand=sb2.set)
        self.gguf_listbox.bind("<<ListboxSelect>>", lambda e: self._on_gguf_select())

        self.gguf_status = ttk.Label(parent, text="No models", style="Dim.TLabel")
        self.gguf_status.grid(row=2, column=4, sticky="w", pady=(4, 0))

    # ── Quick params ──────────────────────────────────────────────────────────
    def _build_params_panel(self):
        outer = ttk.Frame(self, style="Params.TFrame", padding=(18, 8, 18, 8))
        outer.grid(row=3, column=0, sticky="ew")

        ttk.Label(outer, text="QUICK PARAMS", style="Cap.TLabel",
                  background=BG2).grid(row=0, column=0, sticky="w", padx=(0, 16))

        # (key, flag, label, tooltip, from_, to_, inc, width)
        PARAMS = [
            ("ngl",     "-ngl",   "GPU Layers",  "Layers on GPU (0=CPU only)", 0,   512,    1,    5),
            ("ctx",     "-c",     "Context",     "Context window size",         512, 131072, 512,  7),
            ("temp",    "--temp", "Temperature", "0=deterministic, >1=chaotic", 0.0, 2.0,    0.05, 5),
            ("threads", "-t",     "Threads",     "CPU threads",                 1,   64,     1,    4),
            ("n",       "-n",     "Max tokens",  "-1 = unlimited",             -1,   32768,  64,   6),
        ]

        self._param_enabled = {}
        self._param_vars    = {}

        for col, (key, flag, label, tip, frm, to, inc, w) in enumerate(PARAMS):
            saved_enabled = self.data["params_enabled"].get(key, False)
            saved_val     = self.data["params"].get(key, "")

            en_var  = tk.BooleanVar(value=saved_enabled)
            val_var = tk.StringVar(value=saved_val if saved_val else str(frm))
            self._param_enabled[key] = en_var
            self._param_vars[key]    = val_var

            frame = tk.Frame(outer, bg=BG2)
            frame.grid(row=0, column=col + 1, padx=(0, 14), sticky="w")

            cb = tk.Checkbutton(frame, text=label, variable=en_var,
                                bg=BG2, fg=FG2, activebackground=BG2,
                                activeforeground=FG, selectcolor=BG3,
                                font=FONT_TINY, cursor="hand2",
                                command=self._on_param_change)
            cb.pack(anchor="w")

            spin = tk.Spinbox(
                frame, textvariable=val_var,
                from_=frm, to=to, increment=inc, width=w,
                bg=BG3, fg=FG, buttonbackground=BG3,
                insertbackground=FG, relief="flat",
                font=FONT_MONO, highlightbackground=BORDER, highlightthickness=1)
            spin.pack(anchor="w")
            # Trace the StringVar directly — Spinbox <<Increment>>/<<Decrement>> fire
            # BEFORE the var updates, so binding those events misses the new value.
            val_var.trace_add("write", lambda *_, k=key: self._on_param_change())

            ttk.Label(frame, text=tip, style="Par.TLabel",
                      background=BG2).pack(anchor="w")

        # ── Reasoning dropdown ──
        rea_col = len(PARAMS) + 1
        rea_frame = tk.Frame(outer, bg=BG2)
        rea_frame.grid(row=0, column=rea_col, padx=(0, 14), sticky="w")

        rea_en_var  = tk.BooleanVar(value=self.data["params_enabled"].get("reasoning", False))
        rea_val_var = tk.StringVar(value=self.data["params"].get("reasoning", "auto"))
        self._param_enabled["reasoning"] = rea_en_var
        self._param_vars["reasoning"]    = rea_val_var

        tk.Checkbutton(rea_frame, text="Reasoning", variable=rea_en_var,
                       bg=BG2, fg=FG2, activebackground=BG2,
                       activeforeground=FG, selectcolor=BG3,
                       font=FONT_TINY, cursor="hand2",
                       command=self._on_param_change).pack(anchor="w")

        rea_menu = tk.OptionMenu(rea_frame, rea_val_var, "auto", "on", "off")
        rea_menu.config(bg=BG3, fg=FG, activebackground=BORDER, activeforeground=FG,
                        relief="flat", bd=0, font=FONT_MONO,
                        highlightbackground=BORDER, highlightthickness=1,
                        indicatoron=True, width=4)
        rea_menu["menu"].config(bg=BG3, fg=FG, activebackground=SEL_BG,
                                activeforeground=ACCENT, font=FONT_MONO)
        rea_menu.pack(anchor="w")
        rea_val_var.trace_add("write", lambda *_: self._on_param_change())

        ttk.Label(rea_frame, text="--reasoning", style="Par.TLabel",
                  background=BG2).pack(anchor="w")

        # ── Help / autocomplete fetch button ──
        help_col = rea_col + 1
        help_frame = tk.Frame(outer, bg=BG2)
        help_frame.grid(row=0, column=help_col, padx=(8, 0), sticky="ne")
        ttk.Button(help_frame, text="⟳ Load flags", style="Sec.TButton",
                   command=self._fetch_help_flags).pack(anchor="w", pady=(0, 4))
        self.help_status_var = tk.StringVar(value="")
        ttk.Label(help_frame, textvariable=self.help_status_var,
                  style="Dim.TLabel").pack(anchor="w")

        # Reset button
        reset_btn = ttk.Button(outer, text="Reset params", style="Sec.TButton",
                               command=self._reset_params)
        reset_btn.grid(row=0, column=help_col + 1, padx=(10, 0), sticky="e")
        outer.columnconfigure(help_col + 1, weight=1)

    # ── Preview ───────────────────────────────────────────────────────────────
    def _build_preview_panel(self):
        pf = ttk.Frame(self, padding=(18, 6, 18, 6))
        pf.grid(row=5, column=0, sticky="ew")
        pf.columnconfigure(1, weight=1)

        ttk.Label(pf, text="PREVIEW", style="Cap.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10))

        preview_box = tk.Frame(pf, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
        preview_box.grid(row=0, column=1, sticky="ew")
        preview_box.columnconfigure(0, weight=1)

        self.preview_text = tk.Text(
            preview_box, bg=BG2, fg=CYAN,
            relief="flat", bd=0, font=FONT_MONO,
            height=2, wrap="none", state="disabled",
            highlightthickness=0, padx=10, pady=6)
        self.preview_text.grid(row=0, column=0, sticky="ew")

        prev_sb = tk.Scrollbar(preview_box, orient="horizontal",
                               command=self.preview_text.xview,
                               bg=BG3, troughcolor=BG2, width=6)
        prev_sb.grid(row=1, column=0, sticky="ew")
        self.preview_text.configure(xscrollcommand=prev_sb.set)

        ttk.Button(pf, text="↑ Use as base", style="Sec.TButton",
                   command=self._use_preview_as_base).grid(row=0, column=2, padx=(10, 0))

    # ── Footer: paths ─────────────────────────────────────────────────────────
    def _build_footer(self):
        f = ttk.Frame(self, padding=(18, 10, 18, 8))
        f.grid(row=7, column=0, sticky="ew")
        f.columnconfigure(1, weight=1)
        f.columnconfigure(4, weight=1)

        ttk.Label(f, text="Binaries:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.bin_var = tk.StringVar(value=self.data["bin_path"])
        tk.Entry(f, textvariable=self.bin_var, bg=BG3, fg=FG, insertbackground=FG,
                 relief="flat", bd=0, font=FONT_MONO,
                 highlightbackground=BORDER, highlightthickness=1
                 ).grid(row=0, column=1, sticky="ew", ipady=5, padx=1)
        self.bin_var.trace_add("write", lambda *_: self._autosave("bin_path", self.bin_var))
        ttk.Button(f, text="...", style="Sec.TButton", width=3,
                   command=self._browse_bin).grid(row=0, column=2, padx=(6, 20))

        ttk.Label(f, text="Models:").grid(row=0, column=3, sticky="w", padx=(0, 8))
        self.gguf_dir_var = tk.StringVar(value=self.data["gguf_path"])
        tk.Entry(f, textvariable=self.gguf_dir_var, bg=BG3, fg=FG, insertbackground=FG,
                 relief="flat", bd=0, font=FONT_MONO,
                 highlightbackground=BORDER, highlightthickness=1
                 ).grid(row=0, column=4, sticky="ew", ipady=5, padx=1)
        self.gguf_dir_var.trace_add("write", lambda *_: (
            self._autosave("gguf_path", self.gguf_dir_var),
            self._schedule_gguf_scan()
        ))
        ttk.Button(f, text="...", style="Sec.TButton", width=3,
                   command=self._browse_gguf_dir).grid(row=0, column=5, padx=(6, 0))

    # ── Log panel ─────────────────────────────────────────────────────────────
    def _build_log_panel(self):
        lf = ttk.Frame(self, padding=(18, 6, 18, 14))
        lf.grid(row=9, column=0, sticky="ew")
        lf.columnconfigure(0, weight=1)

        hdr = ttk.Frame(lf)
        hdr.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(hdr, text="DEBUG LOG", style="Cap.TLabel").pack(side="left")
        ttk.Button(hdr, text="Clear", style="Sec.TButton",
                   command=self._clear_log).pack(side="right")

        log_box = tk.Frame(lf, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
        log_box.grid(row=1, column=0, sticky="ew")
        log_box.columnconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_box, bg=BG2, fg=FG2,
            relief="flat", bd=0, font=FONT_LOG,
            height=5, wrap="none", state="disabled",
            highlightthickness=0, padx=10, pady=6)
        self.log_text.grid(row=0, column=0, sticky="ew")

        lsb = tk.Scrollbar(log_box, orient="horizontal", command=self.log_text.xview,
                           bg=BG3, troughcolor=BG2, width=8)
        lsb.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(xscrollcommand=lsb.set)

        self.log_text.tag_configure("ts",   foreground=FG_DIM)
        self.log_text.tag_configure("info", foreground=FG2)
        self.log_text.tag_configure("ok",   foreground=GREEN)
        self.log_text.tag_configure("warn", foreground=YELLOW)
        self.log_text.tag_configure("err",  foreground=RED)
        self.log_text.tag_configure("cmd",  foreground=ACCENT)

    # ── Logging ───────────────────────────────────────────────────────────────
    def log(self, level: str, msg: str):
        ts     = datetime.now().strftime("%H:%M:%S")
        prefix = {"info": "·", "ok": "✓", "warn": "!", "err": "✗", "cmd": "▶"}.get(level, "·")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] ", "ts")
        self.log_text.insert("end", f"{prefix} {msg}\n", level)
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.log("info", "Log cleared")

    # ── Preview ───────────────────────────────────────────────────────────────
    def _schedule_preview(self):
        if self._preview_job:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(150, self._update_preview)

    def _update_preview(self):
        try:
            base  = self.cmd_text.get("1.0", "end").strip()
            p     = {k: v.get() for k, v in self._param_vars.items()}
            pe    = {k: v.get() for k, v in self._param_enabled.items()}
            final = build_final_cmd(base, self._selected_gguf or "", p, pe)
            self.preview_text.configure(state="normal")
            self.preview_text.delete("1.0", "end")
            self.preview_text.insert("1.0", final)
            self.preview_text.configure(state="disabled")
        except Exception as exc:
            print(f"[_update_preview error] {exc}")

    def _use_preview_as_base(self):
        """Copy preview into base command, stripping -m so it is injected fresh at run time."""
        final = self.preview_text.get("1.0", "end").strip()
        if not final:
            self.log("warn", "Preview is empty — nothing to transfer")
            return
        # Remove -m <path> before writing to base — model injection happens at run time
        final = re.sub(r'\s*-m\s+(?:"[^"]*"|\'[^\']*\'|\S+)', "", final).strip()
        self.cmd_text.delete("1.0", "end")
        self.cmd_text.insert("1.0", final)
        # Disable all quick params — they are now baked into the base command
        for v in self._param_enabled.values():
            v.set(False)
        self._selected_gguf = None
        self.gguf_listbox.selection_clear(0, "end")
        self._on_param_change()
        self._save_command()
        self.log("ok", "Preview transferred to base command; params & model cleared")

    # ── Params ────────────────────────────────────────────────────────────────
    def _on_param_change(self):
        try:
            for k, v in self._param_vars.items():
                self.data["params"][k] = v.get()
            for k, v in self._param_enabled.items():
                self.data["params_enabled"][k] = v.get()
            save_data(self.data)
            self._schedule_preview()
        except Exception as exc:
            # Never let this raise — a Tkinter trace that raises is silently removed,
            # which would permanently break the spinbox arrows.
            print(f"[_on_param_change error] {exc}")

    def _reset_params(self):
        for k, v in self._param_enabled.items():
            v.set(False)
        defaults = {"ngl": "0", "ctx": "4096", "temp": "0.8", "threads": "4", "n": "512", "reasoning": "auto"}
        for k, v in self._param_vars.items():
            v.set(defaults.get(k, ""))
        self._on_param_change()
        self.log("info", "Params reset")

    # ── Lógica: comandos ──────────────────────────────────────────────────────
    def _refresh_list(self):
        sel = self.listbox.curselection()
        self.listbox.delete(0, "end")
        for i, c in enumerate(self.data["commands"]):
            label = f"  {c['name']}" if c["name"] else f"  (unnamed {i+1})"
            self.listbox.insert("end", label)
        if sel:
            try: self.listbox.selection_set(sel[0])
            except Exception: pass

    def _on_select(self):
        sel = self.listbox.curselection()
        if not sel: return
        idx = sel[0]
        if idx >= len(self.data["commands"]): return
        self._selected_index = idx
        cmd = self.data["commands"][idx]
        self.name_var.set(cmd["name"])
        self.cmd_text.delete("1.0", "end")
        self.cmd_text.insert("1.0", cmd["cmd"])
        self.log("info", f"Selected: '{cmd['name']}'")
        self._schedule_preview()

    def _new_command(self):
        new = {"name": f"Command {len(self.data['commands'])+1}", "cmd": "llama-cli.exe "}
        self.data["commands"].append(new)
        save_data(self.data)
        self._refresh_list()
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(len(self.data["commands"]) - 1)
        self._on_select()
        self.name_entry.focus_set()
        self.name_entry.select_range(0, "end")
        self.log("info", f"New: '{new['name']}'")

    def _duplicate_command(self):
        if self._selected_index is None:
            self.log("warn", "Duplicate: no command selected")
            return
        src  = self.data["commands"][self._selected_index]
        copy = {"name": src["name"] + " (copy)", "cmd": src["cmd"]}
        self.data["commands"].append(copy)
        save_data(self.data)
        self._refresh_list()
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(len(self.data["commands"]) - 1)
        self._on_select()
        self.log("ok", f"Duplicated: '{copy['name']}'")

    def _save_command(self):
        if self._selected_index is None: return
        name = self.name_var.get().strip()
        cmd  = self.cmd_text.get("1.0", "end").strip()
        if not cmd: return
        self.data["commands"][self._selected_index] = {"name": name, "cmd": cmd}
        save_data(self.data)
        self._refresh_list()
        self.listbox.selection_set(self._selected_index)
        self.log("ok", f"Saved: '{name}'")

    def _delete_command(self):
        if self._selected_index is None:
            self.log("warn", "Delete: no command selected"); return
        name = self.data["commands"][self._selected_index]["name"]
        if not messagebox.askyesno("Confirm", f"Delete '{name}'?"):
            self.log("info", "Delete cancelled"); return
        self.data["commands"].pop(self._selected_index)
        save_data(self.data)
        self._selected_index = None
        self._selected_gguf  = None
        self.name_var.set("")
        self.cmd_text.delete("1.0", "end")
        self._refresh_list()
        self.log("warn", f"Deleted: '{name}'")
        if self.data["commands"]:
            self.listbox.selection_set(0)
            self._on_select()

    def _run_command(self):
        self._save_command()
        if self._selected_index is None:
            self.log("err", "Run: no command selected"); return
        base_cmd = self.data["commands"][self._selected_index]["cmd"].strip()
        if not base_cmd:
            self.log("err", "Run: command is empty"); return

        p  = {k: v.get() for k, v in self._param_vars.items()}
        pe = {k: v.get() for k, v in self._param_enabled.items()}
        final_cmd = build_final_cmd(base_cmd, self._selected_gguf or "", p, pe)
        bin_path  = self.bin_var.get().strip()

        if self._selected_gguf:
            self.log("info", f"Model: {os.path.basename(self._selected_gguf)}")
        else:
            self.log("warn", "No GGUF model selected — running without -m")

        active_params = [f"{k}={p[k]}" for k, en in pe.items() if en and p.get(k)]
        if active_params:
            self.log("info", f"Active params: {', '.join(active_params)}")

        self.log("info", f"CWD: {bin_path or '(ninguno)'}")
        self.log("cmd",  f"CMD: {final_cmd}")

        try:
            run_in_terminal(bin_path, final_cmd)
            self.log("ok", "Terminal opened")
        except Exception as e:
            self.log("err", f"Error: {e}")

    # ── Lógica: GGUF ──────────────────────────────────────────────────────────
    def _refresh_gguf_list(self):
        folder = self.gguf_dir_var.get().strip() if hasattr(self, "gguf_dir_var") else self.data["gguf_path"]
        self._all_gguf_files = find_gguf_files(folder)
        self._apply_gguf_filter()
        if self._all_gguf_files:
            self.log("info", f"GGUF: {len(self._all_gguf_files)} archivos en '{folder}'")
        elif folder and os.path.isdir(folder):
            self.log("warn", f"GGUF: no .gguf files in '{folder}'")

    def _apply_gguf_filter(self):
        q = self.gguf_filter_var.get().strip().lower() if hasattr(self, "gguf_filter_var") else ""
        if q == "search…":
            q = ""
        folder  = self.gguf_dir_var.get().strip() if hasattr(self, "gguf_dir_var") else ""
        files   = [f for f in self._all_gguf_files if q in f.lower()] if q else self._all_gguf_files

        self.gguf_listbox.delete(0, "end")
        for fname in files:
            size = fmt_size(os.path.join(folder, fname)) if folder else ""
            self.gguf_listbox.insert("end", f"  {fname}  [{size}]")

        if files:
            self.gguf_status.config(text=f"{len(files)}/{len(self._all_gguf_files)} model(s)")
        elif folder and os.path.isdir(folder):
            self.gguf_status.config(text="No results")
        else:
            self.gguf_status.config(text="Folder not set")

    def _schedule_gguf_scan(self):
        if self._gguf_scan_job:
            self.after_cancel(self._gguf_scan_job)
        self._gguf_scan_job = self.after(400, self._refresh_gguf_list)

    def _on_gguf_select(self):
        sel = self.gguf_listbox.curselection()
        if not sel: return
        # El entry tiene "  nombre.gguf  [4.2GB]" — extraer solo el nombre
        raw      = self.gguf_listbox.get(sel[0]).strip()
        filename = re.sub(r'\s*\[.*?\]\s*$', '', raw).strip()
        gguf_dir = self.gguf_dir_var.get().strip()
        self._selected_gguf = os.path.join(gguf_dir, filename)
        self.log("ok", f"Model selected: {filename}")
        self._schedule_preview()

    # ── Autocomplete ──────────────────────────────────────────────────────────
    def _get_current_binary(self) -> str:
        """Extract the binary name (first token) from the command textbox."""
        line = self.cmd_text.get("1.0", "end").strip().split()[0] if self.cmd_text.get("1.0", "end").strip() else ""
        return os.path.basename(line) if line else ""

    def _fetch_help_flags(self):
        """Run binary --help and cache the parsed flags."""
        binary   = self._get_current_binary()
        bin_path = self.bin_var.get().strip()
        if not binary:
            self.log("warn", "Load flags: no binary found in command")
            self.help_status_var.set("No binary in command")
            return
        self.log("info", f"Parsing --help for: {binary}")
        self.help_status_var.set("Loading…")
        self.update_idletasks()
        flags = parse_help_flags(bin_path, binary)
        if flags:
            self._help_cache[binary] = flags
            self.data["help_cache"][binary] = flags
            save_data(self.data)
            msg = f"{len(flags)} flags loaded"
            self.log("ok", f"{binary}: {msg}")
            self.help_status_var.set(msg)
        else:
            self.log("warn", f"{binary}: no flags parsed (check binary path)")
            self.help_status_var.set("No flags found")

    def _on_cmd_keyrelease(self, event):
        self._schedule_preview()
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):
            return
        # Auto-load flags when a .exe binary is detected and not yet cached
        first_token = self.cmd_text.get("1.0", "end").strip().split()[0] if self.cmd_text.get("1.0", "end").strip() else ""
        binary = os.path.basename(first_token)
        if binary.lower().endswith(".exe") and binary not in self._help_cache:
            self.after(300, self._auto_fetch_if_needed)
        self._schedule_autocomplete()

    def _auto_fetch_if_needed(self):
        """Silently load flags for the current binary if not already cached."""
        first_token = self.cmd_text.get("1.0", "end").strip().split()[0] if self.cmd_text.get("1.0", "end").strip() else ""
        binary = os.path.basename(first_token)
        if binary and binary not in self._help_cache:
            self._fetch_help_flags()

    def _schedule_autocomplete(self):
        if self._ac_job:
            self.after_cancel(self._ac_job)
        self._ac_job = self.after(120, self._try_autocomplete)

    def _try_autocomplete(self):
        try:
            # Get text from start of current line up to cursor
            cursor_pos = self.cmd_text.index("insert")
            line_start = f"{cursor_pos.split('.')[0]}.0"
            text_before = self.cmd_text.get(line_start, cursor_pos)

            # Match any flag prefix: --long or -short (at least - + 1 char)
            m = re.search(r'(-{1,2}[\w][\w\-]*)$', text_before)
            if not m:
                self._hide_autocomplete()
                return
            prefix = m.group(1)
            # For single-dash require at least 2 chars (e.g. "-t"), double-dash at least 3
            min_len = 2 if prefix.startswith('--') else 2
            if len(prefix) < min_len:
                self._hide_autocomplete()
                return

            binary = self._get_current_binary()
            # Load from memory cache first, then persisted cache
            flags = self._help_cache.get(binary) or self.data.get("help_cache", {}).get(binary, [])
            if not flags:
                self._hide_autocomplete()
                return

            matches = [f for f in flags if f["flag"].startswith(prefix)]
            if not matches:
                self._hide_autocomplete()
                return

            self._show_autocomplete(matches, prefix)
        except Exception as exc:
            print(f"[autocomplete error] {exc}")

    def _show_autocomplete(self, matches: list, prefix: str):
        """Two-pane popup: flag list on the left, full description on the right."""
        try:
            bbox = self.cmd_text.bbox("insert")
            if not bbox:
                return
            x_root = self.cmd_text.winfo_rootx() + bbox[0]
            y_root = self.cmd_text.winfo_rooty() + bbox[1] + bbox[3] + 4
        except Exception:
            return

        MAX_VISIBLE = 12
        visible = matches[:MAX_VISIBLE]

        # ── (Re)create popup window ──
        if self._ac_popup and self._ac_popup.winfo_exists():
            pop = self._ac_popup
            pop.geometry(f"+{x_root}+{y_root}")
        else:
            pop = tk.Toplevel(self)
            pop.wm_overrideredirect(True)
            pop.configure(bg=BORDER)
            pop.geometry(f"+{x_root}+{y_root}")
            self._ac_popup = pop

        for w in pop.winfo_children():
            w.destroy()

        # ── Two-pane frame ──
        container = tk.Frame(pop, bg=BG2)
        container.pack(fill="both", expand=True, padx=1, pady=1)

        # Left: flag list
        list_frame = tk.Frame(container, bg=BG2)
        list_frame.pack(side="left", fill="y")

        lb = tk.Listbox(
            list_frame,
            bg=BG2, fg=FG, selectbackground=SEL_BG, selectforeground=ACCENT,
            activestyle="none", relief="flat", bd=0,
            font=FONT_MONO, highlightthickness=0,
            width=32, height=len(visible)
        )
        lb.pack(fill="both", expand=True, padx=(4, 0), pady=4)

        for item in visible:
            lb.insert("end", f"  {item['flag']}")

        lb.selection_set(0)

        # Divider
        tk.Frame(container, bg=BORDER, width=1).pack(side="left", fill="y", padx=4)

        # Right: description text
        desc_frame = tk.Frame(container, bg=BG2)
        desc_frame.pack(side="left", fill="both", expand=True)

        desc_text = tk.Text(
            desc_frame,
            bg=BG2, fg=FG2, relief="flat", bd=0,
            font=FONT_TINY, wrap="word",
            width=52, height=len(visible),
            highlightthickness=0, padx=8, pady=4,
            state="disabled", cursor="arrow"
        )
        desc_text.pack(fill="both", expand=True, pady=4)

        def update_desc(idx: int):
            desc_text.configure(state="normal")
            desc_text.delete("1.0", "end")
            desc_text.insert("1.0", visible[idx]["desc"])
            desc_text.configure(state="disabled")

        update_desc(0)

        # Footer: more results hint
        if len(matches) > MAX_VISIBLE:
            footer = tk.Frame(pop, bg=BG3)
            footer.pack(fill="x", padx=1, pady=(0, 1))
            tk.Label(footer,
                     text=f"  ↑↓ navigate  ·  {len(matches) - MAX_VISIBLE} more matches",
                     bg=BG3, fg=FG_DIM, font=FONT_TINY,
                     anchor="w").pack(fill="x", padx=4, pady=2)

        def on_select(event=None):
            sel = lb.curselection()
            if not sel:
                return
            self._insert_autocomplete(visible[sel[0]]["flag"], prefix)
            self._hide_autocomplete()

        def on_lb_select(event=None):
            sel = lb.curselection()
            if sel:
                update_desc(sel[0])

        lb.bind("<<ListboxSelect>>",  on_lb_select)
        lb.bind("<Double-Button-1>",  on_select)
        lb.bind("<Return>",           lambda e: on_select())
        lb.bind("<Escape>",           lambda e: (self._hide_autocomplete(), self.cmd_text.focus_set()))
        lb.bind("<FocusOut>",         lambda e: self.after(120, self._maybe_hide_popup))

        # Arrow-key navigation from cmd_text
        def cmd_text_arrow(event):
            if not (self._ac_popup and self._ac_popup.winfo_exists()):
                return
            cur = lb.curselection()
            idx = cur[0] if cur else 0
            if event.keysym == "Down":
                idx = min(idx + 1, len(visible) - 1)
            elif event.keysym == "Up":
                idx = max(idx - 1, 0)
            elif event.keysym in ("Return", "Tab"):
                on_select(); return "break"
            lb.selection_clear(0, "end")
            lb.selection_set(idx)
            lb.see(idx)
            update_desc(idx)
            return "break"

        self.cmd_text.bind("<Down>",   cmd_text_arrow, add="+")
        self.cmd_text.bind("<Up>",     cmd_text_arrow, add="+")
        self.cmd_text.bind("<Return>", cmd_text_arrow, add="+")
        self.cmd_text.bind("<Tab>",    cmd_text_arrow, add="+")

    def _insert_autocomplete(self, flag: str, prefix: str):
        """Replace the partial --prefix with the chosen flag in the textbox."""
        cursor_pos  = self.cmd_text.index("insert")
        line_no     = cursor_pos.split(".")[0]
        line_start  = f"{line_no}.0"
        line_text   = self.cmd_text.get(line_start, cursor_pos)
        # Find start of the prefix in this line
        start_char  = len(line_text) - len(prefix)
        delete_from = f"{line_no}.{start_char}"
        self.cmd_text.delete(delete_from, cursor_pos)
        self.cmd_text.insert(delete_from, flag + " ")
        self._schedule_preview()

    def _maybe_hide_popup(self):
        """Hide popup only if neither the popup nor cmd_text has focus."""
        try:
            focused = self.focus_get()
            if self._ac_popup and self._ac_popup.winfo_exists():
                if focused not in self._ac_popup.winfo_children() and focused is not self.cmd_text:
                    self._hide_autocomplete()
        except Exception:
            pass

    def _hide_autocomplete(self):
        if self._ac_popup:
            try:
                self._ac_popup.destroy()
            except Exception:
                pass
            self._ac_popup = None
        # Unbind transient arrow overrides
        for seq in ("<Down>", "<Up>", "<Return>"):
            try:
                self.cmd_text.unbind(seq)
            except Exception:
                pass
        # Re-bind preview trigger
        self.cmd_text.bind("<KeyRelease>", self._on_cmd_keyrelease)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _autosave(self, key, var):
        self.data[key] = var.get().strip()
        save_data(self.data)

    def _browse_bin(self):
        path = filedialog.askdirectory(title="llama.cpp binaries folder")
        if path:
            self.bin_var.set(path)
            self.log("info", f"Binaries: {path}")

    def _browse_gguf_dir(self):
        path = filedialog.askdirectory(title=".gguf models folder")
        if path:
            self.gguf_dir_var.set(path)
            self._refresh_gguf_list()
            self.log("info", f"Models: {path}")

    def _on_close(self):
        self._save_command()
        self.destroy()


if __name__ == "__main__":
    app = LlamaLauncher()
    app.mainloop()
