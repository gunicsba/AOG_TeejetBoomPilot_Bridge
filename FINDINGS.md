# Findings

## 1. From the PCAN-View traces (`traces/`)

`teejet-bridge decode --summary traces\*.trc` reproduces everything below.

| Observation | Trace |
|---|---|
| Console heartbeat `18EFFFC0 0A …` every 100 ms; MSS `18F022C0` every 100 ms; `00 S1 S2 S3` every ~167–200 ms; `02 M` every 1 s | all Matrix traces |
| The Radion starts sending (`00 00 00 00 00 80 FF`, `02 00`) **~0.5 s after the first 0x0A**, even when 0x0A is the only console frame on the bus | `console alive no speed response.trc` |
| The Radion also accepted an **8-byte** all-FF heartbeat (`0A FF FF FF FF FF FF FF`). The bridge sends 7 bytes like the Matrix | same |
| Radion 7-byte frame: once per second, plus within ~25 ms of a change (e.g. 994.5 ms `00` → 1018.2 ms `7F`) | `radion section master on…trc` |
| Radion master on/off and the boom switches show up directly in SS: `00→7F→7E→7F→00` | `radion section master on then 1 off and on then master off.trc` |
| Toggling the Radion's switches one by one: `00→7F→7E→7C→78→70→60→40→00→40→60→70→78→7C→7E→7F→00` | `radion section toggle.trc` |
| **Console master ON + `00 7F` alone does not open anything**: SS stayed `00` throughout, so the Radion's own master/switches were off | `teejet auto mode all sections ON.trc`, `134_4km_h.trc` |
| In the "Matrix turned master ON then…" capture, the console sent **no** `02`/`00` frames at all, and SS stayed `00` | `Matrix PRO570gs turned master ON….trc` |
| Probe `01` → Radion `01 00 02 61` | `teejet got gnss.trc` |
| Console address claim SA C0 NAME `00 00 60 16 07 80 00 2F` | `Matrix PRO570gs boot.trc` |
| b2–b4 always `00`, b5 always `80`, byte 6 always `FF` | all |
| `0A` bytes 1–2 are a speed in mm/s: in the GNSS demo they equal the MSS speed (5358–5364 mm/s). **But** with GNSS at 134.4 km/h (MSS 37330 mm/s) `0A` stays at 5364 mm/s and its odometer (bytes 3–6) also advances at 19.3 km/h, so `0A` carries another (simulated, filtered or capped?) speed than MSS. The bridge sends all-FF by default, so this does not affect it | `…gnss demo.trc`, `134_4km_h.trc`, `radion section toggle.trc` |

**Hypothesis (unverified):** the Radion's frame `00 SS b2 b3 …` mirrors the console's
`00 S1-8 S9-16 S17-24`, so b2/b3 might be sections 9–16/17–24 rather than rate/volume. A Radion
with more than 8 sections, or bench test 6, would settle this.

## 2. AgOpenGPS PGN study

Source: AgOpenGPS (AgOpenGPS-Official layout, checked in a local fork at `d1dad081`).

| What | Where | Result |
|---|---|---|
| PGN 239 (0xEF) machine data | `SourceCode/GPS/Forms/PGN.Designer.cs` `CPGN_EF` | byte 6 speed (km/h×10, 1 byte), byte 11 sections 1–8, byte 12 sections 9–16 |
| PGN 229 (0xE5) 64 sections | same, `CPGN_E5` | bytes 5–12 = sections 1–64 |
| PGN 254 (0xFE) steer data | same, `CPGN_FE` | bytes 5–6 speed (km/h×10, LE), bytes 11–12 sections |
| When 239/229 are sent | `SourceCode/GPS/Forms/Position.designer.cs` | only `if (isJobStarted)`, once per GPS fix |
| Meaning of the section bits | `Sections.Designer.cs` `BuildMachineByte` | `section[j].isSectionOn`, which already includes the master Auto/Manual button and per-section buttons. **There is no separate master or auto bit in any PGN** |
| PGN 234 (0xEA) from modules | `UDPComm.Designer.cs` `case 234`, `Sections.Designer.cs` `DoRemoteSwitches`, `Classes/CModuleComm.cs` | must be exactly 14 bytes. Byte 5 main (bit0 = master-ON press, bit1 = master-OFF press, bit2 = 0 for buttons / 1 for switches), 6–8 auto/num, 9 ON 1–8, 10 OFF 1–8, 11 ON 9–16, 12 OFF 9–16. An OFF bit forces a section off; when it clears and master is Auto, the section returns to Auto |
| Module hello | `AgIO/Source/Forms/UDP.designer.cs` `ReceiveFromUDP` | AgIO forwards every `80 81` module frame to AOG; `data[3]==123 && Length==11` marks the machine module as alive |

**Master decision:** because AOG only sends section data with a job open, the bridge sets
BoomPilot master = 1 while 239/229 keep arriving (`job` mode). At headlands AOG sends mask 0, and
the Radion keeps BoomPilot on with all sections closed, which is what the Matrix does. When the
job closes or AOG stops, master goes to 0 after 1 s. `mask` mode (master only while a section is
on) is available if bench test 2/4 shows that is better.

### Subnet and ISOBUS PGNs (added in 0.2.0)

