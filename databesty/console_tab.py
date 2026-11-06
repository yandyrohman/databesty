"""SQL console: editor on top, results below."""

import time

import gi

gi.require_version("GtkSource", "5")
from gi.repository import Adw, Gdk, GLib, GtkSource, Gtk  # noqa: E402

from .grid import ResultGrid, run_async  # noqa: E402


class ConsoleTab(Gtk.Box):
    counter = 0

    def __init__(self, window, session, initial_sql=""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        ConsoleTab.counter += 1
        self.window = window
        self.session = session
        self.number = ConsoleTab.counter
        self.key = ("console", session.conn.id, self.number)
        self.running = False

        bar = Gtk.Box(spacing=6, css_classes=["toolbar", "table-toolbar"])
        self.run_btn = Gtk.Button(css_classes=["suggested-action"], tooltip_text="Jalankan (Ctrl+Enter)")
        self.run_btn.set_child(Adw.ButtonContent(icon_name="media-playback-start-symbolic", label="Jalankan"))
        self.run_btn.connect("clicked", lambda *_: self.run())
        bar.append(self.run_btn)
        hint = Gtk.Label(
            label="Ctrl+Enter menjalankan semua, atau hanya teks yang diblok",
            css_classes=["dim-label", "caption"],
            hexpand=True,
            xalign=0,
            margin_start=6,
        )
        bar.append(hint)
        self.status = Gtk.Label(css_classes=["caption", "numeric"], xalign=1, ellipsize=3)
        bar.append(self.status)
        self.append(bar)

        lang = GtkSource.LanguageManager.get_default().get_language("sql")
        self.buffer = GtkSource.Buffer(language=lang, highlight_syntax=True)
        self.buffer.set_text(initial_sql)
        self.editor = GtkSource.View(
            buffer=self.buffer,
            monospace=True,
            show_line_numbers=True,
            highlight_current_line=True,
            auto_indent=True,
            tab_width=4,
            insert_spaces_instead_of_tabs=True,
            top_margin=8,
            bottom_margin=8,
            left_margin=6,
        )
        self.editor.add_css_class("sql-editor")
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.editor.add_controller(keys)
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_: self._apply_scheme())
        self._apply_scheme()

        editor_scroll = Gtk.ScrolledWindow(child=self.editor, vexpand=True)

        self.grid = ResultGrid()
        self.result_stack = Gtk.Stack()
        self.empty = Adw.StatusPage(
            icon_name="utilities-terminal-symbolic",
            title="Belum ada hasil",
            description="Tulis query lalu tekan Ctrl+Enter",
            css_classes=["compact"],
        )
        self.message = Adw.StatusPage(css_classes=["compact"])
        self.result_stack.add_named(self.empty, "empty")
        self.result_stack.add_named(self.grid, "grid")
        self.result_stack.add_named(self.message, "message")

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True, wide_handle=True)
        paned.set_start_child(editor_scroll)
        paned.set_end_child(self.result_stack)
        paned.set_resize_start_child(True)
        paned.set_shrink_start_child(False)
        paned.set_position(260)
        self.append(paned)

    @property
    def title(self):
        return f"Console {self.number}"

    @property
    def tooltip(self):
        return f"{self.session.conn.name} · SQL console"

    def focus(self):
        self.editor.grab_focus()

    def _apply_scheme(self):
        dark = Adw.StyleManager.get_default().get_dark()
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme("Adwaita-dark" if dark else "Adwaita")
        if scheme:
            self.buffer.set_style_scheme(scheme)

    def _key(self, _ctl, keyval, _code, state):
        if state & Gdk.ModifierType.CONTROL_MASK and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.run()
            return True
        return False

    def run(self):
        if self.running:
            return
        b = self.buffer
        if b.get_has_selection():
            start, end = b.get_selection_bounds()
        else:
            start, end = b.get_bounds()
        sql = b.get_text(start, end, False).strip()
        if not sql:
            return
        self.running = True
        self.run_btn.set_sensitive(False)
        self.status.remove_css_class("error")
        self.status.set_text("Menjalankan…")
        drv = self.session.driver
        t0 = time.monotonic()

        def done(results):
            self.running = False
            self.run_btn.set_sensitive(True)
            ms = (time.monotonic() - t0) * 1000
            last_set = next((r for r in reversed(results) if r.columns), None)
            msgs = " · ".join(r.message for r in results if r.message)
            self.status.set_text(f"{msgs}  ·  {ms:,.0f} ms")
            if last_set is not None:
                self.grid.set_columns(last_set.columns)
                self.grid.set_rows([(None, r) for r in last_set.rows])
                self.result_stack.set_visible_child_name("grid")
            else:
                self.message.set_icon_name("object-select-symbolic")
                self.message.set_title("Berhasil")
                self.message.set_description(GLib.markup_escape_text(msgs or "OK"))
                self.result_stack.set_visible_child_name("message")
            if any(not r.columns for r in results):
                self.window.schema_changed(self.session)

        def fail(e):
            self.running = False
            self.run_btn.set_sensitive(True)
            self.status.set_text("Error")
            self.status.add_css_class("error")
            self.message.set_icon_name("dialog-error-symbolic")
            self.message.set_title("Query gagal")
            self.message.set_description(GLib.markup_escape_text(str(e)))
            self.result_stack.set_visible_child_name("message")

        run_async(lambda: drv.execute(sql), done, fail)

    def copy(self):
        n = self.grid.copy_selection()
        if n:
            self.window.toast(f"{n} baris disalin")
