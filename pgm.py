#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
동업계 셀럽PGM 품목 취합 자동화 스크립트
==========================================

hdmzp.github.io/hdhs (홈쇼핑 시청환경조회) 사이트가 공개하는 JSON 데이터를 이용해
CJ온스타일 / GS샵 / 롯데홈쇼핑의 지정 셀럽 프로그램(대표 PGM) 판매 품목을
'이번 주 / 다음 주' 기준으로 자동 취합해 템플릿 형식으로 출력합니다.

사용된 데이터 소스 (모두 공개 정적 JSON, 로그인/인증 불필요):
  - https://hdmzp.github.io/hdhs/homeshopping/representative_programs/{PROGRAM_KEY}.json
        -> 각 프로그램의 "가장 가까운 예정(아직 방송 전)" 회차 상품 목록
  - https://hdmzp.github.io/hdhs/homeshopping/representative_programs/history/{YYYY-MM}.json
        -> 해당 월에 이미 방송된(종료된) 회차들의 전체 상품 목록 (프로그램별)
  - https://hdmzp.github.io/hdhs/homeshopping/{COMPANY}_live/{YYYY-MM}.json
        -> 홈사(채널)별 시간대별 전체 편성 데이터. category(가전/리빙주방/미용 등) 필드를
           이용해 ★가전 여부를 판별하는 용도로만 사용합니다.

사용법:
    python celeb_pgm_monitor.py                     # 오늘 기준 이번주/다음주 취합, 콘솔 출력 + 파일 저장
    python celeb_pgm_monitor.py --date 2026-08-19    # 기준일 지정
    python celeb_pgm_monitor.py --weeks this         # 이번주만 (this/next/both, 기본 both)
    python celeb_pgm_monitor.py --output report.txt  # 저장 파일명 지정

주의:
  - 사이트가 보유한 데이터 범위 밖(예: 아직 편성이 공개되지 않은 다음 주 화/수요일 등)은
    "(데이터 미공개)"로 표시됩니다. 이는 스크립트 오류가 아니라 원본 사이트에 아직
    해당 회차 정보가 올라오지 않았기 때문입니다.
  - ★ 표기는 원본 사이트의 category 값이 "가전"인 경우에 붙입니다. 혹시 사이트에
    category 정보가 없는 날짜/브랜드가 있으면 보수적으로 별표를 붙이지 않습니다.
  - 매주 월/금 오전 자동 공유가 필요하면, 이 스크립트를 Windows 작업 스케줄러(또는 cron)에
    등록해 실행하고 표준출력/저장 파일을 원하는 방식(이메일, 메신저 등)으로 전달하면 됩니다.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import re
import ssl

BASE_URL = "https://hdmzp.github.io/hdhs/homeshopping"
REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; CelebPGMMonitor/1.0)"

# 회사/기관 네트워크의 보안 프로그램(HTTPS 검사 프록시)이 인증서를 가로채면서
# "CERTIFICATE_VERIFY_FAILED / Missing Authority Key Identifier" 오류가 나는 경우가 있다.
# 그런 경우를 자동 감지하면 이 플래그를 켜고, 이후 요청부터는 인증서 검증을 건너뛴다.
# (내려받는 데이터는 로그인/개인정보 없는 완전 공개 JSON이라 검증을 건너뛰어도 안전하다.)
INSECURE_SSL = False


def _is_cert_error(err: BaseException) -> bool:
    reason = getattr(err, "reason", None)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    text = str(err)
    return "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text


def _ssl_context() -> Optional[ssl.SSLContext]:
    if not INSECURE_SSL:
        return None  # 기본(검증 O) 컨텍스트 사용
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

WEEKDAY_LABEL = ["월", "화", "수", "목", "금", "토", "일"]


@dataclass
class ProgramSpec:
    weekday: int          # 0=월 ... 5=토 (date.weekday() 기준)
    company: str          # CJ / GS / LT / HD
    program_key: str      # 사이트 내부 프로그램 코드 (예: CJ_KJE)
    celeb: str            # 메인 셀럽 이름 (표시용)
    program_title: str    # 프로그램명 (참고용)


