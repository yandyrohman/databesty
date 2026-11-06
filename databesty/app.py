"""Databesty application entry point."""

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from . import __version__  # noqa: E402

APP_ID = "io.github.yandyrohman.Databesty"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIRS = [os.path.join(HERE, "..", "data"), os.path.join(HERE, "data")]


def _data_path(*parts):
    for d in DATA_DIRS:
        p = os.path.join(d, *parts)
        if os.path.exists(p):
            return os.path.abspath(p)
    return None


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        icons = _data_path("icons")
        if icons:
            Gtk.IconTheme.get_for_display(display).add_search_path(icons)
        css = _data_path("style.css")
        if css:
            provider = Gtk.CssProvider()
            provider.load_from_path(css)
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._about)
        self.add_action(about)
        quit_ = Gio.SimpleAction.new("quit", None)
        quit_.connect("activate", lambda *_: self.quit())
        self.add_action(quit_)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        shortcuts = Gio.SimpleAction.new("shortcuts", None)
        shortcuts.connect("activate", lambda *_: _shortcuts().present(self.window))
        self.add_action(shortcuts)
        self.set_accels_for_action("app.shortcuts", ["<Control>question"])

    def do_activate(self):
        from .window import MainWindow

        if self.window is None:
            self.window = MainWindow(self)
            self.window.connect("close-request", lambda w: (w.close_all(), False)[1])
        self.window.present()

    def _about(self, *_):
        about = Adw.AboutDialog(
            application_name="Databesty",
            application_icon=APP_ID,
            version=__version__,
            developer_name="Yandi",
            comments="Client database sederhana untuk PostgreSQL, MySQL dan SQLite.",
            website="https://github.com/yandyrohman/databesty",
            issue_url="https://github.com/yandyrohman/databesty/issues",
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self.window)


def _shortcuts():
    groups = [
        ("Umum", [("<Control>n", "Koneksi baru"), ("<Control>t", "SQL console baru"), ("<Control>w", "Tutup tab"),
                  ("<Control>p", "Cari tabel"), ("<Control>q", "Keluar")]),
        ("Data", [("F5", "Muat ulang tabel"), ("<Control>c", "Salin baris terpilih (TSV)"),
                  ("Delete", "Hapus baris terpilih"), ("Return", "Edit baris (atau klik dua kali)")]),
        ("SQL console", [("<Control>Return", "Jalankan query / teks yang diblok")]),
    ]
    dlg = Adw.ShortcutsDialog()
    for title, items in groups:
        section = Adw.ShortcutsSection(title=title)
        for accel, label in items:
            section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
        dlg.add(section)
    return dlg


def main():
    app = Application()
    return app.run(sys.argv)
