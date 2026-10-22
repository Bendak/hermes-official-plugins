"""Camofox implementations of the browser_* tools.

Each function returns the JSON string the model sees. Handlers (``handle_<tool>(args, **kw)``)
adapt the registry's call shape; ``task_id`` arrives in ``kw`` and keys the Camofox tab.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from typing import Any, Callable, Optional

import requests

from hermes_cli.config import cfg_get, read_raw_config
from tools.registry import tool_error

from . import camofox_client as client
from . import camofox_policy as policy

logger = logging.getLogger(__name__)

VISION_TASK = "camofox_vision"
_SCROLL_REPEAT = 5  # the Camofox REST scroll has a fixed step; five steps ≈ core's half-viewport-plus
_EVAL_UNSUPPORTED = ("JavaScript evaluation is not supported by this Camofox server. "
                     "Use browser_snapshot or browser_vision to inspect page state.")


def _dumps(payload: Any, **kw: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, **kw)


def _fail(message: str) -> str:
    return tool_error(message, success=False)


# ---- Session plumbing ----

def _snapshot_data(session: dict[str, Any]) -> dict:
    return client.get(client.tab_path(session, "snapshot"), params=client.user_params(session))


def _fetch_snapshot(session: dict[str, Any]) -> tuple[str, int]:
    """``(snapshot_text, refs_count)``, cut at ``browser.snapshot_threshold`` with the full tree stored.

    Camoufox's aria snapshot prints a filled input's value (a typed or vault-filled password included),
    so the text crosses the same redaction boundary as every other browser read."""
    data = _snapshot_data(session)
    snapshot, threshold = policy.redact_text(data.get("snapshot", "")), policy.snapshot_threshold()
    if len(snapshot) > threshold:
        snapshot = policy.truncate_snapshot(snapshot, max_chars=threshold)
    return snapshot, data.get("refsCount", 0)


def _navigate_tab(task_id: Optional[str], browser_url: str) -> tuple[dict[str, Any], dict]:
    """Open ``browser_url`` in the task's tab (creating it if missing) → ``(session, response)``.
    A 404 (the server garbage-collected the tab) or 410 (Camofox >= 1.17 after a browser restart)
    means the cached tab id is dead: recreate instead of failing every call from now on."""
    session = client.get_session(task_id)
    if session["tab_id"]:
        try:
            data = client.post(client.tab_path(session, "navigate"),
                               {"userId": session["user_id"], "url": browser_url}, timeout=60)
            return session, data
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code not in (404, 410):
                raise
            logger.warning("Camofox tab %s returned %s — tab is gone. Creating a fresh tab.",
                           session["tab_id"], e.response.status_code)
            session["tab_id"] = None
    return client.ensure_tab(task_id, browser_url), {"ok": True, "url": browser_url}


_TAB_GONE_ERROR = ("The browser tab is gone (the Camofox server reaped it or restarted). "
                   "Call browser_navigate to open the page again.")


def _tab_gone(exc: BaseException) -> bool:
    return isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code in (404, 410)


def _run_on_tab(session: dict[str, Any], body: Callable[[dict[str, Any]], str]) -> str:
    """``body(session)``; when the server says the cached tab is gone, resync once and retry."""
    tracked = session["tab_id"]
    try:
        return body(session)
    except requests.HTTPError as e:
        if not _tab_gone(e):
            raise
        if not client.resync_tab(session):
            return _fail(_TAB_GONE_ERROR)
        if session["tab_id"] == tracked:  # the tab is alive: the 404 was about something else
            raise
        logger.info("Camofox tab %s was gone; continuing in live tab %s", tracked, session["tab_id"])
        return body(session)


def _with_tab(task_id: Optional[str], body: Callable[[dict[str, Any]], str]) -> str:
    """Run ``body(session)`` on the task's existing tab; no tab → the navigate-first error."""
    try:
        session = client.get_session(task_id)
        if not session["tab_id"]:
            return _fail(client.NO_SESSION_ERROR)
        return _run_on_tab(session, body)
    except Exception as e:
        return _fail(str(e))


