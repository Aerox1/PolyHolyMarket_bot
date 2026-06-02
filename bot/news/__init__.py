"""News pipeline: crawl → translate/summarize → render → (publish).

Ported from NabzarSocial onto PHM's PTB JobQueue + budget-gated core.gemini.
No process here ever holds wallet/key material.
"""
