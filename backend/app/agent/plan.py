"""Plans: a short, validated list of tool calls. The model may *propose* one; nothing runs until it has been checked here.

A step is `{id, tool, args, when?, on_fail, retries?}`. An argument may point at an earlier result with "$s1.key" (a
whole-value reference keeps its type, so a list stays a list). `when` is a tiny condition over earlier results
("$s1.count > 0"), so a plan can branch without letting the model write code."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.capabilities import Registry

MAX_STEPS = 8
ON_FAIL = ("stop", "continue", "replan")
_REF = re.compile(r"\$(s\d+)((?:\.[A-Za-z0-9_]+)+)?")
_WHOLE = re.compile(r"^\$(s\d+)((?:\.[A-Za-z0-9_]+)*)$")
_COND = re.compile(r"^\s*(\$s\d+(?:\.[A-Za-z0-9_]+)*)\s*(==|!=|>=|<=|>|<)?\s*(.*?)\s*$")


@dataclass
class PlanStep:
    id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    when: str = ""
    on_fail: str = "continue"
    retries: int | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tool": self.tool, "args": self.args, "when": self.when, "on_fail": self.on_fail,
                "retries": self.retries, "note": self.note}


@dataclass
class Plan:
    steps: list[PlanStep]
    criteria: list[str] = field(default_factory=list)
    workflow: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"steps": [s.as_dict() for s in self.steps], "criteria": self.criteria, "workflow": self.workflow}


def plan_from_dict(d: dict[str, Any]) -> Plan:
    return Plan([PlanStep(s["id"], s["tool"], s.get("args") or {}, s.get("when") or "", s.get("on_fail") or "continue", s.get("retries"),
                          s.get("note") or "") for s in d.get("steps", [])], list(d.get("criteria") or []), d.get("workflow") or "")


def parse_plan(raw: dict[str, Any], registry: Registry, max_steps: int = MAX_STEPS,
               known: set[str] | None = None) -> tuple[Plan | None, str]:
    """Turn model output into a Plan, or say exactly what is wrong (so the model can be asked once to fix it)."""
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        return None, "A plan needs a non-empty 'steps' list."
    if len(steps_raw) > max_steps:
        return None, f"Too many steps ({len(steps_raw)}); at most {max_steps}."
    steps: list[PlanStep] = []
    seen: set[str] = set(known or ())
    for i, s in enumerate(steps_raw, 1):
        if not isinstance(s, dict):
            return None, f"Step {i} must be an object."
        sid = str(s.get("id") or f"s{len(seen) + 1}")
        if sid in seen or not re.fullmatch(r"s\d+", sid):
            return None, f"Step ids must be unique and look like s1, s2 (got '{sid}')."
        tool = str(s.get("tool") or "")
        args: dict[str, Any] = s["args"] if isinstance(s.get("args"), dict) else {}
        on_fail = str(s.get("on_fail") or "continue")
        if on_fail not in ON_FAIL:
            return None, f"{sid}: on_fail must be one of {', '.join(ON_FAIL)}."
        if registry.get(tool) is None:
            return None, f"{sid}: unknown tool '{tool}'. Use only the listed tools."
        for ref in _REFS_IN(args) + _REFS_IN({"when": s.get("when") or ""}):
            if ref not in seen:
                return None, f"{sid}: refers to ${ref}, which does not come earlier."
        if not _has_ref(args):
            _t, parsed, err = registry.validate(tool, args)
            if err:
                return None, f"{sid}: {err}"
        retries = s.get("retries")
        steps.append(PlanStep(sid, tool, args, str(s.get("when") or ""), on_fail, retries if isinstance(retries, int) else None,
                              str(s.get("note") or "")[:200]))
        seen.add(sid)
    crit = [str(c)[:160] for c in (raw.get("criteria") or []) if isinstance(c, str)][:6]
    return Plan(steps, crit), ""


def dependencies(step: PlanStep) -> set[str]:
    """Earlier steps this one needs (its arguments or condition read their results)."""
    return set(_walk(step.args)) | set(_walk(step.when))


def _walk(v: Any) -> list[str]:
    if isinstance(v, str):
        return [m.group(1) for m in _REF.finditer(v)]
    if isinstance(v, dict):
        return [r for x in v.values() for r in _walk(x)]
    if isinstance(v, list):
        return [r for x in v for r in _walk(x)]
    return []


def _REFS_IN(v: Any) -> list[str]:  # noqa: N802
    return _walk(v)


def _has_ref(v: Any) -> bool:
    return bool(_walk(v))


# ---------------------------------------------------------------- reference resolution
def _lookup(results: dict[str, dict[str, Any]], sid: str, path: str) -> Any:
    cur: Any = results.get(sid)
    for key in [p for p in path.split(".") if p]:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        elif isinstance(cur, dict) and isinstance(cur.get("data"), dict) and key in cur["data"]:
            cur = cur["data"][key]
        else:
            return None
    return cur


def resolve_args(v: Any, results: dict[str, dict[str, Any]]) -> Any:
    """Substitute "$s1.key" references with earlier results (a whole-value reference keeps its type)."""
    if isinstance(v, str):
        m = _WHOLE.match(v)
        if m:
            return _lookup(results, m.group(1), m.group(2))
        return _REF.sub(lambda mm: str(_lookup(results, mm.group(1), mm.group(2) or "")), v)
    if isinstance(v, dict):
        return {k: resolve_args(x, results) for k, x in v.items()}
    if isinstance(v, list):
        return [resolve_args(x, results) for x in v]
    return v


def _coerce(text: str) -> Any:
    t = text.strip().strip("'\"")
    if t.lower() in ("true", "false"):
        return t.lower() == "true"
    try:
        return float(t) if "." in t else int(t)
    except ValueError:
        return t


def eval_when(cond: str, results: dict[str, dict[str, Any]]) -> bool:
    """`$s1.count > 0`, `$s1.status == succeeded`, `$s2.verified` (truthy). An empty condition is true; unparseable is false."""
    if not cond.strip():
        return True
    m = _COND.match(cond)
    if not m:
        return False
    ref = _WHOLE.match(m.group(1))
    if not ref:
        return False
    left = _lookup(results, ref.group(1), ref.group(2))
    op, right = m.group(2), m.group(3)
    if op is None:
        return bool(left)
    r = _coerce(right)
    try:
        return {"==": left == r, "!=": left != r, ">": left > r, "<": left < r, ">=": left >= r, "<=": left <= r}[op]
    except TypeError:
        return False


# ---------------------------------------------------------------- built-in workflows
def organize_folder(folder: str) -> Plan:
    """Preview, ask, then move. The preview is read-only; the move needs approval and is verified file by file."""
    return Plan([
        PlanStep("s1", "files.plan_organize", {"folder": folder}, note="Look at the folder and decide where each file goes"),
        PlanStep("s2", "files.apply_moves", {"folder": folder, "moves": "$s1.moves"}, when="$s1.count > 0",
                 note="Move the files (needs your approval)"),
    ], ["every proposed file is in its new folder", "nothing was overwritten"], "organize_folder")


BUILTIN_WORKFLOWS: dict[str, dict[str, Any]] = {
    "organize_folder": {"description": "Sort the files of an approved folder into folders by type (preview, approval, move, check).",
                        "keywords": ["organize", "folder"], "params": ["folder"]},
}
