# CAN traces: TeeJet Matrix Pro 570GS + Radion 8140

PCAN-View `$FILEVERSION=1.1` text traces, recorded with a PEAK PCAN-USB at 250 kbit/s, 29-bit IDs.
The bus is J1939-style; the console is SA `C0`, the Radion is SA `B4`. They are the source of the
protocol in [FINDINGS.md](../FINDINGS.md), and the test suite also uses them as fixtures.

Decode any of them with:

```bat
teejet-bridge decode "traces\radion section toggle.trc"            every frame, annotated
teejet-bridge decode --summary traces\*.trc                        message rates + section changes
```

Main IDs: `18EFFFC0` console → all (mux `0A` heartbeat, `02` master, `00` sections, `01` probe),
`18EFC0B4` Radion → console (`00 SS …` actual sections, `02 xx` status, `01 00 02 61` ID),
`18F022C0` ISO 11783 Machine Selected Speed from the console.

| File | Length | What was done | What it shows |
|---|---|---|---|
| `Matrix PRO570gs boot.trc` | 47 s | Matrix powered up, no Radion | Address claims for a whole table of SAs (C0 NAME `00 00 60 16 07 80 00 2F`), probes `01` to many slots, the `0A` heartbeat starting |
| `Matrix PRO570gs connected radion 8140.trc` | 24 s | Radion connected, idle | Radion answers with `00 00 00 00 00 80 FF` + `02 00` every second |
| `Matrix PRO570gs connected radion 8140 started gnss demo.trc` | 96 s | Matrix GNSS demo mode | `0A` speed field equals MSS speed (5358–5364 mm/s ≈ 19.3 km/h); Radion stays `00` |
| `Matrix PRO570gs turned master ON then all sections OFF then master then all sections ON.trc` | 20 s | Master / sections pressed on the Matrix | The console sent **no** `02`/`00` frames in this capture; Radion stays `00` |
| `teejet got gnss.trc` | 95 s | Matrix boot with GNSS | Address claims, probe `01` and the Radion's reply `01 00 02 61`; master `02 00` |
| `teejet got gnss maybe speed flickers at standstill needs checking.trc` | 84 s | Standing still with GNSS | `0A` speed stays 0 throughout this capture; master off, mask `00` |
| `teejet auto mode all sections ON.trc` | 10 s | Matrix BoomPilot auto, all sections on | Console master `0→1→0`, mask `00→7F→00`, but the Radion reports `00` (its own master or switches were off) |
| `134_4km_h.trc` | 4 s | GNSS speed 134.4 km/h | MSS = 37330 mm/s (134.4 km/h), but `0A` stays at 5364 mm/s (19.3 km/h) and its odometer follows 19.3 km/h |
| `console alive no speed response.trc` | 2 s | Only a hand-made heartbeat from PCAN-View: `0A FF FF FF FF FF FF FF` (8 bytes) | The Radion starts answering ~0.5 s after the first heartbeat, with nothing else on the bus |
| `radion section master on then 1 off and on then master off.trc` | 9 s | Console commanding master on + `7F`; Radion master on, switch 1 off/on, master off | Radion `SS`: `00 → 7F → 7E → 7F → 00` |
| `radion section toggle.trc` | 27 s | Console commanding master on + `7F`; Radion switches turned off one by one, then on again | Radion `SS`: `00 → 7F → 7E → 7C → 78 → 70 → 60 → 40 → 00 → 40 → … → 7F → 00` |

More traces are welcome, especially with more than 8 sections, with flow on the flowmeter input
(Radion bytes b2–b5 are still unknown), or from other TeeJet consoles. Name the file after what you
did and open a pull request.
