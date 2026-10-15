"""Two small MCP servers (selected by SERVER=notes|utils) used as the 2nd and 3rd upstreams.

notes : a persistent team scratchpad  (add_note, list_notes, search_notes, delete_note, ping)
utils : stateless helpers             (current_time, calculate, convert_currency, ping)

Both expose `ping` on purpose: it makes the gateway's tool-name collision handling visible
(notes__ping vs utils__ping). `notes` + `woocommerce` make a natural two-server prompt:
"summarise today's orders and save the summary as a note".
"""
from __future__ import annotations

import ast
import hmac
import json
import operator
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

PORT = int(os.environ.get("PORT", "8000"))
TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
DATA = Path(os.environ.get("DATA_DIR", "/tmp/notes"))


def _mcp(name: str, instructions: str) -> FastMCP:
    m = FastMCP(name, instructions=instructions, host="0.0.0.0", port=PORT, stateless_http=True, json_response=True,
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @m.custom_route("/healthz", methods=["GET"])
    async def _h(_: Request):
        return JSONResponse({"status": "ok"})
    return m


# --------------------------------------------------------------------- notes
def build_notes() -> FastMCP:
    m = _mcp("notes", "Shared team notes.")
    lock, f = threading.Lock(), DATA / "notes.json"

    def load() -> list[dict]:
        try:
            return json.loads(f.read_text())
        except (FileNotFoundError, ValueError):
            return []

    def save(n: list[dict]):
        DATA.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(n))

    @m.tool()
    def add_note(title: str, body: str) -> dict:
        """Save a note (e.g. a summary of today's orders)."""
        with lock:
            notes = load()
            note = {"id": max([n["id"] for n in notes], default=0) + 1, "title": title[:200], "body": body[:5000],
                    "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
            save(notes + [note])
        return note

    @m.tool()
    def list_notes(limit: int = 20) -> list[dict]:
        """List the most recent notes."""
        return load()[-max(1, min(limit, 100)):][::-1]

    @m.tool()
    def search_notes(query: str) -> list[dict]:
        """Case-insensitive search over note titles and bodies."""
        q = query.lower()
        return [n for n in load() if q in n["title"].lower() or q in n["body"].lower()]

    @m.tool()
    def delete_note(note_id: int) -> str:
        """Delete a note by id."""
        with lock:
            notes = load()
            save([n for n in notes if n["id"] != note_id])
        return "deleted" if any(n["id"] == note_id for n in notes) else "no such note"

    @m.tool()
    def ping() -> str:
        """Health check; returns the server name."""
        return "pong from notes"
    return m


# --------------------------------------------------------------------- utils
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}


def safe_eval(expr: str) -> float:
    """Arithmetic only: walks the AST and allows nothing but numbers and + - * / ** %."""
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 100:
                raise ValueError("exponent too large")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("only arithmetic expressions are allowed")
    if len(expr) > 200:
        raise ValueError("expression too long")
    return ev(ast.parse(expr, mode="eval").body)


# Static, illustrative rates (USD base). A real deployment would call a rates API.
RATES = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79, "INR": 83.2, "JPY": 149.5}


def build_utils() -> FastMCP:
    m = _mcp("utils", "Small utilities: time, arithmetic, currency.")

    @m.tool()
    def current_time(timezone: str = "UTC") -> str:
        """Current date/time in an IANA timezone, e.g. Asia/Kolkata."""
        try:
            return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")
        except ZoneInfoNotFoundError:
            raise ValueError(f"unknown timezone '{timezone}'")

    @m.tool()
    def calculate(expression: str) -> float:
        """Evaluate an arithmetic expression such as '(19.99*3)+4.5'."""
        return safe_eval(expression)

    @m.tool()
    def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict:
        """Convert using fixed demo rates (USD, EUR, GBP, INR, JPY)."""
        a, b = from_currency.upper(), to_currency.upper()
        if a not in RATES or b not in RATES:
            raise ValueError(f"supported currencies: {', '.join(RATES)}")
        return {"amount": round(amount / RATES[a] * RATES[b], 2), "currency": b, "note": "static demo rates"}

    @m.tool()
    def ping() -> str:
        """Health check; returns the server name."""
        return "pong from utils"
    return m


class BearerAuth:
    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token and scope["path"] != "/healthz":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(supplied.encode(), self.token.encode()):
                return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        await self.app(scope, receive, send)


def factory():
    kind = os.environ.get("SERVER", "notes")
    mcp = {"notes": build_notes, "utils": build_utils}[kind]()
    return BearerAuth(mcp.streamable_http_app(), TOKEN)
