"""
Foundation Model helpers — Databricks serving endpoints.

Two entry points, both degrade gracefully so the app never hard-fails when the
model endpoint is unavailable or doesn't support a feature on Free Edition:

  * simple_chat(system, user)  -> a plain text completion (used for the agent's
    natural-language explanations). Uses serving_endpoints.query, the same call
    the weather app already runs successfully on Free Edition.
  * tool_chat(messages, tools) -> an OpenAI-style tool-calling turn (used by the
    in-app agent). Uses the Databricks-provided OpenAI-compatible client.

If a call fails, both return a sentinel (None / an error dict) and the caller
falls back to deterministic behavior — so the MCP + Agent Bricks path and the
deterministic planner still work regardless.
"""

import logging
import os

logger = logging.getLogger("llm")

AGENT_MODEL = os.environ.get("PLANNER_AGENT_MODEL", "databricks-meta-llama-3-3-70b-instruct")
EXPLAIN_MODEL = os.environ.get("PLANNER_EXPLAIN_MODEL", "databricks-meta-llama-3-1-8b-instruct")


def _workspace_client():
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient()


def simple_chat(system: str, user: str, model: str | None = None,
                max_tokens: int = 400, temperature: float = 0.2) -> str | None:
    """Plain text completion via serving_endpoints.query. Returns the text, or
    None if the endpoint is unavailable (caller should fall back)."""
    try:
        from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

        resp = _workspace_client().serving_endpoints.query(
            name=model or EXPLAIN_MODEL,
            messages=[
                ChatMessage(role=ChatMessageRole.SYSTEM, content=system),
                ChatMessage(role=ChatMessageRole.USER, content=user),
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content
    except Exception as exc:  # noqa: BLE001 - degrade to deterministic caller
        logger.warning("simple_chat failed (%s); caller will fall back.", exc)
        return None


def get_openai_client():
    """Return a Databricks-configured OpenAI client for tool-calling, or None if
    unavailable (missing `openai`, or endpoint not reachable)."""
    try:
        return _workspace_client().serving_endpoints.get_open_ai_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenAI client unavailable (%s).", exc)
        return None


def tool_chat(messages: list[dict], tools: list[dict], model: str | None = None,
              temperature: float = 0.1):
    """One tool-calling turn. Returns the response's `message` object (with
    `.content` and `.tool_calls`), or None if tool-calling isn't available so
    the agent can fall back to deterministic routing."""
    client = get_openai_client()
    if client is None:
        return None
    try:
        resp = client.chat.completions.create(
            model=model or AGENT_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=temperature,
        )
        return resp.choices[0].message
    except Exception as exc:  # noqa: BLE001 - e.g. endpoint rejects `tools`
        logger.warning("tool_chat failed (%s); agent will fall back.", exc)
        return None
