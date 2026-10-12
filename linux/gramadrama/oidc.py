"""Logging in to servers through an OpenID Connect provider such as Pocket ID (docs/remote-access.md).

A server out of the home network sits behind a reverse proxy that wants `Authorization: Bearer <access token>`
of a provider the user logs in to once, in the browser, with a passkey. This is the client side, as on the Mac
(RemoteAccess.swift): authorization code with PKCE, a public client (no secret),
the redirect gramadrama://auth, the scope offline_access so that a refresh token keeps the login for months.

- start_login(settings) makes the URL the browser opens (the verifier and the state wait in auth-pending.json,
  mode 0600, for ten minutes); finish_login(url) takes the address the browser came back to, checks the state
  and trades the code for the tokens, saved in auth.json (mode 0600).
- access_token(settings) is what every request asks for: the saved token while it has a minute left, a refreshed one
  after. The engine, its children and the app all refresh, so it is done under a file lock and a process that
  waited for the lock first looks whether another one has already done it (the provider may rotate refresh tokens:
  using an old one twice would log everybody out).
- authorization(settings, url, key) is the header's value for a request. The token goes ONLY to the provider's own
  origin (scheme, host and port of the issuer): a server elsewhere never sees it. A key the user typed for a
  server wins over the login.

Pure: standard library only, no gi. core.py imports it, so it imports core.py's network helpers inside functions.
Tokens are never logged, never in a message and never in a repr."""
import base64
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import paths
from .i18n import tr

REDIRECT_URI = 'gramadrama://auth'
SCOPE = 'openid profile offline_access'
USER_AGENT = 'GramaDrama-Linux'  # a proxy in front of a server (Cloudflare) may turn Python's own away
EXPIRY_MARGIN = 60  # seconds: a token with less left is refreshed first
PENDING_SECONDS = 600
LOCK_SECONDS = 30
DISCOVERY = '/.well-known/openid-configuration'
LOGIN_HINT = 'Zaloguj się w aplikacji GramaDrama (Usługi › Dostęp zdalny).'


class AuthError(Exception):
    """A login that does not work, with a localized message that is shown as it is."""


class LoggedOut(AuthError):
    """There is no login (any more): the user has to log in again."""


class AuthUnreachable(AuthError):
    """The provider could not be asked (network, a server error): a login that may well be fine."""


# ---- addresses ----------------------------------------------------------------------------------
def origin(url):
    """(scheme, host, port) of an address: what must be equal for the token to go with a request."""
    parts = urllib.parse.urlsplit((url or '').strip())
    try:
        port = parts.port
    except ValueError:
        port = None
    return parts.scheme.lower(), (parts.hostname or '').lower(), port or {'http': 80, 'https': 443}.get(parts.scheme.lower())


def issuer_of(settings):
    return (settings.auth_issuer or '').strip().rstrip('/')


def configured(settings):
    return bool(issuer_of(settings) and (settings.auth_client_id or '').strip())


def applies(settings, url):
    """True when a request to url carries the login's token: an issuer is set and url is on its origin."""
    issuer = issuer_of(settings)
    return bool(issuer) and origin(url) == origin(issuer)


def issuer_problem(value):
    """Why value is no issuer address (a localized reason without a full stop), or None. The tokens are secrets of an
    account: over the internet they only travel encrypted, so https, unless the host is on this computer's network."""
    from .core import lan_host, url_problem
    if not value.strip():
        return None
    problem = url_problem(value)
    if problem:
        return problem.rstrip('.')
    parts = urllib.parse.urlsplit(value.strip())
    if parts.scheme != 'https' and not lan_host(parts.hostname):
        return tr('Adres logowania spoza sieci domowej musi zaczynać się od https://')
    return None


# ---- the files ----------------------------------------------------------------------------------
def _pending_path():
    return paths.auth_path().with_name('auth-pending.json')


def _lock_path():
    return paths.auth_path().with_name('auth.lock')


def _read(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path, data):
    """data as a file only the user reads, put in place whole (a process reading meanwhile sees the old one or the
    new one, never half of it)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(json.dumps(data, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def _remove(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


class _Lock:
    """An exclusive lock on auth.lock between all processes (and threads) of the user; waits up to LOCK_SECONDS."""

    def __enter__(self):
        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() > deadline:
                    os.close(self.fd)
                    raise AuthUnreachable(tr('Logowanie jest zajęte. Spróbuj za chwilę.'))
                time.sleep(0.05)

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        return False


def _tokens(settings):
    """The saved login when it is the one of the settings' provider and client, else {}."""
    data = _read(paths.auth_path())
    if data.get('issuer') == issuer_of(settings) and data.get('client_id') == (settings.auth_client_id or '').strip() \
            and isinstance(data.get('access_token'), str) and data['access_token']:
        return data
    return {}


