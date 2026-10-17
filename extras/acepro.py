"""Standalone KlipperScreen panel; installed as panels/acepro.py."""

import logging
import math
from pathlib import Path
import re
import time

import gi

gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gtk
from ks_includes.screen_panel import ScreenPanel


def rgb(color):
    color = str(color).upper()
    if not re.fullmatch(r'[0-9A-F]{6}', color):
        color = '808080'
    return tuple(int(color[i:i + 2], 16) / 255. for i in (0, 2, 4))


class Panel(ScreenPanel):
    def __init__(self, screen, title=None):
        super().__init__(screen, title or 'ACE Pro')
        self.css = Gtk.CssProvider()
        self.css.load_from_path(str(Path(__file__).resolve().parent.parent / 'templates/acepro.css'))
        self.snapshot = {}
        self.print_state = 'unknown'
        self.timer = None
        self.active = False
        self.epoch = 0
        self.sequence = 0
        self.pending_since = None
        self.dialog = None
        self.context = None
        self.dryer_dialog = None
        self.dryer_notice = None
        self.dialog_layers = {}
        self.gate_dialog = None
        self.gate_context = None
        self.dryer_selection = None
        self.dryer_pending = None
        self.dryer_error = ''
        self.suppressed = set()
        self.cards, self.icons, self.names, self.edit = [], [], [], []
        grid = Gtk.Grid(column_spacing=8, column_homogeneous=True, hexpand=True, vexpand=True)
        for gate in range(4):
            card = self._style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4), 'ace-card')
            button = self._style(Gtk.Button(hexpand=True, vexpand=True), 'ace-gate')
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            icon = self._swatch('FFFFFF', spool=True)
            spool = Gtk.Overlay(hexpand=True, vexpand=True)
            spool.add(icon)
            number = self._style(Gtk.Label(label=str(gate + 1)), 'ace-number')
            spool.add_overlay(number)
            spool.set_overlay_pass_through(number, True)
            spool.connect('get-child-position', self._number_position)
            name = self._style(Gtk.Label(label='Нет данных', wrap=True), 'ace-name')
            body.pack_start(spool, True, True, 0)
            body.pack_start(name, False, False, 0)
            button.add(body)
            button.connect('clicked', self._gate_clicked, gate)
            edit = self._style(self._gtk.Button(None, 'Тип и цвет'), 'ace-edit')
            edit.connect('clicked', self._edit_clicked, gate)
            card.pack_start(button, True, True, 0)
            card.pack_start(edit, False, False, 0)
            grid.attach(card, gate, 0, 1, 1)
            self.cards.append(button)
            self.icons.append(icon)
            self.names.append(name)
            self.edit.append(edit)
        footer = Gtk.Box(spacing=8, homogeneous=True)
        self.unload_button = self._style(self._gtk.Button(None, 'Извлечь'), 'ace-action')
        self.unload_button.connect('clicked', self._unload_clicked)
        footer.pack_start(self.unload_button, True, True, 0)
        self.dryer_button = self._style(Gtk.Button(hexpand=True), 'ace-action')
        dryer_body = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.dryer_icon = Gtk.DrawingArea()
        size = max(24, int(self._gtk.img_scale * .7))
        self.dryer_icon.set_size_request(size, size)
        self.dryer_icon.ace_color = 'FFFFFF'
        self.dryer_icon.connect('draw', self._draw_dryer)
        self.dryer_label = Gtk.Label(label='Сушилка')
        dryer_body.pack_start(self.dryer_icon, False, False, 0)
        dryer_body.pack_start(self.dryer_label, False, False, 0)
        self.dryer_button.add(dryer_body)
        self.dryer_button.connect('clicked', self._dryer_clicked)
        footer.pack_start(self.dryer_button, True, True, 0)
        self.message = self._style(Gtk.Label(label='Подключение к ACE Pro…', wrap=True), 'ace-message')
        self.message.set_no_show_all(True)
        heading = self._style(Gtk.Label(label='ACE Pro', xalign=0), 'ace-heading')
        self.content.pack_start(heading, False, False, 0)
        self.content.pack_start(grid, True, True, 0)
        self.content.pack_start(footer, False, False, 0)
        self.content.pack_start(self.message, False, False, 0)
        self._style(self.content)
        self._render()

    def _style(self, widget, name=None):
        context = widget.get_style_context()
        context.add_provider(self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        if name:
            context.add_class(name)
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                self._style(child)
        return widget

    @staticmethod
    def _number_position(overlay, number, allocation):
        width, height = overlay.get_allocated_width(), overlay.get_allocated_height()
        size = number.get_preferred_size()[1]
        allocation.width, allocation.height = size.width, size.height
        allocation.x = (width - size.width) // 2
        allocation.y = max(0, int(height / 2. - min(width, height) * .4))
        return True

    def activate(self):
        self.active = True
        self.epoch += 1
        self.pending_since = None
        self.snapshot = {}
        self.dryer_pending = None
        self.dryer_error = ''
        self._poll()
        if self.timer is None:
            self.timer = GLib.timeout_add_seconds(1, self._poll)

    def deactivate(self):
        self.active = False
        self.epoch += 1
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        self._close_picker()
        self._close_gate_dialog()
        self._close_dryer()
        self._close_dryer_notice()
        self.dryer_pending = None

    def _poll(self):
        if not self.active:
            return False
        if self.pending_since is not None:
            if time.monotonic() - self.pending_since < 5.:
                return True
            # Do not keep controls active using an unanswered status query.
            self.snapshot = {}
            self._render()
        self.sequence += 1
        self.pending_since = time.monotonic()
        ws = self._screen._ws
        sent = ws is not None and ws.send_method('printer.objects.query',
                              {'objects': {'ace_screen': None, 'print_stats': ['state']}},
                              self._received, self.epoch, self.sequence, ws)
        if not sent:
            self.pending_since = None
            self.snapshot = {}
            self._render()
        return True

    def _received(self, response, method, params, epoch, sequence, ws):
        if (not self.active or epoch != self.epoch or sequence != self.sequence
                or ws is not self._screen._ws):
            return
        self.pending_since = None
        status = response.get('result', {}).get('status', {})
        self.snapshot = status.get('ace_screen', {})
        self.print_state = status.get('print_stats', {}).get('state', 'unknown')
        self._render()

    def _array(self, name, default):
        value = self.snapshot.get(name, [])
        return value if isinstance(value, list) and len(value) == 4 else [default] * 4

    def _safe(self):
        return (self.snapshot.get('connected', False) and not self.snapshot.get('busy', True)
                and self.print_state not in ('printing', 'paused', 'unknown'))

    def _render(self):
        materials = self._array('gate_material', '')
        colors = self._array('gate_color', 'FFFFFF')
        statuses = self._array('gate_status', -1)
        for gate in range(4):
            present = statuses[gate] == 1
            self.names[gate].set_text((materials[gate] or '—') if present else
                                      'Пусто' if statuses[gate] == 0 else 'Нет данных')
            self.icons[gate].ace_color = colors[gate] if present else '808080'
            self.icons[gate].set_opacity(1. if present else .35)
            self.icons[gate].queue_draw()
            self.cards[gate].set_sensitive(self._safe())
            self.edit[gate].set_sensitive(self._safe() and statuses[gate] == 1)
        self.unload_button.set_sensitive(self._safe() and self.snapshot.get('filament_present', False))
        self._render_dryer()
        message = self.dryer_error or self.snapshot.get('error') or (
            '' if self.snapshot.get('connected') else 'Нет свежих данных ACE Pro')
        self.message.set_text(message)
        self.message.set_visible(bool(message))
        if self.context and (not self._safe() or not self._valid_context(self.context)):
            self._close_picker()
        if self.gate_context and (not self._safe() or not self._valid_context(self.gate_context)):
            self._close_gate_dialog()
        self.suppressed = {(gate, token) for gate, token in self.suppressed
                           if self._array('tokens', -1)[gate] == token and statuses[gate] == 1}
        if (self.dialog is None and self.gate_dialog is None and self.dryer_dialog is None
                and self.dryer_notice is None and self._safe()
                and not getattr(self._screen, 'dialogs', [])):
            for gate in self.snapshot.get('insertions', []):
                if gate in range(4):
                    token = self._array('tokens', -1)[gate]
                    if (gate, token) not in self.suppressed and statuses[gate] == 1:
                        self._open_picker(gate, manual=False)
                        break

    def _valid_context(self, context):
        gate = context['gate']
        return (self._array('tokens', -1)[gate] == context['token']
                and self._array('gate_status', -1)[gate] == 1
                and (context['manual'] or gate in self.snapshot.get('insertions', [])))

    def _send(self, widget, script):
        self._screen._send_action(widget, 'printer.gcode.script', {'script': script})

    def _unload_clicked(self, widget):
        if self._safe() and self.snapshot.get('filament_present', False):
            self._send(widget, 'ACE_UI_UNLOAD')

    def _gate_clicked(self, widget, gate):
        if (not self._safe() or self.dialog is not None or self.gate_dialog is not None
                or self.dryer_dialog is not None or self.dryer_notice is not None
                or self._array('gate_status', -1)[gate] != 1):
            return
        self.gate_context = {'gate': gate, 'token': self._array('tokens', -1)[gate], 'manual': True}
        body = self._style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8), 'ace-spool-body')
        body.pack_start(self._style(Gtk.Label(label='Катушка %d' % (gate + 1)), 'ace-heading'),
                        False, False, 0)
        body.pack_start(self._style(Gtk.Label(
            label='Загрузить в экструдер или полностью\nизвлечь катушку из ACE Pro?',
            wrap=True, justify=Gtk.Justification.CENTER), 'ace-spool-caption'), False, False, 0)
        actions = Gtk.Box(spacing=8, homogeneous=True)
        for title, action in (('Загрузить', 'LOAD'), ('Извлечь', 'EJECT')):
            button = self._style(self._gtk.Button(None, title),
                                 'ace-spool-load' if action == 'LOAD' else 'ace-spool-eject')
            button.connect('clicked', self._gate_action, action)
            actions.pack_start(button, True, True, 0)
        body.pack_start(actions, False, False, 0)
        self.gate_dialog = self._gtk.Dialog(
            'Катушка %d' % (gate + 1), [{'name': 'Отмена', 'response': Gtk.ResponseType.CANCEL}],
            body, self._close_gate_dialog)
        self.gate_dialog.connect('delete-event', self._close_gate_dialog)
        self._compact_dialog(self.gate_dialog, (Gtk.ResponseType.CANCEL,), 'ace-spool-dialog')

    def _gate_action(self, widget, action):
        context = self.gate_context
        if context is None:
            return
        valid = self._safe() and self._valid_context(context)
        self._close_gate_dialog()
        if valid:
            self._send(None, 'ACE_UI_%s GATE=%d' % (action, context['gate']))

    def _close_gate_dialog(self, dialog=None, *args):
        if self.gate_dialog is not None and (dialog is None or dialog is self.gate_dialog):
            self._gtk.remove_dialog(self.gate_dialog)
            self.gate_dialog = self.gate_context = None
        return True

    def _edit_clicked(self, widget, gate):
        self._open_picker(gate, manual=True)

    def _dryer_state(self):
        dryer = self.snapshot.get('dryer_status', {})
        if self.snapshot.get('connected') and dryer.get('status') in ('stop', 'drying'):
            return dryer['status']
        return None

    def _dryer_clamp(self, field, value):
        settings = self.snapshot['dryer_settings']
        minimum, maximum = settings['min_' + field], settings['max_' + field]
        step = settings[field + '_step']
        return minimum + (max(minimum, min(maximum, value)) - minimum) // step * step

    def _render_dryer(self):
        state = self._dryer_state()
        running = state == 'drying'
        target = self.snapshot.get('dryer_status', {}).get('target_temp')
        try:
            target = float(target)
            text = '%g°C' % target if math.isfinite(target) and target > 0 else '—°C'
        except (TypeError, ValueError):
            text = '—°C'
        self.dryer_label.set_text('Сушка - ' + text if running else 'Сушилка')
        self.dryer_icon.ace_color = 'FF8000' if running else 'FFFFFF'
        style = self.dryer_button.get_style_context()
        style.add_class('ace-drying') if running else style.remove_class('ace-drying')
        self.dryer_icon.queue_draw()
        pending = self.dryer_pending
        if pending:
            if state == pending['expected']:
                self.dryer_pending = None
                self.dryer_error = ''
            elif state is None or pending['ws'] is not self._screen._ws:
                self.dryer_pending = None
                self.dryer_error = 'Нет связи: состояние сушилки не подтверждено'
            elif time.monotonic() - pending['since'] > pending['timeout']:
                self.dryer_pending = None
                self.dryer_error = 'ACE не подтвердил переключение сушилки. Проверьте состояние и журнал.'
                logging.error('ACE Pro: %s; command=%s', self.dryer_error, pending['script'])
        self.dryer_button.set_sensitive(state is not None and self.dryer_pending is None)
        if self.dryer_notice is not None and (state is None or self.dryer_error):
            self._close_dryer_notice()
        if self.dryer_dialog is not None:
            if state != 'stop' or not self.snapshot.get('dryer_settings', {}).get('valid'):
                self._close_dryer()
            else:
                for key, field in (('temp', 'temperature'), ('duration', 'duration')):
                    self.dryer_selection[key] = self._dryer_clamp(field, self.dryer_selection[key])
                self._dryer_values_changed()

    @staticmethod
    def _draw_dryer(area, cr):
        width, height = area.get_allocated_width(), area.get_allocated_height()
        size = min(width, height)
        cr.set_source_rgb(*rgb(area.ace_color))
        cr.set_line_width(max(2., size * .06))
        # Three rising heat waves form a theme-independent dryer icon.
        for fraction in (.25, .5, .75):
            x = width / 2. + (fraction - .5) * size
            cr.move_to(x, height / 2. + size * .35)
            cr.curve_to(x - size * .25, height / 2.,
                        x + size * .25, height / 2., x, height / 2. - size * .35)
            cr.stroke()
        return False

    def _dryer_clicked(self, widget):
        if self.dryer_pending is not None or self.dryer_notice is not None:
            return
        state = self._dryer_state()
        if state == 'drying':
            if self._dryer_command('ACE_STOP_DRYING', 'stop'):
                self._show_dryer_stopped()
        elif state == 'stop':
            self._open_dryer()

    def _open_dryer(self):
        if (self.dryer_dialog is not None or self.dryer_notice is not None or self.dialog is not None
                or self.gate_dialog is not None
                or self._dryer_state() != 'stop' or self.dryer_pending is not None):
            return
        settings = self.snapshot.get('dryer_settings', {})
        if not settings.get('valid'):
            self.dryer_error = settings.get('error', 'Обновите ace_screen.cfg: нет настроек _ACE_UI_DRYER')
            self.message.set_text(self.dryer_error)
            return
        self.dryer_selection = {'temp': settings['default_temperature'], 'duration': settings['default_duration']}
        self.dryer_error = ''
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)
        body.pack_start(self._style(Gtk.Label(label='Сушилка', xalign=0), 'ace-heading'),
                        False, False, 0)
        self.dryer_values, self.dryer_steps = {}, {}
        for field, title in (('temp', 'Температура'), ('duration', 'Время сушки')):
            setting = self._style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8),
                                  'ace-dryer-setting')
            setting.pack_start(self._style(Gtk.Label(label=title, xalign=0), 'ace-dryer-caption'),
                               False, False, 0)
            row = Gtk.Box(spacing=8)
            minus = self._style(self._gtk.Button(None, '−'), 'ace-dryer-step')
            plus = self._style(self._gtk.Button(None, '+'), 'ace-dryer-step')
            minus.connect('clicked', self._dryer_adjust, field, -1)
            plus.connect('clicked', self._dryer_adjust, field, 1)
            value = self._style(Gtk.Label(), 'ace-dryer-value')
            row.pack_start(minus, False, False, 0)
            row.pack_start(value, True, True, 0)
            row.pack_start(plus, False, False, 0)
            setting.pack_start(row, False, False, 0)
            body.pack_start(setting, False, False, 0)
            self.dryer_values[field] = value
            self.dryer_steps[field] = (minus, plus)
        self.dryer_dialog = self._gtk.Dialog(
            'Сушилка', [{'name': 'ОК', 'response': Gtk.ResponseType.OK},
                        {'name': 'Отмена', 'response': Gtk.ResponseType.CANCEL}],
            body, self._dryer_response)
        self.dryer_dialog.connect('delete-event', self._delete_dryer)
        self._compact_dialog(self.dryer_dialog, (Gtk.ResponseType.OK, Gtk.ResponseType.CANCEL))
        self._dryer_values_changed()

    def _compact_dialog(self, dialog, responses, style='ace-dryer-dialog'):
        dialog.hide()
        dialog.get_action_area().set_layout(Gtk.ButtonBoxStyle.EXPAND)
        for response in responses:
            button = dialog.get_widget_for_response(response)
            button.set_size_request(-1, -1)
            self._style(button, 'ace-dialog-confirm' if response == Gtk.ResponseType.OK else 'ace-dialog-cancel')
        # Keep KlipperScreen's response buttons and dialog lifecycle, but put
        # the card inside its overlay: no window manager is needed to center it.
        contents = dialog.get_child()
        dialog.remove(contents)
        card = self._style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                   halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER), style)
        width = min(450, int(self._screen.width * .72))
        card.set_size_request(width, -1)
        card.pack_start(contents, False, False, 0)
        layer = Gtk.EventBox(visible_window=False, hexpand=True, vexpand=True)
        layer.add(card)
        self.dialog_layers[dialog] = layer
        dialog.connect('destroy', self._destroy_dialog_layer)
        self._screen.overlay.add_overlay(layer)
        self._style(card)
        self.content.set_opacity(.35)
        layer.show_all()
        layer.grab_add()

    def _destroy_dialog_layer(self, dialog):
        layer = self.dialog_layers.pop(dialog, None)
        if layer is not None:
            layer.grab_remove()
            self._screen.overlay.remove(layer)
            layer.destroy()
            self.content.set_opacity(1.)
        if dialog is self.dryer_dialog:
            self.dryer_dialog = self.dryer_selection = None
        if dialog is self.dryer_notice:
            self.dryer_notice = None
        if dialog is self.gate_dialog:
            self.gate_dialog = self.gate_context = None

    def _show_dryer_stopped(self):
        if self.dryer_notice is not None:
            return
        body = self._style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10), 'ace-dryer-notice')
        body.pack_start(self._style(Gtk.Label(
            label='Сушка остановлена.', wrap=True, justify=Gtk.Justification.CENTER),
            'ace-dryer-notice-title'), False, False, 0)
        body.pack_start(Gtk.Label(
            label='(выключается не сразу,\nтребуется время, чтобы охладиться)',
            wrap=True, max_width_chars=44, justify=Gtk.Justification.CENTER), False, False, 0)
        self.dryer_notice = self._gtk.Dialog(
            'Сушилка', [{'name': 'ОК', 'response': Gtk.ResponseType.OK}],
            body, self._close_dryer_notice)
        self.dryer_notice.connect('delete-event', self._close_dryer_notice)
        self._compact_dialog(self.dryer_notice, (Gtk.ResponseType.OK,))

    def _close_dryer_notice(self, dialog=None, response_id=None):
        if self.dryer_notice is not None and (dialog is None or dialog is self.dryer_notice):
            self._gtk.remove_dialog(self.dryer_notice)
            self.dryer_notice = None
            self.content.set_opacity(1.)
        return True

    def _dryer_adjust(self, widget, field, delta):
        settings = self.snapshot.get('dryer_settings', {})
        if self.dryer_dialog is None or not settings.get('valid'):
            return
        name = 'temperature' if field == 'temp' else 'duration'
        step = settings[name + '_step'] * (-1 if delta < 0 else 1)
        self.dryer_selection[field] = self._dryer_clamp(name, self.dryer_selection[field] + step)
        self._dryer_values_changed()

    def _dryer_values_changed(self):
        self.dryer_values['temp'].set_text('%d °C' % self.dryer_selection['temp'])
        self.dryer_values['duration'].set_text('%d мин' % self.dryer_selection['duration'])
        settings = self.snapshot['dryer_settings']
        for field, name in (('temp', 'temperature'), ('duration', 'duration')):
            minus, plus = self.dryer_steps[field]
            minus.set_sensitive(self.dryer_selection[field] > settings['min_' + name])
            plus.set_sensitive(self.dryer_selection[field] < settings['max_' + name])

    def _dryer_response(self, dialog, response_id):
        if dialog is not self.dryer_dialog:
            return
        if response_id != Gtk.ResponseType.OK:
            self._close_dryer()
            return
        settings = self.snapshot.get('dryer_settings', {})
        if (self._dryer_state() != 'stop' or self.dryer_pending is not None
                or not settings.get('valid')):
            self._close_dryer()
            return
        temp, duration = self.dryer_selection['temp'], self.dryer_selection['duration']
        if (temp != self._dryer_clamp('temperature', temp)
                or duration != self._dryer_clamp('duration', duration)):
            return
        self._close_dryer()
        self._dryer_command('ACE_START_DRYING TEMP=%d DURATION=%d' % (temp, duration), 'drying')

    def _dryer_command(self, script, expected):
        ws = self._screen._ws
        if ws is None or not ws.connected:
            self.dryer_error = 'Нет связи: команда сушилки не отправлена'
            self._render()
            return False
        self.dryer_pending = {'expected': expected, 'since': time.monotonic(), 'ws': ws,
                              'timeout': self.snapshot['dryer_settings']['confirmation_timeout'],
                              'script': script}
        self.dryer_error = ''
        self.dryer_button.set_sensitive(False)
        try:
            # Use the same action handler as KlipperScreen's macro menu.
            # _send_action has no return value; confirmation comes from ACE status.
            self._send(None, script)
        except Exception as exc:
            logging.exception('ACE Pro: unable to send dryer command')
            self.dryer_pending = None
            self.dryer_error = 'Не удалось отправить команду сушилки: %s' % exc
        self._render()
        return not self.dryer_error

    def _delete_dryer(self, dialog, event):
        if dialog is self.dryer_dialog:
            self._close_dryer()
        return True

    def _close_dryer(self):
        if self.dryer_dialog is not None:
            self._gtk.remove_dialog(self.dryer_dialog)
            self.content.set_opacity(1.)
        self.dryer_dialog = self.dryer_selection = None

    def _swatch(self, color, spool=False):
        area = Gtk.DrawingArea(hexpand=True, vexpand=spool)
        size = max(24, int(self._gtk.img_scale * (2. if spool else .7)))
        area.set_size_request(size, size)
        area.ace_color = color
        area.connect('draw', self._draw_swatch, spool)
        return area

    @staticmethod
    def _draw_swatch(area, cr, spool):
        width, height = area.get_allocated_width(), area.get_allocated_height()
        radius = min(width, height) * .4
        cr.set_source_rgb(*rgb(area.ace_color))
        cr.arc(width / 2., height / 2., radius, 0., math.tau)
        cr.fill_preserve()
        cr.set_source_rgb(*rgb('171C24') if spool else (.65, .65, .65))
        cr.set_line_width(max(1., radius * (.035 if spool else .07)))
        cr.stroke()
        if spool:
            # The flange and concentric hub use the same dark outlines for
            # every filament color, including white, as on a physical spool.
            cr.arc(width / 2., height / 2., radius * .44, 0., math.tau)
            cr.stroke()
            cr.set_source_rgb(*rgb('252C36'))
            cr.arc(width / 2., height / 2., radius * .36, 0., math.tau)
            cr.fill_preserve()
            cr.set_source_rgb(*rgb('171C24'))
            cr.stroke()
            cr.arc(width / 2., height / 2., radius * .15, 0., math.tau)
            cr.fill()
        return False

    def _open_picker(self, gate, manual):
        if (self.dialog is not None or self.dryer_dialog is not None or self.dryer_notice is not None
                or self.gate_dialog is not None
                or not self._safe() or self._array('gate_status', -1)[gate] != 1):
            return
        profiles = self.snapshot.get('materials', {})
        colors = self.snapshot.get('colors', {})
        valid_profiles = {}
        for name, value in profiles.items():
            try:
                temperature = float(value)
            except (TypeError, ValueError):
                continue
            if re.fullmatch(r'[A-Z0-9_+\-]+', name) and math.isfinite(temperature) and temperature > 0:
                valid_profiles[name] = temperature
        profiles = valid_profiles
        if not profiles or len(colors) != 16 or any(not re.fullmatch(r'[0-9A-F]{6}', c) for c in colors):
            self.message.set_text('Настройте variable_temperatures и палитру из 16 цветов в ace_screen.cfg')
            return
        context = {'gate': gate, 'token': self._array('tokens', -1)[gate], 'manual': manual}
        self.context = context
        material = self._array('gate_material', '')[gate]
        color = str(self._array('gate_color', '')[gate]).upper()
        self.selection = {'material': material if material in profiles else None,
                          'color': color if color in colors else None}
        self.saving = False
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        columns = Gtk.Box(spacing=16, homogeneous=True, hexpand=True, vexpand=True)
        material_section = self._style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12),
                                       'ace-picker-section')
        material_section.pack_start(self._style(Gtk.Label(label='Тип пластика'), 'ace-picker-title'),
                                    False, False, 0)
        types = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True,
                         row_homogeneous=True, hexpand=True, vexpand=True)
        self.material_buttons = {}
        for index, (material, temp) in enumerate(profiles.items()):
            button = self._style(self._gtk.Button(), 'ace-picker-material')
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            labels.pack_start(self._style(Gtk.Label(label=material), 'ace-material-name'), False, False, 0)
            labels.pack_start(self._style(Gtk.Label(label='%g °C' % float(temp)), 'ace-material-temp'),
                              False, False, 0)
            button.add(labels)
            button.connect('clicked', self._choose, 'material', material)
            types.attach(button, index % 2, index // 2, 1, 1)
            self.material_buttons[material] = button
        material_section.pack_start(types, True, True, 0)
        color_section = self._style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12),
                                    'ace-picker-section')
        color_section.pack_start(self._style(Gtk.Label(label='Цвет'), 'ace-picker-title'), False, False, 0)
        palette = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True,
                           row_homogeneous=True, hexpand=True, vexpand=True)
        self.color_buttons = {}
        for index, (color, name) in enumerate(colors.items()):
            button = self._style(self._gtk.Button(), 'ace-color')
            button.set_tooltip_text(str(name))
            tile = Gtk.DrawingArea(hexpand=True, vexpand=True)
            size = max(24, int(self._gtk.img_scale * .8))
            tile.set_size_request(size, size)
            tile.ace_color = color
            tile.connect('draw', self._draw_color_tile)
            button.add(tile)
            button.connect('clicked', self._choose, 'color', color)
            palette.attach(button, index % 4, index // 4, 1, 1)
            self.color_buttons[color] = button
        color_section.pack_start(palette, True, True, 0)
        columns.pack_start(material_section, True, True, 0)
        columns.pack_start(color_section, True, True, 0)
        self.choice_label = self._style(Gtk.Label(wrap=True), 'ace-picker-choice')
        scroll = self._gtk.ScrolledWindow()
        scroll.add(columns)
        body.pack_start(scroll, True, True, 0)
        body.pack_start(self.choice_label, False, False, 0)
        self.dialog = self._gtk.Dialog('Катушка %d: материал и цвет' % (gate + 1),
                                      [{'name': 'Сохранить', 'response': Gtk.ResponseType.OK},
                                       {'name': 'Отмена', 'response': Gtk.ResponseType.CANCEL}],
                                      body, self._response, context)
        self.dialog.connect('delete-event', self._delete_picker)
        self._style(self.dialog, 'ace-picker-dialog')
        self.dialog.get_action_area().set_layout(Gtk.ButtonBoxStyle.EXPAND)
        for response, style in ((Gtk.ResponseType.OK, 'ace-picker-save'),
                                (Gtk.ResponseType.CANCEL, 'ace-picker-cancel')):
            button = self.dialog.get_widget_for_response(response)
            button.set_size_request(-1, -1)
            self._style(button, style)
        self._selection_changed()

    @staticmethod
    def _draw_color_tile(area, cr):
        width, height = area.get_allocated_width(), area.get_allocated_height()
        radius = min(7., (min(width, height) - 2.) / 2.)
        for x, y, angle in ((width - 1 - radius, 1 + radius, -math.pi / 2),
                            (width - 1 - radius, height - 1 - radius, 0),
                            (1 + radius, height - 1 - radius, math.pi / 2),
                            (1 + radius, 1 + radius, math.pi)):
            cr.arc(x, y, radius, angle, angle + math.pi / 2)
        cr.close_path()
        cr.set_source_rgb(*rgb(area.ace_color))
        cr.fill_preserve()
        cr.set_source_rgba(1., 1., 1., .2)
        cr.set_line_width(1.)
        cr.stroke()

    def _choose(self, widget, field, value):
        if not self.saving:
            self.selection[field] = value
            self._selection_changed()

    def _selection_changed(self):
        for field, buttons in (('material', self.material_buttons), ('color', self.color_buttons)):
            for value, button in buttons.items():
                style = button.get_style_context()
                style.remove_class('suggested-action')
                if self.selection[field] == value:
                    style.add_class('suggested-action')
        material, color = self.selection['material'], self.selection['color']
        color_name = self.snapshot.get('colors', {}).get(color, 'Выберите цвет')
        self.choice_label.set_text('%s · %s' % (material or 'Выберите пластик', color_name))
        self.dialog.get_widget_for_response(Gtk.ResponseType.OK).set_sensitive(
            bool(material and color and not self.saving))
        self.dialog.get_widget_for_response(Gtk.ResponseType.CANCEL).set_sensitive(not self.saving)

    def _response(self, dialog, response_id, context):
        if dialog is not self.dialog:
            return
        if response_id != Gtk.ResponseType.OK:
            self._cancel_picker()
            return
        if self.saving or not all(self.selection.values()) or not self._valid_context(context):
            return
        self.saving = True
        self._selection_changed()
        script = ('ACE_UI_SET_FILAMENT GATE=%d TOKEN=%d MATERIAL=%s COLOR=%s MANUAL=%d'
                  % (context['gate'], context['token'], self.selection['material'],
                     self.selection['color'], context['manual']))
        self.choice_label.set_text('Сохранение…')
        logging.info('ACE Pro: saving filament: %s', script)
        try:
            sent = self._screen._ws.send_method(
                'printer.gcode.script', {'script': script}, self._saved, context)
        except Exception as exc:
            logging.exception('ACE Pro: unable to send filament settings')
            self.saving = False
            self._selection_changed()
            self.choice_label.set_text('Не удалось отправить настройки: %s' % exc)
            return
        if not sent:
            self.saving = False
            self._selection_changed()
            self.choice_label.set_text('Нет связи: данные не отправлены')

    def _saved(self, response, method, params, context):
        logging.info('ACE Pro: filament save response: %s', response)
        if self.context is not context:
            return
        if 'error' in response:
            self.saving = False
            self._selection_changed()
            self.choice_label.set_text(str(response['error'].get('message', 'Не удалось сохранить')))
            return
        self.suppressed.add((context['gate'], context['token']))
        self._close_picker()
        self.pending_since = None
        self._poll()

    def _cancel_picker(self):
        context = self.context
        if context and not self.saving:
            self.suppressed.add((context['gate'], context['token']))
            if not context['manual']:
                self._send(None, 'ACE_UI_ACK_INSERTION GATE=%d TOKEN=%d'
                           % (context['gate'], context['token']))
            self._close_picker()

    def _delete_picker(self, dialog, event):
        self._cancel_picker()
        return True

    def _close_picker(self):
        if self.dialog is not None:
            self._gtk.remove_dialog(self.dialog)
        self.dialog = self.context = None