# [취합 필요한 PGM] 템플릿 순서 그대로 정의
PROGRAMS: list[ProgramSpec] = [
    ProgramSpec(0, "CJ", "CJ_KJE", "강주은", "굿 라이프"),
    ProgramSpec(1, "CJ", "CJ_KCO", "김창옥", "더 김창옥 라이브"),
    ProgramSpec(1, "CJ", "CJ_KSY", "김신영", "김신영이 산다"),
    ProgramSpec(2, "CJ", "CJ_CHJ", "최화정", "최화정쇼"),
    ProgramSpec(3, "LT", "LT_CYR", "최유라", "최유라쇼"),
    ProgramSpec(3, "GS", "GS_BJY", "백지연", "지금 백지연"),
    ProgramSpec(5, "LT", "LT_CYR", "최유라", "최유라쇼"),
]

APPLIANCE_KEYWORDS = [
    "청소기", "건조기", "세탁기", "냉장고", "전자레인지", "에어프라이어", "드라이어",
    "공기청정기", "제습기", "가습기", "믹서기", "블렌더", "무선청소기", "식기세척기",
    "정수기", "안마의자", "선풍기", "냉난방기", "에어컨", "전기밥솥", "커피머신",
    "제빙기", "음식물처리기", "김치냉장고", "인덕션레인지", "전기포트", "스타일러",
    "다리미", "고데기", "면도기", "이발기", "칫솔살균기", "구강세정기", "워시콤보",
]

# --- 상품명 간소화 규칙 (긴 홍보 문구를 줄여서 "브랜드 + 핵심품목" 형태로 보여주기 위함) ---
# 한국어 상품명은 보통 [브랜드/수식어 ... 핵심품목명]처럼 핵심 단어가 "맨 뒤"에 온다.
# (예: "엑스쿠첸 통5중 저압 냄비" -> 핵심은 맨 뒤 "냄비") 그래서 길이를 줄일 때도
# 앞이 아니라 뒤쪽(핵심 단어가 있는 쪽)을 최대한 살리는 방향으로 자른다.
_BRACKET_RE = re.compile(r"\[[^\]]*\]")          # [최초공개], [국민영양프로젝트] 등
_PAREN_RE = re.compile(r"\([^)]*\)")             # (더블), (24팬) 등
_QTY_UNIT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*"
    r"(?:g|kg|mg|mcg|ml|mL|l|L|cm|mm|매|개월분|개월|개입|개|박스|봉|팩|세트|병|정|캡슐"
    r"|포|주분|주|년|일|회|장|롤|호|종|겹|중|팬|웍|커버|kcal|IU|EA|ea)\b",
    re.IGNORECASE,
)
_TRAILING_NUM_RE = re.compile(r"[\d.\-*]+$")     # 맨 끝에 홀로 남은 숫자/기호 (예: "로얄젤리 5")
_TRAILING_CAPS_RE = re.compile(r"(?:\s+[A-Za-z]{2,}(?=\s|$))+$")  # 끝의 영문 모델명 (예: "ECM ACTIVE")
_TRAILING_VERSION_RE = re.compile(r"(?:\s+\d+(?:\.\d+)+)+$")      # 끝의 버전 숫자 (예: "2.0")
_FILLER_WORDS = [
    "단독", "단품", "특가", "특별가", "한정물량", "최초공개", "신상", "리뉴얼", "기획",
    "구성", "패키지", "사은품", "증정", "무료배송", "방송단독", "방송에서만", "미리주문",
    "본품", "정품", "공식", "공식수입원", "백화점동일", "국민영양프로젝트", "역대급",
    "단한번", "올해단한번", "특집구성", "단독구성", "추가구성", "전고객", "특별구성",
    "세트", "대용량", "만능", "PICK", "NEW", "MAX", "PRO", "VIP", "미리주문10%",
]

DEFAULT_NAME_MAX_LEN = 16


def _keep_tail(s: str, max_len: int) -> str:
    """길이가 넘치면 '앞쪽 단어'부터 지워서 뒤쪽(핵심 단어)을 최대한 보존한다."""
    if len(s) <= max_len:
        return s
    words = s.split(" ")
    while len(words) > 1 and len(" ".join(words)) > max_len:
        words.pop(0)
    result = " ".join(words).strip()
    if len(result) > max_len:
        # 단어 하나가 이미 max_len보다 길면, 그 단어의 뒷부분(핵심에 더 가까운 쪽)을 보존
        result = "…" + result[-(max_len - 1):]
    return result