# ---- the provider -------------------------------------------------------------------------------
def _request(url, form=None, timeout=15, bearer=''):
    """(status, JSON object) of a request to the provider; a status 0 and {} when it cannot be reached. Never
    raises for an HTTP error: the provider's own words ({"error": "invalid_grant"}) decide what it means."""
    from .core import opener_for
    headers = {'Accept': 'application/json', 'User-Agent': USER_AGENT}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    if bearer:
        headers['Authorization'] = 'Bearer ' + bearer
    try:
        with opener_for(url).open(urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as response:
            status, body = response.status, response.read(1 << 20)
    except urllib.error.HTTPError as exc:
        try:
            status, body = exc.code, exc.read(1 << 16)
        except OSError:
            status, body = exc.code, b''
        finally:
            exc.close()
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return 0, {}
    try:
        parsed = json.loads(body.decode('utf-8', 'replace'))
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _endpoint_problem(url):
    """Why the provider's own endpoint is not used (see issuer_problem: https unless on this network), or None."""
    from .core import lan_host
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname or parts.scheme not in ('http', 'https') or (parts.scheme != 'https' and not lan_host(parts.hostname)):
        return tr('tylko https')
    return None


def discover(issuer):
    """The provider's endpoints from its discovery document: {'authorization', 'token', 'userinfo'} (the last may be
    ''). AuthError when it cannot be read or is no provider."""
    issuer = issuer.strip().rstrip('/')
    problem = issuer_problem(issuer)
    if problem or not issuer:
        raise AuthError((problem or tr('Podaj adres logowania')) + '.')
    status, document = _request(issuer + DISCOVERY)
    if status == 0:
        raise AuthUnreachable(tr('Nie można połączyć się z serwerem logowania. Sprawdź adres i sieć.'))
    if status != 200 or not isinstance(document.get('authorization_endpoint'), str) \
            or not isinstance(document.get('token_endpoint'), str):
        raise AuthError(tr('Pod tym adresem nie ma serwera logowania (OpenID Connect). Sprawdź adres.'))
    endpoints = {'authorization': document['authorization_endpoint'], 'token': document['token_endpoint'],
                 'userinfo': document.get('userinfo_endpoint') if isinstance(document.get('userinfo_endpoint'), str) else ''}
    for url in endpoints.values():
        if url and _endpoint_problem(url):
            raise AuthError(tr('Serwer logowania podaje adres, którego nie użyję (%s): %s.') % (_endpoint_problem(url), url[:80]))
    return endpoints


def pkce_challenge(verifier):
    """The S256 code challenge of a verifier (RFC 7636)."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')


# ---- logging in ---------------------------------------------------------------------------------
def start_login(settings):
    """The address to open in the browser. Remembers what finish_login needs; a login started before is dropped."""
    if not configured(settings):
        raise AuthError(tr('Podaj adres logowania i identyfikator klienta.'))
    endpoints = discover(issuer_of(settings))
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(24)
    _write(_pending_path(), {'state': state, 'verifier': verifier, 'issuer': issuer_of(settings),
                             'client_id': settings.auth_client_id.strip(), 'token_endpoint': endpoints['token'],
                             'userinfo_endpoint': endpoints['userinfo'], 'created': time.time()})
    parts = urllib.parse.urlsplit(endpoints['authorization'])
    query = urllib.parse.parse_qsl(parts.query) + [
        ('response_type', 'code'), ('client_id', settings.auth_client_id.strip()), ('redirect_uri', REDIRECT_URI),
        ('scope', SCOPE), ('state', state), ('code_challenge', pkce_challenge(verifier)), ('code_challenge_method', 'S256')]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def pending():
    """True while a login waits for the browser to come back."""
    data = _read(_pending_path())
    return bool(data) and time.time() - data.get('created', 0) < PENDING_SECONDS


def is_callback(url):
    return (url or '').strip().lower().startswith(REDIRECT_URI)


def finish_login(url):
    """Finish the login the browser came back from (url: gramadrama://auth?code=…&state=…). Returns the name of the
    account as the provider says it ('' when it does not). AuthError with a reason otherwise; a callback nobody
    asked for (no login started, another state, too late) is refused."""
    data = _read(_pending_path())
    if not data or time.time() - data.get('created', 0) > PENDING_SECONDS:
        raise AuthError(tr('Nie czekam na logowanie. Zacznij je w aplikacji GramaDrama (Usługi › Dostęp zdalny).'))
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url.strip()).query))
    if not hmac.compare_digest(str(query.get('state', '')), str(data.get('state', ''))):
        raise AuthError(tr('Odpowiedź przeglądarki nie pasuje do rozpoczętego logowania. Zacznij je jeszcze raz.'))
    if query.get('error'):
        _remove(_pending_path())
        raise AuthError(tr('Logowanie przerwane: %s.') % (query.get('error_description') or query['error'])[:200])
    if not query.get('code'):
        raise AuthError(tr('W odpowiedzi przeglądarki nie ma kodu logowania.'))
    status, reply = _request(data['token_endpoint'], {
        'grant_type': 'authorization_code', 'code': query['code'], 'redirect_uri': REDIRECT_URI,
        'client_id': data['client_id'], 'code_verifier': data['verifier']})
    if status == 0:
        raise AuthUnreachable(tr('Nie można połączyć się z serwerem logowania.'))
    tokens = _tokens_from(reply)
    if status != 200 or not tokens:
        _remove(_pending_path())
        raise AuthError(tr('Serwer logowania odmówił: %s.') % _reason(reply, status))
    name = ''
    if data.get('userinfo_endpoint'):
        _, info = _request(data['userinfo_endpoint'], timeout=6, bearer=tokens['access_token'])
        name = next((str(info[k]) for k in ('name', 'preferred_username', 'email') if info.get(k)), '')[:120]
    with _Lock():
        _write(paths.auth_path(), dict(tokens, issuer=data['issuer'], client_id=data['client_id'],
                                       token_endpoint=data['token_endpoint'], name=name))
    _remove(_pending_path())
    return name


def _tokens_from(reply, previous=None):
    """The fields a token response gives, or {} when it has no usable access token."""
    token = reply.get('access_token')
    if not isinstance(token, str) or not token or str(reply.get('token_type', 'Bearer')).lower() != 'bearer':
        return {}
    expires = reply.get('expires_in')
    lifetime = expires if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0 else 3600
    refresh = reply.get('refresh_token')
    if not isinstance(refresh, str) or not refresh:
        refresh = (previous or {}).get('refresh_token', '')  # a provider that does not rotate says nothing new
    return {'access_token': token, 'refresh_token': refresh, 'expires_at': time.time() + lifetime}


def _reason(reply, status):
    text = reply.get('error_description') or reply.get('error')
    return (str(text)[:200] if text else 'HTTP %d' % status)


# ---- using the login ----------------------------------------------------------------------------
def access_token(settings, rejected=''):
    """A token the servers of the provider's origin accept. rejected: the token one of them answered 401 to, which is
    then replaced even though it looks valid (unless another process has already put a new one in its place).
    LoggedOut when there is no login (or the provider ended it), AuthUnreachable when it cannot be asked and
    the saved token has run out."""
    tokens = _tokens(settings)
    if not tokens:
        raise LoggedOut(tr('Nie zalogowano. ') + tr(LOGIN_HINT))
    if not rejected and tokens.get('expires_at', 0) - EXPIRY_MARGIN > time.time():
        return tokens['access_token']
    with _Lock():
        tokens = _tokens(settings)  # another process may have refreshed while this one waited
        if not tokens:
            raise LoggedOut(tr('Nie zalogowano. ') + tr(LOGIN_HINT))
        if rejected:
            if tokens['access_token'] != rejected:
                return tokens['access_token']
        elif tokens.get('expires_at', 0) - EXPIRY_MARGIN > time.time():
            return tokens['access_token']
        return _refresh(settings, tokens, rejected)


def _refresh(settings, tokens, rejected):
    refresh = tokens.get('refresh_token')
    if not refresh:
        _remove(paths.auth_path())
        raise LoggedOut(tr('Logowanie wygasło. ') + tr(LOGIN_HINT))
    endpoint = tokens.get('token_endpoint')
    if not endpoint:
        endpoint = discover(issuer_of(settings))['token']
    status, reply = _request(endpoint, {'grant_type': 'refresh_token', 'refresh_token': refresh,
                                        'client_id': tokens['client_id']}, timeout=8)
    fresh = _tokens_from(reply, tokens) if status == 200 else {}
    if fresh:
        _write(paths.auth_path(), dict(tokens, **fresh))
        return fresh['access_token']
    if status in (400, 401, 403) and reply.get('error') in ('invalid_grant', 'invalid_token', 'unauthorized_client',
                                                            'invalid_client'):
        _remove(paths.auth_path())  # the provider ended this login (a revoked session, a refresh token used twice)
        raise LoggedOut(tr('Logowanie wygasło. ') + tr(LOGIN_HINT))
    if not rejected and tokens.get('expires_at', 0) > time.time():
        return tokens['access_token']  # the provider is out of reach but this token still works
    if status == 0 or status >= 500:
        raise AuthUnreachable(tr('Nie można odświeżyć logowania: serwer logowania nie odpowiada.'))
    raise AuthError(tr('Nie można odświeżyć logowania: %s.') % _reason(reply, status))


def authorization(settings, url, key='', rejected=''):
    """The value of the Authorization header for a request to url, or ''. A key the user typed for the server wins;
    the login's token is for the provider's own origin only. Raises AuthError (LoggedOut: not logged in)."""
    if key:
        return 'Bearer ' + key
    if not applies(settings, url):
        return ''
    return 'Bearer ' + access_token(settings, rejected)


def status(settings):
    """What to say about the login without asking anybody: ('off', '') when no provider is set, ('out', '') when
    there is none to log in with, ('in', name) when logged in (the token itself may be refreshed at the next request)."""
    if not issuer_of(settings):
        return 'off', ''
    tokens = _tokens(settings)
    if not tokens:
        return 'out', ''
    return 'in', str(tokens.get('name') or '')


def logout():
    """Forget the login (and a login that was started). The provider is not told: its session ends by itself, and a
    token that leaked from here would only live as long as it does anyway."""
    with _Lock():
        _remove(paths.auth_path())
    _remove(_pending_path())
