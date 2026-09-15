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
import secrets
import threading
import gzip
import io
import re
import base64
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
# (HOST=0.0.0.0). Two ways to lock the site down, either or both:
#   - ALEX_USERNAME + ALEX_PASSWORD: a single owner login.
#   - GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET: "Sign in with Google" -- any
#     Google account may sign in (there's no allowlist; this app has no
#     concept of rejecting a Google identity, only of scoping each user's own
#     data to their own account -- see the Telegram bridge below).
# Whichever way someone signs in, every page redirects to /login until they
# do, then they stay signed in via a session cookie. Locally, leaving all of
# these unset (the default) means no login gate at all -- the app is already
# private just by being bound to 127.0.0.1.
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")
USERNAME = os.environ.get("ALEX_USERNAME", "").strip()
PASSWORD = os.environ.get("ALEX_PASSWORD", "").strip()
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def _login_required():
    # Single-owner only now -- public "anyone with a Google account" sign-in
    # was removed, so GOOGLE_CLIENT_ID/SECRET (still used for the separate,
    # already-logged-in-only Gmail-connect feature) no longer implies a login
    # requirement on their own.
    return bool(USERNAME and PASSWORD)


# Session cookies are HMAC-signed rather than stored server-side, so any
# machine behind the load balancer can verify one without shared state.
# ALEX_SECRET_KEY is the recommended signing key; if unset it falls back to
# the password (fine for password-only setups, but Google-only setups should
# set ALEX_SECRET_KEY explicitly since there's no password to derive from).
_SECRET_KEY = os.environ.get("ALEX_SECRET_KEY", "").strip() or PASSWORD or "alex-dev-key"
_SESSION_KEY = hashlib.sha256(("alex-session:" + _SECRET_KEY).encode("utf-8")).digest()
_SESSION_MAX_AGE = 31536000  # 1 year, matches the cookie's Max-Age


