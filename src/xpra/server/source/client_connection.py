# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010-2018 Antoine Martin <antoine@devloop.org.uk>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from threading import Event

from xpra.log import Logger
log = Logger("server")
elog = Logger("encoding")
keylog = Logger("keyboard")
mouselog = Logger("mouse")
timeoutlog = Logger("timeout")
proxylog = Logger("proxy")
avsynclog = Logger("av-sync")
statslog = Logger("stats")
notifylog = Logger("notify")
netlog = Logger("network")
bandwidthlog = Logger("bandwidth")


from xpra.server.source.source_stats import GlobalPerformanceStatistics
from xpra.server.source.audio_mixin import AudioMixin
from xpra.server.source.mmap_connection import MMAP_Connection
from xpra.server.source.fileprint_mixin import FilePrintMixin
from xpra.server.source.clipboard_connection import ClipboardConnection
from xpra.server.source.networkstate_mixin import NetworkStateMixin
from xpra.server.source.clientinfo_mixin import ClientInfoMixin
from xpra.server.source.dbus_mixin import DBUS_Mixin
from xpra.server.source.windows_mixin import WindowsMixin
from xpra.server.source.encodings_mixin import EncodingsMixin
from xpra.make_thread import start_thread
from xpra.os_util import monotonic_time
from xpra.util import merge_dicts, flatten_dict, notypedict, get_screen_info, envint, envbool, \
    AtomicInteger, XPRA_IDLE_NOTIFICATION_ID

GRACE_PERCENT = envint("XPRA_GRACE_PERCENT", 90)
AV_SYNC_DELTA = envint("XPRA_AV_SYNC_DELTA", 0)
BANDWIDTH_DETECTION = envbool("XPRA_BANDWIDTH_DETECTION", True)

counter = AtomicInteger()


