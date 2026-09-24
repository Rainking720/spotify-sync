import re, unicodedata

# Bump whenever a rule below changes: the index re-keys itself on mismatch.
NORM_VERSION = 3

# Parenthetical junk that does NOT change which recording this is.
JUNK = re.compile(r"""\s*[\(\[]\s*(
 \d{4}\s+)?(digital\s+)?(re[- ]?master(ed)?(\s+\d{4})?|deluxe(\s+\w+)*|bonus\s+track|
 radio\s+edit|single\s+version|album\s+version|explicit(\s+version)?|clean(\s+version)?|
 mono|stereo|original\s+mix|expanded(\s+\w+)*|anniversary(\s+\w+)*|reissue|
 from\s+the\s+vault|special\s+edition|edit)\s*[\)\]]""", re.I | re.X)

# Trailing " - Remastered 2011" style (Spotify's very common form)
DASHJUNK = re.compile(r"""\s+-\s+(
 (\d{4}\s+)?(digital\s+)?re[- ]?master(ed)?(\s+\d{4})?|radio\s+edit|single\s+version|
 album\s+version|explicit|mono|stereo|bonus\s+track|deluxe(\s+\w+)*|
 anniversary\s+edition|from\s+the\s+vault|edit)\s*$""", re.I | re.X)

# Bare "with" is part of real titles ("Dance With You"), so only treat it as a
# feature marker inside brackets. feat/ft/featuring are safe unbracketed.
FEAT_BR = re.compile(r"\s*[\(\[]\s*(feat|ft|featuring|with)\.?\s+[^\)\]]*[\)\]]\s*", re.I)
FEAT_BARE = re.compile(r"\s*(feat|ft|featuring)\.?\s+.*$", re.I)
def FEATSUB(s): return FEAT_BARE.sub(" ", FEAT_BR.sub(" ", s))

# Spotify writes "Matchbox Twenty"; this library has "Matchbox 20" (134 tracks).
# Folding number words to digits makes both sides agree, and also covers
# "3 Doors Down" / "Three Doors Down", "5 Seconds of Summer", etc.
NUMWORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40",
    "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80",
    "ninety": "90", "hundred": "100",
}


def _numfold(s):
    return " ".join(NUMWORDS.get(w, w) for w in s.split())


def _base(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = s.replace("’", "'").replace("ʼ", "'")
    s = s.replace("&", " and ").replace("+", " ")
    s = s.replace("'", "")  # We'll -> Well, matching Spotify's apostrophe-less forms
    return s

def norm_title(s):
    s = _base(s)
    for _ in range(3):
        s2 = JUNK.sub(" ", s); s2 = DASHJUNK.sub("", s2)
        if s2 == s: break
        s = s2
    s = FEATSUB(s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return _numfold(re.sub(r"\s+", " ", s).strip())

def norm_artist(s):
    s = _base(s)
    s = FEATSUB(s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^the\s+", "", s)
    return _numfold(s)

def key(artist, title):
    return f"{norm_artist(artist)}|||{norm_title(title)}"
