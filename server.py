#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alex Dashboard - local server + Yahoo Finance proxy (stdlib only).

Run:  python server.py
Then open http://127.0.0.1:8787  (start.bat does this for you)
"""
import http.server
import socketserver
import http.cookiejar
import urllib.request
import urllib.parse
import urllib.error
import json
import time
import threading
import gzip
import io
import os
import sys
import webbrowser

# Windows consoles default to a legacy codepage (cp1252/936); printing anything
# outside it raises UnicodeEncodeError and kills the server on startup.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


def log(*parts):
    msg = " ".join(str(p) for p in parts)
    try:
        print(msg, flush=True)
    except Exception:
        try:
            sys.stdout.buffer.write((msg + "\n").encode("utf-8", "replace"))
            sys.stdout.buffer.flush()
        except Exception:
            pass

# Local default is 127.0.0.1:8787. In the cloud, set PORT / HOST via env
# (HOST=0.0.0.0). Set ALEX_TOKEN="some-secret" to lock it down: the first visit
# must be  https://your-app/?key=<token>  (it drops a cookie and redirects).
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")
TOKEN = os.environ.get("ALEX_TOKEN", "").strip()
ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# ----------------------------------------------------------------------------
# Yahoo credentials (cookie + crumb) manager
# ----------------------------------------------------------------------------
_cred_lock = threading.Lock()
_opener = None
_crumb = None
_crumb_ts = 0


def _build_opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Accept", "*/*"),
                     ("Accept-Language", "en-US,en;q=0.9")]
    return op


def _http_get(opener, url, timeout=12):
    req = urllib.request.Request(url)
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        resp = e  # cookies still processed; body may still be useful
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return getattr(resp, "status", 200) or 200, raw


def refresh_credentials():
    """Seed Yahoo cookies then fetch a crumb. Raises on failure."""
    global _opener, _crumb, _crumb_ts
    op = _build_opener()
    # 1) seed the A1/A3 cookies
    for seed in ("https://fc.yahoo.com/",
                 "https://finance.yahoo.com/quote/AAPL/"):
        try:
            _http_get(op, seed)
        except Exception:
            pass
    # 2) grab a crumb
    last = ""
    for host in ("https://query2.finance.yahoo.com/v1/test/getcrumb",
                 "https://query1.finance.yahoo.com/v1/test/getcrumb"):
        try:
            _, raw = _http_get(op, host)
            crumb = raw.decode("utf-8", "replace").strip()
            last = crumb
            if crumb and "Unauthorized" not in crumb and "<" not in crumb and len(crumb) < 40:
                _opener = op
                _crumb = crumb
                _crumb_ts = time.time()
                print("[cred] crumb ok:", crumb)
                return
        except Exception as e:  # noqa
            last = repr(e)
    raise RuntimeError("could not obtain Yahoo crumb: %s" % last[:120])


def ensure_credentials(force=False):
    with _cred_lock:
        if force or _opener is None or _crumb is None or (time.time() - _crumb_ts) > 1800:
            for attempt in range(4):
                try:
                    refresh_credentials()
                    return
                except Exception as e:
                    print("[cred] attempt %d failed: %s" % (attempt + 1, e))
                    time.sleep(1.5 * (attempt + 1))
            raise RuntimeError("Yahoo credential refresh failed")


def yahoo_get(path_qs, retry=True):
    """GET https://query{1,2}.finance.yahoo.com/<path_qs> with crumb, as JSON."""
    ensure_credentials()
    sep = "&" if "?" in path_qs else "?"
    url = "https://query1.finance.yahoo.com/%s%scrumb=%s" % (
        path_qs, sep, urllib.parse.quote(_crumb, safe=""))
    status, raw = _http_get(_opener, url)
    if status in (401, 403) and retry:
        ensure_credentials(force=True)
        return yahoo_get(path_qs, retry=False)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        if retry:
            ensure_credentials(force=True)
            return yahoo_get(path_qs, retry=False)
        raise


# ----------------------------------------------------------------------------
# tiny TTL cache
# ----------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def cache_get(key, ttl):
    with _cache_lock:
        v = _cache.get(key)
        if v and (time.time() - v[0]) < ttl:
            return v[1]
    return None


def cache_put(key, val):
    with _cache_lock:
        _cache[key] = (time.time(), val)


