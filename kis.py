"""한국투자증권(KIS) Open API — 국내주식 현재가 조회.

네이버 폴링 API 를 대신하는 공식 경로다. 지연 없는 시세라는 점은 네이버와 같고,
다른 것은 **한도가 공개돼 있다는 것**이다(실전 초당 20건). 차단이 가까워졌는지
알 수 없어 빈 요청을 줄이려 애쓸 이유가 없으므로, 여기서는 적응형 주기를 쓰지
않고 사용자가 고른 주기를 그대로 쓴다(stock_widget.Poller 참고).

이 파일은 tkinter 를 모른다. kakao.py 와 같은 규칙이다 — UI 는 stock_widget.pyw
가 담당하고 여기서는 "토큰이 살아 있게 유지하고 시세를 가져온다"만 한다.
표준 라이브러리만 쓴다.

토큰에 관해 반드시 알아야 할 것:

  * 유효기간 1일이고, **발급할 때마다 사용자 휴대폰으로 알림톡이 간다.**
    그래서 파일에 반드시 캐시한다. 위젯을 하루에 열 번 켜도 발급은 한 번이다.
  * 재발급은 1분에 1회로 막혀 있다(EGW00133). 실패했다고 곧바로 다시 부르면
    그 제한에 걸려 더 오래 막힌다. _MIN_ISSUE_GAP 으로 스스로 간격을 지킨다.
  * 6시간 이내에 다시 신청하면 서버가 같은 토큰을 돌려준다. 즉 재발급은 싸지
    않지만 위험하지도 않다 — 다만 알림톡이 가므로 사용자가 놀란다.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TOKEN_PATH = BASE_DIR / "kis_token.json"

# 앱키를 적어 두는 파일. 사용자가 메모장으로 고치는 파일이라 config.json 과 같은
# 규칙을 따른다 -- 못 읽는 값은 예외를 던지지 말고 없는 것으로 친다.
#
# .env 가 앱키의 정답이다. 여기에 유효한 값이 있으면 kis_token.json 에 저장된
# 앱키보다 우선한다. 두 곳을 다 볼 수 있게 두면 "고쳤는데 왜 안 바뀌지"가 된다.
ENV_PATH = BASE_DIR / ".env"
ENV_KEY = "KIS_APP_KEY"
ENV_SECRET = "KIS_APP_SECRET"

# 파일이 없을 때 위젯이 만들어 주는 내용. 안내문을 한글로 둔 것은 의도다 --
# _is_placeholder 가 한글이 남아 있으면 "아직 안 적었다"로 보고 연동하지 않는다.
ENV_TEMPLATE = """\
# 한국투자증권(KIS) Open API 앱키
#
# apiportal.koreainvestment.com -> My App 에서 발급받은 값을 = 뒤에 붙여넣으세요.
# 아래 안내문(한글)을 지우고 그 자리에 넣으면 됩니다.
# 저장하면 위젯이 다음 갱신 때 알아서 읽습니다. 다시 켤 필요 없습니다.
#
# 실전투자 계좌용 앱키여야 합니다. 모의투자 키는 동작하지 않습니다.
#
# !! 이 파일은 비밀번호나 다름없습니다. 남에게 보내지 마세요. !!

