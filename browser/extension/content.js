// Skrypt w każdej ramce strony. Na prośbę aplikacji (przez background.js) podaje tekst aktywnego pola,
// zaznaczenie i kursor oraz wykonuje podmiany i wstawienia. Zakresy w jednostkach UTF-16 (jak NSString),
// odpowiedzi w kształcie `ImeProtocol.Reply` aplikacji.
(() => {
  // Ponowne wstrzyknięcie (po aktualizacji wtyczki): stara kopia bez `chrome.runtime.id` jest martwa.
  if (globalThis.__gramadrama && globalThis.__gramadrama.alive()) return;
  globalThis.__gramadrama = { alive: () => Boolean(chrome.runtime && chrome.runtime.id) };

  const MAX_WINDOW = 6000;
  const TEXT_INPUT_TYPES = new Set(['', 'text', 'search', 'url', 'tel']);
  const ids = new WeakMap();
  let nextId = 1;

  function fieldId(element) {
    if (!ids.has(element)) ids.set(element, nextId++);
    return ids.get(element);
  }

  function deepActiveElement() {
    let element = document.activeElement;
    while (element && element.shadowRoot && element.shadowRoot.activeElement) element = element.shadowRoot.activeElement;
    return element;
  }

  function isSecure(element) {
    const autocomplete = (element.getAttribute('autocomplete') || '').toLowerCase();
    return autocomplete.startsWith('cc-') || autocomplete === 'one-time-code' || autocomplete.includes('password');
  }

  /// Aktywne pole: { element, kind: 'plain' | 'rich' | 'secure' } albo null (fokus poza polem tekstowym).
  function activeField() {
    const element = deepActiveElement();
    if (!element) return null;
    if (element instanceof HTMLTextAreaElement) {
      if (element.readOnly || element.disabled) return null;
      return { element, kind: isSecure(element) ? 'secure' : 'plain' };
    }
    if (element instanceof HTMLInputElement) {
      const type = (element.getAttribute('type') || '').toLowerCase();
      if (type === 'password') return { element, kind: 'secure' };
      if (!TEXT_INPUT_TYPES.has(type) || element.readOnly || element.disabled) return null;
      return { element, kind: isSecure(element) ? 'secure' : 'plain' };
    }
    if (element.isContentEditable) {
      // Korzeń edycji: najwyższy przodek, który wciąż jest edytowalny.
      let root = element;
      while (root.parentElement && root.parentElement.isContentEditable) root = root.parentElement;
      return { element: root, kind: 'rich' };
    }
    return null;
  }

  // MARK: - Model tekstu contenteditable

  const BLOCK_DISPLAYS = new Set(['block', 'list-item', 'flex', 'grid', 'table', 'table-row', 'table-caption', 'flow-root']);

  /// Tekst pola jak widzi go użytkownik: bloki i <br> jako „\n”, obrazy jako U+FFFC, wyspy contenteditable=false
  /// (wzmianki, chipy) z tekstem, ale oznaczone. Każdy znak tekstu odpowiada jednemu znakowi węzła (bez zwijania
  /// spacji — tylko tabulacje i końce linii w zwykłym HTML zamieniane na spację), żeby pozycje wracały do DOM 1:1.
  function buildModel(root) {
    let text = '';
    const segments = [];
    let pendingBreak = false;
    const styles = new Map();
    const style = (element) => {
      if (!styles.has(element)) styles.set(element, getComputedStyle(element));
      return styles.get(element);
    };
    const push = (value, node, kind, island) => {
      if (pendingBreak && text.length && !text.endsWith('\n')) {
        segments.push({ start: text.length, length: 1, node: null, kind: 'virtual', island: false });
        text += '\n';
      }
      pendingBreak = false;
      segments.push({ start: text.length, length: value.length, node, kind, island });
      text += value;
    };
    const visit = (node, island) => {
      if (node.nodeType === Node.TEXT_NODE) {
        let value = node.data;
        if (!value) return;
        const parent = node.parentElement;
        const whiteSpace = parent ? style(parent).whiteSpace : 'normal';
        if (!whiteSpace.startsWith('pre') && whiteSpace !== 'break-spaces') {
          // Odstępy formatowania HTML między blokami.
          if (/^[ \t\n\r]*$/.test(value) && /[\n\r]/.test(value)) return;
          value = value.replace(/[\t\n\r]/g, ' ');
        }
        push(value, node, 'text', island);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const tag = node.tagName;
      if (tag === 'BR') return push('\n', node, 'br', island);
      if (tag === 'IMG') return push('￼', node, 'object', island);
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'TEMPLATE' || tag === 'NOSCRIPT') return;
      const computed = style(node);
      if (computed.display === 'none') return;
      const block = node !== root && BLOCK_DISPLAYS.has(computed.display);
      const innerIsland = island || node.getAttribute('contenteditable') === 'false';
      if (block) pendingBreak = true;
      for (const child of node.childNodes) visit(child, innerIsland);
      if (block) pendingBreak = true;
    };
    for (const child of root.childNodes) visit(child, false);
    return { text, segments };
  }

  /// Pozycja w tekście modelu dla punktu DOM (np. z zaznaczenia).
  function positionOf(model, node, offset) {
    if (node.nodeType === Node.TEXT_NODE) {
      const segment = model.segments.find((s) => s.node === node);
      if (segment) return segment.start + Math.min(offset, segment.length);
    }
    const point = document.createRange();
    point.setStart(node, offset);
    for (const segment of model.segments) {
      if (!segment.node) continue;
      // Pierwszy węzeł, który zaczyna się w punkcie albo za nim.
      if (point.comparePoint(segment.node, 0) >= 0) return segment.start;
    }
    return model.text.length;
  }

  /// Punkt DOM dla pozycji. Początek zakresu woli początek następnego węzła, koniec — koniec poprzedniego.
  function pointAt(model, position, isEnd) {
    let fallback = null;
    for (const segment of model.segments) {
      if (segment.kind !== 'text') continue;
      const end = segment.start + segment.length;
      if (position < segment.start || position > end) continue;
      const point = { node: segment.node, offset: position - segment.start };
      if (!isEnd && position === end) { fallback = fallback || point; continue; }
      if (isEnd && position === segment.start && segment.length > 0) { fallback = fallback || point; continue; }
      return point;
    }
    return fallback;
  }

  // MARK: - Odczyt

  function readField(field) {
    if (field.kind === 'plain') {
      const { value, selectionStart, selectionEnd } = field.element;
      return { text: value, start: selectionStart ?? value.length, end: selectionEnd ?? value.length, model: null };
    }
    const model = buildModel(field.element);
    let start = model.text.length, end = model.text.length;
    const selection = getSelection();
    if (selection && selection.rangeCount && field.element.contains(selection.anchorNode)) {
      const range = selection.getRangeAt(0);
      start = positionOf(model, range.startContainer, range.startOffset);
      end = positionOf(model, range.endContainer, range.endOffset);
    }
    return { text: model.text, start: Math.min(start, end), end: Math.max(start, end), model };
  }

  /// Brzeg okna nie może rozciąć pary surogatów.
  function safeStart(text, index) {
    const code = text.charCodeAt(index);
    return index > 0 && code >= 0xdc00 && code <= 0xdfff ? index + 1 : index;
  }
  function safeEnd(text, index) {
    const code = text.charCodeAt(index - 1);
    return index < text.length && code >= 0xd800 && code <= 0xdbff ? index - 1 : index;
  }

  /// Ramka kursora w układzie ekranu z lewym górnym rogiem (przybliżenie: powiększenie 100%, pasek narzędzi
  /// nad stroną). Tylko w głównej ramce — położenia ramki zagnieżdżonej w oknie nie znamy.
  function caretTopLeft(field) {
    if (window !== window.top) return undefined;
    let rect = null;
    if (field.kind === 'rich') {
      const selection = getSelection();
      if (selection && selection.rangeCount) {
        const rects = selection.getRangeAt(0).getClientRects();
        if (rects.length) rect = rects[rects.length - 1];
      }
    }
    if (!rect || (rect.width === 0 && rect.height === 0)) rect = field.element.getBoundingClientRect();
    const top = window.screenY + (window.outerHeight - window.innerHeight);
    return [window.screenX + rect.left, top + rect.top, Math.max(rect.width, 1), rect.height];
  }

  function context(field, client) {
    const { text, start, end } = readField(field);
    const windowStart = safeStart(text, Math.max(0, start - MAX_WINDOW));
    const afterEnd = safeEnd(text, Math.min(text.length, end + MAX_WINDOW));
    const tooLong = end - start > MAX_WINDOW;
    return {
      ok: true,
      client,
      windowStart,
      selectionLocation: start,
      selectionLength: end - start,
      before: text.slice(windowStart, start),
      selected: tooLong ? undefined : text.slice(start, end),
      after: text.slice(end, afterEnd),
      shortAfter: afterEnd === text.length,
      selectionTooLong: tooLong || undefined,
      caretTopLeft: caretTopLeft(field),
    };
  }

  // MARK: - Położenie na ekranie (sprawdzanie na żywo)

  // Ostatnia pozycja myszy nad tą ramką: ekran (względem okna) i strona. Z niej aplikacja liczy dokładny początek
  // strony na ekranie — także z paskiem bocznym, kartami pionowymi i w ramkach zagnieżdżonych.
  let mouse = null;
  document.addEventListener('mousemove', (event) => {
    mouse = [event.screenX - window.screenX, event.screenY - window.screenY, event.clientX, event.clientY];
  }, { passive: true, capture: true });

  // Ramka zagnieżdżona: gdzie jej (0, 0) leży w ramce głównej (piksele CSS). Ramka pyta rodzica przez postMessage (działa
  // też między domenami), rodzic odpowiada położeniem jej elementu <iframe> plus własnym — i tak do góry. Ramka główna
  // dokłada swój rozmiar i ostatnią pozycję myszy: z nich aplikacja liczy początek strony w oknie, a mysz nad samą
  // ramką zagnieżdżoną nie jest potrzebna (i nie starzeje się, gdy strona główna się przewinie).
  const FRAME_ASK = 'gramadramaFrameAsk', FRAME_ANSWER = 'gramadramaFrameAnswer';
  const FRAME_WAIT_MS = 150;
  const frameAsked = new Map();

  function frameOrigin() {
    if (window === window.top) {
      return Promise.resolve({ origin: [0, 0], inner: [window.innerWidth, window.innerHeight], mouse });
    }
    return new Promise((resolve) => {
      const id = crypto.randomUUID();
      const timer = setTimeout(() => { frameAsked.delete(id); resolve(null); }, FRAME_WAIT_MS);
      frameAsked.set(id, (answer) => { clearTimeout(timer); resolve(answer); });
      window.parent.postMessage({ [FRAME_ASK]: id }, '*');
    });
  }

  const finite = (list, length) => Array.isArray(list) && list.length === length && list.every(Number.isFinite);

  window.addEventListener('message', (event) => {
    const data = event.data;
    if (!data || typeof data !== 'object') return;
    if (typeof data[FRAME_ASK] === 'string' && event.source) {
      // Pyta ramka w tej stronie: który to <iframe>.
      const frame = [...document.querySelectorAll('iframe, frame')].find((element) => element.contentWindow === event.source);
      if (!frame) return;
      const id = data[FRAME_ASK];
      frameOrigin().then((here) => {
        if (!here) return;
        const box = frame.getBoundingClientRect();
        const style = getComputedStyle(frame);
        const x = here.origin[0] + box.left + frame.clientLeft + (parseFloat(style.paddingLeft) || 0);
        const y = here.origin[1] + box.top + frame.clientTop + (parseFloat(style.paddingTop) || 0);
        event.source.postMessage({ [FRAME_ANSWER]: id, origin: [x, y], inner: here.inner, mouse: here.mouse }, '*');
      });
    } else if (typeof data[FRAME_ANSWER] === 'string' && event.source === window.parent) {
      const done = frameAsked.get(data[FRAME_ANSWER]);
      if (!done || !finite(data.origin, 2) || !finite(data.inner, 2)) return;
      frameAsked.delete(data[FRAME_ANSWER]);
      done({ origin: data.origin, inner: data.inner, mouse: finite(data.mouse, 4) ? data.mouse : null });
    }
  });

  /// Surowe dane do przeliczenia prostokątów strony na ekran (przelicza aplikacja: zna skalę ekranu). `frame` tylko w ramce
  /// zagnieżdżonej, gdy rodzice odpowiedzieli: jej początek w ramce głównej, rozmiar i mysz ramki głównej.
  async function geometry() {
    const top = window === window.top;
    return {
      dpr: window.devicePixelRatio,
      win: [window.screenX, window.screenY, window.outerWidth, window.outerHeight, window.innerWidth, window.innerHeight],
      mouse,
      top,
      frame: top ? undefined : (await frameOrigin()) || undefined,
    };
  }

  const rectArray = (rect) => [rect.left, rect.top, rect.width, rect.height];

  function intersect(a, b) {
    const left = Math.max(a.left, b.left), top = Math.max(a.top, b.top);
    const right = Math.min(a.right, b.right), bottom = Math.min(a.bottom, b.bottom);
    return right > left && bottom > top ? { left, top, right, bottom, width: right - left, height: bottom - top } : null;
  }

  /// Widoczna część pola w stronie: przycięta do przewijanych przodków i okna.
  function visibleRect(element) {
    let rect = element.getBoundingClientRect();
    for (let parent = element.parentElement, depth = 0; parent && rect && depth < 12; parent = parent.parentElement, depth++) {
      const overflow = getComputedStyle(parent);
      if (overflow.overflowX !== 'visible' || overflow.overflowY !== 'visible') rect = intersect(rect, parent.getBoundingClientRect());
    }
    rect = rect && intersect(rect, { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight });
    return rect ? rectArray(rect) : null;
  }

  const MIRROR_STYLES = ['boxSizing', 'width', 'height', 'borderTopWidth', 'borderRightWidth', 'borderBottomWidth',
    'borderLeftWidth', 'borderStyle', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'fontStyle',
    'fontVariant', 'fontWeight', 'fontStretch', 'fontSize', 'fontFamily', 'lineHeight', 'textAlign', 'textTransform',
    'textIndent', 'letterSpacing', 'wordSpacing', 'tabSize', 'direction'];

  /// Prostokąty zakresów w textarea/input: niewidoczna kopia pola z tym samym stylem i przewinięciem.
  function plainRects(element, ranges) {
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    const mirror = document.createElement('div');
    for (const name of MIRROR_STYLES) mirror.style[name] = style[name];
    Object.assign(mirror.style, {
      position: 'fixed', left: `${box.left}px`, top: `${box.top}px`, visibility: 'hidden', overflow: 'hidden',
      whiteSpace: element instanceof HTMLTextAreaElement ? 'pre-wrap' : 'pre', overflowWrap: 'break-word', pointerEvents: 'none',
    });
    mirror.textContent = element.value + '\u200b';
    document.documentElement.appendChild(mirror);
    mirror.scrollTop = element.scrollTop;
    mirror.scrollLeft = element.scrollLeft;
    const node = mirror.firstChild;
    const result = ranges.map(([location, length]) => {
      if (location < 0 || location + length > element.value.length) return [];
      const range = document.createRange();
      range.setStart(node, location);
      range.setEnd(node, location + length);
      return [...range.getClientRects()].map((rect) => intersect(rect, box)).filter(Boolean).map(rectArray);
    });
    mirror.remove();
    return result;
  }

  function richRects(root, ranges) {
    const model = buildModel(root);
    return ranges.map(([location, length]) => {
      const from = pointAt(model, location, false);
      const to = pointAt(model, location + length, true);
      if (!from || !to) return [];
      const range = document.createRange();
      range.setStart(from.node, from.offset);
      range.setEnd(to.node, to.offset);
      return [...range.getClientRects()].filter((rect) => rect.width > 0 && rect.height > 0).map(rectArray);
    });
  }

  async function rects(field, request) {
    const ranges = Array.isArray(request.ranges) ? request.ranges.slice(0, 200) : [];
    const words = field.kind === 'plain' ? plainRects(field.element, ranges) : richRects(field.element, ranges);
    return { ok: true, words, field: visibleRect(field.element), geometry: await geometry() };
  }

  // MARK: - Zapis

  const comparable = (text) => text.replace(/ /g, ' ');

  function currentText(field) {
    return field.kind === 'plain' ? field.element.value : buildModel(field.element).text;
  }

  /// Jedna podmiana w polu. Zwraca kod odmowy albo null (zrobione).
  function applyEdit(field, edit) {
    const { location, length, text } = edit;
    if (field.kind === 'plain') {
      field.element.setSelectionRange(location, location + length);
      if (!document.execCommand('insertText', false, text)) {
        // Pole, które nie przyjmuje poleceń edycji: zapis wprost i zdarzenie dla frameworków (React itp.).
        field.element.setRangeText(text, location, location + length, 'end');
        field.element.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertReplacementText', data: text }));
      }
      return null;
    }
    const model = buildModel(field.element);
    const original = model.text.slice(location, location + length);
    if (original.includes('\n') || original.includes('￼')) return 'not_text';
    const touchesIsland = model.segments.some((s) => s.island
      && s.start < location + Math.max(length, 1) && s.start + s.length > location);
    if (touchesIsland) return 'not_text';
    const from = pointAt(model, location, false);
    const to = pointAt(model, location + length, true);
    if (!from || !to) return 'not_text';
    const range = document.createRange();
    range.setStart(from.node, from.offset);
    range.setEnd(to.node, to.offset);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    // Polecenie edycji przechodzi przez beforeinput, więc edytory (ProseMirror, Lexical, Gmail) aktualizują swój stan.
    const done = text === '' ? document.execCommand('delete', false) : document.execCommand('insertText', false, text);
    return done ? null : 'exec_failed';
  }

  /// Kursor po podmianie: przesunięty o zmiany długości poprawek przed nim; w środku poprawionego słowa — za nim.
  function restoreCaret(field, caret, edits) {
    let wanted = caret;
    for (const edit of edits) {
      if (edit.location + edit.length <= caret) wanted += edit.text.length - edit.length;
      else if (edit.location < caret) wanted += edit.location + edit.text.length - caret;
    }
    if (field.kind === 'plain') {
      field.element.setSelectionRange(wanted, wanted);
      return;
    }
    const point = pointAt(buildModel(field.element), wanted, true) || pointAt(buildModel(field.element), wanted, false);
    if (!point) return;
    const selection = getSelection();
    selection.removeAllRanges();
    selection.collapse(point.node, point.offset);
  }

  /// Czeka (do 300 ms), aż warunek będzie spełniony — edytory potrafią zastosować zmianę asynchronicznie.
  async function landed(condition) {
    const started = performance.now();
    while (performance.now() - started < 300) {
      if (condition()) return Math.round(performance.now() - started);
      await new Promise((resolve) => setTimeout(resolve, 15));
    }
    return condition() ? 300 : null;
  }

  async function replace(field, request, expired) {
    const { check, edits, expected } = request;
    if (!check || !Array.isArray(edits) || typeof expected !== 'string') return { ok: false, reason: 'bad_request', attempted: false };
    const before = currentText(field);
    if (comparable(before.slice(check.location, check.location + check.length)) !== comparable(check.text)) {
      return { ok: false, reason: 'changed', attempted: false };
    }
    if (expired()) return { ok: false, reason: 'expired', attempted: false };
    const caret = readField(field).end;
    field.element.focus();
    let attempted = false;
    for (const edit of edits) {
      const refusal = applyEdit(field, edit);
      if (refusal) return { ok: false, reason: refusal, attempted };
      attempted = true;
    }
    const newLength = expected.length;
    const after = await landed(() =>
      comparable(currentText(field).slice(check.location, check.location + newLength)) === comparable(expected));
    if (after === null) return { ok: false, reason: 'not_landed', attempted: true };
    restoreCaret(field, caret, edits);
    return { ok: true, attempted: true, landedAfterMs: after };
  }

  async function insert(field, request, expired) {
    if (typeof request.text !== 'string' || !request.text) return { ok: false, reason: 'bad_request', attempted: false };
    if (expired()) return { ok: false, reason: 'expired', attempted: false };
    const { text: before, start } = readField(field);
    // Gdzie szukać wstawionego tekstu w polu bez końców linii (edytor może je zamienić): tyle znaków przed kursorem.
    const from = Math.max(0, comparable(before.slice(0, start)).replace(/\n/g, '').length - 1);
    field.element.focus();
    let done = document.execCommand('insertText', false, request.text);
    if (!done && field.kind === 'plain') {
      const { selectionStart, selectionEnd } = field.element;
      field.element.setRangeText(request.text, selectionStart, selectionEnd, 'end');
      field.element.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: request.text }));
      done = true;
    }
    if (!done) return { ok: false, reason: 'exec_failed', attempted: true };
    const text = comparable(request.text.replace(/\n/g, ''));
    const after = await landed(() => comparable(currentText(field).replace(/\n/g, '')).includes(text, from));
    return after === null ? { ok: false, reason: 'not_landed', attempted: true } : { ok: true, attempted: true, landedAfterMs: after };
  }

  async function handle(request, tabId, frameId) {
    const received = performance.now();
    const expired = () => typeof request.expiresInMs === 'number' && performance.now() - received > request.expiresInMs;
    const field = activeField();
    if (!field) return { ok: false, reason: request.client ? 'client_changed' : 'no_client', attempted: false };
    if (field.kind === 'secure') return { ok: false, reason: 'secure_input', attempted: false };
    const client = `${tabId}:${frameId}:${fieldId(field.element)}`;
    // Podmiana i wstawienie tylko w tym samym polu, z którego aplikacja pobrała tekst.
    if (request.client && request.client !== client) return { ok: false, reason: 'client_changed', attempted: false };
    switch (request.op) {
      case 'hello':
        return { ok: true };
      case 'context':
        return context(field, client);
      case 'live':
        // Sprawdzanie na żywo: tekst pola i jego widoczna część, bez prostokąta kursora.
        return { ...context(field, client), caretTopLeft: undefined, field: visibleRect(field.element), geometry: await geometry() };
      case 'rects':
        return rects(field, request);
      case 'replace':
        return replace(field, request, expired);
      case 'insert':
        return insert(field, request, expired);
      default:
        return { ok: false, reason: 'bad_request', attempted: false };
    }
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || !message.gramadrama || sender.id !== chrome.runtime.id) return false;
    const request = message.gramadrama;
    const writes = request.op === 'replace' || request.op === 'insert';
    handle(request, message.tabId, message.frameId).then(sendResponse, () =>
      sendResponse(writes ? { ok: false, reason: 'extension_error' } : { ok: false, reason: 'no_client', attempted: false }));
    return true;
  });
})();
