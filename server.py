#!/usr/bin/env python3
"""
Portfolio Tracker - local proxy server

Serves index.html and exposes two JSON endpoints that yfinance/Yahoo Finance
power under the hood:

  GET /api/resolve?q=<isin-or-text>
      -> candidate tickers matching an ISIN or free-text search

  GET /api/history?ticker=<symbol>&start=YYYY-MM-DD&end=YYYY-MM-DD
      -> daily close price series for one ticker

Run:
    pip install yfinance requests --break-system-packages   (if needed)
    python3 server.py
    open http://localhost:8765
"""

import json
import os
import sys
import sqlite3
import threading
import traceback
from datetime import datetime, date, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    import yfinance as yf
except ImportError:
    print("Manca la libreria 'yfinance'. Installala con:\n"
          "    pip install yfinance requests --break-system-packages")
    sys.exit(1)

try:
    import requests
except ImportError:
    requests = None  # /api/resolve will degrade gracefully

PORT = 8765
WEB_ROOT = os.path.dirname(os.path.abspath(__file__))

YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def resolve_symbol(query: str):
    """Ask Yahoo's search endpoint for tickers matching an ISIN or free text."""
    if requests is None:
        return {"error": "La libreria 'requests' non e' installata."}

    resp = requests.get(
        YAHOO_SEARCH_URL,
        params={"q": query, "quotesCount": 10, "newsCount": 0},
        headers={"User-Agent": BROWSER_UA},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    candidates = []
    for q in data.get("quotes", []):
        symbol = q.get("symbol")
        if not symbol:
            continue
        candidates.append({
            "symbol": symbol,
            "name": q.get("shortname") or q.get("longname") or symbol,
            "exchange": q.get("exchange", ""),
            "type": q.get("quoteType", ""),
        })
    return {"candidates": candidates}


CACHE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_cache.sqlite3")
_cache_lock = threading.Lock()


def _get_cache_conn():
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS prices (
        ticker TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL,
        PRIMARY KEY (ticker, date)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS meta (
        ticker TEXT PRIMARY KEY, currency TEXT, last_fetched TEXT
    )""")
    return conn


def _date_add(date_str: str, days: int) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _fetch_from_yahoo(ticker: str, start: str, end: str):
    """Raw Yahoo fetch (no cache). Returns (points, currency) or raises."""
    t = yf.Ticker(ticker)
    hist = t.history(start=start, end=end, auto_adjust=True)
    points = []
    if hist is not None and not hist.empty:
        series = hist["Close"].dropna()
        points = [
            {"date": idx.strftime("%Y-%m-%d"), "close": round(float(val), 6)}
            for idx, val in series.items()
        ]
    currency = None
    try:
        currency = t.fast_info.get("currency")
    except Exception:
        pass
    return points, currency


def fetch_history(ticker: str, start: str, end: str):
    """Daily close prices for a ticker, served from a local SQLite cache and
    topped up incrementally from Yahoo — only the date ranges actually missing
    from the cache are downloaded, so repeated 'Calcola' clicks on the same
    (or an overlapping) period are fast and don't re-hit Yahoo every time.
    """
    today = date.today().strftime("%Y-%m-%d")
    end_capped = min(end, today)

    with _cache_lock:
        conn = _get_cache_conn()
        try:
            row = conn.execute(
                "SELECT MIN(date), MAX(date) FROM prices WHERE ticker=?", (ticker,)
            ).fetchone()
            cached_min, cached_max = row if row else (None, None)

            currency = None
            meta_row = conn.execute("SELECT currency FROM meta WHERE ticker=?", (ticker,)).fetchone()
            if meta_row:
                currency = meta_row[0]

            def store(points):
                if points:
                    conn.executemany(
                        "INSERT OR REPLACE INTO prices (ticker, date, close) VALUES (?,?,?)",
                        [(ticker, p["date"], p["close"]) for p in points],
                    )

            def overlap_ratio(points, use_latest):
                """new/old price ratio on dates present both in the cache and in a fresh download."""
                if not points:
                    return None
                dates = [p["date"] for p in points]
                cached = dict(conn.execute(
                    "SELECT date, close FROM prices WHERE ticker=? AND date>=? AND date<=?",
                    (ticker, min(dates), max(dates)),
                ).fetchall())
                common = [p for p in points if p["date"] in cached and cached[p["date"]] > 0]
                if not common:
                    return None
                ref = common[-1] if use_latest else common[0]
                return ref["close"] / cached[ref["date"]]

            if cached_min is None:
                if start <= end_capped:
                    try:
                        points, fetched_currency = _fetch_from_yahoo(ticker, start, end_capped)
                        store(points)
                        currency = currency or fetched_currency
                    except Exception:
                        traceback.print_exc()
            else:
                # Forward gap. Yahoo prices are dividend-adjusted: every new dividend rescales the
                # whole history. Re-download an overlap window and rescale the cached history so the
                # series stays continuous and total return keeps including dividends.
                if end_capped > cached_max:
                    try:
                        points, fetched_currency = _fetch_from_yahoo(ticker, _date_add(cached_max, -30), end_capped)
                        ratio = overlap_ratio(points, use_latest=True)
                        if ratio and abs(ratio - 1) > 1e-7:
                            conn.execute("UPDATE prices SET close = close * ? WHERE ticker=?", (ratio, ticker))
                        store(points)
                        currency = currency or fetched_currency
                    except Exception:
                        traceback.print_exc()
                # Backward gap: scale the older download onto the (now current) cache.
                if start < cached_min:
                    try:
                        points, fetched_currency = _fetch_from_yahoo(ticker, start, _date_add(cached_min, 30))
                        ratio = overlap_ratio(points, use_latest=False)
                        older = [p for p in points if p["date"] < cached_min]
                        if ratio and ratio > 0:
                            older = [{"date": p["date"], "close": p["close"] / ratio} for p in older]
                        store(older)
                        currency = currency or fetched_currency
                    except Exception:
                        traceback.print_exc()

            conn.execute(
                "INSERT INTO meta (ticker, currency, last_fetched) VALUES (?,?,?) "
                "ON CONFLICT(ticker) DO UPDATE SET currency=COALESCE(excluded.currency, meta.currency), last_fetched=excluded.last_fetched",
                (ticker, currency, datetime.now().isoformat()),
            )
            conn.commit()

            rows = conn.execute(
                "SELECT date, close FROM prices WHERE ticker=? AND date>=? AND date<=? ORDER BY date",
                (ticker, start, end_capped),
            ).fetchall()
        finally:
            conn.close()

    if not rows:
        return {"error": f"Nessun dato trovato per '{ticker}' nel periodo indicato."}

    points = [{"date": d, "close": c} for d, c in rows]
    return {"ticker": ticker, "currency": currency, "points": points}


# Best-effort keyword mapping from Yahoo/Morningstar category + fund/instrument
# name to a broad asset class. Not perfect (Yahoo doesn't expose a clean
# top-level asset class for most ETFs, especially UCITS ones) but good enough
# to pre-fill the field for the user to correct if needed. Order matters:
# more specific buckets are checked before the broad "Azionario" catch-all.
_CLASSIFY_RULES = [
    ("Oro", ["gold", "precious metal", "silver", "platinum", "palladium"]),
    ("Immobiliare", ["real estate", "reit", "property", "real asset"]),
    ("Materie Prime", ["commodit", "broad basket", "natural resources", "energy fund"]),
    ("Liquidita", ["money market", "ultrashort", "cash management", "treasury bill"]),
    ("Alternativi", ["preferred", "convertible", "multialternative", "market neutral",
                       "long/short", "hedge fund"]),
    ("Obbligazionario", ["bond", "fixed income", "duration", "credit", "treasury",
                           "gilt", "muni", "debt fund"]),
    ("Azionario", ["equity", "stock", "blend", "growth", "value", "large cap", "mid cap",
                    "small cap", "index", "dividend", "msci", "ftse", "s&p", "russell",
                    "nasdaq", "dow jones", "stoxx", "world", "emerging market", "shares",
                    "aristocrat"]),
]

_classify_cache = {}


def classify_ticker(ticker: str):
    """Best-effort asset-class lookup. Reuses the search endpoint (resolve_symbol),
    which is known to work reliably without authentication, instead of Yahoo's
    quoteSummary endpoint — which now requires a "crumb"/session token that a
    plain requests call cannot obtain, and was silently failing every time.
    """
    if ticker in _classify_cache:
        return _classify_cache[ticker]

    quote_type, long_name = "", ""
    try:
        search_result = resolve_symbol(ticker)
        candidates = search_result.get("candidates") or []
        match = next((c for c in candidates if c["symbol"].upper() == ticker.upper()), None)
        if not match and candidates:
            match = candidates[0]
        if match:
            quote_type = (match.get("type") or "").upper()
            long_name = match.get("name") or ""
    except Exception:
        pass

    haystack = long_name.lower()

    asset_class = ""
    if quote_type == "CRYPTOCURRENCY":
        asset_class = "Cripto"
    else:
        for label, keywords in _CLASSIFY_RULES:
            if any(kw in haystack for kw in keywords):
                asset_class = label
                break
        if not asset_class and quote_type == "EQUITY":
            asset_class = "Azionario"

    result = {"assetClass": asset_class, "category": "", "quoteType": quote_type, "longName": long_name}
    _classify_cache[ticker] = result
    return result


_fx_cache = {}


def fetch_fx_rates(base: str, symbols, start: str, end: str):
    """Historical daily FX rates (ECB reference rates) via the free Frankfurter API.
    `symbols` is a list of foreign-currency codes; `base` is the portfolio's
    reference currency. Returns {"rates": {"YYYY-MM-DD": {"GBP": 0.87, ...}}}.
    """
    if requests is None:
        return {"error": "La libreria 'requests' non e' installata."}

    symbols = sorted(set(s.upper() for s in symbols if s and s.upper() != base.upper()))
    if not symbols:
        return {"rates": {}}

    cache_key = (base.upper(), tuple(symbols), start, end)
    if cache_key in _fx_cache:
        return _fx_cache[cache_key]

    url = f"https://api.frankfurter.dev/v1/{start}..{end}"
    resp = requests.get(
        url,
        params={"base": base.upper(), "symbols": ",".join(symbols)},
        headers={"User-Agent": BROWSER_UA},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    result = {"rates": data.get("rates", {})}
    _fx_cache[cache_key] = result
    return result


_ecb_rate_cache = {"rate": None, "asOf": None, "fetched_at": None}
_ECB_RATE_TTL_SECONDS = 6 * 3600  # refresh at most every 6 hours


def fetch_ecb_deposit_rate():
    """Latest ECB deposit facility rate (the euro risk-free proxy), from the
    ECB's official SDMX API. Cached in-process for a few hours since it only
    changes on Governing Council decisions, not every request.
    """
    now = datetime.now()
    if (_ecb_rate_cache["rate"] is not None and _ecb_rate_cache["fetched_at"]
            and (now - _ecb_rate_cache["fetched_at"]).total_seconds() < _ECB_RATE_TTL_SECONDS):
        return {"rate": _ecb_rate_cache["rate"], "asOf": _ecb_rate_cache["asOf"]}

    if requests is None:
        return {"error": "La libreria 'requests' non e' installata."}

    import csv
    import io

    resp = requests.get(
        "https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV",
        params={"format": "csvdata", "lastNObservations": 1},
        headers={"User-Agent": BROWSER_UA},
        timeout=10,
    )
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)
    if not rows:
        raise ValueError("Nessun dato ricevuto dalla BCE.")

    last = rows[-1]
    # Column names are stable ECB SDMX-CSV conventions, but match case-insensitively to be safe.
    lower = {k.lower(): v for k, v in last.items()}
    rate = float(lower.get("obs_value"))
    as_of = lower.get("time_period")

    _ecb_rate_cache["rate"] = rate
    _ecb_rate_cache["asOf"] = as_of
    _ecb_rate_cache["fetched_at"] = now
    return {"rate": rate, "asOf": as_of}


_hicp_cache = {"points": None, "source": None, "fetched_at": None}
_HICP_TTL_SECONDS = 12 * 3600
_HICP_SERIES = [
    ("HICP", "M.U2.N.000000.4D0.INX"),   # new ECB HICP dataset (2025 = 100), from Feb 2026
    ("ICP", "M.U2.N.000000.4.INX"),      # legacy dataset (2015 = 100)
]


def _fetch_ecb_series(flow, key):
    import csv
    import io
    resp = requests.get(
        f"https://data-api.ecb.europa.eu/service/data/{flow}/{key}",
        params={"format": "csvdata", "startPeriod": "1996-01"},
        headers={"User-Agent": BROWSER_UA},
        timeout=15,
    )
    resp.raise_for_status()
    out = {}
    for row in csv.DictReader(io.StringIO(resp.text)):
        low = {k.lower(): v for k, v in row.items()}
        try:
            out[low["time_period"][:7]] = float(low["obs_value"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_hicp():
    """Euro-area HICP monthly index. Uses the new ECB HICP series and chain-links the legacy
    ICP series for months the new one does not cover (index ratios do not depend on the base year)."""
    now = datetime.now()
    if (_hicp_cache["points"] and _hicp_cache["fetched_at"]
            and (now - _hicp_cache["fetched_at"]).total_seconds() < _HICP_TTL_SECONDS):
        return {"points": _hicp_cache["points"], "source": _hicp_cache["source"]}
    if requests is None:
        return {"error": "La libreria 'requests' non e' installata."}

    series, sources = [], []
    for flow, key in _HICP_SERIES:
        try:
            s = _fetch_ecb_series(flow, key)
            if s:
                series.append(s)
                sources.append(f"{flow}.{key}")
        except Exception:
            traceback.print_exc()
    if not series:
        raise ValueError("Serie HICP non disponibile dalla BCE.")

    merged = dict(series[0])
    for other in series[1:]:
        common = sorted(set(merged) & set(other))
        if not common:
            continue
        ratio = merged[common[0]] / other[common[0]]           # link at the earliest common month
        for m, v in other.items():
            if m < common[0] and m not in merged:
                merged[m] = v * ratio
        latest = max(merged)
        if max(other) > latest:                                   # legacy more recent than new: extend
            r2 = merged[latest] / other[latest] if latest in other else ratio
            for m, v in other.items():
                if m > latest:
                    merged[m] = v * r2

    points = [{"month": m, "index": merged[m]} for m in sorted(merged)]
    _hicp_cache.update({"points": points, "source": " + ".join(sources), "fetched_at": now})
    return {"points": points, "source": _hicp_cache["source"]}


def _get_state_conn():
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS app_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        data TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    return conn


def load_app_state():
    with _cache_lock:
        conn = _get_state_conn()
        try:
            row = conn.execute("SELECT data FROM app_state WHERE id=1").fetchone()
        finally:
            conn.close()
    if not row:
        return {"exists": False, "state": None}
    try:
        return {"exists": True, "state": json.loads(row[0])}
    except Exception:
        return {"exists": False, "state": None}


def save_app_state(state_obj):
    data = json.dumps(state_obj)
    with _cache_lock:
        conn = _get_state_conn()
        try:
            conn.execute(
                "INSERT INTO app_state (id, data, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (data, datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_ROOT, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/resolve":
            qs = parse_qs(parsed.query)
            query = (qs.get("q") or [""])[0].strip()
            if not query:
                self._send_json({"error": "Parametro 'q' mancante."}, 400)
                return
            try:
                self._send_json(resolve_symbol(query))
            except Exception as exc:
                traceback.print_exc()
                self._send_json({"error": str(exc)}, 500)
            return

        if parsed.path == "/api/history":
            qs = parse_qs(parsed.query)
            ticker = (qs.get("ticker") or [""])[0].strip()
            start = (qs.get("start") or [""])[0].strip()
            end = (qs.get("end") or [""])[0].strip()
            if not ticker or not start or not end:
                self._send_json({"error": "Parametri 'ticker', 'start' e 'end' richiesti."}, 400)
                return
            try:
                self._send_json(fetch_history(ticker, start, end))
            except Exception as exc:
                traceback.print_exc()
                self._send_json({"error": str(exc)}, 500)
            return

        if parsed.path == "/api/classify":
            qs = parse_qs(parsed.query)
            ticker = (qs.get("ticker") or [""])[0].strip()
            if not ticker:
                self._send_json({"error": "Parametro 'ticker' mancante."}, 400)
                return
            try:
                self._send_json(classify_ticker(ticker))
            except Exception as exc:
                traceback.print_exc()
                self._send_json({"error": str(exc)}, 500)
            return

        if parsed.path == "/api/fx":
            qs = parse_qs(parsed.query)
            base = (qs.get("base") or ["EUR"])[0].strip()
            symbols = (qs.get("symbols") or [""])[0].split(",")
            start = (qs.get("start") or [""])[0].strip()
            end = (qs.get("end") or [""])[0].strip()
            if not start or not end:
                self._send_json({"error": "Parametri 'start' e 'end' richiesti."}, 400)
                return
            try:
                self._send_json(fetch_fx_rates(base, symbols, start, end))
            except Exception as exc:
                traceback.print_exc()
                self._send_json({"error": str(exc)}, 500)
            return

        if parsed.path == "/api/state":
            try:
                self._send_json(load_app_state())
            except Exception as exc:
                traceback.print_exc()
                self._send_json({"error": str(exc)}, 500)
            return

        if parsed.path == "/api/inflation":
            try:
                self._send_json(fetch_hicp())
            except Exception as exc:
                traceback.print_exc()
                self._send_json({"error": str(exc)}, 500)
            return

        if parsed.path == "/api/risk-free":
            qs = parse_qs(parsed.query)
            currency = (qs.get("currency") or ["EUR"])[0].strip().upper()
            if currency != "EUR":
                self._send_json({"error": "Aggiornamento automatico disponibile solo per EUR."}, 400)
                return
            try:
                self._send_json(fetch_ecb_deposit_rate())
            except Exception as exc:
                traceback.print_exc()
                self._send_json({"error": str(exc)}, 500)
            return

        # Everything else: serve static files (index.html, etc.) from WEB_ROOT
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/state":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                state_obj = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                self._send_json({"error": f"JSON non valido: {exc}"}, 400)
                return
            try:
                save_app_state(state_obj)
                self._send_json({"ok": True})
            except Exception as exc:
                traceback.print_exc()
                self._send_json({"error": str(exc)}, 500)
            return

        self._send_json({"error": "Not found"}, 404)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Portfolio Tracker in ascolto su http://localhost:{PORT}")
    print("Premi Ctrl+C per fermare il server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nChiusura del server...")
        server.shutdown()


if __name__ == "__main__":
    main()