def simplify_name(name: str, max_len: int = DEFAULT_NAME_MAX_LEN) -> str:
    """'[국민영양프로젝트] 여에스더 포스파티딜세린 5개월분+비타민D+쇼핑백' 같은 상품명을
    '여에스더 포스파티딜세린' 처럼 핵심 품목명 위주로 줄인다. 완벽한 요약은 아니고
    규칙 기반 축약이라, 상품에 따라 다소 어색할 수 있다."""
    s = name
    s = _BRACKET_RE.sub(" ", s)
    s = _PAREN_RE.sub(" ", s)
    s = s.split("+", 1)[0]  # '+추가구성' 이후는 잘라냄
    s = _QTY_UNIT_RE.sub(" ", s)
    for w in _FILLER_WORDS:
        s = s.replace(w, " ")
    s = re.sub(r"\s+", " ", s).strip(" -_/·!,")
    # 끝에 남은 영문 모델명/버전 숫자를 번갈아 제거 (예: "마스크팩 2.0 ECM ACTIVE" -> "마스크팩")
    for _ in range(3):
        before = s
        s = _TRAILING_CAPS_RE.sub("", s).strip()
        s = _TRAILING_VERSION_RE.sub("", s).strip()
        if s == before:
            break
    s = _TRAILING_NUM_RE.sub("", s).strip()
    if not s:
        s = name.strip()
    return _keep_tail(s, max_len)


def http_get_json(url: str) -> Optional[dict]:
    """공개 JSON을 가져온다. 존재하지 않으면(404 등) None을 반환한다."""
    global INSECURE_SSL
    req = Request(url, headers={"User-Agent": USER_AGENT})
    tries_left = 3
    while tries_left > 0:
        try:
            with urlopen(req, timeout=REQUEST_TIMEOUT, context=_ssl_context()) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except HTTPError as e:
            if e.code == 404:
                return None
            tries_left -= 1
            if tries_left <= 0:
                print(f"[경고] {url} 요청 실패 (HTTP {e.code})", file=sys.stderr)
                return None
            time.sleep(1)
        except URLError as e:
            if not INSECURE_SSL and _is_cert_error(e):
                INSECURE_SSL = True
                print(
                    "[안내] 이 네트워크의 보안 프로그램(HTTPS 검사 프록시)이 인증서를 가로채는 것으로 보여, "
                    "인증서 검증을 건너뛰고 다시 시도합니다. (내려받는 데이터는 로그인 없는 공개 정보라 안전합니다)",
                    file=sys.stderr,
                )
                continue  # 방금 실패는 횟수에서 차감하지 않고 즉시 재시도
            tries_left -= 1
            if tries_left <= 0:
                print(f"[경고] {url} 요청 실패 ({e})", file=sys.stderr)
                return None
            time.sleep(1)
        except (TimeoutError, json.JSONDecodeError) as e:
            tries_left -= 1
            if tries_left <= 0:
                print(f"[경고] {url} 요청 실패 ({e})", file=sys.stderr)
                return None
            time.sleep(1)
    return None
    return None


def get_week_monday(base: date, week_offset: int) -> date:
    monday_this_week = base - timedelta(days=base.weekday())
    return monday_this_week + timedelta(weeks=week_offset)


def year_months_between(dates: list[date]) -> list[str]:
    ym = sorted({d.strftime("%Y-%m") for d in dates})
    return ym