| What | Where | Result |
|---|---|---|
| Subnet change 201 | `AgIO/Source/Forms/FormUDP.cs` `btnSendSubnet_Click`, `Machine_UDP_v5.ino` | `80 81 7F C9 05 C9 C9 a b c CRC` to 255.255.255.255:8888 from every adapter; module stores `a.b.c` |
| Scan 202 / reply 203 | same | scan `80 81 7F CA 03 CA CA 05 CRC`; reply 13 bytes `80 81 7B CB 07 ip0..ip3 s0..s2 CRC` to 255.255.255.255:9999 (s = subnet of the scanning AgIO) |
| Module → AgIO address | `Machine_UDP_v5.ino`, `UDP.designer.cs` | modules send to `<subnet>.255:9999`; AgIO binds `Any:9999`. Checked on this PC: a broadcast to 192.168.5.255 reaches a local listener |
| AOG → modules | `UDP.designer.cs` `ReceiveFromLoopBack` | AgIO forwards *every* AOG frame to `<subnet>.255:8888`, so 0xF1 reaches modules |
| ISOBUS heartbeat 0xF0 | `GPS/Classes/CISOBUS.cs` `DeserializeHeartbeat`, `UDPComm.Designer.cs` | data `[flags, numSections, state bits]`, flags bit0 = SC enabled, bits1-3 = clients; length must equal `2 + ceil(n/8)`; alive 1 s |
| What AOG does with it | `OpenGL.Designer.cs` | while alive and numSections > 0: `section[j].isMappingOn = isobus.IsSectionOn(j)`, so **coverage follows the implement's actual state**. Section *commands* (239/229) are unaffected, so there is no feedback loop |
| 0xF1 | `CISOBUS.RequestSectionControlEnabled`, `FormGPS.btnIsobusSC_Click` | `80 81 7F F1 01 en CRC`, sent when the ISOBUS button is pressed |
| Button icon quirk | `CISOBUS.DeserializeHeartbeat` / `SectionControlEnabled` setter | With 0 sections the flag is stored without redrawing. When N sections arrive with the same flag, the setter returns early, so the button stays "idle" until pressed twice (seen in the field with 0.2.0). Since 0.3.0 the bridge sends the flag flipped once on every 0 → N transition |

## 3. Agromehanika (AG-Tronik) pattern

`AOG_AGTronik_bridge` already reports implement section state to AOG correctly. Its pattern is reused:

- reply to AgIO hello (0xC8) with the machine hello (src/PGN 123, relay bytes = actual state),
  **only while the implement is connected**
- on every 239 from AOG, send 0xED "from machine" with the actual relay state
- in auto mode, send 0xEA with ON = 0 and OFF = sections disabled on the implement
- optionally send PGN 32618 (Rate Controller switch box) on change

The AG-Tronik reports a *switch/enable* state separate from what is spraying. The Radion's
SS may be either (bench test 3), so 0xEA OFF bits are behind the `override` setting and off by default.

**First field test (2026-10-12): sections flickered on the sprayer and in the app.** The
log showed the commanded mask going to the AOG value and back to `00` within 1–10 ms, every fix. A
UDP capture on :8888 showed **two AgIO senders**: this PC's AgIO (192.168.5.13, 10 Hz, sections on,
with PGN 229) and another machine on the Wi-Fi hotspot (10.65.137.118, ~100 Hz, PGN 239/254 all
zero, no 229). Every AgIO broadcasts to 255.255.255.255:8888, so any module on a shared network
hears all of them. The AGTronik bridge has the same weakness. Since 0.2.0 the bridge, like a real
module, only obeys AgIO inside its subnet (default 192.168.5.x).

Also learned while testing: on Windows, a UDP reply to a port with no listener makes the next
`recvfrom` fail (WSAECONNRESET). The bridge disables `SIO_UDP_CONNRESET`, otherwise AOG packets
are lost and the bridge drops master.

## 4. Bench tests

Setup: PCAN-USB + Radion on the bench with 120 Ω at both ends, AOG running with a job open
(simulator is fine), the bridge window connected. The trace of every session is in `logs/*.trc`.

| # | How (with the bridge + AOG) | Result | Date |
|---|---|---|---|
| 1 | **Bit order.** AOG manual mode: turn on only section 1, then only 2, … 7. *Sent to Radion* shows `00 01`, `00 02` … `00 40`. Note which digit lights on the Radion BoomPilot test menu | | |
| 2 | **Master needed?** AOG sections 1–7 on, then tick *Force master OFF* (`02 00` + `00 7F`). Do the sections open? | | |
| 3 | **AND vs switch state.** All Radion boom switches + Radion master ON, AOG only section 1 on. *Radion actual* = `01` (AND) or `7F` (switch state)? Decides whether `override` feedback is usable | | |
| 4 | **Mode field.** Does the Radion's "Mode (manual/auto)" follow `02 01` (job open) / `02 00` (job closed, or *Force master OFF*)? | | |
| 5 | **Heartbeat loss.** Sections open, untick *Send 0x0A heartbeat* and note the log time. Measure until the Radion shows "connection NO" and what the open sections do. Does the Radion keep sending (*Radion actual* rx age)? | | |
| 6 | **b2–b5.** Pulse generator on the flowmeter input; watch b2–b5 in the window/log at several frequencies | | |

## 5. Open questions

- Does the Radion use MSS (`18F022C0`) or the speed in 0x0A at all? It has its own speed sensor.
  Both are off by default (`--mss`, `hb_speed`).
- Does it need the address-claim table? It worked without one; `claim` is optional.
- What do the console's `18EFD0C0` mux 10/11/12 frames and the slot probes do? They're not sent.
