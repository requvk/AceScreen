"""Decode the ACE serial byte stream without depending on USB read boundaries."""

import json
import logging
import struct


def decode_responses(buffer, checksum):
    while buffer:
        start = buffer.find(b'\xff\xaa')
        if start < 0:
            # The next read may complete a header split after its first byte.
            buffer[:] = b'\xff' if buffer[-1] == 0xff else b''
            return
        if start:
            del buffer[:start]
        if len(buffer) < 4:
            return
        payload_length = struct.unpack_from('<H', buffer, 2)[0]
        frame_length = payload_length + 7
        if len(buffer) < frame_length:
            return
        payload = bytes(buffer[4:4 + payload_length])
        crc = struct.unpack_from('<H', buffer, 4 + payload_length)[0]
        if buffer[frame_length - 1] != 0xfe or crc != checksum(payload):
            logging.warning('ACE: discarded invalid serial frame (length=%d)', payload_length)
            del buffer[0]
            continue
        del buffer[:frame_length]
        try:
            response = json.loads(payload.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            logging.warning('ACE: discarded invalid JSON response')
            continue
        if isinstance(response, dict) and isinstance(response.get('id'), int):
            yield response

