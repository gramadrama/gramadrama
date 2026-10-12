# How to compare models

GramaDrama does not need a particular model. The choice depends on the language, the quality of the answers,
the available memory, the context limit and an acceptable correction time.
There is no single portable measure of speed: the result depends on the server, the hardware, the quantisation,
the length of the text, the size of the prompt and whether the model was already loaded.

## Repeatable measurement

From the `linux/` directory, after setting up the server:

```bash
python3 tests/bench.py ../bench/cases.json --prompts default
python3 tests/bench.py ../bench/translate.json --mode=translate --prompts default
```

The benchmark sends the sentences from the file to the server set in the app. It reports exact matches,
false alarms, response time and generation speed. Cases with correct text are needed:
a model that rewrites every sentence can look good when only the fixed errors are counted.

When comparing, record:

- the name and version of the server and the full name of the model;
- the version of the weights, the quantisation, the context and generation limits;
- the GramaDrama version, the prompts directory and the cases file;
- the model's reasoning setting and how it was warmed up before the measurement;
- the result of every case, not only the average.

Reports meant for publication should not contain addresses of private services, keys or
texts from real correspondence. Do not publish a ranking based on data that is not shared.

## Reasoning and context

For Ollama, GramaDrama sends `think=false` by default. The **Model reasoning** (*Myślenie modelu*) setting in
**Services → Language model → Advanced** (*Usługi → Model językowy → Zaawansowane*) lets you keep the model's own behaviour.
If the server rejects this parameter, the client repeats the request without it. The effect of this option on quality
and latency has to be measured for the particular model.

The answer limit can be used up by internal reasoning instead of the text for the user.
A truncated-answer error, an unexpected language or a change of meaning are separate test results, not correct matches.

The default instructions and examples take up part of the context. A configuration of 8192 tokens is a starting point
for the trials described in the [service setup](../../docs/self-hosting.md), not a guarantee that
any text or your own prompt will fit.

## Licences

The licence of the GramaDrama code does not replace the licence of the model weights, the server or the libraries.
The repository contains no weights. Before downloading, check the model card of the exact variant and its full licence,
including the terms of the base model, of distributing derivatives and any usage policy.
Quantisation or a change of format does not automatically change these terms.

Compare a prompt change separately from a model change: [editable prompts](prompts.md).
Extended validation of the backends is postponed in the [release plan](../../docs/open-source-plan.md).
