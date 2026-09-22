"""카카오톡 "나에게 보내기" — OAuth 토큰 수명 관리와 메시지 전송.

친구에게 보내기(/friends/message/)와 달리 나에게 보내기(/memo/)는 앱 심사나
별도 권한 신청이 필요 없다. talk_message 동의항목 하나면 된다.

이 파일은 tkinter 를 모른다. UI 는 stock_widget.pyw 가 담당하고, 여기서는
"토큰이 살아 있게 유지하고 글자를 보낸다"만 한다. 표준 라이브러리만 쓴다.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TOKEN_PATH = BASE_DIR / "kakao_token.json"

AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

SCOPE = "talk_message"
CALLBACK_PORT = 8321
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"

TIMEOUT = 10
REFRESH_MARGIN = 300        # 만료 5분 전이면 미리 갱신한다
TEXT_LIMIT = 200            # 기본 텍스트 템플릿의 text 최대 길이

# 폰에서 알림을 누르면 바로 호가를 볼 수 있게 한다.
STOCK_URL = "https://m.stock.naver.com/domestic/stock/{}/total"


class KakaoError(Exception):
    """카카오 호출 실패의 공통 조상."""


class TokenExpired(KakaoError):
    """재연동이 필요하다. 재시도해도 소용없다."""


class Transient(KakaoError):
    """네트워크 등 일시적 실패. 재시도할 가치가 있다."""


class Fatal(KakaoError):
    """설정이 잘못됐다. 사용자가 고쳐야 한다."""


# ---------------------------------------------------------------------------
# 토큰 파일
# ---------------------------------------------------------------------------

def load_tokens():
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def save_tokens(data):
    TOKEN_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def clear_tokens():
    try:
        TOKEN_PATH.unlink()
    except Exception:
        pass


def is_linked():
    t = load_tokens()
    return bool(t and t.get("refresh_token"))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _post(url, params, headers=None):
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                 **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _classify(e)
    except urllib.error.URLError as e:
        raise Transient(f"네트워크 오류: {e.reason}")
    except Exception as e:
        raise Transient(f"{type(e).__name__}: {e}")


def _classify(e):
    """카카오가 돌려준 본문을 읽어 재시도 가능 여부를 판정한다.

    본문에는 KOE320 같은 진단 코드가 들어 있다. 이걸 버리고 "실패"라고만
    말하면 사용자는 무엇을 고쳐야 하는지 알 방법이 없다.
    """
    try:
        detail = json.loads(e.read().decode("utf-8"))
    except Exception:
        detail = {}
    msg = detail.get("error_description") or detail.get("msg") or str(e)
    code = detail.get("error_code") or detail.get("code")
    tag = f"[{code}] " if code else ""

    if e.code in (401, 403):
        # -401 유효하지 않은 토큰, -402 동의항목 미동의
        if code == -402:
            return Fatal(f"{tag}동의항목 '카카오톡 메시지 전송'이 꺼져 있습니다. {msg}")
        return TokenExpired(f"{tag}{msg}")
    if e.code == 400:
        return Fatal(f"{tag}{msg}")
    if e.code == 429 or e.code >= 500:
        return Transient(f"{tag}{msg}")
    return Fatal(f"{tag}{msg}")


# ---------------------------------------------------------------------------
# 인가 흐름
# ---------------------------------------------------------------------------

def build_authorize_url(rest_key):
    q = urllib.parse.urlencode({
        "client_id": rest_key,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
    })
    return f"{AUTHORIZE_URL}?{q}"


def extract_code(text):
    """리다이렉트된 주소(또는 code 값 자체)에서 인가 코드를 꺼낸다.

    브라우저가 안 열리는 환경을 위한 수동 입력 경로다.
    """
    text = (text or "").strip()
    if not text:
        return None
    if "?" in text or "code=" in text:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
        if not q and "code=" in text:               # 쿼리만 붙여넣은 경우
            q = urllib.parse.parse_qs(text.split("?", 1)[-1])
        vals = q.get("code")
        return vals[0] if vals else None
    return text


def exchange_code(rest_key, client_secret, code):
    """인가 코드를 토큰으로 바꾸고 저장한다."""
    params = {
        "grant_type": "authorization_code",
        "client_id": rest_key,
        "redirect_uri": REDIRECT_URI,
        "code": code,
    }
    if client_secret:
        params["client_secret"] = client_secret

    data = _post(TOKEN_URL, params)
    if not data.get("access_token"):
        raise Fatal("토큰 응답에 access_token 이 없습니다.")

    tokens = {
        "rest_key": rest_key,
        "client_secret": client_secret or "",
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": time.time() + float(data.get("expires_in", 0) or 0),
        "linked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_tokens(tokens)
    return tokens


def refresh(tokens):
    """액세스 토큰을 갱신한다.

    응답에 refresh_token 이 없는 것이 정상이다. 카카오는 리프레시 토큰의 잔여
    유효기간이 1개월 미만일 때만 새로 내려준다. 이때 없다고 기존 것을 지우면
    두 달 뒤 알림이 조용히 멎는다.
    """
    if not tokens.get("refresh_token"):
        raise TokenExpired("리프레시 토큰이 없습니다.")

    params = {
        "grant_type": "refresh_token",
        "client_id": tokens.get("rest_key", ""),
        "refresh_token": tokens["refresh_token"],
    }
    if tokens.get("client_secret"):
        params["client_secret"] = tokens["client_secret"]

    data = _post(TOKEN_URL, params)
    if not data.get("access_token"):
        raise TokenExpired("갱신 응답에 access_token 이 없습니다.")

    tokens["access_token"] = data["access_token"]
    tokens["expires_at"] = time.time() + float(data.get("expires_in", 0) or 0)
    if data.get("refresh_token"):
        tokens["refresh_token"] = data["refresh_token"]
    tokens["refreshed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_tokens(tokens)
    return tokens


# ---------------------------------------------------------------------------
# 전송
# ---------------------------------------------------------------------------

def send_text(text, code=None):
    """나에게 텍스트 메시지를 보낸다. code 를 주면 종목 페이지 링크를 단다."""
    tokens = load_tokens()
    if not tokens:
        raise TokenExpired("카카오톡이 연동되어 있지 않습니다.")

    if time.time() >= float(tokens.get("expires_at", 0)) - REFRESH_MARGIN:
        tokens = refresh(tokens)

    link_url = STOCK_URL.format(code) if code else "https://m.stock.naver.com/"
    template = {
        "object_type": "text",
        "text": text[:TEXT_LIMIT],
        "link": {"web_url": link_url, "mobile_web_url": link_url},
    }

    def call(tok):
        return _post(SEND_URL,
                     {"template_object": json.dumps(template, ensure_ascii=False)},
                     headers={"Authorization": f"Bearer {tok['access_token']}"})

    try:
        data = call(tokens)
    except TokenExpired:
        # 만료 시각을 믿을 수 없는 경우(수동 편집·서버측 무효화)가 있다.
        # 한 번은 갱신하고 다시 시도한다.
        tokens = refresh(tokens)
        data = call(tokens)

    if data.get("result_code") not in (0, None):
        raise Fatal(f"전송 실패 result_code={data.get('result_code')}")
    return True
