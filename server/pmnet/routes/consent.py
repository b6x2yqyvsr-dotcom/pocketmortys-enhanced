"""Privacy-consent endpoints.

The client refuses to leave the splash screen until it has loaded the privacy
policy and terms of use, and it loads them over HTTP from the hosts baked into
the IL2CPP metadata.  Our metadata patch re-anchors those hosts on this server,
so the pages have to exist here -- otherwise every request 404s and the client
reports "must be online to view the privacy policy" with a dead Accept button.

Endpoints the client asks for (all observed in the patched metadata):

* ``GET /consent``                       -- OneTrust consent probe
* ``GET /is-gdpr``                       -- already served by ``misc.py``
* ``GET /ccpa-link-redirector/?link=X``  -- redirector the WebView follows
* ``GET /?link=X``                       -- the policy page itself
                                            (privacy / terms / faq / center)

``link`` accepts ``privacy``, ``terms``, ``faq`` and ``center``.  The pages are
deliberately self-contained: no external CSS, fonts or scripts, because the
emulator's WebView has no working network sandbox and any off-origin fetch
would hang the page and stall the consent flow again.
"""

from __future__ import annotations

from pathlib import Path

from ..http import Response, get, post

# --------------------------------------------------------------------------
# the policy pages
# --------------------------------------------------------------------------

# Titles per link target.  Anything unknown falls back to "support".
_PAGES = {
    "privacy": "隐私政策 / Privacy Policy",
    "terms": "使用条款 / Terms of Use",
    "faq": "常见问题 / FAQ",
    "center": "隐私偏好中心 / Privacy Preference Center",
}

_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; padding:20px 22px 28px;
         font-family:-apple-system,"Helvetica Neue","PingFang SC",sans-serif;
         font-size:16px; line-height:1.75; color:#1c1c1e; background:#fff; }}
  h1 {{ font-size:20px; margin:0 0 6px; }}
  .sub {{ color:#8e8e93; font-size:13px; margin-bottom:18px; }}
  h2 {{ font-size:16px; margin:20px 0 6px; }}
  p, li {{ margin:6px 0; }}
  ul {{ padding-left:20px; }}
  .note {{ margin-top:22px; padding:12px 14px; background:#f2f2f7;
           border-radius:10px; font-size:14px; color:#3a3a3c; }}
  footer {{ margin-top:26px; color:#8e8e93; font-size:12px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="sub">Pocket Mortys private server</div>

<p>本页面由本地私服提供，用于让客户端完成隐私政策确认流程。
This page is served by the private server so the client can finish its
privacy-consent flow.</p>

<h2>1. 本服务收集什么 / What this service stores</h2>
<ul>
  <li>账号标识与登录会话 / account id and login session</li>
  <li>游戏进度（莫蒂、道具、关卡状态）/ game progress</li>
  <li>客户端请求日志 / request logs, for debugging only</li>
</ul>

<h2>2. 数据存在哪 / Where the data lives</h2>
<p>全部数据保存在本服务器的 SQLite 数据库中，不发送到任何第三方。
All data stays in this server's SQLite database; nothing is sent to a third
party.</p>

<h2>3. 删除数据 / Deleting your data</h2>
<p>可通过游戏内的账号删除入口，或直接删除服务器上的数据库文件。
Use the in-game account-deletion entry point, or remove the server's database
file directly.</p>

<div class="note">
  这是一个非官方的游戏保存项目，与 Pocket Mortys 的原始发行方无关。
  This is an unofficial preservation project, unaffiliated with the original
  publisher of Pocket Mortys.
</div>

<footer>接受本页即表示同意上述条款。<br>Accepting this page means you agree to the above.</footer>
</body>
</html>
"""


def _page(title: str) -> Response:
    body = _PAGE_HTML.format(title=title).encode("utf-8")
    return Response(status=200, body=body,
                    content_type="text/html; charset=utf-8")


def _title_for(link: str) -> str:
    return _PAGES.get((link or "").strip().lower(), "支持 / Support")


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@get("/consent")
@post("/consent")
def consent(req):
    """OneTrust consent probe.

    The client calls this before showing the banner.  Answering with the same
    shape as ``/is-gdpr`` is enough to let it move on: nothing here requires
    consent, so the banner becomes purely informational.
    """
    return {
        "countryCode": "US",
        "GDPR": True,
        "CCPA": False,
        "consentRequired": False,
        "consented": True,
    }


@get("/ccpa-link-redirector/")
def ccpa_link_redirector(req):
    """Serve the policy page directly.

    The real host 302s to the OneTrust preference centre.  We render the page
    inline instead: a redirect would send the WebView to a host that does not
    resolve, which lands us right back at "must be online".
    """
    return _page(_title_for(str(req.arg("link") or "")))


_OTT_ROOT = Path(__file__).resolve().parent.parent.parent / "cdn" / "bannersdk"


@get("/")
def support_page(req):
    """Two different callers share this path.

    * with ``?link=`` -- the client opening a policy page in its WebView
    * **without** a query -- the bare root.

    The bare root used to answer with the OneTrust banner config, on the theory
    that the SDK base URL (``https://mobile-data.onetrust.io``) had been
    re-anchored here.  Request-header capture proved that guess wrong: the only
    anonymous caller is the game's own HTTP stack, which asks for ``/`` with
    nothing but a ``Host`` header and retries every few seconds.  The community
    server answers that with **403** (``RewriteRule ^ - [F,L]`` at the end of
    its ``.htaccess``), so we match it.  The OneTrust config still lives at
    ``/bannersdk/v2/applicationdata``, which is where the SDK actually asks.
    """
    link = req.arg("link")
    if not link:
        payload = _OTT_ROOT / "v2" / "applicationdata"
        if payload.is_file():
            return Response(status=200, body=payload.read_bytes(),
                            content_type="application/json; charset=utf-8")
        return Response(status=404, body=b'{"error":{"code":"NOT_FOUND"}}',
                        content_type="application/json; charset=utf-8")
    return _page(_title_for(str(link)))
