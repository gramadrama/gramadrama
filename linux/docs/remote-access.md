# Remote access and OpenID Connect

GramaDrama can use servers protected by OpenID Connect sign-in, e.g. Pocket ID.
The client signs in through the browser and authenticates the following requests with a token.
This document describes the Linux setup; compatibility with a particular provider needs to be checked.

```
GramaDrama ── Bearer token ──▶ reverse proxy ──▶ model / LanguageTool
     │                              │
     └── browser ──▶ OIDC ◀─────────┘ token check
```

## On the server

Register a public client (no secret) with PKCE, the redirect URI `gramadrama://auth`
and the scopes `openid profile offline_access`. Whether refresh tokens are allowed, and how long a session lasts,
depend on the provider. Give the right group of users access to the client.

The reverse proxy must verify the token before it passes a request on to the model or LanguageTool.
The `userinfo_endpoint` address comes from the provider's metadata at `/.well-known/openid-configuration`.
Example nginx configuration fragments, to be adapted to the provider and to an existing HTTPS server:

```nginx
location = /_auth {
    internal;
    proxy_pass https://id.example.com/USERINFO_PATH;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Authorization $http_authorization;
}
location /ollama/ {
    auth_request /_auth;
    proxy_pass http://127.0.0.1:11434/;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
location /languagetool/ {
    auth_request /_auth;
    proxy_pass http://127.0.0.1:8010/;
}
location = /whisper/inference {
    auth_request /_auth;
    client_max_body_size 8m;  # recordings up to 3 min
    proxy_pass http://127.0.0.1:8080/inference;
}
```

A complete gateway exposes only the paths the client uses, each behind the token check:

- `/ollama/api/chat` and `/ollama/api/generate` (POST, streamed answers), `/ollama/api/tags`,
  `/ollama/api/version` and `/ollama/api/ps` (GET);
- `/languagetool/v2/check` (POST) and `/languagetool/v2/languages` (GET);
- for whisper.cpp `/whisper/inference` (POST) and `/whisper/health` (GET); for an OpenAI-compatible
  speech server `/whisper/audio/transcriptions` (POST) and `/whisper/models` (GET).

Keep model management closed, e.g. Ollama's `/api/pull` and `/api/delete` and the whisper.cpp server's `/load`.
Limit the request size: a 3-minute recording is about 5.8 MB of WAV (16 kHz, 16-bit); the other requests are much
smaller. The services behind the gateway do not need the token.

These are fragments, not a complete HTTPS configuration. You need the nginx `auth_request` module,
the right token-checking endpoint and access control for the services behind the proxy.

The client sends the token only to the same **origin** as the configured sign-in address:
the scheme, host and port must match. In practice the provider and the protected services can be
served at one address, e.g. `https://gramadrama.example.com`, under separate paths.
Services on another origin need another mechanism, e.g. a separate API key.

## In the app

**Remote access** (*Dostęp zdalny*) is the last group of the **Services** (*Usługi*) tab and stays collapsed until it is set up.
**Set up…** (*Skonfiguruj…*) shows two fields: **Sign-in address** (*Adres logowania*; e.g. `https://gramadrama.example.com`)
and **Client ID** (*Identyfikator klienta*). Once they are saved, the **Account** (*Konto*) and **Connection** (*Połączenie*)
rows appear. **Sign in…** (*Zaloguj się…*) opens the browser. After you sign in, the browser opens `gramadrama://auth`
and the app saves the session. If the browser did not start the app, paste the whole return address
into the field below the **Account** row. The address contains a short-lived sign-in code: do not publish it.

**Sign out** (*Wyloguj*) removes the tokens from the computer. **Turn remote access off** (*Wyłącz dostęp zdalny*)
also removes the sign-in address and the client ID. Changing the address or the ID ends the sign-in, because the token
belongs to the previous provider. An API key of your own set for a service takes precedence over the OIDC token, but it
is never sent to the gateway.

## The gateway and the route

The gateway is the origin of the sign-in address, as on macOS. The services are under the paths
`/ollama` (the model, with the Ollama protocol), `/languagetool` and `/whisper` (the speech server, with the same
protocol as in the settings). There is no need to enter separate fallback addresses. The settings from before
the Services tab (`llm_remote_url`, `languagetool_remote_url`) are no longer read; they were the same
gateway paths.

Each service chooses its route separately:

- a service on this computer, a server at this computer's address and a model server with the OpenAI-compatible
  protocol always connect directly;
- the server from the settings is asked for its status (`/api/version`, `/models`, `/v2/languages` or `/health`);
  any HTTP answer, including 401, means that it is on this network, and the request goes to it directly, without the token;
- when the server does not answer and you are signed in, the request goes through the gateway with the token;
  when you are not signed in, the service's row and the **Connection** row ask you to sign in;
- a server without an address means the gateway alone (e.g. access only from outside).

The check has a 1.2 s limit. The answers are shared by the processes in a cache:
a positive one for 5 minutes, a negative one for 30 s. A change of network refreshes all of them, and a connection error
only the answer for that server. The **Connection** row shows which services currently go directly
and which through the gateway. `GRAMADRAMA_FORCE_REMOTE=1` forces the route through the gateway, for testing from home.

## Tokens and privacy

The tokens are stored in `auth.json` in the GramaDrama configuration directory, with 0600 permissions.
Refreshing is synchronised between processes so that a rotated token is not used twice.
Tokens never go into logs or error messages.

The address of a provider on the internet must use HTTPS. An intermediary that terminates TLS, e.g. a reverse proxy
or a tunnel operator, can read the text sent; signing in alone does not encrypt the traffic
between the app and the model so that this intermediary is bypassed.

Choosing a speech recognition server means that recordings are sent: directly or, away from home, through
the gateway (`/whisper`). With recognition on this computer the recording does not leave the computer.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Sign-in required / sign-in expired | sign in again |
| No sign-in server | the address and the `/.well-known/openid-configuration` metadata |
| `invalid_client` / `unauthorized_client` | the client ID, the redirect URI, PKCE and the user's access |
| HTTP 401 despite signing in | token verification in the proxy and matching origins |
| No return from the browser | the `gramadrama://` registration by the installer, or the manual return field |
| HTTP 403 from an intermediary | access rules or bot protection for the API |
