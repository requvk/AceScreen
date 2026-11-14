"""KlipperScreen state and UI-specific actions using BunnyACE commands."""

import copy
import logging
import math
import queue
import re
import uuid

from gcode import CommandError


class OperationCancelled(CommandError):
    pass


class AceScreen:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.response_timeout = config.getfloat('response_timeout', above=0.)
        self.operation_timeout = config.getfloat('operation_timeout', above=0.)
        self.cancel_check_interval = config.getfloat('cancel_check_interval', above=0.)
        self.busy = False
        self.error = ''
        self.deadline = None
        self.operation = None
        self.cancel_requested = False
        self._cleaning_up = False
        self.gcode.register_command('_ACE_SCREEN_RUN', self.cmd_RUN)
        self.gcode.register_command('_ACE_SCREEN_RESET', self.cmd_RESET)
        self.gcode.register_command('_ACE_SCREEN_SET_FILAMENT', self.cmd_SET_FILAMENT)
        self.printer.register_event_handler('klippy:ready', self._ready)
        self.printer.lookup_object('webhooks').register_endpoint('ace_screen/cancel', self._handle_cancel)

    def _ready(self):
        self.ace = self.printer.lookup_object('ace')
        self.variables = self.printer.lookup_object('save_variables')

    def get_status(self, eventtime):
        variables = self.variables.allVariables if hasattr(self, 'variables') else {}
        events = self._macro_values('_ACE_UI_INSERTIONS', eventtime)
        ace = getattr(self, 'ace', None)
        return {
            'busy': self.busy, 'error': self.error or getattr(ace, 'error_msg', ''),
            'operation': dict(self.operation, cancelling=self.cancel_requested) if self.operation else None,
            'connected': hasattr(self, 'ace') and self.ace._connected,
            'gate_status': list(self.ace.gate_status) if hasattr(self, 'ace') else [-1] * 4,
            'gate_material': list(variables.get('ace_gate_type', [''] * 4)),
            'gate_color': list(variables.get('ace_gate_color', ['FFFFFF'] * 4)),
            'gate_temp': list(variables.get('ace_gate_temp', [0] * 4)),
            'filament_present': any(status['filament_detected']
                                    for _, status in self._filament_sensors(eventtime)),
            'materials': dict(self._macro_values('_ACE_UI_MATERIALS', eventtime).get('temperatures', {})),
            'colors': dict(self._macro_values('_ACE_UI_COLORS', eventtime).get('colors', {})),
            'insertions': list(events.get('queue', [])),
            'tokens': list(events.get('tokens', [0] * 4)),
            'dryer_status': dict(ace.get_status(eventtime).get('dryer_status', {})) if ace else {},
            'dryer_settings': self._dryer_settings(eventtime),
        }

    def _macro_values(self, name, eventtime=None):
        macro = self.printer.lookup_object('gcode_macro ' + name, None)
        return macro.get_status(eventtime) if macro is not None else {}

    def _filament_sensors(self, eventtime):
        ace = getattr(self, 'ace', None)
        if ace is None:
            return []
        sensors = []
        for name in (ace.extruder_sensor_name, ace.toolhead_sensor_name):
            if name:
                sensor = self.printer.lookup_object('filament_switch_sensor ' + name, None)
                if sensor is not None:
                    sensors.append((name, sensor.get_status(eventtime)))
        return sensors

    def _dryer_settings(self, eventtime):
        raw = self._macro_values('_ACE_UI_DRYER', eventtime)
        integers = ('min_temperature', 'temperature_step', 'default_temperature',
                    'min_duration', 'max_duration', 'duration_step', 'default_duration')
        try:
            settings = {name: int(raw[name]) for name in integers}
            if any(float(raw[name]) != settings[name] for name in integers):
                raise ValueError('integer settings required')
            timeout = float(raw['confirmation_timeout'])
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError('invalid timeout')
            settings['max_temperature'] = self.ace.max_dryer_temperature
            for field in ('temperature', 'duration'):
                minimum, maximum = settings['min_' + field], settings['max_' + field]
                step = settings[field + '_step']
                if minimum <= 0 or step <= 0 or maximum < minimum:
                    raise ValueError('invalid bounds or step')
                # Keep the dialog values on its configured button grid.
                maximum = minimum + (maximum - minimum) // step * step
                default = max(minimum, min(maximum, settings['default_' + field]))
                settings['max_' + field] = maximum
                settings['default_' + field] = minimum + (default - minimum) // step * step
            return dict(settings, confirmation_timeout=timeout, valid=True)
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            return {'valid': False, 'confirmation_timeout': self.response_timeout,
                    'error': 'ACE: проверьте _ACE_UI_DRYER в ace_screen.cfg '
                    'и max_dryer_temperature в ace.cfg: границы и шаги должны быть допустимыми'}

    def cmd_SET_FILAMENT(self, gcmd):
        gate = gcmd.get_int('GATE', minval=0, maxval=3)
        token = gcmd.get_int('TOKEN', minval=0)
        manual = gcmd.get_int('MANUAL', 0, minval=0, maxval=1)
        material = gcmd.get('MATERIAL').upper()
        color = gcmd.get('COLOR').upper()
        now = self.reactor.monotonic()
        profiles = self._macro_values('_ACE_UI_MATERIALS', now).get('temperatures', {})
        palette = self._macro_values('_ACE_UI_COLORS', now).get('colors', {})
        if (not re.fullmatch(r'[A-Z0-9_+\-]+', material) or material not in profiles
                or not re.fullmatch(r'[0-9A-F]{6}', color) or color not in palette):
            raise gcmd.error('ACE: choose a material and one of the configured colors')
        try:
            temperature = float(profiles[material])
        except (TypeError, ValueError):
            raise gcmd.error('ACE: invalid temperature in variable_temperatures')
        limits = self.printer.lookup_object('configfile').get_status(now)['settings']['extruder']
        if (not math.isfinite(temperature) or not temperature.is_integer()
                or temperature < limits['min_extrude_temp']
                or temperature >= limits['max_temp']):
            raise gcmd.error('ACE: invalid temperature in variable_temperatures')
        stats = self.printer.lookup_object('print_stats', None)
        if self.busy or (stats and stats.get_status(now)['state'] in ('printing', 'paused')):
            raise gcmd.error('ACE: wait until the print or filament operation finishes')

        def validate_event():
            events = self._macro_values('_ACE_UI_INSERTIONS', self.reactor.monotonic())
            tokens = events.get('tokens', [])
            if (len(tokens) != 4 or tokens[gate] != token
                    or (not manual and gate not in events.get('queue', []))):
                raise gcmd.error('ACE: spool removed or replaced; reopen the picker')

        validate_event()
        if not self.ace._connected:
            raise gcmd.error('ACE: device is disconnected')
        if self.ace.get_status(now)['gate_status'][gate] != 1:
            raise gcmd.error('ACE: spool removed or not ready')
        fields = ('gate_material', 'gate_color', 'gate_temp')
        previous_fields = {field: list(getattr(self.ace, field)) for field in fields}
        previous_variables = copy.deepcopy(self.variables.allVariables)
        self.error = ''
        self.ace.error_msg = ''
        try:
            self._script('ACE_GATE_MAP GATE=%d MATERIAL=%s COLOR=%s TEMP=%d'
                         % (gate, material, color, temperature))
        except Exception:
            for field, value in previous_fields.items():
                setattr(self.ace, field, value)
            self.variables.allVariables.clear()
            self.variables.allVariables.update(previous_variables)
            raise
        self._script('_ACE_UI_ACK_INSERTION GATE=%d TOKEN=%d' % (gate, token))
        gcmd.respond_info('ACE: катушка %d: %s, #%s, %g C сохранены'
                         % (gate + 1, material, color, temperature))

    def _script(self, script):
        self.gcode.run_script_from_command(script)

    def _restore_nozzle_target(self, target):
        if not self.printer.is_shutdown():
            self._script('M400\nM104 S%s' % target)
            self.printer.lookup_object('gcode_move').reset_last_position()

    def run_toolchange(self, change_tool, gate):
        # Panel operations restore after their entire operation, including eject
        # and cancellation cleanup. Direct G-code toolchanges need their own scope.
        if self.busy:
            return change_tool(gate)
        previous_target = self.printer.lookup_object('extruder').get_status(
            self.reactor.monotonic())['target']
        try:
            return change_tool(gate)
        finally:
            self._restore_nozzle_target(previous_target)

    def _check(self):
        if self.printer.is_shutdown():
            raise self.gcode.error('ACE: printer is shut down')
        if self.busy and not self.ace._connected:
            raise self.gcode.error('ACE: device is disconnected')
        if self.deadline is not None and self.reactor.monotonic() >= self.deadline:
            raise self.gcode.error('ACE: operation timed out; inspect the filament')

    def _handle_cancel(self, web_request):
        request = web_request.get_str('request').lower()
        accepted = bool(self.busy and self.operation and self.operation['request'] == request)
        if accepted:
            self.cancel_requested = True
        web_request.send({'accepted': accepted})

    def check_cancel(self):
        if not self.busy:
            return
        self._check()
        if self.cancel_requested and not self._cleaning_up:
            raise OperationCancelled('ACE: операция отменена пользователем')

    def _wait_heat(self, gcmd):
        # Keep Klipper's heater setup; only its blocking wait needs cancellation.
        self.check_cancel()
        self._script('M104 ' + gcmd.get_raw_command_parameters())
        if gcmd.get_float('S', 0.) <= 0:
            return
        index = gcmd.get_int('T', None, minval=0)
        extruder = (self.printer.lookup_object('toolhead').get_extruder() if index is None else
                    self.printer.lookup_object('extruder%d' % index if index else 'extruder'))
        heater = extruder.get_heater()
        while heater.check_busy(self.reactor.monotonic()):
            self.check_cancel()
            self.reactor.pause(self.reactor.monotonic() + self.cancel_check_interval)
        self.check_cancel()

    def _confirm_stop(self, stop, index):
        replies = []
        stop(index, on_response=replies.append)
        end = self.reactor.monotonic() + self.response_timeout
        while not replies:
            self._check()
            if self.reactor.monotonic() >= end:
                raise self.gcode.error('ACE: нет подтверждения остановки подачи')
            self.reactor.pause(min(end, self.reactor.monotonic() + self.cancel_check_interval))
        reply = replies[0]
        if (not isinstance(reply, dict) or reply.get('code') != 0
                or reply.get('msg') == 'FORBIDDEN'):
            raise self.gcode.error('ACE: остановка подачи отклонена: %s' % reply)

    def _finish_cancel(self):
        self._cleaning_up = True
        self.deadline = self.reactor.monotonic() + self.operation_timeout
        try:
            if self.ace.ace_action == 'Loading' and self.ace.current_gate >= 0:
                self._confirm_stop(self.ace._stop_feeding, self.ace.current_gate)
            if self.ace._feed_assist_index >= 0:
                self._confirm_stop(self.ace._disable_feed_assist, self.ace._feed_assist_index)
            self.ace.wait_ace_ready()
            if self.ace.error_msg:
                raise self.gcode.error(self.ace.error_msg)
            self.ace.save_variable('ace_current_index', self.ace.current_gate, True)
            self.ace.save_variable('ace_filament_pos', self.ace.filament_pos, True)
            self.ace.ace_action = 'Idle'
        finally:
            self._cleaning_up = False

    def _pause(self, duration):
        end = self.reactor.monotonic() + duration
        while self.reactor.monotonic() < end:
            self._check()
            self.reactor.pause(min(end, self.reactor.monotonic() + self.cancel_check_interval))

    def _discard_request(self, request, callback):
        # Remove only this unanswered request after its caller has failed.
        pending = []
        while self.ace._queue is not None:
            try:
                item = self.ace._queue.get_nowait()
            except queue.Empty:
                break
            if item[0] is not request:
                pending.append(item)
        for item in pending:
            self.ace._queue.put(item)
        for ident, handler in list(self.ace._callback_map.items()):
            if handler is callback:
                self.ace._callback_map.pop(ident, None)

    def _send_eject(self, gate, length, speed):
        self._check()
        if not self.ace._connected or self.ace._queue is None:
            raise self.gcode.error('ACE: device is disconnected')
        replies = []

        def received(self, response):
            replies.append(response)

        request = {'method': 'unwind_filament',
                   'params': {'index': gate, 'length': length, 'speed': speed}}
        logging.info('ACE Screen: eject request=%s', request)
        self.ace.send_request(request, received)
        end = self.reactor.monotonic() + self.response_timeout
        try:
            while not replies:
                if not self.ace._connected:
                    raise self.gcode.error('ACE: connection lost')
                if self.reactor.monotonic() >= end:
                    raise self.gcode.error('ACE: no response to unwind_filament')
                self._pause(.05)
        except Exception:
            self._discard_request(request, received)
            raise
        reply = replies[0]
        logging.info('ACE Screen: eject response=%s', reply)
        if (not isinstance(reply, dict) or reply.get('code') != 0
                or reply.get('msg') == 'FORBIDDEN'):
            raise self.gcode.error('ACE: unwind_filament failed: %s' % reply)

    def _native(self, script):
        self.check_cancel()
        self._script(script)
        self.check_cancel()
        # BunnyACE reports some device failures via log_error(), without raising.
        if self.ace.error_msg:
            raise self.gcode.error(self.ace.error_msg)

    def _validate_unload(self, filament_present):
        position = self.variables.allVariables.get('ace_filament_pos', -1)
        # BunnyACE only cuts at 10; 9 is its saved state after the cut.
        # An earlier/unknown state with an occupied sensor must be inspected,
        # rather than silently taking the native path straight to ACE retraction.
        if filament_present and position not in (9, 10):
            raise self.gcode.error(
                'ACE: датчик видит филамент, но ace_filament_pos=%s; '
                'BunnyACE пропустит резку. Проверьте положение филамента и сохранённое состояние' % position)
        if position != 10:
            return
        command = self.ace.cut_macros.strip()
        if not command:
            raise self.gcode.error('ACE: не задан cut_macros в [ace]; выгрузка отменена до движения')
        configuration = self.printer.lookup_object('configfile').get_status(self.reactor.monotonic())['config']
        macro_name = 'gcode_macro ' + command.split()[0]
        macro = next((value for name, value in configuration.items()
                      if name.casefold() == macro_name.casefold()), None)
        if macro is not None:
            lines = (line.split('#', 1)[0].strip() for line in macro.get('gcode', '').splitlines())
            if not any(line and not line.startswith(';') for line in lines):
                raise self.gcode.error(
                    'ACE: макрос резки %s пустой; настройте cut_macros в [ace]. '
                    'Выгрузка отменена до движения' % command.split()[0])

    def _eject(self, gate):
        self.check_cancel()
        options = self._macro_values('_ACE_UI_STATE', self.reactor.monotonic())
        length = options['eject_length']
        if not isinstance(length, int) or length <= 0:
            raise self.gcode.error('ACE: eject_length must be a positive integer')
        speed = self.ace.retract_speed
        if speed <= 0:
            raise self.gcode.error('ACE: retract_speed in ace.cfg must be positive')
        previous_assist = self.ace._feed_assist_index
        logging.info('ACE Screen: eject start gate=%d length=%s speed=%s feed_assist=%d',
                     gate, length, speed, previous_assist)
        if previous_assist >= 0:
            self._native('ACE_DISABLE_FEED_ASSIST INDEX=%d' % previous_assist)
        self.deadline = self.reactor.monotonic() + self.operation_timeout
        self._check_eject(gate)
        while not self.ace.is_ace_ready():
            self._check_eject(gate)
            self._pause(.1)
        self.check_cancel()
        self._send_eject(gate, length, speed)
        # Acknowledgement accepts the request; the actual move finishes later.
        # Use BunnyACE's existing heartbeat and never resend the movement.
        earliest_finish = self.reactor.monotonic() + length / speed
        previous_state = None
        while True:
            self._check_eject(gate)
            status = self.ace._info
            state = (status['status'], status.get('action'),
                     tuple((slot.get('index'), slot.get('status')) for slot in status['slots']))
            if state != previous_state:
                logging.info('ACE Screen: eject gate=%d device_status=%s action=%s slots=%s', gate, *state)
                previous_state = state
            if self.reactor.monotonic() >= earliest_finish and self.ace.is_ace_ready():
                break
            self._pause(.1)
        self.check_cancel()
        if self.ace.get_status(self.reactor.monotonic())['gate_status'][gate] != 0:
            raise self.gcode.error(
                'ACE: после отката на %d мм датчик гейта %d всё ещё видит филамент; '
                'проверьте нить и eject_length' % (length, gate + 1))
        logging.info('ACE Screen: eject complete gate=%d slot=empty requested_mm=%s', gate, length)
        if previous_assist >= 0 and self.ace.current_gate >= 0:
            self._native('ACE_ENABLE_FEED_ASSIST INDEX=%d' % self.ace.current_gate)

    def _check_eject(self, gate):
        self._check()
        if not self.ace._connected:
            raise self.gcode.error('ACE: connection lost')
        if self.ace.error_msg:
            raise self.gcode.error(self.ace.error_msg)
        if self.ace._info['slots'][gate]['status'] == 'gear_err':
            raise self.gcode.error(
                'ACE: ошибка механизма гейта %d (gear_err); извлечение остановлено. '
                'Проверьте филамент и журнал' % (gate + 1))

    def cmd_RUN(self, gcmd):
        action = gcmd.get('ACTION').upper()
        gate = gcmd.get_int('GATE', -1, minval=-1, maxval=3)
        if action not in ('LOAD', 'UNLOAD', 'EJECT') or (action != 'UNLOAD' and gate not in range(4)):
            raise gcmd.error('ACE: invalid screen action or spool')
        if self.busy:
            raise gcmd.error('ACE: операция ещё выполняется; дождитесь её завершения')
        # A previous failure is diagnostic, not a permanent operation lock.
        # Clear BunnyACE's old message so _native detects only this attempt's errors.
        self.error = ''
        self.ace.error_msg = ''
        now = self.reactor.monotonic()
        stats = self.printer.lookup_object('print_stats', None)
        if stats is not None and stats.get_status(now)['state'] in ('printing', 'paused'):
            raise gcmd.error('ACE: screen filament operations require an idle printer')
        if stats is None and self.printer.lookup_object('idle_timeout').get_status(now)['state'] == 'Printing':
            raise gcmd.error('ACE: screen filament operations require an idle printer')
        if self.ace.current_gate not in range(-1, 4):
            raise gcmd.error('ACE: invalid active spool')
        # The native toolchange owns sensor handling; preserve user enable flags.
        sensor_statuses = self._filament_sensors(now)
        sensors = [(name, status['enabled']) for name, status in sensor_statuses]
        filament_present = any(status['filament_detected'] for _, status in sensor_statuses)
        if self.ace.current_gate == -1 and filament_present:
            raise gcmd.error('ACE: filament detected but active spool is unknown')
        if action == 'UNLOAD' and self.ace.current_gate == -1:
            gcmd.respond_info('ACE: сейчас ничего не загружено')
            return
        if action == 'LOAD' and gate == self.ace.current_gate and self.ace.filament_pos != 10:
            raise gcmd.error('ACE: загрузка этой катушки не завершена; проверьте положение филамента')
        if self.ace.current_gate >= 0 and (
                action == 'UNLOAD' or action == 'LOAD' and gate != self.ace.current_gate
                or action == 'EJECT' and gate == self.ace.current_gate):
            self._validate_unload(filament_present)
        self.busy = True
        request = gcmd.get('REQUEST', uuid.uuid4().hex).lower()
        self.operation = {'action': action, 'gate': gate, 'request': request}
        self.cancel_requested = False
        started = False
        restore_target = False
        message = None
        previous_m109 = None
        previous_target = self.printer.lookup_object('extruder').get_status(now)['target']
        try:
            if not self.ace._connected:
                raise gcmd.error('ACE: device is disconnected')
            gates = self.ace.get_status(now)['gate_status']
            previous_m109 = self.gcode.register_command('M109', None)
            self.gcode.register_command('M109', self._wait_heat)
            if action == 'LOAD':
                if gates[gate] != 1:
                    raise gcmd.error('ACE: selected spool is not ready')
                restore_target = gate != self.ace.current_gate
                started = True
                self._native('ACE_CHANGE_TOOL GATE=%d' % gate)
                message = 'ACE: катушка %d загружена' % (gate + 1)
            elif action == 'UNLOAD':
                restore_target = True
                started = True
                self._native('ACE_CHANGE_TOOL GATE=-1')
                message = 'ACE: филамент выгружен до сплиттера'
            else:
                if gates[gate] == 0:
                    gcmd.respond_info('ACE: выбранная катушка уже извлечена')
                    return
                restore_target = True
                started = True
                if gate == self.ace.current_gate:
                    self._native('ACE_CHANGE_TOOL GATE=-1')
                self._eject(gate)
                message = 'ACE: катушка %d извлечена, можно вынуть её из ACE Pro' % (gate + 1)
        except OperationCancelled:
            restore_target = True
            try:
                self._finish_cancel()
            except Exception as exc:
                self.error = 'Не удалось завершить отмену операции: %s' % exc
                logging.exception('ACE Screen: cancellation failed')
                raise
            self.error = 'Операция отменена. Проверьте положение филамента перед следующей операцией.'
            message = 'ACE: ' + self.error
        except Exception as exc:
            restore_target = restore_target or self.cancel_requested
            if started:
                self.error = str(exc)
                logging.error('ACE Screen: %s gate=%d failed: %s', action, gate, exc)
            raise
        finally:
            self.deadline = None
            try:
                if restore_target:
                    # Restore even if cancellation was interrupted by another error.
                    self._restore_nozzle_target(previous_target)
            except Exception as exc:
                self.error = 'ACE: unable to restore nozzle temperature: %s' % exc
                raise
            finally:
                try:
                    if previous_m109 is not None:
                        self.gcode.register_command('M109', None)
                        self.gcode.register_command('M109', previous_m109)
                    for name, enabled in sensors:
                        self._script('SET_FILAMENT_SENSOR SENSOR=%s ENABLE=%d' % (name, enabled))
                except Exception as exc:
                    self.error = 'ACE: unable to restore sensor settings: %s' % exc
                    raise
                finally:
                    self.busy = False
                    self.operation = None
                    self.cancel_requested = False
        if message is not None:
            gcmd.respond_info(message)

    def cmd_RESET(self, gcmd):
        if self.busy:
            raise gcmd.error('ACE: operation is still running')
        self.error = ''
        self.ace.error_msg = ''
        gcmd.respond_info('ACE: ошибка сброшена; положение филамента автоматически не изменялось')


def load_config(config):
    return AceScreen(config)
