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
import hmac
import hashlib
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
# (HOST=0.0.0.0). Set ALEX_USERNAME + ALEX_PASSWORD to lock it down: the site
# is publicly reachable, but every page redirects to /login until you sign in
# with those credentials, then stays signed in via a session cookie. Locally,
# leaving these unset (the default) means no login gate at all -- the app is
# already private just by being bound to 127.0.0.1.
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")
USERNAME = os.environ.get("ALEX_USERNAME", "").strip()
PASSWORD = os.environ.get("ALEX_PASSWORD", "").strip()
ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# Session cookies are HMAC-signed rather than stored server-side, so any
# machine behind the load balancer can verify one without shared state. The
# signing key is derived from the password itself -- nothing extra to
# configure, and it changes automatically if the password ever does.
_SESSION_KEY = hashlib.sha256(("alex-session:" + PASSWORD).encode("utf-8")).digest()
_SESSION_MAX_AGE = 31536000  # 1 year, matches the cookie's Max-Age


def _make_session_cookie():
    payload = USERNAME + "|" + str(int(time.time()) + _SESSION_MAX_AGE)
    sig = hmac.new(_SESSION_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return payload + "." + sig


def _verify_session_cookie(value):
    if not value or "." not in value:
        return False
    payload, _, sig = value.rpartition(".")
    expected = hmac.new(_SESSION_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        user, expiry = payload.rsplit("|", 1)
        return user == USERNAME and int(expiry) >= time.time()
    except ValueError:
        return False

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
    "bid", "ask", "bidSize", "askSize",
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
            "floatShares": g(ks, "floatShares"),
            "shortPercentOfFloat": g(ks, "shortPercentOfFloat"),
            "heldPercentInsiders": g(ks, "heldPercentInsiders"),
            "heldPercentInstitutions": g(ks, "heldPercentInstitutions"),
        }
        cache_put(key, out)
        return out
    except Exception as e:
        return {"error": str(e)}


# ----------------------------------------------------------------------------
# moomoo/Futu OpenD portfolio bridge — LOCAL USE ONLY, read-only, no trading.
#
# This talks to moomoo's own OpenD gateway, which you run yourself on this same
# machine (download it from moomoo, log into your account inside it). OpenD
# then listens on a local port (11111 by default) that only this local Python
# process can reach — nothing here goes over the network to anywhere else, and
# nothing here can place an order: only position_list_query() is called.
#
# Requires:  pip install futu-api
# (moomoo's SDK ships under the `futu` package name; the OpenD wire protocol
# is shared between moomoo and Futu accounts.)
#
# This is intentionally never wired into the cloud deploy — it only matters
# when server.py is run locally (Start Alex.bat) alongside a locally-running
# OpenD, so a missing `futu` package or an unreachable OpenD just degrades to
# "not connected" here instead of breaking anything else.
try:
    from futu import OpenSecTradeContext, TrdMarket, SecurityFirm
    _HAS_FUTU = True
except ImportError:
    _HAS_FUTU = False

OPEND_HOST = os.environ.get("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.environ.get("OPEND_PORT", "11111"))
# Which brokerage entity OpenD was set up under. SecurityFirm.FUTUSECURITIES
# (the SDK's generic default) only surfaces SIMULATE accounts for this login —
# the REAL accounts only appear under SecurityFirm.FUTUMY, confirmed against a
# real funded account (moomoo Malaysia). If your OpenD login is under a
# different regional moomoo entity and your real account isn't showing up,
# try the matching SecurityFirm.* name (FUTUINC/FUTUSG/FUTUAU/FUTUCA/FUTUJP)
# via OPEND_SECURITY_FIRM in the env.
_FIRM_NAME = os.environ.get("OPEND_SECURITY_FIRM", "FUTUMY")
_TRD_MARKET_NAME = os.environ.get("OPEND_TRD_MARKET", "US")


def _open_trd_ctx():
    """Open an OpenSecTradeContext, or raise with a message fit to show the user."""
    if not _HAS_FUTU:
        raise RuntimeError("futu-api is not installed. Run:  pip install futu-api")
    try:
        firm = getattr(SecurityFirm, _FIRM_NAME, SecurityFirm.FUTUSECURITIES)
        market = getattr(TrdMarket, _TRD_MARKET_NAME, TrdMarket.US)
        return OpenSecTradeContext(filter_trdmarket=market, host=OPEND_HOST, port=OPEND_PORT, security_firm=firm)
    except Exception as e:
        raise RuntimeError("Could not reach OpenD at %s:%d — is it running? (%s)" % (OPEND_HOST, OPEND_PORT, e))


def _get_accounts(trd_ctx):
    """List the accounts (trd_env/acc_id pairs) OpenD exposes — a moomoo login can
    have a real account, a paper/simulate account, or both, so callers should loop
    over these rather than assuming TrdEnv.REAL."""
    ret, accs = trd_ctx.get_acc_list()
    if ret != 0:
        raise RuntimeError(str(accs))
    return accs.to_dict("records") if hasattr(accs, "to_dict") else list(accs)


def fetch_positions():
    try:
        trd_ctx = _open_trd_ctx()
    except RuntimeError as e:
        return {"connected": False, "error": str(e)}
    try:
        positions = []
        for acc in _get_accounts(trd_ctx):
            ret, data = trd_ctx.position_list_query(trd_env=acc.get("trd_env"), acc_id=acc.get("acc_id"))
            if ret != 0:
                continue
            rows = data.to_dict("records") if hasattr(data, "to_dict") else list(data)
            for r in rows:
                positions.append({
                    "symbol": (r.get("code") or "").split(".")[-1],
                    "name": r.get("stock_name"),
                    "qty": r.get("qty"),
                    "costPrice": r.get("cost_price"),
                    "price": r.get("nominal_price") or r.get("cur_price"),
                    "marketValue": r.get("market_val"),
                    "pl": r.get("pl_val"),
                    "plPct": r.get("pl_ratio"),
                    "env": acc.get("trd_env"),
                })
        return {"connected": True, "positions": positions}
    except Exception as e:
        return {"connected": False, "error": str(e)}
    finally:
        try:
            trd_ctx.close()
        except Exception:
            pass


def fetch_assets():
    try:
        trd_ctx = _open_trd_ctx()
    except RuntimeError as e:
        return {"connected": False, "error": str(e)}
    try:
        assets = []
        for acc in _get_accounts(trd_ctx):
            ret, data = trd_ctx.accinfo_query(trd_env=acc.get("trd_env"), acc_id=acc.get("acc_id"), currency="USD")
            if ret != 0:
                continue
            rows = data.to_dict("records") if hasattr(data, "to_dict") else list(data)
            for r in rows:
                assets.append({
                    # stringified: real account ids are 64-bit and lose precision
                    # once JS parses them back out of JSON as a Number
                    "accId": str(acc.get("acc_id")),
                    "env": acc.get("trd_env"),
                    "totalAssets": r.get("total_assets"),
                    "cash": r.get("cash"),
                    "marketValue": r.get("market_val"),
                    "longMv": r.get("long_mv"),
                    "shortMv": r.get("short_mv"),
                    "buyingPower": r.get("power"),
                    "currency": "USD",
                })
        return {"connected": True, "assets": assets}
    except Exception as e:
        return {"connected": False, "error": str(e)}
    finally:
        try:
            trd_ctx.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# one-click OpenD install + launch — LOCAL USE ONLY.
#
# Automates the mechanical, credential-free part of setting up OpenD: fetch it
# from moomoo's own CDN, unzip it, start it. It deliberately stops there — it
# never touches your moomoo password. OpenD's own window opens and you log in
# yourself, same as running it by hand.
#
# Only reachable when this server is bound to localhost (see _is_local_run),
# and py7zr (needed only to unzip the .7z) is optional/ImportError-guarded
# just like futu-api, so this is a no-op import-wise on the cloud image.
try:
    import py7zr
    _HAS_PY7ZR = True
except ImportError:
    _HAS_PY7ZR = False

_OPEND_DIR = os.path.join(ROOT, "opend")
_OPEND_VERSION = "10.10.7008"  # last version verified end-to-end; bump if moomoo retires it
_OPEND_ARCHIVE_NAME = "moomoo_OpenD_%s_Windows.7z" % _OPEND_VERSION
_OPEND_LINK_API = "https://www.moomoo.com/api/download-link?file=" + _OPEND_ARCHIVE_NAME

_opend_setup_lock = threading.Lock()
_opend_setup_state = {"status": "idle", "detail": "", "pct": 0}


def _is_local_run():
    return HOST in ("127.0.0.1", "localhost")


def _opend_gui_exe_path():
    if not os.path.isdir(_OPEND_DIR):
        return None
    for dirpath, _dirs, files in os.walk(_OPEND_DIR):
        for f in files:
            if f.lower().startswith("moomoo_opend-gui") and f.lower().endswith(".exe"):
                return os.path.join(dirpath, f)
    return None


def _opend_port_open():
    import socket
    try:
        with socket.create_connection((OPEND_HOST, OPEND_PORT), timeout=1):
            return True
    except OSError:
        return False


def _run_opend_setup():
    global _opend_setup_state
    try:
        if _opend_port_open():
            _opend_setup_state = {"status": "done", "detail": "OpenD is already running.", "pct": 100}
            return
        if not _HAS_PY7ZR:
            raise RuntimeError("py7zr is not installed. Run:  pip install py7zr")

        exe = _opend_gui_exe_path()
        if not exe:
            _opend_setup_state = {"status": "downloading", "detail": "Asking moomoo for a download link...", "pct": 0}
            op = _build_opener()
            _, raw = _http_get(op, _OPEND_LINK_API)
            link_data = json.loads(raw.decode("utf-8", "replace"))
            url = (link_data.get("data") or {}).get("link")
            if not url:
                raise RuntimeError("moomoo did not return a download link: %s" % link_data.get("message"))

            os.makedirs(_OPEND_DIR, exist_ok=True)
            archive_path = os.path.join(_OPEND_DIR, _OPEND_ARCHIVE_NAME)
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length", 0) or 0)
                downloaded = 0
                with open(archive_path, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        pct = int(downloaded * 90 / total) if total else 0
                        _opend_setup_state = {"status": "downloading",
                                               "detail": "%d / %d MB" % (downloaded // 1048576, total // 1048576 or 1),
                                               "pct": pct}

            _opend_setup_state = {"status": "extracting", "detail": "Extracting...", "pct": 92}
            with py7zr.SevenZipFile(archive_path, mode="r") as z:
                z.extractall(path=_OPEND_DIR)
            try:
                os.remove(archive_path)
            except OSError:
                pass
            exe = _opend_gui_exe_path()
            if not exe:
                raise RuntimeError("extracted the archive but couldn't find moomoo_OpenD-GUI*.exe inside it")

        _opend_setup_state = {"status": "launching",
                               "detail": "Opening moomoo OpenD — log in with your moomoo account in its window.",
                               "pct": 96}
        import subprocess
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
        for _ in range(20):
            time.sleep(1)
            if _opend_port_open():
                break
        _opend_setup_state = {"status": "done",
                               "detail": "OpenD is open. Log in with your moomoo account in its window, then open Portfolio.",
                               "pct": 100}
    except Exception as e:
        _opend_setup_state = {"status": "error", "detail": str(e), "pct": 0}


def start_opend_setup():
    global _opend_setup_state
    with _opend_setup_lock:
        if _opend_setup_state["status"] in ("downloading", "extracting", "launching"):
            return dict(_opend_setup_state)
        _opend_setup_state = {"status": "downloading", "detail": "Starting...", "pct": 0}
        threading.Thread(target=_run_opend_setup, daemon=True).start()
        return dict(_opend_setup_state)


# ----------------------------------------------------------------------------
# Telegram bot bridge — push-only, no trading.
#
# Lets ALEX send your screener results to a Telegram bot you create yourself
# (via @BotFather). This never places trades and never will — it only ever
# calls Telegram's sendMessage API with text you already see on screen.
#
# The bot token is a real credential (whoever has it can control the bot), so
# it's kept in a local JSON file next to server.py, gitignored, and never
# echoed back in any API response once saved.
_TG_CONFIG_PATH = os.path.join(ROOT, "telegram.json")
_tg_lock = threading.Lock()


def _tg_load():
    try:
        with open(_TG_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _tg_save(data):
    with open(_TG_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _tg_call(token, method, params=None):
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = "https://api.telegram.org/bot%s/%s%s" % (token, method, qs)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # Telegram still sends a JSON body with a real description on 4xx —
        # urlopen raises before we get to read it, so recover it here instead
        # of surfacing a bare "HTTP Error 404".
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"ok": False, "description": str(e)}


def connect_telegram(token):
    token = (token or "").strip()
    if not token:
        return {"connected": False, "error": "token required"}
    try:
        me = _tg_call(token, "getMe")
        if not me.get("ok"):
            return {"connected": False, "error": me.get("description") or "invalid token"}
        username = me["result"].get("username")

        updates = _tg_call(token, "getUpdates", {"limit": 1, "offset": -1})
        results = updates.get("result") or []
        if not results:
            return {"connected": False,
                    "error": "Send any message to @%s on Telegram first, then click Connect again "
                             "(that's how Telegram tells ALEX which chat to send to)." % username}
        chat = (results[-1].get("message") or results[-1].get("channel_post") or {}).get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            return {"connected": False, "error": "could not find a chat id in your most recent message"}

        with _tg_lock:
            _tg_save({"token": token, "chatId": chat_id, "botUsername": username})
        _tg_call(token, "sendMessage", {"chat_id": chat_id, "text": "✅ Connected to Alex."})
        return {"connected": True, "botUsername": username}
    except Exception as e:
        return {"connected": False, "error": str(e)}


def telegram_status():
    cfg = _tg_load()
    if not cfg.get("token"):
        return {"connected": False}
    return {"connected": True, "botUsername": cfg.get("botUsername")}


def disconnect_telegram():
    with _tg_lock:
        try:
            os.remove(_TG_CONFIG_PATH)
        except OSError:
            pass
    return {"connected": False}


def send_telegram_message(text):
    cfg = _tg_load()
    if not cfg.get("token"):
        return {"ok": False, "error": "Telegram is not connected."}
    try:
        r = _tg_call(cfg["token"], "sendMessage", {"chat_id": cfg["chatId"], "text": text})
        if not r.get("ok"):
            return {"ok": False, "error": r.get("description") or "send failed"}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
        if not (USERNAME and PASSWORD):
            return True
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("alex_session=") and _verify_session_cookie(part[len("alex_session="):]):
                return True
        if self.command == "GET":
            dest = "/login?next=" + urllib.parse.quote(self.path, safe="")
            self.send_response(302)
            self.send_header("Location", dest)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(401, {"error": "unauthorized"})
        return False

    def _set_session_cookie(self, value, max_age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        self.send_header("Set-Cookie", "alex_session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s"
                                        % (value, max_age, secure))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        route = u.path
        if route not in ("/api/health", "/login", "/login.html", "/favicon.ico") and not self._authed():
            return
        try:
            if route in ("/login", "/login.html"):
                return self._file("login.html", "text/html; charset=utf-8")
            if route in ("/", "/home.html"):
                return self._file("home.html", "text/html; charset=utf-8")
            if route in ("/portfolio", "/portfolio.html"):
                return self._file("portfolio.html", "text/html; charset=utf-8")
            if route in ("/assets", "/assets.html"):
                return self._file("assets.html", "text/html; charset=utf-8")
            if route in ("/telegram", "/telegram.html"):
                return self._file("telegram.html", "text/html; charset=utf-8")
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

            if route == "/api/positions":
                return self._send(200, fetch_positions())

            if route == "/api/assets":
                return self._send(200, fetch_assets())

            if route == "/api/opend/setup":
                if not _is_local_run():
                    return self._send(403, {"error": "local only"})
                return self._send(200, start_opend_setup())

            if route == "/api/opend/status":
                if not _is_local_run():
                    return self._send(403, {"error": "local only"})
                return self._send(200, dict(_opend_setup_state))

            if route == "/api/opend/probe":
                # cheap, side-effect-free port check for "are we already
                # connected" on page load -- unlike /api/opend/setup this
                # never starts a download.
                if not _is_local_run():
                    return self._send(403, {"error": "local only"})
                return self._send(200, {"connected": _opend_port_open()})

            if route == "/api/telegram/status":
                return self._send(200, telegram_status())

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

    def _json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except Exception:
            return {}

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        route = u.path
        try:
            if route == "/api/login":
                body = self._json_body()
                ok = (not (USERNAME and PASSWORD)) or (
                    body.get("username") == USERNAME
                    and hmac.compare_digest(str(body.get("password") or ""), PASSWORD)
                )
                if not ok:
                    return self._send(401, {"ok": False, "error": "Invalid username or password."})
                payload = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._set_session_cookie(_make_session_cookie(), _SESSION_MAX_AGE)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if route == "/api/logout":
                payload = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._set_session_cookie("", 0)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if not self._authed():
                return

            if route == "/api/telegram/connect":
                body = self._json_body()
                return self._send(200, connect_telegram(body.get("token")))

            if route == "/api/telegram/disconnect":
                return self._send(200, disconnect_telegram())

            if route == "/api/telegram/send":
                body = self._json_body()
                text = (body.get("text") or "").strip()
                if not text:
                    return self._send(400, {"error": "text required"})
                return self._send(200, send_telegram_message(text))

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
