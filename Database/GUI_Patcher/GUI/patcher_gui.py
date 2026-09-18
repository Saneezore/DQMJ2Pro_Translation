#!/usr/bin/env python3
import contextlib
import os
import queue
import sys
import subprocess
import webbrowser
import threading
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    TKDND_AVAILABLE = True
except Exception:
    DND_FILES = None
    TkinterDnD = None
    TKDND_AVAILABLE = False

import gui_backend


def app_root():
    if getattr(sys, "frozen", False):
        return Path.cwd()
    return Path(__file__).resolve().parents[3]

ROOT = app_root()
PATCHER_VERSION = "1.3.2"

def open_url(url):
    if sys.platform.startswith("linux"):
        env = dict(os.environ)

        # AppImage/PyInstaller can poison launched desktop apps with bundled libs.
        for key in (
            "LD_LIBRARY_PATH",
            "PYTHONHOME",
            "PYTHONPATH",
            "APPDIR",
            "APPIMAGE",
            "ARGV0",
        ):
            env.pop(key, None)

        for cmd in (
            ["xdg-open", url],
            ["gio", "open", url],
        ):
            try:
                subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return
            except Exception:
                pass

    try:
        webbrowser.open(url)
    except Exception:
        pass



class ToolTip:
    def __init__(self, widget, text, wraplength=360):
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.tip = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _event=None):
        if self.tip or not self.text:
            return

        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6

        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        if sys.platform == "darwin":
            try:
                self.tip.wm_attributes("-topmost", True)
                self.tip.lift()
                self.tip.transient(self.widget.winfo_toplevel())
            except tk.TclError:
                pass
        self.tip.wm_geometry(f"+{x}+{y}")

        label_kwargs = {}
        if sys.platform == "darwin":
            label_kwargs["foreground"] = "black"

        label = tk.Label(
            self.tip,
            text=self.text,
            justify="left",
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=4,
            wraplength=self.wraplength,
            **label_kwargs,
        )
        label.pack()

    def hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def add_tooltip(widget, text):
    ToolTip(widget, text)
    return widget


def add_info_icon(parent, text):
    icon = ttk.Label(
        parent,
        text="ⓘ",
        cursor="question_arrow",
        foreground="blue",
    )
    add_tooltip(icon, text)
    return icon


def add_check_with_info(parent, label, variable, info):
    row = ttk.Frame(parent)
    row.pack(anchor="w", padx=10, pady=3)

    ttk.Checkbutton(row, text=label, variable=variable).pack(side="left")
    add_info_icon(row, info).pack(side="left", padx=(5, 0))

    return row


def add_value_option_with_info(parent, label, variable, value_var, width, info):
    row = ttk.Frame(parent)
    row.pack(anchor="w", padx=10, pady=3)

    ttk.Checkbutton(row, text=label, variable=variable).pack(side="left")
    ttk.Entry(row, textvariable=value_var, width=width).pack(side="left", padx=(6, 5))
    add_info_icon(row, info).pack(side="left")

    return row



