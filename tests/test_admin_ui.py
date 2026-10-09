"""管理面板的 UI 契约测试。

锁定苹果风格改造的几个关键点，防止后续改动无意中破坏：
  1. JS 内联样式引用的 CSS 变量必须在浅/深两套令牌中都有定义
  2. 两套主题必须定义同一组变量（否则深色会悄悄回退到浅色值）
  3. 全部元素 ID 必须存在（JS 靠 getElementById 取节点）
  4. 关键 JS 函数一个都不能少
  5. 苹果设计语言的关键特征（字体栈 / 分段控件 / iOS 开关 / 毛玻璃 / 主题切换）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parent.parent / "traeapi" / "server" / "templates" / "admin.html"

# JS 通过 getElementById / id="..." 访问的元素
REQUIRED_IDS = (
    "acctTable", "apikey", "checkinBtn", "checkinDetail", "checkinStatus",
    "fnSelect", "fnStatus", "grid", "impInput", "impStatus",
    "keyCurrent", "keyFilePath", "keyModalBg", "keyModalStatus", "keyNew", "keySaveBtn",
    "keyserver", "keystatus", "loginBtn", "loginStatus", "modalBg", "modalBody", "modalTitle",
    "modelsNote", "modelsRefreshBtn", "modelTable", "poolsNote", "poolsTable", "ts",
    "themeBtn", "usageSummary", "usageTable",
)

# 关键 JS 函数
REQUIRED_FUNCS = (
    "switchTab", "refreshActive", "loadAccounts", "loadCredits", "loadModels", "loadUsage",
    "loadPools", "loadFunction", "loadServerKey", "toggleTheme", "runCheckin", "startLogin",
    "pollLogin", "importCred", "showJSON", "toggleAcct", "refreshAcct", "deleteAcct",
    "submitNewKey", "clearServerKey", "expireBadge", "fmtExpire", "fmtRemain",
)

# 原始 Go 版面板就有的变量名，保留以兼容既有内联样式
LEGACY_VARS = ("--bg", "--card", "--card2", "--fg", "--dim", "--ok", "--warn", "--bad", "--accent")


@pytest.fixture(scope="module")
def html() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _theme_blocks(html: str) -> dict[str, str]:
    """取出浅色与深色两套令牌块。"""
    light = re.search(r":root\s*\{(.*?)\}", html, re.S)
    dark = re.search(r':root\[data-theme="dark"\]\s*\{(.*?)\}', html, re.S)
    assert light, "缺少浅色令牌块 :root{...}"
    assert dark, "缺少深色令牌块 :root[data-theme=\"dark\"]{...}"
    return {"light": light.group(1), "dark": dark.group(1)}


def _vars_of(block: str) -> set[str]:
    """取出令牌块里定义的所有 CSS 变量名。

    注意字符集要包含连字符：--ok-t / --shadow-sm 这类带后缀的令牌
    否则会被漏掉，导致「两套主题变量集一致」的比对失去意义。
    """
    return set(re.findall(r"(--[\w-]+)\s*:", block))


class TestTemplateIntegrity:
    """模板结构完整性。"""

    def test_template_exists(self):
        assert TEMPLATE.exists(), f"缺少面板模板: {TEMPLATE}"

    def test_tags_balanced(self, html: str):
        assert html.count("<style>") == html.count("</style>") == 1
        assert html.count("<script>") == html.count("</script>")

    def test_no_leftover_placeholder(self, html: str):
        assert "{{" not in html and "TODO" not in html


class TestCssVariables:
    """CSS 变量契约。"""

    def test_js_referenced_vars_defined_in_both_themes(self, html: str):
        """JS 内联样式引用的每个变量，两套主题都必须定义。

        这是最关键的一条：JS 生成 HTML 时直接写 var(--xxx)，
        若某套主题漏定义，颜色会静默失效（回退到初始值/继承值）。
        """
        js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        referenced = set(re.findall(r"var\((--[a-z0-9]+)", js))
        assert referenced, "JS 似乎没有引用任何 CSS 变量？"
        blocks = _theme_blocks(html)
        for name, block in blocks.items():
            defined = _vars_of(block)
            missing = referenced - defined
            assert not missing, f"{name} 主题缺少 JS 引用的变量: {sorted(missing)}"

    def test_no_dangling_var_in_dark_mode(self, html: str):
        """深色模式下不能有未定义的 var() 引用。

        结构令牌（--radius/--ease 等主题无关）只定义在 :root，
        颜色令牌在 [data-theme="dark"] 覆盖；两者作用于同一个 <html> 元素，
        未覆盖的会继承。这里穷举 CSS 里所有 var(--x)，确保每个都在
        :root 或深色块中有定义 —— 否则深色下会静默失效（如圆角变 0）。
        """
        blocks = _theme_blocks(html)
        root_block = re.search(r":root\s*\{(.*?)\}", html, re.S).group(1)
        available = _vars_of(root_block) | _vars_of(blocks["dark"])

        css = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
        referenced = set(re.findall(r"var\((--[\w-]+)", css))
        dangling = referenced - available
        assert not dangling, f"CSS 引用了未定义的变量: {sorted(dangling)}"

    def test_legacy_variable_names_preserved(self, html: str):
        """原版面板的变量名保留（既有内联样式与外部引用依赖它们）。"""
        blocks = _theme_blocks(html)
        available = _vars_of(blocks["light"]) | _vars_of(blocks["dark"])
        missing = [v for v in LEGACY_VARS if v not in available]
        assert not missing, f"丢失原版变量: {missing}"

    def test_dark_theme_uses_distinct_values(self, html: str):
        """深色不能只是浅色的复制，背景/卡片/文字必须不同。"""
        blocks = _theme_blocks(html)

        def grab(block: str, name: str) -> str:
            m = re.search(rf"{name}:\s*([^;]+);", block)
            return m.group(1).strip() if m else ""

        for var in ("--bg", "--card", "--fg", "--accent"):
            assert grab(blocks["light"], var) != grab(blocks["dark"], var), (
                f"{var} 在两套主题中取值相同"
            )

    def test_badge_tokens_exist_for_contrast(self, html: str):
        """浅色底需要更深的文字色，徽章令牌要齐全。

        注意 dim 只有 -bg（文字直接用 --dim），ok/warn/bad 才有 -t。
        """
        blocks = _theme_blocks(html)
        for name, block in blocks.items():
            defined = _vars_of(block)
            for kind in ("ok", "warn", "bad"):
                assert f"--{kind}-t" in defined, f"{name} 缺少 --{kind}-t"
            for kind in ("ok", "warn", "bad", "dim"):
                assert f"--{kind}-bg" in defined, f"{name} 缺少 --{kind}-bg"



class TestElementIds:
    """元素 ID 契约。"""

    @pytest.mark.parametrize("el_id", REQUIRED_IDS)
    def test_id_present(self, html: str, el_id: str):
        assert f'id="{el_id}"' in html, f"缺少元素 #{el_id}"


class TestJsFunctions:
    """JS 函数契约。"""

    @pytest.mark.parametrize("func", REQUIRED_FUNCS)
    def test_function_defined(self, html: str, func: str):
        assert f"function {func}(" in html, f"缺少 JS 函数 {func}()"


class TestAppleDesign:
    """苹果设计语言的关键特征。"""

    def test_sf_font_stack(self, html: str):
        assert "-apple-system" in html
        assert "SF Pro Text" in html
        assert "PingFang SC" in html  # 苹果中文黑体

    def test_segmented_tabs(self, html: str):
        """页签是 iOS 分段控件样式（容器有圆角+底色，选中项浮起）。"""
        assert ".tabs" in html
        assert ".tab.active" in html
        # 分段控件容器有 padding 与圆角
        m = re.search(r"\.tabs\s*\{([^}]*)\}", html, re.S)
        assert m and "border-radius" in m.group(1) and "padding" in m.group(1)

    def test_ios_switch(self, html: str):
        """开关是 iOS 样式（50x30 胶囊，选中变绿）。"""
        assert ".toggle input:checked + .slider" in html
        m = re.search(r"\.toggle\s*\{([^}]*)\}", html, re.S)
        assert m and "width:50px" in m.group(1) and "height:30px" in m.group(1)

    def test_frosted_glass_header(self, html: str):
        """顶栏使用毛玻璃。"""
        assert "backdrop-filter" in html

    def test_soft_layered_shadow(self, html: str):
        """卡片用分层柔和阴影，而非硬边框。"""
        assert "--shadow:" in html

    def test_theme_toggle_wired(self, html: str):
        """主题切换：按钮 + 函数 + 持久化 + 首屏防闪烁初始化。"""
        assert 'id="themeBtn"' in html
        assert "function toggleTheme()" in html
        assert "tw2a_theme" in html
        # <head> 里要有首屏初始化脚本，避免刷新时闪一下
        head = html.split("<body>")[0]
        assert "tw2a_theme" in head

    def test_light_is_default(self, html: str):
        """默认浅色（localStorage 无值时回退 light）。"""
        assert "localStorage.getItem('tw2a_theme')||'light'" in html

    def test_mobile_responsive(self, html: str):
        assert "@media (max-width:640px)" in html