"""
This class mediates between the server class (which only knows about actual window objects and display server events)
and the client specific WindowSource instances (which only know about window ids
and manage window pixel compression).
It sends messages to the client via its 'protocol' instance (the network connection),
directly for a number of cases (cursor, sound, notifications, etc)
or on behalf of the window sources for pixel data.

Strategy: if we have 'ordinary_packets' to send, send those.
When we don't, then send packets from the 'packet_queue'. (compressed pixels or clipboard data)
See 'next_packet'.

The UI thread calls damage(), which goes into WindowSource and eventually (batching may be involved)
adds the damage pixels ready for processing to the encode_work_queue,
items are picked off by the separate 'encode' thread (see 'encode_loop')
and added to the damage_packet_queue.
"""
class ClientConnection(AudioMixin, MMAP_Connection, ClipboardConnection, FilePrintMixin, NetworkStateMixin, ClientInfoMixin, DBUS_Mixin, WindowsMixin, EncodingsMixin):

    def __init__(self, protocol, disconnect_cb, idle_add, timeout_add, source_remove, setting_changed,
                 idle_timeout, idle_timeout_cb, idle_grace_timeout_cb,
                 socket_dir, unix_socket_paths, log_disconnect, dbus_control,
                 get_transient_for, get_focus, get_cursor_data_cb,
                 get_window_id,
                 window_filters,
                 file_transfer,
                 supports_mmap, mmap_filename, min_mmap_size,
                 bandwidth_limit,
                 av_sync,
                 core_encodings, encodings, default_encoding, scaling_control,
                 sound_properties,
                 sound_source_plugin,
                 supports_speaker, supports_microphone,
                 speaker_codecs, microphone_codecs,
                 default_quality, default_min_quality,
                 default_speed, default_min_speed):
        log("ServerSource%s", (protocol, disconnect_cb, idle_add, timeout_add, source_remove, setting_changed,
                 idle_timeout, idle_timeout_cb, idle_grace_timeout_cb,
                 socket_dir, unix_socket_paths, log_disconnect, dbus_control,
                 get_transient_for, get_focus,
                 get_window_id,
                 window_filters,
                 file_transfer,
                 supports_mmap, mmap_filename, min_mmap_size,
                 bandwidth_limit,
                 av_sync,
                 core_encodings, encodings, default_encoding, scaling_control,
                 sound_properties,
                 sound_source_plugin,
                 supports_speaker, supports_microphone,
                 speaker_codecs, microphone_codecs,
                 default_quality, default_min_quality,
                 default_speed, default_min_speed))
        AudioMixin.__init__(self, sound_properties, sound_source_plugin,
                 supports_speaker, supports_microphone, speaker_codecs, microphone_codecs)
        MMAP_Connection.__init__(self, supports_mmap, mmap_filename, min_mmap_size)
        ClipboardConnection.__init__(self)
        FilePrintMixin.__init__(self, file_transfer)
        NetworkStateMixin.__init__(self)
        ClientInfoMixin.__init__(self)
        DBUS_Mixin.__init__(self, dbus_control)
        WindowsMixin.__init__(self, get_transient_for, get_focus, get_cursor_data_cb, get_window_id, window_filters)
        EncodingsMixin.__init__(self, core_encodings, encodings, default_encoding, scaling_control, default_quality, default_min_quality, default_speed, default_min_speed)
        global counter
        self.counter = counter.increase()
        self.close_event = Event()
        self.ordinary_packets = []
        self.protocol = protocol
        self.disconnect = disconnect_cb
        self.idle_add = idle_add
        self.timeout_add = timeout_add
        self.source_remove = source_remove
        self.setting_changed = setting_changed
        self.idle = False
        self.idle_timeout = idle_timeout
        self.idle_timeout_cb = idle_timeout_cb
        self.idle_grace_timeout_cb = idle_grace_timeout_cb
        #grace duration is at least 10 seconds:
        self.idle_grace_duration = max(10, int(self.idle_timeout*(100-GRACE_PERCENT)//100))
        self.idle_timer = None
        self.idle_grace_timer = None
        self.schedule_idle_grace_timeout()
        self.schedule_idle_timeout()
        self.socket_dir = socket_dir
        self.unix_socket_paths = unix_socket_paths
        self.log_disconnect = log_disconnect
        # network constraints:
        self.server_bandwidth_limit = bandwidth_limit

        self.av_sync = av_sync
        self.av_sync_delay = 0
        self.av_sync_delay_total = 0
        self.av_sync_delta = AV_SYNC_DELTA

        self.icc = None
        self.display_icc = {}

        self.connection_time = monotonic_time()

        #these statistics are shared by all WindowSource instances:
        self.statistics = GlobalPerformanceStatistics()
        self.last_user_event = monotonic_time()

        self.init_vars()

        # ready for processing:
        protocol.set_packet_source(self.next_packet)
        self.encode_thread = start_thread(self.encode_loop, "encode")


    def __repr__(self):
        return  "%s(%i : %s)" % (type(self).__name__, self.counter, self.protocol)

    def init_vars(self):
        self.hello_sent = False

        self.info_namespace = False
        self.send_notifications = False
        self.send_notifications_actions = False
        self.notification_callbacks = {}
        self.randr_notify = False
        self.share = False
        self.lock = False
        self.desktop_size = None
        self.desktop_mode_size = None
        self.desktop_size_unscaled = None
        self.desktop_size_server = None
        self.screen_sizes = ()
        self.desktops = 1
        self.desktop_names = ()
        self.control_commands = ()
        self.double_click_time  = -1
        self.double_click_distance = -1, -1
        self.bandwidth_limit = self.server_bandwidth_limit
        self.soft_bandwidth_limit = self.bandwidth_limit
        self.bandwidth_warnings = True
        self.bandwidth_warning_time = 0
        #what we send back in hello packet:
        self.ui_client = True
        self.wants_aliases = True
        self.wants_encodings = True
        self.wants_versions = True
        self.wants_features = True
        self.wants_display = True
        self.wants_events = False
        self.wants_default_cursor = False

        self.keyboard_config = None


    def is_closed(self):
        return self.close_event.isSet()

    def close(self):
        log("%s.close()", self)
        for c in ClientConnection.__bases__:
            c.cleanup(self)
        self.close_event.set()
        #it is now safe to add the end of queue marker:
        #(all window sources will have stopped queuing data)
        self.encode_work_queue.put(None)
        self.cancel_recalculate_timer()
        self.protocol = None


    def update_bandwidth_limits(self):
        if self.mmap_size>0:
            return
        #calculate soft bandwidth limit based on send congestion data:
        bandwidth_limit = 0
        if BANDWIDTH_DETECTION:
            bandwidth_limit = self.statistics.avg_congestion_send_speed
            bandwidthlog("avg_congestion_send_speed=%s", bandwidth_limit)
            if bandwidth_limit>20*1024*1024:
                #ignore congestion speed if greater 20Mbps
                bandwidth_limit = 0
        if self.bandwidth_limit>0:
            #command line options could overrule what we detect?
            bandwidth_limit = min(self.bandwidth_limit, bandwidth_limit)
        self.soft_bandwidth_limit = bandwidth_limit
        bandwidthlog("update_bandwidth_limits() bandwidth_limit=%s, soft bandwidth limit=%s", self.bandwidth_limit, bandwidth_limit)
        if self.soft_bandwidth_limit<=0:
            return
        #figure out how to distribute the bandwidth amongst the windows,
        #we use the window size,
        #(we should actually use the number of bytes actually sent: framerate, compression, etc..)
        window_weight = {}
        for wid, ws in self.window_sources.items():
            weight = 0
            if not ws.suspended:
                ww, wh = ws.window_dimensions
                #try to reserve bandwidth for at least one screen update,
                #and add the number of pixels damaged:
                weight = ww*wh + ws.statistics.get_damage_pixels()
            window_weight[wid] = weight
        bandwidthlog("update_bandwidth_limits() window weights=%s", window_weight)
        total_weight = sum(window_weight.values())
        for wid, ws in self.window_sources.items():
            weight = window_weight.get(wid)
            if weight is not None:
                ws.bandwidth_limit = max(1, bandwidth_limit*weight//total_weight)


    def user_event(self):
        timeoutlog("user_event()")
        self.last_user_event = monotonic_time()
        self.schedule_idle_grace_timeout()
        self.schedule_idle_timeout()
        if self.idle:
            self.no_idle()
        try:
            self.notification_callbacks.pop(XPRA_IDLE_NOTIFICATION_ID)
        except KeyError:
            pass
        else:
            self.notify_close(XPRA_IDLE_NOTIFICATION_ID)
        

    def schedule_idle_timeout(self):
        timeoutlog("schedule_idle_timeout() idle_timer=%s, idle_timeout=%s", self.idle_timer, self.idle_timeout)
        if self.idle_timer:
            self.source_remove(self.idle_timer)
            self.idle_timer = None
        if self.idle_timeout>0:
            self.idle_timer = self.timeout_add(self.idle_timeout*1000, self.idle_timedout)

    def schedule_idle_grace_timeout(self):
        timeoutlog("schedule_idle_grace_timeout() grace timer=%s, idle_timeout=%s", self.idle_grace_timer, self.idle_timeout)
        if self.idle_grace_timer:
            self.source_remove(self.idle_grace_timer)
            self.idle_grace_timer = None
        if self.idle_timeout>0 and not self.is_closed():
            grace = self.idle_timeout - self.idle_grace_duration
            self.idle_grace_timer = self.timeout_add(max(0, int(grace*1000)), self.idle_grace_timedout)

    def idle_grace_timedout(self):
        self.idle_grace_timer = None
        timeoutlog("idle_grace_timedout() callback=%s", self.idle_grace_timeout_cb)
        self.idle_grace_timeout_cb(self)

    def idle_timedout(self):
        self.idle_timer = None
        timeoutlog("idle_timedout() callback=%s", self.idle_timeout_cb)
        self.idle_timeout_cb(self)
        if not self.is_closed():
            self.schedule_idle_timeout()


    def parse_hello(self, c):
        self.ui_client = c.boolget("ui_client", True)
        self.wants_encodings = c.boolget("wants_encodings", self.ui_client)
        self.wants_display = c.boolget("wants_display", self.ui_client)
        self.wants_events = c.boolget("wants_events", False)
        self.wants_aliases = c.boolget("wants_aliases", True)
        self.wants_versions = c.boolget("wants_versions", True)
        self.wants_features = c.boolget("wants_features", True)
        self.wants_default_cursor = c.boolget("wants_default_cursor", False)

        ClientInfoMixin.parse_client_caps(self, c)
        FilePrintMixin.parse_client_caps(self, c)
        AudioMixin.parse_client_caps(self, c)
        MMAP_Connection.parse_client_caps(self, c)
        WindowsMixin.parse_client_caps(self, c)
        EncodingsMixin.parse_client_caps(self, c)

        #general features:
        self.info_namespace = c.boolget("info-namespace")
        self.send_notifications = c.boolget("notifications")
        self.send_notifications_actions = c.boolget("notifications.actions")
        self.randr_notify = c.boolget("randr_notify")
        self.share = c.boolget("share")
        self.lock = c.boolget("lock")
        self.control_commands = c.strlistget("control_commands")
        self.double_click_time = c.intget("double_click.time")
        self.double_click_distance = c.intpair("double_click.distance")
        bandwidth_limit = c.intget("bandwidth-limit", 0)
        if self.server_bandwidth_limit<=0:
            self.bandwidth_limit = bandwidth_limit
        else:
            self.bandwidth_limit = min(self.server_bandwidth_limit, bandwidth_limit)
        bandwidthlog("server bandwidth-limit=%s, client bandwidth-limit=%s, value=%s", self.server_bandwidth_limit, bandwidth_limit, self.bandwidth_limit)

        self.desktop_size = c.intpair("desktop_size")
        if self.desktop_size is not None:
            w, h = self.desktop_size
            if w<=0 or h<=0 or w>=32768 or h>=32768:
                log.warn("ignoring invalid desktop dimensions: %sx%s", w, h)
                self.desktop_size = None
        self.desktop_mode_size = c.intpair("desktop_mode_size")
        self.desktop_size_unscaled = c.intpair("desktop_size.unscaled")
        self.set_screen_sizes(c.listget("screen_sizes"))
        self.set_desktops(c.intget("desktops", 1), c.strlistget("desktop.names"))

        self.icc = c.dictget("icc")
        self.display_icc = c.dictget("display-icc")

        av_sync = c.boolget("av-sync")
        self.set_av_sync_delay(int(self.av_sync and av_sync) * c.intget("av-sync.delay.default", 150))
        avsynclog("av-sync: server=%s, client=%s, total=%s", self.av_sync, av_sync, self.av_sync_delay_total)
        log("cursors=%s (encodings=%s), bell=%s, notifications=%s", self.send_cursors, self.cursor_encodings, self.send_bell, self.send_notifications)
        log("client uuid %s", self.uuid)

        cinfo = self.get_connect_info()
        for i,ci in enumerate(cinfo):
            log.info("%s%s", ["", " "][int(i>0)], ci)
        if self.client_proxy:
            from xpra.version_util import version_compat_check
            msg = version_compat_check(self.proxy_version)
            if msg:
                proxylog.warn("Warning: proxy version may not be compatible: %s", msg)
        self.update_connection_data(c.dictget("connection-data"))

        #keyboard is now injected into this class, default to undefined:
        self.keyboard_config = None

        if self.mmap_size>0:
            log("mmap enabled, ignoring bandwidth-limit")
            self.bandwidth_limit = 0
        #adjust max packet size if file transfers are enabled:
        if self.file_transfer:
            self.protocol.max_packet_size = max(self.protocol.max_packet_size, self.file_size_limit*1024*1024)


    def startup_complete(self):
        log("startup_complete()")
        self.send("startup-complete")


    ######################################################################
    # a/v sync:
    def set_av_sync_delta(self, delta):
        avsynclog("set_av_sync_delta(%i)", delta)
        self.av_sync_delta = delta
        self.update_av_sync_delay_total()

    def set_av_sync_delay(self, v):
        #update all window sources with the given delay
        self.av_sync_delay = v
        self.update_av_sync_delay_total()

    def update_av_sync_delay_total(self):
        if self.av_sync:
            encoder_latency = self.get_sound_source_latency()
            self.av_sync_delay_total = min(1000, max(0, int(self.av_sync_delay) + self.av_sync_delta + encoder_latency))
            avsynclog("av-sync set to %ims (from client queue latency=%s, encoder latency=%s, delta=%s)", self.av_sync_delay_total, self.av_sync_delay, encoder_latency, self.av_sync_delta)
        else:
            avsynclog("av-sync support is disabled, setting it to 0")
            self.av_sync_delay_total = 0
        for ws in self.window_sources.values():
            ws.set_av_sync_delay(self.av_sync_delay_total)


    ######################################################################
    # keyboard :
    def set_layout(self, layout, variant, options):
        return self.keyboard_config.set_layout(layout, variant, options)

    def keys_changed(self):
        if self.keyboard_config:
            self.keyboard_config.compute_modifier_map()
            self.keyboard_config.compute_modifier_keynames()
        keylog("keys_changed() updated keyboard config=%s", self.keyboard_config)

    def make_keymask_match(self, modifier_list, ignored_modifier_keycode=None, ignored_modifier_keynames=None):
        if self.keyboard_config and self.keyboard_config.enabled:
            self.keyboard_config.make_keymask_match(modifier_list, ignored_modifier_keycode, ignored_modifier_keynames)

    def set_default_keymap(self):
        keylog("set_default_keymap() keyboard_config=%s", self.keyboard_config)
        if self.keyboard_config:
            self.keyboard_config.set_default_keymap()
        return self.keyboard_config


    def set_keymap(self, current_keyboard_config, keys_pressed, force=False, translate_only=False):
        keylog("set_keymap%s", (current_keyboard_config, keys_pressed, force, translate_only))
        if self.keyboard_config and self.keyboard_config.enabled:
            current_id = None
            if current_keyboard_config and current_keyboard_config.enabled:
                current_id = current_keyboard_config.get_hash()
            keymap_id = self.keyboard_config.get_hash()
            keylog("current keyboard id=%s, new keyboard id=%s", current_id, keymap_id)
            if force or current_id is None or keymap_id!=current_id:
                self.keyboard_config.keys_pressed = keys_pressed
                self.keyboard_config.set_keymap(translate_only)
                self.keyboard_config.owner = self.uuid
                current_keyboard_config = self.keyboard_config
            else:
                keylog.info("keyboard mapping already configured (skipped)")
                self.keyboard_config = current_keyboard_config
        return current_keyboard_config


    def get_keycode(self, client_keycode, keyname, modifiers):
        if self.keyboard_config is None:
            keylog.info("ignoring client key %s / %s since keyboard is not configured", client_keycode, keyname)
            return -1
        return self.keyboard_config.get_keycode(client_keycode, keyname, modifiers)


    def update_mouse(self, wid, x, y, rx, ry):
        mouselog("update_mouse(%s, %i, %i, %i, %i) current=%s, client=%i, show=%s", wid, x, y, rx, ry, self.mouse_last_position, self.counter, self.mouse_show)
        if not self.mouse_show:
            return
        if self.mouse_last_position!=(x, y, rx, ry):
            self.mouse_last_position = (x, y, rx, ry)
            self.send_async("pointer-position", wid, x, y, rx, ry)


    ######################################################################
    # network:
    def next_packet(self):
        """ Called by protocol.py when it is ready to send the next packet """
        packet, start_send_cb, end_send_cb, fail_cb, synchronous, have_more, will_have_more = None, None, None, None, True, False, False
        if not self.is_closed():
            if len(self.ordinary_packets)>0:
                packet, synchronous, fail_cb, will_have_more = self.ordinary_packets.pop(0)
            elif len(self.packet_queue)>0:
                packet, _, _, start_send_cb, end_send_cb, fail_cb, will_have_more = self.packet_queue.popleft()
            have_more = packet is not None and (len(self.ordinary_packets)>0 or len(self.packet_queue)>0)
        return packet, start_send_cb, end_send_cb, fail_cb, synchronous, have_more, will_have_more

    def send(self, *parts, **kwargs):
        """ This method queues non-damage packets (higher priority) """
        synchronous = kwargs.get("synchronous", True)
        will_have_more = kwargs.get("will_have_more", not synchronous)
        fail_cb = kwargs.get("fail_cb", None)
        p = self.protocol
        if p:
            self.ordinary_packets.append((parts, synchronous, fail_cb, will_have_more))
            p.source_has_more()

    def send_more(self, *parts, **kwargs):
        kwargs["will_have_more"] = True
        self.send(*parts, **kwargs)

    def send_async(self, *parts, **kwargs):
        kwargs["synchronous"] = False
        self.send(*parts, **kwargs)


    #client tells us about network connection status:
    def update_connection_data(self, data):
        netlog("update_connection_data(%s)", data)
        self.client_connection_data = data


    def send_hello(self, server_capabilities):
        capabilities = server_capabilities.copy()
        merge_dicts(capabilities, AudioMixin.get_caps(self))
        merge_dicts(capabilities, MMAP_Connection.get_caps(self))
        merge_dicts(capabilities, WindowsMixin.get_caps(self))
        merge_dicts(capabilities, EncodingsMixin.get_caps(self))
        #expose the "modifier_client_keycodes" defined in the X11 server keyboard config object,
        #so clients can figure out which modifiers map to which keys:
        if self.keyboard_config:
            mck = getattr(self.keyboard_config, "modifier_client_keycodes", None)
            if mck:
                capabilities["modifier_keycodes"] = mck
        self.send("hello", capabilities)
        self.hello_sent = True


    ######################################################################
    # info:
    def get_info(self):
        info = {
                "protocol"          : "xpra",
                "idle_time"         : int(monotonic_time()-self.last_user_event),
                "idle"              : self.idle,
                "desktop_size"      : self.desktop_size or "",
                "desktops"          : self.desktops,
                "desktop_names"     : self.desktop_names,
                "connection_time"   : int(self.connection_time),
                "elapsed_time"      : int(monotonic_time()-self.connection_time),
                "suspended"         : self.suspended,
                "counter"           : self.counter,
                "hello-sent"        : self.hello_sent,
                "bandwidth-limit"   : {
                    "setting"       : self.bandwidth_limit,
                    "actual"        : self.soft_bandwidth_limit,
                    }
                }
        if self.desktop_mode_size:
            info["desktop_mode_size"] = self.desktop_mode_size
        if self.client_connection_data:
            info["connection-data"] = self.client_connection_data
        if self.desktop_size_unscaled:
            info["desktop_size"] = {"unscaled" : self.desktop_size_unscaled}

        def addattr(k, name):
            v = getattr(self, name)
            if v is not None:
                info[k] = v
        for x in ("type", "platform", "release", "machine", "processor", "proxy", "wm_name", "session_type"):
            addattr(x, "client_"+x)
        #encoding:
        info.update({
                     "connection"       : self.protocol.get_info(),
                     "av-sync"          : {
                                           "client"     : {"delay"  : self.av_sync_delay},
                                           "total"      : self.av_sync_delay_total,
                                           "delta"      : self.av_sync_delta,
                                           },
                     })
        info.update(self.get_features_info())
        info.update(self.get_screen_info())
        merge_dicts(info, FilePrintMixin.get_info(self))
        merge_dicts(info, AudioMixin.get_info(self))
        merge_dicts(info, MMAP_Connection.get_info(self))
        merge_dicts(info, NetworkStateMixin.get_info(self))
        merge_dicts(info, ClientInfoMixin.get_info(self))
        merge_dicts(info, WindowsMixin.get_info(self))
        merge_dicts(info, EncodingsMixin.get_info(self))
        return info

    def get_screen_info(self):
        return get_screen_info(self.screen_sizes)

    def get_features_info(self):
        info = {}
        def battr(k, prop):
            info[k] = bool(getattr(self, prop))
        for prop in ("lock", "share", "randr_notify",
                     "system_tray"):
            battr(prop, prop)
        for prop, name in {
            "send_notifications" : "notifications",
            }.items():
            battr(name, prop)
        for prop, name in {
                           "file_size_limit"        : "file-size-limit",
                           }.items():
            info[name] = getattr(self, prop)
        dcinfo = info.setdefault("double_click", {})
        for prop, name in {
                           "double_click_time"      : "time",
                           "double_click_distance"  : "distance",
                           }.items():
            dcinfo[name] = getattr(self, prop)
        return info


    def send_info_response(self, info):
        if self.info_namespace:
            v = notypedict(info)
        else:
            v = flatten_dict(info)
        self.send_async("info-response", v)


    def send_setting_change(self, setting, value):
        if self.client_setting_change:
            self.send_more("setting-change", setting, value)


    def send_server_event(self, *args):
        if self.wants_events:
            self.send_more("server-event", *args)


    ######################################################################
    # notifications:
    """ Utility functions for mixins (makes notifications optional) """
    def may_notify(self, nid, summary, body, actions=[], hints={}, expire_timeout=10*1000, icon_name=None, user_callback=None):
        try:
            from xpra.platform.paths import get_icon_filename
            from xpra.notifications.common import parse_image_path
        except ImportError as e:
            notifylog("not sending notification: %s", e)
        else:
            icon_filename = get_icon_filename(icon_name)
            icon = parse_image_path(icon_filename)
            self.notify("", nid, "Xpra", 0, "", summary, body, actions, hints, expire_timeout, icon, user_callback)

    def notify(self, dbus_id, nid, app_name, replaces_nid, app_icon, summary, body, actions, hints, expire_timeout, icon, user_callback=None):
        args = (dbus_id, nid, app_name, replaces_nid, app_icon, summary, body, actions, hints, expire_timeout, icon)
        notifylog("notify%s types=%s", args, tuple(type(x) for x in args))
        if not self.send_notifications:
            notifylog("client %s does not support notifications", self)
            return False
        if self.suspended:
            notifylog("client %s is suspended, notification not sent", self)
            return False
        if user_callback:
            self.notification_callbacks[nid] = user_callback
        #this is one of the few places where we actually do care about character encoding:
        try:
            summary = summary.encode("utf8")
        except:
            summary = str(summary)
        try:
            body = body.encode("utf8")
        except:
            body = str(body)
        if self.hello_sent:
            #Warning: actions and hints are send last because they were added later (in version 2.3)
            self.send_async("notify_show", dbus_id, nid, app_name, replaces_nid, app_icon, summary, body, expire_timeout, icon, actions, hints)
        return True

    def notify_close(self, nid):
        if not self.send_notifications or self.suspended  or not self.hello_sent:
            return
        self.send_more("notify_close", nid)


    def set_deflate(self, level):
        self.send("set_deflate", level)


    ######################################################################
    # webcam:
    def send_webcam_ack(self, device, frame, *args):
        if self.hello_sent:
            self.send_async("webcam-ack", device, frame, *args)

    def send_webcam_stop(self, device, message):
        if self.hello_sent:
            self.send_async("webcam-stop", device, message)


    def send_client_command(self, *args):
        if self.hello_sent:
            self.send_more("control", *args)


    def rpc_reply(self, *args):
        if self.hello_sent:
            self.send("rpc-reply", *args)


    ######################################################################
    # screen and desktops:
    def set_screen_sizes(self, screen_sizes):
        self.screen_sizes = screen_sizes or []
        log("client screen sizes: %s", screen_sizes)

    def set_desktops(self, desktops, desktop_names):
        self.desktops = desktops or 1
        self.desktop_names = desktop_names or []

    def updated_desktop_size(self, root_w, root_h, max_w, max_h):
        log("updated_desktop_size%s randr_notify=%s, desktop_size=%s", (root_w, root_h, max_w, max_h), self.randr_notify, self.desktop_size)
        if not self.hello_sent:
            return False
        if self.randr_notify and (not self.desktop_size_server or tuple(self.desktop_size_server)!=(root_w, root_h)):
            self.desktop_size_server = root_w, root_h
            self.send("desktop_size", root_w, root_h, max_w, max_h)
            return True
        return False

    def show_desktop(self, show):
        if self.show_desktop_allowed and self.hello_sent:
            self.send_async("show-desktop", show)

    def refresh(self, wid, window, opts):
        if not self.can_send_window(window):
            return
        self.cancel_damage(wid)
        w, h = window.get_dimensions()
        self.damage(wid, window, 0, 0, w, h, opts)
