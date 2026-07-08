"""Processing services — the "vs KSSL" compute.

Mirrors the service catalog in ``docs/02_DATA_ENGINEERING.md``. The core signal and tender
pipelines are implemented; LLM-dependent steps go through the :mod:`llm` provider interface so
the whole system runs offline with a deterministic stub and switches to real models via config.
"""