def _tab_action(task_id: Optional[str], suffix: str, body: dict[str, Any], result: Callable[[dict], dict]) -> str:
    """POST ``body`` to ``/tabs/<id>/<suffix>`` and build the tool result from the response."""
    return _with_tab(task_id, lambda session: _dumps(
        result(client.post(client.tab_path(session, suffix), {"userId": session["user_id"], **body}))))


# ---- Tools ----

def _navigate_title(session: dict[str, Any], data: dict) -> str:
    """The page title: from the navigate reply when present, else from this task's own tab in
    ``GET /tabs`` (Camofox navigate replies carry no title). Lookup failure degrades to ""."""
    response_title = data.get("title")
    if isinstance(response_title, str) and response_title:
        return response_title
    try:
        tabs = client.get("/tabs", params=client.user_params(session)).get("tabs", [])
        for tab in tabs if isinstance(tabs, list) else []:
            if isinstance(tab, dict) and tab.get("tabId") == session["tab_id"]:
                title = tab.get("title")
                return title if isinstance(title, str) else ""
    except Exception as exc:
        logger.debug("Camofox title lookup failed for tab %s: %s", session.get("tab_id"), exc)
    return ""


def browser_navigate(url: str, task_id: Optional[str] = None) -> str:
    """Navigate to ``url``; the result carries the title and a compact snapshot."""
    url, policy_error = policy.navigation_policy_error(url)
    if policy_error is not None:
        return _dumps(policy_error)
    try:
        browser_url, rewrite_info = client.rewrite_loopback_url(url)
        session, data = _navigate_tab(task_id, browser_url)
        result: dict[str, Any] = {"success": True, "url": data.get("url", browser_url),
                                  "title": _navigate_title(session, data)}
        if rewrite_info:
            result["requested_url"], result["url_rewrite"] = url, rewrite_info
            result["warning"] = ("Rewrote loopback URL for Docker-hosted Camofox: "
                                 f"{rewrite_info['from']} -> {rewrite_info['to']}")
        vnc = client.get_vnc_url()
        if vnc:
            result["vnc_url"] = vnc
            result["vnc_hint"] = ("Browser is visible via VNC. "
                                  "Share this link with the user so they can watch the browser live.")
        try:  # a compact snapshot so the model can act immediately
            result["snapshot"], result["element_count"] = _fetch_snapshot(session)
        except Exception:
            pass  # navigation succeeded; the snapshot is a bonus
        return _dumps(result)
    except requests.HTTPError as e:
        return _fail(f"Navigation failed: {e}")
    except requests.ConnectionError:
        return _dumps({"success": False, "error": client.connection_error_message()})
    except Exception as e:
        return _fail(str(e))


def browser_snapshot(task_id: Optional[str] = None) -> str:
    def body(session):
        snapshot, refs_count = _fetch_snapshot(session)
        return _dumps({"success": True, "snapshot": snapshot, "element_count": refs_count})
    return _with_tab(task_id, body)


def browser_click(ref: str, task_id: Optional[str] = None) -> str:
    clean_ref = ref.lstrip("@")  # tool convention prefixes refs with @; Camofox wants the bare ref
    return _tab_action(task_id, "click", {"ref": clean_ref},
                       lambda data: {"success": True, "clicked": clean_ref, "url": data.get("url", "")})


def browser_type(ref: str, text: str, task_id: Optional[str] = None) -> str:
    """Type ``text`` into ``ref``; the raw text goes to the page, the result masks secret-looking text."""
    try:
        session = client.get_session(task_id)
        if not session["tab_id"]:
            return _fail(client.NO_SESSION_ERROR)
        clean_ref = ref.lstrip("@")

        def body(sess):
            client.post(client.tab_path(sess, "type"), {"userId": sess["user_id"], "ref": clean_ref, "text": text})
            return _dumps(policy.redact_typed_text(
                {"success": True, "typed": policy.redact_text(text), "element": clean_ref}, text))
        return policy.redact_typed_text(_run_on_tab(session, body), text)
    except Exception as e:
        return _fail(policy.redact_typed_text(str(e), text))


