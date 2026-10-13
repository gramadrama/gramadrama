# Your own services for GramaDrama

On both systems all services are in the **GramaDrama → Services** (*Usługi*) tab: the language model, LanguageTool
and speech recognition run on this computer (**This computer**, *Ten komputer*; on macOS **This Mac**, *Ten Mac*)
or on a server at the address you enter (**Server**, *Serwer*), and remote access is optional. A service on this
computer has a default address, which can be changed to another address of this computer: Ollama
`http://127.0.0.1:11434`, LanguageTool on Linux `http://127.0.0.1:8010`, and on macOS, with the field empty,
the first of the ports 8010 and 8081 that answers.
The language model is enough for the first correction and for everyday use; LanguageTool and dictation are optional
(what changes without LanguageTool: [services in the README](../README.md#services-prompts-and-privacy)).
There is no default project server. The commands below start the services on the same computer;
for another computer, use its address and set up network access.

## Model through Ollama

Install Ollama following its project's instructions and download the chosen model with
`ollama pull MODEL_NAME`, replacing the name with a model that suits the computer's resources and the language you need.
Start the Ollama service; for an installation without a service you can use `ollama serve`.

In GramaDrama set:

- address: `http://127.0.0.1:11434`;
- protocol: **Ollama**;
- model: the exact name from `ollama list` or from the list of models in the app.

You can check the local API with `curl http://127.0.0.1:11434/api/tags`.
Do not add `/v1` to the address with the native Ollama protocol. Addresses and the API are described in the
[Ollama documentation](https://docs.ollama.com/api/introduction).

On macOS, for Ollama on the same computer, choose **Services → Language model → This Mac**
(*Usługi → Model językowy → Ten Mac*). The app connects to `http://127.0.0.1:11434`, shows whether Ollama is installed
and running, with **Get Ollama…** (*Pobierz Ollamę…*) and **Start** (*Uruchom*), and pulls the model entered in the
**Pull a model** (*Pobierz model*) row with a progress bar. Any model from the Ollama library works; the recommended
one is `gemma4:e4b` (about 6.6 GB), and a new install starts with its name in the settings.
Ollama from Homebrew starts as a service, not with the button in the app. On this Mac the model stays in memory
for 30 min after its last use and is not loaded when the app starts; on a server, for 24 hours.
Set up Ollama on another computer in **Server** as above.

## Model through llama.cpp

After installing `llama-server` and downloading a compatible GGUF model, run:

```bash
llama-server -m /path/to/model.gguf -c 8192 --alias corrector --host 127.0.0.1 --port 8080
```

Replace the path with the downloaded file. In GramaDrama choose **OpenAI compatible** (*Zgodny z OpenAI*),
the address `http://127.0.0.1:8080/v1` and the model `corrector`. Check the list of models with:

```bash
curl http://127.0.0.1:8080/v1/models
```

Building the server and its options are described in the [llama.cpp documentation](https://github.com/ggml-org/llama.cpp/tree/master/tools/server).
A context of 8192 is a starting point for the default prompts. Longer text or your own prompts may
need a larger context and more memory. A message about a context that is too small means the server
configuration has to change, not the address in GramaDrama.

## Other OpenAI-compatible APIs

The client uses the model list and `/chat/completions`, including streamed responses.
Enter the API's base address, e.g. `https://llm.example.com/v1`, the model name and the API key if one is required.
An address with its own path should contain the provider's full base path.
On macOS the key is stored in the keychain. On both platforms, after a 400 error, the app replaces
`max_tokens` with `max_completion_tokens` or leaves out `temperature` if the server says so; macOS remembers
this change for the server and model until the app is restarted. On both platforms **Check** (*Sprawdź*)
suggests the right protocol when the other kind of server answers at the address (through the OpenAI API
Ollama loads the model again), and a server without a model list needs the model name typed in.

The documented trials cover Ollama and llama.cpp. The macOS client was checked against mock servers and against the
OpenAI-compatible API provided by Ollama (`/v1`). Tests of further backends are postponed in
[G3 of the release plan](open-source-plan.md); the description “OpenAI compatible” alone does not guarantee
compatibility of all parameters, errors and streaming features.

## LanguageTool

Download and unpack the LanguageTool server distribution, provide the Java version it requires,
and prepare an empty `server.properties` file in a new configuration directory.
From the directory of the unpacked distribution run:

```bash
java -cp languagetool-server.jar org.languagetool.server.HTTPServer --config server.properties --port 8010
```

On Linux enter `http://127.0.0.1:8010` in the app. On macOS choose **Services → LanguageTool → This Mac**
(*Usługi → LanguageTool → Ten Mac*): the app finds the server on this Mac on port 8010 or 8081 (LanguageTool's
default port and the Homebrew service), unless you enter another address of this Mac in **LanguageTool address**
(*Adres LanguageTool*). Set up a server on another computer in **Server**. An example check:

```bash
curl --data-urlencode 'language=pl-PL' --data-urlencode 'text=To jest pruba.' http://127.0.0.1:8010/v2/check
```

By default such a server accepts local connections and does not need a port opened to the internet.
Remote access options and additional rules are described in the
[LanguageTool server documentation](https://dev.languagetool.org/http-server).
GramaDrama uses LanguageTool for grammar rules and explanations, not as a condition of every correction.

## Speech recognition

Open **Services → Speech recognition** (*Usługi → Rozpoznawanie mowy*) and choose:

- **This computer (Linux):** local Whisper. The `linux/install_engine.py tools` command downloads a separate
  environment, the CUDA libraries and the Whisper large-v3-turbo model. It needs `uv`, a compatible NVIDIA card,
  its driver and space for several GB of data.
- **This Mac (macOS):** Whisper large-v3-turbo through WhisperKit on the Neural Engine. The model (about 1.6 GB)
  is downloaded and loaded in the background as soon as dictation is on (it is by default), so the first dictation
  does not wait; the first load prepares it for the Neural Engine and takes a few minutes. With a speech server it is
  neither downloaded nor loaded.
- **Server:** the server's rows appear below it: enter the base address of the service, choose the **Protocol**
  (*Protokół*) and enter the model for the OpenAI API and an optional key. The recording is sent
  to this service; the local Whisper model is not needed. Whisper large-v3-turbo is the recommended version, the one
  used on this computer, but the server can run another Whisper version or speech model, as you prefer.

The protocol must match the API of the running server:

| Protocol | Example base address | Requests |
|---|---|---|
| **OpenAI compatible** | `https://speech.example.com/v1` | transcription: `POST /audio/transcriptions` with a WAV file in multipart |
| **whisper.cpp** | `https://speech.example.com` | transcription: `POST /inference`; availability check: `GET /health` |

Replace the example address with the address of your own service. With whisper.cpp do not add `/v1` unless
it is part of a path set in your own reverse proxy. The whisper.cpp model is chosen by the server's
configuration. The optional key is sent as a Bearer token, also when the proxy provides the authentication.
Choosing “Server” connects to a service that is already set up; installing and configuring it, on this computer
or another machine, is a separate step.

Switching to **This computer** keeps the saved address, model, key and protocol of the server,
so you can return to it later without configuring it again. **Server** without an address reports
an error; it does not start the local Whisper automatically. On Linux, after an update, the previous choice
follows the address: a saved address means a server, an empty one local recognition,
until you choose an option explicitly.

On macOS the API key is stored in the keychain, not in the app's settings. On both platforms
the **Check** button suggests changing the protocol when the other kind of server answers at the address, and warns
about a connection without encryption outside the local network. Redirects are not followed: a server that
redirects (e.g. from http to https) needs its target address entered. A whisper.cpp response made up
of several segments goes into the field as a single line. A chosen server that does not answer ends the dictation
with a message — the app does not switch to the local Whisper by itself.
The recording goes to the server in a single request (up to 3 min, about 32 KB per second), so a reverse proxy
must accept such sizes (in nginx `client_max_body_size`). The text preview in the pill works
only with recognition on the Mac.

Optional transcript clean-up sends the transcript's text to the language model server.
Details of the behaviour: [dictation](../linux/docs/dictation.md).

## Remote access and diagnostics

A server reachable over the internet should have HTTPS and authentication. GramaDrama supports an API
key and [remote access with OIDC sign-in](../linux/docs/remote-access.md).
On both systems, after you sign in to remote access (Pocket ID), a server from the settings that does not answer
in the current network (e.g. away from home) is replaced by the gateway: the model in the Ollama protocol by
`/ollama`, LanguageTool by `/languagetool`, the speech server by `/whisper` in the same protocol, with the sign-in
token instead of the API key. Services on this Mac and an OpenAI-compatible model server always connect directly,
and a model server or LanguageTool without an address means the gateway alone. Each service checks its route
separately; the **Connection** (*Połączenie*) row shows which one goes through the gateway.
Linux no longer stores separate fallback addresses (`llm_remote_url`, `languagetool_remote_url`):
the gateway is the origin of the sign-in address. For speech the gateway must expose only recognition and status
(`/whisper/inference`, `/whisper/health`) and accept recordings of up to about 6 MB. For the model it needs
`/ollama/api/chat`, `/ollama/api/generate`, `/ollama/api/tags`, `/ollama/api/version` and `/ollama/api/ps`,
for LanguageTool `/languagetool/v2/check` and `/languagetool/v2/languages`. It checks the sign-in token before
passing a request on and does not expose model management (pulling, deleting or loading models); see
[remote access](../linux/docs/remote-access.md). `GRAMADRAMA_FORCE_REMOTE=1` forces the route through the gateway,
for a test from home.
A variant without TLS is suitable only for a properly protected local network.
The operator of the service or of a reverse proxy that terminates TLS has access to the text.

First check the API response, then the service row in the Services tab and a correction attempt.
For speech recognition use **Check** at the speech server and a short recording.
A 401/403 error concerns permissions; a missing model list may mean a wrong API path; a cut-off response
may come from the generation limit or the model's thinking mode.

Model weights are not part of the repository. Record the name, version, quantisation and licence of the variant
you download. Compare quality results on your own server following the [benchmark instructions](../linux/docs/models.md).
