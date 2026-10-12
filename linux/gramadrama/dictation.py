"""Dictation's text rules, as on macOS (SpeechModel.swift: TranscriptFilter, DictationTidy;
DictationPrompt.swift; DictationSettings.swift). Pure: no gi, no network; the speech service
(speech.py) and the tests import it, the engine does not (docs/dictation.md).

Python's re has no \\p{L}: [^\\W\\d_] is a letter, and a lookaround on it is the same boundary."""
import re

from .prompts import CLEANUP_EXAMPLES, CLEANUP_SYSTEM, SPELLING_RULE, Prompts  # the defaults and the user's prompt files (prompts.py)

SAMPLE_RATE = 16000
MIN_SECONDS = 0.4  # shorter: a tap of the key, dropped silently
MAX_SECONDS = 180  # the recording stops by itself and goes on to recognition
SILENCE_PEAK = 0.003  # the loudest 50 ms chunk must reach this RMS (-50.46 dBFS), else "Nic nie usłyszałem"
CLEANUP_SECONDS = 10  # Bielik's cleanup races this; the tidied transcript wins when it loses
LETTER = r'[^\W\d_]'

# Fixed jargon in the Whisper prompt when the language is not English: Whisper spells it as written.
DEFAULT_VOCABULARY = ('deploy', 'staging', 'PR', 'merge', 'code review', 'feedback', 'release', 'Jira', 'Slack')
PROMPT_CHARS = 300  # the words added to the dictionary join the own ones in the Whisper prompt up to this length
LANGUAGES = {'pl': 'pl', 'en': 'en', 'de': 'de', 'auto': None}  # the setting -> Whisper's language (None: detected)
# Shift held with the dictation key dictates in the other language (macOS DictationLanguage.alternate):
# English when the setting is Polish, Polish when it is English, English when Whisper detects it.
ALTERNATE = {'pl': 'en', 'en': 'pl', 'de': 'en', 'auto': 'en'}  # legacy default-pair lookup; use alternate_language
LABELS = {'pl': 'PL', 'en': 'EN', 'de': 'DE', 'auto': 'auto'}  # on the pill (macOS shortLabel)
TRANSLATED_MIN_WORDS = 4  # a shorter all-English result ("OK, thanks!") is no translation


def level(rms):
    """0..1 for the HUD's bars: -50 dB is 0, -10 dB is 1 (AudioRecorder.swift)."""
    import math
    return min(max((20 * math.log10(max(rms, 1e-6)) + 50) / 40, 0.0), 1.0)


def parse_vocabulary(text):
    """'GramaDrama, Bielik\\nOrion,, staging ' -> ['GramaDrama', 'Bielik', 'Orion', 'staging']: split on
    commas, semicolons and line ends, the first spelling of each word (case-insensitively) kept."""
    seen, words = set(), []
    for word in re.split(r'[,;\n\r\x0b\x0c\x85  ]', text or ''):
        word = word.strip(' \t')
        if word and word.lower() not in seen:
            seen.add(word.lower())
            words.append(word)
    return words


def prompt_words(own, learned):
    """The words for the Whisper prompt and the cleanup's spelling list: every own word, then the words added to the
    dictionary (the latest first) while the list stays within PROMPT_CHARS. Whisper reads only the last 224 tokens of
    its prompt (faster-whisper drops the start of a longer one, the sentence that keeps the language), and the jargon
    still comes after these words."""
    words = list(own)
    seen = {word.lower() for word in words}
    length = len(', '.join(words))
    for word in learned:
        if word.lower() in seen:
            continue
        length += len(word) + (2 if words else 0)
        if length > PROMPT_CHARS:
            break
        seen.add(word.lower())
        words.append(word)
    return words


def effective_language(language, languages=('pl', 'en')):
    """A single language is forced even for 'auto'; invalid saved choices use the primary one."""
    if len(languages) == 1:
        return languages[0]
    return language if language in languages or language == 'auto' else languages[0]


def alternate_language(language, languages=('pl', 'en')):
    """Shift selects the other configured language, and is a no-op for a single language."""
    if len(languages) == 1:
        return languages[0]
    current = effective_language(language, languages)
    return languages[1] if current in (languages[0], 'auto') else languages[0]


