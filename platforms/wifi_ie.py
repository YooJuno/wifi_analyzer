"""Conservative parsing of advertised HT/VHT channel widths and BSS Load.

Unsupported EHT operation layouts and 80+80 MHz remain unknown. Capability
bits describe what an AP supports, so they are never used as its current width.
"""


def parse_information_elements(data, band):
    result = {}
    position = 0
    ht = vht = he = None
    eht = False
    while position + 2 <= len(data):
        element, length = data[position:position + 2]
        position += 2
        if position + length > len(data):
            break
        payload = data[position:position + length]
        position += length
        if element == 11 and length >= 5:
            result.update(station_count=int.from_bytes(payload[:2], "little"),
                          channel_utilization=payload[2])
        elif element == 61 and length >= 2:
            ht = payload
        elif element == 192 and length >= 3:
            vht = payload
        elif element == 255 and length >= 1:
            if payload[0] == 36 and length >= 7:
                he = payload[1:]
            elif payload[0] == 106:
                eht = True
    if band == 6:
        if he and not eht:
            # IEEE 802.11ax HE Operation: fixed six-byte payload followed by
            # VHT (optional 3), co-hosted BSS (optional 1), then 6 GHz (5).
            params = int.from_bytes(he[:4], "little")
            offset = 6 + (3 if params & 0x4000 else 0) + (1 if params & 0x8000 else 0)
            if params & 0x20000 and offset + 5 <= len(he):
                _primary, control, center0, center1, _rate = he[offset:offset + 5]
                mode = control & 3
                if mode < 3:
                    result["width"] = (20, 40, 80)[mode]
                elif center1 and abs(center1 - center0) == 8:
                    result["width"] = 160
        return result
    if ht:
        secondary = ht[1] & 3
        if secondary in {1, 3} and ht[1] & 4:
            result.update(width=40, secondary_offset="above" if secondary == 1 else "below")
        elif secondary == 0:
            result["width"] = 20
    if vht:
        mode, center1, center2 = vht[:3]
        if mode == 1:
            if center2 == 0:
                result["width"] = 80
            elif abs(center2 - center1) == 8:
                result["width"] = 160
            else:
                result.pop("width", None)  # Non-contiguous 80+80 is not 160 MHz.
        elif mode == 2:
            result["width"] = 160
        elif mode == 3:
            result.pop("width", None)
    if eht:
        result.pop("width", None)
    return result
