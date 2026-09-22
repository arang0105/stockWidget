"""바탕화면 주식 시세 위젯.

프레임 없는 반투명 창에 한국 주식 시세를 표시한다. 외부 의존성 없음(표준 라이브러리만).

  실행:  stock_widget.pyw
  복구:  stock_widget.pyw --reset            (창을 화면 밖에서 잃어버렸을 때)
  점검:  stock_widget.pyw --selftest-alert   (알림 판정 엔진 검증)
"""

import ctypes
import html
import json
import os
import re
import shutil
import sys
import threading
import time
import queue
import webbrowser
from collections import deque
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

# 바탕화면 폴더에 __pycache__ 가 생기지 않게 한다. kakao.py 하나를 다시 컴파일하는
# 비용은 무시할 만하고, 이 폴더는 사용자가 직접 들여다보는 곳이다.
sys.dont_write_bytecode = True

try:
    import kakao
except Exception:                    # kakao.py 가 없거나 깨졌어도 위젯은 떠야 한다
    kakao = None

try:
    import kis
except Exception:                    # kis.py 가 없으면 네이버로 돈다. 같은 규칙이다
    kis = None

# ---------------------------------------------------------------------------
# 0. DPI 인식 선언 -- 반드시 Tk() 를 만들기 전에, 즉 이 위치에서 호출해야 한다.
#    레벨 1(시스템 DPI 인식)을 쓴다. 퍼모니터 v2 는 tkinter 8.6 이 WM_DPICHANGED 를
#    처리하지 못해 배율이 다른 모니터로 드래그하면 위젯 크기가 깨진다.
# ---------------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import tkinter as tk  # noqa: E402  (DPI 선언 뒤에 임포트)
from tkinter import messagebox  # noqa: E402


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STOCKS_PATH = BASE_DIR / "stocks.json"
ALERT_STATE_PATH = BASE_DIR / "alert_state.json"
ALERTS_LOG_PATH = BASE_DIR / "alerts.log"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

NAVER_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{}"
# range=1d 여야 previousClose 가 "전일 종가"가 된다. 5d 로 하면 chartPreviousClose
# 가 5일 전 종가라서 등락률이 엉뚱하게 나온다(실측: 4.79% 대신 16.05%).
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}.{}?interval=1d&range=1d"
KRX_URL = ("https://kind.krx.co.kr/corpgeneral/corpList.do"
           "?method=download&searchType=13")

# 종목코드는 6자리지만 숫자만은 아니다. 최근 상장분에는 0039P0, 0218L0 같은
# 영숫자 혼합 코드가 있고 시세 조회도 정상 동작한다.
CODE_RE = re.compile(r"[0-9A-Z]{6}")

# 절전 복귀 시 무한 대기를 막는다. 폴링은 "받고 → 기다리고"의 순차 구조라 주기를
# 이보다 짧게 잡아도 요청이 겹치지 않는다. 느린 응답 한 번이 그 순환만 늦출 뿐이다.
FETCH_TIMEOUT = 5
KRX_TIMEOUT = 30            # 1.2MB 다운로드. KRX 는 느릴 때 15초를 넘긴다.
# 캐시 신선도의 기준선. 신규 상장은 개장 시각에 맞춰 KRX 목록에 오르므로,
# "오늘 개장 이후에 받은 것"만 신선으로 본다(_stocks_fresh). 경과 시간으로
# 재면 어제 낮에 받은 캐시가 오늘 같은 시각까지 신선으로 남아, 오늘 상장한
# 종목이 장 중 내내 검색되지 않는다(실측: 어제 12:24 캐시 → 오늘 12:24까지).
STOCKS_FRESH_HOUR = 9
# 위젯을 며칠씩 켜 둬도 날이 바뀌면 반영되도록, 켜져 있는 동안에도 이 간격마다
# 캐시 나이를 다시 본다. 아직 신선하면 load_stock_list 가 파일만 읽고 끝난다.
# 개장 직후에 반영되려면 이 간격이 그보다 짧아야 한다 -- 6시간이면 09:00 에
# 상장한 종목이 낮까지 안 뜬다. 실제 다운로드는 여전히 하루 한 번뿐이다.
STOCKS_RECHECK = 3600

INTERVAL_OPEN = 7.0
INTERVAL_CLOSED = 60.0
# 15:30~16:00. 정규장은 끝났고 애프터마켓(16:00~)은 아직이다. 이 사이의 시간외
# 종가 매매는 가격이 종가에 고정돼 있어 거래량만 늘고 값은 움직이지 않는다.
# 네이버가 이 구간에도 OPEN 을 주므로 "마감"으로 접지는 못하지만, 1초로 물어볼
# 이유도 없다.
INTERVAL_IDLE = 10.0
STALE_FACTOR = 3            # 마지막 성공 이후 주기의 3배가 지나면 죽은 값으로 본다
# 다만 바닥은 둔다. 주기를 1초로 줄이면 3초만 늦어도 회색이 되는데, 왕복 시간이
# 잠깐 튀는 것만으로 그렇게 되면 표시가 쉴 새 없이 깜빡인다.
STALE_MIN = 20.0

# 사용자가 고르는 장중 갱신 주기. None = 서버 권장값(7초) 고정.
#
# 값을 고르면 그 값은 '고정 주기'가 아니라 **체결이 실제로 일어날 때의 주기(하한)**
# 다. 체결이 멎으면 ADAPT_STEP 씩 늘어나 서버 권장값까지 물러난다(_next_interval).
#
# 고정 주기로 쓰지 않는 이유는 실측 때문이다. 1초 간격으로 20회를 받아 보면
# localTradedAt 은 매번 바뀌지만(요청 시각을 따라올 뿐이다) 누적 거래량이 실제로
# 늘어난 것은 6회뿐이었다. 나머지 14회는 같은 값을 다시 받은 빈 요청이다. 종목의
# 체결 빈도가 상한이지 폴링 주기가 상한이 아니다.
#
# 그러면서 요청 수는 그대로 늘어난다. 1초 고정은 장중 하루 약 23,000회이고
# 7초면 3,300회다. 네이버는 레이트리밋을 공개하지 않고 응답에 한도 헤더도 없어서
# (X-RateLimit-*, Retry-After 모두 없다) 차단이 가까워졌는지 알 방법이 없다.
# ETag 도 Last-Modified 도 없어 조건부 요청으로 부하를 줄일 수도 없고,
# Connection: close 라 연결 재사용도 서버가 거부한다. 빈 요청을 줄이는 것 말고는
# 깎을 여지가 없다는 뜻이다.
INTERVAL_MIN = 1.0
INTERVAL_CHOICES = (None, 5.0, 3.0, 2.0, 1.0)
ADAPT_STEP = 1.0            # 체결이 없는 순환마다 주기를 이만큼 늘린다

COLOR_BG = "#101010"
COLOR_UP = "#e74c3c"
COLOR_DOWN = "#3b7ddd"
COLOR_FLAT = "#aaaaaa"
COLOR_STALE = "#555555"
FONT_FAMILY = "Malgun Gothic"

MIN_VISIBLE_W = 60          # 드래그해도 항상 화면에 남겨둘 최소 폭
MIN_VISIBLE_H = 24

# 알림 --------------------------------------------------------------------
BURST_WINDOW_MIN = 60       # 관측창 하한(초). 이보다 짧으면 호가 진동을 급변동으로 본다
BURST_WINDOW_MAX = 3600
BURST_PCT_MIN = 0.1
BURST_PCT_MAX = 30.0
BURST_GAP_RESET = 60        # 샘플이 이만큼 끊기면 링버퍼를 비운다(오탐 차단)
LIMIT_CONFIRM = 2           # 상·하한가 전이는 연속 이만큼 관측돼야 인정한다
TOAST_MS = 8000             # 화면 팝업이 저절로 사라지기까지
TOAST_MAX = 4               # 동시에 쌓아둘 팝업 수
RECENT_MAX = 5              # 우클릭 → 최근 알림 에 남길 건수
SEND_RETRY = 3
SEND_RETRY_WAIT = 10.0

DEFAULT_CONFIG = {
    "codes": ["005930"],
    "x": None,              # None 이면 주 모니터 우상단에 자동 배치
    "y": None,
    "alpha": 0.85,
    "font_size": 11,
    "topmost": True,
    "show": {"name": True, "price": True, "ratio": True, "profit": True},
    "interval_open": None,  # None 이면 서버 권장 주기를 따른다
    # code -> {"above": 숫자|None, "below": 숫자|None, "burst": bool, "limit": bool}
    "alerts": {},
    "burst": {"window_sec": 300, "threshold_pct": 2.0},
    # code -> {"cost": 매수 단가, "qty": 수량|None}
    "holdings": {},
    "hidden_codes": [],
}


# ---------------------------------------------------------------------------
# 화면 좌표 -- tkinter 는 주 모니터만 본다. 다중 모니터에서는 winfo_screenwidth()
# 로 판정하면 보조 모니터에 놓인 창이 매번 "화면 밖"으로 오판된다. 또 보조
# 모니터가 왼쪽에 있으면 정상 좌표가 음수이므로 "음수면 화면 밖" 검사도 틀린다.
# 반드시 가상 화면 사각형과의 교집합으로 판정한다.
# ---------------------------------------------------------------------------

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
HWND_TOP = 0                                   # SetWindowPos 의 hWndInsertAfter
SWP_RESTACK = 0x0001 | 0x0002 | 0x0010         # NOSIZE | NOMOVE | NOACTIVATE


def virtual_screen_rect():
    """전체 모니터를 감싸는 사각형 (x, y, w, h). 좌표는 음수일 수 있다."""
    try:
        u = ctypes.windll.user32
        x = u.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = u.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = u.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = u.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if w > 0 and h > 0:
            return x, y, w, h
    except Exception:
        pass
    u = ctypes.windll.user32
    return 0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def screen_dpi():
    try:
        u = ctypes.windll.user32
        dc = u.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
        u.ReleaseDC(0, dc)
        return dpi or 96
    except Exception:
        return 96


def clamp_position(x, y, w, h, min_w=MIN_VISIBLE_W, min_h=MIN_VISIBLE_H):
    """창이 통째로 사라지지 않도록 좌표를 제한한다.

    완전 봉쇄가 아니라 최소 가시 영역만 보장한다 -- 가장자리에 반쯤 걸친
    배치는 허용하되, 화면 밖으로 완전히 나가는 것만 막는다.
    """
    vx, vy, vw, vh = virtual_screen_rect()
    min_w = min(min_w, w) if w > 0 else min_w
    min_h = min(min_h, h) if h > 0 else min_h
    x = max(vx - (w - min_w), min(x, vx + vw - min_w))
    y = max(vy - (h - min_h), min(y, vy + vh - min_h))
    return int(x), int(y)


def is_on_screen(x, y, w, h):
    """clamp 가 좌표를 움직인다면 그 위치는 화면 밖이라는 뜻."""
    return (x, y) == clamp_position(x, y, w, h)


def fit_on_screen(x, y, w, h):
    """가능하면 창 '전체'가 화면 안에 들어오도록 민다.

    clamp_position 은 드래그용이라 일부만 보이면 통과시킨다. 반면 내용이 길어져
    창이 커지는 경우(시세가 도착해 폭이 늘어날 때)는 오른쪽으로 삐져나가므로,
    그때는 통째로 들어오도록 되밀어야 한다.
    """
    vx, vy, vw, vh = virtual_screen_rect()
    if w <= vw:
        x = max(vx, min(x, vx + vw - w))
    if h <= vh:
        y = max(vy, min(y, vy + vh - h))
    return clamp_position(x, y, w, h)


def restack_topmost(win):
    """topmost 창을 z-order 맨 위로 되올린다.

    `-topmost` 는 한 번 걸면 끝이 아니다. 작업표시줄도 topmost 라, 위젯을 그 위에
    겹쳐 두면 앱 전환처럼 작업표시줄이 올라오는 순간부터 위젯이 그 밑에 깔린다.
    이 창은 WS_EX_TOOLWINDOW 라 작업표시줄에도 Alt+Tab 에도 없어서 사용자가 되찾을
    방법이 없다 -- 창이 사라진 것으로 보인다. `_check_offscreen` 은 이걸 못 잡는다.
    좌표는 멀쩡히 화면 안이고, 가려졌다는 것은 좌표로 알 수 없기 때문이다.

    HWND_TOPMOST(-1) 로는 안 된다. 이미 topmost 인 창에는 z-order 를 건드리지
    않아 아무 일도 일어나지 않는다. NOTOPMOST 로 내렸다 올리는 것도 마찬가지였고,
    실측으로 HWND_TOP 만 효과가 있었다.

    핸들은 `wm_frame()` 이어야 한다. `winfo_id()` 는 TkChild 라 최상위 창이 아니고,
    거기 걸면 SetWindowPos 가 0(실패)을 돌려주며 조용히 아무 일도 안 한다.
    """
    try:
        hwnd = int(win.wm_frame(), 16)
        ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                                          SWP_RESTACK)
    except Exception:
        pass


def default_position(w, h):
    """주 모니터 우상단."""
    u = ctypes.windll.user32
    return max(0, u.GetSystemMetrics(0) - w - 24), 40


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

def _merge_defaults(cfg):
    out = json.loads(json.dumps(DEFAULT_CONFIG))
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if k == "show" and isinstance(v, dict):
                out["show"].update({kk: bool(vv) for kk, vv in v.items()
                                    if kk in out["show"]})
            elif k in out:
                out[k] = v
    if not any(out["show"].values()):        # 전부 꺼진 설정은 복구
        out["show"]["price"] = True
    if not isinstance(out.get("codes"), list):
        out["codes"] = list(DEFAULT_CONFIG["codes"])
    out["codes"] = [str(c).upper() for c in out["codes"]
                    if CODE_RE.fullmatch(str(c).upper())]
    if not isinstance(out.get("hidden_codes"), list):
        out["hidden_codes"] = []
    out["alerts"] = _clean_alerts(out.get("alerts"))
    out["burst"] = _clean_burst(out.get("burst"))
    out["interval_open"] = _clean_interval(out.get("interval_open"))
    out["holdings"] = _clean_holdings(out.get("holdings"))
    return out


def _clean_interval(raw):
    """None(서버 권장) 또는 INTERVAL_MIN 이상의 초. 못 읽는 값은 None 으로."""
    v = _to_float(raw)
    if v is None:
        return None
    return round(min(max(v, INTERVAL_MIN), INTERVAL_CLOSED), 2)


def _clean_alerts(raw):
    """알림 설정을 훑어 값의 형태를 강제한다.

    사용자가 config.json 을 직접 고칠 수 있으므로(README 가 그렇게 안내한다)
    "above": "오만원" 같은 값이 들어와도 위젯이 죽으면 안 된다. 못 읽는 값은
    조용히 버리고 나머지를 살린다.
    """
    out = {}
    if not isinstance(raw, dict):
        return out
    for code, spec in raw.items():
        code = str(code).upper()
        if not CODE_RE.fullmatch(code) or not isinstance(spec, dict):
            continue
        item = {"above": _to_float(spec.get("above")),
                "below": _to_float(spec.get("below")),
                "burst": bool(spec.get("burst", False)),
                "limit": bool(spec.get("limit", False))}
        if item["above"] is not None and item["above"] <= 0:
            item["above"] = None
        if item["below"] is not None and item["below"] <= 0:
            item["below"] = None
        if not any((item["above"] is not None, item["below"] is not None,
                    item["burst"], item["limit"])):
            continue                       # 아무것도 안 켜진 항목은 남길 이유가 없다
        out[code] = item
    return out


def _clean_burst(raw):
    d = dict(DEFAULT_CONFIG["burst"])
    if isinstance(raw, dict):
        w = _to_float(raw.get("window_sec"))
        p = _to_float(raw.get("threshold_pct"))
        if w is not None:
            d["window_sec"] = int(min(max(w, BURST_WINDOW_MIN), BURST_WINDOW_MAX))
        if p is not None:
            d["threshold_pct"] = round(min(max(p, BURST_PCT_MIN), BURST_PCT_MAX), 2)
    return d


def _clean_holdings(raw):
    """보유 정보를 훑어 형태를 강제한다. code -> {"cost": 평단가, "qty": 수량|None}

    사용자가 config.json 에 직접 적을 수 있으므로 "042510": 12345 처럼 숫자
    하나만 써도 평단가로 받아준다. 못 읽는 값은 조용히 버린다.
    """
    out = {}
    if not isinstance(raw, dict):
        return out
    for code, spec in raw.items():
        code = str(code).upper()
        if not CODE_RE.fullmatch(code):
            continue
        d = spec if isinstance(spec, dict) else {"cost": spec}
        cost = _to_float(d.get("cost"))
        if cost is None or cost <= 0:
            continue                       # 평단가가 없으면 수익률을 낼 수 없다
        qty = _to_float(d.get("qty"))
        if qty is not None and qty <= 0:
            qty = None
        out[code] = {"cost": cost, "qty": qty}
    return out


