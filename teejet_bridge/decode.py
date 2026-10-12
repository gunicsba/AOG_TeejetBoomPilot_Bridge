"""decode: annotate a PCAN-View v1.1 trace and summarise section-mask changes."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, List, Optional, TextIO

from . import frames
from .trc import TrcFrame, read_trc


@dataclass
class MaskChange:
    t_ms: float
    side: str       # "console-master", "console-mask", "radion"
    value: int


def mask_changes(trace: Iterable[TrcFrame]) -> List[MaskChange]:
    """Changes of console master (02), console mask (00) and Radion actual SS."""
    out: List[MaskChange] = []
    last = {}
    for f in trace:
        d = frames.decode(f.can_id, f.data)
        if d.kind == frames.KIND_MASTER:
            side, val = "console-master", int(d.fields["master"])
        elif d.kind == frames.KIND_SECTIONS:
            side, val = "console-mask", d.fields["mask"]
        elif d.kind == frames.KIND_RADION_STATE:
            side, val = "radion", d.fields["mask"]
        else:
            continue
        if last.get(side) != val:
            last[side] = val
            out.append(MaskChange(f.t_ms, side, val))
    return out


def sequence(changes: List[MaskChange], side: str) -> List[int]:
    return [c.value for c in changes if c.side == side]


def decode_file(path: str, out: TextIO, show_frames: bool = True, only_teejet: bool = False) -> List[MaskChange]:
    trace = list(read_trc(path))
    stats = defaultdict(list)
    if show_frames:
        for f in trace:
            d = frames.decode(f.can_id, f.data)
            if only_teejet and d.kind == frames.KIND_UNKNOWN:
                continue
            out.write(f"{f.t_ms:10.1f}  {f.direction}  {f.can_id:08X}  {f.dlc}  "
                      f"{f.data.hex(' ').upper():<23}  {d.text}\n")
    for f in trace:
        d = frames.decode(f.can_id, f.data)
        key = d.kind if d.kind != frames.KIND_UNKNOWN else f"{f.can_id:08X}"
        stats[key].append(f.t_ms)

    out.write(f"\n=== {path}\n=== {len(trace)} frames\n\nMessage rates:\n")
    for key, ts in sorted(stats.items(), key=lambda kv: -len(kv[1])):
        period = f"{(ts[-1] - ts[0]) / (len(ts) - 1):8.1f} ms" if len(ts) > 1 else "       -   "
        out.write(f"  {key:<22} {len(ts):6d}  mean period {period}\n")

    changes = mask_changes(trace)
    out.write("\nSection / master changes:\n")
    for c in changes:
        if c.side == "console-master":
            txt = "ON" if c.value else "OFF"
        else:
            txt = f"0x{c.value:02X} [{frames.mask_str(c.value)}]"
        out.write(f"  {c.t_ms:10.1f} ms  {c.side:<15} {txt}\n")
    for side in ("console-master", "console-mask", "radion"):
        seq = sequence(changes, side)
        if seq:
            out.write(f"  {side:<15} sequence: {' -> '.join(f'{v:02X}' for v in seq)}\n")
    return changes
