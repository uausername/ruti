# docs

`CC CLI Routing Executive Summary.pdf` is the proposal this project started from. It is kept for
context, not as guidance — its central recommendation (put a router proxy in front of Claude Code
and fall back to a local model on HTTP 429) turned out to be impossible, for the reasons the main
README explains. The delegation approach shipped here is what survived the research.

Parts of it that did hold up: LM Studio as the local serving layer, quantization sizing for a
6 GB card, and YAML-driven routing policy.