KIS_APP_KEY=여기에앱키를붙여넣으세요
KIS_APP_SECRET=여기에앱시크릿을붙여넣으세요
"""

# 실전투자 도메인. 모의투자(openapivts...:29443)는 지원하지 않는다 — 모의는
# 시세가 실전과 다르게 나오는 구간이 있어 "바탕화면에서 보는 진짜 시세"라는
# 이 위젯의 전제와 맞지 않는다.
BASE_URL = "https://openapi.koreainvestment.com:9443"

TOKEN_URL = "/oauth2/tokenP"
PRICE_URL = "/uapi/domestic-stock/v1/quotations/inquire-price"
HOLIDAY_URL = "/uapi/domestic-stock/v1/quotations/chk-holiday"

TR_PRICE = "FHKST01010100"      # 주식현재가 시세
TR_HOLIDAY = "CTCA0903R"        # 국내휴장일조회

TIMEOUT = 5
REFRESH_MARGIN = 600            # 만료 10분 전이면 미리 받는다
_MIN_ISSUE_GAP = 70.0           # 토큰 재발급 최소 간격 (서버 제한 1분 + 여유)
_MIN_CALL_GAP = 0.06            # 실전 초당 20건. 종목이 여러 개여도 넘지 않게
RATE_RETRY_WAIT = 0.35          # EGW00201 을 만났을 때 쉬는 시간

# 정규장. 네이버 marketStatus 가 OPEN 인 구간과 맞춰야 판정 엔진이 소스에 따라
# 다르게 굴지 않는다.
OPEN_HM = (9, 0)
CLOSE_HM = (15, 30)

# 애프터마켓. 2026-09-14 부터 KRX 전 종목 대상으로 신설됐다(종전 시간외단일가
# 16:00~18:00 을 20:00 까지 늘리고, 10분 단위 단일가에서 접속매매로 바꾼 것).
# **접속매매라 정규장처럼 가격이 계속 움직인다.** 이 구간을 그냥 "장 마감"으로
# 두면 위젯이 60초에 한 번만 깨어나 낡은 값을 보여준다.
#
# 그래도 정규장과 같게 취급하지는 않는다:
#   * 애프터마켓 종가는 공식 종가가 아니다
#   * ETF·ETN 은 애프터마켓에서 거래되지 않는다
#   * 15:30~16:00 은 애프터마켓이 아니다. 15:40~16:00 의 시간외종가 매매는
#     가격이 종가에 고정돼 있어 거래량만 늘고 값은 움직이지 않는다
AFTER_OPEN_HM = (16, 0)
AFTER_CLOSE_HM = (20, 0)


class KisError(Exception):
    """KIS 호출 실패의 공통 조상."""


class TokenExpired(KisError):
    """재연동이 필요하다. 재시도해도 소용없다."""


class Transient(KisError):
    """네트워크·한도 등 일시적 실패. 재시도할 가치가 있다."""


class Fatal(KisError):
    """앱키가 틀렸다거나 하는, 사용자가 고쳐야 하는 문제."""


# ---------------------------------------------------------------------------
# 토큰 파일 -- kakao_token.json 과 같은 취급의 자격증명이다
# ---------------------------------------------------------------------------

_cache = {"key": None, "data": None}


def load_tokens():
    """저장된 토큰. 파일이 그대로면 다시 파싱하지 않는다.

    장중 1초 주기면 이 함수가 초당 한 번씩 불린다. 작은 파일이지만 매번 열어
    파싱할 이유는 없다. 파일이 바뀌면(mtime) 그때만 다시 읽는다.
    """
    try:
        st = TOKEN_PATH.stat()
    except OSError:
        _cache["key"] = _cache["data"] = None
        return None
    key = (str(TOKEN_PATH), st.st_mtime_ns, st.st_size)
    if _cache["key"] == key and _cache["data"] is not None:
        return dict(_cache["data"])
    try:
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    _cache["key"], _cache["data"] = key, data
    return dict(data)


# ---------------------------------------------------------------------------
# .env -- 앱키를 손으로 적어 넣는 경로
# ---------------------------------------------------------------------------

def parse_env(text):
    """KEY=VALUE 를 읽는다. 주석(#)·빈 줄·따옴표·export 접두사를 받아넘긴다.

    python-dotenv 는 pip 대상이라 쓰지 않는다. 우리에게 필요한 것은 두 줄이다.
    """
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def _is_placeholder(v):
    """템플릿을 그대로 둔 것인가.

    앱키·앱시크릿은 영숫자다. 한글이 섞여 있으면 안내문을 지우지 않은 것이므로
    "적어 넣었다"고 보면 안 된다 -- 그대로 발급을 시도하면 사용자는 영문도 모를
    오류만 보게 된다.
    """
    v = (v or "").strip()
    return (not v) or v.startswith("<") or any(ord(c) > 127 for c in v)


def env_keys():
    """.env 에 적힌 (앱키, 앱시크릿). 없거나 템플릿 그대로면 None."""
    try:
        data = parse_env(ENV_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    k, s = data.get(ENV_KEY, ""), data.get(ENV_SECRET, "")
    if _is_placeholder(k) or _is_placeholder(s):
        return None
    return k.strip(), s.strip()


def save_tokens(data):
    """원자적으로 쓴다.

    토큰 갱신은 폴링 스레드에서 일어나는데, 그 사이 메인 스레드가 메뉴를 그리며
    같은 파일을 읽는다. 반쯤 쓰인 파일을 읽으면 is_linked() 가 잠깐 False 가 되어
    "연동 안 됨"으로 보이고, 그 순환은 네이버로 떨어진다.
    """
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(TOKEN_PATH)


def clear_tokens():
    _cache["key"] = _cache["data"] = None
    try:
        TOKEN_PATH.unlink()
    except Exception:
        pass


def is_linked():
    """앱키를 어디서든 구할 수 있는가. .env 가 먼저다."""
    if env_keys():
        return True
    t = load_tokens()
    return bool(t and t.get("appkey") and t.get("appsecret"))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_call_lock = threading.Lock()
_last_call = 0.0
_last_issue = 0.0


def _space_calls():
    """초당 건수 한도를 스스로 지킨다.

    폴링 스레드 하나가 순차로 부르므로 경쟁은 없지만, 종목이 여러 개면 한 순환
    안에서 연달아 나간다. 그 묶음이 한도를 넘지 않게 최소 간격만 둔다.
    """
    global _last_call
    with _call_lock:
        gap = time.monotonic() - _last_call
        if gap < _MIN_CALL_GAP:
            time.sleep(_MIN_CALL_GAP - gap)
        _last_call = time.monotonic()


def _read_error(e):
    try:
        return json.loads(e.read().decode("utf-8"))
    except Exception:
        return {}


def _classify(status, body, where):
    """KIS 가 돌려준 msg_cd 를 살려서 무엇을 고쳐야 하는지 말할 수 있게 한다."""
    code = str(body.get("msg_cd") or body.get("error_code") or "").strip()
    msg = (body.get("msg1") or body.get("error_description")
           or body.get("msg") or f"HTTP {status}").strip()
    tag = f"[{code}] " if code else ""

    if code in ("EGW00121", "EGW00123"):        # 유효하지 않은/만료된 token
        return TokenExpired(f"{tag}{msg}")
    if code == "EGW00133":                      # 접근토큰 발급 잠시 후 다시
        return Transient(f"{tag}{msg}")
    if code == "EGW00201":                      # 초당 거래건수 초과
        return Transient(f"{tag}{msg}")
    if status in (401, 403):
        return Fatal(f"{tag}앱키 또는 앱시크릿이 올바르지 않습니다. {msg}")
    if status == 429 or status >= 500:
        # KIS 는 토큰 만료도 500 으로 주는 일이 있다. 위에서 msg_cd 로 이미
        # 걸렀으니 여기 남은 500 은 진짜 서버 문제로 본다.
        return Transient(f"{tag}{msg}")
    return Fatal(f"{tag}{where}: {msg}")


def _post_json(path, payload, headers=None):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path, data=body, method="POST",
        headers={"content-type": "application/json; charset=utf-8",
                 **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _classify(e.code, _read_error(e), path)
    except urllib.error.URLError as e:
        raise Transient(f"네트워크 오류: {e.reason}")
    except Exception as e:
        raise Transient(f"{type(e).__name__}: {e}")


def _get(path, params, headers):
    url = BASE_URL + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _classify(e.code, _read_error(e), path)
    except urllib.error.URLError as e:
        raise Transient(f"네트워크 오류: {e.reason}")
    except Exception as e:
        raise Transient(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 토큰
# ---------------------------------------------------------------------------

def issue_token(appkey, appsecret):
    """접근토큰을 새로 발급받아 저장한다. 사용자 폰으로 알림톡이 간다."""
    global _last_issue
    gap = time.monotonic() - _last_issue
    if _last_issue and gap < _MIN_ISSUE_GAP:
        raise Transient(f"토큰 재발급은 1분에 한 번입니다. "
                        f"{int(_MIN_ISSUE_GAP - gap)}초 뒤에 다시 시도합니다.")
    _last_issue = time.monotonic()

    data = _post_json(TOKEN_URL, {"grant_type": "client_credentials",
                                  "appkey": appkey, "appsecret": appsecret})
    token = data.get("access_token")
    if not token:
        raise Fatal("토큰 응답에 access_token 이 없습니다.")

    old = load_tokens() or {}
    tokens = {
        "appkey": appkey,
        "appsecret": appsecret,
        "access_token": token,
        # expires_in 은 초. 서버가 주는 access_token_token_expired 는 문자열
        # 일시라 시계가 틀어진 PC 에서 어긋난다. 상대 시간으로 센다.
        "expires_at": time.time() + float(data.get("expires_in", 0) or 0),
        "expires_text": data.get("access_token_token_expired", ""),
        "linked_at": old.get("linked_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "issued_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_tokens(tokens)
    return tokens


def get_token(force=False):
    """살아 있는 접근토큰. 없거나 만료가 가까우면 새로 받는다.

    .env 의 앱키가 저장된 것과 다르면 사용자가 방금 고쳐 넣은 것이다. 그쪽을
    정답으로 보고 토큰을 다시 받는다 -- 고쳤는데 옛 키로 계속 도는 것이 최악이다.
    """
    tokens = load_tokens() or {}
    env = env_keys()
    if env and (tokens.get("appkey"), tokens.get("appsecret")) != env:
        return issue_token(*env)
    if not tokens.get("appkey"):
        raise TokenExpired("한국투자증권이 연동되어 있지 않습니다.")
    if (not force and tokens.get("access_token")
            and time.time() < float(tokens.get("expires_at", 0)) - REFRESH_MARGIN):
        return tokens
    return issue_token(tokens["appkey"], tokens["appsecret"])


def link(appkey, appsecret):
    """앱키를 저장하고 곧바로 발급까지 해 본다. 틀린 키를 조용히 받아두지 않는다."""
    appkey = (appkey or "").strip()
    appsecret = (appsecret or "").strip()
    if not appkey or not appsecret:
        raise Fatal("앱키와 앱시크릿을 모두 입력하세요.")
    global _last_issue
    _last_issue = 0.0                   # 사용자가 직접 누른 것이니 간격 제한 면제
    return issue_token(appkey, appsecret)


def _authed_get(path, tr_id, params):
    """토큰을 붙여 부른다. 만료면 다시 받고, 유량 초과면 쉬었다 한 번 더 건다."""
    def once(tokens):
        _space_calls()
        data = _get(path, params, {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {tokens['access_token']}",
            "appkey": tokens["appkey"],
            "appsecret": tokens["appsecret"],
            "tr_id": tr_id,
            "custtype": "P",            # 개인
        })
        # 유량 초과는 HTTP 200 에 rt_cd 로 실려 온다. 재시도 판정이 이 검사
        # 뒤에 있으면 그 경우를 못 잡으므로 여기서 함께 본다.
        if str(data.get("rt_cd", "0")) != "0":
            raise _classify(200, data, path)
        return data

    tokens = get_token()
    try:
        return once(tokens)
    except TokenExpired:
        # 만료 시각을 믿을 수 없는 경우(수동 편집·서버측 무효화)가 있다.
        return once(get_token(force=True))
    except Transient as e:
        # EGW00201(초당 거래건수 초과)은 실측상 호출 간격과 상관없이 드문드문
        # 난다 -- 0.05초 간격 5회는 멀쩡한데 0.5초 간격에서 나기도 했다. 고정
        # 간격으로는 피할 수 없으니 한 번 쉬었다 다시 건다. 그래도 안 되면
        # 그 순환만 네이버로 넘어간다.
        if "EGW00201" not in str(e):
            raise
        time.sleep(RATE_RETRY_WAIT)
        return once(tokens)


# ---------------------------------------------------------------------------
# 시세
# ---------------------------------------------------------------------------

def fetch_price(code):
    """주식현재가 시세. output 딕셔너리를 그대로 돌려준다.

    한글 종목명은 응답에 없다(bstp_kor_isnm 은 업종명이다). 이름은 부르는 쪽에서
    KRX 캐시(stocks.json)로 붙인다.
    """
    data = _authed_get(PRICE_URL, TR_PRICE, {
        "FID_COND_MRKT_DIV_CODE": "J",      # J: KRX
        "FID_INPUT_ISCD": code,
    })
    out = data.get("output")
    if not isinstance(out, dict) or not out.get("stck_prpr"):
        raise Transient(f"{code}: 빈 응답")
    return out


# ---------------------------------------------------------------------------
# 장 개장 여부
#
# inquire-price 응답에는 네이버의 marketStatus 같은 필드가 없다. 그래서 시계로
# 판정하되, 휴장일은 공식 API 로 하루 한 번만 확인해 기억한다. 확인에 실패하면
# 평일 여부로 물러난다 -- 휴장일에 잘못 "개장"으로 보아도 값이 고정이라 알림이
# 잘못 나가지는 않는다. 반대로 개장일을 "휴장"으로 보면 알림이 통째로 멎으므로,
# 애매하면 열린 쪽으로 판단한다.
# ---------------------------------------------------------------------------

_open_days = {}         # "YYYYMMDD" -> bool


def is_open_day(day=None):
    """그 날이 개장일인가. 확인 불가면 None."""
    day = day or time.strftime("%Y%m%d")
    if day in _open_days:
        return _open_days[day]
    try:
        data = _authed_get(HOLIDAY_URL, TR_HOLIDAY, {
            "BASS_DT": day, "CTX_AREA_FK": "", "CTX_AREA_NK": "",
        })
    except Exception:
        # 개장 여부 하나 때문에 시세가 통째로 막히면 안 된다. 모르면 모르는 대로
        # 돌려주고, 부르는 쪽이 "열린 쪽"으로 판단한다.
        return None
    rows = data.get("output")
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows or []:
        if str(row.get("bass_dt", "")).strip() == day:
            opened = str(row.get("opnd_yn", "")).strip().upper() == "Y"
            _open_days[day] = opened
            return opened
    return None


def session_now(now=None):
    """지금이 어느 장인가. "regular" | "between" | "after" | "closed".

    PC 시계를 한국 시각으로 본다. 개장일 확인은 시간대 안에 들어왔을 때만 한다 --
    새벽에 켜 두었다고 휴장일 API 를 매번 부를 이유가 없다.
    """
    t = time.localtime(now) if now is not None else time.localtime()
    if t.tm_wday >= 5:                      # 토·일
        return "closed"
    hm = (t.tm_hour, t.tm_min)
    if OPEN_HM <= hm < CLOSE_HM:
        where = "regular"
    elif CLOSE_HM <= hm < AFTER_OPEN_HM:
        where = "between"
    elif AFTER_OPEN_HM <= hm < AFTER_CLOSE_HM:
        where = "after"
    else:
        return "closed"
    opened = is_open_day(time.strftime("%Y%m%d", t))
    # 확인이 안 되면 열린 쪽으로 본다. 휴장일을 장중으로 잘못 보면 값이 고정이라
    # 알림이 잘못 나가지 않지만, 반대로 보면 알림이 통째로 멎는다.
    return where if opened is not False else "closed"


def market_open_now(now=None):
    """오늘 거래가 아직 안 끝났는가. **네이버 marketStatus 와 같은 뜻이다.**

    정규장(~15:30)만 True 로 두면 안 된다. 실측(2026-09-15)에서 네이버는 15:30 이
    지나도 20:00 까지 계속 OPEN 을 준다. 두 소스가 같은 뜻을 내야 한다는 quote
    계약이 깨지면, 시세 출처가 바뀌는 것만으로 알림이 켜졌다 꺼졌다 한다.

    애프터마켓에 값이 실제로 움직이는 것도 같은 날 실측했다 -- 16:01~16:08 에
    6,930 → 7,100 → 7,000 으로 2.5% 를 오갔다. 그 구간을 "마감"으로 보면 목표가
    알림이 조용히 안 나간다.
    """
    return session_now(now) != "closed"