def _make_session_cookie(user_id):
    payload = user_id + "|" + str(int(time.time()) + _SESSION_MAX_AGE)
    sig = hmac.new(_SESSION_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return payload + "." + sig


def _verify_session_cookie(value):
    """Returns the signed-in user's id (email, or the login username), or None."""
    if not value or "." not in value:
        return None
    payload, _, sig = value.rpartition(".")
    expected = hmac.new(_SESSION_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        user, expiry = payload.rsplit("|", 1)
        return user if int(expiry) >= time.time() else None
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Google helper shared by the Gmail-connect feature (see gmail bridge below).
# There's no general "Sign in with Google" anymore -- this app is
# single-owner only; only the Gmail read-only OAuth grant (a separate,
# already-logged-in-only flow) still needs Google's API.
# ----------------------------------------------------------------------------
def _google_userinfo(access_token):
    req = urllib.request.Request("https://www.googleapis.com/oauth2/v3/userinfo",
                                  headers={"Authorization": "Bearer " + access_token, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


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


def _opend_gui_running():
    """Is a moomoo_OpenD-GUI*.exe process already running? Used to avoid
    launching a second instance on top of one still sitting at its login
    screen -- two instances fight over the port and neither ever binds it."""
    try:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq moomoo_OpenD-GUI*"],
            capture_output=True, text=True, timeout=10
        ).stdout
        return "moomoo_OpenD-GUI" in out
    except Exception:
        return False


# Windows Startup folder shortcut -- lets OpenD launch itself the moment you
# log into Windows, instead of needing to be relaunched by hand every time.
# Windows will still show its own permission prompt each login (OpenD needs
# admin rights to run at all -- see the "runas" launch above); nothing here
# can silence that, it's inherent to the program requiring elevation.
def _opend_autostart_link():
    startup = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                            "Start Menu", "Programs", "Startup")
    return os.path.join(startup, "moomoo OpenD.lnk")


def opend_autostart_enabled():
    return os.path.isfile(_opend_autostart_link())


def set_opend_autostart(enabled):
    link = _opend_autostart_link()
    if not enabled:
        try:
            os.remove(link)
        except OSError:
            pass
        return {"enabled": False}
    exe = _opend_gui_exe_path()
    if not exe:
        return {"enabled": False, "error": "Connect moomoo at least once first, so OpenD is downloaded."}
    try:
        import subprocess
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s'); "
              "$s.TargetPath='%s'; $s.WorkingDirectory='%s'; $s.Save()"
              % (link.replace("'", "''"), exe.replace("'", "''"), os.path.dirname(exe).replace("'", "''")))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=15, check=True)
        return {"enabled": True}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


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

        # If OpenD is already open (e.g. sitting at its login screen from a
        # previous click), launching again spawns a second instance that
        # fights the first one for the port -- neither ever binds it, and the
        # user is left with two stuck windows and no way in. So: only launch
        # if nothing's running yet: otherwise just tell them to finish
        # logging into the window that's already open.
        if _opend_gui_running():
            _opend_setup_state = {"status": "launching",
                                   "detail": "moomoo OpenD is already open — finish logging in in its window.",
                                   "pct": 96}
        else:
            _opend_setup_state = {"status": "launching",
                                   "detail": "Opening moomoo OpenD — log in with your moomoo account in its window.",
                                   "pct": 96}
            # OpenD's GUI requires administrator rights to run -- plain Popen
            # fails with WinError 740 (elevation required). os.startfile(...,
            # "runas") triggers the normal Windows UAC consent prompt instead,
            # same as right-click > "Run as administrator". The user still has
            # to click Yes there themselves; nothing here bypasses that consent.
            try:
                os.startfile(exe, "runas")
            except Exception:
                import subprocess
                subprocess.Popen([exe], cwd=os.path.dirname(exe))

        # Give them real time to type credentials + any 2FA rather than
        # falsely reporting "done" the moment a short wait elapses.
        for _ in range(180):
            time.sleep(1)
            if _opend_port_open():
                _opend_setup_state = {"status": "done",
                                       "detail": "OpenD is open and connected. Open Portfolio to see your data.",
                                       "pct": 100}
                return
        _opend_setup_state = {"status": "error",
                               "detail": "Still waiting for you to finish logging into the OpenD window — "
                                         "once you have, click Connect again.",
                               "pct": 0}
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
# it's kept in a local JSON file next to server.py (gitignored) and never
# echoed back in any API response once saved. Since sign-in now allows any
# Google account, this is keyed per signed-in user -- one file holding
# {user_id: {token, chatId, botUsername}}, so different people's bots stay
# separate. "local" is the key used when no login is configured at all
# (single-machine local runs, matching the previous single-user behavior).
#
# On Fly, the container's own disk is wiped on every redeploy -- if a Fly
# Volume is mounted, the store lives there instead so Telegram connections
# survive deploys. Auto-detected at /data (fly.toml's mount destination) so
# nothing extra needs configuring beyond attaching the volume; ALEX_DATA_DIR
# overrides that path if you mount it somewhere else. Locally there's no
# volume, so it just falls back to sitting next to server.py, same as before.
_DATA_DIR = os.environ.get("ALEX_DATA_DIR", "").strip() or ("/data" if os.path.isdir("/data") else ROOT)
_TG_STORE_PATH = os.path.join(_DATA_DIR, "telegram.json")
_tg_lock = threading.Lock()


