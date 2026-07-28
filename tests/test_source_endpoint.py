"""`/api/source` 的读取边界测试。

这个端点存在的理由很窄：能力账本声称"每条能力都有代码证据"，UI 要能点开
evidence 路径让人当场核对。但"读取仓库里的文件"这件事一旦放宽一寸，就会
把本机私有配置端出去。

这些测试锁定的是一次开发期实测事故：端点最初用"文件名里含 secret/token 就拒绝"
的黑名单，提交前探测发现 `symbio.yaml` 照样返回 200，响应里带着真实的
`anthropic_api_key`——那个文件名毫无可疑之处，黑名单根本看不见它。
（该写法未曾进入提交；这些测试的作用是防止有人再改回去。）现在的防线是目录白名单。

因此下面同时钉住两侧：
- 该挡的必须挡（否则密钥泄漏回归）；
- 该放的必须放（否则能力账本的证据链变成死链，端点就失去了存在意义）。
"""

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.capabilities import CAPABILITY_ITEMS  # noqa: E402
from symbio.interfaces.api import app  # noqa: E402


@pytest_asyncio.fixture
async def client():
    app.state.api_token = ""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.state.api_token = ""


async def _get(client, path: str):
    return await client.get("/api/source", params={"path": path})


# ---------- 必须挡住的 ----------


@pytest.mark.asyncio
async def test_repo_root_config_is_never_served(client):
    """`symbio.yaml` 存着真实 API Key，且被 .gitignore 忽略，绝不能被读出。

    这是原始泄漏点：曾返回 200，正文含长度 51 的 anthropic_api_key。
    """
    resp = await _get(client, "symbio.yaml")
    assert resp.status_code == 403, f"symbio.yaml 必须被拒绝，实际 {resp.status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "pyproject.toml",
        "README.md",
        "activate.bat",
    ],
)
async def test_root_level_files_are_denied_by_default(client, path):
    """仓库根是本机私有配置的家（symbio.yaml、*.env），整层默认不开放。

    README.md 这类无害文件也一并挡掉：白名单的价值在于"默认拒绝"，
    为了少数无害文件开个口子，就等于把判断权交还给黑名单思路。
    """
    resp = await _get(client, path)
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "../../../Windows/System32/drivers/etc/hosts",
        "../symbio.yaml",
        "src/../symbio.yaml",
        "config/../symbio.yaml",
        "src/symbio/../../../symbio.yaml",
    ],
)
async def test_path_traversal_is_blocked(client, path):
    """`..` 不能用来爬出仓库，也不能用来绕开目录白名单再落回根上。

    注意 `src/../symbio.yaml`：它形式上以允许的 `src` 开头，解析后却指向根。
    所以白名单必须在 resolve() 之后判断，不能对原始字符串做前缀匹配。
    """
    resp = await _get(client, path)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_runtime_data_dir_is_not_readable(client):
    """data/ 是运行期状态（会话、记忆、令牌落盘），不属于"代码证据"。"""
    resp = await _get(client, "data/a2a_state.json")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_binary_suffix_rejected(client):
    """只服务文本。二进制读成 utf-8 只会得到一堆替换字符。"""
    resp = await _get(client, "assets/screenshots/ui-chat.png")
    assert resp.status_code in (403, 415)


@pytest.mark.asyncio
async def test_sensitive_filename_still_rejected_inside_allowed_dir(client):
    """纵深防御：即使落在白名单目录内，可疑文件名依然被拒。

    用不存在的路径也能验证——因为这道检查排在 exists() 之后、
    但拒绝码不同于 404，说明命中的是名字规则而不是"文件不存在"。
    """
    target = PROJECT_ROOT / "config" / "credentials.yaml"
    target.write_text("api_key: should-never-be-served\n", encoding="utf-8")
    try:
        resp = await _get(client, "config/credentials.yaml")
        assert resp.status_code == 403
        assert "should-never-be-served" not in resp.text
    finally:
        target.unlink(missing_ok=True)


# ---------- 必须放行的 ----------


@pytest.mark.asyncio
async def test_source_file_is_served(client):
    resp = await _get(client, "src/symbio/capabilities.py")
    assert resp.status_code == 200
    body = resp.json()
    assert "CAPABILITY_ITEMS" in body["content"]
    assert body["lines"] > 1
    assert body["size"] > 0


@pytest.mark.asyncio
async def test_every_capability_evidence_path_is_readable(client):
    """账本里每条 evidence 都必须能被打开——否则 UI 上就是一串死链。

    这条测试是端点的存在理由本身。若将来收紧白名单导致某条证据读不到，
    这里会立刻失败，迫使人明确选择：要么改账本，要么改白名单。
    """
    evidence: set[str] = set()
    for item in CAPABILITY_ITEMS:
        for path in item.get("evidence") or []:
            evidence.add(path.replace("\\", "/"))

    assert evidence, "账本里应当有证据路径"

    failures = []
    for path in sorted(evidence):
        resp = await _get(client, path)
        if resp.status_code != 200:
            failures.append(f"{path} -> {resp.status_code}")

    assert not failures, "以下证据路径无法读取：\n" + "\n".join(failures)


@pytest.mark.asyncio
async def test_served_files_carry_no_live_api_keys(client):
    """遍历所有放行的证据文件，确认没有 sk- 形状的真实密钥漏出去。

    白名单挡的是"位置"，这条测试查的是"内容"——两者独立，
    以防将来有人把凭据提交进 src/ 或 config/。
    """
    import re

    key_shaped = re.compile(r"sk-[A-Za-z0-9_-]{16,}")

    evidence: set[str] = set()
    for item in CAPABILITY_ITEMS:
        for path in item.get("evidence") or []:
            evidence.add(path.replace("\\", "/"))

    leaks = []
    for path in sorted(evidence):
        resp = await _get(client, path)
        if resp.status_code != 200:
            continue
        if key_shaped.search(resp.json()["content"]):
            leaks.append(path)

    assert not leaks, f"这些文件里出现了密钥形状的字符串：{leaks}"
