#!/usr/bin/env python3
"""Dump a HID report descriptor (the sysfs `report_descriptor` file) as a field list and flag fields wider than 32 bits.

Used to show that the kernel's `hid_field_extract() called with n (64) > 32` warning comes from the iBridge touchpad
interface, not from the ambient light sensor. The HID device numbers change at every boot, so list them first:

    ls /sys/bus/hid/devices | grep 05AC:8600
    python3 -I tools/hiddump.py /sys/bus/hid/devices/0003:05AC:8600.<n>/report_descriptor | grep 'size>32'
"""
import sys

TAGS = {8: "Input", 9: "Output", 11: "Feature"}


def signed(v, nbytes):
    return v - (1 << (8 * nbytes)) if nbytes and v >> (8 * nbytes - 1) else v


def dump(data):
    i, depth = 0, 0
    st = dict(page=0, size=0, count=0, lmin=0, lmax=0, rid=0)
    stack, usages = [], []
    while i < len(data):
        b = data[i]
        if b == 0xFE:                                   # long item: skip
            i += 3 + data[i + 1]
            continue
        n = (0, 1, 2, 4)[b & 3]
        typ, tag = (b >> 2) & 3, b >> 4
        raw = int.from_bytes(data[i + 1:i + 1 + n], "little")
        i += 1 + n
        if typ == 1:                                    # global items
            if tag == 0:
                st["page"] = raw
            elif tag == 1:
                st["lmin"] = signed(raw, n)
            elif tag == 2:
                st["lmax"] = signed(raw, n) if st["lmin"] < 0 else raw
            elif tag == 7:
                st["size"] = raw
            elif tag == 8:
                st["rid"] = raw
            elif tag == 9:
                st["count"] = raw
            elif tag == 10:
                stack.append(dict(st))
            elif tag == 11 and stack:
                st = stack.pop()
        elif typ == 2 and tag == 0:                     # local: usage
            usages.append(raw if n == 4 else (st["page"] << 16 | raw))
        elif typ == 0:                                  # main items
            pad = "  " * depth
            if tag == 12:
                depth = max(0, depth - 1)
            elif tag == 10:
                print(f"{pad}Collection page={st['page']:#x} usage={[hex(u) for u in usages[-1:]]}")
                depth += 1
            elif tag in TAGS:
                flag = "   <<<<<< size>32" if st["size"] > 32 else ""
                print(f"{pad}{TAGS[tag]} rid={st['rid']} size={st['size']} count={st['count']} "
                      f"range={st['lmin']}..{st['lmax']} usages={[hex(u) for u in usages[:4]]}{flag}")
            usages = []


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open(sys.argv[1], "rb") as f:
        dump(f.read())
