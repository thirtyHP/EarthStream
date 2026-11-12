# Earthdata Login and HTTP plumbing, standard library only.
#
# Signing in trades a username and password for an Earthdata Login user token
# (urs.earthdata.nasa.gov/api/users/find_or_create_token). The token is then:
#   - handed to GDAL as an "Authorization: Bearer" header, so /vsicurl/ can stream
#     protected COGs from LP DAAC and PO.DAAC, and
#   - used by our own downloader, which drops the header when the archive redirects
#     to a signed S3 URL (S3 rejects requests that carry two kinds of auth).
# The password is never written to disk. "Remember" keeps the username and token.

import base64
import datetime
import json
import os
import ssl
import stat
import urllib.error
import urllib.parse
import urllib.request

from .providers import EDL_TOKEN_URL

USER_AGENT = "EarthStream-QGIS/0.1"
CLIENT_ID = "earthstream-qgis"

_state = {"token": None, "username": None, "expires": None, "header_file": None}


# ----- SSL ------------------------------------------------------------------------

def _ssl_context():
    ctx = ssl.create_default_context()
    try:
        # Some QGIS bundles ship a Python without system CA certificates.
        import certifi  # noqa: F401
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


# ----- token handling -------------------------------------------------------------

class AuthError(Exception):
    pass


def fetch_token(username, password, timeout=30):
    """Return (token, expiry date or None) for an Earthdata Login account."""
    raw = ("%s:%s" % (username, password)).encode("utf-8")
    req = urllib.request.Request(EDL_TOKEN_URL, data=b"", method="POST")
    req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode("ascii"))
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise AuthError("Earthdata Login rejected that username or password.")
        if e.code == 403:
            raise AuthError("Earthdata Login refused to issue a token (HTTP 403). Check that your account "
                            "is verified, or generate a token at urs.earthdata.nasa.gov and paste it in.")
        raise AuthError("Earthdata Login returned HTTP %s." % e.code)
    except urllib.error.URLError as e:
        raise AuthError("Could not reach Earthdata Login: %s" % e.reason)

    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    token = payload.get("access_token")
    if not token:
        raise AuthError("Earthdata Login answered without a token.")
    return token, parse_expiry(payload.get("expiration_date"))


def parse_expiry(text):
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def token_expiry_from_jwt(token):
    # EDL user tokens are JWTs; the payload carries an "exp" timestamp.
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        exp = json.loads(base64.urlsafe_b64decode(part)).get("exp")
        if not exp:
            return None
        return datetime.datetime.fromtimestamp(int(exp), datetime.timezone.utc).date()
    except Exception:
        return None


def set_session(token, username=None, expires=None, cache_dir=None):
    _state["token"] = token
    _state["username"] = username
    _state["expires"] = expires or token_expiry_from_jwt(token)
    configure_gdal(token, cache_dir)


def clear_session(cache_dir=None):
    _state.update(token=None, username=None, expires=None)
    configure_gdal(None, cache_dir)


def token():
    return _state["token"]


def username():
    return _state["username"]


def expires():
    return _state["expires"]


def is_signed_in():
    tok = _state["token"]
    if not tok:
        return False
    exp = _state["expires"]
    return exp is None or exp >= datetime.date.today()


# ----- GDAL configuration -----------------------------------------------------------

def configure_gdal(tok, cache_dir=None):
    """Point GDAL's /vsicurl/ at our token and tune it for COG streaming."""
    from osgeo import gdal

    opts = {
        # Don't list remote "directories" before opening a file; COGs don't need it.
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff,.vrt,.zip,.hgt",
        "GDAL_HTTP_MAX_RETRY": "4",
        "GDAL_HTTP_RETRY_DELAY": "1",
        "GDAL_HTTP_MULTIRANGE": "YES",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "GDAL_HTTP_USERAGENT": USER_AGENT,
        "VSI_CACHE": "TRUE",
        "VSI_CACHE_SIZE": str(256 * 1024 * 1024),
    }
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cookie = os.path.join(cache_dir, "edl_cookies.txt")
        opts["GDAL_HTTP_COOKIEFILE"] = cookie
        opts["GDAL_HTTP_COOKIEJAR"] = cookie

    for k, v in opts.items():
        gdal.SetConfigOption(k, v)
    _state["cache_dir"] = cache_dir or _state.get("cache_dir")
    _apply_token(tok)


