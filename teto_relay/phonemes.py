"""ARPAbet to X-SAMPA, and phonetic hints for the phonemizer.

`EnXSampaPhonemizer` normally looks each word up in an English dictionary.
Supplying a *phonetic hint* - space-separated X-SAMPA - overrides that lookup
while still letting OpenUtau build the CVVC transitions. Two things follow:

* Words the dictionary does not know ("kasane") stop being sung as silence.
* Words it knows but mispronounces ("teto" as TEE-toh) can be corrected exactly
  rather than by guessing at a respelling.

The symbol set here was read out of the English bank's own oto aliases rather
than assumed - see `bank_symbols`. Its vowels are
`3 @ A E I O OI U V aI aU e eI i oU u {` and its consonants
`D N S T Z b d dZ f g h j k l m n p r s t tS v w z`, which is standard
X-SAMPA, and every ARPAbet phoneme maps into it.

Separator note: hints must be **space** separated. Commas are taken literally
and come back as a single unusable phoneme.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# The 39 ARPAbet phonemes (as produced by CMUdict, g2p and forced aligners)
# mapped to the X-SAMPA this voicebank uses.
ARPABET_TO_XSAMPA: dict[str, str] = {
    # vowels
    "AA": "A",   # odd
    "AE": "{",   # at
    "AH": "V",   # hut  (unstressed AH0 becomes the schwa @ below)
    "AO": "O",   # ought
    "AW": "aU",  # cow
    "AY": "aI",  # hide
    "EH": "E",   # ed
    "ER": "3",   # hurt
    "EY": "eI",  # ate
    "IH": "I",   # it
    "IY": "i",   # eat
    "OW": "oU",  # oat
    "OY": "OI",  # toy
    "UH": "U",   # hood
    "UW": "u",   # two
    # consonants
    "B": "b",
    "CH": "tS",
    "D": "d",
    "DH": "D",   # thee
    "F": "f",
    "G": "g",
    "HH": "h",
    "JH": "dZ",  # gee
    "K": "k",
    "L": "l",
    "M": "m",
    "N": "n",
    "NG": "N",   # ping
    "P": "p",
    "R": "r",
    "S": "s",
    "SH": "S",   # she
    "T": "t",
    "TH": "T",   # theta
    "V": "v",
    "W": "w",
    "Y": "j",
    "Z": "z",
    "ZH": "Z",   # seizure
}

# Unstressed AH is a schwa, which the bank has as a separate sample.
UNSTRESSED_SCHWA = "@"


def arpabet_to_xsampa(phones: list[str]) -> str:
    """Convert ARPAbet phones (with or without stress digits) into a hint.

    Aligners and G2P emit stress markers - "AH0", "EY1". The digit carries the
    stress, which this voicebank has no separate samples for, so it is stripped
    - except that an unstressed AH0 is a schwa and maps to a different sample.
    """
    out: list[str] = []
    for phone in phones:
        token = phone.strip().upper()
        if not token:
            continue
        stress = token[-1] if token[-1].isdigit() else ""
        base = token[:-1] if stress else token

        if base == "AH" and stress == "0":
            out.append(UNSTRESSED_SCHWA)
            continue

        mapped = ARPABET_TO_XSAMPA.get(base)
        if mapped is None:
            log.debug("no X-SAMPA for ARPAbet %r; dropping it", phone)
            continue
        out.append(mapped)
    return " ".join(out)


def bank_symbols(bank) -> set[str]:
    """Every phoneme symbol the voicebank actually has samples for.

    Read from the oto aliases, so a hint can be checked against reality instead
    of against an assumption about which symbols exist.
    """
    from .voicebank import parse_oto

    symbols: set[str] = set()
    for sub in bank.subbanks:
        for entry in parse_oto(sub.oto_path):
            parts = entry.alias.strip().split()
            if len(parts) == 2:
                for part in parts:
                    if part != "-":
                        # Coda forms carry a trailing dash: "l-" is still "l".
                        symbols.add(part.rstrip("-"))
            elif len(parts) == 1 and parts[0] != "-":
                symbols.add(parts[0].rstrip("-"))
    return symbols


def unsupported(hint: str, symbols: set[str]) -> list[str]:
    """Symbols in `hint` the bank has no sample for."""
    if not symbols:
        return []
    return [s for s in hint.split() if s not in symbols]