class DataStore:
    """필요한 JSON 파일들을 한 번씩만 내려받아 캐시한다."""

    def __init__(self) -> None:
        self._history_cache: dict[str, dict] = {}
        self._upcoming_cache: dict[str, Optional[dict]] = {}
        self._live_cache: dict[tuple[str, str], Optional[dict]] = {}

    def get_history(self, year_month: str) -> dict:
        if year_month not in self._history_cache:
            url = f"{BASE_URL}/representative_programs/history/{year_month}.json"
            data = http_get_json(url) or {"programs": []}
            self._history_cache[year_month] = data
        return self._history_cache[year_month]

    def get_upcoming(self, program_key: str) -> Optional[dict]:
        if program_key not in self._upcoming_cache:
            url = f"{BASE_URL}/representative_programs/{program_key}.json"
            self._upcoming_cache[program_key] = http_get_json(url)
        return self._upcoming_cache[program_key]

    def get_live(self, company: str, year_month: str) -> Optional[dict]:
        key = (company, year_month)
        if key not in self._live_cache:
            url = f"{BASE_URL}/{company}_live/{year_month}.json"
            self._live_cache[key] = http_get_json(url)
        return self._live_cache[key]

    def brand_category_map(self, company: str, date_str: str) -> dict[str, str]:
        year_month = date_str[:7]
        live = self.get_live(company, year_month)
        if not live:
            return {}
        day_items = (live.get("days") or {}).get(date_str) or []
        mapping: dict[str, str] = {}
        for item in day_items:
            brand = (item.get("brand") or "").strip()
            category = item.get("category")
            if brand and category:
                mapping[brand] = category
        return mapping

    def find_broadcast_products(self, spec: ProgramSpec, target_date: date) -> Optional[list[dict]]:
        """지정 프로그램(program_key)의 target_date 방송 상품 목록을 찾는다.
        1) 해당 월의 history(이미 종료된 회차) 우선 조회
        2) 없으면 '가장 가까운 예정' 파일(upcoming)에서 날짜가 일치하는지 확인
        둘 다 없으면 None (데이터 미공개)
        """
        date_str = target_date.strftime("%Y-%m-%d")
        year_month = target_date.strftime("%Y-%m")

        history = self.get_history(year_month)
        for prog in history.get("programs", []):
            if prog.get("program_key") == spec.program_key:
                for bc in prog.get("broadcasts", []):
                    if bc.get("date") == date_str:
                        return bc.get("products", [])

        upcoming = self.get_upcoming(spec.program_key)
        if upcoming:
            products = upcoming.get("products", [])
            if products:
                first_label = products[0].get("broadcast_date_label", "")
                # broadcast_date_label 예: "08/24(월) 19:35"
                if first_label:
                    try:
                        md = first_label.split("(")[0]  # "08/24"
                        month, day = md.split("/")
                        upcoming_date = date(target_date.year, int(month), int(day))
                        if upcoming_date == target_date:
                            return products
                    except (ValueError, IndexError):
                        pass
        return None


def is_appliance(name: str, brand: str, category: Optional[str]) -> bool:
    if category:
        return category == "가전"
    text = f"{brand} {name}"
    return any(kw in text for kw in APPLIANCE_KEYWORDS)