# ----------------------------------------------------------------------------
# data endpoints
# ----------------------------------------------------------------------------
QUOTE_FIELDS = ",".join([
    "symbol", "shortName", "longName", "regularMarketPrice", "regularMarketChange",
    "regularMarketChangePercent", "regularMarketVolume", "regularMarketDayHigh",
    "regularMarketDayLow", "regularMarketOpen", "regularMarketPreviousClose",
    "averageDailyVolume3Month", "averageDailyVolume10Day", "marketCap",
    "trailingPE", "forwardPE", "priceToBook", "epsTrailingTwelveMonths",
    "epsForward", "dividendYield", "trailingAnnualDividendYield", "dividendRate",
    "bookValue", "sharesOutstanding", "fiftyDayAverage", "twoHundredDayAverage",
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "fiftyTwoWeekChangePercent",
    "averageAnalystRating", "preMarketPrice", "preMarketChangePercent",
    "postMarketPrice", "postMarketChangePercent", "marketState",
])


def fetch_quotes(symbols):
    out = {}
    CHUNK = 50
    chunks = [symbols[i:i + CHUNK] for i in range(0, len(symbols), CHUNK)]
    results = [None] * len(chunks)

    def work(idx, syms):
        qs = "v7/finance/quote?fields=%s&symbols=%s" % (
            QUOTE_FIELDS, urllib.parse.quote(",".join(syms)))
        try:
            data = yahoo_get(qs)
            results[idx] = data.get("quoteResponse", {}).get("result", []) or []
        except Exception as e:
            print("[quotes] chunk %d error: %s" % (idx, e))
            results[idx] = []

    threads = [threading.Thread(target=work, args=(i, c)) for i, c in enumerate(chunks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for lst in results:
        for r in lst or []:
            sym = r.get("symbol")
            if sym:
                out[sym] = r
    return out


def fetch_chart(symbol, rng, interval):
    key = ("chart", symbol, rng, interval)
    cached = cache_get(key, 240)
    if cached:
        return cached
    qs = "v8/finance/chart/%s?range=%s&interval=%s&includePrePost=false" % (
        urllib.parse.quote(symbol), rng, interval)
    try:
        # chart endpoint does not require a crumb, but sending one is harmless
        data = yahoo_get(qs)
    except Exception as e:
        return {"error": str(e)}
    cache_put(key, data)
    return data


def fetch_enrich(symbol):
    key = ("enrich", symbol)
    cached = cache_get(key, 1800)
    if cached:
        return cached
    mods = "summaryDetail,defaultKeyStatistics,financialData"
    qs = "v10/finance/quoteSummary/%s?modules=%s" % (urllib.parse.quote(symbol), mods)
    try:
        data = yahoo_get(qs)
        res = data.get("quoteSummary", {}).get("result") or [{}]
        r = res[0]
        sd = r.get("summaryDetail", {})
        ks = r.get("defaultKeyStatistics", {})
        fd = r.get("financialData", {})

        def g(d, k):
            v = d.get(k)
            if isinstance(v, dict):
                return v.get("raw")
            return v
        out = {
            "priceToSales": g(sd, "priceToSalesTrailing12Months"),
            "pegRatio": g(ks, "pegRatio"),
            "beta": g(sd, "beta") or g(ks, "beta"),
            "returnOnEquity": g(fd, "returnOnEquity"),
            "returnOnAssets": g(fd, "returnOnAssets"),
            "grossMargins": g(fd, "grossMargins"),
            "operatingMargins": g(fd, "operatingMargins"),
            "profitMargins": g(fd, "profitMargins") or g(ks, "profitMargins"),
            "revenueGrowth": g(fd, "revenueGrowth"),
            "earningsGrowth": g(fd, "earningsGrowth") or g(ks, "earningsQuarterlyGrowth"),
            "debtToEquity": g(fd, "debtToEquity"),
            "currentRatio": g(fd, "currentRatio"),
            "quickRatio": g(fd, "quickRatio"),
            "totalCash": g(fd, "totalCash"),
            "freeCashflow": g(fd, "freeCashflow"),
            "targetMeanPrice": g(fd, "targetMeanPrice"),
            "recommendationMean": g(fd, "recommendationMean"),
            "payoutRatio": g(sd, "payoutRatio"),
            "fiftyTwoWeekChange": g(ks, "52WeekChange"),
        }
        cache_put(key, out)
        return out
    except Exception as e:
        return {"error": str(e)}


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, ctype):
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    def _authed(self):
        if not TOKEN:
            return True
        u = urllib.parse.urlparse(self.path)
        if ("alex=" + TOKEN) in self.headers.get("Cookie", "").replace(" ", ""):
            return True
        # first visit: /...?key=<token>  -> set a cookie and redirect to a clean URL
        if urllib.parse.parse_qs(u.query).get("key", [""])[0] == TOKEN:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            self.send_response(302)
            self.send_header("Set-Cookie",
                             "alex=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000%s" % (TOKEN, secure))
            self.send_header("Location", u.path or "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        msg = b"Alex is private. Open it once as this URL with  ?key=YOUR_TOKEN  appended."
        self.send_response(401)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        try:
            self.wfile.write(msg)
        except Exception:
            pass
        return False

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        route = u.path
        if route != "/api/health" and not self._authed():
            return
        try:
            if route in ("/", "/home.html"):
                return self._file("home.html", "text/html; charset=utf-8")
            if route in ("/index.html", "/screener", "/screener.html"):
                return self._file("index.html", "text/html; charset=utf-8")
            if route == "/universe.json":
                return self._file("universe.json", "application/json; charset=utf-8")
            if route == "/favicon.ico":
                return self._file("favicon.ico", "image/x-icon")

            if route == "/api/health":
                try:
                    ensure_credentials()
                    return self._send(200, {"ok": True, "crumb": bool(_crumb)})
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})

            if route == "/api/quotes":
                syms = [s for s in ",".join(q.get("symbols", [])).split(",") if s]
                if not syms:
                    return self._send(400, {"error": "symbols required"})
                ckey = ("quotes", ",".join(sorted(syms)))
                cached = cache_get(ckey, 8)
                if cached:
                    return self._send(200, {"quotes": cached, "cached": True})
                data = fetch_quotes(syms)
                cache_put(ckey, data)
                return self._send(200, {"quotes": data, "cached": False})

            if route == "/api/chart":
                sym = (q.get("symbol") or [""])[0]
                rng = (q.get("range") or ["1y"])[0]
                interval = (q.get("interval") or ["1d"])[0]
                if not sym:
                    return self._send(400, {"error": "symbol required"})
                return self._send(200, fetch_chart(sym, rng, interval))

            if route == "/api/enrich":
                sym = (q.get("symbol") or [""])[0]
                if not sym:
                    return self._send(400, {"error": "symbol required"})
                return self._send(200, fetch_enrich(sym))

            return self._send(404, {"error": "unknown route"})
        except BrokenPipeError:
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self._send(500, {"error": str(e)})
            except Exception:
                pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _open_browser_when_ready(url):
    import socket
    for _ in range(80):
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    ok = False
    try:
        ok = webbrowser.open(url)
    except Exception:
        ok = False
    if not ok:
        try:
            os.startfile(url)  # Windows fallback
            ok = True
        except Exception:
            pass
    if not ok:
        log("could not open a browser automatically.")
        log("open this address manually:  " + url)


def main():
    url = "http://%s:%d" % (HOST, PORT)
    log("=" * 50)
    log(" Alex  ->  " + url)
    log(" keep this window open; close it to stop the server")
    log("=" * 50)

    try:
        srv = Server((HOST, PORT), Handler)
    except OSError as e:
        log("")
        log("port %d is busy - Alex may already be running." % PORT)
        log("close the old window, or just open %s" % url)
        log("(%s)" % e)
        try:
            input("press Enter to exit...")
        except Exception:
            pass
        return

    if "--no-browser" not in sys.argv and HOST in ("127.0.0.1", "localhost"):
        threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()
        log("opening your browser at %s ..." % url)
        log("(if it does not open, paste that address into your browser)")

    # warm the Yahoo credentials in the background so the page loads immediately
    def _warm():
        try:
            ensure_credentials()
            log("market data ready.")
        except Exception as e:
            log("note: Yahoo not reachable yet (%s); will keep retrying." % e)
    threading.Thread(target=_warm, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("stopped.")
        srv.shutdown()


if __name__ == "__main__":
    main()
