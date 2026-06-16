"""V27.4 — fake_search 的 ``query`` 参数必须是纯中英文地名。

控制平面的 ``search`` 工具把 ``query`` 直接拼进 ``wttr.in/<query>``。本套件钉住
参数契约：只接受裸地名（'New York' / '广东' / 'Shenzhen'），拒绝带无意义修饰词
（'Shenzhen weather'）、JSON 串、数字、CJK 噪声词（'深圳天气'）等。校验在发起
网络抓取之前完成，不达标即返回 {"query", "error"}，不查 wttr.in、不发语音事件。

Run::

    .venv/bin/python scripts/test_v27_5_query_validation.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib

fss = importlib.import_module("fake_search_service")


# ---- 接受：纯中英文地名（_validate_query 返回 None）-------------------------
def test_1_accepts_plain_place_names():
    for name in ["New York", "广东", "Shenzhen", "São Paulo", "西安", "Los Angeles", "内蒙古"]:
        err = fss._validate_query(name)
        assert err is None, f"地名 {name!r} 应通过校验，却被拒：{err}"


# ---- 拒绝：地名后带无意义修饰词 ---------------------------------------------
def test_2_rejects_trailing_noise_word_en():
    err = fss._validate_query("Shenzhen weather")
    assert err is not None, "'Shenzhen weather' 含 'weather'，应判不达标"


def test_3_rejects_noise_word_cjk():
    for q in ["深圳天气", "广东 天气", "北京气温", "上海预报"]:
        assert fss._validate_query(q) is not None, f"{q!r} 含 CJK 噪声词，应判不达标"


# ---- 拒绝：JSON / 花括号 / 引号等非地名字符 --------------------------------
def test_4_rejects_json_like_strings():
    for q in ['{"query": "New York"}', '广东"}}', 'Shenzhen weather}', '{"query":"Shenzhen"}']:
        assert fss._validate_query(q) is not None, f"{q!r} 含 JSON/标点，应判不达标"


# ---- 拒绝：空 / 纯空白 / 数字 ------------------------------------------------
def test_5_rejects_empty_and_digits():
    for q in ["", "   ", "12345", "Shenzhen 2026", "区号0755"]:
        assert fss._validate_query(q) is not None, f"{q!r} 应判不达标（空或含数字）"


# ---- 拒绝：词数过多（疑似塞了无意义参数）------------------------------------
def test_6_rejects_too_many_words():
    err = fss._validate_query("New York City of United States")
    assert err is not None, "词数过多应判不达标"


# ---- search()：不达标的 query 直接返回 error，不发起网络抓取 -----------------
def test_7_search_returns_error_without_fetch():
    raw = fss.search("Shenzhen weather")
    payload = json.loads(raw)
    assert "error" in payload, f"不达标 query 应返回 error 字段，实得 {payload}"
    assert "report" not in payload, "不达标时不应有 report（不该查 wttr.in）"
    assert payload["query"] == "Shenzhen weather"


# ---- search()：达标的 query 走正常路径（含 report 字段）----------------------
def test_8_search_accepts_valid_place():
    # 走真实 wttr.in 抓取（与 v27_2 一致）；只钉「合法地名不被 error 拦下」。
    raw = fss.search("Shenzhen")
    payload = json.loads(raw)
    assert "error" not in payload, f"合法地名不应被校验拦下：{payload.get('error')}"
    assert isinstance(payload.get("report"), str) and payload["report"]


def main() -> None:
    tests = [
        test_1_accepts_plain_place_names,
        test_2_rejects_trailing_noise_word_en,
        test_3_rejects_noise_word_cjk,
        test_4_rejects_json_like_strings,
        test_5_rejects_empty_and_digits,
        test_6_rejects_too_many_words,
        test_7_search_returns_error_without_fetch,
        test_8_search_accepts_valid_place,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"✓ {test.__name__}")
        except AssertionError as exc:
            print(f"✗ {test.__name__} — {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"✗ {test.__name__} — UNEXPECTED {type(exc).__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print("=" * 60)
    if failed:
        print(f"  FAIL: {failed}/{len(tests)} 不变量未通过")
        sys.exit(1)
    print(f"  PASS: {len(tests)}/{len(tests)} 不变量")


if __name__ == "__main__":
    main()
