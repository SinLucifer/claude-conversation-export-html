#!/usr/bin/env python3
"""Interactive CLI exporter for Claude Code conversations."""

from __future__ import annotations

import argparse
import curses
import datetime as dt
import html
import json
import os
from pathlib import Path
from typing import Any, Iterable, List

PRIMARY_ROLES = {"user", "assistant"}
TUI_PAGE_SIZE = 15


class ExportError(Exception):
    """User-facing export error."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="claude-html-start",
        description=(
            "Export Claude Code conversations to self-contained HTML with interactive selection."
        )
    )
    parser.add_argument(
        "-i",
        "--input",
        default="~/.claude/projects",
        help="Input JSONL file or directory (default: ~/.claude/projects)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path. If omitted, an auto name is used.",
    )
    parser.add_argument(
        "-s",
        "--select",
        help="Conversation indexes, e.g. 1,3-5. Only valid for directory input.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Export all found conversations (skip selection prompt).",
    )
    parser.add_argument(
        "--title",
        default="Claude Code Conversations",
        help="Title used in HTML output.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable prompts. Directory input requires --all or --select.",
    )
    return parser.parse_args()


def find_jsonl_files(input_path: Path) -> List[Path]:
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() == ".jsonl" else []
    if not input_path.exists():
        return []
    return sorted([p for p in input_path.rglob("*.jsonl") if p.is_file()])


def read_jsonl(path: Path) -> List[dict[str, Any]]:
    events: List[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    parsed["_source_line"] = idx
                    events.append(parsed)
            except json.JSONDecodeError:
                events.append(
                    {
                        "type": "parse_error",
                        "timestamp": "",
                        "message": {"content": f"Invalid JSON at line {idx}"},
                        "raw": line,
                        "_source_line": idx,
                    }
                )
    return events


def extract_session_id_from_events(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        session_id = event.get("sessionId")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def parse_timestamp_for_sort(value: str) -> float | None:
    if not value:
        return None
    try:
        norm = value.replace("Z", "+00:00") if value.endswith("Z") else value
        return dt.datetime.fromisoformat(norm).timestamp()
    except ValueError:
        return None


def build_conversation_units(files: list[Path]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Path]] = {}
    for path in files:
        events = read_jsonl(path)
        session_id = extract_session_id_from_events(events)
        key = session_id if session_id else f"path:{path}"
        grouped.setdefault(key, []).append(path)

    units: list[dict[str, Any]] = []
    for key, group_files in grouped.items():
        sorted_files = sorted(group_files)
        primary = next((p for p in sorted_files if "/subagents/" not in str(p)), sorted_files[0])
        mtime_ts = max((p.stat().st_mtime if p.exists() else 0.0) for p in sorted_files)
        units.append(
            {
                "id": key,
                "session_id": None if key.startswith("path:") else key,
                "files": sorted_files,
                "primary_file": primary,
                "mtime_ts": mtime_ts,
                "subagent_file_count": sum(1 for p in sorted_files if "/subagents/" in str(p)),
            }
        )
    units.sort(key=lambda unit: unit["mtime_ts"], reverse=True)
    return units


def read_conversation_events(unit: dict[str, Any]) -> list[dict[str, Any]]:
    merged: list[tuple[float, int, int, dict[str, Any]]] = []
    for file_index, path in enumerate(unit["files"]):
        events = read_jsonl(path)
        for event in events:
            event_copy = dict(event)
            event_copy["_source_file"] = str(path)
            timestamp = parse_timestamp_for_sort(extract_timestamp(event_copy))
            sort_ts = timestamp if timestamp is not None else float("inf")
            source_line = int(event_copy.get("_source_line", 0))
            merged.append((sort_ts, file_index, source_line, event_copy))
    merged.sort(key=lambda item: (item[0], item[1], item[2]))
    ordered = [item[3] for item in merged]

    # Build subagent linkage maps from explicit sidechain events first.
    tool_to_agent: dict[str, str] = {}
    uuid_to_agent: dict[str, str] = {}

    for event in ordered:
        agent_id = event.get("agentId")
        if not isinstance(agent_id, str) or not agent_id.strip():
            continue
        agent_id = agent_id.strip()
        event_uuid = event.get("uuid")
        if isinstance(event_uuid, str) and event_uuid.strip():
            uuid_to_agent[event_uuid.strip()] = agent_id
        for key in ("parentToolUseID", "toolUseID"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                tool_to_agent[value.strip()] = agent_id
        message = event.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = str(item.get("type", "")).lower()
                    if item_type == "tool_use":
                        tool_id = item.get("id")
                        if isinstance(tool_id, str) and tool_id.strip():
                            tool_to_agent[tool_id.strip()] = agent_id

    # Annotate events that are part of subagent flows but logged in main history.
    for event in ordered:
        if isinstance(event.get("agentId"), str) and event.get("agentId", "").strip():
            continue

        inferred_agent: str | None = None
        data = event.get("data")
        if isinstance(data, dict):
            data_message = data.get("message")
            if isinstance(data_message, dict):
                nested_uuid = data_message.get("uuid")
                if (
                    isinstance(nested_uuid, str)
                    and nested_uuid.strip()
                    and nested_uuid.strip() in uuid_to_agent
                ):
                    inferred_agent = uuid_to_agent[nested_uuid.strip()]
        for key in ("sourceToolAssistantUUID",):
            value = event.get(key)
            if isinstance(value, str) and value.strip() and value.strip() in uuid_to_agent:
                inferred_agent = uuid_to_agent[value.strip()]
                break
        if not inferred_agent:
            for key in ("parentToolUseID", "toolUseID"):
                value = event.get(key)
                if isinstance(value, str) and value.strip() and value.strip() in tool_to_agent:
                    inferred_agent = tool_to_agent[value.strip()]
                    break
        if not inferred_agent:
            message = event.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        item_type = str(item.get("type", "")).lower()
                        if item_type == "tool_result":
                            tool_use_id = item.get("tool_use_id")
                            if (
                                isinstance(tool_use_id, str)
                                and tool_use_id.strip()
                                and tool_use_id.strip() in tool_to_agent
                            ):
                                inferred_agent = tool_to_agent[tool_use_id.strip()]
                                break
                    if inferred_agent:
                        pass
        if inferred_agent:
            event["_inferred_agent_id"] = inferred_agent

    return ordered


def isoformat(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(value).isoformat(sep=" ", timespec="seconds")
        except (ValueError, OSError):
            return str(value)
    if isinstance(value, str):
        return value
    return str(value)


def extract_timestamp(event: dict[str, Any]) -> str:
    for key in ("timestamp", "created_at", "createdAt", "time", "date"):
        if key in event:
            return isoformat(event.get(key))
    message = event.get("message")
    if isinstance(message, dict):
        for key in ("timestamp", "created_at", "createdAt", "time"):
            if key in message:
                return isoformat(message.get(key))
    return ""


def extract_role(event: dict[str, Any]) -> str:
    for key in ("role", "type", "event"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    message = event.get("message")
    if isinstance(message, dict):
        role = message.get("role")
        if isinstance(role, str) and role:
            return role.lower()
    return "unknown"


def flatten_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [flatten_content(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        vtype = value.get("type")
        if vtype in {"text", "input_text", "output_text"}:
            return flatten_content(value.get("text") or value.get("content"))
        for key in (
            "text",
            "content",
            "message",
            "input",
            "output",
            "result",
            "error",
            "name",
            "tool_name",
            "arguments",
        ):
            if key in value:
                text = flatten_content(value.get(key))
                if text:
                    return text
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def extract_text(event: dict[str, Any]) -> str:
    message = event.get("message")
    if isinstance(message, dict):
        for key in ("content", "text"):
            if key in message:
                text = flatten_content(message.get(key))
                if text:
                    return text
    for key in ("content", "text", "input", "output", "result", "error", "raw"):
        if key in event:
            text = flatten_content(event.get(key))
            if text:
                return text
    return json.dumps(event, ensure_ascii=False, indent=2)


def secondary_category(event: dict[str, Any]) -> str:
    if resolve_subagent_id(event):
        return "subagent"
    role = extract_role(event)
    if role == "system":
        return "system"
    call_name = extract_call_name(event).lower()
    text = extract_text(event).lower()
    raw = json.dumps(event, ensure_ascii=False).lower()
    if "skill" in call_name or "<command-name>/skill" in text or "/skill" in text:
        return "skill"
    if "mcp" in call_name or "mcp" in raw:
        return "mcp"
    if has_structured_tool_payload(event):
        return "tool"
    if "subagent" in raw:
        return "subagent"
    return "other"


def is_secondary_event(event: dict[str, Any], role: str, text: str) -> bool:
    if resolve_subagent_id(event):
        return True
    if has_structured_tool_payload(event):
        return True
    if role == "system":
        return True
    if role in {"progress", "file-history-snapshot"}:
        return True
    if "<command-name>/skill" in text or "<command-message>skill" in text:
        return True
    if role not in PRIMARY_ROLES:
        return True
    return False


def has_structured_tool_payload(event: dict[str, Any]) -> bool:
    if any(key in event for key in ("toolUseResult", "sourceToolAssistantUUID", "parentToolUseID", "toolUseID", "tool_name", "tool")):
        return True
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", "")).lower()
        if item_type in {"tool_use", "tool_result"}:
            return True
    return False


def extract_call_name(event: dict[str, Any]) -> str:
    for key in ("tool_name", "tool", "name"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type", "")).lower()
                if item_type == "tool_use":
                    name = item.get("name")
                    if isinstance(name, str) and name.strip():
                        return name.strip()
                if item_type == "tool_result":
                    tool_use_id = item.get("tool_use_id")
                    if isinstance(tool_use_id, str) and tool_use_id.strip():
                        return tool_use_id.strip()
    data = event.get("data")
    if isinstance(data, dict):
        data_message = data.get("message")
        if isinstance(data_message, dict):
            nested = data_message.get("message")
            if isinstance(nested, dict):
                nested_content = nested.get("content")
                if isinstance(nested_content, list):
                    for item in nested_content:
                        if not isinstance(item, dict):
                            continue
                        item_type = str(item.get("type", "")).lower()
                        if item_type == "tool_use":
                            name = item.get("name")
                            if isinstance(name, str) and name.strip():
                                return name.strip()
                        if item_type == "tool_result":
                            tool_use_id = item.get("tool_use_id")
                            if isinstance(tool_use_id, str) and tool_use_id.strip():
                                return tool_use_id.strip()
    return ""


def resolve_subagent_id(event: dict[str, Any]) -> str | None:
    agent_id = event.get("agentId")
    if isinstance(agent_id, str) and agent_id.strip():
        return agent_id.strip()
    inferred = event.get("_inferred_agent_id")
    if isinstance(inferred, str) and inferred.strip():
        return inferred.strip()
    return None


def extract_flow_name(event: dict[str, Any]) -> str:
    agent_id = resolve_subagent_id(event)
    if agent_id:
        return f"agent:{agent_id}"
    return ""


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    role = extract_role(event)
    text = extract_text(event)
    secondary = is_secondary_event(event, role, text)
    category = secondary_category(event) if secondary else "primary"
    return {
        "role": role,
        "timestamp": extract_timestamp(event),
        "text": text,
        "secondary": secondary,
        "category": category,
        "flow_name": extract_flow_name(event),
        "call_name": extract_call_name(event),
        "raw": event,
    }


def first_user_preview(events: Iterable[dict[str, Any]]) -> str:
    for event in events:
        normalized = normalize_event(event)
        if normalized["role"] == "user":
            text = normalized["text"].strip().replace("\n", " ")
            return text[:80] + ("..." if len(text) > 80 else "")
    return "(no user message)"


def count_primary_secondary(events: Iterable[dict[str, Any]]) -> tuple[int, int]:
    primary = 0
    secondary = 0
    for event in events:
        normalized = normalize_event(event)
        if normalized["secondary"]:
            secondary += 1
        else:
            primary += 1
    return primary, secondary


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def compress_middle(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 5:
        return truncate(text, width)
    head = (width - 3) // 2
    tail = width - 3 - head
    return f"{text[:head]}...{text[-tail:]}"


def format_mtime(path: Path) -> str:
    try:
        stamp = dt.datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return "-"
    return stamp.strftime("%Y-%m-%d %H:%M")


def parse_selection(selection: str, allowed_indexes: set[int]) -> List[int]:
    selection = selection.strip().lower()
    if selection in {"", "all", "*"}:
        return sorted(allowed_indexes)

    chosen: set[int] = set()
    for part in selection.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            pieces = part.split("-", 1)
            if len(pieces) != 2:
                raise ExportError(f"Invalid range: {part}")
            start, end = pieces
            if not start.isdigit() or not end.isdigit():
                raise ExportError(f"Invalid range: {part}")
            lo = int(start)
            hi = int(end)
            if lo > hi:
                lo, hi = hi, lo
            for idx in range(lo, hi + 1):
                if idx not in allowed_indexes:
                    raise ExportError(f"Selection out of bounds: {part}")
                chosen.add(idx)
        else:
            if not part.isdigit():
                raise ExportError(f"Invalid index: {part}")
            idx = int(part)
            if idx not in allowed_indexes:
                raise ExportError(f"Selection out of bounds: {part}")
            chosen.add(idx)

    if not chosen:
        raise ExportError("No conversations selected")
    return sorted(chosen)


def prompt_selection(
    units: list[dict[str, Any]],
    previews: list[str],
    counts: list[int],
    primary_counts: list[int],
    secondary_counts: list[int],
) -> List[int]:
    rows = build_selection_rows(units, previews, counts, primary_counts, secondary_counts)
    if not (os.isatty(0) and os.isatty(1)):
        raise ExportError("Interactive selection requires a TTY terminal")
    try:
        return curses.wrapper(run_selection_tui, rows)
    except curses.error as exc:
        raise ExportError(f"Failed to initialize terminal UI: {exc}") from exc


def build_selection_rows(
    units: list[dict[str, Any]],
    previews: list[str],
    counts: list[int],
    primary_counts: list[int],
    secondary_counts: list[int],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    primary_files = [unit["primary_file"] for unit in units]
    root = Path(os.path.commonpath(primary_files)) if len(primary_files) > 1 else primary_files[0].parent
    for idx, unit in enumerate(units, 1):
        path = unit["primary_file"]
        rel = str(path)
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            pass
        subagent_count = int(unit.get("subagent_file_count", 0))
        preview = previews[idx - 1]
        if subagent_count > 0:
            preview = f"[+{subagent_count} subagent file(s)] {preview}"
        rows.append(
            {
                "idx": idx,
                "path": path,
                "rel_path": rel,
                "mtime_ts": float(unit.get("mtime_ts", 0.0)),
                "events": counts[idx - 1],
                "primary_events": primary_counts[idx - 1],
                "secondary_events": secondary_counts[idx - 1],
                "preview": preview,
                "mtime": format_mtime(path),
            }
        )
    return rows


def filter_rows(rows: list[dict[str, Any]], keyword: str) -> list[dict[str, Any]]:
    if not keyword:
        return rows
    key = keyword.lower()
    return [
        row
        for row in rows
        if key in row["rel_path"].lower() or key in row["preview"].lower()
    ]


def _tui_input(stdscr: Any, prompt: str, initial: str = "") -> str:
    h, w = stdscr.getmaxyx()
    text = initial
    curses.curs_set(1)
    while True:
        stdscr.move(h - 1, 0)
        stdscr.clrtoeol()
        shown = truncate(f"{prompt}{text}", max(0, w - 1))
        stdscr.addnstr(h - 1, 0, shown, max(0, w - 1))
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13):
            curses.curs_set(0)
            return text.strip()
        if ch in (27,):
            curses.curs_set(0)
            return initial
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            text = text[:-1]
            continue
        if 32 <= ch <= 126:
            text += chr(ch)


def init_tui_colors() -> dict[str, int]:
    colors = {
        "title": 0,
        "meta": 0,
        "header": 0,
        "selected": 0,
        "selected_mark": 0,
        "dim": 0,
    }
    if not curses.has_colors():
        return colors
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_BLUE, -1)
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(5, curses.COLOR_GREEN, -1)
    curses.init_pair(6, curses.COLOR_WHITE, -1)
    colors["title"] = curses.color_pair(1) | curses.A_BOLD
    colors["meta"] = curses.color_pair(2)
    colors["header"] = curses.color_pair(3) | curses.A_BOLD
    colors["selected"] = curses.color_pair(4)
    colors["selected_mark"] = curses.color_pair(5) | curses.A_BOLD
    colors["dim"] = curses.color_pair(6) | curses.A_DIM
    return colors


def run_selection_tui(stdscr: Any, rows: list[dict[str, Any]]) -> List[int]:
    curses.curs_set(0)
    stdscr.keypad(True)
    palette = init_tui_colors()
    page = 0
    cursor_in_page = 0
    selected: set[int] = set()
    query = ""
    status = "↑/↓ move  n/p page  Enter/Space toggle  / filter  a all-visible  c clear"
    actions = "Actions: [N]ext page  [P]rev page  [E]xport selected  [Q]uit"
    notice = ""
    legend = "Msg=all events  P/S=primary/secondary events"

    while True:
        filtered = filter_rows(rows, query)
        h, w = stdscr.getmaxyx()
        table_top = 4
        table_bottom = h - 3
        page_size = TUI_PAGE_SIZE
        total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start = page * page_size
        page_rows = filtered[start : start + page_size]
        if not page_rows:
            cursor_in_page = 0
        else:
            cursor_in_page = max(0, min(cursor_in_page, len(page_rows) - 1))

        stdscr.erase()
        stdscr.addnstr(0, 0, "Claude Conversation Exporter", max(0, w - 1), palette["title"])
        stdscr.addnstr(
            1,
            0,
            truncate(
                f"Search: {query or '(none)'} | Total: {len(rows)} | Visible: {len(filtered)} | "
                f"Selected: {len(selected)} | Page: {page + 1}/{total_pages}",
                max(0, w - 1),
            ),
            max(0, w - 1),
            palette["meta"],
        )
        stdscr.addnstr(3, 0, truncate(legend, max(0, w - 1)), max(0, w - 1), palette["dim"])
        stdscr.addnstr(
            2,
            0,
            truncate("ID  Sel  Msg    P/S      Updated           Preview                             Path", max(0, w - 1)),
            max(0, w - 1),
            palette["header"] | curses.A_UNDERLINE,
        )

        for screen_i, row in enumerate(page_rows, start=table_top):
            marker = "[x]" if row["idx"] in selected else "[ ]"
            ps = f"{row['primary_events']}/{row['secondary_events']}"
            line = (
                f"{str(row['idx']).rjust(2)}  {marker}  "
                f"{str(row['events']).rjust(3)}  {ps.rjust(7)}  "
                f"{row['mtime']:<16}  "
                f"{row['preview']:<35}  "
                f"{compress_middle(row['rel_path'], 30):<30}"
            )
            is_current = (screen_i - table_top) == cursor_in_page and bool(page_rows)
            attr = palette["selected"] if is_current else 0
            if row["idx"] in selected and not is_current:
                attr = palette["selected_mark"]
            stdscr.addnstr(screen_i, 0, truncate(line, max(0, w - 1)), max(0, w - 1), attr)

        footer_row = min(h - 2, table_top + page_size + 1)
        action_row = min(h - 1, footer_row + 1)
        stdscr.addnstr(footer_row, 0, truncate(status, max(0, w - 1)), max(0, w - 1), palette["dim"])
        stdscr.addnstr(action_row, 0, truncate(actions, max(0, w - 1)), max(0, w - 1), palette["meta"])
        if notice:
            stdscr.addnstr(action_row, 0, truncate(notice, max(0, w - 1)), max(0, w - 1), palette["header"])
        stdscr.refresh()

        ch = stdscr.getch()
        notice = ""
        if ch in (ord("q"), ord("Q")):
            raise ExportError("Selection cancelled")
        if ch in (curses.KEY_UP, ord("k"), ord("K")):
            if page_rows:
                cursor_in_page = max(0, cursor_in_page - 1)
            continue
        if ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            if page_rows:
                cursor_in_page = min(len(page_rows) - 1, cursor_in_page + 1)
            continue
        if ch in (ord("n"), ord("N"), curses.KEY_NPAGE):
            if page < total_pages - 1:
                page += 1
                cursor_in_page = 0
            continue
        if ch in (ord("p"), ord("P"), curses.KEY_PPAGE):
            if page > 0:
                page -= 1
                cursor_in_page = 0
            continue
        if ch in (ord(" "), 10, 13):
            if page_rows:
                idx = page_rows[cursor_in_page]["idx"]
                if idx in selected:
                    selected.remove(idx)
                else:
                    selected.add(idx)
            continue
        if ch in (ord("a"), ord("A")):
            selected.update(row["idx"] for row in page_rows)
            continue
        if ch in (ord("c"), ord("C")):
            selected.clear()
            continue
        if ch == ord("/"):
            query = _tui_input(stdscr, "Filter> ", query)
            page = 0
            cursor_in_page = 0
            continue
        if ch in (ord("e"), ord("E")):
            if selected:
                return sorted(selected)
            notice = "No selection yet. Press Enter/Space to select items first."
            continue


def ensure_output_path(output: str | None) -> Path:
    if output:
        path = Path(output).expanduser().resolve()
        return path

    now = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path.cwd() / f"claude-conversations-{now}.html"


def safe_pre(text: str) -> str:
    return html.escape(text)


def is_long_text(text: str, max_chars: int = 900, max_lines: int = 20) -> bool:
    if len(text) > max_chars:
        return True
    return text.count("\n") + 1 > max_lines


def render_limited_text(text: str) -> str:
    safe = safe_pre(text)
    if not is_long_text(text):
        return f"<pre>{safe}</pre>"
    return (
        '<div class="clamped"><pre>'
        f"{safe}"
        '</pre></div><details class="expand-text"><summary>展开全文</summary>'
        f"<pre>{safe}</pre></details>"
    )


def render_html_message(event: dict[str, Any]) -> str:
    role = event["role"]
    timestamp = event["timestamp"]
    text = event["text"]
    role_class = role
    role_text = role.capitalize()
    timestamp_html = f'<span class="ts">{html.escape(timestamp)}</span>' if timestamp else ""
    header = (
        f'<div class="meta"><span class="role {role_class}">{html.escape(role_text)}</span>'
        f"{timestamp_html}</div>"
    )
    body = render_limited_text(text)
    return f'<article class="msg primary">{header}{body}</article>'


def category_badge(category: str) -> str:
    label = category.upper()
    return f'<span class="badge {html.escape(category)}">{html.escape(label)}</span>'


def subagent_step_kind(event: dict[str, Any]) -> str:
    role = str(event.get("role", "")).lower()
    raw = event.get("raw", {})
    call_name = str(event.get("call_name", "")).lower()
    text = str(event.get("text", "")).lower()
    raw_text = json.dumps(raw, ensure_ascii=False).lower()
    if role in {"user", "assistant"} and not has_structured_tool_payload(raw):
        return "message"
    if "skill" in call_name or "<command-name>/skill" in text or "/skill" in text:
        return "skill"
    if "mcp" in call_name or "mcp" in raw_text:
        return "mcp"
    if has_structured_tool_payload(raw):
        return "tool"
    if role in {"system", "progress", "file-history-snapshot"}:
        return "system"
    return "other"


def render_secondary_step(event: dict[str, Any], badge_category: str) -> str:
    role = event["role"]
    timestamp = event["timestamp"]
    text = event["text"].strip()
    ts = f'<span class="ts">{html.escape(timestamp)}</span>' if timestamp else ""
    call_name = event.get("call_name", "").strip()
    call_html = f' <span class="call-name">{html.escape(call_name)}</span>' if call_name else ""
    step_head = (
        f'<div class="step-head">{category_badge(badge_category)}{call_html} '
        f'<span class="step-role">{html.escape(role.capitalize())}</span>{ts}</div>'
    )
    raw_json = html.escape(json.dumps(event["raw"], ensure_ascii=False, indent=2))
    return (
        '<div class="step">'
        f"{step_head}"
        f"{render_limited_text(text or '(empty)')}"
        f'<details class="raw"><summary>Raw JSON</summary><pre>{raw_json}</pre></details>'
        "</div>"
    )


def render_subagent_group(events: list[dict[str, Any]]) -> str:
    flow_name = next((str(e.get("flow_name", "")).strip() for e in events if str(e.get("flow_name", "")).strip()), "")
    flow_hint = f' <span class="call-name">{html.escape(flow_name)}</span>' if flow_name else ""
    parts = [
        '<details class="msg secondary-group">',
        f"<summary>SUBAGENT {len(events)} 条 {category_badge('subagent')}{flow_hint}</summary>",
    ]

    pending: list[dict[str, Any]] = []
    pending_kind = ""
    pending_key = ""

    def flush_pending() -> None:
        nonlocal pending, pending_kind, pending_key
        if not pending:
            return
        if pending_kind == "message":
            for ev in pending:
                parts.append(render_secondary_step(ev, "subagent"))
        else:
            uniq_names: list[str] = []
            seen: set[str] = set()
            for ev in pending:
                name = str(ev.get("call_name", "")).strip()
                if name and name not in seen:
                    seen.add(name)
                    uniq_names.append(name)
            hint = ""
            if uniq_names:
                display = ", ".join(uniq_names[:3])
                if len(uniq_names) > 3:
                    display += "..."
                hint = f' <span class="call-name">{html.escape(display)}</span>'
            parts.append(
                '<details class="subagent-inner">'
                f"<summary>{pending_kind.upper()} {len(pending)} 条 {category_badge(pending_kind)}{hint}</summary>"
            )
            for ev in pending:
                parts.append(render_secondary_step(ev, pending_kind))
            parts.append("</details>")
        pending = []
        pending_kind = ""
        pending_key = ""

    for ev in events:
        kind = subagent_step_kind(ev)
        key = str(ev.get("call_name", "")).strip().lower() if kind in {"tool", "mcp", "skill"} else ""
        if not pending:
            pending = [ev]
            pending_kind = kind
            pending_key = key
            continue
        if pending_kind == kind and pending_key == key:
            pending.append(ev)
        else:
            flush_pending()
            pending = [ev]
            pending_kind = kind
            pending_key = key
    flush_pending()
    parts.append("</details>")
    return "".join(parts)


def render_secondary_group(events: list[dict[str, Any]], category: str) -> str:
    if category == "subagent":
        return render_subagent_group(events)

    call_names = [e.get("call_name", "") for e in events if e.get("call_name", "").strip()]
    uniq_names: list[str] = []
    seen: set[str] = set()
    for name in call_names:
        if name not in seen:
            seen.add(name)
            uniq_names.append(name)
    name_hint = ""
    if uniq_names:
        display = ", ".join(uniq_names[:3])
        if len(uniq_names) > 3:
            display += "..."
        name_hint = f' <span class="call-name">{html.escape(display)}</span>'
    summary = f"{category.upper()} {len(events)} 条"
    parts = ['<details class="msg secondary-group">', f"<summary>{summary} {category_badge(category)}{name_hint}</summary>"]
    for event in events:
        parts.append(render_secondary_step(event, event["category"]))
    parts.append("</details>")
    return "".join(parts)


def group_events_for_display(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    pending_secondary: list[dict[str, Any]] = []
    pending_category: str | None = None
    pending_group_name: str = ""

    def flush_secondary() -> None:
        nonlocal pending_secondary, pending_category, pending_group_name
        if pending_secondary and pending_category:
            blocks.append(
                {
                    "type": "secondary_group",
                    "category": pending_category,
                    "group_name": pending_group_name,
                    "events": pending_secondary,
                }
            )
        pending_secondary = []
        pending_category = None
        pending_group_name = ""

    def secondary_group_name(event: dict[str, Any]) -> str:
        category = str(event.get("category", ""))
        call_name = str(event.get("call_name", "")).strip().lower()
        # Split contiguous secondary events by concrete caller/tool identity.
        if category == "subagent":
            flow_name = str(event.get("flow_name", "")).strip().lower()
            if flow_name:
                return flow_name
            source_file = event.get("raw", {}).get("_source_file")
            if isinstance(source_file, str) and source_file.strip():
                return f"file:{Path(source_file).resolve()}"
            return "subagent"
        if category in {"tool", "mcp", "skill", "subagent"}:
            return call_name
        return ""

    for event in events:
        if event["secondary"]:
            cat = event["category"]
            grp_name = secondary_group_name(event)
            if pending_category is None or (pending_category == cat and pending_group_name == grp_name):
                pending_category = cat
                pending_group_name = grp_name
                pending_secondary.append(event)
            else:
                flush_secondary()
                pending_category = cat
                pending_group_name = grp_name
                pending_secondary.append(event)
            continue
        flush_secondary()
        blocks.append({"type": "primary", "event": event})
    flush_secondary()
    return blocks


def render_html(
    title: str,
    source: str,
    selected: list[tuple[Path, list[dict[str, Any]]]],
) -> str:
    sections = []
    for path, events in selected:
        blocks = group_events_for_display(events)
        rendered_blocks: list[str] = []
        for block in blocks:
            if block["type"] == "primary":
                rendered_blocks.append(render_html_message(block["event"]))
            else:
                rendered_blocks.append(
                    render_secondary_group(block["events"], block["category"])
                )
        msgs = "\n".join(rendered_blocks)
        section = (
            '<section class="conversation">'
            f"<h2>{html.escape(str(path))}</h2>"
            '<div class="conversation-actions">'
            '<button type="button" class="toggle" data-action="expand">展开子步骤</button>'
            '<button type="button" class="toggle" data-action="collapse">折叠子步骤</button>'
            "</div>"
            f"{msgs}"
            "</section>"
        )
        sections.append(section)

    body_sections = "\n".join(sections)
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #0d1117;
      --panel: #161b22;
      --card: #1c2128;
      --text: #e6edf3;
      --muted: #8b949e;
      --border: #30363d;
      --user: #58a6ff;
      --assistant: #3fb950;
      --system: #d2a8ff;
      --subagent: #f2cc60;
      --skill: #ff7b72;
      --mcp: #56d4dd;
      --tool: #ffa657;
      --other: #a5a5a5;
    }}
    body {{ margin: 0; background: radial-gradient(1200px 600px at 10% -10%, #1f2733, #0d1117); color: var(--text); font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .wrap {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    .hint {{ color: var(--muted); margin: 0 0 20px; }}
    .conversation {{ background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 16px; margin-bottom: 16px; box-shadow: 0 8px 26px rgba(0,0,0,0.35); }}
    h2 {{ margin: 0 0 10px; font-size: 16px; color: #c9d1d9; }}
    .conversation-actions {{ display: flex; gap: 8px; margin-bottom: 10px; }}
    .toggle {{ border: 1px solid var(--border); background: #21262d; color: #c9d1d9; border-radius: 8px; padding: 5px 10px; cursor: pointer; }}
    .msg {{ border: 1px solid var(--border); border-radius: 10px; background: var(--card); padding: 10px; margin: 10px 0; }}
    .meta {{ display: flex; gap: 10px; align-items: center; margin-bottom: 6px; }}
    .role {{ font-weight: 600; }}
    .role.user {{ color: var(--user); }}
    .role.assistant {{ color: var(--assistant); }}
    .role.system {{ color: var(--system); }}
    .role.subagent {{ color: var(--subagent); }}
    .role.skill {{ color: var(--skill); }}
    .role.mcp {{ color: var(--mcp); }}
    .role.tool {{ color: var(--tool); }}
    .role.other {{ color: var(--other); }}
    .ts {{ color: var(--muted); font-size: 12px; }}
    pre {{ white-space: pre-wrap; margin: 0; overflow-wrap: anywhere; font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Monaco, "Courier New", monospace; color: #dce3ea; }}
    details.secondary-group > summary {{ cursor: pointer; color: #c9d1d9; font-weight: 600; display: flex; align-items: center; gap: 8px; }}
    .badge {{ display: inline-block; border: 1px solid var(--border); border-radius: 999px; padding: 1px 8px; font-size: 11px; line-height: 1.4; }}
    .badge.tool {{ color: var(--tool); }}
    .badge.mcp {{ color: var(--mcp); }}
    .badge.skill {{ color: var(--skill); }}
    .badge.subagent {{ color: var(--subagent); }}
    .badge.system {{ color: var(--system); }}
    .badge.other {{ color: var(--other); }}
    .step {{ margin-top: 10px; padding: 10px; border: 1px dashed var(--border); border-radius: 8px; background: #131820; }}
    .step-head {{ display: flex; gap: 8px; align-items: center; margin-bottom: 6px; }}
    .step-role {{ color: #c9d1d9; font-weight: 600; font-size: 12px; }}
    .call-name {{ color: #8b949e; font-size: 12px; }}
    .clamped {{ max-height: 280px; overflow: hidden; position: relative; }}
    .clamped::after {{ content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 48px; background: linear-gradient(to bottom, rgba(22,27,34,0), rgba(22,27,34,1)); }}
    details.expand-text > summary {{ cursor: pointer; color: var(--muted); margin-top: 6px; }}
    details.raw {{ margin-top: 10px; }}
    details.raw > summary {{ cursor: pointer; color: var(--muted); font-weight: 500; }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1>{html.escape(title)}</h1>
    <p class=\"hint\">Source: {html.escape(source)}</p>
    {body_sections}
  </div>
  <script>
    function setSecondary(section, open) {{
      section.querySelectorAll('details.secondary-group').forEach((d) => {{ d.open = open; }});
    }}
    document.querySelectorAll('.conversation').forEach((section) => {{
      section.querySelectorAll('.toggle').forEach((btn) => {{
        btn.addEventListener('click', () => {{
          setSecondary(section, btn.dataset.action === 'expand');
        }});
      }});
    }});
  </script>
</body>
</html>
"""


