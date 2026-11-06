"""Form for inserting or editing one row."""

from gi.repository import Adw, Gtk

from .grid import to_text


class _Field:
    def __init__(self, column, value, editing):
        self.column = column
        self.original = value
        self.editing = editing
        self.row = Adw.EntryRow(title=column.name)
        self.row.set_text(to_text(value) if editing else "")

        typ = Gtk.Label(label=column.type or "", css_classes=["dim-label", "caption", "monospace"])
        typ.set_valign(Gtk.Align.CENTER)
        self.row.add_suffix(typ)

        self.null_btn = Gtk.ToggleButton(label="NULL", valign=Gtk.Align.CENTER)
        self.null_btn.add_css_class("flat")
        self.null_btn.add_css_class("null-toggle")
        self.null_btn.set_tooltip_text("Set value to NULL")
        self.null_btn.set_sensitive(column.nullable)
        self.null_btn.connect("toggled", self._null_toggled)
        self.row.add_suffix(self.null_btn)

        if editing and value is None:
            self.null_btn.set_active(True)
        self._update_hint()

    def _null_toggled(self, btn):
        if btn.get_active():
            self.row.set_text("")
        self.row.set_editable(not btn.get_active())
        self._update_hint()

    def _update_hint(self):
        c = self.column
        bits = []
        if c.primary_key:
            bits.append("primary key")
        if self.null_btn.get_active():
            bits.append("NULL")
        elif not self.editing and (c.auto or c.default is not None):
            bits.append("kosong = default" + (" (auto)" if c.auto else f" ({c.default})"))
        elif not self.editing and not c.nullable:
            bits.append("wajib diisi")
        self.row.set_title(c.name + ("  ·  " + ", ".join(bits) if bits else ""))

    def value(self):
        """Returns (include, value). include=False means leave the column alone."""
        if self.null_btn.get_active():
            if self.editing and self.original is None:
                return False, None
            return True, None
        text = self.row.get_text()
        if self.editing:
            if self.original is not None and text == to_text(self.original):
                return False, None
            if self.original is None and text == "":
                return False, None
        elif text == "":
            return False, None  # let the database apply its default
        if isinstance(self.original, (bytes, bytearray, memoryview)) or "blob" in self.column.type.lower() or "bytea" in self.column.type.lower():
            if text.startswith(("0x", "\\x")):
                try:
                    return True, bytes.fromhex(text[2:])
                except ValueError:
                    pass
        return True, text


class RowDialog(Adw.Dialog):
    """on_save(values_dict, done) is called with the changes; call done(error_or_None)."""

    def __init__(self, table, columns, values=None, on_save=None, read_only=False):
        super().__init__()
        self.editing = values is not None
        self.on_save = on_save
        self.set_title(("Edit baris" if self.editing else "Tambah baris") + f" — {table.name}")
        self.set_content_width(560)
        self.set_content_height(min(720, 110 + 56 * len(columns)))

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Batal")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.save_btn = Gtk.Button(label="Simpan" if self.editing else "Tambah", css_classes=["suggested-action"])
        self.save_btn.connect("clicked", self._save)
        self.save_btn.set_sensitive(not read_only)
        header.pack_end(self.save_btn)

        self.fields = []
        group = Adw.PreferencesGroup()
        for i, col in enumerate(columns):
            f = _Field(col, values[i] if self.editing else None, self.editing)
            f.row.connect("entry-activated", self._save)
            group.add(f.row)
            self.fields.append(f)

        self.error = Gtk.Label(wrap=True, xalign=0, css_classes=["error"], visible=False, selectable=True)
        group_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        group_box.append(self.error)
        group_box.append(group)
        clamp = Adw.Clamp(maximum_size=640, child=group_box, margin_top=12, margin_bottom=18, margin_start=12, margin_end=12)
        scroller = Gtk.ScrolledWindow(child=clamp, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)

        tv = Adw.ToolbarView(content=scroller)
        tv.add_top_bar(header)
        self.set_child(tv)
        if self.fields:
            self.set_focus(self.fields[0].row)

    def _save(self, *_):
        if not self.save_btn.get_sensitive():
            return
        changes = {}
        for f in self.fields:
            include, value = f.value()
            if include:
                changes[f.column.name] = value
        if self.editing and not changes:
            self.close()
            return
        self.save_btn.set_sensitive(False)
        self.error.set_visible(False)

        def done(err):
            self.save_btn.set_sensitive(True)
            if err:
                self.error.set_text(str(err))
                self.error.set_visible(True)
            else:
                self.close()

        self.on_save(changes, done)