def browser_scroll(direction: str, task_id: Optional[str] = None) -> str:
    if direction not in {"up", "down"}:
        return _dumps({"success": False, "error": f"Invalid direction '{direction}'. Use 'up' or 'down'."})
    result = ""
    for _ in range(_SCROLL_REPEAT):
        result = _tab_action(task_id, "scroll", {"direction": direction},
                             lambda data: {"success": True, "scrolled": direction})
    return result


def browser_back(task_id: Optional[str] = None) -> str:
    return _tab_action(task_id, "back", {}, lambda data: {"success": True, "url": data.get("url", "")})


def browser_press(key: str, task_id: Optional[str] = None) -> str:
    return _tab_action(task_id, "press", {"key": key}, lambda data: {"success": True, "pressed": key})


def _parse_snapshot_images(snapshot: str) -> list[dict[str, str]]:
    """Images from an accessibility snapshot: ``img "alt"`` entries with the URL on the following
    ``/url:`` line."""
    images = []
    lines = snapshot.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(("- img ", "img ")):
            continue
        alt_match = re.search(r'img\s+"([^"]*)"', stripped)
        url_match = re.search(r"/url:\s*(\S+)", lines[i + 1].strip()) if i + 1 < len(lines) else None
        alt, src = (alt_match.group(1) if alt_match else ""), (url_match.group(1) if url_match else "")
        if alt or src:
            images.append({"src": src, "alt": alt})
    return images


def browser_get_images(task_id: Optional[str] = None) -> str:
    def body(session):
        images = _parse_snapshot_images(_snapshot_data(session).get("snapshot", ""))
        return _dumps({"success": True, "images": images, "count": len(images)})
    return _with_tab(task_id, body)


