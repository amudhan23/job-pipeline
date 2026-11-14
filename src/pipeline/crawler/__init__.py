"""Real-time crawler: watches every known job board and records postings
within minutes of them going live.

    frontier.py   which board to read next, and when (adaptive revisit)
    fetcher.py    polite HTTP: per-host limits, backoff, conditional GETs
    sources.py    light listing readers for Greenhouse / Lever / Ashby
    worker.py     the loop: fetch due boards, detect new / closed postings,
                  score them, poll feeds, rediscover boards, report status
"""