def _tg_load_all():
    try:
        with open(_TG_STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _tg_save_all(data):
    with open(_TG_STORE_PATH, "w", encoding="utf-8") as f:
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


def connect_telegram(user_id, token):
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
            all_cfg = _tg_load_all()
            all_cfg[user_id] = {"token": token, "chatId": chat_id, "botUsername": username}
            _tg_save_all(all_cfg)
        _tg_call(token, "sendMessage", {"chat_id": chat_id, "text": "✅ Connected to Alex."})
        return {"connected": True, "botUsername": username}
    except Exception as e:
        return {"connected": False, "error": str(e)}


def telegram_status(user_id):
    cfg = _tg_load_all().get(user_id) or {}
    if not cfg.get("token"):
        return {"connected": False}
    return {"connected": True, "botUsername": cfg.get("botUsername")}


def disconnect_telegram(user_id):
    with _tg_lock:
        all_cfg = _tg_load_all()
        if user_id in all_cfg:
            del all_cfg[user_id]
            _tg_save_all(all_cfg)
    return {"connected": False}


def send_telegram_message(user_id, text):
    cfg = _tg_load_all().get(user_id) or {}
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
# Gmail statement reader — opt-in, per-user, read-only.
#
# Lets Alex search your own Gmail for bank/e-wallet statement emails you
# specify by keyword, pull the PDF attachment, and try to read a balance
# figure out of it. This only ever reads messages matching your own search
# query — it never sends, modifies, or deletes anything, and never looks at
# messages outside what you searched for.
#
# This is a separate, opt-in OAuth grant from signing in (gmail.readonly is
# a far more sensitive scope than the openid/email/profile used to sign in),
# and Google classifies it as a "restricted" scope: production apps need to
# pass Google's CASA security review before the public can grant it. Until
# then, only Google accounts added as test users in Cloud Console can connect
# this — everyone else just won't see it work, which is expected during that
# window, not a bug.
try:
    import pypdf
    _HAS_PYPDF = True
except ImportError:
    _HAS_PYPDF = False

_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_GMAIL_STORE_PATH = os.path.join(_DATA_DIR, "gmail.json")
_gmail_lock = threading.Lock()

_BALANCE_RE = re.compile(
    r"(?:ending\s*balance|available\s*balance|current\s*balance|closing\s*balance|"
    r"balance\s*b\W?f|结余|餘額|余额)"
    r"[^\d\-]{0,40}(?:RM|MYR|\$)?\s*([\d,]+\.\d{2})",
    re.IGNORECASE,
)


def _gmail_load_all():
    try:
        with open(_GMAIL_STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _gmail_save_all(data):
    with open(_GMAIL_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _gmail_auth_url(redirect_uri, state):
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _GMAIL_SCOPE + " openid email",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def _gmail_exchange_code(code, redirect_uri):
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _gmail_refresh_token(refresh_token):
    data = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _gmail_access_token(user_id):
    """A valid access token for this user, refreshing if needed. Raises if not connected."""
    entry = _gmail_load_all().get(user_id)
    if not entry or not entry.get("refresh_token"):
        raise RuntimeError("Gmail is not connected.")
    if entry.get("access_token") and entry.get("expiresAt", 0) > time.time() + 60:
        return entry["access_token"]
    tok = _gmail_refresh_token(entry["refresh_token"])
    if "access_token" not in tok:
        raise RuntimeError(tok.get("error_description") or "Could not refresh Gmail access.")
    with _gmail_lock:
        all_data = _gmail_load_all()
        if user_id in all_data:
            all_data[user_id]["access_token"] = tok["access_token"]
            all_data[user_id]["expiresAt"] = time.time() + int(tok.get("expires_in", 3600))
            _gmail_save_all(all_data)
    return tok["access_token"]


def _gmail_api_get(access_token, url):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + access_token, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # Surface Google's actual error body (e.g. "Gmail API has not been
        # used in project ... before or it is disabled") instead of a bare
        # "HTTP Error 403: Forbidden", which is useless for debugging.
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
            msg = (body.get("error") or {}).get("message") or str(e)
        except Exception:
            msg = str(e)
        raise RuntimeError(msg)


def gmail_status(user_id):
    entry = _gmail_load_all().get(user_id)
    if not entry or not entry.get("refresh_token"):
        return {"connected": False}
    return {"connected": True, "email": entry.get("email"),
            "keywords": entry.get("keywords", []), "balances": entry.get("balances", [])}


def gmail_disconnect(user_id):
    with _gmail_lock:
        all_data = _gmail_load_all()
        if user_id in all_data:
            del all_data[user_id]
            _gmail_save_all(all_data)
    return {"connected": False}


def gmail_set_keywords(user_id, keywords):
    with _gmail_lock:
        all_data = _gmail_load_all()
        if user_id not in all_data:
            return {"ok": False, "error": "Gmail is not connected."}
        all_data[user_id]["keywords"] = keywords
        _gmail_save_all(all_data)
    return {"ok": True}


def gmail_confirm_balance(user_id, query, balance):
    """Lets the user manually pick the right figure out of the candidates
    list when the automatic extraction wasn't confident enough to pick one."""
    with _gmail_lock:
        all_data = _gmail_load_all()
        entry = all_data.get(user_id)
        if not entry:
            return {"ok": False, "error": "Gmail is not connected."}
        for b in entry.get("balances") or []:
            if b.get("query") == query:
                b["balance"] = balance
                b["candidates"] = []
                b["error"] = None
        _gmail_save_all(all_data)
    return {"ok": True}


def _extract_balance_from_text(text):
    m = _BALANCE_RE.search(text)
    return m.group(1).replace(",", "") if m else None


# Fallback when no labeled "ending balance"-type phrase matches: just grab
# every money-shaped number in the text, deduplicated, in order of
# appearance. Deliberately permissive -- the exact right figure isn't always
# next to a recognizable label, so surface candidates for a human to glance
# at rather than silently reporting nothing.
_AMOUNT_RE = re.compile(r"(?:RM|MYR|\$)?\s?([\d]{1,3}(?:,\d{3})*\.\d{2})\b")


def _extract_amount_candidates(text, limit=6):
    seen, out = set(), []
    for m in _AMOUNT_RE.finditer(text):
        val = m.group(1)
        if val not in seen:
            seen.add(val)
            out.append(val)
        if len(out) >= limit:
            break
    return out


def _pdf_text(raw_bytes):
    if not _HAS_PYPDF:
        return ""
    reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _walk_gmail_parts(payload):
    parts = []

    def walk(p):
        parts.append(p)
        for sub in p.get("parts") or []:
            walk(sub)
    walk(payload or {})
    return parts


def _b64url_decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def gmail_fetch(user_id):
    entry = _gmail_load_all().get(user_id)
    if not entry:
        return {"ok": False, "error": "Gmail is not connected."}
    keywords = entry.get("keywords") or []
    if not keywords:
        return {"ok": False, "error": "No keywords set yet."}
    if not _HAS_PYPDF:
        return {"ok": False, "error": "pypdf is not installed. Run:  pip install pypdf"}
    try:
        access_token = _gmail_access_token(user_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    results = []
    for kw in keywords:
        label, query = kw.get("label", ""), kw.get("query", "")
        try:
            search = _gmail_api_get(access_token,
                "https://www.googleapis.com/gmail/v1/users/me/messages?" +
                urllib.parse.urlencode({"q": query, "maxResults": 5}))
            msg_ids = [m["id"] for m in (search.get("messages") or [])]
            if not msg_ids:
                results.append({"label": label, "query": query, "balance": None, "candidates": [],
                                 "subject": None, "checkedAt": int(time.time()),
                                 "error": "No emails matched this search."})
                continue

            found_balance, found_subject, candidates = None, None, []
            for mid in msg_ids:
                msg = _gmail_api_get(access_token,
                    "https://www.googleapis.com/gmail/v1/users/me/messages/%s?format=full" % mid)
                subject = ""
                for h in (msg.get("payload", {}).get("headers") or []):
                    if h.get("name") == "Subject":
                        subject = h.get("value", "")
                # gather this message's full readable text (PDF attachments +
                # any text/plain or text/html parts) before deciding anything,
                # so the broad fallback below sees everything, not just
                # whichever part happened to come first.
                combined_text = []
                for part in _walk_gmail_parts(msg.get("payload")):
                    mime = part.get("mimeType", "")
                    body = part.get("body", {})
                    if mime == "application/pdf" and body.get("attachmentId"):
                        att = _gmail_api_get(access_token,
                            "https://www.googleapis.com/gmail/v1/users/me/messages/%s/attachments/%s"
                            % (mid, body["attachmentId"]))
                        if att.get("data"):
                            combined_text.append(_pdf_text(_b64url_decode(att["data"])))
                    elif mime.startswith("text/") and body.get("data"):
                        combined_text.append(_b64url_decode(body["data"]).decode("utf-8", "replace"))
                text = "\n".join(combined_text)
                balance = _extract_balance_from_text(text)
                if balance:
                    found_balance, found_subject = balance, subject
                    break
                if not candidates:
                    msg_candidates = _extract_amount_candidates(text)
                    if msg_candidates:
                        candidates, found_subject = msg_candidates, subject

            results.append({
                "label": label, "query": query, "balance": found_balance,
                "candidates": [] if found_balance else candidates,
                "subject": found_subject, "checkedAt": int(time.time()),
                "error": None if (found_balance or candidates) else
                         "Found matching emails, but no dollar amounts in them.",
            })
        except Exception as e:
            results.append({"label": label, "query": query, "balance": None, "candidates": [],
                             "error": str(e), "checkedAt": int(time.time())})

    with _gmail_lock:
        all_data = _gmail_load_all()
        if user_id in all_data:
            all_data[user_id]["balances"] = results
            _gmail_save_all(all_data)
    return {"ok": True, "balances": results}


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

    def _get_cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return None

    def _current_user(self):
        """The signed-in user's id (their email, or the login username), or None."""
        raw = self._get_cookie("alex_session")
        return _verify_session_cookie(raw) if raw else None

    def _authed(self):
        if not _login_required():
            return True
        if self._current_user():
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

    def _base_url(self):
        proto = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
        return "%s://%s" % (proto, self.headers.get("Host", "127.0.0.1"))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        route = u.path
        _public = ("/api/health", "/login", "/login.html", "/favicon.ico",
                   "/privacy", "/privacy.html")
        if route not in _public and not self._authed():
            return
        try:
            if route in ("/login", "/login.html"):
                return self._file("login.html", "text/html; charset=utf-8")

            if route in ("/privacy", "/privacy.html"):
                return self._file("privacy.html", "text/html; charset=utf-8")

            if route == "/api/auth/gmail/start":
                if not self._authed():
                    return
                if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
                    self.send_response(302)
                    self.send_header("Location", "/gmail?error=" + urllib.parse.quote("Google sign-in isn't configured."))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                state = secrets.token_urlsafe(24)
                redirect_uri = self._base_url() + "/api/auth/gmail/callback"
                self.send_response(302)
                self.send_header("Location", _gmail_auth_url(redirect_uri, state))
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                self.send_header("Set-Cookie", "alex_gmail_state=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=600%s" % (state, secure))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if route == "/api/auth/gmail/callback":
                if not self._authed():
                    return
                user_id = self._current_user() or "local"

                def _gfail(msg):
                    self.send_response(302)
                    self.send_header("Location", "/gmail?error=" + urllib.parse.quote(msg))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                code = (q.get("code") or [""])[0]
                state = (q.get("state") or [""])[0]
                expected_state = self._get_cookie("alex_gmail_state")
                if not code or not state or not expected_state or not hmac.compare_digest(state, expected_state):
                    return _gfail("Gmail connect failed (state mismatch) — please try again.")
                try:
                    redirect_uri = self._base_url() + "/api/auth/gmail/callback"
                    tokens = _gmail_exchange_code(code, redirect_uri)
                    if "refresh_token" not in tokens:
                        return _gfail("Google didn't grant offline access — disconnect any prior Alex grant at "
                                      "https://myaccount.google.com/permissions and try again.")
                    info = _google_userinfo(tokens["access_token"])
                    with _gmail_lock:
                        all_data = _gmail_load_all()
                        all_data[user_id] = {
                            "email": info.get("email"),
                            "refresh_token": tokens["refresh_token"],
                            "access_token": tokens.get("access_token"),
                            "expiresAt": time.time() + int(tokens.get("expires_in", 3600)),
                            "keywords": (all_data.get(user_id) or {}).get("keywords", []),
                            "balances": (all_data.get(user_id) or {}).get("balances", []),
                        }
                        _gmail_save_all(all_data)
                except Exception as e:
                    return _gfail("Gmail connect failed: %s" % e)
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                self.send_response(302)
                self.send_header("Location", "/gmail")
                self.send_header("Set-Cookie", "alex_gmail_state=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0%s" % secure)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if route in ("/", "/home.html"):
                return self._file("home.html", "text/html; charset=utf-8")
            if route in ("/portfolio", "/portfolio.html"):
                return self._file("portfolio.html", "text/html; charset=utf-8")
            if route in ("/assets", "/assets.html"):
                return self._file("assets.html", "text/html; charset=utf-8")
            if route in ("/telegram", "/telegram.html"):
                return self._file("telegram.html", "text/html; charset=utf-8")
            if route in ("/moomoo", "/moomoo.html"):
                return self._file("moomoo.html", "text/html; charset=utf-8")
            if route in ("/gmail", "/gmail.html"):
                return self._file("gmail.html", "text/html; charset=utf-8")
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

            if route == "/api/opend/autostart":
                if not _is_local_run():
                    return self._send(403, {"error": "local only"})
                return self._send(200, {"enabled": opend_autostart_enabled()})

            if route == "/api/telegram/status":
                return self._send(200, telegram_status(self._current_user() or "local"))

            if route == "/api/gmail/status":
                return self._send(200, gmail_status(self._current_user() or "local"))

            if route == "/api/whoami":
                user = self._current_user()
                return self._send(200, {"user": user})

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
                self._set_session_cookie(_make_session_cookie(USERNAME or "local"), _SESSION_MAX_AGE)
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
            user_id = self._current_user() or "local"

            if route == "/api/telegram/connect":
                body = self._json_body()
                return self._send(200, connect_telegram(user_id, body.get("token")))

            if route == "/api/telegram/disconnect":
                return self._send(200, disconnect_telegram(user_id))

            if route == "/api/telegram/send":
                body = self._json_body()
                text = (body.get("text") or "").strip()
                if not text:
                    return self._send(400, {"error": "text required"})
                return self._send(200, send_telegram_message(user_id, text))

            if route == "/api/gmail/disconnect":
                return self._send(200, gmail_disconnect(user_id))

            if route == "/api/gmail/keywords":
                body = self._json_body()
                keywords = body.get("keywords")
                if not isinstance(keywords, list):
                    return self._send(400, {"error": "keywords must be a list"})
                clean = [{"label": str(k.get("label", ""))[:60], "query": str(k.get("query", ""))[:300]}
                         for k in keywords if isinstance(k, dict) and k.get("query")]
                return self._send(200, gmail_set_keywords(user_id, clean))

            if route == "/api/gmail/confirm":
                body = self._json_body()
                query = str(body.get("query", ""))
                balance = str(body.get("balance", ""))
                if not query or not balance:
                    return self._send(400, {"error": "query and balance required"})
                return self._send(200, gmail_confirm_balance(user_id, query, balance))

            if route == "/api/gmail/fetch":
                return self._send(200, gmail_fetch(user_id))

            if route == "/api/opend/autostart":
                if not _is_local_run():
                    return self._send(403, {"error": "local only"})
                body = self._json_body()
                return self._send(200, set_opend_autostart(bool(body.get("enabled"))))

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