def _parse_eval_value(raw: Any) -> Any:
    """Camofox returns the JS value; a JSON string is parsed so the model gets structured data."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return raw


def _evaluate(expression: str, task_id: Optional[str]) -> str:
    try:
        # Never open a tab just to evaluate: a blank tab in a shared Camofox profile is a side effect,
        # and with adopt_existing_tab it could later be adopted in place of the real one.
        session = client.get_session(task_id)
        if not session["tab_id"]:
            return _fail(client.NO_SESSION_ERROR)

        def body(sess):
            resp = client.post(client.tab_path(sess, "evaluate"), {"expression": expression, "userId": sess["user_id"]})
            parsed = _parse_eval_value(resp.get("result") if isinstance(resp, dict) else resp)
            return _dumps({"success": True, "result": policy.redact_output(parsed),
                           "result_type": type(parsed).__name__}, default=str)
        return _run_on_tab(session, body)
    except Exception as e:
        if any(code in str(e) for code in ("404", "405", "501")):  # a server without /evaluate
            return _dumps({"success": False, "error": _EVAL_UNSUPPORTED})
        return _fail(str(e))


def browser_console(expression: Optional[str] = None, task_id: Optional[str] = None) -> str:
    """Evaluate ``expression`` in the page; without one, say console capture is unavailable."""
    if expression is not None:
        refusal = policy.eval_policy_error(expression)
        if refusal:
            return _dumps({"success": False, "error": refusal})
        return _evaluate(expression, task_id)
    return _dumps({
        "success": True, "console_messages": [], "js_errors": [], "total_messages": 0, "total_errors": 0,
        "note": "Console log capture is not available with the Camofox backend. "
                "Use browser_snapshot or browser_vision to inspect page state."})


def _vision_settings() -> tuple[float, float]:
    """``auxiliary.vision`` ``(timeout, temperature)``; 120 s / 0.1 on any config error."""
    try:
        cfg = cfg_get(read_raw_config(), "auxiliary", "vision", default={}) or {}
        return float(cfg.get("timeout", 120)), float(cfg.get("temperature", 0.1))
    except Exception:
        return 120.0, 0.1


def _save_screenshot(content: bytes) -> str:
    """Write PNG bytes under ``$HERMES_HOME/browser_screenshots`` and return the path."""
    from hermes_constants import get_hermes_home
    screenshots_dir = get_hermes_home() / "browser_screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    path = screenshots_dir / f"browser_screenshot_{uuid.uuid4().hex[:8]}.png"
    path.write_bytes(content)
    return str(path)


def browser_vision(question: str, task_id: Optional[str] = None, *, llm: Any = None) -> str:
    """Screenshot the tab and answer ``question`` with the auxiliary vision model (``ctx.llm``,
    task ``camofox_vision``, which inherits ``auxiliary.vision``)."""
    def body(session):
        resp = client.get_raw(client.tab_path(session, "screenshot"), params=client.user_params(session))
        screenshot_path = _save_screenshot(resp.content)
        img_b64 = base64.b64encode(resp.content).decode("utf-8")
        timeout, temperature = _vision_settings()
        try:
            result = llm.complete(
                [{"role": "user", "content": [
                    {"type": "text", "text": f"Analyze this browser screenshot and answer: {question}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}]}],
                task=VISION_TASK, temperature=temperature, timeout=timeout, purpose="browser_vision")
        except Exception as e:
            logger.warning("browser_vision analysis failed: %s", e)
            return _dumps({"success": False, "error": f"Error during vision analysis: {e!s}",
                           "screenshot_path": screenshot_path,
                           "note": "Screenshot was captured but vision analysis failed. "
                                   "You can still share it via MEDIA:<path>."})
        analysis = (result.text or "").strip() or "Vision analysis returned no content."
        # Secrets the vision model read off the page never reach the conversation.
        return _dumps({"success": True, "analysis": policy.redact_text(analysis), "screenshot_path": screenshot_path})
    return _with_tab(task_id, body)


def check_camofox_configured() -> bool:
    """Tools are offered once ``CAMOFOX_URL`` is set (no network probe: this runs at schema build)."""
    return bool(client.get_camofox_url())


def cleanup_task(task_id: Optional[str]) -> None:
    """End-of-session cleanup: managed identities keep their server-side profile, ephemeral
    sessions are deleted on the server."""
    if not client.soft_cleanup(task_id):
        warning = client.close_session(task_id)
        if warning:
            logger.debug("Camofox close for task %s: %s", task_id, warning)


# ---- Registry adapters ----

def _kw_task(kw: dict[str, Any]) -> Optional[str]:
    return kw.get("task_id")


def handle_navigate(args: dict, **kw: Any) -> str:
    return browser_navigate(str(args.get("url", "")), task_id=_kw_task(kw))


def handle_snapshot(args: dict, **kw: Any) -> str:
    return browser_snapshot(task_id=_kw_task(kw))


def handle_click(args: dict, **kw: Any) -> str:
    return browser_click(str(args.get("ref", "")), task_id=_kw_task(kw))


def handle_type(args: dict, **kw: Any) -> str:
    return browser_type(str(args.get("ref", "")), str(args.get("text", "")), task_id=_kw_task(kw))


def handle_scroll(args: dict, **kw: Any) -> str:
    return browser_scroll(str(args.get("direction", "down")), task_id=_kw_task(kw))


def handle_back(args: dict, **kw: Any) -> str:
    return browser_back(task_id=_kw_task(kw))


def handle_press(args: dict, **kw: Any) -> str:
    return browser_press(str(args.get("key", "")), task_id=_kw_task(kw))


def handle_get_images(args: dict, **kw: Any) -> str:
    return browser_get_images(task_id=_kw_task(kw))


def handle_console(args: dict, **kw: Any) -> str:
    expression = args.get("expression")
    return browser_console(None if expression is None else str(expression), task_id=_kw_task(kw))


def make_vision_handler(llm_source: Callable[[], Any]) -> Callable[..., str]:
    def handle_vision(args: dict, **kw: Any) -> str:
        return browser_vision(str(args.get("question", "")), task_id=_kw_task(kw), llm=llm_source())
    return handle_vision