# The token only goes to NASA archive hosts. GDAL 3.6+ supports path-specific
# options, so the header is attached to these URL prefixes and nothing else;
# any other COG QGIS streams through GDAL never sees it. Hosts found in search
# results are added as they appear.
DEFAULT_HOSTS = [
    "data.lpdaac.earthdatacloud.nasa.gov",
    "archive.podaac.earthdata.nasa.gov",
]
_hosts = set(DEFAULT_HOSTS)


def is_nasa_host(host):
    host = host.lower()
    return host.endswith(".earthdata.nasa.gov") or host.endswith(".earthdatacloud.nasa.gov")


def register_hosts(urls):
    """Remember archive hosts seen in search results so the token is attached to them too."""
    new = set()
    for u in urls:
        host = urllib.parse.urlparse(u).netloc
        if host and is_nasa_host(host) and host not in _hosts:
            new.add(host)
    if new:
        _hosts.update(new)
        _apply_token(_state["token"])


def release_gdal():
    _apply_token(None)


def _prefixes():
    return ["/vsicurl/https://%s/" % h for h in sorted(_hosts)]


def _apply_token(tok):
    from osgeo import gdal

    old = _state.get("header_file")
    if old and os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass
    _state["header_file"] = None
    gdal.SetConfigOption("GDAL_HTTP_HEADER_FILE", None)
    gdal.SetConfigOption("GDAL_HTTP_HEADERS", None)

    header = "Authorization: Bearer %s" % tok if tok else None
    if hasattr(gdal, "SetPathSpecificOption"):
        for prefix in _prefixes():
            gdal.SetPathSpecificOption(prefix, "GDAL_HTTP_HEADERS", header)
        return

    # GDAL older than 3.6 has no per-host options, so the header has to be global there.
    if tok:
        cache_dir = _state.get("cache_dir")
        gdal.SetConfigOption("GDAL_HTTP_HEADERS", header)
        if cache_dir:
            path = os.path.join(cache_dir, "edl_header.txt")
            with open(path, "w") as fh:
                fh.write(header + "\n")
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            gdal.SetConfigOption("GDAL_HTTP_HEADER_FILE", path)
            _state["header_file"] = path


# ----- HTTP -------------------------------------------------------------------------

class _DropAuthOnHostChange(urllib.request.HTTPRedirectHandler):
    # Earthdata archives answer with a redirect to a pre-signed S3 URL. urllib would
    # carry our Authorization header along, and S3 refuses requests with two auth
    # mechanisms, so the header goes away whenever the host changes.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            for key in list(new.headers):
                if key.lower() == "authorization":
                    del new.headers[key]
            for key in list(new.unredirected_hdrs):
                if key.lower() == "authorization":
                    del new.unredirected_hdrs[key]
        return new


def _opener():
    import http.cookiejar
    return urllib.request.build_opener(
        _DropAuthOnHostChange(),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        urllib.request.HTTPSHandler(context=_ssl_context()),
    )


def get_json(url, params=None, headers=None, timeout=60):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Client-Id", CLIENT_ID)
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body), dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("errors", "")
        except Exception:
            pass
        raise RuntimeError("CMR returned HTTP %s %s" % (e.code, detail or e.reason))
    except urllib.error.URLError as e:
        raise RuntimeError("Could not reach CMR: %s" % e.reason)


def download(url, dest, progress=None, is_canceled=None, chunk=1024 * 1024, timeout=120):
    """Stream url to dest with the Earthdata token. Returns dest."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    if _state["token"]:
        req.add_header("Authorization", "Bearer %s" % _state["token"])
    tmp = dest + ".part"
    try:
        with _opener().open(req, timeout=timeout) as resp:
            final = resp.geturl()
            if "urs.earthdata.nasa.gov" in urllib.parse.urlparse(final).netloc:
                raise AuthError("The archive sent the request to the Earthdata Login page, which means the "
                                "token was not accepted. Sign in again, and make sure you have authorized "
                                "this DAAC's applications in your Earthdata profile.")
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as out:
                while True:
                    if is_canceled and is_canceled():
                        raise InterruptedError("Canceled")
                    block = resp.read(chunk)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
        os.replace(tmp, dest)
        return dest
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError("The archive refused the download (HTTP %s). Sign in again." % e.code)
        raise RuntimeError("Download failed with HTTP %s: %s" % (e.code, url))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