def load_config(root):
    """설정을 읽는다. 깨져 있으면 조용히 넘어가지 않고 사용자에게 알린다.

    .pyw 는 콘솔이 없어서, 여기서 예외를 그냥 던지면 위젯이 아무 말 없이
    안 뜨고 사용자는 원인을 알 방법이 없다.
    """
    if not CONFIG_PATH.exists():
        return _merge_defaults({})
    try:
        # utf-8-sig 로 읽는다. 메모장이나 PowerShell 로 저장하면 UTF-8 BOM 이
        # 붙는데, 그냥 utf-8 로 읽으면 "형식 오류"로 튕겨서 사용자는 자기가
        # 뭘 잘못했는지 알 수 없다. BOM 이 없어도 동일하게 동작한다.
        return _merge_defaults(
            json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
    except json.JSONDecodeError as e:
        where = f"{e.lineno}번째 줄 {e.colno}번째 글자"
        msg = (f"config.json 을 읽을 수 없습니다.\n\n"
               f"위치: {where}\n오류: {e.msg}\n\n"
               f"[기본값으로 초기화하고 실행]을 누르면 지금 파일은\n"
               f"config.json.bak 으로 옮겨 보관합니다.")
    except Exception as e:
        msg = f"config.json 을 읽을 수 없습니다.\n\n{type(e).__name__}: {e}"

    if ask_reset_config(root, msg):
        try:
            shutil.copy2(CONFIG_PATH, CONFIG_PATH.with_suffix(".json.bak"))
        except Exception:
            pass
        return _merge_defaults({})
    return None                                # 사용자가 직접 고치겠다고 함


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 대화 상자 -- overrideredirect 창이 부모라 포커스를 명시적으로 뺏어야 한다.
# ---------------------------------------------------------------------------

def _prep_dialog(dlg, parent):
    dlg.configure(bg="#1c1c1c")
    dlg.attributes("-topmost", True)
    dlg.resizable(False, False)
    dlg.update_idletasks()
    w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
    vx, vy, vw, vh = virtual_screen_rect()
    try:
        px = parent.winfo_rootx() + parent.winfo_width() // 2 - w // 2
        py = parent.winfo_rooty() + 30
    except Exception:
        px, py = vx + vw // 2 - w // 2, vy + vh // 2 - h // 2
    x, y = clamp_position(px, py, w, h, min_w=w, min_h=h)
    dlg.geometry(f"+{x}+{y}")
    dlg.lift()
    dlg.focus_force()
    try:
        dlg.grab_set()
    except tk.TclError:
        pass


def ask_reset_config(parent, message):
    """설정 손상 안내. True = 기본값으로 초기화하고 실행, False = 종료."""
    result = {"v": False}
    dlg = tk.Toplevel(parent)
    dlg.title("설정 파일 오류")
    tk.Label(dlg, text=message, bg="#1c1c1c", fg="#e8e8e8", justify="left",
             font=(FONT_FAMILY, 9), padx=16, pady=14).pack()
    bar = tk.Frame(dlg, bg="#1c1c1c")
    bar.pack(pady=(0, 14))

    def choose(v):
        result["v"] = v
        dlg.destroy()

    tk.Button(bar, text="기본값으로 초기화하고 실행", width=24,
              command=lambda: choose(True)).pack(side="left", padx=6)
    tk.Button(bar, text="종료", width=10,
              command=lambda: choose(False)).pack(side="left", padx=6)
    dlg.protocol("WM_DELETE_WINDOW", lambda: choose(False))
    _prep_dialog(dlg, parent)
    parent.wait_window(dlg)
    return result["v"]


def ask_text(parent, title, prompt):
    """한 줄 입력. 취소하면 None."""
    result = {"v": None}
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    # padx/pady 에 튜플은 pack() 에서만 쓸 수 있다. 위젯 옵션으로 주면
    # TclError: bad screen distance 로 죽는다.
    tk.Label(dlg, text=prompt, bg="#1c1c1c", fg="#e8e8e8", justify="left",
             font=(FONT_FAMILY, 9)).pack(anchor="w", padx=16, pady=(14, 6))
    entry = tk.Entry(dlg, width=30, font=(FONT_FAMILY, 11))
    entry.pack(padx=16, pady=(0, 10))
    bar = tk.Frame(dlg, bg="#1c1c1c")
    bar.pack(pady=(0, 14))

    def ok(_=None):
        result["v"] = entry.get().strip()
        dlg.destroy()

    tk.Button(bar, text="확인", width=10, command=ok).pack(side="left", padx=6)
    tk.Button(bar, text="취소", width=10,
              command=dlg.destroy).pack(side="left", padx=6)
    entry.bind("<Return>", ok)
    dlg.bind("<Escape>", lambda e: dlg.destroy())
    _prep_dialog(dlg, parent)
    entry.focus_set()
    parent.wait_window(dlg)
    return result["v"]


def ask_kis_keys(parent, appkey=""):
    """KIS 앱키·앱시크릿 입력. 취소하면 None.

    앱시크릿은 180자가 넘는다. 손으로 칠 값이 아니라 붙여넣을 값이므로 입력칸을
    넓게 두고 가리지 않는다 -- 가려 놓으면 잘못 붙여넣은 것을 확인할 길이 없다.
    """
    result = {"v": None}
    dlg = tk.Toplevel(parent)
    dlg.title("한국투자증권 연동")
    tk.Label(dlg, bg="#1c1c1c", fg="#e8e8e8", justify="left",
             font=(FONT_FAMILY, 9),
             text="KIS Developers 에서 발급받은 값을 붙여넣으세요.\n"
                  "apiportal.koreainvestment.com → My App\n\n"
                  "실전투자 계좌용 앱키입니다. 모의투자 키는 동작하지 않습니다.",
             ).pack(anchor="w", padx=16, pady=(14, 10))

    entries = {}
    for key, label in (("appkey", "앱키 (APP KEY)"),
                       ("appsecret", "앱시크릿 (APP SECRET)")):
        tk.Label(dlg, text=label, bg="#1c1c1c", fg="#9a9a9a",
                 font=(FONT_FAMILY, 9)).pack(anchor="w", padx=16)
        e = tk.Entry(dlg, width=52, font=("Consolas", 9))
        e.pack(padx=16, pady=(2, 8))
        entries[key] = e
    entries["appkey"].insert(0, appkey or "")

    bar = tk.Frame(dlg, bg="#1c1c1c")
    bar.pack(pady=(4, 14))

    def ok(_=None):
        k = entries["appkey"].get().strip()
        s = entries["appsecret"].get().strip()
        if not k or not s:
            messagebox.showwarning("한국투자증권 연동",
                                   "앱키와 앱시크릿을 모두 입력하세요.",
                                   parent=dlg)
            return
        result["v"] = (k, s)
        dlg.destroy()

    tk.Button(bar, text="연동", width=10, command=ok).pack(side="left", padx=6)
    tk.Button(bar, text="취소", width=10,
              command=dlg.destroy).pack(side="left", padx=6)
    dlg.bind("<Return>", ok)
    dlg.bind("<Escape>", lambda e: dlg.destroy())
    _prep_dialog(dlg, parent)
    entries["appkey" if not appkey else "appsecret"].focus_set()
    parent.wait_window(dlg)
    return result["v"]


def ask_stock(parent, get_items, title="종목 추가", on_reload=None):
    """치는 동안 후보를 보여주고 고르게 한다. 고른 종목코드, 취소하면 None.

    get_items 가 리스트가 아니라 콜러블인 이유: 이 창이 떠 있는 동안 KRX
    목록 다운로드가 끝나면(_load_stocks_async) 캐시가 통째로 새 리스트로
    교체된다. 열 때 잡아둔 리스트를 들고 있으면 영영 비어 있는 셈이다.

    on_reload 를 주면 "목록 새로고침" 버튼이 생긴다. 받은 목록을 인자로
    부르므로, 호출자가 자기 캐시를 갈아끼우면 된다. 캐시 수명이 지나기 전에
    상장한 종목을 사용자가 직접 당겨올 수 있는 유일한 길이다.
    """
    result = {"v": None}
    hits = []

    dlg = tk.Toplevel(parent)
    dlg.title(title)
    tk.Label(dlg, text="종목명을 입력하세요.  (예: 삼성전자)\n"
                       "6자리 종목코드를 넣으면 바로 등록합니다.",
             bg="#1c1c1c", fg="#e8e8e8", justify="left",
             font=(FONT_FAMILY, 9)).pack(anchor="w", padx=16, pady=(14, 6))

    var = tk.StringVar()
    entry = tk.Entry(dlg, width=30, font=(FONT_FAMILY, 11), textvariable=var)
    entry.pack(padx=16, pady=(0, 6))
    # exportselection=False -- Entry 쪽에서 글자를 끌어 선택하면 셀렉션을
    # 빼앗겨 목록의 파란 줄이 사라진다. Enter 가 먹통이 된 것처럼 보인다.
    box = tk.Listbox(dlg, width=34, height=8, font=(FONT_FAMILY, 10),
                     activestyle="none", exportselection=False)
    box.pack(padx=16, pady=(0, 4))
    status = tk.Label(dlg, text="", bg="#1c1c1c", fg="#888888",
                      font=(FONT_FAMILY, 8))
    status.pack(anchor="w", padx=16, pady=(0, 8))

    def refresh(*_):
        raw = var.get().strip()
        items = get_items() or []
        hits[:] = search_stocks(items, raw) if raw else []
        box.delete(0, "end")
        for h in hits:
            box.insert("end", f"{h['name']}  ({h['code']} · {h['market']})")
        if hits:
            box.selection_set(0)
            box.see(0)
        if not raw:
            status.config(text="")
        elif CODE_RE.fullmatch(raw.upper()):
            status.config(text="종목코드 — Enter 를 누르면 바로 등록합니다.")
        elif not items:
            status.config(text="상장종목 목록을 받는 중입니다. 잠시만 기다리세요.")
        elif not hits:
            status.config(text=f"'{raw}' 와 일치하는 종목이 없습니다 — 오늘 "
                                "상장했다면 [목록 새로고침] 을 눌러 보세요.")
        else:
            status.config(text=f"{len(hits)}건 — ↑↓ 로 고르고 Enter")

    def poll_items():
        """목록이 늦게 도착해도 후보가 뜨게 한다.

        없으면 사용자가 글자를 하나 더 칠 때까지 빈 창만 보게 된다. 창이
        먼저 닫혔을 수 있으니 winfo_exists 로 확인하고 들어간다.
        """
        if not dlg.winfo_exists():
            return
        if get_items():
            refresh()
        else:
            dlg.after(500, poll_items)

    def move(delta):
        if hits:
            cur = box.curselection()
            i = max(0, min(len(hits) - 1, (cur[0] if cur else 0) + delta))
            box.selection_clear(0, "end")
            box.selection_set(i)
            box.see(i)
        return "break"

    def ok(_=None):
        raw = var.get().strip().upper()
        if CODE_RE.fullmatch(raw):
            result["v"] = raw
            dlg.destroy()
            return
        sel = box.curselection()
        if sel and hits:
            result["v"] = hits[sel[0]]["code"]
            dlg.destroy()
        # 고른 것이 없으면 창을 닫지 않는다. 오타를 조용히 삼키면 사용자는
        # 왜 아무 일도 일어나지 않았는지 알 길이 없다.

    bar = tk.Frame(dlg, bg="#1c1c1c")
    bar.pack(pady=(0, 14))
    tk.Button(bar, text="확인", width=10, command=ok).pack(side="left", padx=6)
    tk.Button(bar, text="취소", width=10,
              command=dlg.destroy).pack(side="left", padx=6)

    def reload_now():
        """KRX 목록을 캐시 수명과 무관하게 지금 다시 받는다.

        1.2MB 다운로드라 메인 스레드에서 하면 창이 통째로 얼어붙는다. 워커가
        받아오고 UI 는 after(0) 로 돌아와서 건드린다 -- 위젯 본체의 규칙과
        같다. 받는 사이 창이 닫혔어도 목록은 호출자에게 넘긴다(받아둔 것을
        버릴 이유가 없다). 버튼을 잠가 두 번 누르는 것을 막는다.
        """
        reload_btn.config(state="disabled")
        status.config(text="상장종목 목록을 새로 받는 중입니다 … (10초쯤)")

        def work():
            items = load_stock_list(force=True)
            if items and on_reload:
                on_reload(items)

            def done():
                if not dlg.winfo_exists():
                    return
                reload_btn.config(state="normal")
                if items:
                    refresh()
                    if not var.get().strip():
                        status.config(text=f"{len(items)}개 종목을 받았습니다.")
                else:
                    status.config(text="목록을 받지 못했습니다. "
                                       "잠시 뒤 다시 눌러 보세요.")

            try:
                dlg.after(0, done)
            except (RuntimeError, tk.TclError):
                pass

        threading.Thread(target=work, daemon=True).start()

    reload_btn = tk.Button(bar, text="목록 새로고침", width=14,
                           command=reload_now)
    if on_reload:
        reload_btn.pack(side="left", padx=6)

    # KeyRelease 가 아니라 StringVar 추적이다. 한글은 IME 조합이 끝나야
    # 글자가 들어오는데 그 확정 시점에 KeyRelease 가 오지 않는 경우가 있다.
    var.trace_add("write", refresh)
    for w in (entry, box):
        w.bind("<Down>", lambda e: move(1))
        w.bind("<Up>", lambda e: move(-1))
        w.bind("<Return>", ok)
    box.bind("<Double-Button-1>", ok)
    dlg.bind("<Escape>", lambda e: dlg.destroy())
    _prep_dialog(dlg, parent)
    entry.focus_set()
    refresh()
    if not get_items():
        dlg.after(500, poll_items)
    parent.wait_window(dlg)
    return result["v"]


def ask_alert(parent, name, code, price, spec):
    """종목 알림 설정. 취소하면 None, 확인하면 {above, below, burst}."""
    result = {"v": None}
    dlg = tk.Toplevel(parent)
    dlg.title("가격 알림")

    head = f"{name} ({code})"
    if price is not None:
        head += f"    현재 {fmt_won(price)}"
    tk.Label(dlg, text=head, bg="#1c1c1c", fg="#e8e8e8",
             font=(FONT_FAMILY, 11, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
    tk.Label(dlg, text="비워두면 그 조건은 쓰지 않습니다.", bg="#1c1c1c",
             fg="#888888", font=(FONT_FAMILY, 8)).pack(anchor="w", padx=16)

    grid = tk.Frame(dlg, bg="#1c1c1c")
    grid.pack(padx=16, pady=(10, 6), anchor="w")
    entries = {}
    for row, (key, label) in enumerate((("above", "이 값 이상이 되면"),
                                        ("below", "이 값 이하가 되면"))):
        tk.Label(grid, text=label, bg="#1c1c1c", fg="#e8e8e8",
                 font=(FONT_FAMILY, 9), width=15, anchor="w"
                 ).grid(row=row, column=0, pady=3)
        e = tk.Entry(grid, width=14, font=(FONT_FAMILY, 11), justify="right")
        if spec.get(key) is not None:
            e.insert(0, f"{float(spec[key]):.0f}")
        e.grid(row=row, column=1, pady=3)
        tk.Label(grid, text="원", bg="#1c1c1c", fg="#888888",
                 font=(FONT_FAMILY, 9)).grid(row=row, column=2, padx=(4, 0))
        entries[key] = e

    burst_var = tk.BooleanVar(value=bool(spec.get("burst")))
    limit_var = tk.BooleanVar(value=bool(spec.get("limit")))
    for text, var in (("급변동 감지 (종목별 하루 1회)", burst_var),
                      ("상·하한가 도달·풀림 (바뀔 때마다)", limit_var)):
        tk.Checkbutton(dlg, text=text, variable=var,
                       bg="#1c1c1c", fg="#e8e8e8", selectcolor="#1c1c1c",
                       activebackground="#1c1c1c", activeforeground="#e8e8e8",
                       font=(FONT_FAMILY, 9)).pack(anchor="w", padx=14)

    note = tk.Label(dlg, text="", bg="#1c1c1c", fg=COLOR_UP,
                    font=(FONT_FAMILY, 8), justify="left", wraplength=300)
    note.pack(anchor="w", padx=16, pady=(4, 0))

    bar = tk.Frame(dlg, bg="#1c1c1c")
    bar.pack(pady=(10, 14))

    def ok(_=None):
        vals = {}
        for key, e in entries.items():
            raw = e.get().strip().replace(",", "")
            if not raw:
                vals[key] = None
                continue
            v = _to_float(raw)
            if v is None or v <= 0:
                note.configure(text=f"'{raw}' 는 가격으로 읽을 수 없습니다.")
                return
            vals[key] = v
        # 이미 충족된 값을 넣으면 다음 폴링에 바로 울린다. 사용자가 원한 건
        # 그게 아니므로 여기서 막는다. (config.json 을 직접 고치는 길은 열려 있다)
        if price is not None:
            if vals["above"] is not None and price >= vals["above"]:
                note.configure(text=f"현재가 {fmt_won(price)}이 이미 그 값 이상입니다. "
                                    f"더 높은 값을 넣으세요.")
                return
            if vals["below"] is not None and price <= vals["below"]:
                note.configure(text=f"현재가 {fmt_won(price)}이 이미 그 값 이하입니다. "
                                    f"더 낮은 값을 넣으세요.")
                return
        if (vals["above"] is not None and vals["below"] is not None
                and vals["below"] >= vals["above"]):
            note.configure(text="하한이 상한보다 높습니다.")
            return
        result["v"] = {"above": vals["above"], "below": vals["below"],
                       "burst": bool(burst_var.get()),
                       "limit": bool(limit_var.get())}
        dlg.destroy()

    tk.Button(bar, text="확인", width=10, command=ok).pack(side="left", padx=6)
    tk.Button(bar, text="취소", width=10, command=dlg.destroy).pack(side="left", padx=6)
    dlg.bind("<Return>", ok)
    dlg.bind("<Escape>", lambda e: dlg.destroy())
    _prep_dialog(dlg, parent)
    entries["above"].focus_set()
    parent.wait_window(dlg)
    return result["v"]


def ask_holding(parent, name, code, price, spec):
    """매수 평단가·수량. 취소하면 None, 평단가를 비우고 확인하면 {}(해제)."""
    result = {"v": None}
    dlg = tk.Toplevel(parent)
    dlg.title("매수 평단가")

    head = f"{name} ({code})"
    if price is not None:
        head += f"    현재 {fmt_won(price)}"
    tk.Label(dlg, text=head, bg="#1c1c1c", fg="#e8e8e8",
             font=(FONT_FAMILY, 11, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
    tk.Label(dlg, text="평단가를 비우고 확인하면 수익률 표시를 끕니다.",
             bg="#1c1c1c", fg="#888888",
             font=(FONT_FAMILY, 8)).pack(anchor="w", padx=16)

    grid = tk.Frame(dlg, bg="#1c1c1c")
    grid.pack(padx=16, pady=(10, 6), anchor="w")
    entries = {}
    for row, (key, label, unit) in enumerate((("cost", "매수 평단가", "원"),
                                              ("qty", "보유 수량 (선택)", "주"))):
        tk.Label(grid, text=label, bg="#1c1c1c", fg="#e8e8e8",
                 font=(FONT_FAMILY, 9), width=15, anchor="w"
                 ).grid(row=row, column=0, pady=3)
        e = tk.Entry(grid, width=14, font=(FONT_FAMILY, 11), justify="right")
        if spec.get(key) is not None:
            e.insert(0, f"{float(spec[key]):g}")
        e.grid(row=row, column=1, pady=3)
        tk.Label(grid, text=unit, bg="#1c1c1c", fg="#888888",
                 font=(FONT_FAMILY, 9)).grid(row=row, column=2, padx=(4, 0))
        entries[key] = e

    HINT = ("총 매수금액이 아니라 1주당 평균 단가입니다 (총액 / 수량).\n"
            "수량까지 넣으면 평가손익 금액도 함께 보여줍니다.")
    note = tk.Label(dlg, text=HINT, bg="#1c1c1c", fg="#888888",
                    font=(FONT_FAMILY, 8), justify="left", wraplength=300)
    note.pack(anchor="w", padx=16, pady=(4, 0))

    preview = tk.Label(dlg, text="", bg="#1c1c1c", fg="#e8e8e8",
                       font=(FONT_FAMILY, 9), justify="left")
    preview.pack(anchor="w", padx=16, pady=(4, 0))

    def repreview(*_):
        """입력하는 동안 지금 기준 수익률을 보여준다. 자릿수 실수를 바로 잡는다."""
        cost = _to_float(entries["cost"].get().strip().replace(",", ""))
        if price is None or cost is None or cost <= 0:
            preview.configure(text="")
            return
        pct = profit_pct(cost, price)
        line = f"지금 기준  {pct:+.2f}%"
        qty = _to_float(entries["qty"].get().strip().replace(",", ""))
        if qty is not None and qty > 0:
            line += f"   ({(price - cost) * qty:+,.0f}원)"
        preview.configure(text=line, fg=(COLOR_UP if pct > 0 else
                                         COLOR_DOWN if pct < 0 else COLOR_FLAT))

    for e in entries.values():
        e.bind("<KeyRelease>", repreview)
    repreview()

    bar = tk.Frame(dlg, bg="#1c1c1c")
    bar.pack(pady=(10, 14))

    def ok(_=None):
        raw = entries["cost"].get().strip().replace(",", "")
        if not raw:
            result["v"] = {}                   # 비우고 확인하면 해제
            dlg.destroy()
            return
        cost = _to_float(raw)
        if cost is None or cost <= 0:
            note.configure(text=f"'{raw}' 는 가격으로 읽을 수 없습니다.", fg=COLOR_UP)
            return
        qty = None
        qraw = entries["qty"].get().strip().replace(",", "")
        if qraw:
            qty = _to_float(qraw)
            if qty is None or qty <= 0:
                note.configure(text=f"'{qraw}' 는 수량으로 읽을 수 없습니다.",
                               fg=COLOR_UP)
                return
        result["v"] = {"cost": cost, "qty": qty}
        dlg.destroy()

    tk.Button(bar, text="확인", width=10, command=ok).pack(side="left", padx=6)
    tk.Button(bar, text="취소", width=10, command=dlg.destroy).pack(side="left", padx=6)
    dlg.bind("<Return>", ok)
    dlg.bind("<Escape>", lambda e: dlg.destroy())
    _prep_dialog(dlg, parent)
    entries["cost"].focus_set()
    parent.wait_window(dlg)
    return result["v"]


# ---------------------------------------------------------------------------
# 카카오톡 연동 마법사 -- 카카오 개발자 콘솔에서 앱을 만드는 일만은 대신 해줄 수
# 없다(본인 인증·약관 동의). 대신 무엇을 어디에 넣어야 하는지를 다이얼로그에
# 그대로 적어두고, 그 뒤의 OAuth 왕복은 전부 자동으로 처리한다.
# ---------------------------------------------------------------------------

KAKAO_STEPS = """1.  developers.kakao.com → 내 애플리케이션 → 애플리케이션 추가하기
2.  앱 설정 → 앱 키 → REST API 키를 복사해 아래에 붙여넣기
3.  제품 설정 → 카카오 로그인 → 활성화 상태 ON
4.  같은 화면의 Redirect URI 에 아래 주소를 등록
5.  제품 설정 → 카카오 로그인 → 동의항목 →
     '카카오톡 메시지 전송' 을 선택 동의로 설정"""


def link_kakao(parent):
    """연동 마법사. 성공하면 True."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs

    state = {"ok": False, "httpd": None, "deadline": None}
    events = queue.Queue()

    dlg = tk.Toplevel(parent)
    dlg.title("카카오톡 연동")

    tk.Label(dlg, text="카카오톡으로 알림을 받으려면 카카오 개발자 앱이 하나 필요합니다.",
             bg="#1c1c1c", fg="#e8e8e8", font=(FONT_FAMILY, 10, "bold")
             ).pack(anchor="w", padx=16, pady=(14, 8))
    tk.Label(dlg, text=KAKAO_STEPS, bg="#141414", fg="#cccccc", justify="left",
             font=(FONT_FAMILY, 9), padx=12, pady=10
             ).pack(anchor="w", padx=16, fill="x")

    uri_bar = tk.Frame(dlg, bg="#1c1c1c")
    uri_bar.pack(anchor="w", padx=16, pady=(8, 4), fill="x")
    tk.Label(uri_bar, text="Redirect URI", bg="#1c1c1c", fg="#888888",
             font=(FONT_FAMILY, 8)).pack(side="left")
    tk.Label(uri_bar, text=kakao.REDIRECT_URI, bg="#1c1c1c", fg="#7ecb7e",
             font=(FONT_FAMILY, 9, "bold")).pack(side="left", padx=8)

    def copy_uri():
        parent.clipboard_clear()
        parent.clipboard_append(kakao.REDIRECT_URI)

    tk.Button(uri_bar, text="복사", command=copy_uri).pack(side="left")
    tk.Button(uri_bar, text="개발자 사이트 열기",
              command=lambda: webbrowser.open("https://developers.kakao.com/console/app")
              ).pack(side="left", padx=6)

    form = tk.Frame(dlg, bg="#1c1c1c")
    form.pack(anchor="w", padx=16, pady=(6, 4))
    tk.Label(form, text="REST API 키", bg="#1c1c1c", fg="#e8e8e8",
             font=(FONT_FAMILY, 9), width=13, anchor="w").grid(row=0, column=0, pady=3)
    key_e = tk.Entry(form, width=40, font=(FONT_FAMILY, 10))
    key_e.grid(row=0, column=1, pady=3)
    tk.Label(form, text="Client Secret", bg="#1c1c1c", fg="#e8e8e8",
             font=(FONT_FAMILY, 9), width=13, anchor="w").grid(row=1, column=0, pady=3)
    sec_e = tk.Entry(form, width=40, font=(FONT_FAMILY, 10))
    sec_e.grid(row=1, column=1, pady=3)
    tk.Label(form, text="Client Secret 을 안 쓰면 비워두세요.", bg="#1c1c1c",
             fg="#888888", font=(FONT_FAMILY, 8)).grid(row=2, column=1, sticky="w")

    # 기본 브라우저가 안 열리거나 8321 포트가 막힌 환경을 위한 수동 경로.
    manual = tk.Frame(dlg, bg="#1c1c1c")
    tk.Label(manual, text="브라우저 주소창의 주소를 통째로 붙여넣으세요",
             bg="#1c1c1c", fg="#888888",
             font=(FONT_FAMILY, 8)).pack(anchor="w")
    paste_e = tk.Entry(manual, width=52, font=(FONT_FAMILY, 9))
    paste_e.pack(side="left", pady=(2, 0))

    status = tk.Label(dlg, text="", bg="#1c1c1c", fg="#e8e8e8", justify="left",
                      font=(FONT_FAMILY, 9), wraplength=430)
    status.pack(anchor="w", padx=16, pady=(8, 0))

    # 브라우저에서 나는 오류는 위젯이 알 길이 없다(리다이렉트가 아예 안 온다).
    # 무엇을 못 했다는 뜻인지 여기 적어두는 것이 유일한 도움말이다.
    trouble = tk.Label(
        dlg, bg="#141414", fg="#cccccc", justify="left", padx=12, pady=8,
        font=(FONT_FAMILY, 8), wraplength=430,
        text="브라우저에 오류 화면이 떴다면\n"
             "  KOE205  동의항목 '카카오톡 메시지 전송'이 아직 꺼져 있습니다.\n"
             "            → 카카오 로그인 → 동의항목 → 선택 동의로 설정 (위 5번)\n"
             "  KOE006  Redirect URI 가 등록되지 않았습니다. (위 4번)\n"
             "  KOE101  REST API 키가 잘못됐습니다. (위 2번)\n"
             "고친 뒤 [연동 시작]을 다시 누르세요.")

    bar = tk.Frame(dlg, bg="#1c1c1c")
    bar.pack(pady=(10, 14))

    def say(text, color="#e8e8e8"):
        status.configure(text=text, fg=color)

    def shutdown_server():
        httpd, state["httpd"] = state["httpd"], None
        if httpd:
            threading.Thread(target=httpd.shutdown, daemon=True).start()

    def close():
        shutdown_server()
        dlg.destroy()

    # -- 콜백 서버 ----------------------------------------------------------

    def serve():
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                q = parse_qs(urlparse(self.path).query)
                code = (q.get("code") or [None])[0]
                err = (q.get("error_description") or q.get("error") or [None])[0]
                if code:
                    events.put(("code", code))
                    body = "연동됐습니다. 이 창을 닫고 위젯으로 돌아가세요."
                elif err:
                    events.put(("err", err))
                    body = f"연동 실패: {err}"
                else:
                    body = "잘못된 요청입니다."
                data = (f"<html><head><meta charset='utf-8'></head>"
                        f"<body style='font-family:sans-serif;padding:40px'>"
                        f"<h2>{html.escape(body)}</h2></body></html>").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass                      # 콘솔이 없다. 기본 로깅은 예외만 만든다.

        try:
            httpd = HTTPServer(("127.0.0.1", kakao.CALLBACK_PORT), Handler)
        except OSError as e:
            events.put(("noport", str(e)))
            return
        state["httpd"] = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # -- 진행 ---------------------------------------------------------------

    def start():
        key = key_e.get().strip()
        if not key:
            say("REST API 키를 먼저 붙여넣으세요.", COLOR_UP)
            return
        start_btn.configure(state="disabled")
        serve()
        webbrowser.open(kakao.build_authorize_url(key))
        state["deadline"] = time.monotonic() + 120
        say("브라우저에서 카카오 로그인과 동의를 마치세요.\n"
            "동의가 끝나면 자동으로 이어집니다. (2분 안에)\n"
            "브라우저에 오류 화면이 떴다면 아래 안내를 보세요.")
        trouble.pack(anchor="w", padx=16, pady=(6, 0), fill="x", before=bar)
        manual.pack(anchor="w", padx=16, pady=(6, 0))
        dlg.after(200, poll)

    def apply_manual():
        code = kakao.extract_code(paste_e.get())
        if not code:
            say("그 주소에서 code 값을 찾지 못했습니다.", COLOR_UP)
            return
        events.put(("code", code))

    tk.Button(manual, text="적용", command=apply_manual).pack(side="left", padx=6)

    def poll():
        try:
            kind, payload = events.get_nowait()
        except queue.Empty:
            if state["deadline"] and time.monotonic() > state["deadline"]:
                shutdown_server()
                start_btn.configure(state="normal")
                state["deadline"] = None
                say("2분 동안 응답이 없었습니다.\n"
                    "브라우저에 오류 화면이 떴다면 아래 표를 보고 고친 뒤 다시 "
                    "누르세요. 동의는 마쳤는데 안 이어졌다면 브라우저 주소를 "
                    "맨 아래 칸에 붙여넣으세요.", COLOR_UP)
                return
            dlg.after(200, poll)
            return

        if kind == "noport":
            say(f"포트 {kakao.CALLBACK_PORT} 를 열 수 없습니다 ({payload}).\n"
                "브라우저 주소를 아래에 직접 붙여넣으세요.", COLOR_UP)
            dlg.after(200, poll)
            return
        if kind == "err":
            shutdown_server()
            start_btn.configure(state="normal")
            say(f"카카오가 거절했습니다: {payload}", COLOR_UP)
            return
        if kind == "code":
            shutdown_server()
            state["deadline"] = None
            say("토큰을 받는 중…")
            exchange(payload)
            return
        if kind == "done":
            state["ok"] = True
            say("연동됐습니다. 테스트 메시지를 보냅니다.", "#7ecb7e")
            log_alert("카카오 연동 성공")
            dlg.after(700, close)
            return
        if kind == "fail":
            start_btn.configure(state="normal")
            say(f"{payload}\n\n"
                "· KOE010 이면 Client Secret 이 켜져 있는 것입니다. 값을 넣으세요.\n"
                "· KOE320 이면 인가 코드가 이미 쓰였습니다. 다시 시작하세요.\n"
                "· redirect_uri 오류면 4번 단계의 주소 등록을 확인하세요.", COLOR_UP)
            log_alert(f"카카오 연동 실패 | {payload}")
            return

    def exchange(code):
        key, sec = key_e.get().strip(), sec_e.get().strip()

        def work():
            try:
                kakao.exchange_code(key, sec, code)
                events.put(("done", None))
            except Exception as e:
                events.put(("fail", str(e)))
        threading.Thread(target=work, daemon=True).start()
        dlg.after(200, poll)

    start_btn = tk.Button(bar, text="연동 시작", width=12, command=start)
    start_btn.pack(side="left", padx=6)
    tk.Button(bar, text="닫기", width=10, command=close).pack(side="left", padx=6)
    dlg.protocol("WM_DELETE_WINDOW", close)
    dlg.bind("<Escape>", lambda e: close())

    existing = kakao.load_tokens()
    if existing:
        key_e.insert(0, existing.get("rest_key", ""))
        sec_e.insert(0, existing.get("client_secret", ""))
        say(f"이미 연동되어 있습니다 (연동 {existing.get('linked_at', '?')}).\n"
            "다시 연동하면 기존 토큰을 덮어씁니다.")

    _prep_dialog(dlg, parent)
    key_e.focus_set()
    parent.wait_window(dlg)
    return state["ok"]


# ---------------------------------------------------------------------------
# 종목 목록 -- KRX 가 공식 배포하는 전체 상장종목 파일을 한 번 받아 캐시한다.
# 이후 검색과 코드 유효성 검사는 전부 오프라인이다.
# ---------------------------------------------------------------------------

def download_stock_list():
    req = Request(KRX_URL, headers={"User-Agent": UA})
    with urlopen(req, timeout=KRX_TIMEOUT) as r:   # SSL 검증은 켜둔 채로 동작한다
        raw = r.read()
    text = raw.decode("euc-kr", errors="replace")
    items = []
    for row in re.findall(r"<tr>(.*?)</tr>", text, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 3 and CODE_RE.fullmatch(cells[2].upper()):
            items.append({"code": cells[2].upper(),
                          "name": html.unescape(cells[0]),
                          "market": cells[1]})
    if not items:
        raise ValueError("KRX 응답에서 종목을 찾지 못했습니다")
    return items


def _stocks_fresh(ts, now=None):
    """캐시가 "오늘 개장 이후"의 것인가.

    신규 상장 종목은 개장 시각에 목록에 오른다. 개장 전에 받은 캐시에는 그날
    상장분이 없으므로, 기준선을 넘기 전이면 어제 기준선으로 물러나 그때 받은
    것까지만 인정한다. 그래야 새벽에 켜 둔 위젯이 매 재확인마다 1.2MB 를 다시
    받지 않으면서도, 09:00 이 지나면 그날 것을 한 번 받는다.
    """
    if not ts:
        return False
    now = time.time() if now is None else now
    t = time.localtime(now)
    cut = time.mktime((t.tm_year, t.tm_mon, t.tm_mday,
                       STOCKS_FRESH_HOUR, 0, 0, 0, 0, -1))
    if now < cut:
        cut -= 86400
    return ts >= cut


def load_stock_list(force=False):
    """캐시를 읽고, 없거나 오래됐으면 새로 받는다. 실패하면 빈 목록."""
    if not force and STOCKS_PATH.exists():
        try:
            data = json.loads(STOCKS_PATH.read_text(encoding="utf-8-sig"))
            items = data.get("items") or []
            if items and _stocks_fresh(data.get("updated", 0)):
                return items
            fresh = download_stock_list()
            _save_stock_list(fresh)
            return fresh
        except json.JSONDecodeError:
            # 검색 캐시는 언제든 다시 받을 수 있다 -- 묻지 않고 조용히 재생성한다.
            try:
                STOCKS_PATH.unlink()
            except Exception:
                pass
        except Exception:
            try:                                # 갱신 실패 시 묵은 캐시로 계속
                data = json.loads(STOCKS_PATH.read_text(encoding="utf-8-sig"))
                if data.get("items"):
                    return data["items"]
            except Exception:
                pass
    try:
        items = download_stock_list()
        _save_stock_list(items)
        return items
    except Exception:
        return []


def _save_stock_list(items):
    try:
        STOCKS_PATH.write_text(
            json.dumps({"updated": int(time.time()), "items": items},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


# KIS 는 시세 응답에 한글 종목명을 주지 않는다(bstp_kor_isnm 은 업종명이다).
# KRX 캐시를 한 번 읽어 여기에 담아 두고 붙인다. 1초마다 190KB 짜리 stocks.json
# 을 다시 읽을 수는 없으므로, 파일이 아니라 이 딕셔너리가 조회 경로다.
_NAME_CACHE = {}
_names_seeded = False


def remember_names(items):
    for it in items or []:
        code = str(it.get("code", "")).upper()
        if code and it.get("name"):
            _NAME_CACHE[code] = it["name"]


def stock_name(code, default=None):
    """종목명. 없으면 종목코드를 그대로 쓴다.

    KRX 캐시는 위젯이 뜬 뒤 비동기로 읽는데, 폴링은 그보다 먼저 시작한다. 그
    몇 순환을 종목코드로 보내면 그 사이 발동한 알림 메시지에도 코드가 박힌다.
    그래서 처음 이름이 필요해지는 순간 파일을 한 번만 직접 읽어 채운다.
    """
    global _names_seeded
    if code not in _NAME_CACHE and not _names_seeded:
        _names_seeded = True            # 실패해도 다시 읽지 않는다
        try:
            data = json.loads(STOCKS_PATH.read_text(encoding="utf-8-sig"))
            remember_names(data.get("items"))
        except Exception:
            pass
    return _NAME_CACHE.get(code) or default or code


def _is_subsequence(q, n):
    it = iter(n)
    return all(ch in it for ch in q)


def search_stocks(items, keyword, limit=30):
    """종목명 검색. 완전일치 > 앞부분일치 > 부분일치 > 부분수열 순.

    부분수열 단계가 필요한 이유: KRX 는 법인명을 쓰고(현대자동차) 사람은 통칭을
    친다(현대차). 부분일치만 쓰면 '현대차'가 엉뚱하게 현대차증권 하나만 잡아
    조용히 잘못된 종목이 등록된다. 부분수열을 넣으면 현대자동차가 함께 올라와
    선택 창이 뜬다.
    """
    q = keyword.strip().lower().replace(" ", "")
    if not q:
        return []
    exact, starts, contains, subseq = [], [], [], []
    for it in items:
        n = it["name"].lower().replace(" ", "")
        if n == q:
            exact.append(it)
        elif n.startswith(q):
            starts.append(it)
        elif q in n:
            contains.append(it)
        elif len(q) >= 2 and _is_subsequence(q, n):
            subseq.append(it)
    return (exact + starts + contains + subseq[:10])[:limit]


# ---------------------------------------------------------------------------
# 시세 조회
# ---------------------------------------------------------------------------

def _to_float(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _direction(ratio, label=""):
    label = (label or "").upper()
    if "RIS" in label or "UPPER" in label:
        return 1
    if "FALL" in label or "LOWER" in label:
        return -1
    if ratio is None or abs(ratio) < 1e-9:
        return 0
    return 1 if ratio > 0 else -1


# compareToPreviousPrice.code -- 1 상한 / 2 상승 / 3 보합 / 4 하한 / 5 하락.
# 등락률로 상한가를 짐작하면 안 된다. 가격제한폭은 ±30% 가 전부가 아니라서
# (신규상장 첫날, 정리매매 종목 등) 실측으로 +62% 인 종목도 있다. 라벨만이 정답이다.
LIMIT_CODES = {"1": 1, "4": -1}


def _limit_state(compare):
    """상한 1 / 하한 -1 / 그 외 0. 판단할 근거가 없으면 None."""
    if not isinstance(compare, dict):
        return None
    code = str(compare.get("code", "")).strip()
    if not code:
        return None
    return LIMIT_CODES.get(code, 0)


def fetch_naver(codes):
    """1회 요청으로 여러 종목을 받는다. -> (quotes, 서버 권장 주기(초) 또는 None)"""
    req = Request(NAVER_URL.format(",".join(codes)),
                  headers={"User-Agent": UA})
    with urlopen(req, timeout=FETCH_TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8"))
    quotes = {}
    for d in data.get("datas", []):
        code = d.get("itemCode")
        price = _to_float(d.get("closePrice"))
        if not code or price is None:
            continue
        ratio = _to_float(d.get("fluctuationsRatio")) or 0.0
        compare = d.get("compareToPreviousPrice") or {}
        quotes[code] = {
            "code": code,
            "name": d.get("stockName") or code,
            "price": price,
            "ratio": abs(ratio),
            "direction": _direction(ratio, compare.get("name", "")),
            "limit": _limit_state(compare),
            "market_open": d.get("marketStatus") == "OPEN",
            "delayed": False,
            # 적응형 주기가 "지금 체결이 있나"를 판단하는 근거. 누적값이라
            # 단조 증가한다. 늘지 않았다면 그 순환은 빈 요청이었다는 뜻이다.
            "volume": _to_float(d.get("accumulatedTradingVolumeRaw")),
        }
    interval = data.get("pollingInterval")
    return quotes, (interval / 1000.0 if isinstance(interval, (int, float)) else None)


# prdy_vrss_sign -- 1 상한 / 2 상승 / 3 보합 / 4 하한 / 5 하락.
# 네이버 compareToPreviousPrice.code 와 같은 체계다. 여기서도 등락률로 상한가를
# 짐작하지 않는다. 부호 코드만이 정답이다.
KIS_SIGN_DIRECTION = {"1": 1, "2": 1, "3": 0, "4": -1, "5": -1}
KIS_SIGN_LIMIT = {"1": 1, "4": -1}


def _kis_quote(code, out, market_open):
    """KIS output 딕셔너리를 위젯의 quote 계약으로 옮긴다.

    네트워크를 타지 않는 순수 변환이라 셀프테스트가 여기를 직접 먹인다.
    """
    price = _to_float(out.get("stck_prpr"))
    if price is None:
        raise ValueError(f"{code}: 현재가 없음")
    ratio = _to_float(out.get("prdy_ctrt")) or 0.0
    sign = str(out.get("prdy_vrss_sign", "")).strip()
    return {
        "code": code,
        "name": stock_name(code),
        "price": price,
        "ratio": abs(ratio),
        "direction": KIS_SIGN_DIRECTION.get(sign, _direction(ratio)),
        # 부호 코드가 비어 오면 "상한가 아님"이 아니라 "모른다"다. 0 으로 읽으면
        # 상한가로 마감한 종목에 가짜 "풀림"을 만들어 낸다.
        "limit": KIS_SIGN_LIMIT.get(sign, 0) if sign else None,
        "market_open": market_open,
        "delayed": False,               # 공식 실시간 시세다
        "volume": _to_float(out.get("acml_vol")),
    }


def fetch_kis(codes):
    """KIS 현재가 조회. -> (quotes, 서버 권장 주기(초) 또는 None)

    inquire-price 는 종목을 하나씩만 받는다. 네이버가 한 번에 주던 것을 종목 수
    만큼 나눠 부르는 셈인데, 실전 한도가 초당 20건이라 몇 종목까지는 문제없다
    (kis._space_calls 가 간격을 지킨다).

    권장 주기를 돌려주지 않는 것은 KIS 가 그런 값을 주지 않기 때문이다. 대신
    한도가 공개돼 있어서 Poller 가 적응형을 끄고 사용자가 고른 주기를 그대로 쓴다.
    """
    if kis is None:
        raise ValueError("kis.py 없음")
    market_open = kis.market_open_now()
    quotes, last_err = {}, None
    for code in codes:
        try:
            quotes[code] = _kis_quote(code, kis.fetch_price(code), market_open)
        except Exception as e:
            last_err = e
    if not quotes:
        raise last_err or ValueError("KIS 조회 실패")
    return quotes, None


def fetch_yahoo_one(code, suffixes=("KS", "KQ")):
    """폴백. 한국 주식은 지연 시세라 delayed=True 로 표시한다."""
    last_err = None
    for sfx in suffixes:
        try:
            req = Request(YAHOO_URL.format(code, sfx),
                          headers={"User-Agent": UA})
            with urlopen(req, timeout=FETCH_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
            meta = data["chart"]["result"][0]["meta"]
            price = _to_float(meta.get("regularMarketPrice"))
            prev = _to_float(meta.get("previousClose")
                             or meta.get("chartPreviousClose"))
            if price is None:
                continue
            ratio = ((price - prev) / prev * 100.0) if prev else 0.0
            return {
                "code": code,
                "name": meta.get("shortName") or code,
                "price": price,
                "ratio": abs(ratio),
                "direction": _direction(ratio),
                "limit": None,          # Yahoo 는 상·하한가를 알려주지 않는다
                "market_open": False,
                "delayed": True,
                # 15분 지연 시세라 "지금 체결이 있나"를 말할 수 없다. None 이면
                # 적응형은 판단을 포기하고 서버 권장 주기로 물러난다.
                "volume": None,
            }
        except Exception as e:
            last_err = e
    raise last_err or ValueError(f"{code} 조회 실패")


def fetch_yahoo(codes):
    quotes = {}
    for c in codes:
        try:
            quotes[c] = fetch_yahoo_one(c)
        except Exception:
            pass
    if not quotes:
        raise ValueError("Yahoo 폴백도 실패")
    return quotes, None


# ---------------------------------------------------------------------------
# 폴링 스레드 -- tkinter 는 스레드 안전하지 않다. 네트워크 결과는 큐로만 넘기고
# UI 는 메인 스레드에서만 건드린다.
# ---------------------------------------------------------------------------

class Poller(threading.Thread):
    def __init__(self, get_codes, out_queue, get_override=None):
        super().__init__(daemon=True)
        self.get_codes = get_codes
        self.q = out_queue
        # 사용자가 고른 장중 주기(초) 또는 None. 매번 물어보므로 설정을 바꾸면
        # 스레드를 다시 만들지 않아도 다음 순환부터 반영된다.
        self.get_override = get_override or (lambda: None)
        self._wake = threading.Event()
        # 이름을 _stop 으로 두면 안 된다. threading.Thread 의 내부 메서드
        # _stop() 을 불리언으로 덮어써서 join() 이 TypeError 로 죽는다.
        self._stopping = False
        self.interval = INTERVAL_OPEN
        self._volumes = {}          # code -> 직전 누적 거래량. 적응형 주기용
        self.source = None          # 마지막으로 성공한 시세 출처
        # KIS 가 연동돼 있는데도 계속 실패하면 조용히 네이버로 돌게 된다. 사용자가
        # 그 사실을 알 길이 있어야 해서 마지막 사유를 남긴다(메인 스레드가 읽는다).
        self.kis_error = None
        self._kis_on = False        # kis.is_linked() 캐시
        self._kis_at = 0.0

    def wake(self):
        # 연동·주기 변경 직후에 부르는 자리이기도 하다. 캐시를 무효화해
        # 다음 순환이 바뀐 상태를 곧바로 보게 한다.
        self._kis_at = 0.0
        self._wake.set()

    def stop(self):
        self._stopping = True
        self._wake.set()

    def run(self):
        while not self._stopping:
            codes = list(self.get_codes())
            if codes:
                self._cycle(codes)
            self._wake.wait(self.interval)
            self._wake.clear()

    def _sources(self):
        """시도 순서. (이름, 조회 함수, 적응형 여부).

        KIS 는 적응형을 끈다. 적응형은 "한도를 모르는 서버에 빈 요청을 덜 보내자"
        는 장치인데, 공식 API 는 한도가 공개돼 있어(실전 초당 20건) 물러날 이유가
        없다. 고른 주기가 곧 주기다.
        """
        out = []
        if kis is not None:
            now = time.monotonic()
            if now - self._kis_at > 30.0:
                try:
                    self._kis_on = kis.is_linked()
                except Exception:
                    self._kis_on = False
                self._kis_at = now
            if self._kis_on:
                out.append(("kis", fetch_kis, False))
        out.append(("naver", fetch_naver, True))
        out.append(("yahoo", fetch_yahoo, True))
        return out

    def _session(self):
        """주기를 정하기 위한 장 구분. kis.py 가 없으면 구분하지 않는다.

        장 시간표를 아는 곳은 kis.py 한 곳뿐이다(휴장일 API 가 거기 있다).
        여기서 시계 계산을 다시 쓰면 두 곳이 어긋난다.
        """
        if kis is None:
            return None
        try:
            return kis.session_now()
        except Exception:
            return None

    def _cycle(self, codes):
        """처음 성공한 출처를 쓴다. 전부 실패했을 때만 오류를 올린다."""
        last_err = None
        for name, fetch, adaptive in self._sources():
            try:
                quotes, server_interval = fetch(codes)
                if not quotes:
                    raise ValueError("빈 응답")
            except Exception as e:
                last_err = e
                if name == "kis":
                    self.kis_error = f"{type(e).__name__}: {e}"
                continue
            if name == "kis":
                self.kis_error = None
            self.source = name
            self.interval = self._next_interval(quotes, server_interval,
                                                adaptive=adaptive)
            self.q.put(("ok", quotes))
            return
        self.q.put(("err", f"{type(last_err).__name__}: {last_err}"))

    def _next_interval(self, quotes, server_interval, adaptive=True):
        """체결이 있으면 당기고, 없으면 서버 권장값까지 물러난다.

        사용자가 고른 값은 고정 주기가 아니라 하한이다. 체결이 멎은 구간에서
        같은 값을 반복해 받아 오는 빈 요청이 차단 위험만 키우기 때문이다.

        adaptive=False (KIS) 면 그 타협이 필요 없어 고른 값을 그대로 쓴다.
        """
        if not any(q.get("market_open") for q in quotes.values()):
            self._volumes.clear()   # 장이 닫히면 기준점을 버린다
            return INTERVAL_CLOSED

        if self._session() == "between":
            # 정규장과 애프터마켓 사이. 값이 고정이라 빨리 물어볼 이유가 없다.
            # 기준점은 갱신해 둔다 -- 16:00 에 애프터마켓이 열리는 순간의 첫
            # 관측을 "방금 급증했다"로 읽지 않게 한다.
            self._saw_trade(quotes)
            return INTERVAL_IDLE

        slow = float(server_interval) if server_interval else INTERVAL_OPEN
        fast = _clean_interval(self.get_override())

        if not adaptive:
            # 기준점은 그래도 갱신해 둔다. KIS 가 잠깐 실패해 네이버로 떨어질 때
            # 그 첫 관측을 "방금 체결됐다"로 읽지 않게 하기 위해서다.
            self._saw_trade(quotes)
            return fast if fast is not None else slow

        if fast is None or fast >= slow:
            # 설정이 없거나 권장보다 느리게 쓰겠다면 적응할 이유가 없다.
            return fast if fast is not None else slow

        if self._saw_trade(quotes):
            return fast
        return min(slow, self.interval + ADAPT_STEP)

    def _saw_trade(self, quotes):
        """직전 순환 이후 실제 체결이 있었나. 기준점을 함께 갱신한다."""
        traded = False
        for code, q in quotes.items():
            vol = q.get("volume")
            if vol is None:         # Yahoo 폴백. 판단 근거가 없다
                continue
            prev = self._volumes.get(code)
            self._volumes[code] = vol
            # 첫 관측은 기준점으로만 쓴다. 종목을 새로 추가하자마자 누적
            # 거래량이 "늘었다"고 읽혀 1초로 당겨지는 것을 막는다.
            if prev is not None and vol > prev:
                traded = True
        return traded


# ---------------------------------------------------------------------------
# 알림 판정 -- 이미 흐르고 있는 시세를 한 번 더 통과시킬 뿐이다. 새 네트워크
# 호출은 없다. UI 를 모르는 순수 로직으로 두어야 셀프테스트가 가능하다.
# ---------------------------------------------------------------------------

def log_alert(line):
    """.pyw 는 콘솔이 없다. 사후에 무슨 일이 있었는지 볼 곳은 여기뿐이다."""
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with ALERTS_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} | {line}\n")
    except Exception:
        pass


class AlertEngine:
    """가격 조건을 판정한다.

    두 종류를 다룬다.

      목표가    -- 상/하한을 넘으면 한 번 쏘고 그 조건을 config 에서 지운다.
                   상태 파일이 필요 없고, 꺼졌다는 사실이 우클릭 메뉴에 그대로 보인다.
      급변동    -- 관측창 안의 저점 대비 상승(또는 고점 대비 하락)이 임계를 넘으면
                   쏜다. 같은 종목은 하루 한 번까지만.
      상·하한가 -- 상한/하한 상태로 들어가거나 빠져나올 때마다 쏜다.

    판정의 실제 난이도는 "언제 판정하지 않을지"에 있다. 장이 닫혀 있을 때,
    지연 시세일 때, 샘플이 끊겼다 돌아왔을 때는 값이 튀어도 사건이 아니다.
    """

    def __init__(self, cfg, on_fire, save=save_config):
        self.cfg = cfg
        self.on_fire = on_fire            # 발동 1건 -> 전달 계층
        self.save = save
        self.hist = {}                    # code -> deque[(monotonic, price)]
        self.limit = {}                   # code -> {state, pending, count}
        self.burst_sent = self._load_state()

    # -- 상태(급변동 하루 1회) ------------------------------------------------

    @staticmethod
    def _load_state():
        try:
            data = json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8-sig"))
            sent = data.get("burst_sent")
            return {str(k): str(v) for k, v in sent.items()} if isinstance(sent, dict) else {}
        except Exception:
            return {}

    def _save_state(self):
        try:
            ALERT_STATE_PATH.write_text(
                json.dumps({"burst_sent": self.burst_sent},
                           ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # -- 판정 -----------------------------------------------------------------

    def feed(self, quotes, now=None):
        """새로 받은 시세를 먹인다. 발동한 알림 목록을 돌려준다."""
        now = time.monotonic() if now is None else now
        fired = []
        for code, q in quotes.items():
            spec = self.cfg["alerts"].get(code)
            if not spec:
                self.hist.pop(code, None)
                self.limit.pop(code, None)
                continue
            price = q.get("price")
            if price is None:
                continue
            fired += self._check_target(code, q, price, spec)
            fired += self._check_burst(code, q, price, spec, now)
            fired += self._check_limit(code, q, price, spec)
        for ev in fired:
            self.on_fire(ev)
        return fired

    def _check_target(self, code, q, price, spec):
        """목표가. 발동하면 그 방향은 꺼진다."""
        out = []
        hit = []
        if spec.get("above") is not None and price >= spec["above"]:
            hit.append(("above", spec["above"], "돌파"))
        if spec.get("below") is not None and price <= spec["below"]:
            hit.append(("below", spec["below"], "이탈"))
        for key, threshold, verb in hit:
            spec[key] = None
            out.append(self._event(code, q, price,
                                   f"목표가 {fmt_won(threshold)} {verb}"
                                   f" — 이 알림은 해제되었습니다"))
        if hit:
            if not any((spec.get("above") is not None,
                        spec.get("below") is not None,
                        spec.get("burst"), spec.get("limit"))):
                self.cfg["alerts"].pop(code, None)
            self.save(self.cfg)
        return out

    def _check_burst(self, code, q, price, spec, now):
        """급변동. 판정하지 않아야 할 상황을 먼저 걸러낸다."""
        if not spec.get("burst"):
            self.hist.pop(code, None)
            return []

        # 1) 장이 닫혀 있으면 종가가 고정이라 판정할 값이 없다.
        # 2) Yahoo 폴백(15분 지연)으로는 "지금 급변동 중"을 말할 수 없다.
        if not q.get("market_open") or q.get("delayed"):
            self.hist.pop(code, None)
            return []

        buf = self.hist.get(code)
        if buf is None:
            buf = self.hist[code] = deque()
        # 3) 절전 복귀·네트워크 단절·장 마감에서 개장으로 넘어온 직후의 첫 샘플이
        #    "5분 만에 3% 급등"으로 오판되는 걸 막는다. 끊겼으면 처음부터 다시 센다.
        if buf and (now - buf[-1][0]) > BURST_GAP_RESET:
            buf.clear()
        buf.append((now, price))

        window = float(self.cfg["burst"]["window_sec"])
        while buf and (now - buf[0][0]) > window:
            buf.popleft()
        if len(buf) < 2:
            return []

        hit = self._burst_hit(buf, price, float(self.cfg["burst"]["threshold_pct"]))
        if not hit:
            return []
        if self.burst_sent.get(code) == today_key():
            return []                      # 종목별 하루 1회

        pct, direction = hit
        self.burst_sent[code] = today_key()
        self._save_state()
        minutes = max(1, int(round(window / 60)))
        word = "급등" if direction > 0 else "급락"
        return [self._event(code, q, price,
                            f"{minutes}분간 {pct:.1f}% {word}", burst=direction)]

    def _check_limit(self, code, q, price, spec):
        """상·하한가. 상태가 바뀔 때마다 쏜다.

        상한가는 하루에도 여러 번 붙었다 풀린다. 그 오감 자체가 알고 싶은
        사건이므로 횟수를 막지 않는다. 대신 호가 한 틱이 오가며 도배하는 것은
        막아야 해서, 새 상태가 연속 LIMIT_CONFIRM 회 관측될 때만 인정한다.
        """
        if not spec.get("limit"):
            self.limit.pop(code, None)
            return []

        new = q.get("limit")
        # Yahoo 폴백은 상·하한가를 알려주지 않는다. 모르는 것을 0(보합)으로
        # 읽으면 "상한가 풀림"을 지어내게 된다. 판정 자체를 건너뛴다.
        if new is None:
            return []

        st = self.limit.setdefault(code, {"state": None, "pending": None,
                                          "count": 0})

        # 장이 닫혀 있으면 기준점을 버린다. 상한가로 마감한 다음 날 아침,
        # 개장 첫 시세를 "상한가 풀림"으로 읽는 것을 막는 장치다. 다시 열리면
        # 첫 관측이 새 기준점이 된다.
        if not q.get("market_open"):
            st["state"], st["pending"], st["count"] = None, None, 0
            return []

        if st["state"] is None:            # 첫 관측은 기준점으로만 삼는다
            st["state"] = new
            return []
        if new == st["state"]:
            st["pending"], st["count"] = None, 0
            return []

        if new == st["pending"]:
            st["count"] += 1
        else:
            st["pending"], st["count"] = new, 1
        if st["count"] < LIMIT_CONFIRM:
            return []

        old = st["state"]
        st["state"], st["pending"], st["count"] = new, None, 0
        return [self._event(code, q, price, _limit_reason(old, new),
                            limit=new)]

    @staticmethod
    def _burst_hit(buf, price, threshold):
        """(변동폭%, 방향) 또는 None.

        극값이 현재보다 **앞선** 시점일 때만 인정한다. 뒤쪽 극값으로 재면 이미
        지나간 움직임을 뒤늦게 쏘게 된다.
        """
        past = list(buf)[:-1]
        if not past:
            return None
        low = min(p for _, p in past)
        high = max(p for _, p in past)
        if low > 0:
            up = (price - low) / low * 100.0
            if up >= threshold:
                return up, 1
        if high > 0:
            down = (high - price) / high * 100.0
            if down >= threshold:
                return down, -1
        return None

    def _event(self, code, q, price, reason, burst=None, limit=None):
        return {
            "code": code,
            "name": q.get("name") or code,
            "price": price,
            "ratio": q.get("ratio", 0.0),
            "direction": q.get("direction", 0),
            "delayed": bool(q.get("delayed")),
            "reason": reason,
            "burst": burst,
            "limit": limit,
            "at": time.strftime("%H:%M:%S"),
        }


def _limit_reason(old, new):
    """전이를 사람 말로. 상한 -> 하한 처럼 한 번에 둘 다 일어날 수도 있다."""
    parts = []
    if old == 1:
        parts.append("상한가 풀림")
    elif old == -1:
        parts.append("하한가 풀림")
    if new == 1:
        parts.append("상한가 도달")
    elif new == -1:
        parts.append("하한가 도달")
    return " · ".join(parts)


def today_key():
    return date.today().isoformat()


def alert_summary(spec):
    """툴팁·메뉴에 한 줄로 보일 알림 설정. 아무것도 없으면 빈 문자열."""
    if not spec:
        return ""
    parts = []
    if spec.get("above") is not None:
        parts.append(f"{float(spec['above']):,.0f}↑")
    if spec.get("below") is not None:
        parts.append(f"{float(spec['below']):,.0f}↓")
    if spec.get("burst"):
        parts.append("급변동")
    if spec.get("limit"):
        parts.append("상·하한가")
    return " · ".join(parts)


def fmt_won(v):
    try:
        return f"{float(v):,.0f}원"
    except (TypeError, ValueError):
        return str(v)


def profit_pct(cost, price):
    """매수 평단가 대비 수익률(%). 부호가 그대로 손익 방향이다."""
    return (float(price) / float(cost) - 1.0) * 100.0


def alert_message(ev):
    """카카오톡 본문. 기본 텍스트 템플릿은 200자까지다."""
    limit = ev.get("limit")
    if limit == 1:
        head = "🚀"                       # 상한가 도달
    elif limit == -1:
        head = "🧊"                       # 하한가 도달
    elif limit == 0:
        head = "🔓"                       # 상·하한가 풀림
    elif ev.get("burst"):
        head = "⚡"
    else:
        head = "📈" if ev.get("direction", 0) > 0 else "📉"
    sign = "+" if ev.get("direction", 0) > 0 else ("-" if ev.get("direction", 0) < 0 else "")
    lines = [f"{head} {ev['name']} {fmt_won(ev['price'])} ({sign}{ev.get('ratio', 0):.2f}%)",
             ev["reason"]]
    if ev.get("delayed"):
        lines.append("※ 15분 지연 시세")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 전달 -- UI 스레드에서 네트워크를 타면 위젯이 통째로 멎는다. Poller 와 같은
# 데몬 스레드 + 큐 구조를 쓴다.
# ---------------------------------------------------------------------------

class Notifier(threading.Thread):
    def __init__(self, on_result):
        super().__init__(daemon=True)
        self.q = queue.Queue()
        self.on_result = on_result        # (event, ok, message) -- 워커 스레드에서 호출
        self._stopping = False            # _stop 은 Thread 의 내부 메서드다. Poller 주석 참고.

    def send(self, ev):
        self.q.put(ev)

    def stop(self):
        self._stopping = True
        self.q.put(None)

    def run(self):
        while not self._stopping:
            ev = self.q.get()
            if ev is None:
                break
            ok, msg = self._deliver(ev)
            try:
                self.on_result(ev, ok, msg)
            except Exception:
                pass

    def _deliver(self, ev):
        if kakao is None:
            return False, "kakao.py 를 불러오지 못했습니다"
        if not kakao.is_linked():
            return False, "카카오톡이 연동되어 있지 않습니다"

        text = ev.get("text") or alert_message(ev)
        last = ""
        for attempt in range(SEND_RETRY):
            try:
                kakao.send_text(text, ev.get("code"))
                return True, "전송됨"
            except kakao.TokenExpired as e:
                return False, f"연동 끊김: {e}"      # 재시도해도 소용없다
            except kakao.Fatal as e:
                return False, f"설정 오류: {e}"
            except Exception as e:
                last = str(e)
                if attempt < SEND_RETRY - 1 and not self._stopping:
                    time.sleep(SEND_RETRY_WAIT)
        return False, f"전송 실패: {last}"


class Toast:
    """화면 우하단 팝업.

    PowerShell 로 윈도우 알림 센터를 부르는 방법도 있지만, 실행 정책에 걸리고
    콘솔 창이 튄다. 콘솔 없이 도는 것이 이 위젯의 전제라 tkinter 로 직접 그린다.
    """

    def __init__(self, root):
        self.root = root
        self.stack = []

    def show(self, title, body, color=COLOR_FLAT):
        try:
            self._show(title, body, color)
        except Exception:
            pass

    def _show(self, title, body, color):
        while len(self.stack) >= TOAST_MAX:
            self._close(self.stack[0])

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#2a2a2a")
        frame = tk.Frame(win, bg=COLOR_BG, padx=14, pady=10)
        frame.pack(padx=1, pady=1)
        tk.Label(frame, text=title, bg=COLOR_BG, fg=color,
                 font=(FONT_FAMILY, 11, "bold"), anchor="w",
                 justify="left").pack(anchor="w")
        tk.Label(frame, text=body, bg=COLOR_BG, fg="#cccccc",
                 font=(FONT_FAMILY, 9), anchor="w",
                 justify="left").pack(anchor="w", pady=(3, 0))

        win.update_idletasks()
        self.stack.append(win)
        self._layout()
        for w in (win, frame):
            w.bind("<Button-1>", lambda e, t=win: self._close(t))
        win.after(TOAST_MS, lambda: self._close(win))

    def _layout(self):
        vx, vy, vw, vh = virtual_screen_rect()
        y = vy + vh - 60
        for win in reversed(self.stack):
            try:
                w, h = win.winfo_width(), win.winfo_height()
                y -= h + 8
                win.geometry(f"+{vx + vw - w - 24}+{y}")
            except Exception:
                pass

    def _close(self, win):
        if win in self.stack:
            self.stack.remove(win)
        try:
            win.destroy()
        except Exception:
            pass
        self._layout()


# ---------------------------------------------------------------------------
# 툴팁 -- 종목명을 껐을 때 어느 숫자가 어느 종목인지 확인하는 수단이자,
# 지연 시세임을 알리는 통로.
# ---------------------------------------------------------------------------

class Tooltip:
    DELAY = 500     # 마우스가 스치기만 해도 깜빡이지 않도록 머무름을 요구한다
    WARM = 1.5      # 방금까지 떠 있었으면 지연 없이 바로 다시 띄운다

    def __init__(self, root, text_provider):
        self.root = root
        self.text_provider = text_provider
        self.win = None
        self._after = None
        self._alive_after = None
        self._code = None
        self._shown_at = 0.0

    def pending(self):
        return self._after is not None

    def schedule(self, code, widget):
        # 시세가 갱신되면 창 폭이 변해 창이 밀리고, 그때 Tk 가 Leave 를 쏘아
        # 툴팁이 취소된다. 방금 떠 있던 툴팁이라면 지연 없이 되살려서 사용자
        # 눈에는 끊기지 않은 것처럼 보이게 한다.
        warm = (time.monotonic() - self._shown_at) < self.WARM
        self.cancel()
        self._code = code
        self._after = self.root.after(0 if warm else self.DELAY,
                                      lambda: self._show(widget))

    def cancel(self):
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        if self._alive_after is not None:
            try:
                self.root.after_cancel(self._alive_after)
            except Exception:
                pass
            self._alive_after = None
        self.hide()

    def hide(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None

    def _show(self, widget):
        self._after = None
        try:
            px, py = widget.winfo_pointerxy()
            lx, ly = widget.winfo_rootx(), widget.winfo_rooty()
            w, h = widget.winfo_width(), widget.winfo_height()
            if not (lx <= px < lx + w and ly <= py < ly + h):
                return
        except tk.TclError:
            return
            
        text = self.text_provider(self._code)
        if not text:
            return
        self.hide()
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#2b2b2b")
        tk.Label(win, text=text, bg="#2b2b2b", fg="#f0f0f0",
                 font=(FONT_FAMILY, 9), padx=8, pady=4).pack()
        win.update_idletasks()
        ww, wh = win.winfo_reqwidth(), win.winfo_reqheight()
        # 툴팁도 화면 밖으로 나갈 수 있다 -- 같은 클램프를 재사용한다.
        tx, ty = clamp_position(px + 14, py + 20, ww, wh, min_w=ww, min_h=wh)
        win.geometry(f"+{tx}+{ty}")
        self.win = win
        self._shown_at = time.monotonic()
        self._check_alive(widget)

    def _check_alive(self, widget):
        if self.win is None:
            return
        try:
            px, py = widget.winfo_pointerxy()
            lx, ly = widget.winfo_rootx(), widget.winfo_rooty()
            w, h = widget.winfo_width(), widget.winfo_height()
            if not (lx <= px < lx + w and ly <= py < ly + h):
                self.cancel()
                return
        except tk.TclError:
            self.cancel()
            return
        self._alive_after = self.root.after(200, lambda: self._check_alive(widget))


# ---------------------------------------------------------------------------
# 위젯
# ---------------------------------------------------------------------------

class StockWidget:
    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg
        self.quotes = {}
        self.rows = {}
        self._rendered = {}          # code -> (표시 문자열, 색). 바뀔 때만 다시 그린다.
        self.last_ok = None          # time.monotonic() 기준 마지막 성공 시각
        self.last_error = None
        self.stocks = []
        self.stocks_loading = False
        self.q = queue.Queue()
        self._drag_offset = None
        self.recent_alerts = []      # 최근 발동 내역 (최신이 앞)
        self.kakao_broken = None     # 연동이 끊겼을 때의 사유 문자열

        root.overrideredirect(True)
        root.configure(bg=COLOR_BG)
        root.attributes("-alpha", float(cfg.get("alpha", 0.85)))
        root.attributes("-topmost", bool(cfg.get("topmost", True)))
        root.tk.call("tk", "scaling", screen_dpi() / 72.0)

        self.body = tk.Frame(root, bg=COLOR_BG, padx=8, pady=4)
        self.body.pack()

        self.tooltip = Tooltip(root, self._tooltip_text)
        self.toast = Toast(root)

        # 전송 결과는 워커 스레드에서 돌아온다. tkinter 는 스레드 안전하지 않으므로
        # 시세와 똑같이 큐를 거쳐 메인 스레드로 넘긴다.
        self.notifier = Notifier(
            lambda ev, ok, msg: self.q.put(("sent", (ev, ok, msg))))
        self.notifier.start()
        self.engine = AlertEngine(self.cfg, self._on_alert)
        self.root.bind("<FocusOut>", lambda e: self.tooltip.cancel())

        self.poller = Poller(lambda: self.cfg["codes"], self.q,
                             lambda: self.cfg.get("interval_open"))
        self.poller.start()

        self._build_rows()
        self._place_initial()

        root.after(200, self._drain_queue)
        root.after(1000, self._tick)
        root.after(5000, self._check_offscreen)
        root.after(50, self._load_stocks_async)
        root.after(int(STOCKS_RECHECK * 1000), self._recheck_stocks)

    # -- 배치 ---------------------------------------------------------------

    def _place_initial(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x, y = self.cfg.get("x"), self.cfg.get("y")
        if x is None or y is None or not is_on_screen(int(x), int(y), w, h):
            x, y = default_position(w, h)
        self.root.geometry(f"+{int(x)}+{int(y)}")
        # 첫 실행이면 여기서 config.json 이 만들어진다. 파일이 있어야 사용자가
        # 설정을 들여다보거나 직접 고칠 수 있다.
        self.cfg["x"], self.cfg["y"] = int(x), int(y)
        save_config(self.cfg)

    def _check_offscreen(self):
        """도킹 해제·해상도 변경은 실행 중에 일어난다. 주기적으로 되돌린다."""
        try:
            w, h = self.root.winfo_width(), self.root.winfo_height()
            x, y = self.root.winfo_x(), self.root.winfo_y()
            if not is_on_screen(x, y, w, h):
                nx, ny = default_position(w, h)
                self.root.geometry(f"+{nx}+{ny}")
                self.cfg["x"], self.cfg["y"] = nx, ny
                save_config(self.cfg)
        except Exception:
            pass
        if self.cfg.get("topmost", True):
            restack_topmost(self.root)
            # 툴팁·토스트는 위젯보다 위에 있어야 한다. 위젯을 올린 뒤 되올린다.
            for sub in self.toast.stack + [self.tooltip.win]:
                if sub is not None:
                    restack_topmost(sub)
        self.root.after(5000, self._check_offscreen)

    # -- 행 구성 -------------------------------------------------------------

    def _build_rows(self):
        self.tooltip.cancel()
        for child in self.body.winfo_children():
            child.destroy()
        self.rows.clear()
        self._rendered.clear()
        size = int(self.cfg.get("font_size", 11))
        font = (FONT_FAMILY, size, "bold")

        codes = self.cfg["codes"]
        visible_codes = [c for c in codes if c not in self.cfg.get("hidden_codes", [])]

        if not visible_codes:
            lbl = tk.Label(self.body, text="＋ 종목 추가", bg=COLOR_BG,
                           fg=COLOR_FLAT, font=font, anchor="w")
            lbl.pack(anchor="w")
            self._bind_row(lbl, None)
            self._resize()
            return

        for code in visible_codes:
            lbl = tk.Label(self.body, text=code, bg=COLOR_BG, fg=COLOR_FLAT,
                           font=font, anchor="w")
            lbl.pack(anchor="w")
            self.rows[code] = lbl
            self._bind_row(lbl, code)
        self._refresh_rows()

    def _bind_row(self, widget, code):
        widget.bind("<Button-1>", self._on_press)
        widget.bind("<B1-Motion>", self._on_drag)
        widget.bind("<ButtonRelease-1>", self._on_release)
        if code:
            widget.bind("<Shift-Button-1>", lambda e, c=code: self._on_reorder_press(e, c))
            widget.bind("<Shift-B1-Motion>", self._on_reorder_drag)
            widget.bind("<Shift-ButtonRelease-1>", self._on_reorder_release)
        widget.bind("<Button-3>", self._on_menu)
        widget.bind("<Enter>",
                    lambda e, c=code, w=widget: self.tooltip.schedule(c, w))
        widget.bind("<Leave>", lambda e: self.tooltip.cancel())

    def _resize(self):
        self.root.update_idletasks()
        self.root.geometry("")      # 내용에 맞춰 창을 다시 재게 한다
        self.root.update_idletasks()
        # 폭이 늘어나 화면 밖으로 삐져나갔으면 되민다. 우상단에 붙여둔 위젯에
        # 시세가 도착하거나 종목을 추가하면 오른쪽으로 자라기 때문이다.
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        nx, ny = fit_on_screen(x, y, w, h)
        if (nx, ny) != (x, y):
            self.root.geometry(f"+{nx}+{ny}")
            self.cfg["x"], self.cfg["y"] = nx, ny
            save_config(self.cfg)
        self._rearm_tooltip()

    def _rearm_tooltip(self):
        """창이 다시 그려지면 Tk 가 Leave 를 쏴서 툴팁이 취소된다.

        포인터가 여전히 어떤 줄 위에 있으면 다시 걸어준다. 마우스가 더
        움직이기를 기다리지 않고 지금 위치로 판정한다.
        """
        if self.tooltip.win is not None or self.tooltip.pending():
            return
        try:
            px, py = self.root.winfo_pointerxy()
        except tk.TclError:
            return
        for code, lbl in self.rows.items():
            lx, ly = lbl.winfo_rootx(), lbl.winfo_rooty()
            if (lx <= px < lx + lbl.winfo_width()
                    and ly <= py < ly + lbl.winfo_height()):
                self.tooltip.schedule(code, lbl)
                return

    # -- 렌더링 --------------------------------------------------------------

    @staticmethod
    def _fmt_price(v):
        return f"{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}"

    def _render_line(self, code):
        """켜진 항목만 이어 붙인다. 꺼진 항목은 자리를 남기지 않는다."""
        show = self.cfg["show"]
        q = self.quotes.get(code)
        parts = []
        if show.get("name"):
            parts.append(q["name"] if q else code)
        if q is None:
            if not parts:
                parts.append(code)
            parts.append("…")
            return "  ".join(parts)
        if show.get("price"):
            parts.append(self._fmt_price(q["price"]) + ("*" if q["delayed"] else ""))
        if show.get("ratio"):
            mark = {1: "▲", -1: "▼", 0: "·"}[q["direction"]]
            parts.append(f"{mark}{q['ratio']:.2f}%")
        if show.get("profit"):
            # 오늘 등락률과 나란히 서므로 괄호로 구분한다. 평단가를 넣지 않은
            # 종목은 자리를 남기지 않는다.
            h = self.cfg["holdings"].get(code)
            if h:
                parts.append(f"({profit_pct(h['cost'], q['price']):+.2f}%)")
        if not parts:              # 수익률만 켜 두고 평단가가 없는 종목
            parts.append(code)
        return "  ".join(parts)

    def _row_color(self, code):
        if self.is_stale():
            return COLOR_STALE
        q = self.quotes.get(code)
        if q is None:
            return COLOR_FLAT
        # 등락률을 꺼도 색으로 방향을 알 수 있어야 한다.
        return {1: COLOR_UP, -1: COLOR_DOWN, 0: COLOR_FLAT}[q["direction"]]

    def _refresh_rows(self):
        """바뀐 줄만 다시 그린다.

        1초마다 무조건 창 크기를 다시 재면 창이 미세하게 흔들리고, 마우스가
        올라가 있을 때 Leave 가 발생해 툴팁이 뜨기 전에 취소된다. 실제로 글자가
        바뀐 경우에만 크기를 다시 잰다.
        """
        changed = False
        for code, lbl in self.rows.items():
            text, color = self._render_line(code), self._row_color(code)
            if self._rendered.get(code) != (text, color):
                lbl.configure(text=text, fg=color)
                self._rendered[code] = (text, color)
                changed = True
        if changed:
            self._resize()

    # -- 신선도 --------------------------------------------------------------

    def is_stale(self):
        """스레드가 멎어도 죽은 값이 살아 있는 것처럼 보이지 않게 한다.

        판정을 네트워크 스레드의 건강 상태에서 분리하는 것이 핵심이다.
        절전 복귀 시 시계가 점프하므로 monotonic 을 쓴다.
        """
        if self.last_ok is None:
            return self.quotes == {} and self.last_error is not None
        limit = max(STALE_FACTOR * self.poller.interval, STALE_MIN)
        return (time.monotonic() - self.last_ok) > limit

    def _tick(self):
        self._refresh_rows()
        self.root.after(1000, self._tick)

    def _drain_queue(self):
        updated = False
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "ok":
                    self.quotes.update(payload)
                    self.last_ok = time.monotonic()
                    self.last_error = None
                    self._check_alerts(payload)
                    updated = True
                elif kind == "sent":
                    self._on_send_result(*payload)
                else:
                    self.last_error = payload
                    updated = True
        except queue.Empty:
            pass
        if updated:
            self._refresh_rows()
        self.root.after(200, self._drain_queue)

    # -- 알림 ----------------------------------------------------------------

    def _check_alerts(self, quotes):
        """새 시세를 판정 엔진에 먹인다. 판정이 죽어도 위젯은 계속 돌아야 한다."""
        if not self.cfg.get("alerts"):
            return
        try:
            self.engine.feed(quotes)
        except Exception as e:
            log_alert(f"판정 오류 | {type(e).__name__}: {e}")

    def _on_alert(self, ev):
        """발동. 화면에는 지금 띄우고, 카카오톡은 워커에 맡긴다."""
        self.recent_alerts.insert(0, ev)
        del self.recent_alerts[RECENT_MAX:]
        color = COLOR_UP if ev.get("direction", 0) > 0 else (
            COLOR_DOWN if ev.get("direction", 0) < 0 else COLOR_FLAT)
        self.toast.show(f"{ev['name']}  {fmt_won(ev['price'])}", ev["reason"], color)
        log_alert(f"{ev['code']} {ev['name']} | {fmt_won(ev['price'])} | {ev['reason']}")
        self.notifier.send(ev)

    def _on_send_result(self, ev, ok, msg):
        log_alert(f"{ev['code']} | 카카오 {'성공' if ok else '실패'} | {msg}")
        if ok:
            self.kakao_broken = None
            return
        self.kakao_broken = msg
        # 화면 알림은 이미 떴다. 여기서는 "폰으로는 못 갔다"만 알린다.
        self.toast.show("카카오톡 전송 실패", msg, COLOR_FLAT)

    # -- 툴팁 ----------------------------------------------------------------

    def _tooltip_text(self, code):
        if code is None:
            return "우클릭 → 종목 추가"
        q = self.quotes.get(code)
        if q is None:
            return f"{code} · 불러오는 중"
        lines = [f"{q['name']} ({code})"]
        if q["delayed"]:
            lines.append("Yahoo 폴백 · 15분 지연 시세")
        elif self.poller.source == "kis":
            lines.append("한국투자증권 · 실시간")
        if self.last_ok is not None:
            age = int(time.monotonic() - self.last_ok)
            if age >= 60:
                lines.append(f"마지막 갱신 {age // 60}분 전")
            elif self.is_stale():
                lines.append(f"마지막 갱신 {age}초 전")
        if self.last_error:
            lines.append("연결 실패 · 재시도 중")
        h = self.cfg["holdings"].get(code)
        if h:
            line = f"평단 {fmt_won(h['cost'])}"
            if h.get("qty"):
                line += f" · {h['qty']:g}주"
            lines.append(line)
            line = f"수익률 {profit_pct(h['cost'], q['price']):+.2f}%"
            if h.get("qty"):
                line += f" · 평가손익 {(q['price'] - h['cost']) * h['qty']:+,.0f}원"
            lines.append(line)
        summary = alert_summary(self.cfg["alerts"].get(code))
        if summary:
            lines.append(f"알림: {summary}")
        if self.kakao_broken:
            lines.append("⚠ 카카오 연동 끊김 — 우클릭 → 가격 알림")
        if self.kis_broken:
            # 조용히 네이버로 돌고 있다는 사실 자체를 알려야 한다.
            lines.append("⚠ KIS 조회 실패 — 우클릭 → 시세 출처")
        return "\n".join(lines)

    # -- 드래그 --------------------------------------------------------------

    def _on_press(self, e):
        self.tooltip.cancel()
        self._drag_offset = (e.x_root - self.root.winfo_x(),
                             e.y_root - self.root.winfo_y())

    def _on_drag(self, e):
        if not self._drag_offset:
            return
        nx = e.x_root - self._drag_offset[0]
        ny = e.y_root - self._drag_offset[1]
        nx, ny = clamp_position(nx, ny,
                                self.root.winfo_width(), self.root.winfo_height())
        self.root.geometry(f"+{nx}+{ny}")

    def _on_release(self, _e):
        self._drag_offset = None
        self.cfg["x"] = self.root.winfo_x()
        self.cfg["y"] = self.root.winfo_y()
        save_config(self.cfg)

    # -- 순서 변경 -----------------------------------------------------------

    def _on_reorder_press(self, e, code):
        self.tooltip.cancel()
        self._reorder_code = code
        self._reorder_widget = e.widget
        e.widget.config(bg="#333333")

    def _on_reorder_drag(self, e):
        pass

    def _on_reorder_release(self, e):
        if not hasattr(self, "_reorder_code") or not self._reorder_code:
            return
            
        code = self._reorder_code
        drop_y = e.y_root
        codes = self.cfg["codes"]
        
        if code in codes:
            codes.remove(code)
            visible_after = [c for c in codes if c not in self.cfg.get("hidden_codes", [])]
            
            target_idx = 0
            for i, c in enumerate(visible_after):
                lbl = self.rows.get(c)
                if lbl:
                    lbl_y = lbl.winfo_rooty()
                    lbl_h = lbl.winfo_height()
                    if drop_y > lbl_y + lbl_h / 2:
                        target_idx = i + 1
                        
            if target_idx < len(visible_after):
                target_code = visible_after[target_idx]
                actual_insert_idx = codes.index(target_code)
                codes.insert(actual_insert_idx, code)
            else:
                codes.append(code)
                
        self._reorder_code = None
        if hasattr(self, "_reorder_widget") and self._reorder_widget:
            self._reorder_widget.config(bg=COLOR_BG)
            self._reorder_widget = None
            
        save_config(self.cfg)
        self._build_rows()

    # -- 메뉴 ----------------------------------------------------------------

    def _on_menu(self, e):
        self.tooltip.cancel()
        m = tk.Menu(self.root, tearoff=0)

        show_menu = tk.Menu(m, tearoff=0)
        labels = [("name", "종목명"), ("price", "가격"), ("ratio", "등락률"),
                  ("profit", "수익률")]
        on_count = sum(1 for k, _ in labels if self.cfg["show"].get(k))
        for key, label in labels:
            var = tk.BooleanVar(value=self.cfg["show"].get(key, True))
            # 마지막 하나까지 끄면 아무것도 안 남는다.
            locked = var.get() and on_count == 1
            show_menu.add_checkbutton(
                label=label, variable=var,
                state="disabled" if locked else "normal",
                command=lambda k=key, v=var: self._toggle_show(k, v))
            show_menu._vars = getattr(show_menu, "_vars", [])
            show_menu._vars.append(var)      # GC 방지
        m.add_cascade(label="표시 항목", menu=show_menu)
        m.add_separator()

        m.add_command(label="종목 추가…", command=self._add_stock)
        if self.cfg["codes"]:
            vis_menu = tk.Menu(m, tearoff=0)
            for code in self.cfg["codes"]:
                q = self.quotes.get(code)
                name = q["name"] if q else code
                is_hidden = code in self.cfg.get("hidden_codes", [])
                label = f"{name} ({code}) (숨김)" if is_hidden else f"{name} ({code})"
                vis_menu.add_command(label=label,
                                     command=lambda c=code: self._toggle_stock_visibility(c))
            m.add_cascade(label="종목 숨김/표시", menu=vis_menu)

            del_menu = tk.Menu(m, tearoff=0)
            for code in self.cfg["codes"]:
                q = self.quotes.get(code)
                name = q["name"] if q else code
                del_menu.add_command(label=f"{name} ({code})",
                                     command=lambda c=code: self._remove_stock(c))
            m.add_cascade(label="종목 삭제", menu=del_menu)
        m.add_separator()

        if self.cfg["codes"]:
            m.add_cascade(label="매수 평단가", menu=self._build_holding_menu(m))
        m.add_cascade(label="가격 알림", menu=self._build_alert_menu(m))
        m.add_separator()

        alpha_menu = tk.Menu(m, tearoff=0)
        for pct in (10, 20, 30, 40, 50, 70, 85, 100):
            alpha_menu.add_command(
                label=f"{pct}%" + ("  ✓" if abs(self.cfg["alpha"] - pct / 100) < .01 else ""),
                command=lambda p=pct: self._set_alpha(p / 100))
        m.add_cascade(label="투명도", menu=alpha_menu)

        font_menu = tk.Menu(m, tearoff=0)
        for size in (9, 11, 14, 18, 24):
            font_menu.add_command(
                label=f"{size}pt" + ("  ✓" if self.cfg["font_size"] == size else ""),
                command=lambda s=size: self._set_font(s))
        m.add_cascade(label="글꼴 크기", menu=font_menu)

        cur = self.cfg.get("interval_open")
        # KIS 로 도는 중이면 고른 값이 하한이 아니라 그대로 주기다. 같은 메뉴가
        # 소스에 따라 다른 뜻이 되므로 라벨로 그 차이를 말해 준다.
        kis_on = self._kis_linked()
        rate_menu = tk.Menu(m, tearoff=0)
        for sec in INTERVAL_CHOICES:
            if sec is None:
                label = "서버 권장 (7초) 고정"
            else:
                label = f"{sec:g}초 고정" if kis_on else f"최고 {sec:g}초"
            if sec is not None and sec <= 2.0 and not kis_on:
                label += "  ⚠"
            rate_menu.add_command(label=label + ("  ✓" if cur == sec else ""),
                                  command=lambda s=sec: self._set_interval(s))
        m.add_cascade(label="갱신 주기", menu=rate_menu)
        m.add_cascade(label="시세 출처", menu=self._build_source_menu(m))

        top_var = tk.BooleanVar(value=bool(self.cfg.get("topmost", True)))
        m.add_checkbutton(label="항상 위", variable=top_var,
                          command=lambda: self._set_topmost(top_var))
        m._top_var = top_var
        m.add_separator()
        m.add_command(label="위치 초기화", command=self._reset_position)
        m.add_command(label="종료", command=self._quit)

        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _toggle_show(self, key, var):
        self.cfg["show"][key] = bool(var.get())
        if not any(self.cfg["show"].values()):
            self.cfg["show"][key] = True
        save_config(self.cfg)
        self._refresh_rows()

    def _set_alpha(self, a):
        self.cfg["alpha"] = a
        self.root.attributes("-alpha", a)
        save_config(self.cfg)

    def _set_font(self, size):
        self.cfg["font_size"] = size
        save_config(self.cfg)
        self._build_rows()

    def _set_interval(self, sec):
        """장중 갱신 주기. 서버 권장보다 빠르게 고르면 그 뜻을 분명히 알린다.

        KIS 로 돌고 있으면 경고하지 않는다. 그 경고는 "한도를 모르는 서버의 권장
        주기를 무시한다"는 뜻인데, 공식 API 에서는 해당하지 않는 말이다.
        """
        if sec is not None and sec <= 2.0 and not self._kis_linked():
            per_day = int(6.5 * 3600 / sec)
            if not messagebox.askyesno(
                    "갱신 주기",
                    f"{sec:g}초로 두면 장중 하루 약 {per_day:,}회 요청합니다.\n"
                    f"(서버 권장 7초일 때는 약 3,300회)\n\n"
                    f"네이버가 응답에 담아 보내는 권장 주기를 무시하는 것이라\n"
                    f"차단될 수 있습니다. 차단되면 Yahoo 폴백으로 넘어가는데\n"
                    f"그쪽은 15분 지연 시세입니다.\n\n"
                    f"{sec:g}초로 하시겠습니까?",
                    parent=self.root):
                return
        self.cfg["interval_open"] = sec
        save_config(self.cfg)
        self.poller.wake()               # 다음 순환부터 새 주기가 적용된다
        if sec is None:
            body = "서버 권장 주기를 따릅니다 (장중 7초)"
        elif self._kis_linked():
            body = f"장중 {sec:g}초마다 갱신합니다"
        else:
            body = f"장중 최고 {sec:g}초 — 체결이 있을 때만 이 주기입니다"
        self.toast.show("갱신 주기 변경", body)

    def _set_topmost(self, var):
        self.cfg["topmost"] = bool(var.get())
        self.root.attributes("-topmost", self.cfg["topmost"])
        if self.cfg["topmost"]:
            restack_topmost(self.root)
        save_config(self.cfg)

    def _reset_position(self):
        self.root.update_idletasks()
        x, y = default_position(self.root.winfo_width(), self.root.winfo_height())
        self.root.geometry(f"+{x}+{y}")
        self.cfg["x"], self.cfg["y"] = x, y
        save_config(self.cfg)

    def _quit(self):
        self.poller.stop()
        self.notifier.stop()
        self.root.destroy()

    # -- 종목 편집 -----------------------------------------------------------

    def _load_stocks_async(self, recheck=False):
        """첫 검색 전에 미리 캐시를 채워둔다(1.2MB 다운로드는 최초 1회뿐).

        recheck=True 는 _recheck_stocks 의 주기 호출이다. 목록을 이미 쥐고
        있어도 캐시 나이를 다시 보게 한다.
        """
        if self.stocks_loading or (self.stocks and not recheck):
            return
        self.stocks_loading = True

        def work():
            items = load_stock_list()
            # KIS 는 시세에 종목명을 주지 않으니 이 캐시가 이름의 출처다. UI 를
            # 거치지 않는 순수 딕셔너리라 워커에서 바로 채운다 -- 메인 스레드를
            # 기다리면 첫 몇 순환이 종목코드로 표시된다.
            remember_names(items)

            def done():
                # load_stock_list 는 실패하면 빈 목록을 준다. 주기 재확인이
                # 하필 네트워크가 끊긴 때 돌아도 쥐고 있던 목록은 지킨다.
                if items:
                    self.stocks = items
                self.stocks_loading = False

            # KRX 다운로드는 느린 날 16초까지 걸린다. 그 사이에 사용자가 종료하면
            # 창이 이미 없어서 after() 가 RuntimeError 를 던진다. 콘솔이 없어
            # 보이지도 않는 예외라 여기서 조용히 접는다.
            try:
                self.root.after(0, done)
            except (RuntimeError, tk.TclError):
                pass
        threading.Thread(target=work, daemon=True).start()

    def _recheck_stocks(self):
        """켜 둔 채 날이 바뀌어도 신규 상장이 검색에 뜨게 한다."""
        self._load_stocks_async(recheck=True)
        self.root.after(int(STOCKS_RECHECK * 1000), self._recheck_stocks)

    def _add_stock(self):
        if not self.stocks:
            # 목록 다운로드는 시작할 때 백그라운드로 걸어두었다. 지난번에
            # 실패했다면 이 호출이 재시도를 건다. 기다리는 동안에도 창은
            # 열어 둔다 -- 6자리 코드로는 지금 바로 등록할 수 있다.
            self._load_stocks_async()
        code = ask_stock(self.root, lambda: self.stocks,
                         on_reload=self._adopt_stocks)
        if code:
            self._append_code(code)

    def _adopt_stocks(self, items):
        """새로고침 버튼이 받아온 목록을 쥔다. 워커 스레드에서 불린다.

        _NAME_CACHE 는 순수 딕셔너리라 여기서 바로 채운다(알림 메시지가 곧장
        새 이름을 쓴다). self.stocks 교체는 메인 스레드로 넘긴다 -- 그려지는
        중에 목록이 바뀌는 일을 만들지 않는다.
        """
        remember_names(items)
        try:
            self.root.after(0, lambda: setattr(self, "stocks", items))
        except (RuntimeError, tk.TclError):
            pass

    def _append_code(self, code):
        if code in self.cfg["codes"]:
            return
        self.cfg["codes"].append(code)
        save_config(self.cfg)
        self._build_rows()
        self.poller.wake()

    def _remove_stock(self, code):
        if code in self.cfg["codes"]:
            self.cfg["codes"].remove(code)
            if code in self.cfg.get("hidden_codes", []):
                self.cfg["hidden_codes"].remove(code)
            self.quotes.pop(code, None)
            self.cfg["alerts"].pop(code, None)
            self.cfg["holdings"].pop(code, None)
            save_config(self.cfg)
            self._build_rows()

    def _toggle_stock_visibility(self, code):
        hidden = self.cfg.setdefault("hidden_codes", [])
        if code in hidden:
            hidden.remove(code)
        else:
            hidden.append(code)
        save_config(self.cfg)
        self._build_rows()

    # -- 보유 평단가 ---------------------------------------------------------

    def _build_holding_menu(self, parent):
        m = tk.Menu(parent, tearoff=0)
        for code in self.cfg["codes"]:
            q = self.quotes.get(code)
            name = q["name"] if q else code
            h = self.cfg["holdings"].get(code)
            if h:
                label = f"{name}  —  {fmt_won(h['cost'])}"
                if h.get("qty"):
                    label += f" x {h['qty']:g}주"
            else:
                label = f"{name}  —  없음"
            m.add_command(label=label + " …",
                          command=lambda c=code: self._edit_holding(c))
        return m

    def _edit_holding(self, code):
        q = self.quotes.get(code)
        new = ask_holding(self.root, q["name"] if q else code, code,
                          q["price"] if q else None,
                          self.cfg["holdings"].get(code) or {})
        if new is None:
            return
        if new:
            self.cfg["holdings"][code] = new
        else:
            self.cfg["holdings"].pop(code, None)
        save_config(self.cfg)
        self._refresh_rows()

    # -- 알림 설정 -----------------------------------------------------------

    def _build_alert_menu(self, parent):
        m = tk.Menu(parent, tearoff=0)

        for code in self.cfg["codes"]:
            q = self.quotes.get(code)
            name = q["name"] if q else code
            summary = alert_summary(self.cfg["alerts"].get(code))
            label = f"{name}  —  {summary}" if summary else f"{name}  —  꺼짐"
            m.add_command(label=label + " …",
                          command=lambda c=code: self._edit_alert(c))
        if self.cfg["codes"]:
            m.add_separator()

        b = self.cfg["burst"]
        m.add_command(
            label=f"급변동 기준…  ({int(b['window_sec']) // 60}분 {b['threshold_pct']}%)",
            command=self._edit_burst)
        m.add_separator()

        if kakao is None:
            m.add_command(label="카카오톡 연동 (kakao.py 없음)", state="disabled")
            return m

        if not kakao.is_linked():
            m.add_command(label="카카오톡 연동…", command=self._setup_kakao)
        else:
            state = "⚠ 끊김 — 다시 연동" if self.kakao_broken else "✓ 연동됨"
            m.add_command(label=f"카카오톡 {state}…", command=self._setup_kakao)
            m.add_command(label="테스트 메시지 보내기", command=self._send_test)
            m.add_command(label="카카오톡 연동 해제", command=self._unlink_kakao)

        if self.recent_alerts:
            recent = tk.Menu(m, tearoff=0)
            for ev in self.recent_alerts:
                recent.add_command(
                    label=f"{ev['at']}  {ev['name']}  {ev['reason']}",
                    command=lambda e=ev: self.toast.show(
                        f"{e['name']}  {fmt_won(e['price'])}", e["reason"]))
            m.add_separator()
            m.add_cascade(label="최근 알림", menu=recent)
        return m

    def _edit_alert(self, code):
        q = self.quotes.get(code)
        spec = self.cfg["alerts"].get(code) or {"above": None, "below": None,
                                                "burst": False, "limit": False}
        new = ask_alert(self.root, q["name"] if q else code, code,
                        q["price"] if q else None, spec)
        if new is None:
            return
        if not any((new["above"] is not None, new["below"] is not None,
                    new["burst"], new["limit"])):
            self.cfg["alerts"].pop(code, None)
        else:
            self.cfg["alerts"][code] = new
        save_config(self.cfg)
        # 기준이 바뀌었으니 관측을 새로 시작한다. 지금 이미 상한가인 종목에
        # 알림을 켰다고 곧바로 "도달"이 울리면 안 되므로 상태도 함께 버린다.
        self.engine.hist.pop(code, None)
        self.engine.limit.pop(code, None)

    def _edit_burst(self):
        cur = self.cfg["burst"]
        raw = ask_text(
            self.root, "급변동 기준",
            "관측 시간(분)과 변동 폭(%)을 쉼표로 입력하세요.\n"
            f"예: 5, 2   →  5분 안에 2% 이상 움직이면 알림\n\n"
            f"지금: {int(cur['window_sec']) // 60}분, {cur['threshold_pct']}%")
        if not raw:
            return
        parts = [p.strip() for p in raw.replace("/", ",").split(",")]
        mins = _to_float(parts[0]) if parts else None
        pct = _to_float(parts[1]) if len(parts) > 1 else None
        if mins is None or pct is None:
            messagebox.showinfo("입력 형식", "'5, 2' 처럼 두 숫자를 쉼표로 나눠 입력하세요.",
                                parent=self.root)
            return
        self.cfg["burst"] = _clean_burst({"window_sec": mins * 60,
                                          "threshold_pct": pct})
        save_config(self.cfg)
        self.engine.hist.clear()
        b = self.cfg["burst"]
        self.toast.show("급변동 기준 변경",
                        f"{int(b['window_sec']) // 60}분 안에 {b['threshold_pct']}% 이상")

    def _setup_kakao(self):
        """이미 연동돼 있으면 그 사실부터 알린다.

        연동은 한 번 하고 잊는 것이라, 멀쩡한데 마법사가 그대로 열리면 사용자는
        뭔가 잘못됐나 싶어진다. 연동이 끊긴 상태에서만 곧장 마법사로 보낸다.
        """
        if kakao is None:
            return
        tokens = kakao.load_tokens()
        if tokens and not self.kakao_broken:
            when = tokens.get("refreshed_at") or tokens.get("linked_at") or "?"
            if not messagebox.askyesno(
                    "카카오톡 연동",
                    f"카카오톡이 이미 연동되어 있습니다.\n\n"
                    f"연동한 날:  {tokens.get('linked_at', '?')}\n"
                    f"마지막 갱신:  {when}\n\n"
                    f"다시 연동하시겠습니까?\n"
                    f"기존 토큰을 덮어씁니다. 잘 되고 있다면 [아니요]를 누르고,\n"
                    f"확인만 하고 싶으면 메뉴의 [테스트 메시지 보내기]를 쓰세요.",
                    parent=self.root):
                return
        if link_kakao(self.root):
            self.kakao_broken = None
            self._send_test()

    # -- 시세 출처 -----------------------------------------------------------

    def _kis_linked(self):
        try:
            return kis is not None and kis.is_linked()
        except Exception:
            return False

    @property
    def kis_broken(self):
        """연동은 돼 있는데 조회가 계속 실패하는 중인가. 사유 문자열 또는 None.

        상태를 위젯에도 따로 두면 두 곳이 어긋난다. 판단 근거는 폴링 스레드가
        실제로 겪은 것 하나뿐이므로 거기서만 읽는다.
        """
        return self.poller.kis_error

    def _build_source_menu(self, parent):
        m = tk.Menu(parent, tearoff=0)
        now = {"kis": "한국투자증권 (실시간)", "naver": "네이버 (실시간)",
               "yahoo": "Yahoo (15분 지연)"}.get(self.poller.source, "연결 중")
        m.add_command(label=f"지금: {now}", state="disabled")
        m.add_separator()

        if kis is None:
            m.add_command(label="한국투자증권 연동 (kis.py 없음)", state="disabled")
            return m

        # .env 에 앱키가 있으면 그것이 정답이다. 그 상태에서 입력창을 또 띄우면
        # 어느 쪽이 쓰이는지 알 수 없게 되므로, 파일을 고치라고 안내만 한다.
        from_env = self._kis_env_keys()

        if from_env:
            state = "⚠ 오류" if self.kis_broken else "✓ 연동됨"
            m.add_command(label=f"한국투자증권 {state}  (.env)", state="disabled")
            m.add_command(label="앱키는 .env 파일에서 읽습니다", state="disabled")
            m.add_command(label=".env 파일 열기…", command=self._open_env)
        elif not self._kis_linked():
            m.add_command(label="한국투자증권 연동…", command=self._setup_kis)
            m.add_command(label=".env 파일 열기…", command=self._open_env)
        else:
            t = kis.load_tokens() or {}
            state = "⚠ 오류 — 다시 연동" if self.kis_broken else "✓ 연동됨"
            m.add_command(label=f"한국투자증권 {state}…", command=self._setup_kis)
            m.add_command(label=f"토큰 발급  {t.get('issued_at', '?')}",
                          state="disabled")
            m.add_command(label="한국투자증권 연동 해제", command=self._unlink_kis)
        return m

    def _kis_env_keys(self):
        try:
            return kis is not None and kis.env_keys() is not None
        except Exception:
            return False

    def _open_env(self):
        """메모장으로 .env 를 연다. 없으면 템플릿을 만들어서 연다.

        콘솔이 없는 위젯이라 "파일을 만들어 여기에 적으세요"라고만 하면 사용자가
        탐색기에서 확장자 없는 파일을 만들어야 한다. 그 과정에서 .env.txt 가 되기
        쉬워서, 만드는 것까지 여기서 한다.
        """
        try:
            if not kis.ENV_PATH.exists():
                kis.ENV_PATH.write_text(kis.ENV_TEMPLATE, encoding="utf-8")
            os.startfile(kis.ENV_PATH)      # noqa: S606 -- 사용자가 고를 편집기로
        except Exception as e:
            messagebox.showerror(".env 열기 실패",
                                 f"{kis.ENV_PATH}\n\n{type(e).__name__}: {e}",
                                 parent=self.root)
            return
        self.toast.show(".env 파일",
                        "앱키를 적고 저장하면 다음 갱신부터 적용됩니다.")

    def _setup_kis(self):
        """앱키를 받아 그 자리에서 토큰 발급까지 해 본다.

        연동은 한 번 하고 잊는 것이라, 멀쩡한데 입력창이 그대로 열리면 사용자는
        뭔가 잘못됐나 싶어진다. 카카오와 같은 규칙으로 먼저 확인한다.
        """
        if kis is None:
            return
        if self._kis_env_keys():
            # .env 를 쓰는 중에 [연동]을 눌렀다면 키를 바꾸겠다는 뜻이다. 앱키를
            # 두 곳에서 받지 않겠다는 약속은 지키되, "파일을 고치세요"라고 알리고
            # 끝내면 막다른 길이다. 그냥 그 파일을 열어 준다.
            self._open_env()
            return
        t = kis.load_tokens()
        if t and not self.kis_broken:
            if not messagebox.askyesno(
                    "한국투자증권 연동",
                    f"이미 연동되어 있습니다.\n\n"
                    f"연동한 날:  {t.get('linked_at', '?')}\n"
                    f"마지막 토큰 발급:  {t.get('issued_at', '?')}\n\n"
                    f"다시 연동하시겠습니까?\n"
                    f"토큰을 새로 발급하면 휴대폰으로 알림톡이 한 통 갑니다.",
                    parent=self.root):
                return
        keys = ask_kis_keys(self.root, (t or {}).get("appkey", ""))
        if not keys:
            return
        try:
            kis.link(*keys)
        except Exception as e:
            log_alert(f"KIS 연동 실패 | {type(e).__name__}: {e}")
            messagebox.showerror("한국투자증권 연동 실패", str(e), parent=self.root)
            return
        self.poller.kis_error = None
        self.poller.wake()               # 다음 순환부터 KIS 로 받는다
        log_alert("KIS 연동 완료")
        self.toast.show("한국투자증권 연동됨",
                        "공식 실시간 시세로 받습니다. 갱신 주기가 그대로 적용됩니다.")

    def _unlink_kis(self):
        if not messagebox.askyesno(
                "연동 해제",
                "저장된 KIS 앱키와 토큰을 지웁니다.\n"
                "시세는 네이버로 돌아갑니다(갱신 주기는 다시 하한으로 동작).",
                parent=self.root):
            return
        kis.clear_tokens()
        self.poller.kis_error = None
        self.poller.wake()
        log_alert("KIS 연동 해제")
        self.toast.show("한국투자증권 연동 해제됨", "네이버 시세로 돌아갑니다.")

    def _unlink_kakao(self):
        if not messagebox.askyesno(
                "연동 해제",
                "저장된 카카오 토큰을 지웁니다.\n다시 쓰려면 처음부터 연동해야 합니다.",
                parent=self.root):
            return
        kakao.clear_tokens()
        self.kakao_broken = None
        log_alert("카카오 연동 해제")
        self.toast.show("카카오톡 연동 해제됨", "가격 알림은 화면 팝업으로만 옵니다.")

    def _send_test(self):
        self.notifier.send({
            "code": self.cfg["codes"][0] if self.cfg["codes"] else None,
            "name": "주식 위젯",
            "price": 0,
            "reason": "테스트",
            "at": time.strftime("%H:%M:%S"),
            "text": "🔔 주식 위젯 연동 테스트\n이 메시지가 보이면 가격 알림이 폰으로 옵니다.",
        })
        self.toast.show("테스트 메시지 발송", "폰에서 카카오톡을 확인하세요.")


# ---------------------------------------------------------------------------
# 셀프테스트 -- 급변동은 실제로 일어날 때까지 기다려서 확인할 수가 없다. 가짜
# 시세를 먹여 판정만 따로 검증한다. 장 시간과 무관하게 언제든 돌릴 수 있다.
#
#   python stock_widget.pyw --selftest-alert
# ---------------------------------------------------------------------------

def selftest_alert():
    fails = []

    def quote(price, name="테스트", open_=True, delayed=False, ratio=0.0,
              limit=0):
        return {"code": "000000", "name": name, "price": price, "ratio": ratio,
                "direction": 0, "limit": limit, "market_open": open_,
                "delayed": delayed, "volume": None}

    OFF = {"above": None, "below": None, "burst": False, "limit": False}

    def engine(alerts, burst=None):
        cfg = {"alerts": alerts,
               "burst": burst or {"window_sec": 300, "threshold_pct": 2.0}}
        got = []
        e = AlertEngine(cfg, got.append, save=lambda c: None)
        e.burst_sent = {}
        e._save_state = lambda: None
        return e, cfg, got

    def check(label, ok):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            fails.append(label)

    # 1) 관측창 안에서 임계를 넘는 상승 -> 발동
    e, cfg, got = engine({"000000": {"above": None, "below": None, "burst": True}})
    for i, p in enumerate([10000, 10050, 10100, 10150, 10260]):
        e.feed({"000000": quote(p)}, now=1000.0 + i * 30)
    check("5분 안 2.6% 상승 → 발동", len(got) == 1)

    # 2) 같은 종목 같은 날 두 번째 급등 -> 미발동
    before = len(got)
    for i, p in enumerate([10300, 10600]):
        e.feed({"000000": quote(p)}, now=1200.0 + i * 30)
    check("같은 날 두 번째 급등 → 미발동 (하루 1회)", len(got) == before)

    # 3) 샘플이 끊겼다 돌아온 직후의 점프 -> 미발동 (링버퍼 리셋)
    e, cfg, got = engine({"000000": {"above": None, "below": None, "burst": True}})
    e.feed({"000000": quote(10000)}, now=1000.0)
    e.feed({"000000": quote(10400)}, now=1000.0 + BURST_GAP_RESET + 5)
    check("60초 공백 뒤 4% 점프 → 미발동 (링버퍼 리셋)", not got)

    # 4) 장 마감 중 -> 미발동
    e, cfg, got = engine({"000000": {"above": None, "below": None, "burst": True}})
    for i, p in enumerate([10000, 10500]):
        e.feed({"000000": quote(p, open_=False)}, now=1000.0 + i * 30)
    check("장 마감 중 → 미발동", not got)

    # 5) Yahoo 폴백(15분 지연) -> 미발동
    e, cfg, got = engine({"000000": {"above": None, "below": None, "burst": True}})
    for i, p in enumerate([10000, 10500]):
        e.feed({"000000": quote(p, delayed=True)}, now=1000.0 + i * 30)
    check("지연 시세 → 미발동", not got)

    # 6) 하락도 잡는다
    e, cfg, got = engine({"000000": {"above": None, "below": None, "burst": True}})
    for i, p in enumerate([10000, 9900, 9800, 9700]):
        e.feed({"000000": quote(p)}, now=1000.0 + i * 30)
    check("3% 하락 → 발동", len(got) == 1 and got[0]["burst"] == -1)

    # 7) 목표가 도달 -> 발동하고 그 조건이 사라진다
    e, cfg, got = engine({"000000": {"above": 10200, "below": None, "burst": False}})
    e.feed({"000000": quote(10100)}, now=1000.0)
    check("목표가 미달 → 미발동", not got)
    e.feed({"000000": quote(10250)}, now=1030.0)
    check("목표가 돌파 → 발동", len(got) == 1)
    check("발동한 목표가는 config 에서 사라진다", "000000" not in cfg["alerts"])
    e.feed({"000000": quote(10300)}, now=1060.0)
    check("해제된 뒤에는 다시 안 울린다", len(got) == 1)

    # 8) 하한 이탈
    e, cfg, got = engine({"000000": {"above": None, "below": 9000, "burst": False}})
    e.feed({"000000": quote(8900)}, now=1000.0)
    check("하한 이탈 → 발동", len(got) == 1)

    # 8b) 목표가가 다 소진돼도 다른 알림이 켜져 있으면 항목을 지우지 않는다
    e, cfg, got = engine({"000000": dict(OFF, above=10200, limit=True)})
    e.feed({"000000": quote(10250)}, now=1000.0)
    check("목표가 소진 + 상·하한가 켜짐 → 항목 유지",
          cfg["alerts"].get("000000", {}).get("limit") is True)

    # 9) 설정이 없는 종목은 건드리지 않는다
    e, cfg, got = engine({})
    e.feed({"000000": quote(10000)}, now=1000.0)
    check("알림 미설정 종목 → 아무 일 없음", not got)

    # -- 상·하한가 -----------------------------------------------------------

    def limit_engine():
        return engine({"000000": dict(OFF, limit=True)})

    def feed(e, states, base=1000.0):
        for i, s in enumerate(states):
            e.feed({"000000": quote(13000, limit=s)}, now=base + i * 10)

    # 10) 이미 상한가인 상태로 시작 -> 첫 관측은 기준점일 뿐이다
    e, cfg, got = limit_engine()
    feed(e, [1, 1, 1])
    check("켤 때 이미 상한가 → 미발동 (첫 관측은 기준점)", not got)

    # 11) 보합에서 상한가로 -> 확인 후 발동
    e, cfg, got = limit_engine()
    feed(e, [0, 1])
    check(f"상한가 1회 관측 → 아직 미발동 (확인 {LIMIT_CONFIRM}회 필요)", not got)
    feed(e, [1], base=1100.0)
    check("상한가 연속 관측 → 발동", len(got) == 1)
    check("사유가 '상한가 도달'", got and got[0]["reason"] == "상한가 도달")

    # 12) 이어서 풀리면 또 발동한다 (하루 1회 제한 없음)
    feed(e, [0, 0], base=1200.0)
    check("상한가 풀림 → 발동", len(got) == 2 and got[1]["reason"] == "상한가 풀림")
    feed(e, [1, 1], base=1300.0)
    check("다시 붙으면 또 발동 (바뀔 때마다)", len(got) == 3)

    # 13) 한 틱 튀는 것은 무시한다
    e, cfg, got = limit_engine()
    feed(e, [0, 1, 0, 1, 0, 0])
    check("1회씩 오가는 잡음 → 미발동", not got)

    # 14) 하한가도 같은 규칙
    e, cfg, got = limit_engine()
    feed(e, [0, -1, -1])
    check("하한가 도달 → 발동", len(got) == 1 and got[0]["reason"] == "하한가 도달")

    # 15) 장 마감이 끼면 기준점을 버린다 (다음 날 아침의 가짜 '풀림' 차단)
    e, cfg, got = limit_engine()
    feed(e, [0, 1, 1])                                   # 상한가로 마감
    before = len(got)
    e.feed({"000000": quote(13000, limit=1, open_=False)}, now=2000.0)
    e.feed({"000000": quote(11000, limit=0)}, now=2100.0)   # 다음 날 개장
    e.feed({"000000": quote(11000, limit=0)}, now=2110.0)
    check("장 마감 뒤 개장 → 가짜 '풀림' 없음", len(got) == before)

    # 16) Yahoo 폴백은 상·하한가를 모른다 -> 지어내지 않는다
    e, cfg, got = limit_engine()
    feed(e, [0, 1, 1])
    before = len(got)
    for i in range(3):
        e.feed({"000000": quote(13000, limit=None, delayed=True)},
               now=2200.0 + i * 10)
    check("지연 시세(limit 모름) → 미발동", len(got) == before)

    # 17) 알림을 안 켠 종목은 상태를 추적하지도 않는다
    e, cfg, got = engine({"000000": dict(OFF, above=99999)})
    feed(e, [0, 1, 1])
    check("상·하한가 알림 꺼짐 → 미발동", not got and "000000" not in e.limit)

    # 18) 메시지가 카카오 텍스트 템플릿 한도 안에 들어간다
    for ev in ({"name": "삼성전자", "price": 72300, "ratio": 2.41, "direction": 1,
                "reason": "목표가 72,000원 돌파 — 이 알림은 해제되었습니다",
                "delayed": True},
               {"name": "케이앤에스아이앤씨", "price": 24600, "ratio": 29.95,
                "direction": 1, "reason": "상한가 도달", "limit": 1},
               {"name": "케이앤에스아이앤씨", "price": 23100, "ratio": 22.10,
                "direction": 1, "reason": "상한가 풀림", "limit": 0}):
        msg = alert_message(ev)
        check(f"메시지 {len(msg)}자 ≤ 200자", len(msg) <= 200)
        print("\n" + msg)
    print()

    # -- 적응형 갱신 주기 --------------------------------------------------
    # 네트워크를 타지 않는 순수 로직이라 여기서 같이 본다. 빈 요청을 줄이는 것이
    # 목적이므로 "체결이 없을 때 물러나는가"가 핵심이다.
    print()

    def pq(vol, open_=True, code="000000"):
        return {code: {"code": code, "market_open": open_, "volume": vol}}

    def poller(override):
        p = Poller(lambda: ["000000"], queue.Queue(), lambda: override)
        # 장 구분을 PC 시계에 맡기면 15:30~16:00 에 돌릴 때 주기 검사가
        # 통째로 어긋난다. 이 검사들은 정규장 기준이므로 고정해 둔다.
        p._session = lambda: "regular"
        return p

    p = poller(3.0)                     # 하한 3초, 서버 권장 7초
    p.interval = 3.0
    p.interval = p._next_interval(pq(100), 7.0)
    check("첫 관측은 기준점 → 당기지 않는다", p.interval > 3.0)

    p.interval = p._next_interval(pq(150), 7.0)
    check("체결 발생 → 하한(3초)으로 당김", p.interval == 3.0)

    p.interval = p._next_interval(pq(150), 7.0)
    check("체결 없음 → 한 단계 물러남(4초)", p.interval == 4.0)

    for _ in range(10):                 # 계속 체결이 없으면
        p.interval = p._next_interval(pq(150), 7.0)
    check("체결이 계속 없으면 서버 권장까지만 물러남", p.interval == 7.0)

    p.interval = p._next_interval(pq(151), 7.0)
    check("다시 체결 → 즉시 하한으로 복귀", p.interval == 3.0)

    check("설정이 없으면 서버 권장 고정(기존 동작)",
          poller(None)._next_interval(pq(100), 7.0) == 7.0)
    check("하한이 권장보다 느리면 적응하지 않는다",
          poller(10.0)._next_interval(pq(100), 7.0) == 10.0)

    p = poller(1.0)
    p.interval = 1.0
    p.interval = p._next_interval(pq(None), None)
    check("Yahoo 폴백(volume None) → 판단 포기하고 물러남", p.interval > 1.0)

    p = poller(1.0)
    p._volumes["000000"] = 100
    closed = p._next_interval(pq(100, open_=False), 7.0)
    check("장 마감 → 60초 + 기준점 버림", closed == INTERVAL_CLOSED and not p._volumes)

    p = poller(1.0)
    p.interval = 7.0
    two = {"000000": {"market_open": True, "volume": 100},
           "000001": {"market_open": True, "volume": 999999}}
    check("종목을 새로 추가해도 그 첫 관측으로는 당기지 않는다",
          p._next_interval(two, 7.0) > 1.0)

    # -- KIS(공식 API) 고정 주기 -------------------------------------------
    # 적응형은 "한도를 모르는 서버에 빈 요청을 덜 보내자"는 타협이다. 한도가
    # 공개된 공식 API 에서는 그 타협을 하지 않는다 -- 고른 값이 곧 주기다.
    print()
    p = poller(1.0)
    p.interval = 1.0
    p.interval = p._next_interval(pq(100), None, adaptive=False)
    check("KIS: 첫 관측에도 고른 주기 그대로", p.interval == 1.0)
    p.interval = p._next_interval(pq(100), None, adaptive=False)
    check("KIS: 체결이 없어도 물러나지 않는다", p.interval == 1.0)
    check("KIS: 기준점은 갱신해 둔다(네이버로 떨어질 때를 위해)",
          p._volumes.get("000000") == 100)
    check("KIS: 설정이 없으면 7초",
          poller(None)._next_interval(pq(100), None, adaptive=False) == 7.0)
    check("KIS: 장 마감이면 소스와 무관하게 60초",
          poller(1.0)._next_interval(pq(100, open_=False), None,
                                     adaptive=False) == INTERVAL_CLOSED)

    # -- KIS 응답 -> quote 계약 --------------------------------------------
    # 두 fetch 가 같은 모양을 내야 판정 엔진이 소스를 몰라도 된다.
    print()

    def kout(sign, prpr="10000", ctrt="-2.5", vol="1234"):
        return {"stck_prpr": prpr, "prdy_ctrt": ctrt,
                "prdy_vrss_sign": sign, "acml_vol": vol}

    q = _kis_quote("000000", kout("1", ctrt="29.9"), True)
    check("KIS 부호 1 → 상한가", q["limit"] == 1 and q["direction"] == 1)
    q = _kis_quote("000000", kout("4", ctrt="-29.9"), True)
    check("KIS 부호 4 → 하한가", q["limit"] == -1 and q["direction"] == -1)
    check("KIS 부호 2 → 상승·상한 아님",
          _kis_quote("000000", kout("2"), True)["limit"] == 0
          and _kis_quote("000000", kout("2"), True)["direction"] == 1)
    check("KIS 부호 3 → 보합",
          _kis_quote("000000", kout("3", ctrt="0"), True)["direction"] == 0)
    check("KIS 부호 5 → 하락",
          _kis_quote("000000", kout("5"), True)["direction"] == -1)
    check("KIS 부호가 비면 limit 은 None (0 으로 읽으면 가짜 '풀림'이 난다)",
          _kis_quote("000000", kout(""), True)["limit"] is None)

    q = _kis_quote("000000", kout("5", ctrt="-2.5", vol="1,234"), True)
    check("KIS 등락률은 절댓값", q["ratio"] == 2.5)
    check("KIS 거래량은 숫자로", q["volume"] == 1234.0)
    check("KIS 는 지연이 아니다", q["delayed"] is False)
    check("KIS 는 장 개장 여부를 받아서 채운다",
          _kis_quote("000000", kout("2"), False)["market_open"] is False)
    check("KIS quote 가 네이버 quote 와 같은 키를 낸다",
          set(q) == set(quote(100)))

    _NAME_CACHE["000000"] = "테스트종목"
    check("이름은 KRX 캐시에서 붙인다",
          _kis_quote("000000", kout("2"), True)["name"] == "테스트종목")
    check("캐시에 없으면 종목코드로",
          _kis_quote("999999", kout("2"), True)["name"] == "999999")
    _NAME_CACHE.clear()

    # -- 상장종목 캐시 신선도 -----------------------------------------------
    # 오늘 상장한 종목이 장 중에 검색되지 않던 원인. 경과 시간이 아니라 "오늘
    # 개장 이후에 받았나" 로 본다.
    print()
    day = time.mktime((2026, 9, 22, 0, 0, 0, 0, 0, -1))
    h = 3600
    check("어제 낮에 받은 캐시는 오늘 개장 후에는 묵은 것",
          _stocks_fresh(day - 12 * h, day + 10 * h) is False)
    check("오늘 개장 후에 받았으면 신선",
          _stocks_fresh(day + 9.5 * h, day + 10 * h) is True)
    check("개장 전이면 어제 개장분까지 인정 — 새벽마다 다시 받지 않는다",
          _stocks_fresh(day - 14 * h, day + 7 * h) is True)
    check("개장 전이라도 그저께 것은 묵은 것",
          _stocks_fresh(day - 38 * h, day + 7 * h) is False)
    check("개장 직후 캐시는 그날 9시 경계에서 신선",
          _stocks_fresh(day + 9 * h, day + 9 * h) is True)
    check("updated 가 없으면(0) 묵은 것으로 본다",
          _stocks_fresh(0, day + 10 * h) is False)

    # -- .env 읽기 ----------------------------------------------------------
    # 사용자가 메모장으로 고치는 파일이다. config.json 과 같은 규칙 -- 못 읽는
    # 값에 예외를 던지지 말고 없는 것으로 친다.
    print()
    if kis is None:
        check("kis.py 가 있어야 .env 를 본다", False)
    else:
        env = kis.parse_env
        check(".env 기본형", env("KIS_APP_KEY=abc\nKIS_APP_SECRET=xyz")
              == {"KIS_APP_KEY": "abc", "KIS_APP_SECRET": "xyz"})
        check(".env 주석과 빈 줄은 건너뛴다",
              env("# 주석\n\nKIS_APP_KEY=abc\n") == {"KIS_APP_KEY": "abc"})
        check(".env 따옴표를 벗긴다", env('K="a b"')["K"] == "a b")
        check(".env export 접두사를 받아넘긴다",
              env("export K=v")["K"] == "v")
        check(".env 값 안의 = 는 살린다", env("K=a=b")["K"] == "a=b")
        check(".env 좌우 공백을 턴다", env("  K  =  v  ")["K"] == "v")
        check(".env = 없는 줄은 버린다", env("그냥 글자\nK=v") == {"K": "v"})
        check(".env 가 비어도 죽지 않는다", env("") == {})
        check(".env 가 None 이어도 죽지 않는다", env(None) == {})

        # -- 장 구분 (2026-09-14 애프터마켓 신설) ---------------------------
        # 실측: 애프터마켓(16:00~20:00)에 값이 실제로 움직이고, 네이버는 그동안
        # 내내 marketStatus=OPEN 을 준다. 정규장만 장중으로 보면 두 소스가 다른
        # 뜻을 내고 목표가 알림이 조용히 멎는다.
        print()

        def at(h, mi, day=15):
            """2026-09-15 는 화요일. 19 일은 토요일."""
            return time.mktime((2026, 9, day, h, mi, 0, 0, 0, -1))

        kis._open_days["20260915"] = True       # 휴장일 API 를 타지 않게
        kis._open_days["20260919"] = True
        sess = kis.session_now

        check("09:00 정규장 시작", sess(at(9, 0)) == "regular")
        check("15:29 아직 정규장", sess(at(15, 29)) == "regular")
        check("15:30 정규장 끝 → 사이 구간", sess(at(15, 30)) == "between")
        check("15:59 아직 사이 구간", sess(at(15, 59)) == "between")
        check("16:00 애프터마켓 시작", sess(at(16, 0)) == "after")
        check("19:59 아직 애프터마켓", sess(at(19, 59)) == "after")
        check("20:00 완전 마감", sess(at(20, 0)) == "closed")
        check("08:59 개장 전", sess(at(8, 59)) == "closed")
        check("토요일은 언제나 마감", sess(at(12, 0, day=19)) == "closed")

        kis._open_days["20260915"] = False
        check("휴장일이면 장중 시각이어도 마감", sess(at(12, 0)) == "closed")
        kis._open_days["20260915"] = True

        check("정규장은 장중", kis.market_open_now(at(10, 0)))
        check("애프터마켓도 장중 (네이버 marketStatus 와 같은 뜻)",
              kis.market_open_now(at(17, 0)))
        check("사이 구간도 장중 (네이버가 OPEN 을 준다)",
              kis.market_open_now(at(15, 45)))
        check("20:00 이후는 장중 아님", not kis.market_open_now(at(21, 0)))
        kis._open_days.clear()

        # 사이 구간의 주기 -- 값이 고정이라 당길 이유가 없다
        p = poller(1.0)
        p._session = lambda: "between"
        p.interval = 1.0
        p.interval = p._next_interval(pq(100), None, adaptive=False)
        check("사이 구간(15:30~16:00) 은 10초로 물러난다",
              p.interval == INTERVAL_IDLE)
        check("사이 구간에도 기준점은 갱신한다(16:00 첫 관측 오판 방지)",
              p._volumes.get("000000") == 100)

        p._session = lambda: "after"
        p.interval = p._next_interval(pq(150), None, adaptive=False)
        check("애프터마켓은 정규장과 같은 주기", p.interval == 1.0)

        p._session = lambda: "closed"
        check("장 마감은 소스·구간과 무관하게 60초",
              p._next_interval(pq(150, open_=False), None,
                               adaptive=False) == INTERVAL_CLOSED)

        ph = kis._is_placeholder
        check("템플릿(한글) 은 안 적은 것으로 본다", ph("여기에앱키를붙여넣으세요"))
        check("빈 값은 안 적은 것", ph("") and ph("   "))
        check("<꺾쇠> 도 안 적은 것", ph("<APP KEY>"))
        check("진짜 앱키는 통과", not ph("PSxAbC123-_kLm"))

    # -- 보유 수익률 --------------------------------------------------------
    # 네트워크도 UI 도 타지 않는 순수 계산·검증이라 여기서 같이 본다.
    print()
    check("평단보다 오르면 양수", abs(profit_pct(10000, 10321) - 3.21) < 1e-9)
    check("평단보다 내리면 음수", abs(profit_pct(10000, 9900) + 1.0) < 1e-9)
    check("평단과 같으면 0", profit_pct(10000, 10000) == 0.0)

    hd = _clean_holdings({"042510": {"cost": "12,345", "qty": 10},
                          "005930": 70000,           # 숫자 하나면 평단가로 본다
                          "000001": {"cost": 0},     # 0원은 수익률을 낼 수 없다
                          "000002": {"cost": 100, "qty": -3},
                          "000003": {"qty": 5},      # 평단가 없는 항목
                          "bad": {"cost": 100}})     # 종목코드 형식이 아니다
    check("문자열 평단가도 읽는다", hd.get("042510", {}).get("cost") == 12345.0)
    check("수량도 함께 읽는다", hd.get("042510", {}).get("qty") == 10.0)
    check("숫자 하나는 평단가로", hd.get("005930", {}).get("cost") == 70000.0)
    check("0원 평단가는 버린다", "000001" not in hd)
    check("음수 수량은 없는 것으로", hd.get("000002", {}).get("qty") is None)
    check("평단가 없는 항목은 버린다", "000003" not in hd)
    check("종목코드 형식이 아니면 버린다", "BAD" not in hd)
    check("못 읽는 값이어도 죽지 않는다", _clean_holdings("엉터리") == {})

    check("설정에 holdings 가 없으면 빈 dict",
          _merge_defaults({"codes": ["005930"]})["holdings"] == {})
    check("표시 항목에 수익률이 들어 있다",
          "profit" in _merge_defaults({})["show"])

    print()
    print("실패 없음" if not fails else f"실패 {len(fails)}건: {fails}")
    return 0 if not fails else 1


# ---------------------------------------------------------------------------

def main():
    if "--selftest-alert" in sys.argv[1:]:
        sys.exit(selftest_alert())

    reset = "--reset" in sys.argv[1:]

    root = tk.Tk()
    root.withdraw()

    cfg = load_config(root)
    if cfg is None:                 # 사용자가 파일을 직접 고치겠다고 선택
        root.destroy()
        return

    if reset:                       # 창을 화면 밖에서 잃어버렸을 때의 탈출구
        cfg["x"] = cfg["y"] = None
        save_config(cfg)

    root.deiconify()
    StockWidget(root, cfg)
    root.mainloop()


if __name__ == "__main__":
    main()