def select_conversations(
    units: list[dict[str, Any]],
    args: argparse.Namespace,
    interactive: bool,
) -> list[int]:
    if len(units) == 1:
        return [1]
    if args.all:
        return list(range(1, len(units) + 1))
    if args.select:
        return parse_selection(args.select, set(range(1, len(units) + 1)))
    if interactive:
        previews = []
        counts = []
        primary_counts = []
        secondary_counts = []
        for unit in units:
            events = read_conversation_events(unit)
            previews.append(first_user_preview(events))
            counts.append(len(events))
            primary, secondary = count_primary_secondary(events)
            primary_counts.append(primary)
            secondary_counts.append(secondary)
        return prompt_selection(
            units,
            previews,
            counts,
            primary_counts,
            secondary_counts,
        )
    raise ExportError("Directory input requires --all or --select in non-interactive mode")


def maybe_prompt_output(path: Path, interactive: bool, provided: bool) -> Path:
    if provided or not interactive:
        return path

    default_str = str(path)
    raw = input(f"Output path (Enter to use {default_str}): ").strip()
    if not raw:
        return path

    chosen = Path(raw).expanduser().resolve()
    if chosen.suffix.lower() != ".html":
        chosen = chosen.with_suffix(".html")
    return chosen


def main() -> int:
    args = parse_args()
    input_path = Path(os.path.expanduser(args.input)).resolve()

    files = find_jsonl_files(input_path)
    if not files:
        print(f"No .jsonl files found in: {input_path}")
        return 1

    units = build_conversation_units(files)
    interactive = (not args.non_interactive) and os.isatty(0)

    try:
        chosen_indexes = select_conversations(units, args, interactive)
    except ExportError as exc:
        print(f"Error: {exc}")
        return 2

    selected_items: list[tuple[Path, list[dict[str, Any]]]] = []
    for idx in chosen_indexes:
        unit = units[idx - 1]
        path = unit["primary_file"]
        merged_events = read_conversation_events(unit)
        normalized = [normalize_event(event) for event in merged_events]
        selected_items.append((path, normalized))

    output_path = ensure_output_path(args.output)
    output_path = maybe_prompt_output(output_path, interactive, args.output is not None)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    content = render_html(args.title, str(input_path), selected_items)

    output_path.write_text(content, encoding="utf-8")
    print(f"Exported {len(selected_items)} conversation(s) to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
