# RetroAchievements gameplay-option marker

ROMs built with any of these optional gameplay patches set a common,
irreversible marker in save-backed RAM:

- XP multiplier
- full substitute and Monster Pen EXP
- scouting after a monster takes offense
- removal of the multiple-owned scouting penalty
- minimum synthesis level
- removal of the synthesis polarity requirement
- Randomiser master option

The X/XY suffix and polarity-icon fixes are cosmetic and do not set it.

## Runtime check

- Address: `0x021C0AB8`
- Mask: `0x20`
- Marked condition: `(byte & 0x20) != 0`

The bit is the unused Rigor Mortex XY "seen" flag. The patched ARM9 only ORs
this bit; it never clears it. It becomes permanent when the player next saves
normally, because the address belongs to the checksummed save payload.

Within either raw 64 KiB save copy, the equivalent byte is at offset `0x362C`.
The second copy begins at raw save offset `0x7100`.

## Compatibility note

The marker hook expects the unmodified ARM9 accessor at `0x020633BC` and an
empty code cave at `0x020DC708`. The patcher validates both and aborts on an
unexpected ROM instead of emitting an unmarked gameplay-patched build.
