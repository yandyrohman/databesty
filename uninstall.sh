#!/usr/bin/env bash
# Removes Databesty from ~/.local. Saved connections in ~/.config/databesty are kept.
set -euo pipefail
APP_ID=io.github.yandyrohman.Databesty
PREFIX="${PREFIX:-$HOME/.local}"
rm -rf "$PREFIX/share/databesty"
rm -f "$PREFIX/bin/databesty" "$PREFIX/share/applications/$APP_ID.desktop" \
      "$PREFIX/share/icons/hicolor/scalable/apps/$APP_ID.svg"
update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true
echo "Databesty dihapus. Koneksi tersimpan masih ada di ~/.config/databesty (hapus manual kalau mau)."
