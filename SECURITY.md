# Security

These skills run **local** Python on the machine that installed them.

- Review `skills/*/scripts/*.py` before you install.
- They do not send your code to a network API.
- The one optional network call is `vf.py imports --online` (PyPI / npm existence check).
- `VF scan` is a heuristic. A clean receipt is "no known red flags", not an audit.
- Report a real vulnerability as a private GitHub security advisory, or an issue without a proof-of-concept against a third party.