def whisper_prompt(vocabulary, language):
    """The user's words, then (unless English is forced) the jargon they do not already contain. Whisper takes
    the prompt for the previous sentence of the talk and follows its language: a bare list of English
    jargon pushed forced-Polish dictation into English, so for Polish the words come in a Polish sentence
    (macOS SpeechModel.prompt, 2026-09-30)."""
    words = list(vocabulary)
    if language != 'en':
        words += [w for w in DEFAULT_VOCABULARY if w.lower() not in {v.lower() for v in words}]
    listed = ', '.join(words)
    if language == 'pl' and words:
        return 'Mówię po polsku, czasem wtrącam słowa: %s.' % listed
    if language == 'de' and words:
        return 'Ich spreche Deutsch und verwende manchmal diese Begriffe: %s.' % listed
    return listed


def looks_translated(text, detect):
    """A whole English result of Polish dictation: Whisper sometimes answers Polish speech in English even with
    the language forced. detect is core.detect_language; fewer than TRANSLATED_MIN_WORDS words never count."""
    return len(text.split()) >= TRANSLATED_MIN_WORDS and detect(text) == 'en'


# ---- TranscriptFilter: what Whisper writes from silence or noise ----------------------------------
# Whole utterances from the film subtitles it learnt from. A short "Dziękuję." may be a real answer;
# silence is refused earlier by SILENCE_PEAK.
HALLUCINATIONS = frozenset(('dzięki za obejrzenie', 'dziękuję za obejrzenie', 'do zobaczenia w następnym odcinku',
                            'zapraszam na kolejny odcinek', 'thank you for watching', 'thanks for watching'))
ANNOTATION = re.compile(r'\s*(\[[^\]]*\]|\([^)]*\)|\*[^*]*\*)\s*')  # [MUZYKA], (śmiech), *kaszel*


def _normalize(text):
    return re.sub(r'[.!?…,]+$', '', text.lower()).strip()


def filter_transcript(text, prompt=''):
    result = ANNOTATION.sub(' ', (text or '').strip())
    result = '\n'.join(line for line in result.split('\n') if line and 'amara.org' not in line.lower())
    result = re.sub(r'[ \t]{2,}', ' ', result).strip()
    normalized = _normalize(result)
    if normalized in HALLUCINATIONS or (prompt and normalized == _normalize(prompt)):
        return ''  # a subtitle line, or the prompt itself instead of speech
    return result


# ---- DictationTidy: fillers and times, without a model --------------------------------------------
FILLER = re.compile(r'(?<!%s)(?:y{2,}|e{2,}|m{2,}|hm+|ehm|yhm|um+|uh+|uhm|erm|äh+m?)(?!%s),?\s*' % (LETTER, LETTER),
                    re.IGNORECASE)
GERMAN_FILLER = re.compile(FILLER.pattern.replace('|um+', '|umm+'), FILLER.flags)  # 'um zehn Uhr' is not hesitation
# A time after a typical word ("o", "od", "do", "około"…), never a price or a version ("po 2.50 zł").
TIME = re.compile(r'(?<!%s)(o|od|do|około|ok\.|godz\.|godzina|godzinie|przed|po|między|a|na)\s+([01]?\d|2[0-3])'
                  r'\.([0-5]\d)(?!\d|[.,]\d)(?!\s*(?:zł|pln|%%|€|eur|usd|\$|gb|mb|kg|km|cm|m\b))' % LETTER, re.IGNORECASE)
AFTER_FILLERS = ((r'\s+([,.!?…])', r'\1'), (r',\s*,', ','), (r',([.!?…])', r'\1'), (r'^[,\s]+', ''), (r'[ \t]{2,}', ' '))


def tidy(text, language=None):
    result = _remove_fillers(text, language)
    if language in (None, 'pl', 'mixed'):
        result = TIME.sub(r'\1 \2:\3', result)
    return result if any(c.isalnum() for c in result) else ''  # fillers only ("Hmm."): nothing to insert


def _remove_fillers(text, language=None):
    matches = list((GERMAN_FILLER if language == 'de' else FILLER).finditer(text))
    if not matches:
        return text
    pieces, cursor, capitalize = [], 0, False
    for match in matches + [None]:
        fragment = text[cursor:match.start() if match else len(text)]
        if capitalize and fragment:
            fragment, capitalize = fragment[0].upper() + fragment[1:], False
        pieces.append(fragment)
        if match:
            # "Eee, zrobię to": the filler began the sentence, so now the next word does.
            capitalize = capitalize or match.group()[0].isupper()
            cursor = match.end()
    result = ''.join(pieces)
    for pattern, template in AFTER_FILLERS:
        result = re.sub(pattern, template, result)
    return result.strip(' \t')


