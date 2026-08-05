"""Lightweight peak-RSS checkpoint logging.

Added after a real Render cron job OOM'd (killed at >2GiB) ingesting a
252-page, 42MB "compiled" agenda packet — with zero visibility into which
pipeline stage actually caused it, since the crash left no log line between
"Ingesting ..." and the kill. Every code path this file's functions could
plausibly implicate (PyMuPDF text extraction, the OCR/pixmap fallback, and
the full Gemini extraction pipeline including the 20-chunk POLICY_ORDINANCE
branch) was reproduced locally against the exact same file and stayed under
150MB, so the spike couldn't be localized from local testing alone — these
checkpoints exist to pinpoint it directly from the next real Render run's
own logs instead.

Deliberately stdlib-only (resource.getrusage) — a single syscall, safe to
leave logging on every run permanently rather than gating behind a debug
flag."""

import resource
import sys


def peak_rss_mb() -> float:
    """Peak resident set size for this process so far, in MB. ru_maxrss is
    KB on Linux (Render's actual deploy target) but bytes on macOS/BSD —
    normalized to MB here so the same log line means the same thing
    regardless of which platform produced it."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / 1024 if sys.platform != "darwin" else raw / (1024 * 1024)
