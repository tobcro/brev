"""User preferences, stored in ~/.config/brev/settings.ini.

A plain key file rather than GSettings, so Brev works when run from the source
tree without installing a schema. Changes are written right away and announced
through GObject property notifications ("notify::always-show-images" etc.).
"""

from __future__ import annotations

import logging
import os

from gi.repository import GLib, GObject

log = logging.getLogger(__name__)

_GROUP = "Brev"


class Settings(GObject.Object):
    always_show_images = GObject.Property(type=bool, default=False)
    compose_format = GObject.Property(type=str, default="plain")  # "plain" or "html"
    signature_new = GObject.Property(type=str, default="")
    signature_reply = GObject.Property(type=str, default="")
    cache_days = GObject.Property(type=int, default=7, minimum=1, maximum=365)  # mail kept for offline use
    refresh_interval = GObject.Property(type=int, default=15, minimum=0)  # seconds; 0 = no automatic checks

    _KEYS = ("always_show_images", "compose_format", "signature_new", "signature_reply")
    _INT_KEYS = {"cache_days": (1, 365), "refresh_interval": (0, 24 * 3600)}

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self._loading = True
        self._load()
        self._loading = False
        self.connect("notify", self._on_changed)

    @classmethod
    def default_path(cls, demo=False) -> str:
        folder = os.path.join(GLib.get_user_config_dir(), "brev")
        return os.path.join(folder, "settings-demo.ini" if demo else "settings.ini")

    def _load(self):
        keyfile = GLib.KeyFile()
        try:
            keyfile.load_from_file(self.path, GLib.KeyFileFlags.NONE)
        except GLib.Error:
            return  # first run: defaults
        for key, (low, high) in self._INT_KEYS.items():
            try:
                self.set_property(key, max(low, min(high, keyfile.get_integer(_GROUP, key))))
            except GLib.Error:
                pass  # missing or not a number: keep the default
        for key in self._KEYS:
            try:
                if key == "always_show_images":
                    self.always_show_images = keyfile.get_boolean(_GROUP, key)
                else:
                    value = keyfile.get_string(_GROUP, key)
                    if key == "compose_format" and value not in ("plain", "html"):
                        continue
                    self.set_property(key, value)
            except GLib.Error:
                pass  # missing key: keep the default

    def _on_changed(self, _obj, _pspec):
        if not self._loading:
            self.save()

    def save(self):
        keyfile = GLib.KeyFile()
        keyfile.set_boolean(_GROUP, "always_show_images", self.always_show_images)
        for key in ("compose_format", "signature_new", "signature_reply"):
            keyfile.set_string(_GROUP, key, self.get_property(key))
        for key in self._INT_KEYS:
            keyfile.set_integer(_GROUP, key, self.get_property(key))
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            keyfile.save_to_file(self.path)
        except (GLib.Error, OSError) as err:
            log.warning("Could not save settings: %s", err)
