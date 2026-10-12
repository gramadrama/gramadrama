"""Evdev keycode sequences for Mutter RemoteDesktop injection (Polish layout 'pl').

A sequence is a list of items ['code', evdev, pressed], ['sym', keysym, pressed] or
['sleep', ms], executed in order by ctl.py. Pure module: no gi, no display.
"""

KC = dict(esc=1, minus=12, equal=13, bs=14, backspace=14, tab=15, bracketleft=26, bracketright=27,
          ret=28, enter=28, lctrl=29, semicolon=39, apostrophe=40, grave=41, lshift=42, backslash=43,
          comma=51, period=52, slash=53, rshift=54, lalt=56, space=57, capslock=58, numlock=69,
          kpenter=96, rctrl=97, ralt=100, altgr=100, home=102, up=103, pgup=104, left=105, right=106,
          end=107, down=108, pgdn=109, insert=110, delete=111, lsuper=125, rsuper=126, menu=127,
          f11=87, f12=88)
for _i, _ch in enumerate('1234567890'):
    KC[_ch] = 2 + _i
for _i in range(10):
    KC['f%d' % (_i + 1)] = 59 + _i
for _i, _ch in enumerate('qwertyuiop'):
    KC[_ch] = 16 + _i
for _i, _ch in enumerate('asdfghjkl'):
    KC[_ch] = 30 + _i
for _i, _ch in enumerate('zxcvbnm'):
    KC[_ch] = 44 + _i
ALIASES = {'ctrl': 'lctrl', 'control': 'lctrl', 'shift': 'lshift', 'alt': 'lalt', 'super': 'lsuper',
           'escape': 'esc', 'return': 'ret', 'page_up': 'pgup', 'page_down': 'pgdn', 'del': 'delete'}

# Level-3 (AltGr) letters of the 'pl' layout; uppercase adds Shift.
ALTGR = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'x', 'ż': 'z'}
# ASCII punctuation: key name, shifted?
PUNCT = {' ': ('space', False), '\n': ('ret', False), '\t': ('tab', False),
         '-': ('minus', False), '_': ('minus', True), '=': ('equal', False), '+': ('equal', True),
         '[': ('bracketleft', False), '{': ('bracketleft', True), ']': ('bracketright', False),
         '}': ('bracketright', True), ';': ('semicolon', False), ':': ('semicolon', True),
         "'": ('apostrophe', False), '"': ('apostrophe', True), '`': ('grave', False), '~': ('grave', True),
         '\\': ('backslash', False), '|': ('backslash', True), ',': ('comma', False), '<': ('comma', True),
         '.': ('period', False), '>': ('period', True), '/': ('slash', False), '?': ('slash', True)}
for _ch, _sh in zip('!@#$%^&*()', '1234567890'):
    PUNCT[_ch] = (_sh, True)
# Dead keys of the 'pl' layout: AltGr+; = dead_acute, AltGr+Shift+' = dead_caron,
# AltGr+Shift+= = dead_ogonek, AltGr+[ = dead_diaeresis.
DEAD = {'acute': ('semicolon', ['ralt']), 'caron': ('apostrophe', ['ralt', 'lshift']),
        'ogonek': ('equal', ['ralt', 'lshift']), 'diaeresis': ('bracketleft', ['ralt'])}
COMPOSED = {'é': ('acute', 'e'), 'É': ('acute', 'E'), 'á': ('acute', 'a'), 'í': ('acute', 'i'),
            'ú': ('acute', 'u'), 'ý': ('acute', 'y'), 'č': ('caron', 'c'), 'Č': ('caron', 'C'),
            'š': ('caron', 's'), 'Š': ('caron', 'S'), 'ř': ('caron', 'r'), 'ž': ('caron', 'z'),
            'ě': ('caron', 'e'), 'ä': ('diaeresis', 'a'), 'ö': ('diaeresis', 'o'), 'ü': ('diaeresis', 'u')}


def code(name):
    name = ALIASES.get(name.lower(), name.lower()) if isinstance(name, str) else name
    return name if isinstance(name, int) else KC[name]


def tap(key, mods=()):
    """Press mods, press+release key, release mods in reverse order."""
    seq = [['code', code(m), 1] for m in mods] + [['code', code(key), 1], ['code', code(key), 0]]
    return seq + [['code', code(m), 0] for m in reversed(mods)]


def combo(*names):
    """combo('ctrl', 'alt', 'g'): all but the last name are held modifiers."""
    return tap(names[-1], names[:-1])


def press(key):
    return [['code', code(key), 1]]


def release(key):
    return [['code', code(key), 0]]


def char_seq(ch):
    if ch in COMPOSED:
        dead, base = COMPOSED[ch]
        key, mods = DEAD[dead]
        return tap(key, mods) + char_seq(base)
    if ch in PUNCT:
        key, shifted = PUNCT[ch]
        return tap(key, ['lshift'] if shifted else [])
    low = ch.lower()
    mods = ['lshift'] if ch != low else []
    if low in ALTGR:
        return tap(ALTGR[low], ['ralt'] + mods)
    if low in KC and len(low) == 1:
        return tap(low, mods)
    raise ValueError('no key for %r on the pl layout (use keysym mode)' % ch)


def text_seq(text):
    """Keycode typing on the 'pl' layout: AltGr for Polish letters, Shift for
    uppercase, dead keys for é/č and friends."""
    seq = []
    for ch in text:
        seq += char_seq(ch)
    return seq


def typeable(text):
    try:
        text_seq(text)
        return True
    except ValueError:
        return False