# ---- Bielik's cleanup: when it is needed, and whether its answer can be trusted --------------------
NEEDS_CLEANUP = [re.compile(p % {'L': LETTER}) for p in (
    # fillers and stammering
    r'(?<!%(L)s)(y{2,}|e{2,}|m{2,}|hm+|yhm|ym+|um+|uh+|uhm|er+m?)(?!%(L)s)',
    # corrections in Polish
    r'(?<!%(L)s)(znaczy|to znaczy|czyli nie|nie,? nie|a nie|albo nie|nie,? jednak|jednak nie|chociaż nie|a właściwie|'
    r'właściwie nie|albo lepiej|czy raczej|czekaj|poczekaj|poprawka|pomyłka|wróć|cofam|cofnij|cofnijmy|skreśl|skreślmy|'
    r'zmieniam zdanie|źle powiedział%(L)s*|przepraszam|sorki|sorry)(?!%(L)s)',
    # corrections in English
    r'(?<!%(L)s)(i mean|no wait|wait|actually|actually no|or rather|let me rephrase|scratch that|strike that)(?!%(L)s)',
    # German hesitations and self-corrections
    r'(?<!%(L)s)(äh+m?|also nein|nein,? ich meine|ich meine|oder vielmehr|ich korrigiere|entschuldigung)(?!%(L)s)',
    # asides
    r'(?<!%(L)s)(no więc|no to|jakby|you know|kind of like)(?!%(L)s)',
    # a repeated word: "że że", "the the"
    r'(?<!%(L)s)(%(L)s+)[ ,]+\1(?!%(L)s)',
)]


def _words(text):
    return [w for w in re.split(r'[\W_]+', text.lower()) if w]


def restarts_sentence(text):
    """The same three words twice ("myślałem, że zrobi… myślałem, że zrobi…")."""
    words = _words(text)
    if len(words) < 6:
        return False
    trigrams = [' '.join(words[i:i + 3]) for i in range(len(words) - 2)]
    return len(set(trigrams)) < len(trigrams)


def needs_cleanup(text, language=None):
    """Whisper punctuates by itself: the model is needed only for fillers, stammering and corrections.
    A false hit costs time, a miss leaves both versions in the text."""
    lower = text.lower()
    patterns = NEEDS_CLEANUP
    if language == 'de':
        patterns = [re.compile(patterns[0].pattern.replace('|um+', '|umm+'))] + patterns[1:]
    return any(p.search(lower) for p in patterns) or restarts_sentence(lower)


def cleanup_too_long(raw):
    """Conservative cleanup limit: assume 7.8 tokens/s and 2.3 UTF-8 bytes per token."""
    return len(raw.encode('utf-8')) / 2.3 / 7.8 + 0.5 > CLEANUP_SECONDS


def cleanup_looks_safe(cleaned, transcript):
    """The model sometimes answers a dictated question or adds content: the result must be mostly the
    transcript's words (or words sharing their first four letters) and of a similar length."""
    source, result = _words(transcript), _words(cleaned)
    if not result:
        return False
    if not source:
        return True
    if not 0.15 <= len(result) / len(source) <= 1.4:
        return False
    known, stems = set(source), {w[:4] for w in source}
    return sum(w in known or w[:4] in stems for w in result) / len(result) >= 0.6


def cleanup_messages(transcript, vocabulary, detect, prompts=None, languages=('pl', 'en')):
    """The messages for the model (macOS Backend.dictationMessages). detect: core.detect_language, passed in so that this
    module stays free of the network code. prompts: the user's prompt files by default (prompts.py)."""
    p = (prompts or Prompts.current()).for_languages(languages)
    system = p.text('cleanup.txt')
    if vocabulary:
        system += SPELLING_RULE % ', '.join(vocabulary)
    names = p.get('names.json')['language']

    def user(text):
        return p.text('cleanup-user.txt').replace('{language}', names[detect(text)]).replace('{text}', text)
    messages = [{'role': 'system', 'content': system}]
    for source, output in p.get('cleanup-examples.json'):
        messages += [{'role': 'user', 'content': user(source)}, {'role': 'assistant', 'content': output}]
    return messages + [{'role': 'user', 'content': user(transcript)}]
