#!/usr/bin/env python
"""i18n 模块测试：验证 tr 回退、normalize_lang、EN_TABLE 完整性与 load_po_to_en 存在性"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from i18n import tr, normalize_lang, EN_TABLE, DEFAULT_LANG, load_po_to_en  # noqa

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS: {name}")
    else:
        failed += 1
        print(f"FAIL: {name}")


# 1) tr 回退：未知文本返回原文（中英均回退）
check("tr-unknown-zh", tr("zh", "@@NO_SUCH_KEY@@") == "@@NO_SUCH_KEY@@")
check("tr-unknown-en", tr("en", "@@NO_SUCH_KEY@@") == "@@NO_SUCH_KEY@@")

# 2) tr 双语已知词条
check("tr-zh-创作", tr("zh", "创作") == "创作")
check("tr-en-创作", tr("en", "创作") == "Create")

# 3) normalize_lang 多种输入归一
check("norm-zh", normalize_lang("zh") == "zh")
check("norm-cn", normalize_lang("中文") == "zh")
check("norm-zhcn", normalize_lang("zh-CN") == "zh")
check("norm-en", normalize_lang("English") == "en")
check("norm-enus", normalize_lang("en-US") == "en")
check("norm-none", normalize_lang(None) == DEFAULT_LANG)
check("norm-invalid", normalize_lang("fr") == DEFAULT_LANG)

# 4) EN_TABLE 完整性：足够大且所有值非空
check("entable-size-50+", len(EN_TABLE) > 50)
check("entable-all-nonempty", all(isinstance(v, str) and v.strip() for v in EN_TABLE.values()))
check("entable-keys-nonempty", all(isinstance(k, str) and k.strip() for k in EN_TABLE.keys()))

# 5) load_po_to_en 可调用（Babel/polib 归档入口）
check("loadpo-callable", callable(load_po_to_en))

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)