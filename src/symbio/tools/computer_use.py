"""Computer Use 最小闭环：浏览器会话控制 + 截图 + 动作规划 + 审计回放。

兑现 README/能力账本里唯一标注 missing 的承诺。设计目标是"最小但真实的闭环"：

- ComputerUseSession：管理一个浏览器会话，串起 导航→截图→理解→动作 的循环
- 动作集：navigate / screenshot / click / type / scroll / extract_text
- 每一步都写入审计轨迹（时间、动作、参数、结果、截图路径、成败）
- ActionPlanner：给定目标 + 当前页面状态，产出下一步动作（内置启发式规划器，
  并暴露 LLM 规划接口供接管）
- replay()：从审计轨迹逐步回放

关键工程选择：Playwright 可用时执行真实浏览器操作；不可用时降级为
"record-only（dry-run）"模式——仍然记录意图动作、推进闭环、产出审计与回放，
只是不真正驱动浏览器。这样无论环境是否装了 Playwright，闭环结构都完整可测。
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from symbio.utils.logger import get_logger

logger = get_logger("computer_use")

_AUDIT_DIR = Path("data") / "computer_use"

VALID_ACTIONS = {"navigate", "screenshot", "click", "type", "scroll", "extract_text", "wait"}


def _playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401

        return True
    except Exception:
        return False


def _image_to_block(image_path: str) -> Optional[dict[str, Any]]:
    """把 PNG 截图读成 Anthropic 视觉 image block；读不到返回 None。

    VLM 视觉规划的关键一环：截图像素 → base64 → 多模态 message，让模型真正
    "看到"页面而非只读文字描述。
    """
    import base64
    import mimetypes

    try:
        p = Path(image_path)
        if not p.is_file():
            return None
        data = p.read_bytes()
        if not data:
            return None
        media_type = mimetypes.guess_type(str(p))[0] or "image/png"
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(data).decode("ascii"),
            },
        }
    except Exception as exc:
        logger.warning(f"截图转 base64 失败 {image_path}: {exc}")
        return None


class ComputerUseSession:
    """单个浏览器会话，维护页面状态与审计轨迹。"""

    def __init__(self, session_id: str, start_url: str = "", headless: bool = True):
        self.session_id = session_id
        self.start_url = start_url
        self.headless = headless
        self.current_url = start_url
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.steps: list[dict[str, Any]] = []
        self.status = "open"
        self.dry_run = not _playwright_available()
        # Playwright 运行时句柄（仅真实模式使用）
        self._pw = None
        self._browser = None
        self._page = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def _ensure_page(self) -> Any:
        if self.dry_run:
            return None
        if self._page is not None:
            return self._page
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        self._page = await self._browser.new_page(viewport={"width": 1280, "height": 800})
        return self._page

    async def close(self) -> None:
        self.status = "closed"
        if self.dry_run:
            return
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
        except Exception as e:
            logger.warning(f"关闭浏览器会话异常: {e}")
        finally:
            self._page = self._browser = self._pw = None

    # ------------------------------------------------------------------
    # 动作执行
    # ------------------------------------------------------------------

    async def act(self, action: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """执行一个动作并记录审计步。"""
        params = params or {}
        action = (action or "").lower().strip()
        step_index = len(self.steps)
        started = time.time()

        if action not in VALID_ACTIONS:
            step = self._record(
                step_index, action, params, success=False, error=f"未知动作: {action}", elapsed_ms=0
            )
            return step

        try:
            if self.dry_run:
                result = self._dry_run_result(action, params)
            else:
                result = await self._real_act(action, params)
            elapsed = int((time.time() - started) * 1000)
            step = self._record(
                step_index, action, params, success=True, result=result, elapsed_ms=elapsed
            )
        except Exception as e:
            elapsed = int((time.time() - started) * 1000)
            logger.warning(f"动作执行失败 {action}: {e}")
            step = self._record(
                step_index, action, params, success=False, error=str(e), elapsed_ms=elapsed
            )
        return step

    def _dry_run_result(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """无 Playwright 时的模拟结果（记录意图，推进闭环）。"""
        if action == "navigate":
            self.current_url = params.get("url", self.current_url)
            return {"mode": "dry_run", "navigated_to": self.current_url}
        if action == "screenshot":
            return {"mode": "dry_run", "screenshot": None, "note": "Playwright 未安装，未真正截图"}
        if action == "extract_text":
            return {"mode": "dry_run", "text": "", "note": "Playwright 未安装"}
        return {"mode": "dry_run", "action": action, "params": params}

    async def _real_act(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        page = await self._ensure_page()
        if action == "navigate":
            url = params.get("url", "")
            await page.goto(url, wait_until="networkidle", timeout=30000)
            self.current_url = page.url
            return {"navigated_to": self.current_url, "title": await page.title()}
        if action == "screenshot":
            _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            path = str(_AUDIT_DIR / f"{self.session_id}_{len(self.steps)}.png")
            await page.screenshot(path=path, full_page=bool(params.get("full_page", False)))
            return {"screenshot": path}
        if action == "click":
            if params.get("selector"):
                await page.click(params["selector"], timeout=10000)
            else:
                await page.mouse.click(float(params.get("x", 0)), float(params.get("y", 0)))
            return {"clicked": params}
        if action == "type":
            if params.get("selector"):
                await page.fill(params["selector"], params.get("text", ""))
            else:
                await page.keyboard.type(params.get("text", ""))
            return {"typed": params.get("text", "")}
        if action == "scroll":
            dy = int(params.get("dy", 400))
            await page.mouse.wheel(0, dy)
            return {"scrolled": dy}
        if action == "extract_text":
            text = await page.inner_text(params.get("selector", "body"))
            return {"text": text[:5000]}
        if action == "wait":
            await page.wait_for_timeout(int(params.get("ms", 500)))
            return {"waited_ms": int(params.get("ms", 500))}
        return {"action": action}

    def _record(
        self, index, action, params, success, result=None, error="", elapsed_ms=0
    ) -> dict[str, Any]:
        step = {
            "index": index,
            "action": action,
            "params": params,
            "success": success,
            "result": result or {},
            "error": error,
            "elapsed_ms": elapsed_ms,
            "url": self.current_url,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self.steps.append(step)
        return step

    def latest_screenshot(self) -> Optional[str]:
        """返回最近一次成功截图的文件路径；没有则 None（供视觉规划取图）。"""
        for s in reversed(self.steps):
            if s["action"] == "screenshot" and s.get("success"):
                path = s.get("result", {}).get("screenshot")
                if path and Path(path).is_file():
                    return path
        return None

    # ------------------------------------------------------------------
    # 回放
    # ------------------------------------------------------------------

    async def replay(self) -> dict[str, Any]:
        """从已记录的动作轨迹重新执行一遍，返回回放报告。"""
        recorded = list(self.steps)
        self.steps = []
        replayed = []
        for step in recorded:
            new_step = await self.act(step["action"], step.get("params", {}))
            replayed.append({"original_index": step["index"], "success": new_step["success"]})
        return {
            "replayed_steps": len(replayed),
            "succeeded": sum(1 for r in replayed if r["success"]),
            "details": replayed,
        }

    def to_dict(self, include_steps: bool = True) -> dict[str, Any]:
        data = {
            "session_id": self.session_id,
            "start_url": self.start_url,
            "current_url": self.current_url,
            "status": self.status,
            "dry_run": self.dry_run,
            "created_at": self.created_at,
            "step_count": len(self.steps),
        }
        if include_steps:
            data["steps"] = self.steps
        return data


class ActionPlanner:
    """动作规划器：给定目标与当前页面状态，产出下一步动作。

    内置启发式规划器不依赖 LLM——它把目标里出现的 URL 转成 navigate，
    并按 navigate→screenshot→extract_text 的标准侦察序列推进，
    适合作为最小闭环演示与回归测试的确定性后端。
    真实部署可注入 LLM 规划器（plan_with_llm）替换 plan()。
    """

    @staticmethod
    def plan(goal: str, session: ComputerUseSession) -> dict[str, Any]:
        steps_done = {s["action"] for s in session.steps}
        goal = goal or ""

        # 目标里包含 URL 且尚未导航 -> 先导航
        import re

        url_match = re.search(r"https?://[^\s]+", goal)
        if url_match and "navigate" not in steps_done:
            return {
                "action": "navigate",
                "params": {"url": url_match.group()},
                "reason": "目标中包含 URL，先导航到目标页面",
            }
        if not session.current_url and "navigate" not in steps_done:
            return {"action": "wait", "params": {"ms": 100}, "reason": "无目标 URL，等待进一步指令"}
        if "screenshot" not in steps_done:
            return {"action": "screenshot", "params": {}, "reason": "导航完成，截图以理解当前页面"}
        if "extract_text" not in steps_done:
            return {
                "action": "extract_text",
                "params": {"selector": "body"},
                "reason": "提取页面文本用于后续决策",
            }
        return {
            "action": "wait",
            "params": {"ms": 100},
            "reason": "标准侦察序列已完成，等待目标判定",
        }


_LLM_PLANNER_SYSTEM = (
    "你是一个浏览器操作 Agent 的动作规划器。根据用户目标和当前页面状态，"
    "决定下一步要执行的单个动作。只能从以下动作中选择：\n"
    "- navigate(url): 打开网址\n"
    "- click(selector | x,y): 点击元素或坐标\n"
    "- type(text, selector?): 输入文本\n"
    "- scroll(dy): 滚动\n"
    "- extract_text(selector?): 提取页面文本\n"
    "- screenshot(): 截图理解页面\n"
    "- wait(ms): 等待\n"
    "- done(): 目标已达成时返回\n\n"
    "严格只输出一个 JSON 对象，格式："
    '{"action": "<动作>", "params": {<参数>}, "reason": "<中文理由>"}。'
    "不要输出任何额外文字或 markdown 代码块。"
)

_LLM_PLANNER_VISION_HINT = (
    "\n\n【视觉模式】随本消息附带了当前页面的截图。请**看图**判断下一步：\n"
    '- 需要点击时，用像素坐标定位：params 填 {"x": <横向像素>, "y": <纵向像素>}，'
    "截图左上角为原点 (0,0)，向右 x 增大、向下 y 增大。\n"
    '- 需要输入时，先 click 定位输入框，再 type({"text": ...})。\n'
    "- 依据截图中实际可见的元素决策，不要臆测不可见的内容。"
)


class LLMActionPlanner:
    """LLM 驱动的动作规划器。

    结合目标 + 当前页面状态（URL、最近提取的文本、已执行动作）让 LLM 决定下一步。
    通过注入 `complete`（async (system, user) -> str）便于测试与替换后端；
    默认从 settings 构建 Anthropic 客户端。任何失败都回退到启发式 ActionPlanner，
    保证 Computer Use 闭环永不因规划层故障而中断。
    """

    def __init__(self, complete=None, model: Optional[str] = None):
        self._complete = complete
        self._model = model

    async def plan(
        self,
        goal: str,
        session: "ComputerUseSession",
        use_vision: bool = False,
    ) -> dict[str, Any]:
        """规划下一步动作。

        use_vision=True 且能拿到当前截图时，把截图像素喂给 VLM（视觉规划），
        让模型看着页面产出坐标动作；否则退纯文本规划。complete 注入的后端签名为
        ``async (system, user, image_path=None) -> str``，向后兼容仅收两参的旧后端。
        三级回退：视觉/文本 LLM → 启发式，任何失败都不中断闭环。
        """
        complete = self._complete or self._default_complete()
        if complete is None:
            result = ActionPlanner.plan(goal, session)
            result["planner"] = "heuristic"
            result["reason"] = "未配置 LLM，回退启发式规划：" + result.get("reason", "")
            return result

        image_path = session.latest_screenshot() if use_vision else None
        vision = image_path is not None
        system = _LLM_PLANNER_SYSTEM + (_LLM_PLANNER_VISION_HINT if vision else "")
        user_prompt = self._build_prompt(goal, session)
        try:
            raw = await self._invoke(complete, system, user_prompt, image_path)
            plan = self._parse(raw)
            if plan is None:
                raise ValueError(f"无法解析 LLM 规划输出: {raw[:120]}")
            plan["planner"] = "vlm" if vision else "llm"
            return plan
        except Exception as e:
            logger.warning(f"LLM 规划失败，回退启发式: {e}")
            result = ActionPlanner.plan(goal, session)
            result["planner"] = "heuristic-fallback"
            return result

    @staticmethod
    async def _invoke(complete, system: str, user: str, image_path: Optional[str]) -> str:
        """调用 complete，优先带图（三参签名）；旧的两参后端自动降级为纯文本。"""
        if image_path is not None:
            try:
                return await complete(system, user, image_path=image_path)
            except TypeError:
                # 后端不接受 image_path（旧签名）→ 退纯文本
                logger.debug("complete 后端不支持 image_path，降级纯文本")
        return await complete(system, user)

    @staticmethod
    def _build_prompt(goal: str, session: "ComputerUseSession") -> str:
        recent = session.steps[-4:]
        history_lines = []
        for s in recent:
            res = s.get("result", {})
            snippet = res.get("text") or res.get("navigated_to") or ""
            history_lines.append(
                f"- {s['action']}({s.get('params', {})}) -> "
                f"{'成功' if s['success'] else '失败'} {str(snippet)[:100]}"
            )
        last_text = ""
        for s in reversed(session.steps):
            if s["action"] == "extract_text" and s.get("result", {}).get("text"):
                last_text = s["result"]["text"][:1200]
                break
        return (
            f"目标：{goal}\n"
            f"当前页面 URL：{session.current_url or '(空白)'}\n"
            f"已执行动作（最近 {len(recent)} 步）：\n"
            + ("\n".join(history_lines) if history_lines else "(无)")
            + (f"\n\n当前页面文本片段：\n{last_text}" if last_text else "")
            + "\n\n请决定下一步动作（只输出 JSON）。"
        )

    @staticmethod
    def _parse(raw: str) -> Optional[dict[str, Any]]:
        if not raw:
            return None
        text = raw.strip()
        # 容忍 ```json ... ``` 包裹
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        # 抓第一个 { ... } 段
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        try:
            obj = json.loads(text[start : end + 1])
        except Exception:
            return None
        action = str(obj.get("action", "")).lower().strip()
        if action == "done":
            return {
                "action": "wait",
                "params": {"ms": 0},
                "reason": obj.get("reason", "目标已达成"),
                "done": True,
            }
        if action not in VALID_ACTIONS:
            return None
        params = obj.get("params", {})
        if not isinstance(params, dict):
            params = {}
        return {"action": action, "params": params, "reason": str(obj.get("reason", ""))}

    def _default_complete(self):
        """从 settings 构建 Anthropic 补全函数；无 key 返回 None。

        支持视觉：传入 image_path 时构造多模态 message（image block + 文本），
        让 Claude 视觉看着截图规划；不传则纯文本（向后兼容）。
        """
        try:
            from symbio.config.settings import get_settings

            settings = get_settings()
            api_key = settings.model.anthropic_api_key
            if not api_key:
                return None
            base_url = settings.model.anthropic_base_url
            model = self._model or settings.model.model_medium
        except Exception:
            return None

        async def _complete(system: str, user: str, image_path: Optional[str] = None) -> str:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
            if image_path is not None:
                block = _image_to_block(image_path)
                if block is not None:
                    content = [block, {"type": "text", "text": user}]
                else:
                    content = user  # 图读不到就退纯文本
            else:
                content = user
            resp = await client.messages.create(
                model=model,
                max_tokens=512,
                system=system,
                messages=[{"role": "user", "content": content}],
            )
            return "".join(getattr(b, "text", "") for b in resp.content)

        return _complete


class ComputerUseManager:
    """会话管理器（进程级单例）。"""

    def __init__(self, audit_dir: Path = _AUDIT_DIR):
        self._sessions: dict[str, ComputerUseSession] = {}
        self._audit_dir = Path(audit_dir)

    def create_session(self, start_url: str = "", headless: bool = True) -> ComputerUseSession:
        session_id = f"cu-{uuid.uuid4().hex[:10]}"
        session = ComputerUseSession(session_id, start_url=start_url, headless=headless)
        if start_url:
            session.current_url = start_url
        self._sessions[session_id] = session
        logger.info(f"创建 Computer Use 会话: {session_id}, dry_run={session.dry_run}")
        return session

    def get(self, session_id: str) -> Optional[ComputerUseSession]:
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [s.to_dict(include_steps=False) for s in self._sessions.values()]

    async def close_session(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None:
            return False
        await session.close()
        self._persist_audit(session)
        del self._sessions[session_id]
        return True

    def _persist_audit(self, session: ComputerUseSession) -> None:
        try:
            self._audit_dir.mkdir(parents=True, exist_ok=True)
            path = self._audit_dir / f"{session.session_id}_audit.json"
            path.write_text(
                json.dumps(session.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"持久化审计失败: {e}")


_manager: Optional[ComputerUseManager] = None


def get_computer_use_manager() -> ComputerUseManager:
    global _manager
    if _manager is None:
        _manager = ComputerUseManager()
    return _manager


def reset_computer_use_manager() -> None:
    global _manager
    _manager = None
