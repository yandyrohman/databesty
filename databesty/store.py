"""Saved connections, kept in ~/.config/databesty/connections.json (mode 0600)."""

import json
import os
import uuid
from dataclasses import asdict, dataclass, field

from .drivers import ConnInfo

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "databesty")
CONFIG_FILE = os.path.join(CONFIG_DIR, "connections.json")

COLORS = ["blue", "teal", "green", "yellow", "orange", "red", "pink", "purple", "slate"]


@dataclass
class SavedConnection:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    color: str = "blue"
    mode: str = "url"  # which tab the user filled in: url | fields
    info: ConnInfo = field(default_factory=ConnInfo)

    def to_json(self):
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, d):
        info = ConnInfo(**d.get("info", {}))
        return cls(
            id=d.get("id") or uuid.uuid4().hex[:12],
            name=d.get("name", ""),
            color=d.get("color", "blue"),
            mode=d.get("mode", "url"),
            info=info,
        )


def load():
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return []
    out = []
    for d in data.get("connections", []):
        try:
            out.append(SavedConnection.from_json(d))
        except TypeError:
            continue
    return out


def save(conns):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"version": 1, "connections": [c.to_json() for c in conns]}, f, indent=2)
    os.replace(tmp, CONFIG_FILE)
