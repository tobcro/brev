"""The main window: folders | message list | message."""

from __future__ import annotations

import html
import os
from gettext import gettext as _
from urllib.parse import parse_qs, unquote, urlsplit

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Goa", "1.0")
from gi.repository import Adw, Gio, GLib, GObject, Goa, Gtk, Pango  # noqa: E402

from .compose import ComposeDialog  # noqa: E402
from .contacts import DemoContactSource, load_sources  # noqa: E402
from .contacts_view import ContactsDialog  # noqa: E402
from .html_view import AVAILABLE as HTML_AVAILABLE  # noqa: E402
from .offline import CachingBackend, OfflineError  # noqa: E402
from .util import describe_error, format_full_date  # noqa: E402
from .widgets import FolderRow, MessageRow, MessageView  # noqa: E402

PAGE_SIZE = 50
MIN_FULL_REFRESH_SECONDS = 120  # all folder counts, reconnecting, the Outbox: never more often than this


def _status_page(icon, title, description=""):
    return Adw.StatusPage(icon_name=icon, title=title, description=description)


def _spinner_page(label):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                  halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    box.append(Adw.Spinner(width_request=48, height_request=48))
    box.append(Gtk.Label(label=label, css_classes=["dim-label"]))
    return box




def _prefixed(subject: str, prefix: str) -> str:
    subject = subject or ""
    return subject if subject.lower().startswith(prefix.lower()) else f"{prefix} {subject}".strip()


class BrevWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="Brev")
        self.set_default_size(1240, 780)
        self.set_size_request(360, 294)

        self.services = []
        self.goa_client = None
        self._goa_reload_source = 0

        self.current_service = None
        self.current_folder = None
        self.current_summary = None
        self.current_content = None
        self.rows: dict[int, MessageRow] = {}
        self.folder_rows: dict[tuple, FolderRow] = {}
        self._list_token = 0
        self._message_token = 0
        self._has_more = False
        self._loading_more = False
        self._searching = False
        self._inbox_seen: dict[str, int] = {}  # account id -> highest Inbox UID already accounted for
        self._checking_new_mail = False
        self._busy = 0
        self._rebuilding_folders = False
        self.work_offline = False
        self._unreachable = False
        self._monitor = Gio.NetworkMonitor.get_default()
        self.plain_text = False
        self.unread_only = False  # list only unread messages in the current folder
        self._syncing = False
        self._remote_allowed: set[tuple] = set()

        self._build_ui()
        self._setup_actions()
        self._update_actions()
        self._monitor.connect("network-changed", self._on_network_changed)
        application.settings.connect("notify::always-show-images", lambda *_a: self.current_content is not None
                                     and self._render_content())
        self._load_accounts()
        self._sync_timer = self._refresh_timer = 0
        self._schedule_refresh()
        application.settings.connect("notify::refresh-interval", lambda *_a: self._schedule_refresh())

    # ================================================================ UI

    def _build_ui(self):
        self.toasts = Adw.ToastOverlay()
        self.main_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.main_stack)
        self.set_content(self.toasts)

        # -- loading / no accounts pages
        loading = Adw.ToolbarView(content=_spinner_page(_("Loading accounts…")))
        loading.add_top_bar(Adw.HeaderBar(show_title=False))
        self.main_stack.add_named(loading, "loading")

        self.no_accounts_page = _status_page(
            "mail-unread-symbolic", _("No Mail Accounts"),
            _("Brev uses the accounts from Settings → Online Accounts. Add an account with mail enabled to get started."),
        )
        open_settings = Gtk.Button(label=_("_Open Online Accounts"), use_underline=True,
                                   halign=Gtk.Align.CENTER, action_name="app.accounts",
                                   css_classes=["pill", "suggested-action"])
        self.no_accounts_page.set_child(open_settings)
        no_accounts = Adw.ToolbarView(content=self.no_accounts_page)
        no_accounts.add_top_bar(Adw.HeaderBar(show_title=False))
        self.main_stack.add_named(no_accounts, "no-accounts")

        # -- folder sidebar
        self.folder_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.folder_list.set_sort_func(lambda a, b: (a.order > b.order) - (a.order < b.order))
        self.folder_list.set_header_func(self._folder_header)
        self.folder_list.connect("row-selected", self._on_folder_selected)
        self.folder_list.update_property([Gtk.AccessibleProperty.LABEL], [_("Folders")])

        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_title_widget(Adw.WindowTitle(title="Brev"))
        # Searches the selected folder; the search bar itself sits above the message list.
        self.search_button = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                              tooltip_text=_("Search Selected Folder (Ctrl+F)"))
        sidebar_header.pack_start(self.search_button)
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("_Refresh"), "win.refresh")
        section.append(_("_Work Offline"), "win.work-offline")
        section.append(_("_Contacts"), "win.contacts")
        section.append(_("_Manage Accounts"), "app.accounts")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("_Preferences"), "app.preferences")
        section.append(_("_Keyboard Shortcuts"), "app.shortcuts")
        section.append(_("_About Brev"), "app.about")
        menu.append_section(None, section)
        sidebar_header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                               primary=True, tooltip_text=_("Main Menu")))
        sidebar = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=self.folder_list,
                                                             hscrollbar_policy=Gtk.PolicyType.NEVER))
        sidebar.add_top_bar(sidebar_header)

        # -- message list
        self.list_title = Adw.WindowTitle(title=_("Mail"))
        list_header = Adw.HeaderBar(title_widget=self.list_title)
        self.sidebar_button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text=_("Show Folders"))
        list_header.pack_start(self.sidebar_button)
        self.unread_button = Gtk.ToggleButton(action_name="win.unread-only",
                                              tooltip_text=_("Show Only Unread Messages"))
        list_header.pack_start(self.unread_button)
        self._sync_unread_icon()
        # Narrow windows show one pane at a time, so the reading pane's New Message button
        # may be out of sight: this one appears only then.
        self.narrow_compose_button = Gtk.Button(icon_name="mail-message-new-symbolic", action_name="win.compose",
                                                tooltip_text=_("New Message"), visible=False)
        list_header.pack_end(self.narrow_compose_button)
        list_header.pack_end(Gtk.Button(icon_name="view-refresh-symbolic", action_name="win.refresh",
                                        tooltip_text=_("Refresh (F5)")))
        self._activity = [Adw.Spinner(visible=False, tooltip_text=_("Working…"))]
        list_header.pack_end(self._activity[0])

        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Search this folder"), hexpand=True, search_delay=450)
        self.search_button.connect("toggled", self._on_search_toggled)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_bar = Gtk.SearchBar(child=Adw.Clamp(child=self.search_entry, maximum_size=420))
        self.search_bar.connect_entry(self.search_entry)
        self.search_button.bind_property("active", self.search_bar, "search-mode-enabled",
                                         GObject.BindingFlags.BIDIRECTIONAL)

        self.message_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.message_list.set_sort_func(self._sort_messages)
        self.message_list.connect("row-selected", self._on_message_selected)
        self.message_list.connect("row-activated", self._on_message_activated)
        self.message_list.update_property([Gtk.AccessibleProperty.LABEL], [_("Messages")])
        self.more_spinner = Gtk.Revealer(child=Adw.Spinner(margin_top=12, margin_bottom=12))
        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        list_box.append(self.message_list)
        list_box.append(self.more_spinner)
        list_scroller = Gtk.ScrolledWindow(child=list_box, hscrollbar_policy=Gtk.PolicyType.NEVER)
        # Load more when close to the end, and also when a page does not fill the view
        # (then there is nothing to scroll, and no edge would ever be reached).
        self._list_adjustment = list_scroller.get_vadjustment()
        self._list_adjustment.connect("value-changed", lambda _a: self._maybe_load_more())
        self._list_adjustment.connect("changed", lambda _a: self._maybe_load_more())  # after layout

        self.list_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.list_stack.add_named(_status_page("mail-inbox-symbolic", ""), "none")
        self.list_stack.add_named(_spinner_page(_("Loading messages…")), "loading")
        self.list_stack.add_named(list_scroller, "list")
        self.list_empty = _status_page("mail-inbox-symbolic", _("No Messages"))
        self.list_empty.add_css_class("compact")
        self.list_stack.add_named(self.list_empty, "empty")
        self.list_error = _status_page("dialog-warning-symbolic", _("Could Not Load Messages"))
        self.list_error.add_css_class("compact")
        retry = Gtk.Button(label=_("_Try Again"), use_underline=True, halign=Gtk.Align.CENTER,
                           action_name="win.reload-folder", css_classes=["pill"])
        self.list_error.set_child(retry)
        self.list_stack.add_named(self.list_error, "error")

        list_view = Adw.ToolbarView(content=self.list_stack)
        list_view.add_top_bar(list_header)
        list_view.add_top_bar(self.search_bar)
        self.offline_banner = Adw.Banner()
        self.offline_banner.connect("button-clicked", self._on_offline_banner_clicked)
        list_view.add_top_bar(self.offline_banner)
        list_page = Adw.NavigationPage(title=_("Messages"), tag="list", child=list_view)

        # -- message
        self.message_view = MessageView(on_mailto=self._compose_mailto, on_address=self._on_address_action,
                                        address_status=self._address_status)
        # No cross-fade here: fading snapshots the web view and can flash the previous message.
        self.message_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE)
        self.message_stack.add_named(_status_page("mail-unread-symbolic", _("No Message Selected")), "empty")
        self.message_stack.add_named(_spinner_page(_("Opening message…")), "loading")
        self.message_stack.add_named(self.message_view, "message")
        self.message_error = _status_page("dialog-warning-symbolic", _("Could Not Open Message"))
        self.message_stack.add_named(self.message_error, "error")

        self._toggle_buttons = []  # (button, kind) so icons can follow the message state
        message_header = Adw.HeaderBar(show_title=False)
        self.header_start = Gtk.Box(spacing=6)
        for action, icon, tip in (
            ("win.reply", "mail-reply-sender-symbolic", _("Reply")),
            ("win.reply-all", "mail-reply-all-symbolic", _("Reply to All")),
            ("win.forward", "mail-forward-symbolic", _("Forward")),
        ):
            self.header_start.append(Gtk.Button(icon_name=icon, tooltip_text=tip, action_name=action))
        message_header.pack_start(Gtk.Button(icon_name="mail-message-new-symbolic", action_name="win.compose",
                                             tooltip_text=_("New Message (Ctrl+N)")))
        message_header.pack_start(self.header_start)

        more = Gio.Menu()
        if HTML_AVAILABLE:
            more.append(_("Show as _Plain Text"), "win.plain-text")
        more.append(_("Open in _Browser"), "win.open-in-browser")
        more.append(_("_Save Message As…"), "win.save-message")
        self.header_end = Gtk.Box(spacing=6)
        for widget in self._message_action_buttons(narrow=False):
            self.header_end.append(widget)
        self.header_end.append(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=more,
                                              tooltip_text=_("More Actions")))
        message_header.pack_end(self.header_end)
        self._activity.append(Adw.Spinner(visible=False, tooltip_text=_("Working…")))
        message_header.pack_end(self._activity[1])

        bottom_box = Gtk.Box(spacing=6, homogeneous=True, hexpand=True,
                             margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for action, icon, tip in (
            ("win.reply", "mail-reply-sender-symbolic", _("Reply")),
            ("win.forward", "mail-forward-symbolic", _("Forward")),
        ):
            bottom_box.append(Gtk.Button(icon_name=icon, tooltip_text=tip, action_name=action))
        for widget in self._message_action_buttons(narrow=True):
            widget.add_css_class("flat")
            bottom_box.append(widget)
        bottom_box.append(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=more,
                                         tooltip_text=_("More Actions"), css_classes=["flat"]))
        for child in (bottom_box.get_first_child(), bottom_box.get_first_child().get_next_sibling()):
            child.add_css_class("flat")
        self.bottom_bar = bottom_box

        self.html_banner = Adw.Banner()
        self.message_toolbar = Adw.ToolbarView(content=self.message_stack, reveal_bottom_bars=False)
        self.message_toolbar.add_top_bar(message_header)
        self.message_toolbar.add_top_bar(self.html_banner)
        self.message_toolbar.add_bottom_bar(self.bottom_bar)
        message_page = Adw.NavigationPage(title=_("Message"), tag="message", child=self.message_toolbar)

        # -- split views
        self.inner = Adw.NavigationSplitView(sidebar=list_page, content=message_page,
                                             min_sidebar_width=300, max_sidebar_width=440,
                                             sidebar_width_fraction=0.4)
        self.outer = Adw.OverlaySplitView(sidebar=sidebar, content=self.inner,
                                          min_sidebar_width=200, max_sidebar_width=280,
                                          sidebar_width_fraction=0.22)
        self.outer.bind_property("show-sidebar", self.sidebar_button, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self.main_stack.add_named(self.outer, "mail")
        self.main_stack.set_visible_child_name("loading")

        def setter(bp, obj, prop, value):
            bp.add_setter(obj, prop, GObject.Value(GObject.TYPE_BOOLEAN, value))

        medium = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 860sp"))
        setter(medium, self.outer, "collapsed", True)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 580sp"))
        setter(narrow, self.outer, "collapsed", True)
        setter(narrow, self.inner, "collapsed", True)
        setter(narrow, self.message_toolbar, "reveal-bottom-bars", True)
        setter(narrow, self.header_start, "visible", False)
        setter(narrow, self.narrow_compose_button, "visible", True)
        setter(narrow, self.header_end, "visible", False)
        self.add_breakpoint(medium)
        self.add_breakpoint(narrow)

    def _message_action_buttons(self, narrow):
        buttons = [
            (Gtk.Button(icon_name="mail-archive-symbolic", tooltip_text=_("Archive"), action_name="win.archive"), None),
            (Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=_("Move to Trash"), action_name="win.trash"), None),
            (Gtk.Button(action_name="win.toggle-read"), "read"),
            (Gtk.Button(action_name="win.toggle-star"), "star"),
        ]
        for button, kind in buttons:
            if kind:
                self._toggle_buttons.append((button, kind))
        return [b for b, _k in buttons]

    def _sync_toggle_buttons(self):
        summary = self.current_summary
        for button, kind in self._toggle_buttons:
            if kind == "read":
                unread = bool(summary and summary.unread)
                button.set_icon_name("mail-read-symbolic" if unread else "mail-unread-symbolic")
                button.set_tooltip_text(_("Mark as Read") if unread else _("Mark as Unread"))
            else:
                starred = bool(summary and summary.flagged)
                button.set_icon_name("starred-symbolic" if starred else "non-starred-symbolic")
                button.set_tooltip_text(_("Unstar") if starred else _("Star"))

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    # =========================================================== actions

    def _setup_actions(self):
        self._actions = {}
        for name, callback in (
            ("compose", lambda *_a: self.compose()),
            ("reply", lambda *_a: self._reply("reply")),
            ("reply-all", lambda *_a: self._reply("reply-all")),
            ("forward", lambda *_a: self._reply("forward")),
            ("archive", lambda *_a: self._move_current("archive")),
            ("trash", lambda *_a: self._move_current("trash")),
            ("toggle-read", lambda *_a: self._toggle_flag("\\Seen")),
            ("toggle-star", lambda *_a: self._toggle_flag("\\Flagged")),
            ("refresh", lambda *_a: self.refresh(user=True)),
            ("reload-folder", lambda *_a: self._reload_folder()),
            ("search", lambda *_a: self.search_bar.set_search_mode(True)),
            ("open-in-browser", lambda *_a: self._open_in_browser()),
            ("save-message", lambda *_a: self._save_message()),
            ("contacts", lambda *_a: self.show_contacts()),
            ("show-remote", lambda *_a: self._show_remote()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            self._actions[name] = action
        unread = Gio.SimpleAction.new_stateful("unread-only", None, GLib.Variant.new_boolean(False))
        unread.connect("change-state", self._on_unread_only_changed)
        self.add_action(unread)
        plain = Gio.SimpleAction.new_stateful("plain-text", None, GLib.Variant.new_boolean(False))
        plain.connect("change-state", self._on_plain_text_changed)
        self.add_action(plain)
        offline = Gio.SimpleAction.new_stateful("work-offline", None, GLib.Variant.new_boolean(False))
        offline.connect("change-state", self._on_work_offline_changed)
        self.add_action(offline)

    def _enable(self, name, enabled):
        self._actions[name].set_enabled(bool(enabled))

    def _update_actions(self):
        has_accounts = bool(self.services)
        has_message = self.current_summary is not None
        has_content = self.current_content is not None
        role = self.current_folder.role if self.current_folder else None
        can_archive = (
            has_message and role not in ("archive", "all", "trash", "drafts", "sent")
            and self.current_service is not None
            and (self.current_service.folder_for_role("archive") or self.current_service.folder_for_role("all"))
        )
        self._enable("compose", has_accounts)
        self._enable("refresh", has_accounts)
        self._enable("search", self.current_folder is not None)
        for name in ("reply", "reply-all", "forward", "open-in-browser", "save-message"):
            self._enable(name, has_content)
        self._enable("archive", can_archive)
        for name in ("trash", "toggle-read", "toggle-star"):
            self._enable(name, has_message)
        self._sync_toggle_buttons()

    # ========================================================== accounts

    def _load_accounts(self):
        if self.get_application().demo:
            from . import demo
            self._set_services(demo.create_services(self.get_application().cache))
            self.refresh_contacts()
            return
        Goa.Client.new(None, self._on_goa_client)

    def _on_goa_client(self, _source, result):
        try:
            self.goa_client = Goa.Client.new_finish(result)
        except GLib.Error as err:
            self.no_accounts_page.set_description(
                _("Could not connect to GNOME Online Accounts: {error}").format(error=err.message))
            self.main_stack.set_visible_child_name("no-accounts")
            return
        for signal in ("account-added", "account-removed", "account-changed"):
            self.goa_client.connect(signal, self._on_goa_changed)
        self._reload_services()

    def _on_goa_changed(self, *_args):
        if self._goa_reload_source:
            GLib.source_remove(self._goa_reload_source)

        def reload():
            self._goa_reload_source = 0
            self._reload_services()
            self.refresh_contacts()
            return GLib.SOURCE_REMOVE

        self._goa_reload_source = GLib.timeout_add(800, reload)

    def _reload_services(self):
        from .accounts import load_accounts
        from .imap import ImapBackend
        from .service import MailService

        accounts = load_accounts(self.goa_client)
        if [a.signature for a in accounts] == [s.account.signature for s in self.services]:
            return
        cache = self.get_application().cache
        self._set_services([MailService(a, CachingBackend(a, ImapBackend(a), cache)) for a in accounts])

    def _set_services(self, services):
        for service in self.services:
            service.shutdown()
        self.services = services
        self.folder_rows.clear()
        self.folder_list.remove_all()
        self._show_folder(None, None)
        if not services:
            self.main_stack.set_visible_child_name("no-accounts")
            self._update_actions()
            return
        self.main_stack.set_visible_child_name("mail")
        self._sync_online_state()
        self.refresh_contacts()
        for service in services:
            self._load_folders(service)
        self._flush_all()
        self._update_actions()

    # =========================================================== folders

    def _load_folders(self, service):
        if not service.folders:
            cached = service.cached_folders()
            if cached:
                self._on_folders(service, cached)

        def done(folders):
            if service not in self.services:
                return
            self._on_folders(service, folders)
            self._check_new_mail()  # records where this Inbox starts, for counting new mail later
            ok, bad = self._track(lambda counts: self._apply_counts(service, counts))
            service.call("unread_counts", [f.name for f in folders if f.counts_unread], done=ok, error=bad)

        def error(err):
            if service in self.services and not isinstance(err, OfflineError):
                self.toast(_("{account}: {error}").format(account=service.account.title, error=describe_error(err)))

        if not service.online:
            return
        ok, bad = self._track(done, error)
        service.call("list_folders", False, done=ok, error=bad)

    def _on_folders(self, service, folders):
        index = self.services.index(service)
        self._rebuilding_folders = True
        for key in [k for k in self.folder_rows if k[0] is service]:
            self.folder_list.remove(self.folder_rows.pop(key))
        service.folders = folders
        for position, folder in enumerate(folders):
            row = FolderRow(service, folder, (index, position))
            self.folder_rows[(service, folder.name)] = row
            self.folder_list.append(row)
        if self.current_service is service and self.current_folder is not None:
            row = self.folder_rows.get((service, self.current_folder.name))
            if row is not None:
                self.current_folder = row.folder
                self.folder_list.select_row(row)
        self._rebuilding_folders = False

        if self.current_folder is None and service is self.services[0]:
            inbox = service.folder_for_role("inbox") or (folders[0] if folders else None)
            if inbox:
                row = self.folder_rows[(service, inbox.name)]
                self.folder_list.select_row(row)
                # Keep focus out of the message list; a list box that gains focus
                # selects (and so opens) its first row.
                row.grab_focus()
        self._update_actions()

    def _apply_counts(self, service, counts):
        for name, count in counts.items():
            row = self.folder_rows.get((service, name))
            if row:
                row.folder.unread = count
                row.update()

    def _check_new_mail(self):
        """Count unread mail that arrived in each Inbox since the last check; notify once in total.

        The first check per account only records where to start counting from, so mail that
        was already there when Brev started is not announced.
        """
        if not self._is_online() or self._checking_new_mail:
            return
        inboxes = [(s, s.folder_for_role("inbox")) for s in self.services]
        inboxes = [(s, f) for s, f in inboxes if f is not None]
        if not inboxes:
            return
        self._checking_new_mail = True
        tally = {"left": len(inboxes), "new": 0}

        def finished_one():
            tally["left"] -= 1
            if tally["left"] == 0:
                self._checking_new_mail = False
                if tally["new"]:
                    self.get_application().notifier.new_mail(tally["new"])

        def checked(service, result):
            count, top = result
            if service.account.id in self._inbox_seen:
                tally["new"] += count
            self._inbox_seen[service.account.id] = top
            finished_one()

        for service, inbox in inboxes:
            since = self._inbox_seen.get(service.account.id)
            ok, bad = self._track(lambda result, s=service: checked(s, result), lambda _e: finished_one(),
                                  quiet=True)
            service.call("new_arrivals", inbox.name, since, done=ok, error=bad)

    def _folder_header(self, row, before):
        if before is not None and before.service is row.service:
            row.set_header(None)
            return
        if len(self.services) < 2 and before is None:
            row.set_header(None)
            return
        label = Gtk.Label(label=row.service.account.title, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                          css_classes=["heading", "dim-label", "caption"],
                          margin_start=12, margin_end=12, margin_top=18 if before else 6, margin_bottom=6)
        row.set_header(label)

    def _on_folder_selected(self, _listbox, row):
        if row is None or self._rebuilding_folders:
            return
        if self.outer.get_collapsed():
            self.outer.set_show_sidebar(False)
        if row.service is self.current_service and self.current_folder and row.folder.name == self.current_folder.name:
            return
        self._show_folder(row.service, row.folder)

    def _show_folder(self, service, folder):
        self.current_service, self.current_folder = service, folder
        self._list_token += 1
        self._searching = False
        if self.search_bar.get_search_mode():
            self.search_entry.set_text("")
            self.search_bar.set_search_mode(False)
        self._clear_messages()
        self._show_message(None)
        if folder is None:
            self.list_title.set_title(_("Mail"))
            self.list_title.set_subtitle("")
            self.list_stack.set_visible_child_name("none")
            self._update_actions()
            return
        self.list_title.set_title(folder.title)
        self._update_list_labels()
        self._reload_folder()

    def _on_search_toggled(self, button):
        # When the folders are an overlay (narrower windows), get them out of the way of the search bar.
        if button.get_active() and self.outer.get_collapsed():
            self.outer.set_show_sidebar(False)

    def _update_list_labels(self):
        service, folder = self.current_service, self.current_folder
        if folder is None:
            return
        self.search_entry.set_placeholder_text(_("Search {folder}").format(folder=folder.title))
        parts = [_("Unread")] if self.unread_only else []
        if len(self.services) > 1:
            parts.append(service.account.title)
        self.list_title.set_subtitle(" · ".join(parts))
        if self.unread_only:
            self.list_empty.set_icon_name("mail-read-symbolic")
            self.list_empty.set_title(_("No Unread Messages"))
        else:
            self.list_empty.set_icon_name(folder.icon_name)
            self.list_empty.set_title(_("No Messages"))
        self.unread_button.set_tooltip_text(_("Show All Messages") if self.unread_only
                                            else _("Show Only Unread Messages"))

    def _sync_unread_icon(self):
        """The theme's own icons: "read" while all messages are listed, "unread" while only unread ones are."""
        self.unread_button.set_icon_name("mail-unread-symbolic" if self.unread_only else "mail-read-symbolic")

    def _on_unread_only_changed(self, action, value):
        action.set_state(value)
        self.unread_only = value.get_boolean()
        self._sync_unread_icon()
        if self.current_folder is None:
            return
        self._update_list_labels()
        if self._searching:
            self._on_search_changed(self.search_entry)
        else:
            self._show_message(None)
            self._reload_folder()

    def _reload_folder(self):
        if self.current_folder is None:
            return
        self._list_token += 1
        token = self._list_token
        service, folder = self.current_service, self.current_folder
        self._clear_messages()
        service.cancel_prefetch()  # downloads for the list we are leaving can wait
        cached, more = service.cached_messages(folder.name, PAGE_SIZE, None, self.unread_only)
        for summary in cached:
            self._add_summary(summary)
        self._has_more = more
        self.list_stack.set_visible_child_name("list" if cached else "loading")
        self._update_actions()
        if not service.online:
            if not cached:
                self._show_offline_list()
            return
        ok, bad = self._track(self._guard(token, self._on_server_page), self._guard(token, self._on_list_error))
        service.call("list_messages", folder.name, PAGE_SIZE, None, self.unread_only, done=ok, error=bad)

    def _on_server_page(self, result):
        summaries, has_more = result
        live = {s.uid: s for s in summaries}
        lowest = min(live, default=0)
        for uid in [u for u in self.rows if u >= lowest and u not in live]:
            self._remove_row(self.rows[uid].summary)
        for summary in summaries:
            row = self.rows.get(summary.uid)
            if row is None:
                self._add_summary(summary)
            elif row.summary.flags != summary.flags:
                row.summary.flags = summary.flags
                row.update()
        self._has_more = has_more or any(u < lowest for u in self.rows)
        self.list_stack.set_visible_child_name("list" if self.rows else "empty")
        self._update_actions()
        self._check_fill_later()
        self._prefetch_recent(summaries)

    def _prefetch_recent(self, summaries):
        import time
        keep = self.get_application().settings.cache_days * 24 * 3600
        recent = [s for s in summaries if s.timestamp >= time.time() - keep]
        if recent and self.current_service.online:
            ok, _bad = self._track(lambda _n: None)
            self.current_service.prefetch(self.current_folder.name, recent, done=ok)
            # prefetch never raises; its errors are logged and it simply stops

    def _show_offline_list(self):
        self.list_error.set_icon_name("network-offline-symbolic")
        self.list_error.set_title(_("You Are Offline"))
        self.list_error.set_description(_("No messages from this folder are saved on this computer."))
        self.list_stack.set_visible_child_name("error")

    def _guard(self, token, callback, kind="list"):
        def run(value):
            current = self._list_token if kind == "list" else self._message_token
            if token == current:
                callback(value)
        return run

    # ========================================================== messages

    def _sort_messages(self, a, b):
        return (b.summary.uid > a.summary.uid) - (b.summary.uid < a.summary.uid)

    def _clear_messages(self):
        self.rows.clear()
        self.message_list.remove_all()
        self._has_more = False
        self._loading_more = False
        self.more_spinner.set_reveal_child(False)
        self._list_adjustment.set_value(0)  # a new list starts at the top, not where the old one was

    def _add_summary(self, summary):
        if summary.uid in self.rows:
            return
        role = self.current_folder.role if self.current_folder else None
        row = MessageRow(summary, show_recipient=role in ("sent", "drafts"))
        self.rows[summary.uid] = row
        self.message_list.append(row)

    def _on_messages_loaded(self, result):
        summaries, has_more = result
        for summary in summaries:
            self._add_summary(summary)
        self._has_more = has_more
        self._loading_more = False
        self.more_spinner.set_reveal_child(False)
        self.list_stack.set_visible_child_name("list" if self.rows else "empty")
        self._check_fill_later()  # keep going if the view is not full yet

    def _on_list_error(self, err):
        self._loading_more = False
        self.more_spinner.set_reveal_child(False)
        if isinstance(err, OfflineError):
            if not self.rows:
                self._show_offline_list()
            return
        if self.rows:
            self.toast(describe_error(err))
            return
        self.list_error.set_icon_name("dialog-warning-symbolic")
        self.list_error.set_title(_("Could Not Load Messages"))
        self.list_error.set_description(describe_error(err))
        self.list_stack.set_visible_child_name("error")

    def _check_fill_later(self):
        # Short content never changes the scroll range, so check once layout has settled.
        GLib.timeout_add(350, self._maybe_load_more)

    def _maybe_load_more(self):
        """Load the next page when the user is near the end of the list (or it does not fill the view)."""
        if self.list_stack.get_visible_child_name() != "list" or not self._has_more or self._loading_more:
            return GLib.SOURCE_REMOVE
        adj = self._list_adjustment
        page = adj.get_page_size()
        if page <= 0:
            return GLib.SOURCE_REMOVE
        threshold = max(page * 0.5, 200)
        # Cheap check first. Layout can lag behind freshly added rows (e.g. while the window is
        # hidden or between frames), which only ever makes the list look shorter than it is...
        if adj.get_upper() - (adj.get_value() + page) > threshold:
            return GLib.SOURCE_REMOVE
        # ...so confirm with the rows' real height, which does not depend on layout having run.
        width = self.message_list.get_width() or 300
        content = self.message_list.measure(Gtk.Orientation.VERTICAL, width)[1]
        if content - (adj.get_value() + page) <= threshold:
            self._on_list_edge(None, Gtk.PositionType.BOTTOM)
        return GLib.SOURCE_REMOVE

    def _on_list_edge(self, _scroller, position):
        if position != Gtk.PositionType.BOTTOM or not self._has_more or self._loading_more or self._searching:
            return
        if not self.rows or self.current_folder is None:
            return
        oldest = min(self.rows)
        service, folder = self.current_service, self.current_folder
        if not service.online:
            self._on_messages_loaded(service.cached_messages(folder.name, PAGE_SIZE, oldest, self.unread_only))
            return
        self._loading_more = True
        self.more_spinner.set_reveal_child(True)
        token = self._list_token
        unread_only = self.unread_only

        def failed(err):
            if isinstance(err, OfflineError):
                self._on_messages_loaded(service.cached_messages(folder.name, PAGE_SIZE, oldest, unread_only))
            else:
                self._on_list_error(err)

        ok, bad = self._track(self._guard(token, self._on_messages_loaded), self._guard(token, failed))
        service.call("list_messages", folder.name, PAGE_SIZE, oldest, unread_only, done=ok, error=bad)

    def _on_message_selected(self, _listbox, row):
        if row is not None:
            self._open_row(row)

    def _on_message_activated(self, _listbox, row):
        if self.inner.get_collapsed():
            self.inner.set_show_content(True)

    def _open_row(self, row):
        summary = row.summary
        if self.current_summary is summary and self.current_content is not None:
            return
        self._message_token += 1
        token = self._message_token
        self.current_summary = summary
        self.current_content = None
        self.html_banner.set_revealed(False)
        self.message_stack.set_visible_child_name("loading")
        ok, bad = self._track(self._guard(token, self._on_content, "message"),
                              self._guard(token, self._on_content_error, "message"))
        self.current_service.fetch_message(self.current_folder.name, summary.uid, done=ok, error=bad)
        if summary.unread:
            self._set_flag(summary, "\\Seen", True)
        self._update_actions()

    def _on_content(self, content):
        self.current_content = content
        self._render_content()
        self.message_stack.set_visible_child_name("message")
        self._update_actions()

    def _message_key(self):
        return (self.current_service.account.id, self.current_folder.name, self.current_summary.uid)

    def _render_content(self):
        content = self.current_content
        allowed = self._images_allowed()
        mode = self.message_view.show(self.current_summary, content,
                                      prefer_html=not self.plain_text, allow_remote=allowed)
        if mode == "html" and content.remote_content and not allowed:
            self.html_banner.set_title(_("Remote images are hidden to protect your privacy"))
            self.html_banner.set_button_label(_("Show Images"))
            self.html_banner.set_action_name("win.show-remote")
            self.html_banner.set_revealed(True)
        elif not HTML_AVAILABLE and content.html and not content.has_plain:
            self.html_banner.set_title(_("This message is shown as plain text"))
            self.html_banner.set_button_label(_("Open in Browser"))
            self.html_banner.set_action_name("win.open-in-browser")
            self.html_banner.set_revealed(True)
        else:
            self.html_banner.set_revealed(False)

    def _images_allowed(self) -> bool:
        if self.get_application().settings.always_show_images:
            return True
        if self._message_key() in self._remote_allowed:
            return True
        message_id = self.current_summary.message_id
        return bool(message_id) and self.get_application().cache.remote_images_allowed(
            self.current_service.account.id, message_id)

    def _show_remote(self):
        if self.current_content is None:
            return
        key = self._message_key()
        self._remote_allowed.add(key)  # this session, cached or not
        account, folder, uid = key
        message_id = self.current_summary.message_id
        cache = self.get_application().cache
        if message_id and cache.is_cached(account, folder, uid):
            cache.allow_remote_images(account, message_id)  # remembered while the email stays cached
        self._render_content()

    def _on_plain_text_changed(self, action, value):
        action.set_state(value)
        self.plain_text = value.get_boolean()
        if self.current_content is not None:
            self._render_content()

    def _on_content_error(self, err):
        offline = isinstance(err, OfflineError)
        self.message_error.set_icon_name("network-offline-symbolic" if offline else "dialog-warning-symbolic")
        self.message_error.set_description(describe_error(err))
        self.message_stack.set_visible_child_name("error")

    def _show_message(self, row):
        if row is not None:
            self.message_list.select_row(row)
            return
        self._message_token += 1
        self.current_summary = None
        self.current_content = None
        self.message_list.unselect_all()
        self.html_banner.set_revealed(False)
        self.message_stack.set_visible_child_name("empty")
        self._update_actions()

    # ------------------------------------------------------------ flags

    def _set_flag(self, summary, flag, value):
        service, folder = self.current_service, self.current_folder
        if (flag in summary.flags) == value:
            return
        (summary.flags.add if value else summary.flags.discard)(flag)
        row = self.rows.get(summary.uid)
        if row:
            row.update()
        if flag == "\\Seen":
            self._adjust_unread(service, folder, -1 if value else 1)
        self._update_actions()

        def failed(err):
            (summary.flags.discard if value else summary.flags.add)(flag)
            if row:
                row.update()
            if flag == "\\Seen":
                self._adjust_unread(service, folder, 1 if value else -1)
            self._update_actions()
            self.toast(describe_error(err))

        ok, bad = self._track(lambda queued: self._update_offline_banner(), failed)
        service.call("set_flags", folder.name, [summary.uid], flag, value, done=ok, error=bad)

    def _adjust_unread(self, service, folder, delta):
        row = self.folder_rows.get((service, folder.name))
        if row:
            row.folder.unread = max(0, row.folder.unread + delta)
            row.update()

    def _toggle_flag(self, flag):
        summary = self.current_summary
        if summary is None:
            return
        self._set_flag(summary, flag, flag not in summary.flags)

    # ------------------------------------------------------------- moves

    def _move_current(self, role):
        summary = self.current_summary
        if summary is None or self.get_visible_dialog() is not None:
            return
        service, folder = self.current_service, self.current_folder
        if role == "trash" and folder.role == "trash":
            self._confirm_delete(summary)
            return
        destination = service.folder_for_role(role)
        if role == "archive" and destination is None:
            destination = service.folder_for_role("all")
        if destination is None:
            self.toast(_("This account has no Trash folder.") if role == "trash"
                       else _("This account has no Archive folder."))
            return
        self._remove_row(summary)
        if summary.unread:
            self._adjust_unread(service, folder, -1)
        ok, bad = self._track(lambda _r: None, self._on_change_failed)
        service.call("move", folder.name, [summary.uid], destination.name, done=ok, error=bad)
        self.toast(_("Message archived") if role == "archive" else _("Message moved to Trash"))

    def _confirm_delete(self, summary):
        alert = Adw.AlertDialog(heading=_("Delete Message Permanently?"),
                                body=_("This message will be deleted and cannot be recovered."))
        alert.add_response("cancel", _("_Cancel"))
        alert.add_response("delete", _("_Delete"))
        alert.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")

        def response(_alert, answer):
            if answer != "delete":
                return
            service, folder = self.current_service, self.current_folder
            self._remove_row(summary)
            ok, bad = self._track(lambda _r: None, self._on_change_failed)
            service.call("delete", folder.name, [summary.uid], done=ok, error=bad)

        alert.connect("response", response)
        alert.present(self)

    def _on_change_failed(self, err):
        self.toast(describe_error(err))
        self._reload_folder()

    def _remove_row(self, summary):
        row = self.rows.pop(summary.uid, None)
        if row is None:
            return
        neighbour = None
        if not self.inner.get_collapsed():
            index = row.get_index()
            neighbour = self.message_list.get_row_at_index(index + 1) or self.message_list.get_row_at_index(index - 1)
        was_current = self.current_summary is summary
        self.message_list.remove(row)
        if was_current:
            if neighbour is not None:
                self._show_message(neighbour)
            else:
                self._show_message(None)
                self.inner.set_show_content(False)
        if not self.rows and not self._has_more:
            self.list_stack.set_visible_child_name("empty")

    # =========================================================== refresh

    def _schedule_refresh(self):
        """(Re)start the automatic checks from the "Check for New Mail" setting; 0 turns them off."""
        for source in (self._sync_timer, self._refresh_timer):
            if source:
                GLib.source_remove(source)
        self._sync_timer = self._refresh_timer = 0
        interval = self.get_application().settings.refresh_interval
        if interval <= 0:
            return  # only the Refresh button checks
        self._sync_timer = GLib.timeout_add_seconds(interval, self._on_folder_sync_timer)
        self._refresh_timer = GLib.timeout_add_seconds(max(interval, MIN_FULL_REFRESH_SECONDS),
                                                       self._on_refresh_timer)

    def _on_refresh_timer(self):
        self.refresh()
        return GLib.SOURCE_CONTINUE

    def refresh(self, user=False):
        if self.work_offline:
            if user:
                self.toast(_("Turn off Work Offline to check for new mail."))
            return
        if self._unreachable and self._network_ok():
            # Try the server again; failures mark us offline once more.
            self._unreachable = False
            self._sync_online_state()
            self._flush_all()
        if not self._is_online():
            return
        for service in self.services:
            if not service.folders:
                self._load_folders(service)
                continue
            ok, bad = self._track(lambda counts, s=service: self._apply_counts(s, counts))
            service.call("unread_counts", [f.name for f in service.folders if f.counts_unread],
                         done=ok, error=bad)
        self._sync_folder(user=user)
        self._check_new_mail()

    def _on_folder_sync_timer(self):
        if self._is_online():
            if self.get_visible():
                self._sync_folder(quiet=True)
            self._check_new_mail()
        return GLib.SOURCE_CONTINUE

    def _sync_folder(self, user=False, quiet=False):
        """Bring the selected folder up to date without disturbing the open message.

        New mail is inserted above (the list is ordered newest first), changed flags are
        updated, and messages deleted elsewhere disappear. Selection and scroll stay put.
        """
        if self.current_folder is None or self._searching or self._syncing:
            return
        state = self.list_stack.get_visible_child_name()
        if state == "loading":
            return  # the folder is being loaded right now
        if state == "error":
            if not quiet:
                self._reload_folder()
            return
        service, folder = self.current_service, self.current_folder
        token = self._list_token
        known = list(self.rows)
        self._syncing = True

        def synced(result):
            self._syncing = False
            if token == self._list_token:
                self._on_synced(known, result)

        def failed(err):
            self._syncing = False
            if token == self._list_token and user and not isinstance(err, OfflineError):
                self.toast(describe_error(err))

        ok, bad = self._track(synced, failed, quiet=quiet)
        service.call("sync", folder.name, known, done=ok, error=bad)
        if quiet and folder.counts_unread:
            ok, bad = self._track(lambda counts: self._apply_counts(service, counts), quiet=True)
            service.call("unread_counts", [folder.name], done=ok, error=bad)

    def _on_synced(self, known, result):
        new, flags = result
        new = [s for s in new if s.uid not in self.rows and (not self.unread_only or s.unread)]
        adj = self._list_adjustment
        if new and adj.get_value() > 0:
            # Scrolled down: keep what the user is looking at in place while rows are added above.
            old_upper, old_value = adj.get_upper(), adj.get_value()

            def keep_position(adjustment):
                adjustment.disconnect(handler)
                adjustment.set_value(old_value + (adjustment.get_upper() - old_upper))

            handler = adj.connect("changed", keep_position)
        for summary in new:
            self._add_summary(summary)
        for uid in known:
            row = self.rows.get(uid)
            if row is None:
                continue
            if uid not in flags:
                self._remove_row(row.summary)
            elif flags[uid] != row.summary.flags:
                row.summary.flags = flags[uid]
                row.update()
        if self.rows:
            self.list_stack.set_visible_child_name("list")
        self._update_actions()

    # ============================================================ search

    def _on_search_changed(self, entry):
        if self.current_folder is None:
            return
        query = entry.get_text().strip()
        if not query:
            if self._searching:
                self._searching = False
                self._show_message(None)
                self._reload_folder()
            return
        self._searching = True
        self._list_token += 1
        token = self._list_token
        self._clear_messages()
        self._show_message(None)
        self.list_empty.set_icon_name("system-search-symbolic")
        self.list_empty.set_title(_("No Results"))
        self.list_stack.set_visible_child_name("loading")
        unread_only = self.unread_only
        ok, bad = self._track(self._guard(token, lambda summaries: self._on_messages_loaded(
            ([s for s in summaries if s.unread or not unread_only], False))),
                              self._guard(token, self._on_list_error))
        self.current_service.call("search", self.current_folder.name, query, 200, done=ok, error=bad)

    # =========================================================== compose

    def compose(self, signature="new", **fields):
        """Open the compose dialog. signature: "new" for fresh messages, "reply" for replies and forwards."""
        if not self.services:
            return
        settings = self.get_application().settings
        text = (settings.signature_new if signature == "new" else settings.signature_reply).strip("\n")
        if text.strip():
            block = text  # just the signature, separated from the text by a blank line
            body = fields.get("body", "")
            if signature == "new":
                fields["body"] = (body.rstrip("\n") + "\n\n" if body.strip() else "\n\n") + block + "\n"
            else:
                fields["body"] = "\n\n" + block + "\n" + body  # above the quoted message
        service = self.current_service or self.services[0]
        ComposeDialog(self, self.services, service, **fields).present(self)

    def _compose_mailto(self, uri):
        parts = urlsplit(uri)
        query = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
        self.compose(to=unquote(parts.path), cc=query.get("cc", ""),
                     subject=query.get("subject", ""), body=query.get("body", ""))

    def _reply(self, mode):
        summary, content = self.current_summary, self.current_content
        if summary is None or content is None:
            return
        me = self.current_service.account.email.lower()
        sender = summary.sender.format() if summary.sender else _("Unknown Sender")
        date = format_full_date(summary.timestamp)

        if mode == "forward":
            header = "\n".join([
                "-------- " + _("Forwarded Message") + " --------",
                _("From: {value}").format(value=sender),
                _("Date: {value}").format(value=date),
                _("Subject: {value}").format(value=summary.subject),
                _("To: {value}").format(value=", ".join(a.format() for a in summary.to)),
            ])
            self.compose(signature="reply", subject=_prefixed(summary.subject, "Fwd:"), body=f"\n\n{header}\n\n{content.text}",
                         attachments=content.attachments, title=_("Forward"))
            return

        if self.current_folder.role == "sent":
            targets = list(summary.to)
        else:
            targets = list(summary.reply_to or ([summary.sender] if summary.sender else []))
        cc = []
        if mode == "reply-all":
            seen = {a.email.lower() for a in targets} | {me}
            for address in summary.to:
                if address.email.lower() not in seen:
                    targets.append(address)
                    seen.add(address.email.lower())
            for address in summary.cc:
                if address.email.lower() not in seen:
                    cc.append(address)
                    seen.add(address.email.lower())

        quoted = "\n".join("> " + line if line else ">" for line in content.text.splitlines())
        attribution = _("On {date}, {sender} wrote:").format(
            date=date, sender=summary.sender.label if summary.sender else _("Unknown Sender"))
        references = " ".join(filter(None, [summary.references or summary.in_reply_to, summary.message_id]))
        self.compose(
            signature="reply",
            to=", ".join(a.format() for a in targets),
            cc=", ".join(a.format() for a in cc),
            subject=_prefixed(summary.subject, "Re:"),
            body=f"\n\n{attribution}\n{quoted}\n",
            in_reply_to=summary.message_id,
            references=references,
            title=_("Reply"),
        )

    # ================================================== activity + offline

    def _track(self, done=None, error=None, quiet=False):
        """Wrap callbacks so the header spinners show while the call runs.

        quiet: for routine background work; the spinner only appears if it takes over a second.
        """
        state = {"shown": False, "finished": False}

        def show():
            if not state["finished"]:
                state["shown"] = True
                self._busy += 1
                self._update_activity()
            return GLib.SOURCE_REMOVE

        def finish():
            state["finished"] = True
            if state["shown"]:
                self._busy -= 1
                self._update_activity()

        if quiet:
            GLib.timeout_add(1000, show)
        else:
            show()

        def ok(value):
            finish()
            if done:
                done(value)

        def bad(err):
            finish()
            self._note_offline(err)
            if error:
                error(err)

        return ok, bad

    def _update_activity(self):
        for spinner in self._activity:
            spinner.set_visible(self._busy > 0)

    def _network_ok(self) -> bool:
        return (self._monitor.get_network_available()
                and self._monitor.get_connectivity() != Gio.NetworkConnectivity.LOCAL)

    def _is_online(self) -> bool:
        return not self.work_offline and not self._unreachable and self._network_ok()

    def _note_offline(self, err):
        if isinstance(err, OfflineError) and self._is_online():
            self._unreachable = True
            self._sync_online_state()

    def _sync_online_state(self):
        online = self._is_online()
        for service in self.services:
            service.online = online
        self._update_offline_banner()

    def _update_offline_banner(self):
        online = self._is_online()
        waiting = sum(s.outbox_count() for s in self.services)
        if online and not waiting:
            self.offline_banner.set_revealed(False)
            return
        if self.work_offline:
            title, button = _("Working offline"), _("Go Online")
        elif not self._network_ok():
            title, button = _("No network connection"), ""
        elif not online:
            title, button = _("Can’t reach the mail server"), _("Try Again")
        else:
            title, button = "", _("Send Now")
        if waiting:
            outbox = _("{count} in Outbox").format(count=waiting)
            title = f"{title} · {outbox}" if title else outbox
        self.offline_banner.set_title(GLib.markup_escape_text(title))
        self.offline_banner.set_button_label(button or None)
        self.offline_banner.set_revealed(True)

    def _on_offline_banner_clicked(self, _banner):
        if self.work_offline:
            self.change_action_state("work-offline", GLib.Variant.new_boolean(False))
        else:
            self._unreachable = False
            self._sync_online_state()
            self._flush_all()
            self.refresh(user=True)

    def _on_work_offline_changed(self, action, value):
        action.set_state(value)
        self.work_offline = value.get_boolean()
        self._sync_online_state()
        if not self.work_offline:
            self._unreachable = False
            self._sync_online_state()
            self._flush_all()
            self.refresh(user=True)

    def _on_network_changed(self, _monitor, _available):
        was_online = all(s.online for s in self.services) if self.services else False
        if self._network_ok():
            self._unreachable = False
        self._sync_online_state()
        if self._is_online() and not was_online:
            self._flush_all()
            self.refresh()

    def _flush_all(self):
        if not self._is_online():
            return
        for service in self.services:
            ok, bad = self._track(lambda result, s=service: self._on_flushed(s, result))
            service.call("flush", done=ok, error=bad)

    def _on_flushed(self, service, result):
        changes, sent, errors = result
        if sent:
            self.toast((_("Sent {count} message from the Outbox") if sent == 1
                        else _("Sent {count} messages from the Outbox")).format(count=sent))
        for err in errors[:1]:
            self.toast(_("Could not send a queued message: {error}").format(error=describe_error(err)))
        self._update_offline_banner()

    # ========================================================== contacts

    def _contact_sources(self):
        if self.get_application().demo:
            return [DemoContactSource()]
        return load_sources(self.goa_client) if self.goa_client is not None else []

    def refresh_contacts(self, user=False):
        if not self._is_online():
            if user:
                self.toast(_("You are offline. Showing saved contacts."))
            return

        def done(errors):
            if user and errors:
                _source, err = errors[0]
                self.toast(_("Could not update contacts: {error}").format(error=describe_error(err)))

        self.get_application().contacts.refresh(self._contact_sources(), done)

    def _address_status(self, email):
        book = self.get_application().contacts
        if book.lookup(email) is not None:
            return "known"
        return "addable" if book.writable_sources() else "other"

    def _on_address_action(self, action, address):
        from .contact_editor import ContactEditor
        book = self.get_application().contacts
        if action == "edit":
            contact = book.lookup(address.email)
            if contact is not None:
                ContactEditor(self, book, contact, self._is_online()).present(self)
        elif action == "add":
            ContactEditor(self, book, None, self._is_online(), prefill_email=address.email,
                          prefill_name=address.name).present(self)
        elif action == "compose":
            self.compose(to=address.format())
        elif action == "copy":
            self.get_clipboard().set(address.format())
            self.toast(_("Address copied"))

    def show_contacts(self):
        ContactsDialog(self, self.get_application().contacts,
                       on_refresh=lambda: self.refresh_contacts(user=True),
                       is_online=self._is_online).present(self)

    # ============================================================= misc

    def _open_in_browser(self):
        content = self.current_content
        if content is None:
            return
        source = content.html or "<pre>" + html.escape(content.text) + "</pre>"
        # Block scripts; the browser still loads remote images because the user asked for the full view.
        guard = '<meta http-equiv="Content-Security-Policy" content="script-src \'none\'; object-src \'none\'">'
        folder = os.path.join(GLib.get_user_cache_dir(), "brev", "messages")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"message-{self.current_summary.uid}.html")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('<!doctype html><meta charset="utf-8">' + guard + source)
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self, None, None)

    def _save_message(self):
        content = self.current_content
        if content is None:
            return
        name = (self.current_summary.subject or "message").replace("/", "-")[:80] + ".eml"

        def finished(dialog, result):
            try:
                target = dialog.save_finish(result)
            except GLib.Error:
                return
            target.replace_contents(content.raw, None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)

        Gtk.FileDialog(initial_name=name).save(self, None, finished)