def configure_linux_appimage_scaling(root):
    if not (sys.platform.startswith("linux") and getattr(sys, "frozen", False)):
        return

    def detect_font():
        # KDE: "Noto Sans,10,-1,5,50,0,0,0,0,0"
        for cmd in (
            ["kreadconfig6", "--file", "kdeglobals", "--group", "General", "--key", "font"],
            ["kreadconfig5", "--file", "kdeglobals", "--group", "General", "--key", "font"],
        ):
            try:
                out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
                if out:
                    parts = out.split(",")
                    if len(parts) >= 2:
                        return parts[0], int(float(parts[1]))
            except Exception:
                pass

        # GTK/GNOME: "'Noto Sans 10'"
        try:
            out = subprocess.check_output(
                ["gsettings", "get", "org.gnome.desktop.interface", "font-name"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip().strip("'")
            if out:
                name, size = out.rsplit(" ", 1)
                return name, int(float(size))
        except Exception:
            pass

        return None, 10

    try:
        # Avoid AppImage/KDE DPI blowups. Then set actual font sizes manually.
        root.tk.call("tk", "scaling", 1.0)

        family, size = detect_font()
        size = max(8, min(size + 4, 16))

        for name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkMenuFont",
            "TkCaptionFont",
            "TkSmallCaptionFont",
            "TkIconFont",
            "TkTooltipFont",
        ):
            try:
                f = tkfont.nametofont(name)
                if family:
                    f.configure(family=family)
                f.configure(size=size)
            except Exception:
                pass

        try:
            heading = tkfont.nametofont("TkHeadingFont")
            if family:
                heading.configure(family=family)
            heading.configure(size=size, weight="bold")
        except Exception:
            pass

        try:
            fixed = tkfont.nametofont("TkFixedFont")
            fixed.configure(size=size)
        except Exception:
            pass
    except Exception:
        pass


class QueueWriter:
    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(text)

    def flush(self):
        pass


class App((TkinterDnD.Tk if TKDND_AVAILABLE else tk.Tk)):
    def __init__(self):
        super().__init__()
        configure_linux_appimage_scaling(self)
        self.title(f"DQMJ2P Translation Patcher v{PATCHER_VERSION}")
        self.geometry("760x620")
        self.minsize(720, 560)

        self.log_queue = queue.Queue()

        self.rom_var = tk.StringVar()
        self.out_var = tk.StringVar(value=str(Path.home() / f"DQMJ2P_Eng_Patched_v{PATCHER_VERSION}.nds"))

        self.new_synths_var = tk.BooleanVar(value=True)
        self.postgame_pipit_vendor_var = tk.BooleanVar(value=True)
        self.xp_mult_var = tk.BooleanVar(value=False)
        self.xp_mult_value = tk.StringVar(value="2.0")
        self.full_reserve_exp_var = tk.BooleanVar(value=False)
        self.xvariant_var = tk.BooleanVar(value=True)
        self.gender_icons_var = tk.BooleanVar(value=True)
        self.scout_offense_var = tk.BooleanVar(value=False)
        self.scout_penalty_var = tk.BooleanVar(value=False)
        self.synth_level_var = tk.BooleanVar(value=False)
        self.synth_level_value = tk.StringVar(value="10")
        self.synth_polarity_var = tk.BooleanVar(value=False)

        self.randomizer_enabled_var = tk.BooleanVar(value=False)
        self.randomizer_monsters_var = tk.BooleanVar(value=True)
        self.randomizer_seed_value = tk.StringVar(value="0")
        self.randomizer_spoiler_var = tk.BooleanVar(value=True)
        self.randomizer_allow_flee_var = tk.BooleanVar(value=True)
        self.randomizer_xp_var = tk.BooleanVar(value=False)
        self.randomizer_stronger_var = tk.BooleanVar(value=False)
        self.randomizer_no_flee_var = tk.BooleanVar(value=False)
        self.randomizer_level_up_mode = tk.StringVar(value="none")
        self.randomizer_level_up_variance = tk.StringVar(value="140")
        self.randomizer_skill_points_mode = tk.StringVar(value="none")
        self.randomizer_skillsets_var = tk.BooleanVar(value=False)
        self.randomizer_generic_synthesis_var = tk.BooleanVar(value=False)

        self.randomizer_rank_vars = {
            rank: tk.BooleanVar(value=True)
            for rank in ("F", "E", "D", "C", "B", "A", "S", "SS")
        }
        self.randomizer_family_vars = {
            family: tk.BooleanVar(value=True)
            for family in ("Slime", "Dragon", "Nature", "Beast", "Material", "Demon", "Zombie", "???")
        }
        self.randomizer_size_vars = {
            size: tk.BooleanVar(value=True)
            for size in ("1", "2", "3")
        }

        self.show_log_var = tk.BooleanVar(value=False)

        self.build_ui()
        self.after(100, self.drain_log_queue)

    def build_ui(self):
        pad = {"padx": 10, "pady": 6}

        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True)

        self.drop_box = ttk.Label(
            frm,
            text="Drag and drop clean DQMJ2P ROM here",
            anchor="center",
            relief="ridge",
            padding=18,
        )
        self.drop_box.grid(row=0, column=0, columnspan=3, sticky="ew", padx=10, pady=10)

        ttk.Label(frm, text="Clean DQMJ2P ROM:").grid(row=1, column=0, sticky="w", **pad)
        self.rom_entry = ttk.Entry(frm, textvariable=self.rom_var)
        self.rom_entry.grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse", command=self.browse_rom).grid(row=1, column=2, **pad)

        if TKDND_AVAILABLE:
            for widget in (self.drop_box, self.rom_entry):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.handle_rom_drop)

        ttk.Label(frm, text="Output ROM:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.out_var).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse", command=self.browse_output).grid(row=2, column=2, **pad)

        tabs = ttk.Notebook(frm)
        tabs.grid(row=3, column=0, columnspan=3, sticky="nsew", **pad)

        patch_tab = ttk.Frame(tabs)
        rand_tab = ttk.Frame(tabs)
        tabs.add(patch_tab, text="Patch Options")
        tabs.add(rand_tab, text="Randomiser")

        recommended = ttk.LabelFrame(patch_tab, text="Recommended Defaults")
        recommended.pack(fill="x", expand=False, padx=8, pady=(8, 4))

        add_check_with_info(
            recommended,
            "Add New Synthesis Recipes",
            self.new_synths_var,
            "Vanilla Joker 2 Pro makes some monsters wi-fi exclusive or otherwise unobtainable. This checkbox adds new synthesis recipes for those monsters.",
        )
        add_check_with_info(
            recommended,
            "Add Unobtainable Items",
            self.postgame_pipit_vendor_var,
            "Vanilla Joker 2 Pro has a Wi-Fi only tournament, which unlocks a special shop in Pipisle. This checkbox moves those exclusive items to the regular shop in Pipisle once the post-game final boss has been defeated.",
        )
        add_check_with_info(
            recommended,
            "Apply X/XY Variant Suffix Fix",
            self.xvariant_var,
            "Vanilla Joker 2 Pro has X/XY monster variants in front of their name. This checkbox moves it to the end.",
        )
        add_check_with_info(
            recommended,
            "Replace Gender Icons with Polarity",
            self.gender_icons_var,
            "Vanilla Joker 2 Pro uses gender instead of +/- monster polarity for synthesis. This checkbox reverts it to +/-.",
        )

        qol = ttk.LabelFrame(patch_tab, text="Additional Quality of Life")
        qol.pack(fill="x", expand=False, padx=8, pady=(4, 8))

        add_check_with_info(
            qol,
            "Give Substitutes and Monster Pen Full Party EXP",
            self.full_reserve_exp_var,
            "Substitute-team and Monster Pen monsters receive the same battle EXP as monsters in the main party.",
        )
        add_check_with_info(
            qol,
            'Make "Took offense" NOT Disable Scouting',
            self.scout_offense_var,
            "Vanilla Joker 2 Pro disallows scouting after offending a monster. This checkbox removes that restriction.",
        )
        add_check_with_info(
            qol,
            "Remove Multiple Species Owned Check from Scouting",
            self.scout_penalty_var,
            "Vanilla Joker 2 Pro reduces scouting odds if you already own one of that monster. This checkbox removes that.",
        )
        add_check_with_info(
            qol,
            "Remove Synthesis Polarity Requirement",
            self.synth_polarity_var,
            "Vanilla Joker 2 Pro requires monsters be of opposite polarity/gender to be synthesised. This checkbox removes that restriction.",
        )
        add_value_option_with_info(
            qol,
            "Set XP Multiplier:",
            self.xp_mult_var,
            self.xp_mult_value,
            8,
            "Multiply battle XP rewards for defeating monsters for faster leveling.",
        )
        add_value_option_with_info(
            qol,
            "Set Minimum Synthesis Level:",
            self.synth_level_var,
            self.synth_level_value,
            5,
            "Vanilla Joker 2 Pro requires monsters be at least level 10 to be synthesised. This field allows you to set the required level.",
        )

        rand = ttk.Frame(rand_tab)
        rand.pack(fill="both", expand=True, padx=8, pady=8)

        enable_randomiser_info = (
            "The randomiser is naturally going to introduce game instability.\n"
            "E.g. Fighting three slimes for a total of three enemy slots may become three of a 3-slot monster, "
            "for a total of nine enemy monster slots. This will probably crash the game. For a more stable "
            "randomiser run, consider filtering out 3-slot monsters when you configure. Or simply flee if it is "
            "an optional battle."
        )

        synth_randomiser_info = (
            "??? family results are sometimes not able to be synthesised, and will sometimes crash the game when viewed. "
            "If you see synthesis results with no name displayed, avoid selecting or viewing that option to avoid crashing."
        )

        master_row = ttk.Frame(rand)
        master_row.pack(anchor="w", padx=8, pady=(8, 4))

        master_cb = ttk.Checkbutton(
            master_row,
            text="Enable Randomiser",
            variable=self.randomizer_enabled_var,
            command=self.toggle_randomizer_controls,
        )
        master_cb.pack(side="left")
        add_info_icon(master_row, enable_randomiser_info).pack(side="left", padx=(5, 0))

        self.randomizer_widgets = []

        rand_tabs = ttk.Notebook(rand)
        rand_tabs.pack(fill="both", expand=True, padx=8, pady=4)
        self.randomizer_widgets.append(rand_tabs)

        monsters_tab = ttk.Frame(rand_tabs)
        level_tab = ttk.Frame(rand_tabs)
        skill_tab = ttk.Frame(rand_tabs)
        skillsets_tab = ttk.Frame(rand_tabs)
        filters_tab = ttk.Frame(rand_tabs)

        rand_tabs.add(monsters_tab, text="Monsters")
        rand_tabs.add(level_tab, text="Level Up XP")
        rand_tabs.add(skill_tab, text="Skill Points")
        rand_tabs.add(skillsets_tab, text="Skillsets")
        rand_tabs.add(filters_tab, text="Battle monster replacement filters")

        monsters = ttk.Frame(monsters_tab)
        monsters.pack(fill="x", expand=False, padx=8, pady=8)

        randomizer_checks = [
            ("Randomise battle monsters", self.randomizer_monsters_var, None),
            ("Generate spoiler file", self.randomizer_spoiler_var, None),
            ("Randomise synthesis recipes", self.randomizer_generic_synthesis_var, synth_randomiser_info),
            ("Allow Flee/Scout for randomised battles", self.randomizer_allow_flee_var, None),
            ("No flee challenge", self.randomizer_no_flee_var, None),
            ("Stronger randomised monsters (150% stats)", self.randomizer_stronger_var, None),
        ]

        for label, var, info in randomizer_checks:
            row = ttk.Frame(monsters)
            row.pack(anchor="w", padx=8, pady=2)
            cb = ttk.Checkbutton(row, text=label, variable=var)
            cb.pack(side="left")
            if info:
                add_info_icon(row, info).pack(side="left", padx=(5, 0))
            self.randomizer_widgets.append(cb)
        seed_row = ttk.Frame(monsters)
        seed_row.pack(anchor="w", padx=8, pady=4)
        self.randomizer_widgets.append(seed_row)

        ttk.Label(seed_row, text="Seed:").pack(side="left")
        seed_entry = ttk.Entry(seed_row, textvariable=self.randomizer_seed_value, width=12)
        seed_entry.pack(side="left", padx=(6, 6))
        self.randomizer_widgets.append(seed_entry)
        ttk.Label(seed_row, text="0 = random seed").pack(side="left")

        level = ttk.Frame(level_tab)
        level.pack(fill="x", padx=8, pady=8)

        battle_xp_cb = ttk.Checkbutton(level, text="Randomise battle XP rewards", variable=self.randomizer_xp_var)
        battle_xp_cb.pack(anchor="w", padx=8, pady=(2, 8))
        self.randomizer_widgets.append(battle_xp_cb)

        for text, value in [
            ("Do not randomise level XP", "none"),
            ("Swap XP curves", "swap"),
            ("Randomise XP curves", "random"),
        ]:
            rb = ttk.Radiobutton(level, text=text, variable=self.randomizer_level_up_mode, value=value)
            rb.pack(anchor="w", padx=8, pady=2)
            self.randomizer_widgets.append(rb)

        variance_row = ttk.Frame(level)
        variance_row.pack(anchor="w", padx=8, pady=4)
        self.randomizer_widgets.append(variance_row)

        ttk.Label(variance_row, text="XP variance %:").pack(side="left")
        variance_entry = ttk.Entry(variance_row, textvariable=self.randomizer_level_up_variance, width=8)
        variance_entry.pack(side="left", padx=(6, 0))
        self.randomizer_widgets.append(variance_entry)

        skill = ttk.Frame(skill_tab)
        skill.pack(fill="x", padx=8, pady=8)

        for text, value in [
            ("Do not randomise skill points", "none"),
            ("Swap skill point levels", "swap"),
            ("Randomise skill points", "random"),
        ]:
            rb = ttk.Radiobutton(skill, text=text, variable=self.randomizer_skill_points_mode, value=value)
            rb.pack(anchor="w", padx=8, pady=2)
            self.randomizer_widgets.append(rb)

        skillsets = ttk.Frame(skillsets_tab)
        skillsets.pack(fill="x", padx=8, pady=8)
        skillsets_cb = ttk.Checkbutton(
            skillsets,
            text="Randomise skillsets",
            variable=self.randomizer_skillsets_var,
        )
        skillsets_cb.pack(anchor="w", padx=8, pady=2)
        add_tooltip(
            skillsets_cb,
            "Shuffles what each skillset does while keeping related three-stage skillset records together.",
        )
        self.randomizer_widgets.append(skillsets_cb)

        filters = ttk.Frame(filters_tab)
        filters.pack(fill="x", padx=8, pady=8)

        for label, var in [
        ]:
            cb = ttk.Checkbutton(filters, text=label, variable=var)
            cb.pack(anchor="w", padx=8, pady=2)
            self.randomizer_widgets.append(cb)

        ttk.Label(filters, text="Allowed ranks:").pack(anchor="w", padx=8, pady=(8, 2))
        rank_row = ttk.Frame(filters)
        rank_row.pack(anchor="w", padx=18, pady=2)
        self.randomizer_widgets.append(rank_row)
        for rank in ("F", "E", "D", "C", "B", "A", "S", "SS"):
            cb = ttk.Checkbutton(rank_row, text=rank, variable=self.randomizer_rank_vars[rank])
            cb.pack(side="left")
            self.randomizer_widgets.append(cb)

        ttk.Label(filters, text="Allowed families:").pack(anchor="w", padx=8, pady=(8, 2))
        family_row = ttk.Frame(filters)
        family_row.pack(anchor="w", padx=18, pady=2)
        self.randomizer_widgets.append(family_row)
        for family in ("Slime", "Dragon", "Nature", "Beast", "Material", "Demon", "Zombie", "???"):
            cb = ttk.Checkbutton(family_row, text=family, variable=self.randomizer_family_vars[family])
            cb.pack(side="left")
            self.randomizer_widgets.append(cb)

        ttk.Label(filters, text="Allowed sizes:").pack(anchor="w", padx=8, pady=(8, 2))
        size_row = ttk.Frame(filters)
        size_row.pack(anchor="w", padx=18, pady=2)
        self.randomizer_widgets.append(size_row)
        for size, label in [("1", "1-slot"), ("2", "2-slot"), ("3", "3-slot")]:
            cb = ttk.Checkbutton(size_row, text=label, variable=self.randomizer_size_vars[size])
            cb.pack(side="left")
            self.randomizer_widgets.append(cb)

        self.toggle_randomizer_controls()

        self.run_btn = ttk.Button(frm, text="Patch ROM", command=self.start_patch)
        self.run_btn.grid(row=4, column=0, columnspan=3, pady=10)

        self.progress = ttk.Progressbar(frm, mode="indeterminate")
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew", padx=10)

        ttk.Checkbutton(
            frm,
            text="Show command log",
            variable=self.show_log_var,
            command=self.toggle_log,
        ).grid(row=6, column=0, sticky="w", padx=10, pady=6)

        link_row = ttk.Frame(frm)
        link_row.grid(row=7, column=0, columnspan=3, sticky="ew", padx=10, pady=(0, 6))
        link_row.columnconfigure(1, weight=1)

        link_font = (
            tkfont.nametofont("TkDefaultFont").cget("family"),
            tkfont.nametofont("TkDefaultFont").cget("size"),
            "bold",
        )

        release_link = ttk.Label(
            link_row,
            text="Check for the Latest Release",
            cursor="hand2",
            foreground="blue",
            font=(tkfont.nametofont("TkDefaultFont").cget("family"), tkfont.nametofont("TkDefaultFont").cget("size"), "bold"),
        )
        release_link.grid(row=0, column=0, sticky="w")
        release_link.bind(
            "<Button-1>",
            lambda _e: open_url(
                "https://github.com/Saneezore/DQMJ2Pro_Translation/releases"
            ),
        )

        info_link = ttk.Label(
            link_row,
            text="View Information Page",
            cursor="hand2",
            foreground="blue",
            font=(tkfont.nametofont("TkDefaultFont").cget("family"), tkfont.nametofont("TkDefaultFont").cget("size"), "bold"),
        )
        info_link.grid(row=0, column=2, sticky="e")
        info_link.bind(
            "<Button-1>",
            lambda _e: open_url(
                "https://github.com/Saneezore/DQMJ2Pro_Translation"
            ),
        )

        self.log_frame = ttk.Frame(frm)
        self.log_text = tk.Text(self.log_frame, height=14, wrap="word")
        self.log_text.pack(fill="both", expand=True)

        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(8, weight=1)

    def toggle_randomizer_controls(self):
        self.update_randomised_output_name()
        state = "normal" if self.randomizer_enabled_var.get() else "disabled"
        for widget in getattr(self, "randomizer_widgets", []):
            try:
                widget.configure(state=state)
            except Exception:
                pass

    def toggle_log(self):
        if self.show_log_var.get():
            self.log_frame.grid(row=8, column=0, columnspan=3, sticky="nsew", padx=10, pady=6)
        else:
            self.log_frame.grid_forget()

    def clean_dropped_path(self, raw):
        raw = raw.strip()

        # tkinterdnd2 may wrap paths with braces if they contain spaces.
        if raw.startswith("{") and raw.endswith("}"):
            raw = raw[1:-1]

        # If multiple files are dropped, take the first.
        if "} {" in raw:
            raw = raw.split("} {", 1)[0].lstrip("{").rstrip("}")

        return raw.strip().strip('"').strip("'")

    def handle_rom_drop(self, event):
        path = self.clean_dropped_path(event.data)
        if path:
            self.rom_var.set(path)
            self.out_var.set(str(Path(path).with_name(f"DQMJ2P_Eng_Patched_v{PATCHER_VERSION}.nds")))
            self.update_randomised_output_name()


    def browse_rom(self):
        path = filedialog.askopenfilename(
            title="Select clean DQMJ2P ROM",
            filetypes=[("Nintendo DS ROM", "*.nds"), ("All files", "*.*")]
        )
        if path:
            self.rom_var.set(path)
            self.out_var.set(str(Path(path).with_name(f"DQMJ2P_Eng_Patched_v{PATCHER_VERSION}.nds")))
            self.update_randomised_output_name()

    def browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Save patched ROM as",
            defaultextension=".nds",
            filetypes=[("Nintendo DS ROM", "*.nds"), ("All files", "*.*")]
        )
        if path:
            self.out_var.set(path)

    def append_log(self, text):
        self.log_text.insert("end", text)
        self.log_text.see("end")



    def update_randomised_output_name(self):
        out = self.out_var.get().strip()
        if not out:
            return

        out_path = Path(out)
        plain = f"DQMJ2P_Eng_Patched_v{PATCHER_VERSION}.nds"
        rand = f"DQMJ2P_Eng_Patched_Randomised_v{PATCHER_VERSION}.nds"

        if self.randomizer_enabled_var.get():
            if out_path.name == plain:
                self.out_var.set(str(out_path.with_name(rand)))
        else:
            if out_path.name == rand:
                self.out_var.set(str(out_path.with_name(plain)))

    def start_patch(self):
        rom = self.rom_var.get().strip()
        out = self.out_var.get().strip()

        if not rom:
            messagebox.showerror("Missing ROM", "Select a clean .nds ROM first.")
            return
        if not Path(rom).is_file():
            messagebox.showerror("ROM not found", rom)
            return
        if not out:
            messagebox.showerror("Missing output", "Choose an output .nds path.")
            return

        if self.randomizer_enabled_var.get():
            out_path = Path(out)
            default_plain = f"DQMJ2P_Eng_Patched_v{PATCHER_VERSION}.nds"
            default_rand = f"DQMJ2P_Eng_Patched_Randomised_v{PATCHER_VERSION}.nds"
            if out_path.name == default_plain:
                out_path = out_path.with_name(default_rand)
                out = str(out_path)
                self.out_var.set(out)

        args = ["--rom", rom, "--output", out, "--anti-piracy"]
        if self.new_synths_var.get():
            args.append("--new-synths")
        if self.postgame_pipit_vendor_var.get():
            args.append("--postgame-pipit-vendor-items")
        if self.xp_mult_var.get():
            args.extend(["--xp-mult", self.xp_mult_value.get()])
        if self.full_reserve_exp_var.get():
            args.append("--full-reserve-exp")
        if self.xvariant_var.get():
            args.append("--xvariant-suffix")
        if self.gender_icons_var.get():
            args.append("--gender-icons")
        if self.scout_offense_var.get():
            args.append("--scout-offense")
        if self.scout_penalty_var.get():
            args.append("--scout-penalty")
        if self.synth_level_var.get():
            args.extend(["--synthesis-level", self.synth_level_value.get()])
        if self.synth_polarity_var.get():
            args.append("--synthesis-polarity")

        if self.randomizer_enabled_var.get():
            args.append("--randomizer-enabled")
            seed = self.randomizer_seed_value.get().strip() or "0"
            try:
                int(seed)
            except ValueError:
                messagebox.showerror("Invalid seed", "Randomizer seed must be a whole number.")
                return

            args.extend(["--randomizer-seed", seed])

            if self.randomizer_monsters_var.get():
                args.append("--randomizer-monsters")

                rank_excludes = [rank for rank, var in self.randomizer_rank_vars.items() if not var.get()]
                family_excludes = [family for family, var in self.randomizer_family_vars.items() if not var.get()]
                size_excludes = [size for size, var in self.randomizer_size_vars.items() if not var.get()]

                if len(rank_excludes) == len(self.randomizer_rank_vars):
                    messagebox.showerror("Invalid filters", "At least one monster rank must be allowed.")
                    return
                if len(family_excludes) == len(self.randomizer_family_vars):
                    messagebox.showerror("Invalid filters", "At least one monster family must be allowed.")
                    return
                if len(size_excludes) == len(self.randomizer_size_vars):
                    messagebox.showerror("Invalid filters", "At least one monster size must be allowed.")
                    return

                if rank_excludes:
                    args.extend(["--randomizer-rank-excludes", ",".join(rank_excludes)])
                if family_excludes:
                    args.extend(["--randomizer-family-excludes", ",".join(family_excludes)])
                if size_excludes:
                    args.extend(["--randomizer-size-excludes", ",".join(size_excludes)])

            if self.randomizer_spoiler_var.get():
                args.append("--randomizer-spoiler")
            if self.randomizer_allow_flee_var.get():
                args.append("--randomizer-allow-flee")
            if self.randomizer_xp_var.get():
                args.append("--randomizer-xp")
            if self.randomizer_stronger_var.get():
                args.append("--randomizer-stronger")
            if self.randomizer_no_flee_var.get():
                args.append("--randomizer-no-flee")

            level_up_mode = self.randomizer_level_up_mode.get()
            skill_points_mode = self.randomizer_skill_points_mode.get()

            if level_up_mode != "none":
                variance = self.randomizer_level_up_variance.get().strip() or "140"
                try:
                    variance_i = int(variance)
                except ValueError:
                    messagebox.showerror("Invalid variance", "Level Up XP variance must be a whole number.")
                    return
                if variance_i < 100 or variance_i > 300:
                    messagebox.showerror("Invalid variance", "Level Up XP variance must be between 100 and 300.")
                    return

                args.extend(["--randomizer-level-up", level_up_mode])
                args.extend(["--randomizer-level-up-variance", str(variance_i)])

            if skill_points_mode != "none":
                args.extend(["--randomizer-skill-points", skill_points_mode])

            if self.randomizer_skillsets_var.get():
                args.append("--randomizer-skillsets")

            if self.randomizer_generic_synthesis_var.get():
                args.append("--randomizer-generic-synthesis")

        self.log_text.delete("1.0", "end")
        self.append_log("> gui_backend " + " ".join(args) + "\n\n")

        self.run_btn.config(state="disabled")
        self.progress.start(10)

        threading.Thread(target=self.run_backend, args=(args,), daemon=True).start()

    def run_backend(self, args):
        writer = QueueWriter(self.log_queue)
        code = 0

        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                gui_backend.main(args)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if e.code and not isinstance(e.code, int):
                self.log_queue.put(str(e.code) + "\n")
        except Exception as e:
            code = 1
            self.log_queue.put(f"ERROR: {e}\n")

        self.log_queue.put(("__DONE__", code))

    def drain_log_queue(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "__DONE__":
                    code = item[1]
                    self.progress.stop()
                    self.run_btn.config(state="normal")
                    if code == 0:
                        messagebox.showinfo("Done", "Patched ROM created successfully.")
                    else:
                        messagebox.showerror("Failed", f"Patcher failed with exit code {code}.")
                else:
                    self.append_log(item)
        except queue.Empty:
            pass

        self.after(100, self.drain_log_queue)


if __name__ == "__main__":
    App().mainloop()
