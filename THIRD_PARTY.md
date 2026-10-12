# Third-party components

GramaDrama is licensed under GPL-3.0-or-later ([LICENSE](LICENSE)). The repository contains no
third-party libraries, code or model weights, except the Unicode data tables described below.
The user installs the other components: through the system package manager, through the GramaDrama
installer on an explicit command, or through Swift Package Manager when building. Each component has
its own licence; the text supplied with it is binding.

This list was compiled on 2026-10-06 for the versions specified in the repository, from package metadata,
Ubuntu `copyright` files and model cards. A change of dependencies requires an update of this file.

## Included in the repository

| Component | Location | Licence |
|---|---|---|
| Emoji properties from the Unicode Character Database 15.0 and 16.0 | `linux/gramadrama/emoji.py` | Unicode License V3, [notice](#unicode-licence-notice) |

## Linux: system libraries

The distribution's package manager installs them as described in the [Linux guide](linux/README.md#installation).
GramaDrama uses them at run time and does not distribute them. Licence details are
in the `/usr/share/doc/<package>/copyright` files.

| Component | Licence |
|---|---|
| GTK 4, libadwaita, PyGObject, IBus, AT-SPI, Enchant 2 | mostly LGPL-2.1-or-later |
| PipeWire (`pw-record`) | MIT, partly LGPL-2.1-or-later |
| Hunspell dictionaries | depends on the language and the package |

## Linux: local speech recognition (optional)

The command `/usr/bin/python3 linux/install_engine.py tools` creates a separate Python environment with
[uv](https://docs.astral.sh/uv/) and downloads from PyPI and Hugging Face:

| Component | Version | Licence |
|---|---|---|
| faster-whisper | 1.2.1 | MIT |
| CTranslate2 | 4.8.2 | MIT |
| NVIDIA cuBLAS (`nvidia-cublas-cu12`) | 12.9.2.10 | **proprietary NVIDIA licence** |
| NVIDIA cuDNN (`nvidia-cudnn-cu12`) | 9.26.0.51 | **proprietary NVIDIA licence** |
| NVIDIA NVRTC (`nvidia-cuda-nvrtc-cu12`), indirect dependency | chosen by uv | **proprietary NVIDIA licence** |
| huggingface_hub, indirect dependency | chosen by uv | Apache-2.0 |
| Whisper large-v3-turbo in CTranslate2 format (`mobiuslabsgmbh/faster-whisper-large-v3-turbo`) | — | MIT |

The other indirect dependencies, including NumPy, ONNX Runtime, PyAV and tokenizers, have open licences
(BSD, MIT, Apache-2.0, MPL-2.0); uv chooses their versions during installation.

**The NVIDIA libraries are not open-source software.** By running this command, the user downloads them
from PyPI and accepts NVIDIA's terms. Without this step, dictation can use a transcription server.

## macOS: developer build

| Component | Version | Licence |
|---|---|---|
| WhisperKit (`argmax-oss-swift`) | 1.1.0 | MIT |
| swift-argument-parser | 1.8.2 | Apache-2.0 with Runtime Library Exception |
| Whisper large-v3-turbo for Core ML (`argmaxinc/whisperkit-coreml`), downloaded by the app | — | MIT |

## Models and services chosen by the user

The model server (e.g. Ollama, llama.cpp or a service compatible with the OpenAI API), LanguageTool, the
transcription server and the weights of language models are not part of GramaDrama. The user installs them
and checks their licences and terms of use, and for external services also their data processing rules.

On both systems the app offers to download the `gemma4:e4b` model (Gemma 4) through Ollama, and on macOS
the default model name in the settings is `hf.co/google/gemma-4-12B-it-qat-q4_0-gguf`. The cards of these
models state the Apache-2.0 licence. The download happens only when the user asks for it.

## Developer tools

Tests and benchmarks are not part of the installed app. The test page
`browser/test/prosemirror.html` loads ProseMirror from a CDN during the test.
The benchmark dependencies are listed in their directories.

## Unicode licence notice

Applies to the tables in `linux/gramadrama/emoji.py`.

```text
UNICODE LICENSE V3

COPYRIGHT AND PERMISSION NOTICE

Copyright © 1991-2024 Unicode, Inc.

NOTICE TO USER: Carefully read the following legal agreement. BY
DOWNLOADING, INSTALLING, COPYING OR OTHERWISE USING DATA FILES, AND/OR
SOFTWARE, YOU UNEQUIVOCALLY ACCEPT, AND AGREE TO BE BOUND BY, ALL OF THE
TERMS AND CONDITIONS OF THIS AGREEMENT. IF YOU DO NOT AGREE, DO NOT
DOWNLOAD, INSTALL, COPY, DISTRIBUTE OR USE THE DATA FILES OR SOFTWARE.

Permission is hereby granted, free of charge, to any person obtaining a
copy of data files and any associated documentation (the "Data Files") or
software and any associated documentation (the "Software") to deal in the
Data Files or Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, and/or sell
copies of the Data Files or Software, and to permit persons to whom the
Data Files or Software are furnished to do so, provided that either (a)
this copyright and permission notice appear with all copies of the Data
Files or Software, or (b) this copyright and permission notice appear in
associated Documentation.

THE DATA FILES AND SOFTWARE ARE PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF
THIRD PARTY RIGHTS.

IN NO EVENT SHALL THE COPYRIGHT HOLDER OR HOLDERS INCLUDED IN THIS NOTICE
BE LIABLE FOR ANY CLAIM, OR ANY SPECIAL INDIRECT OR CONSEQUENTIAL DAMAGES,
OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,
ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THE DATA
FILES OR SOFTWARE.

Except as contained in this notice, the name of a copyright holder shall
not be used in advertising or otherwise to promote the sale, use or other
dealings in these Data Files or Software without prior written
authorization of the copyright holder.
```