def find_representative_segments(spec: ProgramSpec, target_date: date, store: DataStore) -> list[dict]:
    """시간대(방송 세그먼트)당 대표 상품 1개씩만 뽑아 리스트로 반환한다.

    1순위: {company}_live/{YYYY-MM}.json 의 일자별 편성 데이터. 이 데이터는
           방송사가 실제로 편성한 '시간대(start~end)별 대표 상품' 1건이 이미
           한 행으로 정리되어 있어, 상품 변형(사이즈/색상/구성 등) SKU가
           수십~수백 개씩 나열되는 문제가 없다. pgm 필드로 프로그램을 식별한다.
    2순위(폴백): 위 데이터에 해당 날짜/프로그램이 아직 없는 경우
           representative_programs(단독 상품 목록)에서 브랜드가 같은 상품은
           첫 번째 것만 남겨 대표 상품 1개로 축약한다.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    year_month = target_date.strftime("%Y-%m")

    live = store.get_live(spec.company, year_month)
    day_items = (live.get("days") or {}).get(date_str, []) if live else []
    matched = [it for it in day_items if it.get("pgm") == spec.program_title]

    segments: list[dict] = []
    if matched:
        for it in matched:
            brand = (it.get("brand") or "").strip()
            name = (it.get("product") or "").strip()
            if not name:
                continue
            segments.append({"brand": brand, "name": name, "category": it.get("category")})
        if segments:
            return segments

    # 폴백: representative_programs (history / upcoming), 브랜드당 첫 상품만 사용
    products = store.find_broadcast_products(spec, target_date)
    if not products:
        return []
    cat_map = store.brand_category_map(spec.company, date_str)
    seen_brands: set[str] = set()
    for p in products:
        brand = (p.get("brand") or "").strip()
        name = (p.get("name") or "").strip()
        if not name or brand in seen_brands:
            continue
        seen_brands.add(brand)
        segments.append({"brand": brand, "name": name, "category": cat_map.get(brand)})
    return segments


def summarize_segments(segments: list[dict], simplify: bool = True) -> str:
    seen: list[str] = []
    seen_set: set[str] = set()
    for seg in segments:
        brand = seg.get("brand", "")
        name = seg.get("name", "")
        category = seg.get("category")
        star = "★" if is_appliance(name, brand, category) else ""  # 원래 상품명 기준으로 가전 판별
        display_name = simplify_name(name) if simplify else name
        label = f"{star}{brand} {display_name}".strip() if brand else f"{star}{display_name}"
        if label not in seen_set:
            seen_set.add(label)
            seen.append(label)
    return ", ".join(seen) if seen else ""


def build_report(base_date: date, week_offset: int, store: DataStore, simplify: bool = True) -> tuple[str, str]:
    monday = get_week_monday(base_date, week_offset)
    lines = []
    prev_weekday = None
    for spec in PROGRAMS:
        target_date = monday + timedelta(days=spec.weekday)
        day_label = WEEKDAY_LABEL[spec.weekday]
        prefix = day_label if spec.weekday != prev_weekday else "  "
        prev_weekday = spec.weekday

        segments = find_representative_segments(spec, target_date, store)
        date_tag = target_date.strftime("%m/%d")
        if not segments:
            item_text = "(데이터 미공개)"
        else:
            item_text = summarize_segments(segments, simplify=simplify) or "(상품 정보 없음)"

        line = f"{prefix} {spec.company} {spec.celeb}({date_tag}) {item_text}".rstrip()
        lines.append(line)

    week_range = f"{monday.strftime('%m/%d')}~{(monday + timedelta(days=5)).strftime('%m/%d')}"
    return "\n".join(lines), week_range


def main() -> None:
    parser = argparse.ArgumentParser(description="동업계 셀럽PGM 품목 취합 자동화")
    parser.add_argument("--date", type=str, default=None, help="기준일 YYYY-MM-DD (기본값: 오늘)")
    parser.add_argument("--weeks", choices=["this", "next", "both"], default="both", help="취합 범위 (기본값: both)")
    parser.add_argument("--output", type=str, default=None, help="결과를 저장할 txt 파일 경로")
    parser.add_argument("--no-open", action="store_true",
                         help="저장 후 메모장(기본 텍스트 앱)으로 자동으로 열지 않음 (예약 작업 등 무인 실행 시 사용)")
    parser.add_argument("--full-names", action="store_true",
                         help="상품명을 줄이지 않고 원래(긴) 상품명 그대로 표시")
    args = parser.parse_args()

    if args.date:
        base_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        base_date = date.today()

    simplify = not args.full_names
    store = DataStore()
    sections: list[str] = []

    if args.weeks in ("this", "both"):
        body, rng = build_report(base_date, 0, store, simplify=simplify)
        sections.append(f"동업계 고정PGM 모니터링(금주, {rng})\n{body}")

    if args.weeks in ("next", "both"):
        body, rng = build_report(base_date, 1, store, simplify=simplify)
        sections.append(f"동업계 고정PGM 모니터링(차주, {rng})\n{body}")

    output_text = "\n\n".join(sections)
    print(output_text)

    out_path = args.output or f"동업계_PGM_모니터링_{base_date.strftime('%Y%m%d')}.txt"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(output_text + "\n")
        print(f"\n[저장 완료] {out_path}", file=sys.stderr)
        if not args.no_open:
            open_in_default_app(out_path)
    except OSError as e:
        print(f"[경고] 파일 저장 실패: {e}", file=sys.stderr)

    if sys.stdin is None or not sys.stdin.isatty():
        # 더블클릭 등으로 실행되어 표준입력이 연결되지 않은 경우는 그냥 종료
        return
    if platform.system() == "Windows":
        # 더블클릭으로 실행했을 때 콘솔 창이 바로 닫히지 않도록 대기
        try:
            input("\n종료하려면 Enter 키를 누르세요...")
        except (EOFError, KeyboardInterrupt):
            pass


def open_in_default_app(path: str) -> None:
    """결과 txt 파일을 OS 기본 텍스트 앱(예: 메모장)으로 연다."""
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:  # noqa: BLE001 - 자동 열기는 실패해도 무시하고 계속 진행
        print(f"[안내] 결과 파일을 자동으로 여는 데 실패했어요. 직접 열어 확인해주세요: {path} ({e})", file=sys.stderr)


if __name__ == "__main__":
    main()